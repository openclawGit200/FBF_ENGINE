"""端到端測試：host CLI 終端通道（`terminal send` 寫進終端 PTY）貫穿整個派工流程。

驗證「真的照著規則走一遍」：

1. 建立暫存專案，roles 表預留席位（session_id=NULL，先不綁 session）。
2. engine tick → `_enroll_host_sessions` 把 host session（`term_…`）填進
   尚未綁定 session_id 的席位（agent='claude'）。
3. 建工單 → tick 派到這個 host session，outbox 走 `send_hostterm`。
4. 回執核對（`verify_host`）通過 → 工單推進。

**不碰真的 OpenCode**，也不碰真的 host CLI。`platform.hostterm.send`
與 `receipts.delivery_state` 被替換成測試替身，證明「路由正確、
outbox 正確、回執正確、流程走完」。真實 host CLI 的行為已在本機
對真實 CLI 驗證過（summary §3）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import engine, hostterm, platform, receipts, store, workflow  # noqa: E402

def _slug(path):
    """工作目錄 → transcript 專案資料夾名（與引擎同一個規則）。"""
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


TERM_PLANNER = "term_test_planner"
TERM_THINKER = "term_test_thinker"

HANDOFF = {"ROLE": "TESTER", "OPEN WORK": "none", "SUBAGENTS RUNNING": "none",
           "RULES AND NUMBERS IN FORCE": "none", "RECIPES": "none",
           "DECISIONS AND WHY": "none", "LESSONS": "none", "SPEND": "0"}


def design_payload(**over):
    payload = {
        "solutions": [
            {"subagent_context_id": "ctx-a", "approach": "做法甲",
             "cost": "低", "change_scope": "小", "effort": "少",
             "risk": "低", "verifiable_by": "跑測試", "reuses": "無"},
            {"subagent_context_id": "ctx-b", "approach": "做法乙",
             "cost": "高", "change_scope": "大", "effort": "多",
             "risk": "中", "verifiable_by": "跑測試", "reuses": "無"},
        ],
        "design_note": "設計說明",
        "handoff": HANDOFF,
    }
    payload.update(over)
    return payload


class HostChannelHarness(unittest.TestCase):
    """可跑完整派工→交件→回執→推進的暫存專案。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-host-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.coordinator = engine.Coordinator(self.tmp)
        self.addCleanup(self.coordinator.close)
        self.conn = self.coordinator.conn

        # stub 真實呼叫（不碰真實 host / OpenCode）
        self._real_host_send = platform.hostterm.send
        self._real_active = platform.active_sessions
        self._real_delivery = receipts.delivery_state
        self.host_calls = []  # (handle, text, cli) 紀錄，用於斷言「有送出去」

        def fake_host_send(handle, text, cli="", wait_submit=30, timeout=120):
            self.host_calls.append((handle, text, cli, wait_submit))
            return {"state": "delivered", "channel": "host_terminal",
                    "detail": "fake delivered"}

        platform.hostterm.send = fake_host_send
        platform.active_sessions = lambda *a, **k: []
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERED
        self.addCleanup(self._restore)

        # roles：預留席位，先不綁 session_id（auto-enroll 要填進去）
        with store.tx(self.conn):
            for role, short in (("PLANNER", "P1"), ("THINKER", "T1")):
                self.conn.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at) VALUES (?,?,NULL,'opencode','active','','',?)",
                    (role, short, store.now()))

        # 需求（已確認，才能建工單）
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO requirements(id, text, success_criterion, confirmed_at, created_at)"
                " VALUES (?,?,?,?,?)",
                ("R-001", "做出可驗收的產物", "產物存在且雜湊相符",
                 store.now(), store.now()))

    def _restore(self):
        platform.hostterm.send = self._real_host_send
        platform.active_sessions = self._real_active
        receipts.delivery_state = self._real_delivery

    def _enroll(self):
        """模擬 tick 起頭的 auto-enroll：把 host session 填進空席位。"""
        self.coordinator._enroll_host_sessions()

    def _make_work(self):
        return self.coordinator.create_work("E2E 測試工單", "R-001", "把東西做出來",
                                            [{"name": "out.txt"}])

    def _tick(self):
        return self.coordinator.tick()

    # ---------------------------------------------------------------- 測試

    def _host_sessions(self):
        """假的 host session 清單：**帶專案名的標題** ＋ 本專案的路徑。"""
        return [
            {"handle": TERM_PLANNER, "agent_identity": "claude", "state": "idle",
             "orca_session_id": "", "title": "Claude Code",
             "pane_title": "PLANNER/P1", "worktree_path": self.tmp,
             "connected": True, "writable": True},
            {"handle": TERM_THINKER, "agent_identity": "claude", "state": "idle",
             "orca_session_id": "", "title": "Claude Code",
             "pane_title": "THINKER/T1", "worktree_path": self.tmp,
             "connected": True, "writable": True},
        ]

    def test_auto_enroll_fills_empty_role_seat(self):
        """host session 填進 session_id=NULL 的席位，不會蓋掉已綁定的。"""
        real_scan = platform.hostterm.scan_sessions
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: self._host_sessions()
        self.addCleanup(setattr, platform.hostterm, "scan_sessions", real_scan)

        self._enroll()

        rows = {}
        with store.tx(self.conn):
            for row in self.conn.execute(
                    "SELECT short_name, session_id, agent FROM roles ORDER BY role, short_name"):
                rows[row["short_name"]] = dict(row)
        self.assertEqual(rows["P1"]["session_id"], TERM_PLANNER)
        self.assertEqual(rows["P1"]["agent"], "claude")
        self.assertEqual(rows["T1"]["session_id"], TERM_THINKER)
        self.assertEqual(rows["T1"]["agent"], "claude")

    def test_auto_enroll_uses_pane_title_not_list_order(self):
        """標題寫明角色 → 依標題綁，不依 terminal list 的列舉順序。

        實測過的坑：`terminal list` 的順序跟角色無關，依序填空會把
        thinker 的終端綁成 PLANNER。
        """
        real_scan = platform.hostterm.scan_sessions
        sessions = self._host_sessions()
        sessions.reverse()          # 順序顛倒：THINKER 先出現
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: sessions
        self.addCleanup(setattr, platform.hostterm, "scan_sessions", real_scan)

        self._enroll()

        rows = {}
        for row in self.conn.execute("SELECT short_name, session_id FROM roles"):
            rows[row["short_name"]] = row["session_id"]
        self.assertEqual(rows["P1"], TERM_PLANNER)
        self.assertEqual(rows["T1"], TERM_THINKER)

    def test_auto_enroll_ignores_other_project_sessions(self):
        """**別的專案**的終端 → 不進這個專案的席位（跨專案誤綁的防線）。"""
        real_scan = platform.hostterm.scan_sessions
        other = {
            "handle": "term_other_project", "agent_identity": "claude", "state": "idle",
            "orca_session_id": "", "title": "Claude Code",
            "pane_title": "PLANNER/P1", "worktree_path": "/somewhere/else",
            "connected": True, "writable": True,
        }
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: [other]
        self.addCleanup(setattr, platform.hostterm, "scan_sessions", real_scan)

        self._enroll()

        row = self.conn.execute(
            "SELECT session_id FROM roles WHERE short_name='P1'").fetchone()
        self.assertIsNone(row["session_id"], "別的專案的終端不該佔掉本專案的席位")

    def test_already_bound_session_title_is_upgraded_to_project_format(self):
        """已綁定的 session → tab 標題補成帶專案名的格式（重啟後才有證據對位）。"""
        real_rename = platform.rename_host_session
        renames = []
        platform.rename_host_session = lambda handle, title, cli="": renames.append(
            (handle, title)) or True
        self.addCleanup(setattr, platform, "rename_host_session", real_rename)

        real_scan = platform.hostterm.scan_sessions
        sessions = self._host_sessions()
        # 先綁好（舊格式標題），再跑一次 enroll
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: sessions
        self.addCleanup(setattr, platform.hostterm, "scan_sessions", real_scan)
        self._enroll()
        renames.clear()

        sessions[0]["pane_title"] = "PLANNER/P1"     # 舊格式
        self._enroll()

        self.assertIn((TERM_PLANNER, "%s/PLANNER/P1" % os.path.basename(self.tmp)),
                      renames,
                      "已綁定的 session 應該把標題升級成帶專案名的格式")

    def test_end_to_end_host_dispatch(self):
        """完整流程：build 工單 → tick 派 → outbox 走 host → 回執過 → 推進。"""
        real_scan = platform.hostterm.scan_sessions
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: self._host_sessions()
        self.addCleanup(setattr, platform.hostterm, "scan_sessions", real_scan)
        self._enroll()

        # PLANNER / THINKER 都應該被填到
        with store.tx(self.conn):
            planner = self.conn.execute(
                "SELECT session_id FROM roles WHERE short_name='P1'").fetchone()
            thinker = self.conn.execute(
                "SELECT session_id FROM roles WHERE short_name='T1'").fetchone()
        self.assertEqual(planner["session_id"], TERM_PLANNER)
        self.assertEqual(thinker["session_id"], TERM_THINKER)

        # 建工單（backlog）→ tick 推到 DESIGN，給 THINKER（TERM_THINKER）
        self._make_work()
        report = self._tick()
        self.assertIn("dispatched", report)

        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = 'W-001' AND status='open'").fetchone()
        self.assertIsNotNone(task, "應該有一張 open task")

        # outbox 應該有一筆，recipient_session = TERM_THINKER（host session）
        outbox = self.conn.execute(
            "SELECT * FROM outbox WHERE task_id = ?", (task["id"],)).fetchall()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["recipient_session"], TERM_THINKER)

        # hostterm.send 應該真的被呼叫（經過 send_hostterm）
        self.assertEqual(len(self.host_calls), 1)
        handle, text, cli, wait_submit = self.host_calls[0]
        self.assertEqual(handle, TERM_THINKER)
        self.assertEqual(cli, "", "cli 必須留空讓 resolve_cli 走完整解析")
        self.assertIn(task["id"], text)
        self.assertIn("W-001", text)
        self.assertIn("fbflow_read", text)

        # 再跑一趟 tick：回執已設為 DELIVERED → 工單推進、不報錯
        report2 = self._tick()
        self.assertIsInstance(report2, dict)
        self.assertIn("applied", report2)


