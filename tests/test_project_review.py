"""專案結案檢視（`project_review`）＋ 退件 LOG 的測試。

覆蓋使用者規則（2026-11-24）：

1. **結案 ≠ milestones**
   - milestones＝防止長施工流程偏移方向的**分段**機制；
   - 結案＝**專案層級**「任務目標到底有沒有達成」的判定，**不依賴 `milestones` 表有資料**。
   - 規則：所有單據都標示結案 → 引擎通知 PLANNER → PLANNER 對照產出與任務目標
     → 達成就標專案結案；未達成就針對未完成部分開新工單。

2. **退件 LOG ＋ 結案復盤**
   - 每一次退件都留下 LOG：完整副本、退件原因、為了哪個欄位、退的是誰的件。
   - 每次結案後依退件紀錄做復盤 → 引擎改善建議（存表、網頁顯示、CLI 可讀）。

全程用暫存專案（`Harness`），不碰任何真實專案資料。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import engine, store, workflow  # noqa: E402
from tests.test_fbf_engine import HANDOFF, Harness  # noqa: E402


class ProjectReviewMixin(Harness):
    """把一張工單走完五棒到 closed 的範例步驟。"""

    def _closed_work(self, work_id="W-001"):
        _, report = self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        _, report = self.submit(workflow.ADJUDICATE,
                                {"action": "close", "close_reason": "完成",
                                 "handoff": HANDOFF},
                                "ses_planner")
        return work_id, report

    def _review_task(self):
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_PROJECT_REVIEW,)).fetchone()
        self.assertIsNotNone(row, "應有一張專案結案檢視單")
        return row

    def _review_row(self):
        return self.conn.execute(
            "SELECT * FROM project_reviews WHERE id = ?",
            (engine.PROJECT_REVIEW_ID,)).fetchone()


# ---------------------------------------------------------------- A. 退件 LOG

class TestRejectionLog(ProjectReviewMixin):

    def test_every_rejection_writes_a_log_row(self):
        """**每一次退件都留 LOG**（不是只有第一次、不是只有人工退件）。"""
        self.make_work()
        self.coordinator.tick()
        # 交一份缺欄位的 DESIGN → 守衛退件
        _, report = self.submit(workflow.DESIGN, {"handoff": HANDOFF}, "ses_thinker")
        self.assertTrue(report["rejected"], report)

        row = self.conn.execute(
            "SELECT * FROM rejection_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row, "退件必須留下 LOG")
        self.assertEqual(row["target_kind"], "work")
        self.assertEqual(row["target_id"], "W-001")
        self.assertEqual(row["stage"], workflow.DESIGN)
        self.assertEqual(row["session_id"], "ses_thinker")
        self.assertEqual(row["role"], "THINKER")
        self.assertEqual(row["short_name"], "T1")

    def test_log_contains_full_copy_reason_fields_and_owner(self):
        """LOG 內容必備：完整副本、退件原因、為了哪個欄位、退的是誰的件。"""
        self.make_work()
        self.coordinator.tick()
        _, report = self.submit(workflow.DESIGN, {"handoff": HANDOFF}, "ses_thinker")
        self.assertTrue(report["rejected"], report)

        row = self.conn.execute(
            "SELECT * FROM rejection_log ORDER BY id DESC LIMIT 1").fetchone()
        # 退件單據的完整副本：被退的那次交件內容
        payload = json.loads(row["payload"])
        self.assertIn("handoff", payload)
        # 退件當下單據的整列（快照）
        snapshot = json.loads(row["target_snapshot"])
        self.assertEqual(snapshot.get("id"), "W-001")
        # 退件原因：人看得懂的說明，不是只有代碼
        self.assertTrue(row["reason"], "退件原因不得為空")
        # 為了哪個欄位退件
        fields = json.loads(row["fields"])
        self.assertTrue(fields, "退件必須指出是為了哪個欄位")
        self.assertTrue(any("solutions" in f for f in fields), fields)
        # 原始缺口代碼也留著
        self.assertTrue(json.loads(row["gaps"]))

    def test_rejection_log_is_not_created_on_accept(self):
        """通過的交件不得產生退件 LOG（LOG 是退件專用）。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM rejection_log").fetchone()["n"]
        self.assertEqual(count, 0)


