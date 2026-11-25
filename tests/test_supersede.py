"""**需求變了不要改已發出的工單——照新需求開新工單**（守門測試）。

使用者裁決（2026-11-25）：工單一旦發出去，就**不要再改它**——
改已發出的工單太麻煩（版本、退件、重工、交接全部要重來）。
正確做法是：**照新需求開新工單**，再把舊工單標成「已被取代」。

這個檔案守的是那條裁決的後果：

1. `fbflow_work_supersede` 只**標記與停工**：舊工單的**內容與狀態不動**。
2. 被取代的工單：**開著的 task 被取消**、**不再被派工**、**交件被擋**。
3. 護欄：不能取代已結案的工單、取代者必須存在且不是已結案／已被取代、
   一定要說明原因、只有 PLANNER 能做。
4. 需求層：需求變了要**新增**一條需求（由人確認），舊需求記住被哪條取代；
   結案追溯時舊需求不再要求結案工單，但**它底下的工單必須也被標成已取代**
   （否則還在派工做白工）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import mcp_server, store, workflow  # noqa: E402

PLANNER = "ses_planner"
THINKER = "ses_thinker"
WORKER = "ses_worker"


class SuperseHarness(unittest.TestCase):
    """一個暫存專案 ＋ 一個 MCP 伺服器（照 `test_mcp_server.py` 的做法）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-supersede-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_root = os.environ.get(mcp_server.ENV_PROJECT_ROOT)
        os.environ[mcp_server.ENV_PROJECT_ROOT] = self.tmp

        self.server = mcp_server.MCPServer(resident=False)
        self.addCleanup(self.server.close)
        seed = store.connect(self.tmp)
        store.init_db(seed)
        seed.close()
        self.coordinator = self.server.coordinator_for(self.tmp)
        self.conn = self.coordinator.conn
        self.uuids = {}
        with store.tx(self.conn):
            # 需求編號用引擎自己的流水號（R-001）——counters 也要跟著推進，
            # 否則引擎再建立一條需求會撞到 UNIQUE。
            self.conn.execute(
                "INSERT INTO counters(name, value) VALUES ('requirements', 1)"
                " ON CONFLICT(name) DO UPDATE SET value = 1")
            for role, short in (("PLANNER", "P1"), ("THINKER", "T1"), ("WORKER", "W1")):
                self.conn.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at)"
                    " VALUES (?,?,NULL,?, 'active', '', '', ?)",
                    (role, short, "opencode", store.now()))
            self.conn.execute(
                "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                " created_at) VALUES ('R-001','做東西','做出來',?,?)",
                (store.now(), store.now()))
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_root is None:
            os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        else:
            os.environ[mcp_server.ENV_PROJECT_ROOT] = self._old_root

    # ------------------------------------------------------------ 工具

    def call(self, name, args, session=PLANNER):
        args = dict(args)
        if name in ("fbflow_report_work", "fbflow_work_supersede"):
            args.setdefault("session_uuid", self.uuids.get(session, ""))
        params = {"_meta": {mcp_server.META_SESSION_KEY: session}} if session else {}
        return self.server.call_tool(name, args, params)

    def register(self, session, role, short_name):
        result = self.call("fbflow_register",
                           {"role": role, "short_name": short_name}, session=session)
        if result.get("ok"):
            self.uuids[session] = result["data"]["session_uuid"]
        return result

    def make_work(self, title="測試工單", requirement_id="R-001"):
        return self.coordinator.create_work(title, requirement_id, "內容", [])

    def work_row(self, work_id):
        return self.conn.execute(
            "SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()


class TestSupersedeMarksWithoutTouching(SuperseHarness):

    def test_supersede_keeps_content_and_state(self):
        """**不改已發出的工單**：內容與狀態原封不動。"""
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        before = self.work_row(old)

        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new],
            "reason": "需求改成別的樣子"})
        self.assertTrue(result.get("ok"), result)

        after = self.work_row(old)
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["content"], before["content"])
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["version"], before["version"],
                         "版本號不動——bump 會讓施工中的角色收到看不懂的 TASK-STALE")
        self.assertEqual(json.loads(after["superseded_by"]), [new])
        self.assertEqual(after["supersede_reason"], "需求改成別的樣子")

    def test_supersede_cancels_open_task(self):
        """角色不該再做一張已作廢的單——開著的 task 要被取消。"""
        self.register(PLANNER, "PLANNER", "P1")
        self.register(THINKER, "THINKER", "T1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'",
            (old,)).fetchone()
        self.assertIsNotNone(task, "應該已經派了工")

        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了"})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["data"]["cancelled_tasks"], 1)
        row = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_superseded_work_is_not_dispatched_again(self):
        """被取代的工單**不再被派工**。"""
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了"})

        self.coordinator.tick()
        tasks = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ?", (old,)).fetchall()
        self.assertEqual(tasks, [], "被取代的工單不該再被派工")

    def test_superseded_work_cannot_report(self):
        """收下已作廢單的交件只會讓正本記錄一件沒人要做的事。"""
        self.register(PLANNER, "PLANNER", "P1")
        self.register(THINKER, "THINKER", "T1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'",
            (old,)).fetchone()
        self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了"})

        version = self.work_row(old)["version"]
        # 用**被派工的那個 session**（THINKER）交件——它才是被作廢掉的那棒
        result = self.call("fbflow_report_work", {
            "task_id": task["id"], "work_id": old, "expected_version": version,
            "stage": "design", "payload": {}, "message_id": "m1"}, session=THINKER)
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_WORK_SUPERSEDED)


