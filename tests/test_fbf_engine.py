"""FBF ENGINE 的核心測試。

全部用**暫存專案**，不碰任何真實專案資料。
真實 OpenCode 的回執／TUI／多 session 接力不在這裡測——
那必須標 `REAL_PASS` 或 `NOT_RUN`，不可用 mock 冒充（規格第 10 節）。

這裡注入的回執核對器是**測試替身**，只證明流程邏輯正確，
**不證明**真實平台的回執格式正確。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import coordinator as coordinator_module  # noqa: E402
from fbf_engine import (compact, engine, hostact, platform, prompts, receipts,  # noqa: E402
                         store, workflow)


def fake_verifier(endpoint, session_id, message_id, task_id,
                  dispatched_at=None, submitted_at=None, binary="opencode"):
    """測試替身：假平台回執核對器。**只證明流程，不證明平台。**"""
    return {"verified": True, "reason": "", "platform": "test",
            "message_id": message_id, "session_id": session_id,
            "model_id": "test/model", "context_id": "ctx-" + str(message_id),
            "finish": "stop", "detail": {}}


def unverified_verifier(endpoint, session_id, message_id, task_id,
                        dispatched_at=None, submitted_at=None, binary="opencode"):
    return {"verified": False, "reason": "receipt.unverified",
            "reason_detail": "測試：回執對不上", "message_id": message_id,
            "session_id": session_id, "model_id": "", "context_id": "",
            "finish": "", "detail": {}}


HANDOFF = {"ROLE": "TESTER", "OPEN WORK": "none", "SUBAGENTS RUNNING": "none",
           "RULES AND NUMBERS IN FORCE": "none", "RECIPES": "none",
           "DECISIONS AND WHY": "none", "LESSONS": "none", "SPEND": "0"}


class Harness(unittest.TestCase):
    """一個可跑完整流程的暫存專案。"""

    verifier = staticmethod(fake_verifier)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.coordinator = engine.Coordinator(self.tmp, verifier=self.verifier)
        self.addCleanup(self.coordinator.close)
        self._message_counter = 0
        self.conn = self.coordinator.conn
        # **測試絕不碰真的 OpenCode**：這台機器上有 opencode shim，
        # 真的去呼叫會把 OpenCode 拉起來然後卡住（而且會真的騷擾你的 session）。
        self._real_send = platform.send_to_session
        self._real_active = platform.active_sessions
        self._real_delivery = receipts.delivery_state
        # 預設模擬「派工成功」——測試不會碰到真的 OpenCode，也不會騷擾任何 session。
        # （要測失敗路徑的測試自己換掉這個 stub ✓）
        platform.send_to_session = lambda *a, **k: {"state": "queued", "channel": "test"}
        platform.active_sessions = lambda *a, **k: []
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERY_UNKNOWN
        # host CLI 掃描也照樣擋掉——`tick()` 的 auto-enroll 不能真的去查宿主
        # 現在開著哪些 session（那會把真的 handle 填進測試專案的席位）。
        self._real_host_scan = platform.hostterm.scan_sessions
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: []
        # **上下文收合的偵測也不碰真的 OpenCode**：它會去查本機 opencode server
        # 的訊息 → 「這個 session 有沒有對話紀錄」變成**看機器當下狀態**，
        # 測試紅綠不再由測試決定（實測：本機 server 一活著，
        # 閒置席位的 compact 提醒就跑出來，擾動「忙碌不該被打擾」的斷言）。
        self._real_inspect = compact.inspect_session
        compact.inspect_session = lambda *a, **k: {"has_history": False,
                                                    "last_compact": None}
        # **宿主活動判型也由測試決定**（預設「回合已結束、沒在動」＝idle）：
        # 逾時與喚醒現在要看宿主側狀態（使用者裁決 2026-11-25：
        # 只有「完全沒動靜」才催；在等模型／跑工具／剛壓縮都不算）。
        self._host_state = {"state": hostact.STATE_IDLE, "at": None, "source": "test"}
        self._real_activity = hostact.session_activity
        hostact.session_activity = lambda *a, **k: dict(self._host_state)
        self.addCleanup(self._restore_platform)
        self._seed_roles()
        self._seed_requirement()

    def _set_host_state(self, state, at=None):
        """設定測試裡的「宿主活動狀態」。"""
        self._host_state = {"state": state, "at": at, "source": "test"}

    def _restore_platform(self):
        platform.send_to_session = self._real_send
        platform.active_sessions = self._real_active
        receipts.delivery_state = self._real_delivery
        platform.hostterm.scan_sessions = self._real_host_scan
        compact.inspect_session = self._real_inspect
        hostact.session_activity = self._real_activity

    def _requeue_outbox(self):
        """把 outbox 退回「待送」——測試要重跑派送時用。"""
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE outbox SET status = 'queued', attempts = 0, deferrals = 0,"
                " next_attempt_at = NULL, last_error = NULL")
            self.conn.execute(
                "UPDATE works SET awaiting_human = 0, awaiting_reason = NULL")

    # ------------------------------------------------------------ 準備

    def _seed_roles(self):
        with store.tx(self.conn):
            for role, short, sid in (("PLANNER", "P1", "ses_planner"),
                                     ("THINKER", "T1", "ses_thinker"),
                                     ("WORKER", "W1", "ses_worker")):
                self.conn.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at) VALUES (?,?,?,?, 'active','','',?)",
                    (role, short, sid, "opencode", store.now()))

    def _seed_requirement(self, requirement_id="R-001", confirmed=True):
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                " created_at) VALUES (?,?,?,?,?)",
                (requirement_id, "做出可驗收的產物", "產物存在且雜湊相符",
                 store.now() if confirmed else None, store.now()))

    def make_work(self, work_id="W-001", milestone_id=""):
        return self.coordinator.create_work("測試工單", "R-001", "把東西做出來",
                                            [{"name": "out.txt"}], milestone_id)

    def version(self, work_id="W-001"):
        row = self.conn.execute("SELECT version FROM works WHERE id = ?",
                                (work_id,)).fetchone()
        return int(row["version"]) if row else 0

    def state(self, work_id="W-001"):
        row = self.conn.execute("SELECT state FROM works WHERE id = ?",
                                (work_id,)).fetchone()
        return row["state"] if row else ""

    def open_task(self, work_id="W-001", stage=""):
        sql = "SELECT * FROM tasks WHERE work_id = ? AND status = 'open'"
        args = [work_id]
        if stage:
            sql += " AND stage = ?"
            args.append(stage)
        return self.conn.execute(sql + " ORDER BY created_at DESC LIMIT 1", args).fetchone()

    def submit(self, stage, payload, session_id, work_id="W-001", message_id=""):
        """提交 → 跑一趟 tick。回傳 (提交結果, tick 報告)。

        **預設每筆提交用不同的 message_id**——真實情況下每次 MCP 呼叫的
        平台訊息都不同。共用同一個 message_id 會被「回執不得被其他提交使用」
        那條規則擋下（那是對的行為）。
        """
        if not message_id:
            self._message_counter += 1
            message_id = "msg-%04d" % self._message_counter
        task = self.open_task(work_id, stage)
        self.assertIsNotNone(task, "找不到 %s 階段的待辦" % stage)
        result = self.coordinator.submit_work(
            task["id"], work_id, self.version(work_id), session_id, stage,
            payload, message_id)
        report = self.coordinator.tick()
        return result, report

    # ------------------------------------------------------------ payload

    def design_payload(self, contexts=("ctx-a", "ctx-b"), **over):
        payload = {
            "solutions": [
                {"subagent_context_id": contexts[0], "approach": "做法甲",
                 "cost": "低", "change_scope": "小", "effort": "少",
                 "risk": "低", "verifiable_by": "跑測試", "reuses": "無"},
                {"subagent_context_id": contexts[1], "approach": "做法乙",
                 "cost": "高", "change_scope": "大", "effort": "多",
                 "risk": "中", "verifiable_by": "跑測試", "reuses": "無"},
            ],
            "design_note": "設計說明",
            "handoff": HANDOFF,
        }
        payload.update(over)
        return payload

    def rework_design_payload(self, contexts=("ctx-c", "ctx-d"), **over):
        """重工解法設計中：兩個獨立 SUBAGENT 的案子 ＋ 重工方案。"""
        payload = {
            "solutions": [
                {"subagent_context_id": contexts[0], "approach": "重工做法甲",
                 "cost": "低", "change_scope": "小", "effort": "少",
                 "risk": "低", "verifiable_by": "跑測試", "reuses": "無"},
                {"subagent_context_id": contexts[1], "approach": "重工做法乙",
                 "cost": "高", "change_scope": "大", "effort": "多",
                 "risk": "中", "verifiable_by": "跑測試", "reuses": "無"},
            ],
            "rework_plan": "改一改",
            "handoff": HANDOFF,
        }
        payload.update(over)
        return payload

    def rework_acceptance_payload(self, solution_id="ctx-c", threshold="100%", **over):
        """重工驗收方式設計中：選這一輪的解案 ＋ 每條 check。"""
        payload = {
            "selection": {"solution_id": solution_id, "reason": "成本最小",
                          "rejected_because": ["成本較高"]},
            "checks": [
                {"id": "AC-01", "requirement_id": "R-001", "method": "跑測試",
                 "scope": "全部", "threshold": threshold, "evidence": "out.txt",
                 "required": True, "execution_kind": "test"},
            ],
            "handoff": HANDOFF,
        }
        payload.update(over)
        return payload

    def _submit_rework_design(self, plan="改一改", contexts=("ctx-c", "ctx-d"),
                              threshold="100%"):
        """走完重工支線的前兩棒：rework_design → rework_acceptance_design。

        spec 第 7~8 步：THINKER 出兩案 → PLANNER 選案並設計驗收方式 → 才輪到 WORKER。
        """
        self.submit(workflow.REWORK_DESIGN,
                    self.rework_design_payload(contexts=contexts, rework_plan=plan),
                    "ses_thinker")
        self.submit(workflow.REWORK_ACCEPTANCE_DESIGN,
                    self.rework_acceptance_payload(solution_id=contexts[0],
                                                   threshold=threshold),
                    "ses_planner")

    def acceptance_payload(self, solution_id="ctx-a", **over):
        payload = {
            "selection": {"solution_id": solution_id, "reason": "成本最小",
                          "rejected_because": ["成本較高"]},
            "checks": [
                {"id": "AC-01", "requirement_id": "R-001", "method": "跑測試",
                 "scope": "全部", "threshold": "100%", "evidence": "out.txt",
                 "required": True, "execution_kind": "test"},
            ],
            "handoff": HANDOFF,
        }
        payload.update(over)
        return payload

    def build_payload(self, **over):
        path = os.path.join(self.tmp, "out.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("hello")
        size, digest = store.file_fingerprint(path)
        payload = {
            "outputs": [{"path": "out.txt", "size": size, "sha256": digest}],
            "done": ["做了"], "unfinished": [], "selfcheck": "跑過測試",
            "cases_ran": 3, "handoff": HANDOFF,
        }
        payload.update(over)
        return payload

    def verify_payload(self, status="PASS", **over):
        payload = {
            "results": [{"id": "AC-01", "status": status, "reason": "符合",
                         "evidence": {"path": "out.txt"}}],
            "cases_ran": 3,
            "handoff": HANDOFF,
        }
        payload.update(over)
        return payload


# ---------------------------------------------------------------- A. 狀態機

class TestStateMachine(Harness):

    def test_nine_states_and_transitions(self):
        """狀態的**英文字面**必須與規格一致——防止常數被蓋掉。

        spec 第 7~9 步的重工支線多一關「**重工驗收方式設計中**」
        （PLANNER 挑重工解法 → 設計驗收方式 → 發工單給 WORKER），
        所以是**十個**狀態。
        """
        self.assertEqual(set(workflow.WORK_STATES), {
            "backlog", "design", "acceptance_design", "build", "verify",
            "adjudicate", "rework_design", "rework_acceptance_design",
            "rework_build", "closed",
        })
        self.assertEqual(len(workflow.WORK_STATES), 10)
        self.assertEqual(len(workflow.WORK_TRANSITIONS), 10)
        self.assertEqual(workflow.REWORK_BUILD, "rework_build")
        self.assertEqual(workflow.REWORK_TYPE_BUILD, "施工錯")
        self.assertEqual(workflow.REWORK_TYPES,
                         (workflow.REWORK_TYPE_BUILD,
                          workflow.REWORK_TYPE_ACCEPTANCE,
                          workflow.REWORK_TYPE_SOLUTION))
        self.assertEqual(workflow.WORK_TRANSITIONS[workflow.ADJUDICATE],
                         (workflow.CLOSED, workflow.REWORK_DESIGN))
        self.assertEqual(workflow.WORK_TRANSITIONS[workflow.REWORK_DESIGN],
                         (workflow.REWORK_ACCEPTANCE_DESIGN,))
        self.assertEqual(workflow.WORK_TRANSITIONS[workflow.REWORK_ACCEPTANCE_DESIGN],
                         (workflow.REWORK_BUILD,))
        self.assertEqual(workflow.WORK_TRANSITIONS[workflow.REWORK_BUILD],
                         (workflow.VERIFY,))
        self.assertEqual(workflow.WORK_STATES[workflow.REWORK_BUILD]["role"], "WORKER")
        self.assertEqual(workflow.WORK_STATES[workflow.REWORK_DESIGN]["role"], "THINKER")
        self.assertEqual(workflow.WORK_STATES[workflow.REWORK_ACCEPTANCE_DESIGN]["role"],
                         "PLANNER")

    def test_grill_dispatched_when_no_confirmed_requirement(self):
        """三段啟動：沒有人類確認的需求 → tick 派 grill 給 PLANNER，不建工單。"""
        # 清掉 setUp 種的已確認需求 → 模擬剛 join 完的專案
        with store.tx(self.conn):
            self.conn.execute("UPDATE requirements SET confirmed_at = NULL")
        report = self.coordinator.tick()
        grill = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ?", (workflow.STAGE_GRILL,)).fetchone()
        self.assertIsNotNone(grill, "應派一張 grill task")
        self.assertEqual(grill["assigned_role"], "PLANNER")
        self.assertEqual(grill["status"], "open")
        outbox = self.conn.execute(
            "SELECT * FROM outbox WHERE task_id = ?", (grill["id"],)).fetchone()
        self.assertIsNotNone(outbox, "grill 提示詞要進 outbox")
        self.assertIn("GRILL", outbox["payload"])
        self.assertIn("fbflow_work_create", outbox["payload"])
        self.assertIn("requirement confirm", outbox["payload"])
        # 不建工單
        works = self.conn.execute("SELECT COUNT(*) AS n FROM works").fetchone()["n"]
        self.assertEqual(works, 0, "GRILL 段不得建工單")
        # 人類還沒同意 → 第 3 段（decompose）也不得派出
        d = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE stage = ?",
            (workflow.STAGE_DECOMPOSE,)).fetchone()["n"]
        self.assertEqual(d, 0, "未確認需求時不得派 decompose")
        # 重跑 tick 不重派
        self.coordinator.tick()
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE stage = ?",
            (workflow.STAGE_GRILL,)).fetchone()["n"]
        self.assertEqual(n, 1, "已有 open grill → 不重派")

    def test_grill_closed_when_requirement_confirmed(self):
        """需求一被確認，**engine 自己**收掉開著的 grill task（不是靠人手關）。

        這是引擎端的自動收尾路徑（`_dispatch_grill` 看到已確認需求）——
        若這裡改成「手動關 grill 再 tick」就測不到引擎有沒有真的收，
        跟 `REWORK_BUILD` 被蓋掉時一樣：兩邊照著同一個錯寫，測試全綠。
        """
        with store.tx(self.conn):
            self.conn.execute("UPDATE requirements SET confirmed_at = NULL")
        # grill 被派出
        self.coordinator.tick()
        grill = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ?", (workflow.STAGE_GRILL,)).fetchone()
        self.assertIsNotNone(grill)
        self.assertEqual(grill["status"], "open")
        # 人類確認需求（只動 requirement，**不**手動碰 grill task）
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE requirements SET confirmed_at = ? WHERE confirmed_at IS NULL",
                (store.now(),))
        # engine 下一趟 tick 自己收 grill
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (grill["id"],)).fetchone()
        self.assertEqual(row["status"], "done", "engine 應自己收掉 grill task")
        # 已有確認需求 → 不再派 grill
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_GRILL,)).fetchone()["n"]
        self.assertEqual(n, 0, "人類同意後不得再派 grill")
        # 同意後第 3 段開始：decompose 派給 PLANNER（還沒工單 → 要分解）
        dec = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_DECOMPOSE,)).fetchone()
        self.assertIsNotNone(dec, "需求已確認、尚無工單 → 應派 decompose")
        self.assertEqual(dec["assigned_role"], "PLANNER")
        payload = self.conn.execute(
            "SELECT payload FROM outbox WHERE task_id = ?", (dec["id"],)).fetchone()
        self.assertIsNotNone(payload)
        self.assertIn("fbflow_work_create", payload["payload"])
        self.assertIn("三句話", payload["payload"])

    def test_grill_skipped_when_requirement_already_confirmed(self):
        """setUp 已種 R-001 已確認 → 不派 grill（第 3 段 decompose 直接開始）。"""
        self.coordinator.tick()
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE stage = ?",
            (workflow.STAGE_GRILL,)).fetchone()["n"]
        self.assertEqual(n, 0, "已確認需求的專案不派 GRILL")

    def test_decompose_closed_when_first_work_created(self):
        """第 3 段守衛：工单一建出來，engine 自己收掉 decompose task。

        這測的是引擎自動收尾（`_dispatch_decompose` 看到 works > 0），
        不是人手關——同 `REWORK_BUILD` 那條教訓：兩邊照同一個錯寫，測試全綠。
        """
        # setUp 已種 R-001 已確認 → tick 派 decompose（沒有 grill）
        self.coordinator.tick()
        dec = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_DECOMPOSE,)).fetchone()
        self.assertIsNotNone(dec, "已確認需求、尚無工單 → 應派 decompose")
        self.assertEqual(dec["assigned_role"], "PLANNER")
        # PLANNER 用 fbflow_work_create 建出第一張工單（走真的建單路徑）
        self.coordinator.create_work("測試工單", "R-001", "把東西做出來",
                                     [{"name": "out.txt"}])
        # engine 下一趟 tick 自己收 decompose
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (dec["id"],)).fetchone()
        self.assertEqual(row["status"], "done",
                         "第一張工單建出後 engine 應自己收 decompose")
        # 不重派
        self.coordinator.tick()
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE stage = ? AND status = 'open'",
            (workflow.STAGE_DECOMPOSE,)).fetchone()["n"]
        self.assertEqual(n, 0, "已有工單 → 不得再派 decompose")

    def test_resolve_cli_falls_back_to_macos_app(self):
        """宿主 CLI 不在 PATH 時，走應用程式內附路徑（FBF 啟動自己要找得到）。"""
        from fbf_engine import hostterm
        import shutil as _shutil
        real_which = _shutil.which
        try:
            _shutil.which = lambda name: None
            old_env = os.environ.pop("FBF_HOST_CLI", None)
            try:
                resolved = hostterm.resolve_cli()
                if hostterm._MAC_APP_CLI and os.path.isfile(hostterm._MAC_APP_CLI):
                    self.assertEqual(resolved, hostterm._MAC_APP_CLI)
                else:
                    self.assertEqual(resolved, "", "沒有 PATH 也沒有應用程式 → 回空")
            finally:
                if old_env is not None:
                    os.environ["FBF_HOST_CLI"] = old_env
        finally:
            _shutil.which = real_which

    def test_illegal_transition_not_in_table(self):
        self.assertNotIn(workflow.CLOSED, workflow.WORK_TRANSITIONS[workflow.BUILD])
        self.assertNotIn(workflow.BUILD, workflow.WORK_TRANSITIONS[workflow.DESIGN])
        self.assertEqual(workflow.WORK_TRANSITIONS[workflow.CLOSED], ())

    def test_missing_required_field_blocks_advance(self):
        work_id = self.make_work()
        self.coordinator.tick()
        payload = self.design_payload()
        del payload["design_note"]
        _, report = self.submit(workflow.DESIGN, payload, "ses_thinker")
        self.assertIn("S-000001", report["rejected"])
        self.assertEqual(self.state(work_id), workflow.DESIGN)


# ---------------------------------------------------------------- B. 守衛

class TestGuards(Harness):

    def test_one_solution_is_too_few(self):
        self.make_work()
        self.coordinator.tick()
        payload = self.design_payload()
        payload["solutions"] = payload["solutions"][:1]
        _, report = self.submit(workflow.DESIGN, payload, "ses_thinker")
        self.assertIn("S-000001", report["rejected"])
        row = self.conn.execute("SELECT gaps FROM submissions WHERE id = 'S-000001'").fetchone()
        self.assertIn(workflow.GAP_SOLUTIONS_COUNT, row["gaps"])

    def test_two_identical_solutions_rejected(self):
        self.make_work()
        self.coordinator.tick()
        payload = self.design_payload()
        payload["solutions"][1]["approach"] = payload["solutions"][0]["approach"]
        payload["solutions"][1]["cost"] = payload["solutions"][0]["cost"]
        payload["solutions"][1]["change_scope"] = payload["solutions"][0]["change_scope"]
        _, report = self.submit(workflow.DESIGN, payload, "ses_thinker")
        row = self.conn.execute("SELECT gaps FROM submissions WHERE id = 'S-000001'").fetchone()
        self.assertIn(workflow.GAP_SOLUTION_TOO_SIMILAR, row["gaps"])

    def test_two_identical_solutions_rejected(self):
        """兩案**不得實質相同**（成本與修改範圍不能都一樣）。

        SUBAGENT 取消後，這是「兩案不同」唯一的機器檢查。
        """
        self.make_work()
        self.coordinator.tick()
        payload = self.design_payload(contexts=("ctx-a", "ctx-b"))
        payload["solutions"][1] = dict(payload["solutions"][0])
        self.submit(workflow.DESIGN, payload, "ses_thinker")
        row = self.conn.execute("SELECT gaps FROM submissions WHERE id = 'S-000001'").fetchone()
        self.assertIn(workflow.GAP_SOLUTION_TOO_SIMILAR, row["gaps"])

    def test_unknown_requirement_id_rejected(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        payload = self.acceptance_payload()
        payload["checks"][0]["requirement_id"] = "R-999"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE id = 'S-000002'").fetchone()
        self.assertIn(workflow.GAP_CHECK_REQUIREMENT_UNKNOWN, row["gaps"])

    def test_selection_unknown_solution_rejected(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN,
                    self.acceptance_payload(solution_id="ctx-zzz"), "ses_planner")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE id = 'S-000002'").fetchone()
        self.assertIn(workflow.GAP_SELECTION_UNKNOWN_SOLUTION, row["gaps"])

    def test_cases_ran_zero_rejected_for_test_kind(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        payload = self.build_payload()
        payload["cases_ran"] = 0
        self.submit(workflow.BUILD, payload, "ses_worker")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE id = 'S-000003'").fetchone()
        self.assertIn(workflow.GAP_CASES_ZERO, row["gaps"])

    def test_review_kind_allows_zero_cases(self):
        """純文件／人工檢視的 check 可以 0 案例，但必須自報 review。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        payload = self.acceptance_payload()
        payload["checks"][0]["execution_kind"] = "review"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        build = self.build_payload()
        build["cases_ran"] = 0
        build["execution_kind"] = "review"
        _, report = self.submit(workflow.BUILD, build, "ses_worker")
        self.assertTrue(report["applied"], report)

    def test_evidence_file_missing_rejected(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        payload = self.build_payload()
        payload["outputs"] = [{"path": "not-there.txt"}]
        self.submit(workflow.BUILD, payload, "ses_worker")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE id = 'S-000003'").fetchone()
        self.assertIn(workflow.GAP_EVIDENCE_MISSING, row["gaps"])

    def test_evidence_outside_project_root_rejected(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        payload = self.build_payload()
        payload["outputs"] = [{"path": "../outside.txt"}]
        self.submit(workflow.BUILD, payload, "ses_worker")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE id = 'S-000003'").fetchone()
        self.assertIn(workflow.GAP_EVIDENCE_MISSING, row["gaps"])

    def test_self_verify_rejected(self):
        """驗收者不得是本工單施工階段的執行者。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        # 把 verify 的待辦改派給施工者
        task = self.open_task(stage=workflow.VERIFY)
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET assigned_session = ? WHERE id = ?",
                              ("ses_worker", task["id"]))
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_worker")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE id = 'S-000004'").fetchone()
        self.assertIn(workflow.GAP_SELF_VERIFY, row["gaps"])

    def test_close_requires_all_required_pass(self):
        """required check 沒全 PASS 就不能結案。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(status="FAIL"), "ses_thinker")
        payload = {"action": "close", "close_reason": "想結案", "handoff": HANDOFF}
        _, report = self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertIn("S-000005", report["rejected"])
        self.assertNotEqual(self.state(), workflow.CLOSED)


# ---------------------------------------------------------------- C. 迴圈守護

class TestLoopGuards(Harness):

    def _to_adjudicate(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")

    def test_rework_limit_then_review_required(self):
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "做得不對",
                   "rework_type": workflow.REWORK_TYPE_BUILD, "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_DESIGN)
        self._submit_rework_design("改一改")
        self.assertEqual(self.state(), workflow.REWORK_BUILD)
        self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
        self.assertEqual(self.state(), workflow.VERIFY)
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)
        # 第二次退回 → 應該還能（上限 2）
        _, report = self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertTrue(report["applied"], report)
        self.assertTrue(report["applied"][0]["submission"].startswith("S-"))

    def test_rework_beyond_limit_needs_review(self):
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "做得不對",
                   "rework_type": workflow.REWORK_TYPE_BUILD, "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self._submit_rework_design("改一改", contexts=("ctx-c1", "ctx-d1"))
        self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_DESIGN)
        self._submit_rework_design("再改", contexts=("ctx-c2", "ctx-d2"))
        self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        # 第三次退回 → 必須改走重新檢討
        _, report = self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertTrue(report["rejected"], report)
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(workflow.GAP_REWORK_LIMIT, row["gaps"])

    def test_review_with_diagnosis_acceptance_goes_back_to_acceptance(self):
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "驗收方式訂錯了",
                   "rework_type": workflow.REWORK_TYPE_ACCEPTANCE, "handoff": HANDOFF}
        # 兩次退回，第二次之後必須走重新檢討
        for round_no in (1, 2):
            self.submit(workflow.ADJUDICATE, payload, "ses_planner")
            self._submit_rework_design("改驗收方式",
                                       contexts=("ctx-c%d" % round_no, "ctx-d%d" % round_no))
            self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
            self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)
        review = {"action": "review", "diagnosis": "acceptance", "remedy": "revise",
                  "handoff": HANDOFF}
        _, report = self.submit(workflow.ADJUDICATE, review, "ses_planner")
        self.assertTrue(report["applied"], report)
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)

    def test_review_with_work_diagnosis_cannot_revise(self):
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "做得不對",
                   "rework_type": workflow.REWORK_TYPE_BUILD, "handoff": HANDOFF}
        for round_no in (1, 2):
            self.submit(workflow.ADJUDICATE, payload, "ses_planner")
            self._submit_rework_design("改",
                                       contexts=("ctx-c%d" % round_no, "ctx-d%d" % round_no))
            self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
            self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        review = {"action": "review", "diagnosis": "work", "remedy": "revise",
                  "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, review, "ses_planner")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(workflow.GAP_REVIEW_REVISE_NOT_ALLOWED, row["gaps"])

    def test_revision_cannot_lower_threshold(self):
        """修訂不得放寬門檻。"""
        self._revision_path()
        payload = self.acceptance_payload()
        payload["checks"][0]["threshold"] = "50%"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(workflow.GAP_CHECK_THRESHOLD_LOWERED, row["gaps"])

    def test_revision_cannot_unrequire(self):
        self._revision_path()
        payload = self.acceptance_payload()
        payload["checks"][0]["required"] = False
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(workflow.GAP_CHECK_REQUIRED_DOWNGRADED, row["gaps"])

    def _revision_path(self):
        """走到「已經有驗收結果、而且回到驗收方式設計中」的位置。"""
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "驗收方式訂錯了",
                   "rework_type": workflow.REWORK_TYPE_ACCEPTANCE, "handoff": HANDOFF}
        for round_no in (1, 2):
            self.submit(workflow.ADJUDICATE, payload, "ses_planner")
            self._submit_rework_design("改驗收方式",
                                       contexts=("ctx-c%d" % round_no, "ctx-d%d" % round_no))
            self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
            self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        review = {"action": "review", "diagnosis": "acceptance", "remedy": "revise",
                  "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, review, "ses_planner")
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)