# ---------------------------------------------------------------- B. 觸發條件

class TestProjectReviewTrigger(ProjectReviewMixin):

    def test_dispatch_when_all_works_closed_without_any_milestone(self):
        """**結案不依賴 `milestones` 表**——一個里程碑都沒有也必須派檢視。"""
        self.make_work()
        self.coordinator.tick()
        _, report = self._closed_work()

        milestones = self.conn.execute(
            "SELECT COUNT(*) AS n FROM milestones").fetchone()["n"]
        self.assertEqual(milestones, 0, "這個案例不該有里程碑")

        task = self._review_task()
        self.assertEqual(task["assigned_role"], "PLANNER")
        self.assertEqual(task["project_review_id"], engine.PROJECT_REVIEW_ID)
        row = self._review_row()
        self.assertEqual(row["state"], workflow.PR_REVIEWING)
        self.assertEqual(int(row["version"]), 1)
        self.assertTrue(report["project_reviews"])

    def test_no_dispatch_while_a_work_is_still_open(self):
        """還有工單沒標結案 → 不派檢視。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.coordinator.tick()
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_PROJECT_REVIEW,)).fetchone())

    def test_no_dispatch_while_milestone_review_is_running(self):
        """**段末對照優先**：里程碑還在檢視中 → 不做專案層級判定（順序不可反）。"""
        milestone_id = self.coordinator.create_milestone(1, "A", "第一段成果")
        self.make_work(milestone_id=milestone_id)
        self.coordinator.tick()
        self._closed_work()
        self.coordinator.tick()
        ms = self.conn.execute(
            "SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
        self.assertEqual(ms["state"], workflow.MS_REVIEWING)
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_PROJECT_REVIEW,)).fetchone())

    def test_no_duplicate_dispatch(self):
        """一輪檢視只派一次（重跑 tick 不重複派）。"""
        self.make_work()
        self.coordinator.tick()
        self._closed_work()
        self.coordinator.tick()
        self.coordinator.tick()
        self.coordinator.tick()
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE stage = ?",
            (workflow.STAGE_PROJECT_REVIEW,)).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_freshly_dispatched_task_is_not_expired_as_stalled(self):
        """**剛派出的檢視單不會因為角色「上一棒之前」閒置很久就被收掉**。

        實測 2026-11-24：PLANNER 閒置 162 分鐘 → 剛派出的結案檢視單
        1 分鐘後被逾時收掉 → 結案判定卡死。
        """
        self.make_work()
        self.coordinator.tick()
        self._closed_work()
        task = self._review_task()
        # 角色上一次交件是很久以前
        with store.tx(self.conn):
            self.conn.execute("UPDATE submissions SET created_at = '2000-01-01T00:00:00'")
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "open", "剛派出來的一棒不該被當失聯收掉")

    def test_review_is_redispatched_when_task_was_cancelled(self):
        """檢視單被收掉 → 下一輪要**重新派**（否則結案判定永遠卡住）。"""
        self.make_work()
        self.coordinator.tick()
        self._closed_work()
        task = self._review_task()
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ?",
                              (task["id"],))
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_PROJECT_REVIEW,)).fetchone()
        self.assertIsNotNone(row, "收掉之後要重新派一輪")
        self.assertNotEqual(row["id"], task["id"])
        review = self._review_row()
        self.assertEqual(review["state"], workflow.PR_REVIEWING)
        self.assertEqual(int(review["version"]), 2)

    def test_prompt_carries_goal_outputs_rejections_and_task_id(self):
        """派工提示詞必須帶 task_id（回執核對靠它）＋目標/產出/退件紀錄三份摘要。"""
        self.make_work()
        self.coordinator.tick()
        # 先製造一次退件，讓復盤有東西可依
        self.submit(workflow.DESIGN, {"handoff": HANDOFF}, "ses_thinker")
        self._closed_work()
        self.coordinator.tick()

        task = self._review_task()
        outbox = self.conn.execute(
            "SELECT * FROM outbox WHERE task_id = ?", (task["id"],)).fetchone()
        self.assertIsNotNone(outbox)
        text = outbox["payload"]
        self.assertIn(task["id"], text)
        self.assertIn("expected_version: 1", text)
        self.assertIn("R-001", text)              # 任務目標
        self.assertIn("fbflow_report_project_review", text)
        self.assertIn("退件紀錄", text)          # 復盤依據
        self.assertIn("improvements", text)      # 同一棒要交的復盤產出

    def test_rejection_lines_carry_stage_and_number(self):
        """**退件紀錄要逐欄結構化並編號**——階段照抄，不要憑印象改寫。

        實測 2026-11-25：PLANNER 寫改善建議時把 `design` 階段的退件
        記成 `acceptance_design` → 整條建議的方向就偏了。
        """
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, {"handoff": HANDOFF}, "ses_thinker")
        self._closed_work()
        self.coordinator.tick()

        task = self._review_task()
        text = self.conn.execute(
            "SELECT payload FROM outbox WHERE task_id = ?", (task["id"],)).fetchone()["payload"]
        self.assertIn("退件 #1", text, "退件紀錄沒編號 → 建議無法引用")
        self.assertIn("階段：**design", text, "退件紀錄沒有把階段寫清楚")
        self.assertIn("解法設計中", text, "階段要附中文標題，別讓人猜")
        self.assertIn("solutions.count", text, "缺口代碼要列出來")
        self.assertIn("S-000001", text, "要能對到是哪一筆交件")
        self.assertIn("照抄它的階段", text, "要明講不要憑印象改寫階段")


# ---------------------------------------------------------------- C. 套用結果

class TestApplyProjectReview(ProjectReviewMixin):

    def _dispatched(self):
        self.make_work()
        self.coordinator.tick()
        self._closed_work()
        self.coordinator.tick()
        return self._review_task()

    def test_complete_marks_project_closed_after_traceability(self):
        """達成就標專案結案——但引擎會**自動跑雙向追溯**，不通過就不許標。"""
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_COMPLETE, "目標達成")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["state"], workflow.PR_CLOSED)
        self.assertTrue(store.get_setting(self.conn, "closed_at", ""))
        row = self._review_row()
        self.assertEqual(row["state"], workflow.PR_CLOSED)

    def test_complete_blocked_when_traceability_incomplete(self):
        """追溯不通過 → 不得假結案（回 traceability.incomplete）。"""
        task = self._dispatched()
        # 抽掉驗收證據 → required check 沒有 PASS → 追溯不過
        with store.tx(self.conn):
            self.conn.execute("DELETE FROM results WHERE work_id = 'W-001'")
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_COMPLETE, "想直接結案")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "traceability.incomplete")
        self.assertEqual(store.get_setting(self.conn, "closed_at", ""), "")

    def test_add_work_creates_new_work_and_reopens(self):
        """未達成 → 針對未完成的部分開新工單。"""
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_ADD_WORK, "還缺一份",
            new_works=[{"title": "補做缺的部分", "requirement_id": "R-001",
                        "content": "把缺的補上", "expected_outputs": [{"name": "x.txt"}]}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["data"]["new_works"]), 1)
        new_id = result["data"]["new_works"][0]
        row = self.conn.execute(
            "SELECT * FROM works WHERE id = ?", (new_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["requirement_id"], "R-001")
        review = self._review_row()
        self.assertEqual(review["state"], workflow.PR_REOPENED)

    def test_await_human_stops_review(self):
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_AWAIT_HUMAN, "等人裁決")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["state"], workflow.PR_AWAITING)

    def test_improvements_are_saved(self):
        """結案復盤的產出 → 引擎改善建議（存新表）。"""
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_COMPLETE, "達成",
            improvements=[{"target": "退件守衛 explain_form",
                           "suggestion": "退件訊息要附上完整范例",
                           "reason": "依據退件紀錄 #1"}])
        self.assertTrue(result["ok"], result)
        row = self.conn.execute(
            "SELECT * FROM engine_improvements ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row, "改善建議必須落表")
        self.assertEqual(row["review_id"], engine.PROJECT_REVIEW_ID)
        self.assertIn("范例", row["suggestion"])

    def test_version_mismatch_rejected(self):
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 99, "ses_planner", workflow.PR_COMPLETE, "版本不對")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TASK-STALE")

    def test_wrong_session_rejected(self):
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_thinker", workflow.PR_COMPLETE, "我不是受派者")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ROLE-DENIED")

    def test_wrong_stage_rejected(self):
        """不得拿別的單子來結案。"""
        self._dispatched()
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO tasks(id, work_id, issue_id, milestone_id, project_review_id,"
                " stage, assigned_session, assigned_role, status, created_at, updated_at)"
                " VALUES (?,NULL,NULL,NULL,NULL,?,?,?, 'open', ?, ?)",
                ("T-900001", workflow.DESIGN, "ses_planner", "PLANNER",
                 store.now(), store.now()))
        other = self.conn.execute(
            "SELECT * FROM tasks WHERE id = 'T-900001'").fetchone()
        self.assertIsNotNone(other)
        result = self.coordinator.apply_project_review(
            other["id"], 1, "ses_planner", workflow.PR_COMPLETE, "亂用")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "task.stage_mismatch")

    def test_task_cannot_be_reused(self):
        task = self._dispatched()
        first = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_AWAIT_HUMAN, "先停")
        self.assertTrue(first["ok"], first)
        second = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_COMPLETE, "再送一次")
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "TASK-STALE")

    def test_invalid_action_rejected(self):
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", "whatever", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "action.invalid")

    def test_reopened_review_triggers_another_round_after_new_work_closes(self):
        """補工單 → 新工單完成 → **再觸發一次**檢視（下一輪結案判定）。"""
        task = self._dispatched()
        result = self.coordinator.apply_project_review(
            task["id"], 1, "ses_planner", workflow.PR_ADD_WORK, "還缺一份",
            new_works=[{"title": "補做缺的部分", "requirement_id": "R-001",
                        "content": "把缺的補上", "expected_outputs": [{"name": "x.txt"}]}])
        new_id = result["data"]["new_works"][0]
        # 走完新工單（新工單由 tick 派第一棒）
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker", work_id=new_id)
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(),
                    "ses_planner", work_id=new_id)
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker", work_id=new_id)
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker", work_id=new_id)
        self.submit(workflow.ADJUDICATE,
                    {"action": "close", "close_reason": "完成", "handoff": HANDOFF},
                    "ses_planner", work_id=new_id)
        self.coordinator.tick()
        review = self._review_row()
        self.assertEqual(review["state"], workflow.PR_REVIEWING)
        self.assertEqual(int(review["version"]), 2)
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE stage = ?",
            (workflow.STAGE_PROJECT_REVIEW,)).fetchone()["n"]
        self.assertEqual(count, 2)


# ---------------------------------------------------------------- D. 對外介面

class TestProjectReviewInterfaces(ProjectReviewMixin):

    def test_mcp_tool_exists_for_planner(self):
        """新增第 10 個 MCP 工具：PLANNER 用它回報結案判定與復盤。"""
        from fbf_engine import mcp_server
        names = [t["name"] for t in mcp_server.TOOLS]
        self.assertIn("fbflow_report_project_review", names)
        tool = [t for t in mcp_server.TOOLS
                if t["name"] == "fbflow_report_project_review"][0]
        self.assertEqual(tool["for_roles"], ["PLANNER"])
        self.assertIn("task_id", tool["required"])
        self.assertIn("expected_version", tool["required"])
        self.assertIn("action", tool["required"])
        action = tool["inputSchema"]["properties"]["action"]
        self.assertEqual(set(action["enum"]), set(workflow.PR_REVIEW_ACTIONS))

    def test_cli_review_commands_are_readable(self):
        """CLI 可讀：退件 LOG ＋ 引擎改善建議。"""
        import subprocess
        cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "fbf.py")
        env = dict(os.environ, FBF_PROJECT_ROOT=self.tmp)
        out = subprocess.run([sys.executable, cli, "review", "rejections"],
                             capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("rejections", data)

        out = subprocess.run([sys.executable, cli, "review", "improvements"],
                             capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("improvements", data)

    def test_dashboard_collects_review_data(self):
        """後台網頁顯示：結案檢視狀態、改善建議、退件紀錄。"""
        from fbf_engine import dashboard
        state = dashboard.collect_state(self.conn)
        for key in ("project_review", "improvements", "rejections"):
            self.assertIn(key, state)


if __name__ == "__main__":
    unittest.main()