class TestSupersedeGuards(SuperseHarness):

    def test_cannot_supersede_closed_work(self):
        """結案是歷史，不能被取代。"""
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        with store.tx(self.conn):
            self.conn.execute("UPDATE works SET state = ? WHERE id = ?",
                              (workflow.CLOSED, old))
        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了"})
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_WORK_SUPERSEDE_DENIED)

    def test_replacement_must_exist(self):
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": ["W-999"], "reason": "需求變了"})
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_WORK_SUPERSEDE_DENIED)

    def test_reason_is_required(self):
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": ""})
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_CONTENT_MISSING)

    def test_cannot_supersede_twice(self):
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        other = self.make_work("另一張新工單")
        self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了"})
        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [other], "reason": "又變了"})
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_WORK_SUPERSEDE_DENIED)

    def test_only_planner_can_supersede(self):
        """決定範圍是 PLANNER 的權限。"""
        self.register(WORKER, "WORKER", "W1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了"},
            session=WORKER)
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_ROLE_DENIED)

    def test_uuid_is_required(self):
        """寫入工具要帶 session_uuid。"""
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        result = self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了",
            "session_uuid": "wrong-uuid"})
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_SESSION_UNVERIFIED)


class TestRequirementSupersede(SuperseHarness):
    """需求層：需求變了要**新增**一條，不是改舊的。"""

    def _confirm_requirement(self, text, criterion, supersedes):
        import argparse
        import contextlib
        import io

        import fbf

        args = argparse.Namespace(project=self.tmp, id="", text=text,
                                  criterion=criterion, confirmed_by="human",
                                  supersedes=list(supersedes))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = fbf.cmd_requirement_confirm(args)
        self.assertEqual(code, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_new_requirement_records_the_relation(self):
        payload = self._confirm_requirement("新的需求", "新的判準", ["R-001"])
        new_id = (payload.get("requirement") or {}).get("id") or ""
        self.assertTrue(new_id, payload)
        row = self.conn.execute(
            "SELECT superseded_by FROM requirements WHERE id = 'R-001'").fetchone()
        self.assertEqual(row["superseded_by"], new_id)

    def test_superseded_requirement_no_longer_blocks_project_close(self):
        """需求被新需求取代 → 追溯不再要求它有結案工單。"""
        self._confirm_requirement("新的需求", "新的判準", ["R-001"])
        trace = self.coordinator._traceability_check()
        self.assertNotIn("R-001", trace["details"]["requirements_without_closed_work"],
                         trace)
        self.assertIn("R-001", trace["details"]["superseded_requirements"], trace)

    def test_works_of_superseded_requirement_must_be_superseded_too(self):
        """需求換了但底下的工單還在 → 阻擋並說明（它們還會被派工做白工）。"""
        self._confirm_requirement("新的需求", "新的判準", ["R-001"])
        self.make_work("舊需求底下的單")
        trace = self.coordinator._traceability_check()
        self.assertFalse(trace["ok"], trace)
        self.assertTrue(trace["details"]["works_of_superseded_requirement"], trace)


class TestStatusVisibility(SuperseHarness):

    def test_superseded_work_is_visible_in_status(self):
        """被取代的工單要看得見——不然人會以為它還在跑。"""
        self.register(PLANNER, "PLANNER", "P1")
        old = self.make_work("舊工單")
        new = self.make_work("新工單")
        self.call("fbflow_work_supersede", {
            "work_id": old, "replacement_work_ids": [new], "reason": "需求變了"})

        status = self.call("fbflow_status", {})
        self.assertTrue(status.get("ok"), status)
        ids = [w["id"] for w in status["data"]["superseded_works"]]
        self.assertIn(old, ids)
        open_ids = [w["id"] for w in status["data"]["open_works"]]
        self.assertNotIn(old, open_ids)
        self.assertIn(new, open_ids)


if __name__ == "__main__":
    unittest.main()
