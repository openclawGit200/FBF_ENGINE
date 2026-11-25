"""**後台網頁（儀表板）**的測試。

這裡守三件事：

1. **純讀**：儀表板**不寫正本、不拿租約、不打擾 session**——
   開一次頁面不該讓引擎的狀態有任何變化。
2. **「誰在寫」要誠實**：席位沒人認領時要說「無人認領」，
   不能假裝有人在寫（那會讓人誤判卡在哪）。
3. **「欄位改成什麼」要真的對**：退件帶逐欄位差異、
   套用帶「舊值 → 新值」。

全部用暫存專案，不碰任何真實專案資料。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import dashboard, engine, platform, receipts, store, workflow  # noqa: E402


def fake_verifier(endpoint, session_id, message_id, task_id,
                  dispatched_at=None, submitted_at=None, binary="opencode"):
    return {"verified": True, "reason": "", "platform": "test",
            "message_id": message_id, "session_id": session_id,
            "model_id": "test/model", "context_id": "ctx-" + str(message_id),
            "finish": "stop", "detail": {}}


HANDOFF = {"ROLE": "TESTER", "OPEN WORK": "none", "SUBAGENTS RUNNING": "none",
           "RULES AND NUMBERS IN FORCE": "none", "RECIPES": "none",
           "DECISIONS AND WHY": "none", "LESSONS": "none", "SPEND": "0"}


class DashboardHarness(unittest.TestCase):
    """一個可跑流程的暫存專案（給儀表板讀）。"""

    verifier = staticmethod(fake_verifier)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-dash-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.coordinator = engine.Coordinator(self.tmp, verifier=self.verifier)
        self.addCleanup(self.coordinator.close)
        self.conn = self.coordinator.conn
        self._real_send = platform.send_to_session
        self._real_active = platform.active_sessions
        self._real_delivery = receipts.delivery_state
        platform.send_to_session = lambda *a, **k: {"state": "queued", "channel": "test"}
        platform.active_sessions = lambda *a, **k: []
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERY_UNKNOWN
        self._real_host_scan = platform.hostterm.scan_sessions
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: []
        self.addCleanup(self._restore_platform)
        with store.tx(self.conn):
            for role, short, sid in (("PLANNER", "P1", "ses_planner"),
                                     ("THINKER", "T1", "ses_thinker"),
                                     ("WORKER", "W1", "ses_worker")):
                self.conn.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at) VALUES (?,?,?,?, 'active','','',?)",
                    (role, short, sid, "opencode", store.now()))
            self.conn.execute(
                "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                " created_at) VALUES ('R-001', '做出可驗收的產物', '產物存在', ?, ?)",
                (store.now(), store.now()))

    def _restore_platform(self):
        platform.send_to_session = self._real_send
        platform.active_sessions = self._real_active
        receipts.delivery_state = self._real_delivery
        platform.hostterm.scan_sessions = self._real_host_scan

    # -- 流程 ---------------------------------------------------------
    def make_work(self):
        return self.coordinator.create_work("測試工單", "R-001", "把東西做出來",
                                            [{"name": "out.txt"}])

    def design_payload(self, **over):
        payload = {
            "solutions": [
                {"approach": "做法甲", "cost": "低", "change_scope": "小",
                 "effort": "少", "risk": "低", "verifiable_by": "跑測試"},
                {"approach": "做法乙", "cost": "高", "change_scope": "大",
                 "effort": "多", "risk": "中", "verifiable_by": "跑測試"},
            ],
            "design_note": "設計說明",
            "handoff": HANDOFF,
        }
        payload.update(over)
        return payload

    def _submit_design(self, payload, message_id="msg-1"):
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = 'W-001' AND status = 'open'").fetchone()
        self.coordinator.submit_work(task["id"], "W-001", 2, "ses_thinker",
                                     workflow.DESIGN, payload, message_id)
        self.coordinator.tick()


class TestDashboardIsReadOnly(DashboardHarness):

    def test_reading_the_dashboard_changes_nothing(self):
        """**看儀表板不改變任何東西**——它只是把正本畫出來。"""
        self.make_work()
        self.coordinator.tick()
        before = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM works) AS w,"
            " (SELECT COUNT(*) FROM tasks) AS t,"
            " (SELECT COUNT(*) FROM submissions) AS s,"
            " (SELECT COUNT(*) FROM decisions) AS d,"
            " (SELECT COUNT(*) FROM journal) AS j").fetchone()
        lease_before = store.read_lease(self.conn)

        state = dashboard.collect_state(self.conn)

        after = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM works) AS w,"
            " (SELECT COUNT(*) FROM tasks) AS t,"
            " (SELECT COUNT(*) FROM submissions) AS s,"
            " (SELECT COUNT(*) FROM decisions) AS d,"
            " (SELECT COUNT(*) FROM journal) AS j").fetchone()
        self.assertEqual(dict(before), dict(after))
        self.assertEqual(lease_before, store.read_lease(self.conn))
        self.assertTrue(state["works"])

    def test_snapshot_reports_who_is_working_on_each_work(self):
        """**誰在寫**：開著的 task 派給誰，誰就是正在寫的人。"""
        self.make_work()
        self.coordinator.tick()
        state = dashboard.collect_state(self.conn)
        card = [w for w in state["works"] if w["id"] == "W-001"][0]
        self.assertTrue(card["active"], "這一棒應該有人在寫")
        self.assertEqual(card["active"]["role"], "THINKER")
        self.assertEqual(card["active"]["seat"], "T1")
        self.assertTrue(card["claimed"])
        self.assertIn("THINKER", card["who"])

    def test_unclaimed_seat_is_reported_honestly(self):
        """**席位沒人認領就要說沒人**——不能假裝有人在寫。"""
        self.make_work()
        self.coordinator.tick()
        # 把 THINKER 席位的 session 拿掉（模擬「session 中斷、席位還占著」）
        with store.tx(self.conn):
            self.conn.execute("UPDATE roles SET session_id = NULL WHERE role = 'THINKER'")
        state = dashboard.collect_state(self.conn)
        card = [w for w in state["works"] if w["id"] == "W-001"][0]
        self.assertFalse(card["claimed"], "席位已無人認領")
        self.assertEqual(card["active"]["role"], "THINKER")

    def test_a_work_with_no_open_task_has_no_active_writer(self):
        """沒有開著的 task → 沒有「正在寫的人」（不要顯示殘留的 role）。"""
        self.make_work()
        self.coordinator.tick()
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'done'")
        state = dashboard.collect_state(self.conn)
        card = [w for w in state["works"] if w["id"] == "W-001"][0]
        self.assertIsNone(card["active"])
        self.assertEqual(card["who"], "")


class TestDashboardFieldChanges(DashboardHarness):

    def _insert_applied(self, submission_id, payload, created_at, version=1):
        """直接放一筆「已套用」的提交記錄。

        **為什麼不真的跑流程**：同一個欄位要被改第二次，
        在真實流程裡要繞過重工路（design → acceptance_design → …），
        那是另一套規則在管的事。這裡要守的是**儀表板的衍生邏輯**，
        所以直接給它兩筆已套用的記錄。
        """
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO submissions(id, task_id, work_id, issue_id,"
                " expected_version, session_id, stage, payload, message_id, status,"
                " gaps, created_at, updated_at)"
                " VALUES (?, ?, 'W-001', NULL, ?, 'ses_thinker', 'design',"
                " ?, ?, 'applied', '[]', ?, ?)",
                (submission_id, "T-%s" % submission_id, version, 
                 json.dumps(payload, ensure_ascii=False),
                 submission_id, created_at, created_at))

    def test_applied_submission_shows_the_new_field_values(self):
        """套用的交件 → 時間軸上出現那些欄位與新值。"""
        self.make_work()
        self.coordinator.tick()
        self._submit_design(self.design_payload())
        state = dashboard.collect_state(self.conn)
        applied = [c for c in state["changes"] if c["kind"] == "applied"]
        self.assertTrue(applied, "應該有一筆已套用的變更")
        event = applied[-1]
        self.assertEqual(event["target_id"], "W-001")
        self.assertIn("design_note", event["values"])
        self.assertEqual(event["values"]["design_note"], "設計說明")
        self.assertIn("solutions", event["field"])

    def test_same_field_changed_later_shows_old_and_new(self):
        """同一個欄位後來改了 → 顯示「舊值 → 新值」。"""
        self.make_work()
        self.coordinator.tick()
        self._insert_applied("S-900001", {"design_note": "第一次的設計說明"},
                             "2026-11-24T10:00:00", version=1)
        self._insert_applied("S-900002", {"design_note": "第二次的設計說明"},
                             "2026-11-24T11:00:00", version=2)
        state = dashboard.collect_state(self.conn)
        diffs = [c for c in state["changes"] if c.get("diffs")]
        self.assertTrue(diffs, "應該有一筆欄位被改動的紀錄")
        found = False
        for event in diffs:
            if "design_note" in event["diffs"]:
                found = True
                self.assertEqual(event["diffs"]["design_note"]["old"], "第一次的設計說明")
                self.assertEqual(event["diffs"]["design_note"]["new"], "第二次的設計說明")
        self.assertTrue(found, "design_note 的前後值沒有出來")

    def test_rejection_carries_the_per_field_explanation(self):
        """退件 → 時間軸上帶「哪個欄位、標準、範例」。"""
        self.make_work()
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = 'W-001' AND status = 'open'").fetchone()
        # 交一個缺欄位的 payload → 守衛退件 → 引擎記下逐欄位差異
        self.coordinator.submit_work(task["id"], "W-001", 2, "ses_thinker",
                                     workflow.DESIGN, {"solutions": []}, "msg-bad")
        self.coordinator.tick()
        state = dashboard.collect_state(self.conn)
        rejected = [c for c in state["changes"] if c["kind"] == "rejected"]
        self.assertTrue(rejected, "應該有一筆退件")
        event = rejected[-1]
        self.assertTrue(event["field"], "退件要指名欄位")
        self.assertTrue(event["standard"], "退件要帶欄位標準")


class TestDashboardHTTP(DashboardHarness):

    def _start(self):
        server = dashboard.DashboardServer(self.tmp, host="127.0.0.1", port=0)
        port = server.start()
        self.addCleanup(server.stop)
        return "http://127.0.0.1:%d" % port

    def test_page_and_api_are_served(self):
        """頁面與 API 都要能開。"""
        self.make_work()
        self.coordinator.tick()
        base = self._start()
        with urllib.request.urlopen(base + "/") as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("FBF ENGINE", html)
        with urllib.request.urlopen(base + "/api/state") as resp:
            state = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(state["works"])
        self.assertIn("W-001", [w["id"] for w in state["works"]])

    def test_api_never_exposes_a_broken_payload(self):
        """**排序鍵不能炸**——`decisions.id` 是整數、`submissions.id` 是字串。"""
        self.make_work()
        self.coordinator.tick()
        self._submit_design(self.design_payload())
        base = self._start()
        with urllib.request.urlopen(base + "/api/state") as resp:
            state = json.loads(resp.read().decode("utf-8"))
        self.assertNotIn("error", state)

    def test_refuses_to_serve_a_directory_that_is_not_a_project(self):
        """**不憑空建立正本**——不是專案的目錄要明確報錯。"""
        empty = tempfile.mkdtemp(prefix="fbf-dash-empty-")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        server = dashboard.DashboardServer(empty, host="127.0.0.1", port=0)
        with self.assertRaises(ValueError):
            server.start()


if __name__ == "__main__":
    unittest.main()
