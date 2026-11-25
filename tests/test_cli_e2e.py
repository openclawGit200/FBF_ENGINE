"""端到端測試：**人類 CLI ＋ MCP 一起跑**。

這是「真的照著 README 走一遍」的驗證：
`init` → `requirement confirm` → `role add`（角色席位）→ MCP `fbflow_register`
→ 建工單 → 派工 → 交件 → `engine status` → `awaiting list` → `project close`。

CLI 全部走 `subprocess`（真的開行程），MCP 走行程內呼叫。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from fbf_engine import engine, mcp_server, paths, platform, store, workflow  # noqa: E402

CLI = os.path.join(PKG, "fbf.py")

PLANNER = "ses_planner"
THINKER = "ses_thinker"
WORKER = "ses_worker"


def fake_verifier(endpoint, session_id, message_id, task_id,
                  dispatched_at=None, submitted_at=None, binary="opencode"):
    return {"verified": True, "reason": "", "platform": "test",
            "message_id": message_id, "session_id": session_id,
            "model_id": "test/model", "context_id": "ctx-" + str(message_id),
            "finish": "stop", "detail": {}}


def fake_context_verifier(endpoint, context_id, parent_session_id, binary):
    return {"verified": True, "reason": "", "platform": "test",
            "context_id": context_id, "model_id": "test/model", "finish": "stop",
            "detail": {"via": "test"}}


class TestInstallerConfig(unittest.TestCase):
    """安裝器產生的設定要**真的**能啟動 MCP。

    真實踩坑：設定檔原本把啟動命令寫死成 `python3`，Windows 上沒有這個命令 →
    OpenCode 載入直接失敗。這裡用「產生出來的設定」原樣啟動一次，
    確認握手與工具清單都正常（規格第 11 節的介面契約）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-install-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, "opencode.json")

    def _install(self, *extra, expect_ok=True):
        proc = subprocess.run(
            [sys.executable, CLI, "install", "--config", self.config] + list(extra),
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if expect_ok:
            self.assertEqual(proc.returncode, 0,
                             "安裝失敗：%s\nstdout=%s\nstderr=%s"
                             % (extra, proc.stdout, proc.stderr))
        payload = None
        text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"raw": text}
        entry = None
        if os.path.isfile(self.config):
            with open(self.config, encoding="utf-8") as fh:
                data = json.load(fh)
            entry = data.get("mcp", {}).get("servers", {}).get("fbf-engine")
        return proc.returncode, payload, entry

    def test_installed_command_is_the_running_interpreter(self):
        """命令必須是**正在跑這個安裝器的那個 Python**，不能寫死 `python3`。"""
        _, _, entry = self._install()
        self.assertEqual(entry["command"][0], sys.executable)
        self.assertEqual(entry["command"][1:], ["-m", "fbf_engine.mcp_server"])

    def test_installed_config_actually_starts_the_server(self):
        """用產生出來的設定原樣啟動 → initialize 握手 → 9 個工具。

        **在非 UTF-8 的環境啟動**（模擬預設 Windows 主控台編碼）：
        stdio 傳輸必須是 UTF-8（MCP 契約），不能由主機端的主控台字碼頁決定——
        否則回應裡的中文會變成主機端解不掉的位元組，握手直接失敗。
        """
        _, _, entry = self._install()
        env = os.environ.copy()
        env.update(entry.get("environment") or {})
        # 預設 Windows 環境不是 UTF-8（cp950／cp437…），也不是 UTF-8 模式
        env["PYTHONIOENCODING"] = "latin-1"
        env["PYTHONUTF8"] = "0"
        requests = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]) + "\n"
        proc = subprocess.Popen(entry["command"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            out, err = proc.communicate(requests.encode("utf-8"), timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("產生的設定啟動後沒有回應：%s" % entry["command"])
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError:
            self.fail("產生的設定吐出的不是 UTF-8（stdio 契約要求 UTF-8）："
                      "command=%s\nstdout=%r\nstderr=%r"
                      % (entry["command"], out[:200], err[:200]))
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2,
                                "啟動後沒有回應兩則 JSON-RPC：%s\nstderr=%s"
                                % (text, err))
        initialize = json.loads(lines[0])
        self.assertIn("result", initialize)
        self.assertIn("fbflow_register", initialize["result"]["instructions"])
        tools = json.loads(lines[1])
        names = [t["name"] for t in tools["result"]["tools"]]
        # 12 個：10 個既有工具 ＋ `fbflow_project_join`（新專案加入引擎的入口）
        # ＋ `fbflow_work_supersede`（需求變了 → 開新工單、舊單標成已取代）。
        self.assertEqual(len(names), 12, "工具數不對：%s" % names)
        self.assertIn("fbflow_status", names)

    def test_cross_host_install_requires_the_target_python(self):
        """替另一台主機產生設定時，沒指定目標主機的 Python 就拒絕。

        本機的直譯器在那台機器上不存在——寧可拒絕，也不寫一份跑不起來的設定。
        """
        code, payload, entry = self._install("--host-root", "cross-host-root",
                                             expect_ok=False)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload.get("code"), "usage")
        self.assertIsNone(entry)

    def test_cross_host_install_uses_the_given_python(self):
        """明確指定了就用它。"""
        _, _, entry = self._install("--host-root", "cross-host-root",
                                    "--python", "python3")
        self.assertEqual(entry["command"][0], "python3")

    def test_cli_output_is_utf8_in_any_environment(self):
        """CLI 的輸出必須是 UTF-8，不看主控台字碼頁。

        Windows 預設是 cp950；不強制就會吐出 UTF-8 讀取端解不掉的位元組，
        中文欄位變成亂碼（CLI 的輸出不只是給人看的）。
        """
        env = os.environ.copy()
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONUTF8"] = "0"
        config = os.path.join(self.tmp, "cli-utf8.json")
        proc = subprocess.run([sys.executable, CLI, "install", "--config", config],
                              capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        try:
            text = proc.stdout.decode("utf-8")
        except UnicodeDecodeError:
            self.fail("CLI 輸出不是 UTF-8：%r" % proc.stdout[:200])
        self.assertIn("這是全域安裝", text)

    def test_join_uses_the_running_interpreter(self):
        """專案加入這個 MCP 時寫的設定，命令也要是實際可用的 Python。

        `join` 是多數專案實際會走的那條路（全域 `install` 只是入口）。
        """
        project = os.path.join(self.tmp, "proj")
        proc = subprocess.run([sys.executable, CLI, "join", "--root", project,
                               "--name", "測試專案",
                               "--endpoint", "http://127.0.0.1:4096"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        self.assertEqual(proc.returncode, 0,
                         "join 失敗：stdout=%s\nstderr=%s" % (proc.stdout, proc.stderr))
        payload = json.loads(proc.stdout)
        with open(os.path.join(project, "opencode.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        entry = data["mcp"]["servers"]["fbf-engine"]
        self.assertEqual(entry["command"][0], sys.executable)
        self.assertIn(mcp_server.ENV_PROJECT_ROOT, entry["environment"])
        # Claude Code 讀專案層 .mcp.json——只寫 opencode.json 不夠（上一輪實測缺口）
        with open(os.path.join(project, ".mcp.json"), encoding="utf-8") as fh:
            claude = json.load(fh)
        centry = claude["mcpServers"]["fbf-engine"]
        self.assertEqual(centry["command"], sys.executable)
        self.assertEqual(centry["args"], ["-m", "fbf_engine.mcp_server"])
        self.assertIn(mcp_server.ENV_PROJECT_ROOT, centry["env"])
        self.assertIn("requirement confirm", payload["note"])
        self.assertIn("GRILL", payload["note"])


class TestResidentConcurrency(unittest.TestCase):
    """背景 tick 執行緒與 MCP 請求執行緒**不能共用同一條 sqlite3 連線**。

    實測（macOS，2026-11-19）：共用連線時，**開 resident** 的伺服器在 stdio
    壓測下 3/3 次 SIGSEGV（沒有 Python traceback），**關 resident** 的對照組
    跑一百多萬次工具呼叫零崩潰。CPython 用 `SQLITE_OPEN_NOMUTEX` 開連線，
    底層不再替這條連線序列化 → 兩個執行緒同時用是未定義行為。

    修法是**每個執行緒一條連線**。這條測試守住修好後的樣子：
    壓測之後正本仍然完整、伺服器仍然活著。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-race-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project, exist_ok=True)
        env = dict(os.environ, PYTHONPATH=PKG)
        # 用 `join` 建立：它會同時建正本與三個角色席位（`init` 只建正本）
        subprocess.run([sys.executable, CLI, "join", "--root", self.project,
                        "--name", "race", "--endpoint", "http://127.0.0.1:4096"],
                       capture_output=True, env=env)

    def _flood(self, seconds: float):
        """開著 resident 壓 stdio。回傳 (exit code, 工具呼叫數, stderr)。"""
        env = dict(os.environ)
        env["PYTHONPATH"] = PKG
        env["FBF_PROJECT_ROOT"] = self.project
        # 常駐的 tick 會走 auto-enroll 查宿主 → 指到不存在的 CLI，掃描回空清單
        env["FBF_HOST_CLI"] = "/nonexistent-host-cli"
        proc = subprocess.Popen([sys.executable, "-m", "fbf_engine.mcp_server",
                                 "--interval", "5"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        request = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "fbflow_status", "arguments": {}}}
                               ) + "\n").encode("utf-8")
        count = [0]
        stop = threading.Event()

        def reader():
            while True:
                if not proc.stdout.readline():
                    break
                count[0] += 1

        def writer():
            while not stop.is_set():
                try:
                    proc.stdin.write(request)
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    break

        threads = [threading.Thread(target=reader, daemon=True),
                   threading.Thread(target=writer, daemon=True)]
        for thread in threads:
            thread.start()
        time.sleep(seconds)
        stop.set()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = "timeout"
        err = proc.stderr.read().decode("utf-8", "replace")
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        return code, count[0], err

    def test_resident_tick_keeps_the_canonical_db_intact(self):
        """壓測（resident 在跑 tick）之後：正本完整、伺服器活著、正常結束。"""
        code, calls, err = self._flood(12.0)
        self.assertGreater(calls, 500, "壓測量太少，沒有真的跟 tick 撞在一起")
        self.assertEqual(code, 0, "伺服器不正常結束（%s）：%s" % (code, err[-800:]))
        conn = store.connect(self.project)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            self.assertEqual(row[0], "ok", "正本損毀：%s" % row[0])
            roles = conn.execute("SELECT COUNT(*) AS n FROM roles").fetchone()["n"]
            self.assertEqual(roles, 3, "角色席位不見了")
        finally:
            conn.close()


class TestCliEndToEnd(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project, exist_ok=True)
        self._old_root = os.environ.get(mcp_server.ENV_PROJECT_ROOT)
        os.environ[mcp_server.ENV_PROJECT_ROOT] = self.project
        self.addCleanup(self._restore_env)
        # **測試絕不碰真的 OpenCode**（這台機器有 opencode shim，會真的把它拉起來）。
        self._real_send = platform.send_to_session
        self._real_active = platform.active_sessions
        self._real_host_scan = platform.hostterm.scan_sessions
        platform.send_to_session = lambda *a, **k: {"state": "queued", "channel": "test"}
        platform.active_sessions = lambda *a, **k: []
        # auto-enroll 會真的查宿主 → 擋掉（要測 enroll 的測試自己換替身）
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: []
        self.addCleanup(self._restore_platform)

    def _restore_platform(self):
        platform.send_to_session = self._real_send
        platform.active_sessions = self._real_active
        platform.hostterm.scan_sessions = self._real_host_scan

    def _restore_env(self):
        if self._old_root is None:
            os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        else:
            os.environ[mcp_server.ENV_PROJECT_ROOT] = self._old_root

    def cli(self, *args, expect_ok=True):
        # `errors="strict"`：亂碼要讓測試紅，不要用 replace 掩蓋編碼問題
        proc = subprocess.run([sys.executable, CLI, "--project", self.project] + list(args),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="strict")
        if expect_ok:
            self.assertEqual(proc.returncode, 0,
                             "指令失敗：%s\nstdout=%s\nstderr=%s"
                             % (args, proc.stdout, proc.stderr))
        payload = None
        text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"raw": text}
        return proc.returncode, payload

    def mcp(self):
        server = mcp_server.MCPServer(resident=False)
        coordinator = server.coordinator_for(self.project)
        coordinator.verifier = fake_verifier
        coordinator.context_verifier = fake_context_verifier
        self.addCleanup(server.close)
        return server, coordinator

    def call(self, server, name, args, session):
        params = {"_meta": {mcp_server.META_SESSION_KEY: session}}
        return server.call_tool(name, args, params)

    # ------------------------------------------------------------ 步驟

    def test_full_cli_then_mcp_flow(self):
        # 1) init
        code, out = self.cli("init", "--name", "E2E", "--code", "PRJ-E2E01")
        self.assertTrue(out["ok"])
        self.assertEqual(out["project_code"], "PRJ-E2E01")

        # 2) requirement confirm（建立並確認）
        code, out = self.cli("requirement", "confirm", "--text", "做出燈具",
                             "--criterion", "燈亮著且雜湊相符")
        self.assertTrue(out["ok"])
        requirement_id = out["requirement"]["id"]
        self.assertIsNotNone(out["requirement"]["confirmed_at"])

        # 3) role add（**角色席位**：人類建，session 才認領）
        for role, short in (("PLANNER", "P1"), ("THINKER", "T1"), ("WORKER", "W1")):
            code, out = self.cli("role", "add", "--role", role, "--short-name", short)
            self.assertTrue(out["ok"], out)

        # 4) MCP：三個 session 各自認領席位
        server, coordinator = self.mcp()
        uuids = {}
        for session, role, short in ((PLANNER, "PLANNER", "P1"),
                                     (THINKER, "THINKER", "T1"),
                                     (WORKER, "WORKER", "W1")):
            result = self.call(server, "fbflow_register",
                               {"role": role, "short_name": short}, session)
            self.assertTrue(result["ok"], result)
            uuids[session] = result["data"]["session_uuid"]

        # 5) 建里程碑 → 建工單
        result = self.call(server, "fbflow_milestone_create",
                           {"seq": 1, "name": "A", "outcome": "做出燈具"}, PLANNER)
        self.assertTrue(result["ok"], result)
        milestone_id = result["data"]["milestone_id"]

        result = self.call(server, "fbflow_work_create",
                           {"title": "做出燈具本體", "requirement_id": requirement_id,
                            "content": "做一個會亮的燈具", "milestone_id": milestone_id},
                           PLANNER)
        self.assertTrue(result["ok"], result)
        work_id = result["data"]["work_id"]
        self.assertEqual(result["data"]["state"], workflow.BACKLOG)

        # 6) 派工（tick）
        coordinator.tick()
        row = coordinator.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'", (work_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["stage"], workflow.DESIGN)

        # 7) THINKER 交件
        version = coordinator.conn.execute(
            "SELECT version FROM works WHERE id = ?", (work_id,)).fetchone()["version"]
        result = self.call(server, "fbflow_report_work", {
            "task_id": row["id"], "work_id": work_id, "expected_version": version,
            "stage": workflow.DESIGN, "message_id": "m-design",
            "session_uuid": uuids[THINKER],
            "payload": {
                "solutions": [
                    {"subagent_context_id": "c1", "approach": "甲", "cost": "低",
                     "change_scope": "小", "effort": "少", "risk": "低",
                     "verifiable_by": "跑測試", "reuses": "無"},
                    {"subagent_context_id": "c2", "approach": "乙", "cost": "高",
                     "change_scope": "大", "effort": "多", "risk": "中",
                     "verifiable_by": "跑測試", "reuses": "無"},
                ],
                "design_note": "兩案比較",
                "handoff": {"ROLE": "THINKER"},
            }}, THINKER)
        self.assertTrue(result["ok"], result)
        coordinator.tick()
        state = coordinator.conn.execute(
            "SELECT state FROM works WHERE id = ?", (work_id,)).fetchone()["state"]
        self.assertEqual(state, workflow.ACCEPTANCE_DESIGN)

        # 8) engine status（CLI 讀得到同一份正本）
        code, out = self.cli("engine", "status")
        self.assertTrue(out["ok"])
        self.assertEqual(out["works_by_state"][workflow.ACCEPTANCE_DESIGN], 1)

        # 9) awaiting list（此時應為空）
        code, out = self.cli("awaiting", "list")
        self.assertTrue(out["ok"])
        self.assertEqual(out["awaiting"], [])

        # 10) project close 必須被雙向追溯擋下
        code, out = self.cli("project", "close", "--confirmed-by", "tester", expect_ok=False)
        self.assertNotEqual(code, 0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "traceability.incomplete")

    def test_role_slots_are_created_unclaimed(self):
        self.cli("init", "--code", "PRJ-E2E02")
        for role, short in (("PLANNER", "P1"), ("THINKER", "T1")):
            code, out = self.cli("role", "add", "--role", role, "--short-name", short)
            self.assertTrue(out["ok"], out)
        code, out = self.cli("role", "list")
        self.assertTrue(out["ok"])
        roles = {r["short_name"]: r for r in out["roles"]}
        self.assertEqual(roles["P1"]["claimed"], False)
        self.assertEqual(roles["P1"]["session_id"], "")

    def test_cli_prints_contract_table(self):
        code, out = self.cli("contract")
        self.assertTrue(out["ok"])
        self.assertIn("acceptance_design", out["contract"])
        self.assertIn("rework_design", out["contract"])

    def test_init_refuses_to_overwrite(self):
        self.cli("init", "--name", "E2E")
        code, out = self.cli("init", "--name", "E2E again", expect_ok=False)
        self.assertNotEqual(code, 0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "project.exists")

    def test_requirement_confirm_by_id(self):
        self.cli("init")
        code, out = self.cli("requirement", "confirm", "--text", "T", "--criterion", "C")
        requirement_id = out["requirement"]["id"]
        # 取消確認 → 再確認
        conn = store.connect(self.project)
        try:
            with store.tx(conn):
                conn.execute("UPDATE requirements SET confirmed_at = NULL WHERE id = ?",
                             (requirement_id,))
        finally:
            conn.close()
        code, out = self.cli("requirement", "confirm", "--id", requirement_id)
        self.assertTrue(out["ok"])
        self.assertIsNotNone(out["requirement"]["confirmed_at"])

    def test_work_create_requires_confirmed_requirement(self):
        self.cli("init", "--code", "PRJ-E2E03")
        conn = store.connect(self.project)
        try:
            with store.tx(conn):
                conn.execute(
                    "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                    " created_at) VALUES ('R-001','t','c',NULL,?)", (store.now(),))
                conn.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at)"
                    " VALUES ('PLANNER','P1',NULL,'opencode','active','','',?)",
                    (store.now(),))
        finally:
            conn.close()
        server, coordinator = self.mcp()
        # 認領席位（PLANNER）
        result = self.call(server, "fbflow_register",
                           {"role": "PLANNER", "short_name": "P1"}, PLANNER)
        self.assertTrue(result["ok"], result)
        # 需求還沒被人類確認 → 不准建工單
        result = self.call(server, "fbflow_work_create",
                           {"title": "x", "requirement_id": "R-001", "content": "y"},
                           PLANNER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_WORK_REJECTED)

    # ------------------------------------------------------------ 席位的出口

    def test_join_without_endpoint_still_works(self):
        """endpoint 是**選用的**：預設讓 opencode CLI 自己找到本機的 server。

        OpenCode 的埠號每次重開都會變 → 寫死一定會壞，所以預設不寫。
        """
        proc = subprocess.run([sys.executable, CLI, "join", "--root", self.project],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["opencode_endpoint"], "")

    def test_join_with_endpoint_writes_it(self):
        proc = subprocess.run([sys.executable, CLI, "join", "--root", self.project,
                               "--endpoint", "http://127.0.0.1:4096"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["opencode_endpoint"], "http://127.0.0.1:4096")
        conn = store.connect(self.project)
        try:
            self.assertEqual(
                store.get_setting(conn, "opencode_endpoint", ""), "http://127.0.0.1:4096")
        finally:
            conn.close()

    def test_engine_status_shows_dispatch_warnings(self):
        """卡住時要講得出來：endpoint 狀態 + 還有多少筆在 outbox 等。"""
        self.cli("init")
        code, out = self.cli("engine", "status")
        self.assertTrue(out["ok"], out)
        self.assertIn("opencode_endpoint", out)
        self.assertTrue(out["warnings"], "應該有關於派工的提示")

    def test_seat_can_be_released_so_a_new_session_takes_over(self):
        """席位被一個中斷的 session 占住 → 人能釋放 → 新的 session 接管。

        以前 `session bind` 對已存在的席位會撞 UNIQUE 約束，
        而 `session revoke` 只改 status、留著 session_id → 沒有任何出口。
        """
        self.cli("init")
        code, out = self.cli("role", "add", "--role", "PLANNER", "--short-name", "P9")
        self.assertTrue(out["ok"], out)
        code, out = self.cli("session", "bind", "--session-id", "ses_dead",
                             "--role", "PLANNER", "--short-name", "P9")
        self.assertTrue(out["ok"], out)

        # 另一個 session 認不到（而且訊息要告訴它出口在哪）
        code, out = self.cli("session", "bind", "--session-id", "ses_new",
                             "--role", "PLANNER", "--short-name", "P9", expect_ok=False)
        self.assertEqual(out["code"], "ROLE-SLOT-TAKEN")
        self.assertIn("role release", out["message"])

        # 人釋放席位
        code, out = self.cli("role", "release", "--role", "PLANNER",
                             "--short-name", "P9", "--reason", "舊 session 中斷")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["released_session"], "ses_dead")

        # 新的 session 認得到
        code, out = self.cli("session", "bind", "--session-id", "ses_new",
                             "--role", "PLANNER", "--short-name", "P9")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["session_id"], "ses_new")

    def test_revoke_frees_the_seat(self):
        """`session revoke` 以前留著 session_id → 席位永遠占著。"""
        self.cli("init")
        code, out = self.cli("role", "add", "--role", "WORKER", "--short-name", "W9")
        self.assertTrue(out["ok"], out)
        code, out = self.cli("session", "bind", "--session-id", "ses_x",
                             "--role", "WORKER", "--short-name", "W9")
        self.assertTrue(out["ok"], out)
        code, out = self.cli("session", "revoke", "--session-id", "ses_x")
        self.assertTrue(out["ok"], out)
        code, out = self.cli("role", "list")
        seat = [r for r in out["roles"] if r["short_name"] == "W9"][0]
        self.assertFalse(seat["claimed"], "撤銷後席位還是被占著")
        # 釋放後新的 session 認得到
        code, out = self.cli("session", "bind", "--session-id", "ses_y",
                             "--role", "WORKER", "--short-name", "W9")
        self.assertTrue(out["ok"], out)

    def test_revoke_drops_undelivered_dispatch(self):
        """撤銷 session → 還沒送出去的派工要一併清掉。

        收件人已死卻留著派工 → 每 30 秒重查一次、一直累積 `unknown`
        （實測累積 27 次），還可能誤把工單標成待人處理。
        """
        self.cli("init")
        code, out = self.cli("session", "bind", "--session-id", "ses_x",
                             "--role", "WORKER", "--short-name", "W9")
        self.assertTrue(out["ok"], out)
        conn = store.connect(self.project)
        try:
            with store.tx(conn):
                conn.execute(
                    "INSERT INTO outbox(task_id, kind, recipient_session,"
                    " recipient_role, target_id, payload, status, attempts,"
                    " deferrals, created_at, updated_at)"
                    " VALUES ('T-999','dispatch','ses_x','WORKER','W-999','{}',"
                    " 'unknown', 1, 0, ?, ?)", (store.now(), store.now()))
        finally:
            conn.close()
        code, out = self.cli("session", "revoke", "--session-id", "ses_x")
        self.assertTrue(out["ok"], out)
        self.assertGreaterEqual(out.get("dropped_outbox", 0), 1)
        conn = store.connect(self.project)
        try:
            row = conn.execute(
                "SELECT status FROM outbox WHERE task_id = 'T-999'").fetchone()
            self.assertEqual(row["status"], "failed",
                             "撤銷後還沒送出的派工沒被清掉")
        finally:
            conn.close()


class TestCliWrongRoot(unittest.TestCase):
    """**指令讀寫錯的正本**——「釋放跟認領讀不同地方」的根因（2026-11-24 實測）。

    實測：在 MCP 套件根目錄跑 `fbf.py role release --role PLANNER --short-name P1`
    → 舊版會在該目錄**憑空建立**一份空正本，回報 `ok:true, released_session:""`
    （看起來釋放成功），但真正的專案席位原封不動 → 新 session 重新認領照樣
    被 `ROLE-SLOT-TAKEN` 擋下。人在錯誤的地方操作，卻拿到「成功」的回應。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-wrongroot-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.not_project = os.path.join(self.tmp, "not-a-project")
        os.makedirs(self.not_project, exist_ok=True)
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project, exist_ok=True)
        self._old_root = os.environ.get(mcp_server.ENV_PROJECT_ROOT)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_root is None:
            os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        else:
            os.environ[mcp_server.ENV_PROJECT_ROOT] = self._old_root

    def _run(self, cwd, *args):
        proc = subprocess.run([sys.executable, CLI] + list(args),
                              cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env={k: v for k, v in os.environ.items()
                                   if k != mcp_server.ENV_PROJECT_ROOT})
        payload = None
        text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"raw": text}
        return proc.returncode, payload

    def test_release_in_a_non_project_dir_is_refused_and_creates_nothing(self):
        """**不建立正本**——錯的地方不該被寫出一份正本。"""
        code, out = self._run(self.not_project, "role", "release",
                              "--role", "PLANNER", "--short-name", "P1")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out["code"], "project.not-found", out)
        self.assertFalse(
            os.path.exists(os.path.join(self.not_project, store.DB_FILENAME)),
            "在非專案目錄憑空建立了正本")

    def test_explicit_wrong_project_is_refused(self):
        """明確指到的目錄若不是專案 → 同樣報錯（不猜、不建立）。"""
        code, out = self._run(self.not_project, "--project", self.not_project,
                              "role", "release", "--role", "PLANNER",
                              "--short-name", "P1")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out["code"], "project.not-found", out)
        self.assertFalse(
            os.path.exists(os.path.join(self.not_project, store.DB_FILENAME)))

    def test_release_reports_which_canonical_it_touched(self):
        """回報要**說出是哪一份正本**——不然「ok」無從判斷有沒有作用。"""
        proc = subprocess.run(
            [sys.executable, CLI, "--project", self.project, "init"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(
            [sys.executable, CLI, "--project", self.project, "role", "add",
             "--role", "PLANNER", "--short-name", "P1"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, out = self._run(self.not_project, "--project", self.project,
                              "role", "release", "--role", "PLANNER",
                              "--short-name", "P1")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.path.realpath(out["project_root"]),
                         os.path.realpath(self.project))

    def test_releasing_an_empty_seat_says_nothing_was_released(self):
        """席位本來就空著 → 要說清楚，不能看起來像「釋放了什麼」。"""
        subprocess.run([sys.executable, CLI, "--project", self.project, "init"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
        subprocess.run([sys.executable, CLI, "--project", self.project, "role",
                        "add", "--role", "PLANNER", "--short-name", "P1"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
        code, out = self._run(self.not_project, "--project", self.project,
                              "role", "release", "--role", "PLANNER",
                              "--short-name", "P1")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["released_session"], "")
        self.assertFalse(out["was_occupied"])
        self.assertIn("沒有東西可釋放", out["note"])

    def test_mcp_root_cannot_hold_a_project(self):
        """**MCP 根目錄不記任何專案**（規格：安裝狀態必須與專案無關）。"""
        code, out = self._run(self.not_project, "--project", paths.mcp_root(),
                              "init")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out["code"], "project.root-is-mcp", out)
        self.assertFalse(
            os.path.exists(os.path.join(paths.mcp_root(), store.DB_FILENAME)),
            "MCP 根目錄不該有專案正本")

    def test_project_option_works_after_the_subcommand(self):
        """`--project` 放在子命令**後面**也要有效。

        文件與習慣寫法都是 `fbf.py role release --project <專案>`；
        原本 `--project` 只能放在子命令前面 → 照文件寫會直接報
        「unrecognized arguments」。
        """
        proc = subprocess.run(
            [sys.executable, CLI, "--project", self.project, "init"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, out = self._run(self.not_project, "role", "add",
                              "--role", "PLANNER", "--short-name", "P1",
                              "--project", self.project)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"], out)
        # 放在子命令前面也要有效（兩邊都有 → 子命令後面的值優先）
        code, out = self._run(self.not_project, "--project", self.not_project,
                              "role", "list", "--project", self.project)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["roles"]), 1, "讀到的不是指定的專案")


if __name__ == "__main__":
    unittest.main()