# ---------------------------------------------------------------- D. 提交語意

class TestSubmissionSemantics(Harness):

    def test_same_submission_applied_only_once(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        version = self.version()
        payload = self.design_payload()
        first = self.coordinator.submit_work(task["id"], "W-001", version,
                                             "ses_thinker", workflow.DESIGN, payload, "m1")
        second = self.coordinator.submit_work(task["id"], "W-001", version,
                                              "ses_thinker", workflow.DESIGN, payload, "m1")
        self.assertEqual(first["submission_id"], second["submission_id"])
        self.assertTrue(second["idempotent"])
        report = self.coordinator.tick()
        self.coordinator.tick()
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE action = 'applied'"
            " AND target_id = 'W-001'").fetchone()
        self.assertEqual(rows["n"], 1)

    def test_stale_version_marked_stale_not_applied(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        self.coordinator.submit_work(task["id"], "W-001", 999,
                                     "ses_thinker", workflow.DESIGN,
                                     self.design_payload(), "m1")
        report = self.coordinator.tick()
        self.assertTrue(report["stale"])
        self.assertEqual(self.state(), workflow.DESIGN)

    def test_rejection_bumps_version_so_same_version_cannot_loop(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        version = self.version()
        payload = self.design_payload()
        del payload["design_note"]
        self.coordinator.submit_work(task["id"], "W-001", version, "ses_thinker",
                                     workflow.DESIGN, payload, "m1")
        self.coordinator.tick()
        self.assertNotEqual(self.version(), version)

    def test_rejection_redispatches_with_the_reason(self):
        """退件 → **重新派一棒**，而且提示詞帶著「缺哪個欄位」。

        守衛是在 tick 裡跑的，角色交件當下只會收到「已收到」——
        退件結果是事後才有的。沒有重新派工，沒有人知道要重交，
        工單就靜靜停在原地。
        """
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        payload = self.design_payload()
        del payload["design_note"]
        self.coordinator.submit_work(task["id"], "W-001", self.version(),
                                     "ses_thinker", workflow.DESIGN, payload, "m1")
        report = self.coordinator.tick()
        self.assertTrue(report["rejected"], report)
        nxt = self.open_task(stage=workflow.DESIGN)
        self.assertIsNotNone(nxt, "退件後沒有重新派工 → 沒有人知道要重交")
        self.assertNotEqual(nxt["id"], task["id"])
        sent = self.conn.execute(
            "SELECT payload FROM outbox WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (nxt["id"],)).fetchone()["payload"]
        self.assertIn("退件", sent)
        self.assertIn("design_note", sent)      # 缺哪個欄位
        self.assertIn("handoff", sent)          # 交接檔也要

    def test_too_many_rejections_stop_and_wait_for_a_human(self):
        """退件太多次 → 標待人處理（不允許無限繞圈）。"""
        self.make_work()
        for _ in range(engine.MAX_STAGE_REJECTIONS + 1):
            self.coordinator.tick()
            payload = self.design_payload()
            del payload["design_note"]
            self.submit(workflow.DESIGN, payload, "ses_thinker")
        row = self.conn.execute(
            "SELECT awaiting_human, awaiting_reason FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(row["awaiting_human"], 1)
        self.assertEqual(row["awaiting_reason"], "submit_rejected")

    def test_dispatch_resumes_when_next_role_claims_seat(self):
        self.make_work()
        self.coordinator.tick()
        with store.tx(self.conn):
            self.conn.execute("UPDATE roles SET session_id = NULL WHERE role = 'PLANNER'")
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)
        self.assertIsNone(self.open_task(stage=workflow.ACCEPTANCE_DESIGN))

        sent = []

        def send(root, row, text, **kwargs):
            sent.append((row["session_id"], text))
            return {"state": "submitted"}

        platform.send_to_session = send
        with store.tx(self.conn):
            self.conn.execute("UPDATE roles SET session_id = 'ses_planner' WHERE role = 'PLANNER'")
        self.coordinator.tick()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.ACCEPTANCE_DESIGN)
        self.assertIsNotNone(task)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "ses_planner")
        self.assertIn(task["id"], sent[0][1])
        self.assertIn("W-001", sent[0][1])
        self.assertIn("fbflow_read", sent[0][1])

    def test_awaiting_human_park_is_cleared_when_receipt_arrives(self):
        """**回執逾時停等人 → 回執對上了 → 必須解除標記並派出下一棒。**

        不解除的話，下一棒（PLANNER）的 task 建不出來
        （`_create_task_for_state` 看到 `awaiting_human` 就回頭）→
        工單靜靜卡在原地、沒有錯誤訊息、也沒有人接手。
        """
        self.make_work()
        self.coordinator.tick()
        # 先模擬「回執一直對不上」→ 停等人
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE works SET awaiting_human = 1, awaiting_reason = 'receipt_timeout'"
                " WHERE id = 'W-001'")
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)
        row = self.conn.execute(
            "SELECT awaiting_human, awaiting_reason FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(row["awaiting_human"], 0)
        self.assertIsNone(row["awaiting_reason"])
        # 下一棒真的派出去了（PLANNER 接手）
        nxt = self.open_task(stage=workflow.ACCEPTANCE_DESIGN)
        self.assertIsNotNone(nxt, "下一棒沒有派出來 → 沒有人接手")
        self.assertEqual(nxt["assigned_role"], "PLANNER")


class TestReceipts(Harness):
    verifier = staticmethod(unverified_verifier)

    def test_missing_message_id_is_rejected_not_retried(self):
        """沒有 message id 是永久性問題：重試沒用，要退回要求重交。"""
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        self.coordinator.submit_work(task["id"], "W-001", self.version(),
                                     "ses_thinker", workflow.DESIGN,
                                     self.design_payload(), "")
        report = self.coordinator.tick()
        self.assertTrue(report["rejected"], report)
        row = self.conn.execute(
            "SELECT status, gaps FROM submissions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.SUB_REJECTED)
        self.assertIn("receipt.message_id_missing", row["gaps"])

    def test_unverified_does_not_advance(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        self.coordinator.submit_work(task["id"], "W-001", self.version(),
                                     "ses_thinker", workflow.DESIGN,
                                     self.design_payload(), "m1")
        self.coordinator.tick()
        self.assertEqual(self.state(), workflow.DESIGN)
        row = self.conn.execute(
            "SELECT status FROM submissions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.SUB_UNVERIFIED)


class TestMessageReuse(Harness):
    """同一個 message_id 被別的提交用過 → 這不是可信回執（規格第 7 節）。"""

    def test_reused_message_id_is_rejected(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker",
                    message_id="msg-shared")
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)
        # 下一棒換人，但沿用同一個 message_id → 必須被擋
        _, report = self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(),
                                "ses_planner", message_id="msg-shared")
        self.assertTrue(report["rejected"], report)
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(engine.REASON_RECEIPT_MESSAGE_REUSED, row["gaps"])
        self.assertNotEqual(self.state(), workflow.BUILD)

    def test_reused_message_id_lets_role_resubmit_with_new_one(self):
        """被擋之後版本 +1，角色可以用新的 message_id 再交一次。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker",
                    message_id="msg-shared")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(),
                    "ses_planner", message_id="msg-shared")
        _, report = self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(),
                                "ses_planner", message_id="msg-fresh")
        self.assertTrue(report["applied"], report)
        self.assertEqual(self.state(), workflow.BUILD)

    def test_recorded_receipt_id_points_to_a_real_row(self):
        """寫下的回執 ID 必須真的在資料庫裡（不得回傳沒寫進去的 ID）。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker",
                    message_id="msg-one")
        row = self.conn.execute(
            "SELECT receipt_id FROM decisions WHERE action = 'applied' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertTrue(row["receipt_id"])
        exists = self.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE id = ?", (row["receipt_id"],)).fetchone()
        self.assertEqual(exists["n"], 1)


class TestLease(Harness):

    def test_second_coordinator_cannot_take_lease(self):
        other = engine.Coordinator(self.tmp, verifier=fake_verifier)
        self.addCleanup(other.close)
        self.assertTrue(self.coordinator.acquire())
        self.assertFalse(other.acquire())

    def test_expired_lease_can_be_taken_with_new_generation(self):
        other = engine.Coordinator(self.tmp, verifier=fake_verifier)
        self.addCleanup(other.close)
        self.assertTrue(self.coordinator.acquire())
        # 讓租約過期
        with store.tx(self.conn):
            self.conn.execute("UPDATE leases SET expires_at = 0 WHERE name = ?",
                              (store.LEASE_COORDINATOR,))
        self.assertTrue(other.acquire())
        generation = store.read_lease(self.conn)["generation"]
        self.assertGreater(generation, 1)
        # 舊進程不得續約
        self.assertFalse(self.coordinator.renew())

    def test_old_generation_cannot_write(self):
        other = engine.Coordinator(self.tmp, verifier=fake_verifier)
        self.addCleanup(other.close)
        self.assertTrue(self.coordinator.acquire())
        with store.tx(self.conn):
            self.conn.execute("UPDATE leases SET expires_at = 0 WHERE name = ?",
                              (store.LEASE_COORDINATOR,))
        self.assertTrue(other.acquire())
        self.assertFalse(store.lease_valid(self.conn, self.coordinator.owner,
                                            self.coordinator.generation))
        self.assertTrue(store.lease_valid(self.conn, other.owner, other.generation))


# ---------------------------------------------------------------- F. 派工

class TestDispatch(Harness):

    def test_pick_least_loaded_then_longest_unassigned(self):
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO roles(role, short_name, session_id, agent, status,"
                " project_code, workdir, created_at) VALUES ('THINKER','T2','ses_t2',"
                "'opencode','active','','',?)", (store.now(),))
            # T1 已經有一張未完的單
            self.conn.execute(
                "INSERT INTO works(id, title, requirement_id, state, version,"
                " assigned_session, created_at, updated_at)"
                " VALUES ('W-099','舊單','R-001','build',1,'ses_thinker',?,?)",
                (store.now(), store.now()))
        picked = self.coordinator._pick_session("THINKER")
        self.assertEqual(picked["session_id"], "ses_t2")

    def test_milestone_order_blocks_later_segment(self):
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO milestones(id, seq, name, outcome, state, review_count,"
                " created_at, updated_at) VALUES ('MS-001',1,'A','第一段','open',0,?,?)",
                (store.now(), store.now()))
            self.conn.execute(
                "INSERT INTO milestones(id, seq, name, outcome, state, review_count,"
                " created_at, updated_at) VALUES ('MS-002',2,'B','第二段','open',0,?,?)",
                (store.now(), store.now()))
        self.coordinator.create_work("第一段", "R-001", "c", [], "MS-001")
        self.coordinator.create_work("第二段", "R-001", "c", [], "MS-002")
        self.coordinator.tick()
        dispatched = [d["work"] for d in self.coordinator.tick()["dispatched"]]
        # 第二段的單不該被派出
        rows = self.conn.execute(
            "SELECT work_id FROM tasks ORDER BY created_at").fetchall()
        work_ids = {r["work_id"] for r in rows}
        self.assertIn("W-001", work_ids)
        self.assertNotIn("W-002", work_ids)

    def _presence_defer(self):
        """把暫緩的退避時間清掉（模擬時間過去），再跑一趟 tick。"""
        with store.tx(self.conn):
            self.conn.execute("UPDATE outbox SET next_attempt_at = NULL")
        return self.coordinator.tick()

    def test_presence_unknown_still_dispatches(self):
        """**查不到 presence 也要送。**

        派工的依據是**工單狀態**（哪一棒該誰做）；presence 只是
        「現在打擾它好不好」的參考。查不到就等於整條流程卡死
        （實測就是這樣卡了兩小時，14 張單全部送不出去）。
        """
        self.make_work()
        self.coordinator.tick()
        self._requeue_outbox()
        sent = []
        real_send = platform.send_to_session
        real_active = platform.active_sessions

        def no_server(*args, **kwargs):
            raise platform.PresenceUnknown("Could not reach server")

        platform.send_to_session = lambda *a, **k: sent.append(1) or {"state": "queued"}
        platform.active_sessions = no_server
        try:
            report = self.coordinator.tick()
        finally:
            platform.send_to_session = real_send
            platform.active_sessions = real_active
        self.assertTrue(sent, "presence 查不到就不送 → 流程會癱瘓")
        self.assertEqual(report["deferred"], [])

    def test_presence_busy_is_deferred_and_bounded(self):
        """**忙碌**才暫緩（不打擾正在工作的 session），而且暫緩有上限。

        上限到了就標待人處理——**不允許無限等待**。
        """
        self.make_work()
        self.coordinator.tick()
        self._requeue_outbox()
        with store.tx(self.conn):
            self.conn.execute("UPDATE settings SET value = '2' WHERE key = 'max_deferrals'")
        real_active = platform.active_sessions
        platform.active_sessions = lambda *a, **k: ["ses_thinker"]   # 忙碌中
        try:
            self._presence_defer()
            self._presence_defer()
            self._presence_defer()
        finally:
            platform.active_sessions = real_active
        row = self.conn.execute(
            "SELECT status, deferrals FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.OUT_FAILED)
        work = self.conn.execute(
            "SELECT awaiting_human, awaiting_reason FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(work["awaiting_human"], 1)
        self.assertEqual(work["awaiting_reason"], "dispatch_deferred")

    def test_unknown_delivery_is_not_blindly_resent(self):
        """送達狀態不明 → **先查收件方收沒收到**，不得盲目重送。"""
        self.make_work()
        self.coordinator.tick()
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE outbox SET status = ?, next_attempt_at = NULL",
                (engine.OUT_UNKNOWN,))
        before = int(self.conn.execute(
            "SELECT attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["attempts"])
        original = receipts.delivery_state
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERED
        try:
            self.coordinator.tick()
        finally:
            receipts.delivery_state = original
        row = self.conn.execute(
            "SELECT status, attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.OUT_DELIVERED)
        # 沒有真的重送（attempts 沒增加）
        self.assertEqual(row["attempts"], before)

    def test_unknown_delivery_keeps_waiting_when_platform_unreachable(self):
        """查不到收件狀態 → 不重送，維持 unknown 等下一輪。"""
        self.make_work()
        self.coordinator.tick()
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE outbox SET status = ?, next_attempt_at = NULL",
                (engine.OUT_UNKNOWN,))
        before = int(self.conn.execute(
            "SELECT attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["attempts"])
        original = receipts.delivery_state
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERY_UNKNOWN
        try:
            self.coordinator.tick()
        finally:
            receipts.delivery_state = original
        row = self.conn.execute(
            "SELECT status, attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.OUT_UNKNOWN)
        self.assertEqual(row["attempts"], before)


# ---------------------------------------------------------------- G. 里程碑

class TestMilestone(Harness):

    def _closed_work(self, milestone_id=""):
        work_id = self.make_work(milestone_id=milestone_id)
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.submit(workflow.ADJUDICATE,
                    {"action": "close", "close_reason": "完成", "handoff": HANDOFF},
                    "ses_planner")
        return work_id

    def test_milestone_moves_to_reviewing_when_all_closed(self):
        milestone_id = self.coordinator.create_milestone(1, "A", "第一段成果")
        self._closed_work(milestone_id)
        self.coordinator.tick()
        row = self.conn.execute("SELECT * FROM milestones WHERE id = ?",
                                (milestone_id,)).fetchone()
        self.assertEqual(row["state"], workflow.MS_REVIEWING)

    def test_milestone_continue_marks_done(self):
        milestone_id = self.coordinator.create_milestone(1, "A", "第一段成果")
        self._closed_work(milestone_id)
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = 'milestone_review'").fetchone()
        result = self.coordinator.apply_milestone_review(
            task["id"], milestone_id, 1, "ses_planner", workflow.MS_CONTINUE, "達成")
        self.assertTrue(result["ok"])
        row = self.conn.execute("SELECT * FROM milestones WHERE id = ?",
                                (milestone_id,)).fetchone()
        self.assertEqual(row["state"], workflow.MS_DONE)

    def test_milestone_invalid_action_rejected(self):
        milestone_id = self.coordinator.create_milestone(1, "A", "第一段成果")
        self._closed_work(milestone_id)
        self.coordinator.tick()
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = 'milestone_review'").fetchone()
        result = self.coordinator.apply_milestone_review(
            task["id"], milestone_id, 1, "ses_planner", "whatever", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "action.invalid")


# ---------------------------------------------------------------- H. 結案與追溯

class TestTraceability(Harness):

    def test_project_close_requires_both_directions(self):
        self.make_work()
        self.coordinator.tick()
        result = self.coordinator.project_close("human")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "traceability.incomplete")

    def test_project_close_passes_after_full_delivery(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.submit(workflow.ADJUDICATE,
                    {"action": "close", "close_reason": "完成", "handoff": HANDOFF},
                    "ses_planner")
        result = self.coordinator.project_close("human")
        self.assertTrue(result["ok"], result)


# ---------------------------------------------------------------- I. 待人處理

class TestAwaiting(Harness):

    def test_awaiting_list_and_resolve(self):
        self.make_work()
        self.coordinator.tick()
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE works SET awaiting_human = 1, awaiting_reason = 'rework_limit'"
                " WHERE id = 'W-001'")
        items = self.coordinator.awaiting_list()
        self.assertEqual(len(items), 1)
        result = self.coordinator.awaiting_resolve("W-001", "grant_more", "再給一次")
        self.assertTrue(result["ok"])
        row = self.conn.execute(
            "SELECT awaiting_human, rework_count FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(row["awaiting_human"], 0)
        self.assertEqual(row["rework_count"], 0)

    def test_invalid_resolution_rejected(self):
        result = self.coordinator.awaiting_resolve("W-001", "whatever", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "action.invalid")


# ---------------------------------------------------------------- J. 完整路徑

class TestGoldenPath(Harness):

    def test_full_delivery_to_closed(self):
        work_id = self.make_work()
        self.coordinator.tick()
        self.assertEqual(self.state(), workflow.DESIGN)
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.assertEqual(self.state(), workflow.BUILD)
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.assertEqual(self.state(), workflow.VERIFY)
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)
        self.submit(workflow.ADJUDICATE,
                    {"action": "close", "close_reason": "完成", "handoff": HANDOFF},
                    "ses_planner")
        self.assertEqual(self.state(), workflow.CLOSED)
        row = self.conn.execute("SELECT verdict FROM works WHERE id = ?",
                                (work_id,)).fetchone()
        self.assertEqual(row["verdict"], "pass")
        decisions = self.conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE target_id = ?", (work_id,)).fetchone()
        self.assertGreater(decisions["n"], 0)


class TestDeliveryClassification(unittest.TestCase):
    """「查不到 task id」**不等於**沒送達——視窗被截斷時證明不了。"""

    @staticmethod
    def _message(created, text=""):
        return {"info": {"id": "m%s" % created, "role": "user",
                         "time": {"created": created}},
                "parts": [{"type": "text", "text": text}]}

    def test_found_task_is_delivered(self):
        messages = [self._message(1000, "TASK T-000001")]
        self.assertEqual(receipts._classify_delivery(messages, "T-000001", 500),
                         receipts.DELIVERED)

    def test_window_covering_dispatch_time_proves_not_delivered(self):
        messages = [self._message(400, "早於派送的消息")]
        self.assertEqual(receipts._classify_delivery(messages, "T-000001", 500),
                         receipts.NOT_DELIVERED)

    def test_truncated_window_cannot_prove_not_delivered(self):
        """視窗最早的一則比派送還新 → 舊的可能被截掉 → 證明不了。"""
        messages = [self._message(900, "比派送還新的消息")]
        self.assertEqual(receipts._classify_delivery(messages, "T-000001", 500),
                         receipts.DELIVERY_UNKNOWN)

    def test_without_dispatch_time_cannot_prove_not_delivered(self):
        messages = [self._message(400, "早於派送的消息")]
        self.assertEqual(receipts._classify_delivery(messages, "T-000001", None),
                         receipts.DELIVERY_UNKNOWN)

    def test_unreadable_messages_are_unknown(self):
        self.assertEqual(receipts.delivery_state("", "ses_x", "T-1"),
                         receipts.DELIVERY_UNKNOWN)


class TestOutboxDeliverySafety(Harness):
    """送達狀態不明的三條路徑都必須先查；確定失敗才有指數退避重試。"""

    def _set_status(self, status, age_seconds=0):
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE outbox SET status = ?, next_attempt_at = NULL, updated_at = ?",
                (status, store.now(time.time() - age_seconds)))

    def test_stale_submitted_row_is_delivery_checked(self):
        """逾時的 `submitted`（CLI 已啟動、未確認送達）→ 也要查。

        派工靜默失敗時**不會有任何錯誤訊號**；不查就永遠停在 submitted，
        工單沒有人接手也沒有人知道（最難查的那種卡住）。
        """
        self.make_work()
        self.coordinator.tick()
        self._set_status(engine.OUT_SUBMITTED, age_seconds=600)
        original = receipts.delivery_state
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERED
        try:
            self.coordinator.tick()
        finally:
            receipts.delivery_state = original
        row = self.conn.execute(
            "SELECT status FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.OUT_DELIVERED)

    def test_stale_sending_row_is_delivery_checked(self):
        """逾時的 `sending`（進程被殺）同樣是狀態不明 → 先查，不直接重送。"""
        self.make_work()
        self.coordinator.tick()
        self._set_status(engine.OUT_SENDING, age_seconds=300)
        before = int(self.conn.execute(
            "SELECT attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["attempts"])
        original = receipts.delivery_state
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERED
        try:
            self.coordinator.tick()
        finally:
            receipts.delivery_state = original
        row = self.conn.execute(
            "SELECT status, attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.OUT_DELIVERED)
        self.assertEqual(row["attempts"], before)

    def test_stale_sending_row_not_resent_when_unprovable(self):
        self.make_work()
        self.coordinator.tick()
        self._set_status(engine.OUT_SENDING, age_seconds=300)
        before = int(self.conn.execute(
            "SELECT attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()["attempts"])
        original = receipts.delivery_state
        receipts.delivery_state = lambda *a, **k: receipts.DELIVERY_UNKNOWN
        try:
            self.coordinator.tick()
        finally:
            receipts.delivery_state = original
        row = self.conn.execute(
            "SELECT status, attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.OUT_UNKNOWN)
        self.assertEqual(row["attempts"], before)

    def test_orphan_wakeup_stays_failed_and_stops_calling_the_host(self):
        """**標成 failed 之後不准再走一次送出路徑**——否則每 30 秒敲一次宿主 CLI。

        實測（2026-11-24 線上）：outbox 3（死 handle 的喚醒）累積 40+ 次
        `dispatch.verify unknown` → 一個沒有收件人的喚醒，每 30 秒打一次平台。
        """
        self.make_work()
        self.coordinator.tick()
        # 造一筆收件人已經不是任何席位的喚醒
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO outbox(task_id, kind, recipient_session, recipient_role,"
                " target_id, payload, status, attempts, created_at, updated_at)"
                " VALUES ('WUdead000001', 'wakeup', 'term_dead', 'WORKER', '', '',"
                " 'unknown', 3, ?, ?)", (store.now(), store.now()))
        for _ in range(3):
            self.coordinator.tick()
        row = self.conn.execute(
            "SELECT status, attempts FROM outbox WHERE task_id = 'WUdead000001'").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(int(row["attempts"]), 3,
                         "孤兒喚醒還在重試 → 每 30 秒敲一次宿主 CLI")

    def test_wakeup_to_a_dead_session_is_dropped_not_retried_forever(self):
        """**喚醒類 outbox 的收件人已死 → 不再每 30 秒重查一次。**

        喚醒的 `task_id` 是錨點 `WU…`（不是 task 編號）→ 「task 已關閉」
        那條孤兒檢查撿不到它 → 收件人死後**永遠重查**（實測累積 27 筆
        `dispatch.verify unknown`），outbox 也永遠佔著。
        """
        self.make_work()
        self.coordinator.tick()
        # 造一筆「喚醒」派工，收件人是一個**沒有任何席位**的 session。
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO outbox(task_id, kind, recipient_session,"
                " recipient_role, target_id, payload, status, attempts,"
                " deferrals, created_at, updated_at)"
                " VALUES ('WUdeadbeef0000','wakeup','ses_gone','THINKER','W-001',"
                " '{}','unknown',1,27,?,?)", (store.now(), store.now()))
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT status, deferrals FROM outbox WHERE task_id = 'WUdeadbeef0000'"
        ).fetchone()
        self.assertEqual(row["status"], engine.OUT_FAILED,
                         "收件人已死的喚醒派工沒有被清掉 → 會永遠重查")
        self.assertEqual(row["deferrals"], 27, "不該再累積重試次數")

    def test_proven_not_delivered_is_resent(self):
        self.make_work()
        self.coordinator.tick()
        self._set_status(engine.OUT_SENDING, age_seconds=300)
        sent = []
        real_send = platform.send_to_session
        real_presence = platform.presence_of

        def spy(root, row, text, **kwargs):
            sent.append(text)
            return {"state": "queued", "channel": "test"}

        platform.send_to_session = spy
        platform.presence_detail = lambda *a, **k: (platform.IDLE, "")
        original = receipts.delivery_state
        receipts.delivery_state = lambda *a, **k: receipts.NOT_DELIVERED
        try:
            self.coordinator.tick()
        finally:
            receipts.delivery_state = original
            platform.send_to_session = real_send
            platform.presence_detail = real_presence
        self.assertEqual(len(sent), 1)

    def test_unknown_presence_still_dispatches(self):
        """**查不到 presence 也要送。**

        派工的依據是**工單狀態**（哪一棒該誰做），不是平台的 presence——
        查不到只代表「我們問不到」，不代表那個 session 不該收到提示詞。
        卡住不送等於整條流程癱瘓（實測就是這樣卡了兩小時）。
        """
        self.make_work()
        self.coordinator.tick()
        self._requeue_outbox()
        sent = []
        real_send = platform.send_to_session
        real_active = platform.active_sessions

        def spy(root, row, text, **kwargs):
            sent.append(text)
            return {"state": "queued", "channel": "test"}

        def no_server(*args, **kwargs):
            raise platform.PresenceUnknown("Could not reach server")

        platform.send_to_session = spy
        platform.active_sessions = no_server
        try:
            self.coordinator.tick()
        finally:
            platform.send_to_session = real_send
            platform.active_sessions = real_active
        self.assertTrue(sent, "presence 查不到就不送 → 流程會癱瘓")

    def test_busy_session_is_still_deferred(self):
        """忙碌才暫緩——不打擾正在工作的 session。"""
        self.make_work()
        self.coordinator.tick()
        self._requeue_outbox()
        sent = []
        real_send = platform.send_to_session
        real_active = platform.active_sessions

        def spy(root, row, text, **kwargs):
            sent.append(text)
            return {"state": "queued", "channel": "test"}

        platform.send_to_session = spy
        platform.active_sessions = lambda *a, **k: ["ses_thinker"]   # 真的在忙的那個
        try:
            self.coordinator.tick()
        finally:
            platform.send_to_session = real_send
            platform.active_sessions = real_active
        self.assertEqual(sent, [], "忙碌的 session 不該被打擾")

    def test_failed_row_is_retried_with_backoff_then_capped(self):
        """確定失敗 → 指數退避重試；到上限就停，標待人處理。"""
        self.make_work()
        self.coordinator.tick()
        self._requeue_outbox()
        real_send = platform.send_to_session
        real_presence = platform.presence_detail
        calls = []

        def boom(root, row, text, **kwargs):
            calls.append(1)
            raise platform.DispatchError("測試：送不出去", "failed")

        platform.send_to_session = boom
        platform.presence_detail = lambda *a, **k: (platform.IDLE, "")
        try:
            for _ in range(8):
                with store.tx(self.conn):
                    self.conn.execute("UPDATE outbox SET next_attempt_at = NULL")
                self.coordinator.tick()
        finally:
            platform.send_to_session = real_send
            platform.presence_detail = real_presence
        row = self.conn.execute(
            "SELECT status, attempts FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], engine.OUT_FAILED)
        self.assertLessEqual(row["attempts"], 5)
        self.assertGreaterEqual(len(calls), 1)
        work = self.conn.execute(
            "SELECT awaiting_human, awaiting_reason FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(work["awaiting_human"], 1)
        self.assertEqual(work["awaiting_reason"], "dispatch_failed")


# ---------------------------------------------------------------- N. 回執核對

class TestReceiptVerification(unittest.TestCase):
    """回執核對的**關聯鍵是 task_id**，不是 agent 自報的 message_id。

    agent 收到的是「注入給它的那段提示詞」，**看不到平台給訊息編的 id** →
    它自報的 id 必然是編的。拿它去平台比對只會永遠失敗。
    """

    MESSAGES = [
        {"id": "msg_user_1", "sessionID": "ses_x",
         "info": {"role": "user", "created": 1000},
         "parts": [{"text": "【FBF ENGINE 派工】task_id: T-000001 stage: design"}]},
        {"id": "msg_asst_1", "sessionID": "ses_x",
         "info": {"id": "msg_asst_1", "role": "assistant", "providerID": "anthropic",
                  "modelID": "claude", "finish": "stop", "created": 1100}},
    ]

    def _patch_api(self):
        """平台回應的外殼照**真實契約**給：單一物件端點回 `{"data": {...}}`，
        訊息端點回 `{"data": [...], "cursor": {...}}`。
        """
        original = receipts._api

        def fake(endpoint, path, binary="opencode", **kwargs):
            if "/message" in path:
                return {"data": self.MESSAGES, "cursor": {}}
            return {"data": {"id": "ses_x"}}

        receipts._api = fake
        self.addCleanup(lambda: setattr(receipts, "_api", original))

    def test_receipt_is_anchored_on_task_id(self):
        """agent 自報一個編的 message_id → 仍要能核對成功（用 task_id 對上）。"""
        self._patch_api()
        result = receipts.verify_opencode(
            "", "ses_x", "msg_W001_THA_T1_001", "T-000001",
            dispatched_at=900, submitted_at=1200)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["model_id"], "anthropic/claude")

    def test_no_endpoint_does_not_block_the_query(self):
        """沒有 endpoint 也要去查——CLI 自己找得到本機的 server。"""
        self._patch_api()
        result = receipts.verify_opencode(
            "", "ses_x", "msg_x", "T-000001", dispatched_at=900, submitted_at=1200)
        self.assertTrue(result["verified"], result)

    def test_wrong_task_id_is_not_verified(self):
        """task_id 對不上 → 不能核對通過（關聯鍵失效）。"""
        self._patch_api()
        result = receipts.verify_opencode(
            "", "ses_x", "msg_x", "T-999999", dispatched_at=900, submitted_at=1200)
        self.assertFalse(result["verified"])
        self.assertIn("派工訊息", result.get("reason_detail", ""))


# ---------------------------------------------------------------- N2. 平台格式
class TestOpenCode225Format(unittest.TestCase):
    """**OpenCode 2.2.5 的真實訊息格式**（實測自 Mac 上的 opencode.db）。

    四個關鍵差異，舊版程式全中招：
    1. 訊息是**扁平**的，角色欄位叫 `type`（不是 `role`）；
    2. 使用者文字在 `text`（不是 `parts[]`）；
    3. assistant 的模型在 `model:{id, providerID}`（不是平坦的 `modelID`）；
    4. 端點回應統一包在 `{"data": ...}` 裡，而且訊息端點**只回尾端視窗**，
       要照 `cursor.next` 翻頁才拿得到長 session 的派工訊息。
    """

    def _user(self, task_id="T-000001", created=1795159510000):
        return {"id": "msg_u1", "type": "user",
                "text": "【FBF ENGINE 派工】\ntask_id: %s\nstage: design" % task_id,
                "time": {"created": created}}

    def _assistant(self, task_id="T-000001", mid="msg_a1", created=1795159510050,
                   completed=1795159510300, finish="tool-calls", with_tool=True,
                   output=""):
        content = []
        if with_tool:
            state = {"status": "completed", "input": {
                "code": 'await tools["fbf-engine"].fbflow_report_work('
                        '{ task_id: "%s", work_id: "W-001" });' % task_id}}
            if output:
                state["content"] = [{"type": "text", "text": output}]
            content.append({
                "type": "tool", "id": "call_1", "name": "execute", "state": state})
        info = {"id": mid, "type": "assistant", "agent": "build",
                "model": {"id": "group/never-stuck", "providerID": "magpie",
                          "variant": "default"},
                "content": content, "finish": finish,
                "time": {"created": created, "completed": completed}}
        return info

    def _patch(self, pages):
        """`pages` 是「依序回傳的頁面」；第一頁帶 cursor.next。"""
        original = receipts._api
        state = {"index": 0}

        def fake(endpoint, path, binary="opencode", **kwargs):
            if "/message" not in path:
                return {"data": {"id": "ses_x"}}
            index = min(state["index"], len(pages) - 1)
            state["index"] += 1
            return pages[index]

        receipts._api = fake
        self.addCleanup(lambda: setattr(receipts, "_api", original))

    def test_flat_message_shape_is_understood(self):
        """扁平格式：角色在 `type`、文字在 `text`、模型在 `model:{...}`。"""
        self._patch([{"data": [self._user(), self._assistant()], "cursor": {}}])
        result = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000001",
            dispatched_at=1795159509000, submitted_at=1795159511000)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["model_id"], "magpie/group/never-stuck")
        self.assertEqual(result["context_id"], "msg_a1")
        self.assertEqual(result["detail"]["anchor"], "report_tool")

    def test_each_task_anchors_to_its_own_message(self):
        """兩筆任務 → 各自對到**自己**那則交件訊息（不能都指同一則）。"""
        self._patch([{"data": [
            self._user("T-000001"), self._assistant("T-000001", "msg_a1"),
            self._user("T-000002", created=1795159520000),
            self._assistant("T-000002", "msg_a2", created=1795159520050,
                            completed=1795159520300),
        ], "cursor": {}}])
        first = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000001",
            dispatched_at=1795159509000, submitted_at=1795159511000)
        second = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000002",
            dispatched_at=1795159519000, submitted_at=1795159521000)
        self.assertTrue(first["verified"], first)
        self.assertTrue(second["verified"], second)
        self.assertEqual(first["context_id"], "msg_a1")
        self.assertEqual(second["context_id"], "msg_a2")

    def test_cursor_pagination_follows_next(self):
        """訊息端點只回尾端視窗 → 必須照 cursor.next 翻頁才找得到派工訊息。"""
        self._patch([
            {"data": [self._assistant("T-000001", "msg_a1")], "cursor": {"next": "CURSOR1"}},
            {"data": [self._user("T-000001")], "cursor": {}},
        ])
        result = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000001",
            dispatched_at=1795159509000, submitted_at=1795159511000)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["context_id"], "msg_a1")

    def test_completed_timestamp_alone_is_not_proof_of_completion(self):
        """**有 completed 時間戳不等於完成**——中間步驟訊息也帶它。

        沒有 `finish` 就不能算回執（否則會把「還在跑」當成「已完成」）。
        """
        self._patch([{"data": [
            self._user(),
            self._assistant(finish="", with_tool=True),
        ], "cursor": {}}])
        result = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000001",
            dispatched_at=1795159509000, submitted_at=1795159511000)
        self.assertFalse(result["verified"])
        self.assertIn("完成狀態", result.get("reason_detail", ""))

    def test_receipt_after_submission_is_not_accepted(self):
        """回執遠晚於提交 → 不核對通過（不能拿後面的回執頂替）。

        實測校正：同一次交件的落差不到 1 秒，所以「遠晚於」才拒絕；
        差幾秒是正常的（平台在工具收尾之後才標 `completed`）。
        """
        self._patch([{"data": [
            self._user(),
            self._assistant(completed=1795159512000),
        ], "cursor": {}}])
        result = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000001",
            dispatched_at=1795159509000, submitted_at=1795159511000)
        self.assertTrue(result["verified"], "秒級落差應視為同一次交件")

        late = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000001",
            dispatched_at=1795159509000, submitted_at=1795159511000 - 600000)
        self.assertFalse(late["verified"], "遠晚於提交的回執不能採信")

    def test_failed_attempt_is_skipped_and_the_landed_one_is_used(self):
        """**交件失敗的重試不算回執**——要抓「真的交成」那一則。

        實測（真實資料）：同一工單交三次，前兩次是語法錯誤（工具根本沒跑），
        第三次才真的產生 submission → 回執必須是第三次那一則。
        """
        failed = "SyntaxError: Unterminated string constant."
        landed = ('{"ok": true, "data": {"submission_id": "S-000003", '
                  '"status": "pending"}}')
        self._patch([{"data": [
            self._user("T-000003"),
            self._assistant("T-000003", "msg_bad1", created=1795159530159,
                            completed=1795159530200, output=failed),
            self._assistant("T-000003", "msg_bad2", created=1795159579499,
                            completed=1795159579600, output=failed),
            self._assistant("T-000003", "msg_good", created=1795159586552,
                            completed=1795159586700, output=landed),
        ], "cursor": {}}])
        result = receipts.verify_opencode(
            "", "ses_x", "msg_fake", "T-000003",
            dispatched_at=1795159509000, submitted_at=1795159589000)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["context_id"], "msg_good")

    def test_active_sessions_accepts_record_envelope(self):
        """`/api/session/active` 回的是**以 session id 為鍵的物件**（2.2.5 實測）。"""
        original = platform._api
        platform._api = lambda endpoint, path, binary="opencode", **kwargs: {
            "data": {"ses_x": {"type": "running"}, "ses_y": {"type": "running"}}}
        self.addCleanup(lambda: setattr(platform, "_api", original))
        self.assertEqual(sorted(platform.active_sessions("", "opencode")),
                         ["ses_x", "ses_y"])


# ---------------------------------------------------------------- N3. 派工提示詞＝契約的通道
class TestDispatchCarriesContract(Harness):
    """交件契約必須**讓角色看得到**——現在的通道是**派工提示詞 ＋ fbflow_read**。

    實測事故：派工只寫「solutions、design_note」，agent 就交了兩欄，
    沒有 `handoff`、每案沒有 context 與其餘欄位 → 整批 14 筆被守衛退回，
    工單原地不動、沒有錯誤訊息。

    **取消工單檔案層（D34）**後：SQLite 是唯一資料正本，角色不再讀檔案；
    契約直接寫進派工提示詞，工單現況由 `fbflow_read` 查。
    """

    def _dispatch_text(self, target_id="W-001"):
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = ?"
            " ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()
        return row["payload"] if row else ""

    def test_dispatch_carries_the_design_contract(self):
        self.make_work()
        self.coordinator.tick()
        text = self._dispatch_text()
        self.assertIn("solutions", text)
        # 每案要有哪些欄位（**不再要 subagent_context_id**：SUBAGENT 已取消）
        for field in ("approach", "cost", "change_scope",
                      "effort", "risk", "verifiable_by"):
            self.assertIn(field, text)
        # 至少兩案
        self.assertIn("至少 2 項", text)
        # 交接檔八欄
        self.assertIn("handoff", text)
        for field in workflow.HANDOFF_FIELDS:
            self.assertIn(field, text)

    def test_dispatch_contract_follows_the_state(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        text = self._dispatch_text()
        self.assertIn("selection", text)
        self.assertIn("checks", text)
        self.assertIn("requirement_id", text)
        self.assertIn("handoff", text)

    def test_dispatch_points_at_the_read_tool_and_hides_the_state(self):
        self.make_work()
        self.coordinator.tick()
        text = self._dispatch_text()
        self.assertIn("task_id: T-", text)
        # 工單現況從正本讀（不再指向檔案）
        self.assertIn("fbflow_read(target_kind=work", text)
        self.assertNotIn("工單.md", text)
        self.assertNotIn("ISSUE.md", text)
        # **提示詞不得帶工作狀態**
        self.assertNotIn("stage: ", text)
        self.assertNotIn("解法設計中", text)
        # 但要帶**方法**（pstack 的精神，不是工具名）
        self.assertIn("## 方法", text)

    def test_prompt_contract_matches_the_guard(self):
        """**提示詞說的欄位必須就是守衛檢的欄位**——不然一定漂移。

        規格第 3 節：「新規則只准有一份定義」✓。
        """
        specs = workflow.STAGE_FIELD_SPECS[workflow.DESIGN]
        item_fields = specs["solutions"]["item_fields"]
        for field in workflow.SOLUTION_REQUIRED_FIELDS:
            self.assertIn(field, item_fields)
        # 交接檔欄位與提示詞渲染用的是同一份
        self.assertIs(prompts.HANDOFF_FIELDS, workflow.HANDOFF_FIELDS)


# ---------------------------------------------------------------- N5. 派工提示詞
class TestEveryDispatchCarriesTaskId(Harness):
    """**每一種派工提示詞都必須帶 `task_id`**（規格第 7 節的關聯鍵）。

    少了它，回執核對在平台訊息清單裡**找不到這次的派工訊息**
    → 提交永遠是 `unverified` → 流程卡住，而且沒有任何錯誤訊息。

    工單派工本來就有；**ISSUE 派工與里程碑檢視派工原本漏了** ✓。
    """

    def _outbox_text(self, target_id):
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (target_id,)).fetchone()
        return row["payload"] if row else ""

    def test_work_dispatch_carries_task_id_and_read_hint(self):
        self.make_work()
        self.coordinator.tick()
        text = self._outbox_text("W-001")
        self.assertIn("task_id: T-", text)
        self.assertIn("fbflow_read(target_kind=work", text)

    def test_issue_dispatch_carries_task_id_and_read_hint(self):
        work_id = self.make_work()
        issue_id = self.coordinator.create_issue(
            work_id, "ses_worker", "WORKER", "work_blocked",
            "卡住", "build", "試過三次", "停工", "怎麼辦")
        self.coordinator.tick()
        text = self._outbox_text(issue_id)
        self.assertIn("task_id: T-", text)
        self.assertIn("fbflow_read(target_kind=issue", text)

    def test_issue_prompt_names_the_fields_the_guard_checks(self):
        """ISSUE 派工提示詞必須講出**守衛真的在看的**欄位名。

        原本寫 `answer_or_work`，守衛看的是 `answer`／`linked_work_ids`
        → agent 照提示詞交 → 退件 → 流程卡住。

        spec 第 4(b) 步之後，ISSUE 的流程是
        解法設計（solutions）→ 對策（selection＋countermeasure）→ 施工（outputs＋selfcheck）。
        **每一棒**的 ISSUE 提示詞都要把那一棒的必交欄位講出來。
        """
        work_id = self.make_work()
        issue_id = self.coordinator.create_issue(
            work_id, "ses_worker", "WORKER", "work_blocked",
            "卡住", "build", "試過三次", "停工", "怎麼辦", "能重跑一次就過")
        self.coordinator.tick()
        for state, contract in workflow.ISSUE_STATES.items():
            lines = prompts.contract_lines(workflow.issue_stage_contract(state))
            text = "\n".join(lines)
            for field in contract.get("required", []):
                self.assertIn(field, text,
                              "%s 那一棒的 ISSUE 提示詞沒講到必交欄位 %s" % (state, field))
        # 不該再有舊版的欄位名
        lines = prompts.contract_lines(
            workflow.issue_stage_contract(workflow.ISSUE_DESIGNING))
        self.assertNotIn("answer_or_work", "\n".join(lines))

    def test_milestone_dispatch_carries_task_id_and_read_hint(self):
        ms = self.coordinator.create_milestone(1, "A", "第一段成果")
        work_id = self.make_work(milestone_id=ms)
        self.coordinator.tick()
        for stage, payload, session in (
                (workflow.DESIGN, self.design_payload(), "ses_thinker"),
                (workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner"),
                (workflow.BUILD, self.build_payload(), "ses_worker"),
                (workflow.VERIFY, self.verify_payload(), "ses_thinker"),
                (workflow.ADJUDICATE,
                 {"action": "close", "close_reason": "完成", "handoff": HANDOFF},
                 "ses_planner")):
            self.submit(stage, payload, session)
        self.coordinator.tick()
        text = self._outbox_text(ms)
        self.assertIn("task_id: T-", text)
        self.assertIn("fbflow_read(target_kind=milestone", text)


# ---------------------------------------------------------------- N3b. 退件要講清楚哪一格
class TestRejectionExplainsTheField(Harness):
    """**退件要指名哪個欄位、為什麼、標準是什麼、範例長什麼樣。**

    只丟缺口代碼，session 看不懂就改不對 → 交出同樣的東西再被退
    （live 實測：THINKER 收到重派後回「引擎壞了，我不重交」）。

    **表單以外的欄位是附加項**——引擎不檢查它們，也不會因為它們退件
    （使用者裁決：2026-11-24）。
    """

    def _dispatch_text(self, target_id="W-001"):
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = ?"
            " ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()
        return row["payload"] if row else ""

    def test_rejection_names_the_field_the_reason_and_the_standard(self):
        self.make_work()
        self.coordinator.tick()
        payload = self.design_payload()
        # 第二案少了 cost 與 risk
        payload["solutions"][1].pop("cost")
        payload["solutions"][1].pop("risk")
        payload["design_note"] = ""
        result, report = self.submit(workflow.DESIGN, payload, "ses_thinker")
        self.assertTrue(report["rejected"], report)
        text = self._dispatch_text()
        # 1) 哪個欄位
        self.assertIn("solutions[1].cost", text)
        self.assertIn("solutions[1].risk", text)
        self.assertIn("design_note", text)
        # 2) 為什麼不標準
        self.assertIn("缺漏", text)
        # 3) 標準是什麼
        self.assertIn("標準：", text)
        self.assertIn("成本估算", text)
        # 4) 範例長什麼樣
        self.assertIn("範例：", text)
        # 5) 表單以外的欄位是附加項
        self.assertIn("表單以外的欄位是附加項", text)

    def test_extra_fields_are_accepted_as_extras(self):
        """**表單以外的欄位只是附加項**——不會因為它們被退件。"""
        self.make_work()
        self.coordinator.tick()
        payload = self.design_payload()
        payload["my_own_notes"] = "順手記的，引擎不需要"
        payload["another_extra"] = {"whatever": 1}
        result, report = self.submit(workflow.DESIGN, payload, "ses_thinker")
        self.assertFalse(report["rejected"], report)
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)

    def test_verdict_failure_is_explained_in_field_terms(self):
        """**總判不符也要逐欄位講清楚**——不然角色只看到 `verdict.fail` 會看不懂。"""
        self.make_work()
        self.coordinator.tick()
        # 走完到 adjudicate，但故意交一個會讓總判不通過的組合
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)
        result, report = self.submit(
            workflow.ADJUDICATE,
            {"action": "close", "close_reason": "完成", "handoff": HANDOFF},
            "ses_planner")
        if report["rejected"]:
            text = self._dispatch_text()
            self.assertIn("action", text)
            self.assertIn("總判", text)
            self.assertIn("標準：", text)

    def test_type_mismatch_is_explained(self):
        """格式不對也要講清楚：標準是什麼、你給的是什麼。"""
        self.make_work()
        self.coordinator.tick()
        payload = self.design_payload()
        payload["solutions"] = "我直接寫字串好了"
        result, report = self.submit(workflow.DESIGN, payload, "ses_thinker")
        self.assertTrue(report["rejected"], report)
        text = self._dispatch_text()
        self.assertIn("格式不對", text)
        self.assertIn("標準是清單", text)
        self.assertIn("字串", text)


# ---------------------------------------------------------------- N4. 不啟動 server
class TestNeverStartsOpenCodeServer(unittest.TestCase):
    """**引擎永遠不啟動 OpenCode server。**

    上游 CLI（v2.2.5 `packages/cli/src/services/server-connection.ts`）：
    沒帶 `--server` 時會 `Service.ensure()` → **自動開一個常駐背景 server**
    （連帶它裡面的 MCP）。那是別人的機器，不該由我們偷偷開。
    """

    def setUp(self):
        self.calls = []
        self.fake = type("FakeSubprocess", (), {})()
        self.fake.run = self._run
        self.fake.Popen = self._popen
        self.fake.TimeoutExpired = subprocess.TimeoutExpired
        self.fake.DEVNULL = subprocess.DEVNULL
        self.fake.PIPE = subprocess.PIPE          # receipts._api 用 stderr=PIPE
        self.fake.CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._real_platform_subprocess = platform.subprocess
        self._real_receipts_subprocess = receipts.subprocess
        platform.subprocess = self.fake
        receipts.subprocess = self.fake
        self.addCleanup(self._restore)

    def _restore(self):
        platform.subprocess = self._real_platform_subprocess
        receipts.subprocess = self._real_receipts_subprocess

    def _run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if hasattr(kwargs.get("stdout"), "write"):
            kwargs["stdout"].write(b'{"data": {"ses_x": {"type": "running"}}}')

        class Result:
            returncode = 0
            stdout = '{"data": {"ses_x": {"type": "running"}}}'
            stderr = ""

        return Result()

    def _popen(self, argv, **kwargs):
        self.calls.append(list(argv))

        class Proc:
            pid = 4242

        return Proc()

    def _fake_require(self, url="http://127.0.0.1:49374", password="pw"):
        original = platform.ocserver.require
        receipts.ocserver.require = lambda explicit="": (url, password)
        platform.ocserver.require = lambda explicit="": (url, password)
        self.addCleanup(lambda: setattr(receipts.ocserver, "require", original))
        self.addCleanup(lambda: setattr(platform.ocserver, "require", original))

    def test_presence_api_always_passes_server_flag(self):
        """presence 查詢一定要帶 `--server`（帶了才不會啟動 server）。"""
        self._fake_require()
        platform.active_sessions("", "opencode")
        self.assertTrue(self.calls)
        for argv in self.calls:
            self.assertIn("--server", argv, argv)
            self.assertIn("http://127.0.0.1:49374", argv)

    def test_receipt_api_always_passes_server_flag(self):
        """回執查詢一樣要帶 `--server`。"""
        self._fake_require()
        try:
            receipts._api("", "/api/session/active", "opencode")
        except Exception:
            pass
        self.assertTrue(self.calls)
        for argv in self.calls:
            self.assertIn("--server", argv, argv)

    def test_dispatch_always_passes_server_flag(self):
        """派工也一樣要帶 `--server`。"""
        self._fake_require()
        import tempfile
        tmp = tempfile.mkdtemp(prefix="fbf-send-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        platform.send_opencode(tmp, "ses_x", "提示詞", notif_id="1")
        self.assertTrue(self.calls)
        for argv in self.calls:
            self.assertIn("--server", argv, argv)
            self.assertIn("run", argv)
            self.assertIn("--session", argv)

    def test_no_server_means_no_cli_call_at_all(self):
        """**找不到 server 時，連 CLI 都不呼叫**——不讓它去開一個。"""
        def boom(explicit=""):
            raise platform.ocserver.ServerMissing("沒有 server")

        original = platform.ocserver.require
        receipts.ocserver.require = boom
        platform.ocserver.require = boom
        self.addCleanup(lambda: setattr(receipts.ocserver, "require", original))
        self.addCleanup(lambda: setattr(platform.ocserver, "require", original))

        with self.assertRaises(platform.PresenceUnknown):
            platform.active_sessions("", "opencode")
        with self.assertRaises(receipts.ReceiptUnavailable):
            receipts._api("", "/api/session/active", "opencode")
        import tempfile
        tmp = tempfile.mkdtemp(prefix="fbf-send2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with self.assertRaises(platform.DispatchError):
            platform.send_opencode(tmp, "ses_x", "提示詞", notif_id="1")
        self.assertEqual(self.calls, [], "找不到 server 時還去呼叫 CLI")

    def test_discovery_reads_the_registration_file(self):
        """端點由 OpenCode 自己的註冊檔取得（**唯讀**）。"""
        tmp = tempfile.mkdtemp(prefix="fbf-ocstate-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        folder = os.path.join(tmp, "opencode")
        os.makedirs(folder)
        with open(os.path.join(folder, "service.json"), "w", encoding="utf-8") as fh:
            json.dump({"url": "http://127.0.0.1:55555", "password": "secret",
                       "pid": 1, "version": "2.2.5"}, fh)
        self._only_paths(os.path.join(folder, "service.json"))
        url, password = platform.ocserver.discover("")
        self.assertEqual(url, "http://127.0.0.1:55555")
        self.assertEqual(password, "secret")

    def test_discovery_finds_nothing_without_a_registration_file(self):
        """沒有註冊檔 → 回空 → 呼叫端明確報錯（不猜、不開）。"""
        tmp = tempfile.mkdtemp(prefix="fbf-ocstate2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self._only_paths(os.path.join(tmp, "service.json"))
        url, _ = platform.ocserver.discover("")
        self.assertEqual(url, "")
        with self.assertRaises(platform.ocserver.ServerMissing):
            platform.ocserver.require("")

    def _only_paths(self, *paths):
        """把探索範圍限縮到指定的檔案（不受這台機器上真的 OpenCode 影響）。"""
        original = platform.ocserver.registration_paths
        platform.ocserver.registration_paths = lambda: list(paths)
        self.addCleanup(lambda: setattr(platform.ocserver, "registration_paths", original))


# ---------------------------------------------------------------- M. 租約接手
class TestLeaseTakeover(unittest.TestCase):
    """多個 MCP 行程（每個 session 一個）時，**持有者掛掉要有人接手**。

    以前 Resident 只在啟動時搶一次租約：搶不到就永遠不再試 →
    持有者一死，整個專案沒有人推進（工單靜靜卡住、沒有任何錯誤）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-lease-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.first = engine.Coordinator(self.tmp)
        self.second = engine.Coordinator(self.tmp)
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)

    def test_resident_retries_and_takes_over_the_lease(self):
        self.assertTrue(self.first.acquire(), "第一個應該拿得到租約")
        # 第二個現在拿不到 → 但它**還是會啟動**，而且每輪都再試
        self.assertFalse(self.second.acquire(), "第二個此時不該搶到")
        resident = coordinator_module.Resident(self.second, interval_seconds=1)
        self.assertTrue(resident.start(), "拿不到租約也要啟動（才能日後接手）")
        self.addCleanup(resident.stop)
        # 持有者死掉（行程被關）→ 租約被放掉 → 第二個接手
        self.first.close()
        deadline = time.time() + 15
        while time.time() < deadline and not self.second.generation:
            time.sleep(0.2)
        self.assertTrue(self.second.generation,
                        "持有者死掉後沒有人接手租約 → 整個專案沒有人推進")

    def test_lease_is_not_stolen_while_alive(self):
        """持有者還活著 → 別人不搶（單一寫入者）。"""
        self.assertTrue(self.first.acquire())
        self.assertFalse(self.second.acquire())
        self.assertTrue(self.first.renew())

    def test_newer_engine_takes_over_from_an_older_holder(self):
        """持有者跑舊版 → 新版強制接手（否則修正永遠載入不了）。

        實測踩到的：改了程式碼，但舊行程一直霸著租約 → 整包卡住。
        """
        stale = "fbf-999-abcdef01-20200101"          # 舊版持有者
        with store.tx(self.first.conn):
            self.first.conn.execute(
                "INSERT INTO leases(name, generation, owner, expires_at)"
                " VALUES (?,?,?,?)",
                (store.LEASE_COORDINATOR, 1, stale,
                 time.time() + store.LEASE_TTL_SECONDS))
        self.assertTrue(self.second.acquire(), "新版應該能從舊版手上接手")
        lease = store.read_lease(self.second.conn)
        self.assertEqual(lease["owner"], self.second.owner)
        self.assertEqual(lease["generation"], 2, "接手要換世代 → 舊行程的續約會失敗")

    def test_newer_engine_takes_over_from_a_versionless_holder(self):
        """實戰情況：舊行程的 owner **根本沒有版本字樣** → 一樣要能接手。"""
        stale = "fbf-78011-1e1232f0"                  # 沒有版本字樣
        with store.tx(self.first.conn):
            self.first.conn.execute(
                "INSERT INTO leases(name, generation, owner, expires_at)"
                " VALUES (?,?,?,?)",
                (store.LEASE_COORDINATOR, 1, stale,
                 time.time() + store.LEASE_TTL_SECONDS))
        self.assertTrue(self.second.acquire(), "沒有版本字樣的舊持有者要被接手")

    def test_same_version_does_not_steal_a_live_lease(self):
        """同版本不搶活著的租約（兩個新版行程只能有一個寫入者）。"""
        with store.tx(self.first.conn):
            self.first.conn.execute(
                "INSERT INTO leases(name, generation, owner, expires_at)"
                " VALUES (?,?,?,?)",
                (store.LEASE_COORDINATOR, 1,
                 "fbf-999-abcdef01-%s" % engine.ENGINE_VERSION,
                 time.time() + store.LEASE_TTL_SECONDS))
        self.assertFalse(self.second.acquire(), "同版本不該搶別人的租約")


# ---------------------------------------------------------------- L. 席位生命週期

class TestSeatLifecycle(Harness):
    """席位必須有出口——session 意外中斷時要能釋放、讓新的 session 接管。

    以前席位的綁定**沒有生命週期**：session 一旦占走就永遠不放，
    `session revoke` 也只改 `status`、留著 `session_id` → 沒有任何出口，
    流程靜靜卡住。
    """

    def _seat(self, role="PLANNER", short="P9"):
        return self.conn.execute(
            "SELECT * FROM roles WHERE role = ? AND short_name = ?",
            (role, short)).fetchone()

    def _make_seat(self, role="PLANNER", short="P9"):
        """人先建一個**空的**席位（正常流程：`fbf.py role add`）。"""
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO roles(role, short_name, session_id, agent, status,"
                " project_code, workdir, created_at)"
                " VALUES (?,?,NULL,'opencode','active','','',?)",
                (role, short, store.now()))

    def test_release_frees_the_seat_for_a_new_session(self):
        self._make_seat()
        self.coordinator.bind_role("PLANNER", "P9", "ses_dead")
        result = self.coordinator.release_role("PLANNER", "P9", reason="意外中斷")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["released_session"], "ses_dead")
        again = self.coordinator.bind_role("PLANNER", "P9", "ses_new")
        self.assertTrue(again["ok"], again)
        self.assertEqual(self._seat()["session_id"], "ses_new")

    def test_release_of_an_empty_seat_says_nothing_was_released(self):
        """席位本來就空著 → 回報要說清楚，不能看起來像「釋放了什麼」。

        實測踩坑：指令讀寫到**錯的正本**時，席位列存在但沒綁 session，
        回報「ok」＋`released_session: ""` → 人以為釋放了，其實沒動到。
        """
        self._make_seat()
        result = self.coordinator.release_role("PLANNER", "P9")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["released_session"], "")
        self.assertFalse(result["was_occupied"])
        self.assertIn("沒有東西可釋放", result["note"])

    def test_release_clears_the_uuid_hash(self):
        self._make_seat()
        self.coordinator.bind_role("PLANNER", "P9", "ses_a")
        prompts.issue_uuid(self.conn, "ses_a")
        self.assertTrue(self._seat()["uuid_sha256"])
        self.coordinator.release_role("PLANNER", "P9")
        self.assertFalse(self._seat()["uuid_sha256"], "釋放後不該留著舊的 UUID")

    def test_release_is_journalled(self):
        self._make_seat()
        self.coordinator.bind_role("PLANNER", "P9", "ses_a")
        self.coordinator.release_role("PLANNER", "P9", reason="意外中斷")
        row = self.conn.execute(
            "SELECT detail FROM journal WHERE op = 'role.release' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row, "釋放要留下紀錄")
        detail = json.loads(row["detail"])
        self.assertEqual(detail.get("previous_session"), "ses_a")
        self.assertEqual(detail.get("reason"), "意外中斷")

    def test_slot_taken_message_names_the_escape_hatch(self):
        """被擋的 session 要知道出口在哪——否則它只會一直重試。"""
        self._make_seat()
        self.coordinator.bind_role("PLANNER", "P9", "ses_a")
        result = self.coordinator.bind_role("PLANNER", "P9", "ses_b")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ROLE-SLOT-TAKEN")
        self.assertIn("role release", result["message"])

    def test_seat_status_shows_holder_and_idle_time(self):
        self._make_seat()
        self.coordinator.bind_role("PLANNER", "P9", "ses_a")
        seats = self.coordinator.seat_status()
        seat = [s for s in seats if s["short_name"] == "P9"][0]
        self.assertTrue(seat["claimed"])
        self.assertEqual(seat["session_id"], "ses_a")
        self.assertGreaterEqual(seat["idle_hours"], 0)
        self.assertFalse(seat["stale"])

    def test_stale_seat_is_flagged(self):
        """被占住卻很久沒活動 → 標成 stale，讓人看得到要釋放它。"""
        self._make_seat()
        self.coordinator.bind_role("PLANNER", "P9", "ses_a")
        # 把這個 session 的所有活動倒回去（模擬「很久以前綁定，之後就沒動了」）
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE journal SET ts = ? WHERE session_id = ?",
                ("2020-01-01T00:00:00", "ses_a"))
        seats = self.coordinator.seat_status()
        seat = [s for s in seats if s["short_name"] == "P9"][0]
        self.assertTrue(seat["stale"], "久無活動的席位應該被標成 stale")
        self.assertGreater(seat["idle_hours"], 1000)

    def test_endpoint_is_reread_from_settings(self):
        """改了設定要立刻生效——**不必重啟正在跑的 MCP 伺服器**。

        以前 endpoint 是啟動時讀一次：人在專案裡補上設定，
        正在跑的伺服器還是空的那份 → 派工繼續卡住，還得重啟整個 OpenCode。
        """
        self.assertEqual(self.coordinator.endpoint, "")
        with store.tx(self.conn):
            store.set_setting(self.conn, "opencode_endpoint", "http://127.0.0.1:4096")
        self.assertEqual(self.coordinator.endpoint, "http://127.0.0.1:4096")

    def test_deferral_reason_carries_the_platform_message(self):
        """查不到時，**平台回的那句話要帶回來**。

        「查不到」跟「平台在忙」外觀一樣；沒有原因就查不下去。
        """
        reason = self.coordinator._dispatch_deferral_reason(
            "unknown", "Could not reach server")
        self.assertIn("Could not reach server", reason)
        self.assertIn("presence:unknown", reason)

    def test_api_without_endpoint_uses_the_discovered_server(self):
        """沒有明確 endpoint 時，**仍然要帶 `--server`**——帶的是探索到的既有 server。

        舊的行為（不帶 `--server`，讓 CLI 自己去開一個）會在別人的機器上
        留下一個常駐背景 server → **那不是這個套件該做的事**。
        """
        calls = []

        class FakeResult:
            returncode = 0
            stdout = "[]"
            stderr = ""

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return FakeResult()

        original_run = platform.subprocess.run
        original_require = platform.ocserver.require
        platform.subprocess.run = fake_run
        platform.ocserver.require = lambda explicit="": ("http://127.0.0.1:28894", "pw")
        try:
            platform._api("", "/api/session/active")
        finally:
            platform.subprocess.run = original_run
            platform.ocserver.require = original_require
        self.assertTrue(calls, "沒有真的呼叫 opencode")
        self.assertIn("--server", calls[0],
                      "一定要帶 --server（不然 CLI 會自己開一個 server）：%s" % calls[0])
        self.assertEqual(calls[0][:4],
                         ["opencode", "--server", "http://127.0.0.1:28894", "api"])

    def test_lease_owner_carries_the_engine_version(self):
        """租約的 owner 要帶版本——**跑的是不是新版，直接從正本看得出來**。"""
        self.assertIn(engine.ENGINE_VERSION, self.coordinator.owner)
        row = self.conn.execute("SELECT owner FROM leases WHERE name = ?",
                                (store.LEASE_COORDINATOR,)).fetchone()
        if row is not None:
            self.assertIn(engine.ENGINE_VERSION, row["owner"])

    def test_release_of_unknown_seat_is_refused(self):
        result = self.coordinator.release_role("PLANNER", "NOPE")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ROLE-SLOT-NOT-FOUND")


# ---------------------------------------------------------------- K. 執行緒安全

class TestThreadSafety(Harness):
    """背景 tick 執行緒與 MCP 請求執行緒**不能共用同一條 sqlite3 連線**。

    CPython 用 `SQLITE_OPEN_NOMUTEX` 開連線——底層不再替這條連線序列化，
    兩個執行緒同時用它是未定義行為：實測在 macOS 上是 SIGSEGV（沒有 traceback），
    輕一點的變形是「file is not a database」（正本被寫壞）。
    """

    def test_each_thread_gets_its_own_connection(self):
        main_conn = self.conn
        other = {}

        def worker():
            other["conn"] = self.coordinator.conn

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertIsNot(other["conn"], main_conn, "兩個執行緒共用了同一條連線")
        # 但必須指向**同一個正本檔案**
        def db_file(conn):
            return conn.execute("PRAGMA database_list").fetchone()[2]

        self.assertEqual(db_file(other["conn"]), db_file(main_conn))

    def test_connections_are_closed_when_coordinator_closes(self):
        coordinator = engine.Coordinator(self.tmp)
        other = {}

        def worker():
            other["conn"] = coordinator.conn

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        coordinator.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            other["conn"].execute("SELECT 1")


# ---------------------------------------------------------------- J. 審查回歸

class TestAuditFindings(Harness):
    """2026-11-19 審查的四項缺陷，各留一條回歸測試。"""

    # -- 里程碑不得被錯誤結案 -------------------------------------

    def _review_task(self):
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE stage = 'milestone_review' AND status = 'open'"
        ).fetchone()
        self.assertIsNotNone(task, "找不到里程碑檢視單")
        return task

    def _closed_work(self, milestone_id=""):
        work_id = self.make_work(milestone_id=milestone_id)
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.submit(workflow.ADJUDICATE,
                    {"action": "close", "close_reason": "完成", "handoff": HANDOFF},
                    "ses_planner")
        return work_id

    def test_milestone_review_cannot_use_another_milestones_task(self):
        """用 A 的檢視任務回報 B → 拒絕，B 不得被結案。"""
        ms_a = self.coordinator.create_milestone(1, "A", "第一段成果")
        ms_b = self.coordinator.create_milestone(2, "B", "第二段成果")
        self._closed_work(ms_a)
        self.coordinator.tick()
        result = self.coordinator.apply_milestone_review(
            self._review_task()["id"], ms_b, 1, "ses_planner",
            workflow.MS_CONTINUE, "達成")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "task.target_mismatch")
        row = self.conn.execute("SELECT state FROM milestones WHERE id = ?",
                                (ms_b,)).fetchone()
        self.assertNotEqual(row["state"], workflow.MS_DONE)

    def test_milestone_review_rejects_stale_version(self):
        """帶過期版本 → 拒絕，里程碑不動。"""
        ms = self.coordinator.create_milestone(1, "A", "第一段成果")
        self._closed_work(ms)
        self.coordinator.tick()
        result = self.coordinator.apply_milestone_review(
            self._review_task()["id"], ms, 999, "ses_planner",
            workflow.MS_CONTINUE, "達成")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TASK-STALE")
        row = self.conn.execute("SELECT state FROM milestones WHERE id = ?", (ms,)).fetchone()
        self.assertEqual(row["state"], workflow.MS_REVIEWING)

    def test_milestone_review_cannot_be_resubmitted(self):
        """已完成的檢視單不得重送（同一張單只准結案一次）。"""
        ms = self.coordinator.create_milestone(1, "A", "第一段成果")
        self._closed_work(ms)
        self.coordinator.tick()
        task = self._review_task()
        first = self.coordinator.apply_milestone_review(
            task["id"], ms, 1, "ses_planner", workflow.MS_CONTINUE, "達成")
        self.assertTrue(first["ok"], first)
        again = self.coordinator.apply_milestone_review(
            task["id"], ms, 1, "ses_planner", workflow.MS_CONTINUE, "達成")
        self.assertFalse(again["ok"])
        self.assertEqual(again["code"], "TASK-STALE")
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE action = 'milestone.continue'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_milestone_review_rejects_other_session(self):
        """不是受派者 → 拒絕。"""
        ms = self.coordinator.create_milestone(1, "A", "第一段成果")
        self._closed_work(ms)
        self.coordinator.tick()
        result = self.coordinator.apply_milestone_review(
            self._review_task()["id"], ms, 1, "ses_thinker",
            workflow.MS_CONTINUE, "達成")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ROLE-DENIED")

    # -- ISSUE 退回不是結案 ----------------------------------------

    def _make_issue(self):
        work_id = self.make_work()
        issue_id = self.coordinator.create_issue(
            work_id, "ses_worker", "WORKER", "work_blocked",
            "卡住", "build", "試過三次", "停工", "怎麼辦", "能重跑一次就過")
        self.coordinator.tick()          # 發起中 → 解法設計中，派給 THINKER
        return issue_id

    def _issue_row(self, issue_id):
        return self.conn.execute("SELECT * FROM issues WHERE id = ?",
                                  (issue_id,)).fetchone()

    def _issue_design_payload(self, contexts=("ctx-i1", "ctx-i2")):
        return {
            "solutions": [
                {"subagent_context_id": contexts[0], "approach": "做法甲",
                 "cost": "低", "change_scope": "小", "effort": "少",
                 "risk": "低", "verifiable_by": "跑測試", "reuses": "無"},
                {"subagent_context_id": contexts[1], "approach": "做法乙",
                 "cost": "高", "change_scope": "大", "effort": "多",
                 "risk": "中", "verifiable_by": "跑測試", "reuses": "無"},
            ],
        }

    def _issue_countermeasure_payload(self, solution_id="ctx-i1"):
        return {"selection": {"solution_id": solution_id, "reason": "成本最小"},
                "countermeasure": "照做法甲改，改完重跑驗收"}

    def _issue_build_payload(self):
        path = os.path.join(self.tmp, "issue-out.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("fixed")
        size, digest = store.file_fingerprint(path)
        return {"outputs": [{"path": "issue-out.txt", "size": size, "sha256": digest}],
                "selfcheck": "對策生效了，重跑一次就過"}

    def _run_issue_to_adjudicating(self, issue_id, contexts=("ctx-i1", "ctx-i2")):
        """走完 ISSUE 的前三棒：解法設計 → 對策 → 依對策施工。"""
        self._submit_issue(issue_id, self._issue_design_payload(contexts), "ses_thinker")
        self._submit_issue(issue_id, self._issue_countermeasure_payload(contexts[0]),
                           "ses_planner")
        self._submit_issue(issue_id, self._issue_build_payload(), "ses_worker")
        self.assertEqual(self._issue_row(issue_id)["state"], workflow.ISSUE_ADJUDICATING)

    def _submit_issue(self, issue_id, payload, session_id):
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE issue_id = ? AND status = 'open'"
            " ORDER BY created_at DESC LIMIT 1", (issue_id,)).fetchone()
        self.assertIsNotNone(task, "找不到 ISSUE 的待辦")
        self._message_counter += 1
        result = self.coordinator.submit_issue(
            task["id"], issue_id, int(self._issue_row(issue_id)["version"]),
            session_id, task["stage"], payload,
            "msg-issue-%04d" % self._message_counter)
        self.coordinator.tick()
        return result

    def test_issue_return_reason_does_not_close(self):
        """驗證者附原因退回 → 退回**解法設計中**（重複整個流程），不是結案。"""
        issue_id = self._make_issue()
        self._run_issue_to_adjudicating(issue_id)
        self._submit_issue(issue_id, {"return_reason": "還沒解決"}, "ses_worker")
        row = self._issue_row(issue_id)
        self.assertEqual(row["state"], workflow.ISSUE_DESIGNING)
        self.assertNotEqual(row["state"], workflow.ISSUE_CLOSED)
        self.assertEqual(row["return_count"], 1)
        self.assertEqual(row["return_reason"], "還沒解決")

    def test_issue_return_count_stops_at_limit(self):
        """退回最多 **2** 次（spec：連續兩次未結案就停等人類），之後標待人處理。"""
        issue_id = self._make_issue()
        for round_no in (1, 2):
            self._run_issue_to_adjudicating(
                issue_id, contexts=("ctx-i%d" % round_no, "ctx-j%d" % round_no))
            self._submit_issue(issue_id, {"return_reason": "還是不行"}, "ses_worker")
        row = self._issue_row(issue_id)
        self.assertEqual(row["return_count"], 2)
        self.assertEqual(row["awaiting_human"], 1)
        self.assertEqual(row["awaiting_reason"], "issue_return_limit")
        self.assertIn(issue_id, [i["id"] for i in self.coordinator.awaiting_list()])
        # 標了待人處理就不再派工（等 humans 裁決）
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM tasks WHERE issue_id = ? AND status = 'open'",
            (issue_id,)).fetchone())

    def test_issue_resolution_closes(self):
        """附結論 → 結案（退回與結案是兩條路）。"""
        issue_id = self._make_issue()
        self._run_issue_to_adjudicating(issue_id)
        self._submit_issue(issue_id, {"resolution": "結果可接受"}, "ses_worker")
        self.assertEqual(self._issue_row(issue_id)["state"], workflow.ISSUE_CLOSED)

    # -- 文字門檻不得放寬 ------------------------------------------

    def _revision_path_with_threshold(self, threshold):
        """走到「已經有驗收結果、而且回到驗收方式設計中」，門檻是文字門檻。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        payload = self.acceptance_payload()
        payload["checks"][0]["threshold"] = threshold
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        rework = {"action": "rework", "rework_reason": "驗收方式訂錯了",
                  "rework_type": workflow.REWORK_TYPE_ACCEPTANCE, "handoff": HANDOFF}
        for round_no in (1, 2):
            self.submit(workflow.ADJUDICATE, rework, "ses_planner")
            self._submit_rework_design("改驗收方式",
                                       contexts=("ctx-c%d" % round_no, "ctx-d%d" % round_no),
                                       threshold=threshold)
            self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
            self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        review = {"action": "review", "diagnosis": "acceptance", "remedy": "revise",
                  "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, review, "ses_planner")
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)

    def test_revision_cannot_loosen_text_threshold(self):
        """『at least 95% or manual override』不得通過——含舊字串不等於沒放寬。"""
        self._revision_path_with_threshold("at least 95%")
        payload = self.acceptance_payload()
        payload["checks"][0]["threshold"] = "at least 95% or manual override"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(workflow.GAP_CHECK_THRESHOLD_LOWERED, row["gaps"])
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)

    def test_revision_can_tighten_text_threshold(self):
        """收緊門檻要能通過——不能把所有修訂都擋掉。"""
        self._revision_path_with_threshold("at least 95%")
        payload = self.acceptance_payload()
        payload["checks"][0]["threshold"] = "at least 95% and every critical path verified"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        self.assertEqual(self.state(), workflow.BUILD)

    def test_revision_cannot_make_threshold_optional(self):
        """把必須達到的門檻變成選填 → 擋（各種寫法都要擋）。"""
        self._revision_path_with_threshold("at least 95%")
        for loosener in ("at least 95% or manual override",
                         "at least 95% is optional",
                         "at least 95% optional",
                         "at least 95% best effort",
                         "at least 95% (except flaky tests)",
                         "at least 95% 可選",
                         "at least 95% 除非例外"):
            payload = self.acceptance_payload()
            payload["checks"][0]["threshold"] = loosener
            self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
            row = self.conn.execute(
                "SELECT gaps FROM submissions WHERE status = 'rejected'"
                " ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIsNotNone(row, "門檻被放寬卻通過了：%s" % loosener)
            self.assertIn(workflow.GAP_CHECK_THRESHOLD_LOWERED, row["gaps"],
                          "門檻被放寬卻通過：%s" % loosener)
            self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)

    def test_revision_of_unparsable_threshold_is_blocked(self):
        """拆不出條款的門檻（自然語言）→ 無法判定 → 擋，交由人裁決。"""
        self._revision_path_with_threshold("產物存在且雜湊相符")
        payload = self.acceptance_payload()
        payload["checks"][0]["threshold"] = "產物存在就好"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(workflow.GAP_CHECK_THRESHOLD_LOWERED, row["gaps"])
        self.assertEqual(self.state(), workflow.ACCEPTANCE_DESIGN)

    def test_revision_can_raise_numeric_threshold(self):
        """數字門檻可以調高——門檻判定不是「一律擋」。"""
        self._revision_path_with_threshold("90%")
        payload = self.acceptance_payload()
        payload["checks"][0]["threshold"] = "95%"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")
        self.assertEqual(self.state(), workflow.BUILD)


# ---------------------------------------------------------------- O. 2026-11-20 修正的守門測試

class TestTransientDbErrorDoesNotKillResident(unittest.TestCase):
    """**暫時性 DB 錯誤不得讓常駐協調器熄火**（DEC-003 的本意）。

    原本 `except sqlite3.DatabaseError` 把**所有** DatabaseError 當正本損毀
    並 `break` → 一次 `database is locked` 就讓 tick 執行緒永久死亡
    → 整個專案沒有人推進、沒有任何錯誤訊息。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-resident-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _resident_with_tick(self, tick_fn):
        coordinator = engine.Coordinator(self.tmp)
        self.addCleanup(coordinator.close)
        coordinator.tick = tick_fn
        resident = coordinator_module.Resident(coordinator, interval_seconds=1)
        self.addCleanup(resident.stop)
        resident.start()
        return resident

    def test_locked_db_retries_next_round(self):
        calls = []

        def tick():
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return {"ok": True}

        self._resident_with_tick(tick)
        deadline = time.time() + 10
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.1)
        self.assertGreaterEqual(len(calls), 2,
                                "暫時性 DB 錯誤後協調器沒有再跑下一輪（熄火了）")

    def test_corruption_stops_the_loop(self):
        calls = []

        def tick():
            calls.append(1)
            raise sqlite3.DatabaseError("file is not a database")

        self._resident_with_tick(tick)
        time.sleep(2.5)
        self.assertEqual(len(calls), 1, "正本損毀後還在往壞掉的正本寫")


class TestReleaseCancelsOpenTasks(Harness):
    """**釋放席位要一併作廢該 session 的 open task**。

    session 死掉後它名下的 open task 永遠留著 → `_dispatch_pending`
    看到「已有開啟任務」就跳過那張工單 → **新 session 認領了席位也接不到活**
    （README FAQ 承諾「釋放後新 session 就能接管」，原本是假的）。
    """

    def test_release_then_rebind_gets_a_new_dispatch(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task()
        self.assertIsNotNone(task)
        # session 死了 → 人釋放 THINKER/T1 的席位
        result = self.coordinator.release_role("THINKER", "T1", reason="session 中斷")
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result.get("cancelled_tasks", 0), 1)
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled", "open task 沒被作廢")

        # 新 session 認領席位 → 下一趟 tick 必須**重派**同一張單
        self.assertTrue(self.coordinator.bind_role("THINKER", "T1", "ses_new")["ok"])
        self.coordinator.tick()
        fresh = self.open_task()
        self.assertIsNotNone(fresh, "釋放後工單沒有新 task → 流程接不回去")
        self.assertEqual(fresh["assigned_session"], "ses_new")
        self.assertNotEqual(fresh["id"], task["id"])

    def test_release_also_drops_the_undelivered_dispatch(self):
        """**沒送出去的派工也要清**——收件人已死，留著只會一直重查。

        實測：喚醒類的 outbox **查不到對應 task** → 不會被 `_flush_outbox`
        的「task 已關閉」孤兒檢查撿到 → 每 30 秒重查一次、一直累積
        `unknown`（實測累積 27 次），還可能誤把工單標成待人處理。
        """
        self.make_work()
        self.coordinator.tick()
        task = self.open_task()
        self.assertIsNotNone(task)
        # 模擬實測的卡住狀態：派工送達狀態不明（`unknown`）→ 每 30 秒重查一次。
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE outbox SET status = 'unknown', deferrals = 27 WHERE id = 1")
        rows = self.conn.execute(
            "SELECT id, status FROM outbox WHERE recipient_session = ?",
            ("ses_thinker",)).fetchall()
        self.assertTrue(rows, "測試前提失誤：沒有派給死 session 的 outbox")
        result = self.coordinator.release_role("THINKER", "T1", reason="session 中斷")
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result.get("dropped_outbox", 0), 1)
        after = self.conn.execute(
            "SELECT status FROM outbox WHERE id = ?", (rows[0]["id"],)).fetchone()
        self.assertEqual(after["status"], "failed",
                         "釋放後還沒送出的派工沒被清掉 → 會一直重查")


class TestAdjudicateContractMatchesGuard(Harness):
    """adjudicate 的**提示詞契約必須涵蓋守衛真的看的欄位**。

    原本 `required=["action"]`，守衛卻要 close_reason／rework_reason／
    rework_type／diagnosis／remedy → agent 照提示詞只交 action →
    退件三次 → 標待人處理（與 ISSUE `answer_or_work` 同型的漂移）。
    """

    def test_contract_lists_every_field_the_guard_checks(self):
        contract = workflow.stage_contract(workflow.ADJUDICATE)
        for field in ("action", "close_reason", "rework_reason", "rework_type",
                      "diagnosis", "remedy"):
            self.assertIn(field, contract["required"],
                          "守衛看的 %s 不在契約的 required 裡" % field)
        self.assertTrue(contract["required_note"], "契約沒說依 action 怎麼選填")

    def test_adjudicate_dispatch_names_the_guard_fields(self):
        """契約的通道是派工提示詞——adjudicate 那一棒也要講齊守衛看的欄位。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = 'W-001'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        text = row["payload"]
        for field in ("close_reason", "rework_reason", "rework_type",
                      "diagnosis", "remedy"):
            self.assertIn(field, text, "派工提示詞沒講 %s" % field)
        self.assertIn("依 action", text)


class TestReworkReasonIsPersisted(Harness):
    """**退回原因必須真的落庫**（規格第 5 節：退回要寫原因與類型）。

    原本 `apply_stage` 寫回的是 `work` 裡的舊值（永遠是 None）→
    PLANNER 填的原因沒進正本、`rework_design` 的 THINKER 收不到
    「為什麼被退回」→ 只能猜。
    """

    def _to_adjudicate(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)

    def test_rework_reason_and_type_landed_in_the_db(self):
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "產物雜湊對不上",
                   "rework_type": workflow.REWORK_TYPE_BUILD, "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_DESIGN)
        row = self.conn.execute(
            "SELECT rework_reason, rework_type FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(row["rework_reason"], "產物雜湊對不上",
                         "退回原因沒落庫（下一棒看不到為什麼被退回）")
        self.assertEqual(row["rework_type"], workflow.REWORK_TYPE_BUILD)


class TestOptimisticLockOnApply(Harness):
    """**套用時樂觀鎖沒中 → 標 stale、整個交易回滾**（規格第 4 節）。

    原本工單的主 UPDATE 沒檢查 rowcount → 競爭時仍照樣 mark applied、
    建下一棒、寫回執 → 狀態機被推歪（ISSUE 路徑本來有檢查）。
    """

    def test_version_changed_mid_apply_marks_stale(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        payload = self.design_payload()
        version = self.version()
        result = self.coordinator.submit_work(
            task["id"], "W-001", version, "ses_thinker", workflow.DESIGN,
            payload, "m-stale")

        # 在 apply_stage（同交易內、主 UPDATE 之前）之後偷改版本
        original_apply = workflow.apply_stage
        poisoned = []

        def poison(conn, work, stage, data):
            original_apply(conn, work, stage, data)
            conn.execute("UPDATE works SET version = version + 1 WHERE id = ?",
                         (work["id"],))
            poisoned.append(work["id"])

        workflow.apply_stage = poison
        self.addCleanup(setattr, workflow, "apply_stage", original_apply)
        report = self.coordinator.tick()
        self.assertTrue(poisoned, "測試沒有真的走到套用路徑")
        self.assertIn(result["submission_id"], report["stale"], report)
        row = self.conn.execute(
            "SELECT status FROM submissions WHERE id = ?",
            (result["submission_id"],)).fetchone()
        self.assertEqual(row["status"], engine.SUB_STALE)
        self.assertEqual(self.state(), workflow.DESIGN, "狀態被推歪了")


class TestReceiptTimeoutParksWithoutBurningTheRole(Harness):
    """**回執核對卡住不得讓流程停死，但更不能燒掉角色已經做完的工**。

    舊行為（實測浪費）：到期 → submission 標 `expired` → 作廢 task → 換新 task →
    叫角色「【回執未核對】退回補齊、用新的 message_id 重交」→ 連續三次。
    角色那幾十分鐘的工**全部白花**，而且它做的那件事平台明明有紀錄
    （證據被尾端視窗截掉而已）。

    現在（規格第 11 節：「逾時標 awaiting_human 並保存原 submission，
    不自動宣稱失敗」）：
    - 到期 → 標 `awaiting_human=receipt_timeout`（`awaiting list` 看得到）；
    - **不重派、不叫角色重做**；
    - submission 與 task 都留著 → 證據晚到就**自動套用**，角色不用再交一次。
    """

    verifier = staticmethod(unverified_verifier)

    def _expire_latest_submission(self, age_seconds=601):
        """把最新一筆 submission 的建立時間倒退，讓截止判斷成立。"""
        past = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(time.time() - age_seconds))
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE submissions SET created_at = ? WHERE id = ("
                " SELECT id FROM submissions ORDER BY id DESC LIMIT 1)", (past,))

    def _submit_design(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        self.coordinator.submit_work(task["id"], "W-001", self.version(),
                                     "ses_thinker", workflow.DESIGN,
                                     self.design_payload(), "m1")
        return task

    def test_wakeup_carries_the_contract_and_the_rejection_reason(self):
        """**喚醒要帶「要交什麼」與「上次為什麼被退」**——不然角色會去讀舊檔案。

        live 實測（2026-11-24）：喚醒只寫「照工單檔案的『現在某一棒』交件」，
        而工單檔案層已經取消、那份檔案**不再更新** → 角色照著舊狀態做事，
        交了不該交的東西（或什麼都不交）。
        """
        self.make_work()
        self.coordinator.tick()
        task = self.open_task(stage=workflow.DESIGN)
        # 造一筆被退的提交（讓喚醒帶上退件說明）
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO submissions(id, task_id, work_id, issue_id, expected_version,"
                " session_id, stage, payload, message_id, status, gaps, created_at, updated_at)"
                " VALUES ('S-910001', ?, 'W-001', NULL, 1, ?, 'design', '{}', 'msg-y',"
                " 'rejected', '[\"apply.rejected\"]', ?, ?)",
                (task["id"], task["assigned_session"], store.now(), store.now()))
        # 這一輪 tick：引擎自癒 → 收掉舊 task、改派新 task（T-000002）
        self.coordinator.tick()
        # 把喚醒的時間門檻拉開（task 與角色最近一次交件都算「很久以前」）
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET created_at = '2000-01-01T00:00:00',"
                             " updated_at = '2000-01-01T00:00:00'")
            self.conn.execute("UPDATE submissions SET created_at = '2000-01-01T00:00:00',"
                             " updated_at = '2000-01-01T00:00:00'")
            self.conn.execute("DELETE FROM counters WHERE name LIKE 'wakeup%'")
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'wakeup' ORDER BY id DESC LIMIT 1").fetchone()
        text = row["payload"] if row else ""
        self.assertIn("現在這一棒：要交的東西", text)
        self.assertIn("solutions", text)
        self.assertIn("fbflow_read", text)
        self.assertNotIn("工單檔案", text)
        # **喚醒不得帶 task_id**（會造成假回執）
        self.assertNotIn(task["id"], text)

    def test_timeout_parks_and_never_asks_the_role_to_redo(self):
        task = self._submit_design()
        self._expire_latest_submission()
        report = self.coordinator.tick()
        self.assertTrue(report["expired"], report)
        # 1) 停等人（人看得見）
        row = self.conn.execute(
            "SELECT awaiting_human, awaiting_reason, state FROM works"
            " WHERE id = 'W-001'").fetchone()
        self.assertEqual(int(row["awaiting_human"]), 1)
        self.assertEqual(row["awaiting_reason"], "receipt_timeout")
        self.assertEqual([i["id"] for i in self.coordinator.awaiting_list()], ["W-001"])
        # 2) **沒有重派**（角色不會再收到「退回補齊」）
        dispatched = [d for d in report["dispatched"]
                      if d.get("work") == "W-001" or d.get("note") == "receipt_retry"]
        self.assertEqual(dispatched, [], "又派了一棒 → 角色要重做一次")
        # 3) **舊 task 還開著**（證據晚到可以自動套用）
        old = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(old["status"], "open", "task 被作廢 → 晚到的證據套不上去")
        # 4) 工單狀態沒被推歪
        self.assertEqual(self.state(), workflow.DESIGN)

    def test_evidence_arriving_later_is_applied_without_another_submission(self):
        """**證據晚到 → 自動套用**，角色不用再交一次。"""
        self._submit_design()
        self._expire_latest_submission()
        self.coordinator.tick()
        self.assertEqual(int(self.conn.execute(
            "SELECT awaiting_human FROM works WHERE id = 'W-001'").fetchone()[0]), 1)
        # 平台端後來核得到證據了（換回會驗證的核對器）
        self.coordinator.verifier = fake_verifier
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT awaiting_human, state FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(int(row["awaiting_human"]), 0, "核到了還在等人")
        self.assertEqual(row["state"], workflow.ACCEPTANCE_DESIGN,
                         "證據到了卻沒有往前推")
        applied = self.conn.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE status = 'applied'").fetchone()
        self.assertEqual(int(applied["n"]), 1)

    def test_grant_more_keeps_state_and_revise_forces_a_redo(self):
        """人裁決：`grant_more` 繼續等證據；`revise` 才是真的要重做。"""
        self._submit_design()
        self._expire_latest_submission()
        self.coordinator.tick()
        resolved = self.coordinator.awaiting_resolve("W-001", "grant_more", "再等")
        self.assertTrue(resolved["ok"], resolved)
        row = self.conn.execute(
            "SELECT awaiting_human, awaiting_reason, state FROM works"
            " WHERE id = 'W-001'").fetchone()
        self.assertEqual(int(row["awaiting_human"]), 0)
        self.assertEqual(row["state"], workflow.DESIGN,
                         "回執超時的裁決不該把工單跳到 rework_design")
        counter = self.conn.execute(
            "SELECT value FROM counters WHERE name = 'receipt_expires:W-001'").fetchone()
        self.assertEqual(int(counter["value"]), 0, "裁決後計數沒重置 → 下次直接又停")

        # 再停一次 → 人裁決 revise → 退回重做（**人的決定**）
        self._expire_latest_submission()
        self.coordinator.tick()
        self.coordinator.awaiting_resolve("W-001", "revise", "重做")
        self.assertEqual(self.state(), workflow.REWORK_DESIGN)


if __name__ == "__main__":
    unittest.main()
