"""FBF ENGINE 的 MCP 介面層測試。

這裡測的是**契約**：回應外殼、欄位白名單、角色白名單、版本檢查、
**host 綁定身分**、多專案解析、以及同一組參數重送要冪等。

**沒有測真實 OpenCode**——真實 TUI／回執／多 session 接力必須標
`REAL_PASS` 或 `NOT_RUN`，不可用 mock 冒充（規格第 10 節）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import engine, mcp_server, paths, platform, prompts, store, workflow  # noqa: E402

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


class MCPHarness(unittest.TestCase):
    """一個暫存專案 ＋ 一個 MCP 伺服器。

    **專案是綁在 `FBF_PROJECT_ROOT` 的**（等同專案層 opencode.json 的設定），
    所以測試不會碰到全域註冊表。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-mcp-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_root = os.environ.get(mcp_server.ENV_PROJECT_ROOT)
        self._old_token = os.environ.get(mcp_server.ENV_SESSION_TOKEN)
        os.environ[mcp_server.ENV_PROJECT_ROOT] = self.tmp

        self.server = mcp_server.MCPServer(resident=False)
        # 先把專案正本建好（**MCP 不會自動建立**——那是不該發生的副作用）
        seed = store.connect(self.tmp)
        store.init_db(seed)
        seed.close()
        self.coordinator = self.server.coordinator_for(self.tmp)
        self.coordinator.verifier = fake_verifier
        self.coordinator.context_verifier = fake_context_verifier
        self.addCleanup(self.server.close)
        self.addCleanup(self._restore_env)
        self.conn = self.coordinator.conn
        self.uuids = {}

        # host CLI 掃描要擋掉——`tick()` 的 auto-enroll 會把**真的**宿主 session
        # 填進測試專案的空席位，測試自己的 session 就認不到席位、
        # 派工也派到真的 session（實測踩過：整批測試紅在「task 不是派給你的」）。
        self._real_host_scan = platform.hostterm.scan_sessions
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: []
        self.addCleanup(self._restore_platform)

        # 角色席位（人類先建，session 才認領）
        with store.tx(self.conn):
            for role, short in (("PLANNER", "P1"), ("THINKER", "T1"), ("WORKER", "W1")):
                self.conn.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at) VALUES (?,?,NULL,?, 'active', '', '', ?)",
                    (role, short, "opencode", store.now()))
            self.conn.execute(
                "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                " created_at) VALUES ('R-001','做東西','做出來',?,?)",
                (store.now(), store.now()))

    def _restore_env(self):
        for key, value in ((mcp_server.ENV_PROJECT_ROOT, self._old_root),
                           (mcp_server.ENV_SESSION_TOKEN, self._old_token)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _restore_platform(self):
        platform.hostterm.scan_sessions = self._real_host_scan

    # ------------------------------------------------------------ 工具

    def call(self, name, args, session=PLANNER, token=None):
        """用 **host 綁定**（`_meta` session id）呼叫。

        寫入工具會自動帶上這個 session 在註冊時拿到的 UUID
        （規格：UUID 是附加校驗，登記了就必須帶）。
        """
        if name in ("fbflow_report_work", "fbflow_report_issue",
                    "fbflow_report_milestone"):
            args = dict(args)
            args.setdefault("session_uuid", self.uuids.get(session, ""))
        params = {"_meta": {mcp_server.META_SESSION_KEY: session}} if session else {}
        if token:
            os.environ[mcp_server.ENV_SESSION_TOKEN] = token
        else:
            os.environ.pop(mcp_server.ENV_SESSION_TOKEN, None)
        return self.server.call_tool(name, args, params)

    def register(self, session, role, short_name):
        result = self.call("fbflow_register",
                           {"role": role, "short_name": short_name}, session=session)
        if result.get("ok"):
            self.uuids[session] = result["data"]["session_uuid"]
        return result

    def bind_all(self):
        self.register(PLANNER, "PLANNER", "P1")
        self.register(THINKER, "THINKER", "T1")
        self.register(WORKER, "WORKER", "W1")

    def make_work(self):
        return self.coordinator.create_work(
            "測試工單", "R-001", "內容", [])


# ---------------------------------------------------------------- 身分

class TestIdentity(MCPHarness):

    def test_missing_host_session_blocks_write_tools(self):
        """沒有 host 綁定的 session id → 寫入工具關閉。"""
        result = self.call("fbflow_work_create",
                           {"title": "x", "requirement_id": "R-001", "content": "y"},
                           session=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_SESSION_UNVERIFIED)

    def test_register_binds_host_session_and_issues_uuid(self):
        result = self.register(PLANNER, "PLANNER", "P1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["session_id"], PLANNER)
        self.assertTrue(result["data"]["session_uuid"])
        self.assertIn("PLANNER", result["data"]["prompt"])
        # UUID 明文不得落在資料庫
        row = self.conn.execute(
            "SELECT uuid_sha256 FROM roles WHERE session_id = ?", (PLANNER,)).fetchone()
        self.assertNotEqual(row["uuid_sha256"], result["data"]["session_uuid"])
        self.assertEqual(len(row["uuid_sha256"]), 64)

    def test_register_refuses_slot_taken_by_other_session(self):
        self.register(PLANNER, "PLANNER", "P1")
        result = self.register(THINKER, "PLANNER", "P1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ROLE-SLOT-TAKEN")

    def test_register_unknown_slot_rejected(self):
        result = self.register(PLANNER, "PLANNER", "NOPE")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ROLE-SLOT-NOT-FOUND")

    def test_env_token_is_fallback_identity(self):
        """備援：環境變數 token 對到憑證表。"""
        os.makedirs(os.path.join(self.tmp, mcp_server.CREDENTIALS_DIR), exist_ok=True)
        with open(os.path.join(self.tmp, mcp_server.CREDENTIALS_DIR,
                               mcp_server.CREDENTIALS_FILE), "w", encoding="utf-8") as fh:
            json.dump({"sessions": {"tok-1": {"session_id": PLANNER, "role": "PLANNER"}}}, fh)
        result = self.call("fbflow_status", {}, session=None, token="tok-1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["session_id"], PLANNER)

    def test_uuid_mismatch_is_rejected(self):
        self.bind_all()
        work_id = self.make_work()
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'", (work_id,)).fetchone()
        version = self.conn.execute(
            "SELECT version FROM works WHERE id = ?", (work_id,)).fetchone()["version"]
        result = self.call("fbflow_report_work", {
            "task_id": task["id"], "work_id": work_id, "expected_version": version,
            "stage": "design", "payload": {}, "message_id": "m1",
            "session_uuid": "wrong-uuid"}, session=THINKER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_SESSION_UNVERIFIED)

    def test_no_uuid_registered_still_passes_host_binding(self):
        """沒有登記 UUID 的 session → host 綁定就足以放行。"""
        self.bind_all()
        with store.tx(self.conn):
            self.conn.execute("UPDATE roles SET uuid_sha256 = NULL WHERE session_id = ?",
                              (THINKER,))
        work_id = self.make_work()
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'", (work_id,)).fetchone()
        version = self.conn.execute(
            "SELECT version FROM works WHERE id = ?", (work_id,)).fetchone()["version"]
        result = self.call("fbflow_report_work", {
            "task_id": task["id"], "work_id": work_id, "expected_version": version,
            "stage": "design", "payload": {}, "message_id": "m1",
            "session_uuid": ""}, session=THINKER)
        self.assertTrue(result["ok"], result)

    def test_registered_uuid_must_be_presented(self):
        """登記了 UUID 卻不帶 → 不放行。"""
        self.bind_all()
        work_id = self.make_work()
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'", (work_id,)).fetchone()
        version = self.conn.execute(
            "SELECT version FROM works WHERE id = ?", (work_id,)).fetchone()["version"]
        result = self.call("fbflow_report_work", {
            "task_id": task["id"], "work_id": work_id, "expected_version": version,
            "stage": "design", "payload": {}, "message_id": "m1",
            "session_uuid": ""}, session=THINKER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_SESSION_UNVERIFIED)


# ---------------------------------------------------------------- 專案解析

class TestProjectResolution(MCPHarness):

    def test_env_bound_project_wins(self):
        result = self.call("fbflow_status", {}, session=PLANNER)
        self.assertTrue(result["ok"], result)

    def test_unbound_project_is_refused(self):
        os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        result = self.call("fbflow_status", {}, session=PLANNER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_PROJECT_UNRESOLVED)

    def test_mcp_root_keeps_no_project_state(self):
        """**MCP 根目錄不記任何專案**——安裝狀態必須與專案無關。"""
        entries = [name for name in os.listdir(paths.mcp_root())
                   if name in ("projects.json", "registry.json")]
        self.assertEqual(entries, [],
                         "MCP 根目錄不該有專案註冊表：%s" % entries)
        self.assertFalse(hasattr(paths, "register_project"),
                         "paths 不該提供專案註冊入口")

    def test_session_cannot_point_at_arbitrary_path(self):
        """自報代碼解析不到 → 不准用路徑硬闖。"""
        os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        result = self.call("fbflow_status", {}, session=PLANNER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_PROJECT_UNRESOLVED)

    def test_not_initialized_message_names_the_real_db_file(self):
        """初始化失敗訊息要寫**實際的**正本檔名。"""
        empty = tempfile.mkdtemp(prefix="fbf-mcp-empty-")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        os.environ[mcp_server.ENV_PROJECT_ROOT] = empty
        result = self.call("fbflow_status", {}, session=PLANNER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_PROJECT_NOT_INITIALIZED)
        # 訊息裡出現的資料庫檔名必須**只有**實際那個
        mentioned = re.findall(r"[\w.-]+\.db", result["message"])
        self.assertEqual(mentioned, [store.DB_FILENAME])


# ---------------------------------------------------------------- 輸入檢查

class TestInputValidation(MCPHarness):

    def test_unknown_field_rejected(self):
        result = self.call("fbflow_work_create", {
            "title": "x", "requirement_id": "R-001", "content": "y", "surprise": 1})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_UNKNOWN_FIELD)

    def test_missing_required_rejected(self):
        result = self.call("fbflow_work_create", {"title": "x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_CONTENT_MISSING)

    def test_wrong_type_rejected(self):
        result = self.call("fbflow_work_create", {
            "title": "x", "requirement_id": "R-001", "content": "y",
            "expected_outputs": "not-a-list"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_INPUT_INVALID)

    def test_enum_violation_rejected(self):
        result = self.call("fbflow_register",
                           {"role": "BOSS", "short_name": "B"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_INPUT_INVALID)

    def test_project_code_is_not_a_parameter(self):
        """註冊不需要專案代碼——專案是 host 綁定的。"""
        result = self.call("fbflow_register",
                           {"project_code": "PRJ-X", "role": "PLANNER",
                            "short_name": "P1"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_UNKNOWN_FIELD)

    def test_failure_does_not_touch_canonical_data(self):
        before = self.conn.execute("SELECT COUNT(*) AS n FROM works").fetchone()["n"]
        self.call("fbflow_work_create", {"title": "x"})
        after = self.conn.execute("SELECT COUNT(*) AS n FROM works").fetchone()["n"]
        self.assertEqual(before, after)


# ---------------------------------------------------------------- 角色白名單

class TestRoleGate(MCPHarness):

    def test_worker_cannot_create_work(self):
        self.bind_all()
        result = self.call("fbflow_work_create",
                           {"title": "x", "requirement_id": "R-001", "content": "y"},
                           session=WORKER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_ROLE_DENIED)

    def test_worker_cannot_create_milestone(self):
        self.bind_all()
        result = self.call("fbflow_milestone_create",
                           {"seq": 1, "name": "A", "outcome": "成果"}, session=THINKER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_ROLE_DENIED)

    def test_worker_can_create_issue(self):
        self.bind_all()
        work_id = self.make_work()
        result = self.call("fbflow_issue_create", {
            "work_id": work_id, "trigger_kind": "work_blocked", "problem": "卡住",
            "location": "build", "tried": "試過三次", "impact": "停工",
            "question": "怎麼辦", "completion_criteria": "能重跑一次就過"}, session=WORKER)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["state"], workflow.ISSUE_RAISED)


# ---------------------------------------------------------------- 回報語意

class TestReport(MCPHarness):

    def _dispatched_task(self, work_id):
        self.coordinator.tick()
        return self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'", (work_id,)).fetchone()

    def _version(self, work_id):
        return self.conn.execute(
            "SELECT version FROM works WHERE id = ?", (work_id,)).fetchone()["version"]

    def test_report_is_accepted_and_not_applied_immediately(self):
        self.bind_all()
        work_id = self.make_work()
        task = self._dispatched_task(work_id)
        result = self.call("fbflow_report_work", {
            "task_id": task["id"], "work_id": work_id,
            "expected_version": self._version(work_id),
            "stage": "design", "message_id": "m1",
            "payload": {"solutions": [], "design_note": "x", "handoff": {}}},
            session=THINKER)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["status"], engine.SUB_PENDING)
        row = self.conn.execute("SELECT state FROM works WHERE id = ?", (work_id,)).fetchone()
        self.assertEqual(row["state"], workflow.DESIGN)

    def test_idempotent_resend_returns_same_submission(self):
        self.bind_all()
        work_id = self.make_work()
        task = self._dispatched_task(work_id)
        version = self._version(work_id)
        args = {"task_id": task["id"], "work_id": work_id, "expected_version": version,
                "stage": "design", "message_id": "m1",
                "payload": {"solutions": [], "design_note": "x", "handoff": {}}}
        first = self.call("fbflow_report_work", args, session=THINKER)
        second = self.call("fbflow_report_work", args, session=THINKER)
        self.assertEqual(first["data"]["submission_id"], second["data"]["submission_id"])
        self.assertTrue(second["data"]["idempotent"])

    def test_wrong_version_is_task_stale(self):
        self.bind_all()
        work_id = self.make_work()
        task = self._dispatched_task(work_id)
        result = self.call("fbflow_report_work", {
            "task_id": task["id"], "work_id": work_id, "expected_version": 999,
            "stage": "design", "message_id": "m1", "payload": {}}, session=THINKER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_TASK_STALE)

    def test_task_assigned_to_other_session_is_role_denied(self):
        self.bind_all()
        work_id = self.make_work()
        task = self._dispatched_task(work_id)
        result = self.call("fbflow_report_work", {
            "task_id": task["id"], "work_id": work_id,
            "expected_version": self._version(work_id),
            "stage": "design", "message_id": "m1", "payload": {}}, session=WORKER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_ROLE_DENIED)

    def test_report_to_unknown_task_is_task_stale(self):
        self.bind_all()
        result = self.call("fbflow_report_work", {
            "task_id": "T-999999", "work_id": "W-001", "expected_version": 1,
            "stage": "design", "message_id": "m1", "payload": {}}, session=THINKER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_TASK_STALE)


# ---------------------------------------------------------------- 讀取與狀態

class TestReadAndStatus(MCPHarness):

    def test_read_returns_version_and_snapshot(self):
        self.bind_all()
        work_id = self.make_work()
        result = self.call("fbflow_read", {"target_kind": "work", "target_id": work_id})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["version"], 1)
        self.assertEqual(result["data"]["state"], workflow.BACKLOG)

    def test_read_returns_the_stage_contract(self):
        """**取消工單檔案層後，契約的唯一來源就是 `fbflow_read`**（DEVIATIONS D34）。"""
        self.bind_all()
        work_id = self.make_work()
        self.coordinator.tick()
        result = self.call("fbflow_read", {"target_kind": "work", "target_id": work_id})
        self.assertTrue(result["ok"], result)
        contract = result["data"]["contract"]
        self.assertEqual(contract["stage"], workflow.DESIGN)
        for field in contract["required"]:
            self.assertIn(field, contract["required"])
        self.assertIn("solutions", contract["required"])
        # 沒有退件 → 空 dict（不是 None）
        self.assertEqual(result["data"]["last_rejection"], {})

    def test_read_returns_the_last_rejection(self):
        """退件原因要讓角色看得到（不然它會以為自己還沒交過）。"""
        self.bind_all()
        work_id = self.make_work()
        self.coordinator.tick()
        task = self.coordinator.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'",
            (work_id,)).fetchone()
        with store.tx(self.coordinator.conn):
            self.coordinator.conn.execute(
                "INSERT INTO submissions(id, task_id, work_id, issue_id, expected_version,"
                " session_id, stage, payload, message_id, status, gaps, created_at, updated_at)"
                " VALUES ('S-900001', ?, ?, NULL, 1, ?, 'design', '{}', 'msg-x', 'rejected',"
                " '[\"apply.rejected\"]', ?, ?)",
                (task["id"], work_id, task["assigned_session"], store.now(), store.now()))
        result = self.call("fbflow_read", {"target_kind": "work", "target_id": work_id})
        self.assertTrue(result["ok"], result)
        last = result["data"]["last_rejection"]
        self.assertEqual(last["submission_id"], "S-900001")
        self.assertEqual(last["gaps"], ["apply.rejected"])

    def test_read_missing_target(self):
        self.bind_all()
        result = self.call("fbflow_read", {"target_kind": "work", "target_id": "W-999"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], mcp_server.CODE_NOT_FOUND)

    def test_status_returns_own_tasks_only(self):
        self.bind_all()
        work_id = self.make_work()
        self.coordinator.tick()
        result = self.call("fbflow_status", {}, session=THINKER)
        self.assertTrue(result["ok"], result)
        sessions = {t["assigned_session"] for t in result["data"]["tasks"]}
        self.assertEqual(sessions, {THINKER})

    def test_status_does_not_leak_other_sessions(self):
        self.bind_all()
        self.make_work()
        self.coordinator.tick()
        result = self.call("fbflow_status", {}, session=WORKER)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["tasks"], [])


# ---------------------------------------------------------------- JSON-RPC

class TestJSONRPC(MCPHarness):

    def test_role_lookup_database_error_keeps_stdio_alive(self):
        import io
        import sqlite3
        from unittest.mock import patch

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "fbflow_status",
                        "_meta": {mcp_server.META_SESSION_KEY: PLANNER}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        stdin = io.StringIO("\n".join(map(json.dumps, requests)))
        stdout = io.StringIO()
        with patch.object(self.server, "_role_of",
                          side_effect=sqlite3.DatabaseError("file is not a database")):
            self.server.serve(stdin, stdout)
        failure, ping = map(json.loads, stdout.getvalue().splitlines())
        self.assertTrue(failure["result"]["isError"])
        payload = json.loads(failure["result"]["content"][0]["text"])
        # 正本損毀 → 明確的可行動作碼，不是 INTERNAL
        self.assertEqual(payload["code"], mcp_server.CODE_PROJECT_CORRUPTED)
        self.assertEqual(ping, {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_transient_database_error_is_not_reported_as_corruption(self):
        """暫時性 DB 錯誤（locked）不是「正本損毀」——不要誤導人去還原備份。"""
        import io
        import sqlite3
        from unittest.mock import patch

        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "fbflow_status",
                                "_meta": {mcp_server.META_SESSION_KEY: PLANNER}}}]
        stdout = io.StringIO()
        with patch.object(self.server, "_role_of",
                          side_effect=sqlite3.OperationalError("database is locked")):
            self.server.serve(io.StringIO(json.dumps(requests[0])), stdout)
        payload = json.loads(json.loads(stdout.getvalue().splitlines()[0])
                             ["result"]["content"][0]["text"])
        self.assertEqual(payload["code"], mcp_server.CODE_INTERNAL)
        self.assertNotIn(mcp_server.CODE_PROJECT_CORRUPTED, payload["code"])

    def test_initialize_returns_generic_instructions(self):
        """`initialize` 沒有 session 上下文 → 回通用說明，私有提示詞由註冊給。"""
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertIn("result", response)
        self.assertEqual(response["result"]["serverInfo"]["name"], mcp_server.SERVER_NAME)
        self.assertIn("fbflow_register", response["result"]["instructions"])

    def test_initialize_instructions_match_register_contract(self):
        """握手指示不能叫 session 帶「工具會擋掉」的欄位。

        專案由 host 綁定 → `fbflow_register` 不收 project_code，
        指示也不該叫 session 帶它（照指示做會被 INPUT-UNKNOWN-FIELD 擋下）。
        """
        response = self.server.handle({"jsonrpc": "2.0", "id": 6, "method": "initialize"})
        instructions = response["result"]["instructions"]
        register = next(t for t in mcp_server.TOOLS if t["name"] == "fbflow_register")
        self.assertNotIn("project_code", instructions)
        self.assertNotIn("project_code", register["allowed"])
        for field in register["required"]:
            self.assertIn(field, instructions)

    def test_tools_list_matches_table(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in response["result"]["tools"]]
        self.assertEqual(names, [t["name"] for t in mcp_server.TOOLS])
        for tool in response["result"]["tools"]:
            self.assertIn("inputSchema", tool)

    def test_tools_call_wraps_envelope(self):
        self.bind_all()
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "fbflow_status", "arguments": {},
                       "_meta": {mcp_server.META_SESSION_KEY: PLANNER}}})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertFalse(response["result"]["isError"])

    def test_tools_call_failure_sets_is_error(self):
        self.bind_all()
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "fbflow_work_create",
                       "arguments": {"title": "x", "requirement_id": "R-001",
                                     "content": "y"},
                       "_meta": {mcp_server.META_SESSION_KEY: WORKER}}})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertTrue(response["result"]["isError"])

    def test_unknown_method_returns_jsonrpc_error(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_notification_returns_nothing(self):
        response = self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(response)


# ---------------------------------------------------------------- 人類命令不外露

class TestPlannerGetsRequirements(MCPHarness):
    """回歸：PLANNER 必須能從工具取得已確認需求 id。

    實測踩過：PLANNER 拿不到 requirement_id，回頭問人類「標題要什麼」，
    整個工單生成卡死。所以 status 必須回已確認需求、read 必須能吃
    target_kind=requirement，decompose 提示詞也要把 id 直接寫進去。
    """

    def test_status_lists_confirmed_requirements(self):
        result = self.call("fbflow_status", {})
        self.assertTrue(result.get("ok"), result)
        reqs = result["data"]["confirmed_requirements"]
        self.assertEqual([r["id"] for r in reqs], ["R-001"])
        self.assertIn("做東西", reqs[0]["text"])

    def test_status_hides_unconfirmed_requirements(self):
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                " created_at) VALUES ('R-999','未確認','x',NULL,?)", (store.now(),))
        result = self.call("fbflow_status", {})
        ids = [r["id"] for r in result["data"]["confirmed_requirements"]]
        self.assertEqual(ids, ["R-001"], "未確認需求不得出現在清單")

    def test_read_accepts_requirement_target(self):
        result = self.call("fbflow_read",
                           {"target_kind": "requirement", "target_id": "R-001"})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["data"]["kind"], "requirement")
        self.assertTrue(result["data"]["confirmed"])

    def test_read_unknown_requirement_returns_not_found(self):
        result = self.call("fbflow_read",
                           {"target_kind": "requirement", "target_id": "R-404"})
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["code"], mcp_server.CODE_NOT_FOUND)

    def test_decompose_prompt_embeds_requirement_id(self):
        prompt = prompts.render_decompose_prompt("T-000001", self.tmp)
        self.assertIn("R-001", prompt)
        self.assertIn("不准回頭問人類", prompt)
        self.assertIn("fbflow_work_create", prompt)


class TestHumanCommandsNotExposed(MCPHarness):

    def test_human_commands_are_not_mcp_tools(self):
        names = {t["name"] for t in mcp_server.TOOLS}
        for forbidden in ("fbflow_init", "fbflow_requirement_confirm", "fbflow_project_close",
                          "fbflow_awaiting_list", "fbflow_awaiting_resolve",
                          "fbflow_engine_tick"):
            self.assertNotIn(forbidden, names)

    def test_tool_count(self):
        # 12 個：10 個既有工具 ＋ `fbflow_project_join`（新專案加入引擎的入口）
        # ＋ `fbflow_work_supersede`（需求變了 → 開新工單、舊單標成已取代）。
        self.assertEqual(len(mcp_server.TOOLS), 12)


if __name__ == "__main__":
    unittest.main()