class TestCrossProjectRebind(unittest.TestCase):
    """**跨專案誤綁的防線**：席位換綁時必須比對專案，不只比對角色標題。

    實測過的坑：兩個專案各開一組終端，舊專案的席位 handle 死掉後，
    舊邏輯只看 tab 標題（`PLANNER/P1`）→ 會把舊專案的席位改綁到
    **別的專案**的終端 → 提示詞送進錯目錄的工作階段。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-xproj-")
        self.other = tempfile.mkdtemp(prefix="fbf-xproj-other-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.other, ignore_errors=True)
        self.coordinator = engine.Coordinator(self.tmp)
        self.addCleanup(self.coordinator.close)
        self.conn = self.coordinator.conn
        # 專案名（`settings.name`）→ 標題裡的專案段
        with store.tx(self.conn):
            store.set_setting(self.conn, "name", "舊專案")
            # 席位先綁著一個**已死**的 handle
            self.conn.execute(
                "INSERT INTO roles(role, short_name, session_id, agent, status,"
                " project_code, workdir, created_at)"
                " VALUES ('PLANNER','P1','term_dead_handle','claude','active','','',?)",
                (store.now(),))

    def _scan(self, sessions):
        real_scan = platform.hostterm.scan_sessions
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: sessions
        self.addCleanup(setattr, platform.hostterm, "scan_sessions", real_scan)

    def _bound_session(self):
        return self.conn.execute(
            "SELECT session_id FROM roles WHERE short_name='P1'").fetchone()["session_id"]

    def test_old_format_title_in_other_project_is_not_rebound(self):
        """舊格式標題（`PLANNER/P1`）在**別的專案**的終端 → 不換綁。"""
        self._scan([{
            "handle": "term_other", "agent_identity": "claude", "state": "idle",
            "orca_session_id": "", "title": "Claude Code",
            "pane_title": "PLANNER/P1", "worktree_path": self.other,
            "connected": True, "writable": True,
        }])
        self.coordinator._enroll_host_sessions()
        self.assertEqual(self._bound_session(), "term_dead_handle",
                         "別的專案的終端不該被綁成本專案的席位")

    def test_old_format_title_in_same_project_is_rebound(self):
        """舊格式標題在本專案的終端 → **仍然要能換綁**（重啟後修復不能失效）。"""
        self._scan([{
            "handle": "term_same", "agent_identity": "claude", "state": "idle",
            "orca_session_id": "", "title": "Claude Code",
            "pane_title": "PLANNER/P1", "worktree_path": self.tmp,
            "connected": True, "writable": True,
        }])
        self.coordinator._enroll_host_sessions()
        self.assertEqual(self._bound_session(), "term_same")

    def test_new_format_title_requires_matching_project(self):
        """新格式標題（`舊專案/PLANNER/P1`）→ 專案名對得上才換綁。"""
        self._scan([{
            "handle": "term_named", "agent_identity": "claude", "state": "idle",
            "orca_session_id": "", "title": "Claude Code",
            "pane_title": "舊專案/PLANNER/P1", "worktree_path": self.tmp,
            "connected": True, "writable": True,
        }])
        self.coordinator._enroll_host_sessions()
        self.assertEqual(self._bound_session(), "term_named")

    def test_new_format_title_of_other_project_is_not_rebound(self):
        """新格式標題但專案名是**別的專案** → 不換綁（哪怕路徑在本專案）。"""
        self._scan([{
            "handle": "term_wrong_name", "agent_identity": "claude", "state": "idle",
            "orca_session_id": "", "title": "Claude Code",
            "pane_title": "別的專案/PLANNER/P1", "worktree_path": self.tmp,
            "connected": True, "writable": True,
        }])
        self.coordinator._enroll_host_sessions()
        self.assertEqual(self._bound_session(), "term_dead_handle")

    def test_seat_title_contains_project_name(self):
        """引擎寫進 tab 的席位標題要帶專案名（跨專案不撞）。"""
        title = self.coordinator._seat_title("PLANNER", "P1")
        self.assertEqual(title, "舊專案/PLANNER/P1")
        parsed = platform.parse_seat_title(title)
        self.assertEqual(parsed["project"], "舊專案")
        self.assertEqual(parsed["role"], "PLANNER")
        self.assertEqual(parsed["short_name"], "P1")

    def test_project_name_with_slash_is_sanitized(self):
        """專案名含 `/` → 轉成 `-`，否則標題分段會壞掉。"""
        with store.tx(self.conn):
            store.set_setting(self.conn, "name", "a/b")
        title = self.coordinator._seat_title("PLANNER", "P1")
        parsed = platform.parse_seat_title(title)
        self.assertEqual(parsed["role"], "PLANNER")
        self.assertEqual(parsed["short_name"], "P1")


class TestHostDispatchRouting(unittest.TestCase):
    """驗證平台層路由規則：term_… → host (hostterm.send 被呼叫)；否則不走 host。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-host-route-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._real_host_send = platform.hostterm.send
        self.host_calls = []

    def _restore(self):
        platform.hostterm.send = self._real_host_send

    def tearDown(self):
        self._restore()

    def test_term_handle_calls_hostterm_send(self):
        """session_id 以 term_ 開頭 → hostterm.send 被呼叫，cli='host CLI'。"""
        def fake_send(handle, text, cli="", wait_submit=30, timeout=120):
            self.host_calls.append((handle, cli))
            return {"state": "delivered", "channel": "host_terminal"}

        platform.hostterm.send = fake_send
        platform.send_to_session(
            self.tmp, {"session_id": TERM_PLANNER, "workdir": ""}, "prompt")
        self.assertEqual(len(self.host_calls), 1)
        self.assertEqual(self.host_calls[0][0], TERM_PLANNER)
        self.assertEqual(self.host_calls[0][1], "", "cli 必須留空讓 resolve_cli 走完整解析")

    def test_non_term_handle_does_not_call_hostterm_send(self):
        """非 term_ 開頭 → 不應該呼叫 hostterm.send（走 OpenCode 原生）。"""
        def fake_send(handle, text, cli="", wait_submit=30, timeout=120):
            self.host_calls.append((handle, cli))
            return {"state": "delivered", "channel": "host_terminal"}

        platform.hostterm.send = fake_send
        # ses_xyz 不是 term_ 開頭 → send_opencode（需要 opencode server，測試環境沒有，
        # 所以會失敗；重點是 hostterm.send 不該被呼叫）
        try:
            platform.send_to_session(
                self.tmp, {"session_id": "ses_xyz", "workdir": ""}, "prompt")
        except Exception:
            pass
        self.assertEqual(len(self.host_calls), 0)


if __name__ == "__main__":
    unittest.main()


class TestTranscriptFallback(unittest.TestCase):
    """**宿主 CLI 只給尾端視窗 → 回執核不到 → 角色白做工**（實測事故）。

    live 實測（2026-11-24）：`agent read` 只回最近約 20 條（`--limit` 再大也不會更多），
    工作量大時早期的派工訊息被推出視窗 → 核不到 task_id → 10 分鐘後
    「【回執未核對】退回補齊」→ **角色那幾十分鐘的工白花**。

    證據其實一直都在磁碟上的 transcript 裡（實測：`T-000017` 在磁碟出現 7 次）。
    這裡證明：**改讀磁碟 transcript 就能核到**，而且只認「這個終端自己的」那份。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-transcript-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = os.path.join(self.tmp, "proj")
        self.transcripts = os.path.join(self.tmp, "projects", _slug(self.project))
        os.makedirs(self.transcripts)
        self._real_root = hostterm.CLAUDE_TRANSCRIPT_ROOT
        hostterm.CLAUDE_TRANSCRIPT_ROOT = os.path.join(self.tmp, "projects")
        self.addCleanup(self._restore)

    def _restore(self):
        hostterm.CLAUDE_TRANSCRIPT_ROOT = self._real_root
        hostterm._TRANSCRIPT_FILES.clear()
        hostterm._TRANSCRIPT_VERDICT.clear()

    def _write(self, name, lines):
        path = os.path.join(self.transcripts, name)
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def test_task_id_found_in_the_transcript_file(self):
        handle = "term_aaa111"
        self._write("s1.jsonl", [
            {"type": "user", "message": {"role": "user", "content":
                "【FBF ENGINE 派工】task_id: T-000017 work_id: W-002 %s" % handle}},
            {"type": "assistant", "message": {"role": "assistant", "content":
                [{"type": "text", "text": "收到，我開始做 T-000017"}]}},
        ])
        result = hostterm.verify_in_transcript(handle, "T-000017", self.project)
        self.assertTrue(result.get("verified"), result)
        self.assertEqual(result["detail"]["anchor"], "transcript_file")

    def test_another_session_transcript_is_not_used(self):
        """**對不上一律不採信**——寧可核不到，也不要拿別人的紀錄當證據。"""
        self._write("other.jsonl", [
            {"type": "user", "message": {"role": "user", "content":
                "task_id: T-000099 term_zzz999"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "好了"}},
        ])
        result = hostterm.verify_in_transcript("term_aaa111", "T-000099", self.project)
        self.assertEqual(result, {}, "不該拿別人的 transcript 當證據")

    def test_dispatch_seen_but_no_reply_is_not_verified(self):
        handle = "term_aaa111"
        self._write("s1.jsonl", [
            {"type": "user", "message": {"role": "user", "content":
                "task_id: T-000017 %s" % handle}},
        ])
        result = hostterm.verify_in_transcript(handle, "T-000017", self.project)
        self.assertFalse(result.get("verified"), result)
        self.assertIn("尚未看到 assistant 回覆", result.get("reason_detail", ""))

    def test_agent_mentioning_the_task_counts_as_handled(self):
        """agent 自己的訊息帶著 task_id → 它確實處理了這張任務。"""
        handle = "term_aaa111"
        self._write("s1.jsonl", [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "fbflow_report_work",
                 "input": {"task_id": "T-000017", "handle": handle}}]}},
        ])
        result = hostterm.verify_in_transcript(handle, "T-000017", self.project)
        self.assertTrue(result.get("verified"), result)
