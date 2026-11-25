"""FBF ENGINE 的**端到端工作流**：把一張單從頭到尾跑完，抓流程本身的 BUG。

這支測試不是測單一規則，而是**用引擎程式碼把 spec 的流程整條走一遍**：

    工單：待派工 → 解法設計中 → 驗收方式設計中 → 施工中 → 驗收中 → 結案中 → 完工
    重工：結案中(退回) → 重工解法設計中 → 重工驗收方式設計中 → 重工中 → 驗收中 → …
    ISSUE：發起中 → 解法設計中 → 對策中 → 依對策施工中 → 結案中 → 已結案

每一步都檢查**同一件事**：派工提示詞帶的「現在某一棒」跟這張單當下該交的
欄位一致、提示詞**不帶工作狀態**、而且**整條流程不再寫出任何工單檔案**
（SQLite 是唯一資料正本，見 DEVIATIONS D34）。

**這裡不碰真的 session**：角色交件由測試替身送出，證明的是**流程邏輯**，
不是平台回執格式（那是 `test_fbf_engine.py` 與 e2e 實測的範圍）。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import engine, store, workflow  # noqa: E402
from tests.test_fbf_engine import HANDOFF, Harness  # noqa: E402


class TestWorkLifecycleRunsToClosed(Harness):
    """一張工單從待派工走到完工，**每一步的派工提示詞都要跟得上**。"""

    def test_rework_design_needs_two_different_solutions_no_subagent(self):
        """SUBAGENT 取消（2026-11-23）：重工解法**不帶 context id**，但兩案仍不得實質相同。

        live 實測（2026-11-23 22:4x）：THINKER 分兩個 SUBAGENT 跑解法跑了十幾分鐘，
        還得自己編 context id 交回來給引擎核對；核不過就退件 → 單子一直坐著。

        現在：
        1. 一棒直接產兩案、**不帶** `subagent_context_id` → 照樣收；
        2. 兩案一模一樣 → 守衛擋（`solution.too_similar`）；
        3. 選案用**案號**（"1"），不是 context id。
        """
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(contexts=("ctx-a", "ctx-b")),
                    "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.submit(workflow.ADJUDICATE,
                    {"action": "rework", "rework_reason": "還差一項",
                     "rework_type": workflow.REWORK_TYPE_BUILD, "handoff": HANDOFF},
                    "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_DESIGN)

        # 1) 兩案實質相同 → 擋（並自動重派一棒）
        same = self.rework_design_payload(contexts=("", ""))
        same["solutions"][1] = dict(same["solutions"][0])
        _, report = self.submit(workflow.REWORK_DESIGN, same, "ses_thinker")
        self.assertTrue(report["rejected"], report)
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn(workflow.GAP_SOLUTION_TOO_SIMILAR, row["gaps"])

        # 2) **不帶 context id** 的兩案 → 照樣推進
        self.submit(workflow.REWORK_DESIGN,
                    self.rework_design_payload(contexts=("", "")), "ses_thinker")
        self.assertEqual(self.state(), workflow.REWORK_ACCEPTANCE_DESIGN)

        # 3) 選案用**案號**
        self.submit(workflow.REWORK_ACCEPTANCE_DESIGN,
                    self.rework_acceptance_payload(solution_id="1"), "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_BUILD)

    def _dispatch_text(self, target_id="W-001"):
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (target_id,)).fetchone()
        return row["payload"] if row else ""

    def test_engine_heals_a_task_whose_submission_was_rejected(self):
        """live 實測的卡死：提交被退件、**卻沒有重新派工** → task 一直開著。

        引擎要自己收掉它並重新派（不必人工介入）。
        這個狀態在修好之後很難自然產生（退件路徑都會重派），
        所以直接照 live 的樣子造出來。
        """
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(task)
        # 造出「這個 task 的提交被退件、但沒有新 task」的殘局
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO submissions(id, task_id, work_id, issue_id, expected_version,"
                " session_id, stage, payload, message_id, status, gaps, created_at, updated_at)"
                " VALUES ('S-999999', ?, 'W-001', NULL, ?, 'ses_thinker', 'design', '{}',"
                " 'msg-old', 'rejected', '[\"apply.rejected\"]', ?, ?)",
                (task["id"], self.version("W-001"), store.now(), store.now()))
        self.coordinator.tick()
        # 舊 task 收掉 → 這張單重新被派
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled",
                         "退件卻沒重派的 task 沒被收掉 → 這張單會靜靜卡住")
        new_task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(new_task, "沒有重新派工")
        self.assertNotEqual(new_task["id"], task["id"])
        # 重派提示詞要帶「上一次為什麼被退」（角色讀不到上一輪對話）
        text = self._dispatch_text("W-001")
        self.assertIn("apply.rejected", text)

    def test_redispatch_tells_the_role_why_the_last_one_was_rejected(self):
        """重派時，提示詞要帶「上次為什麼被退」。

        live 實測（2026-11-23 22:0x）：退件已經會重派了，但重派的提示詞**沒說原因**——
        角色讀不到上一輪對話，提示詞又不帶狀態（spec 第 70 行），
        於是它以為自己還沒交過，交出同樣的東西再被退。
        THINKER 當時的回覆是「引擎壞了，我不重交」。

        現在：重派提示詞要同時帶缺口代碼＋**引擎實際擋下的原因**。
        """
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(task)
        # 造出 live 的樣子：守衛過了、套用時被擋（apply.rejected）
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO submissions(id, task_id, work_id, issue_id, expected_version,"
                " session_id, stage, payload, message_id, status, gaps, created_at, updated_at)"
                " VALUES ('S-888888', ?, 'W-001', NULL, ?, 'ses_thinker', 'design', '{}',"
                " 'msg-old', 'rejected', '[\"apply.rejected\"]', ?, ?)",
                (task["id"], self.version("W-001"), store.now(), store.now()))
            self.conn.execute(
                "INSERT INTO decisions(scan_id, target_id, from_state, to_state, submission_id,"
                " receipt_id, gaps, action, detail, created_at)"
                " VALUES ('scan-x', 'W-001', 'design', '', 'S-888888', '',"
                " '[\"apply.rejected\"]', 'rejected', ?, ?)",
                (json.dumps({"error": "subagent_context_id abc 這張工單已經用過了"},
                            ensure_ascii=False), store.now()))
        self.coordinator.tick()

        self.assertIsNotNone(self.open_task("W-001", workflow.DESIGN), "沒有重新派工")
        text = self._dispatch_text("W-001")
        self.assertIn("apply.rejected", text)
        self.assertIn("這張工單已經用過了", text,
                      "重派提示詞沒帶『引擎實際擋下的原因』→ 角色會重交同樣的東西")

    def test_selection_rejected_because_given_as_a_string_stays_one_line(self):
        """live 實測（2026-11-23 22:4x）：「沒選它」變成亂碼。

        角色把「為什麼不要其他案」交成**一個字串**，引擎照收 →
        渲染時把字串當 iterable 逐字元印出來：
        「沒選它：S」「沒選它：O」「沒選它：L」……整段選擇變成亂碼。

        現在：**寫入端**就正規化成清單（取消工單檔案層後已無渲染端）。
        """
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(contexts=("ctx-a", "ctx-b")),
                    "ses_thinker")
        payload = self.acceptance_payload()
        payload["selection"]["rejected_because"] = "成本較高而且要改到核心流程"
        self.submit(workflow.ACCEPTANCE_DESIGN, payload, "ses_planner")

        # 正本存成清單
        row = self.conn.execute("SELECT selection FROM works WHERE id = 'W-001'").fetchone()
        stored = json.loads(row["selection"])
        self.assertIsInstance(stored["rejected_because"], list,
                              "字串沒被正規化 → 渲染會逐字元印")
        self.assertEqual(stored["rejected_because"],
                         ["成本較高而且要改到核心流程"])

    def test_full_path_to_closed_writes_no_work_files(self):
        self.make_work()
        self.coordinator.tick()

        # 待派工 → 解法設計中（THINKER）
        self.assertEqual(self.state(), workflow.DESIGN)

        # 每一棒派工：提示詞帶交件清單、**不帶工作狀態**，也不指向檔案
        for stage in (workflow.DESIGN, workflow.ACCEPTANCE_DESIGN, workflow.BUILD,
                      workflow.VERIFY, workflow.ADJUDICATE):
            dispatch = self._dispatch_text()
            self.assertIn("task_id: T-", dispatch)
            self.assertIn("fbflow_read(target_kind=work", dispatch)
            self.assertNotIn("工單.md", dispatch)
            self.assertNotIn("stage: ", dispatch)
            self.assertIn("## 方法", dispatch)
            if stage == workflow.DESIGN:
                self.submit(stage, self.design_payload(), "ses_thinker")
            elif stage == workflow.ACCEPTANCE_DESIGN:
                self.submit(stage, self.acceptance_payload(), "ses_planner")
            elif stage == workflow.BUILD:
                self.submit(stage, self.build_payload(), "ses_worker")
            elif stage == workflow.VERIFY:
                self.submit(stage, self.verify_payload(), "ses_thinker")
            else:
                self.submit(stage, {"action": "close", "close_reason": "完成",
                                    "handoff": HANDOFF}, "ses_planner")

        self.assertEqual(self.state(), workflow.CLOSED)
        # **全程沒有寫出任何工單檔案**（SQLite 是唯一資料正本）
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "工單")),
                         "引擎又寫出工單資料夾了")

    def test_prompt_version_matches_the_workbook(self):
        """提示詞帶的 `expected_version` 必須等於正本當下的版本。

        不等 → 角色照提示詞交 → 版本不符 → 退件 → 流程原地打轉。
        """
        self.make_work()
        self.coordinator.tick()
        dispatch = self._dispatch_text()
        version = self.version()
        self.assertIn("expected_version: %d" % version, dispatch)

        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        dispatch = self._dispatch_text()
        self.assertIn("expected_version: %d" % self.version(), dispatch)

    def test_dispatch_carries_skill_and_solutions_live_in_the_workbook(self):
        """派工提示詞帶「搭配的專案 SKILL」；已交內容在正本（spec：PLANNER 填寫）。"""
        self.coordinator.create_work("測試工單", "R-001", "把東西做出來",
                                     [{"name": "out.txt"}], skill="my-skill")
        self.coordinator.tick()
        self.assertIn("my-skill", self._dispatch_text())

        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        solutions = workflow.current_solutions(self.conn, "W-001")
        approaches = [sol.get("approach") for sol in solutions]
        self.assertIn("做法甲", approaches)
        self.assertIn("做法乙", approaches)
        # **案號**（PLANNER 選案用）取代 context 欄：解法在正本裡有案號
        self.assertEqual(len(solutions), 2)


class TestNoSeatIsNotSilent(Harness):
    """**沒有人接手時要出聲**——不能靜靜卡住。

    live 實測（2026-11-23 00:43~10:41）：角色沒認領席位 → 派工送不出去、
    10 小時零提交，而引擎**完全沒有留下任何訊息**（journal 只有 dispatch.verify）。
    沒有訊息就沒有人知道要開 session。
    """

    def test_missing_seat_shows_up_in_the_report(self):
        self.make_work()
        # 沒有 THINKER 席位 → 派不出去
        with store.tx(self.conn):
            self.conn.execute("DELETE FROM roles WHERE role = 'THINKER'")
        report = self.coordinator.tick()
        self.assertTrue(report["errors"], "沒有席位卻沒有任何錯誤訊息 → 靜靜卡住")
        self.assertIn("席位", report["errors"][0])

    def test_the_engine_keeps_the_notice_and_reports_it(self):
        """**出聲的只能是引擎**：訊息要留在正本裡，而且要問得到。"""
        self.make_work()
        with store.tx(self.conn):
            self.conn.execute("DELETE FROM roles WHERE role = 'THINKER'")
        self.coordinator.tick()

        # 1) 留在正本（journal）——tick 報告會被覆蓋，journal 不會
        row = self.conn.execute(
            "SELECT target_id, detail FROM journal WHERE op = 'dispatch.no_seat'"
            " ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row, "沒有寫進 journal → 事後查不到")
        self.assertEqual(row["target_id"], "W-001")

        # 2) 問引擎問得到（status 工具帶的那一份）
        listed = self.coordinator.unassigned_targets()
        self.assertTrue(listed, "引擎的狀態裡看不到『沒有人接手』")
        self.assertEqual(listed[0]["id"], "W-001")
        self.assertEqual(listed[0]["role"], "THINKER")

        # 3) 每個 (單, 狀態) 只記一次，不要每 30 秒灌一筆
        self.coordinator.tick()
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM journal WHERE op = 'dispatch.no_seat'").fetchone()["n"]
        self.assertEqual(count, 1, "同一件事被重複記錄 → journal 會灌爆")


class TestWakeUpCallsTheRoleThatShouldWork(Harness):
    """**依照單據狀態判斷現在是誰該做；沒有人在做就喚醒他。**

    角色收到派工就結束回合 → 引擎只能靠再推一把把它叫回來。
    """

    def _backdate_task(self, task_id, minutes=20):
        old = (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat(
            timespec="seconds")
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                              (old, old, task_id))

    def _wakeup_rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM outbox WHERE kind = 'wakeup' ORDER BY id")]

    def test_idle_role_gets_a_wakeup(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(task)
        self._backdate_task(task["id"])
        self.coordinator.tick()

        rows = self._wakeup_rows()
        self.assertEqual(len(rows), 1, "沒有送出喚醒")
        text = rows[0]["payload"]
        # 1) 講清楚現在輪到誰做（依單據狀態判斷）
        self.assertIn("THINKER", text)
        self.assertIn("W-001", text)
        # 2) **不能帶 task_id**——回執核對會把「user 項含 task_id 且之後有
        #    assistant 回覆」當成已交件 → 喚醒帶 task_id 會造成假回執
        self.assertNotIn(task["id"], text, "喚醒帶了 task_id → 會製造假回執")
        # 3) 有獨立錨點（送達狀態靠它核對）
        self.assertIn("WU", text)

    def test_wakeup_stops_after_the_limit(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        for _ in range(6):
            self._backdate_task(task["id"], minutes=20)
            self.coordinator.tick()
        self.assertLessEqual(len(self._wakeup_rows()), engine.MAX_WAKE_UPS,
                             "一直叫不停")

    def test_no_wakeup_while_the_role_is_working(self):
        """角色有在交件 → 不要叫。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.assertEqual(self._wakeup_rows(), [])


class TestHealthCheckFindsTheBadTicket(Harness):
    """**引擎要會巡檢**：自己找出「哪張單有問題」。

    live 實測（2026-11-23 一天）：四段空白合計 20.2 小時（89%），
    每一段的樣子都不一樣，但共同點是**沒有人知道**。
    """

    def test_stuck_work_is_reported(self):
        """很久沒有任何提交的單 → 巡檢要抓出來。"""
        self.make_work()
        self.coordinator.tick()
        # 讓這張單看起來停滯很久（沒有任何提交）
        old = (datetime.datetime.now() - datetime.timedelta(minutes=90)).isoformat(
            timespec="seconds")
        with store.tx(self.conn):
            self.conn.execute("UPDATE works SET created_at = ?, updated_at = ? WHERE id = 'W-001'",
                              (old, old))
        problems = self.coordinator.health_check()
        stuck = [p for p in problems if p["code"] == "stuck" and p["id"] == "W-001"]
        self.assertTrue(stuck, "停滯的單沒有被抓出來：%s" % problems)
        self.assertIn("沒有任何提交", stuck[0]["message"])

    def test_undeliverable_task_is_reported(self):
        """派給一個已經不在角色表上的 session → 派工送不到 → 要抓出來。"""
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(task)
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET assigned_session = 'ses_dead' WHERE id = ?",
                              (task["id"],))
        problems = self.coordinator.health_check()
        codes = {(p["code"], p["id"]) for p in problems}
        self.assertIn(("undeliverable", "W-001"), codes)

    def test_health_problem_is_journalled_once(self):
        """巡檢結果要留在正本裡，但同一個問題不要重複灌。"""
        self.make_work()
        self.coordinator.tick()
        old = (datetime.datetime.now() - datetime.timedelta(minutes=90)).isoformat(
            timespec="seconds")
        with store.tx(self.conn):
            self.conn.execute("UPDATE works SET created_at = ?, updated_at = ? WHERE id = 'W-001'",
                              (old, old))
        self.coordinator.tick()
        first = self.conn.execute(
            "SELECT COUNT(*) AS n FROM journal WHERE op = 'health.problem'").fetchone()["n"]
        self.assertGreaterEqual(first, 1)
        self.coordinator.tick()
        second = self.conn.execute(
            "SELECT COUNT(*) AS n FROM journal WHERE op = 'health.problem'").fetchone()["n"]
        self.assertEqual(first, second, "同一個問題被重複寫進正本")

    def test_healthy_project_has_no_problems(self):
        """正常流程不該被誤報。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        problems = self.coordinator.health_check()
        self.assertEqual([p for p in problems if p["code"] == "stuck"], [])
        self.assertEqual([p for p in problems if p["code"] == "no_seat"], [])


class TestLeaseStaysWithTheNewestVersion(Harness):
    """**租約不能被舊版行程搶走**——搶走 = 這個專案改由舊邏輯驅動。

    live 實測（2026-11-23 23:00~23:05）：一趟 tick 可能比 30 秒的 TTL 還久
    （派工要等宿主 CLI 提交），續約只在 tick 之間做 → 租約在 tick 中途過期 →
    **舊版的 MCP server 搶走**（generation 967）→ 23:02 新版才搶回來。
    中間那一分鐘跑的是舊邏輯（今天的修正全部失效）。

    兩道防線：
    1. **背景續約**：持有者活著就續約，不受慢 tick 影響；
    2. **取得租約時先看版本**：持有者是舊版就直接強制接手，不給空窗期。
    """

    def _write_lease(self, owner, generation, expired=True):
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO leases(name, generation, owner, expires_at) VALUES (?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET generation = excluded.generation,"
                " owner = excluded.owner, expires_at = excluded.expires_at",
                (store.LEASE_COORDINATOR, generation, owner,
                 0 if expired else time.time() + 3600))

    def test_new_version_forces_takeover_from_an_old_holder(self):
        self._write_lease("fbf-old-abcdef01-20200101a", 5, expired=False)
        self.assertTrue(self.coordinator.acquire())
        row = self.conn.execute("SELECT owner, generation FROM leases").fetchone()
        self.assertEqual(row["owner"], self.coordinator.owner)
        self.assertGreater(int(row["generation"]), 5, "舊版持有者沒被換掉")

    def test_takeover_also_works_when_the_lease_already_expired(self):
        """**到期也不能被舊版搶走**——空窗期正是舊版最容易搶到的時候。"""
        self._write_lease("fbf-old-abcdef01-20200101a", 7, expired=True)
        self.assertTrue(self.coordinator.acquire())
        row = self.conn.execute("SELECT owner FROM leases").fetchone()
        self.assertEqual(row["owner"], self.coordinator.owner)

    def test_background_renewer_keeps_the_lease_alive(self):
        """持有者活著 → 租約不該到期（哪怕主迴圈正忙著）。"""
        self.assertTrue(self.coordinator.acquire())
        self.coordinator.lease_renew_interval = 0.2
        # 人為把租約弄成過期 → 背景續約應該把它救回來
        self._write_lease(self.coordinator.owner, self.coordinator.generation, expired=True)
        deadline = time.time() + 5
        alive = False
        while time.time() < deadline:
            row = self.conn.execute("SELECT expires_at FROM leases").fetchone()
            if row["expires_at"] > time.time():
                alive = True
                break
            time.sleep(0.2)
        self.assertTrue(alive, "背景續約沒有把過期的租約救回來")


class TestStalledRoleGetsRedispatched(Harness):
    """角色不回應 → 引擎自己收掉那一棒並重派（最後一個「靜靜卡住」的洞）。

    其他所有重派路徑都假設「角色會看到派工並重交」；
    角色可能根本不理 → task 一直開著、沒有人回報、單子原地不動。
    """

    def _dispatch_text(self, target_id="W-001"):
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (target_id,)).fetchone()
        return row["payload"] if row else ""

    def _backdate(self, task_id, minutes=45):
        old = (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat(
            timespec="seconds")
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                              (old, old, task_id))

    def test_silent_role_gets_a_fresh_task(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(task)
        self._backdate(task["id"])
        self.coordinator.tick()

        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled",
                         "角色不回應卻沒收掉 → 這張單會靜靜卡住")
        new_task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(new_task, "沒有重新派工")
        self.assertNotEqual(new_task["id"], task["id"])
        # 重派的提示詞要講清楚是逾時（角色才知道要接手）
        self.assertIn("逾時", self._dispatch_text("W-001"))

    def test_busy_role_is_not_cut_off(self):
        """**角色還在動就不能收**——計時基準是「角色最近一次交件」。

        同一個 session 排了三棒時，後面兩棒是「在排隊」不是「失聯」。
        """
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        self._backdate(task["id"])
        # 這個角色最近有交件（別張單）→ 還在動 → 不收
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertNotEqual(row["status"], "cancelled",
                            "角色還在交件卻被收掉 → 會白白浪費一次工作")


class TestReworkLifecycleRunsToClosed(Harness):
    """退回 → 重工解法 → **重工驗收方式** → 重工 → 再驗收 → 結案（spec 第 6~9 步）。"""

    def _dispatch_text(self, target_id="W-001"):
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (target_id,)).fetchone()
        return row["payload"] if row else ""

    def test_rework_path(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)

        # PLANNER 判不結案 → 退回（施工錯 → 回重工解法設計）
        self.submit(workflow.ADJUDICATE,
                    {"action": "rework", "rework_reason": "產出少了一項",
                     "rework_type": workflow.REWORK_TYPE_BUILD, "handoff": HANDOFF},
                    "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_DESIGN)

        # 退回原因留在正本（角色用 fbflow_read 讀得到）
        work = workflow.load_work(self.conn, "W-001")
        self.assertIn("產出少了一項", work.get("rework_reason") or "")
        # 這一棒的交件清單在派工提示詞裡
        dispatch = self._dispatch_text("W-001")
        self.assertIn("rework_plan", dispatch)

        # THINKER 出兩案 → PLANNER 選案並設計這一輪的驗收方式
        self.submit(workflow.REWORK_DESIGN,
                    self.rework_design_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.REWORK_ACCEPTANCE_DESIGN)
        dispatch = self._dispatch_text("W-001")
        self.assertIn("selection", dispatch)
        self.assertIn("checks", dispatch)

        self.submit(workflow.REWORK_ACCEPTANCE_DESIGN,
                    self.rework_acceptance_payload(solution_id="ctx-c"), "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_BUILD)

        self.submit(workflow.REWORK_BUILD, self.build_payload(), "ses_worker")
        self.assertEqual(self.state(), workflow.VERIFY)
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")
        self.assertEqual(self.state(), workflow.ADJUDICATE)
        self.submit(workflow.ADJUDICATE,
                    {"action": "close", "close_reason": "重做完成", "handoff": HANDOFF},
                    "ses_planner")
        self.assertEqual(self.state(), workflow.CLOSED)
        # 全程沒有寫出任何工單檔案
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "工單")))


class TestIssueLifecycleRunsToClosed(Harness):
    """ISSUE 六棒（spec 第 4(b) 步）走完 → 正本結案，全程不寫檔案。"""

    def _make_issue(self):
        work_id = self.make_work()
        issue_id = self.coordinator.create_issue(
            work_id, "ses_worker", "WORKER", "work_blocked",
            "同一個問題撞了三次", "build", "換過兩種做法", "停工",
            "要不要換目標", "重跑一次就過")
        self.coordinator.tick()
        return issue_id

    def _dispatch_text(self, target_id):
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (target_id,)).fetchone()
        return row["payload"] if row else ""

    def test_issue_path_to_closed(self):
        issue_id = self._make_issue()
        # ISSUE 內容在正本（角色用 fbflow_read 讀得到）
        issue = workflow.load_issue(self.conn, issue_id)
        self.assertEqual(issue.get("completion_criteria"), "重跑一次就過")

        # 解法設計中（THINKER 兩個獨立 SUBAGENT）
        state = self.conn.execute("SELECT state FROM issues WHERE id = ?",
                                  (issue_id,)).fetchone()["state"]
        self.assertEqual(state, workflow.ISSUE_DESIGNING)
        dispatch = self._dispatch_text(issue_id)
        self.assertIn("fbflow_read(target_kind=issue", dispatch)
        self.assertNotIn("ISSUE.md", dispatch)
        self.assertNotIn("state: ", dispatch)
        self.assertIn("## 方法", dispatch)

        self._submit_issue(issue_id, workflow.ISSUE_DESIGNING, {
            "solutions": [
                {"subagent_context_id": "icx-a", "approach": "甲", "cost": "低",
                 "change_scope": "小", "effort": "少", "risk": "低",
                 "verifiable_by": "跑測試", "reuses": "無"},
                {"subagent_context_id": "icx-b", "approach": "乙", "cost": "高",
                 "change_scope": "大", "effort": "多", "risk": "中",
                 "verifiable_by": "跑測試", "reuses": "無"},
            ], "handoff": HANDOFF}, "ses_thinker")
        self.assertEqual(self._issue_state(issue_id), workflow.ISSUE_HANDLING)

        # 對策中（PLANNER 選案＋對策）
        self._submit_issue(issue_id, workflow.ISSUE_HANDLING, {
            "selection": {"solution_id": "icx-a", "reason": "成本小"},
            "countermeasure": "改用甲，重跑一次", "handoff": HANDOFF}, "ses_planner")
        self.assertEqual(self._issue_state(issue_id), workflow.ISSUE_BUILDING)

        # 依對策施工中（WORKER）
        self._submit_issue(issue_id, workflow.ISSUE_BUILDING,
                            self.build_payload(), "ses_worker")
        self.assertEqual(self._issue_state(issue_id), workflow.ISSUE_ADJUDICATING)

        # 結案中（**原發起者**驗證 → 結案）
        self._submit_issue(issue_id, workflow.ISSUE_ADJUDICATING,
                            {"resolution": "對策生效", "handoff": HANDOFF},
                            "ses_worker")
        self.assertEqual(self._issue_state(issue_id), workflow.ISSUE_CLOSED)
        # 全程沒有寫出任何 ISSUE 檔案
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ISSUE")))

    def test_issue_return_twice_parks_for_human(self):
        """**連續兩次沒解決就停下來等人類裁決**（spec 第 4(b) 步、第 95 行）。"""
        issue_id = self._make_issue()
        for _ in range(2):
            self._submit_issue(issue_id, workflow.ISSUE_DESIGNING, {
                "solutions": [
                    {"subagent_context_id": "icx-a", "approach": "甲", "cost": "低",
                     "change_scope": "小", "effort": "少", "risk": "低",
                     "verifiable_by": "跑測試", "reuses": "無"},
                    {"subagent_context_id": "icx-b", "approach": "乙", "cost": "高",
                     "change_scope": "大", "effort": "多", "risk": "中",
                     "verifiable_by": "跑測試", "reuses": "無"},
                ], "handoff": HANDOFF}, "ses_thinker")
            self._submit_issue(issue_id, workflow.ISSUE_HANDLING, {
                "selection": {"solution_id": "icx-a", "reason": "成本小"},
                "countermeasure": "改用甲", "handoff": HANDOFF}, "ses_planner")
            self._submit_issue(issue_id, workflow.ISSUE_BUILDING,
                                self.build_payload(), "ses_worker")
            self._submit_issue(issue_id, workflow.ISSUE_ADJUDICATING,
                                {"return_reason": "還是沒解決", "handoff": HANDOFF},
                                "ses_worker")
        row = self.conn.execute(
            "SELECT awaiting_human, awaiting_reason, return_count FROM issues WHERE id = ?",
            (issue_id,)).fetchone()
        self.assertEqual(int(row["awaiting_human"]), 1, "退兩次還沒標待人處理")
        self.assertEqual(row["awaiting_reason"], "issue_return_limit")
        self.assertEqual(int(row["return_count"]), 2)

    def test_issue_still_advances_after_a_permanent_rejection(self):
        """永久退件（回執被重複使用）之後，**ISSUE 仍要能往前推**。

        退件原本**不升 ISSUE 版本** → 角色用同一個版本重交 → 撞上
        `(task_id, session_id, stage, expected_version)` 的唯一約束 →
        `submit_issue` 回傳原本那筆（已退件的）submission →
        **這張 ISSUE 再也推不動**（實測會卡死）。
        """
        issue_id = self._make_issue()
        # 第一次交件（正常）→ 進入對策中
        used_message = self._submit_issue(issue_id, workflow.ISSUE_DESIGNING, {
            "solutions": [
                {"subagent_context_id": "icx-a", "approach": "甲", "cost": "低",
                 "change_scope": "小", "effort": "少", "risk": "低",
                 "verifiable_by": "跑測試", "reuses": "無"},
                {"subagent_context_id": "icx-b", "approach": "乙", "cost": "高",
                 "change_scope": "大", "effort": "多", "risk": "中",
                 "verifiable_by": "跑測試", "reuses": "無"},
            ], "handoff": HANDOFF}, "ses_thinker")
        version_before = int(self.conn.execute(
            "SELECT version FROM issues WHERE id = ?", (issue_id,)).fetchone()["version"])

        # 第二棒**沿用同一個 message_id** → 永久退件（回執不可重複使用）
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE issue_id = ? AND stage = ? AND status = 'open'",
            (issue_id, workflow.ISSUE_HANDLING)).fetchone()
        self.assertIsNotNone(task)
        self.coordinator.submit_issue(task["id"], issue_id, version_before,
                                      "ses_planner", workflow.ISSUE_HANDLING,
                                      {"selection": {"solution_id": "icx-a"},
                                       "countermeasure": "改用甲", "handoff": HANDOFF},
                                      used_message)
        report = self.coordinator.tick()
        self.assertTrue(report["rejected"], report)

        # 版本必須 +1，否則重交會被當成重送（冪等）→ ISSUE 卡死
        version_after = int(self.conn.execute(
            "SELECT version FROM issues WHERE id = ?", (issue_id,)).fetchone()["version"])
        self.assertGreater(version_after, version_before,
                           "永久退件沒有升 ISSUE 版本 → 這張單會卡死")

        # **必須重新派一棒**：沒有人通知角色，角色不會自己重交。
        new_task = self.conn.execute(
            "SELECT * FROM tasks WHERE issue_id = ? AND stage = ? AND status = 'open'",
            (issue_id, workflow.ISSUE_HANDLING)).fetchone()
        self.assertIsNotNone(new_task, "永久退件後沒有重新派工 → ISSUE 靜靜卡住")
        self.assertNotEqual(new_task["id"], task["id"])

        # 角色用新版本重交 → ISSUE 真的往前推
        self.coordinator.submit_issue(new_task["id"], issue_id, version_after,
                                      "ses_planner", workflow.ISSUE_HANDLING,
                                      {"selection": {"solution_id": "icx-a"},
                                       "countermeasure": "改用甲", "handoff": HANDOFF},
                                      "msg-fresh")
        self.coordinator.tick()
        self.assertEqual(self._issue_state(issue_id), workflow.ISSUE_BUILDING)

    # ------------------------------------------------------------ 工具

    def _issue_state(self, issue_id):
        return self.conn.execute("SELECT state FROM issues WHERE id = ?",
                                 (issue_id,)).fetchone()["state"]

    def _submit_issue(self, issue_id, stage, payload, session_id):
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE issue_id = ? AND stage = ? AND status = 'open'"
            " ORDER BY created_at DESC LIMIT 1", (issue_id, stage)).fetchone()
        self.assertIsNotNone(task, "找不到 ISSUE %s 的 %s task" % (issue_id, stage))
        issue = self.conn.execute("SELECT version FROM issues WHERE id = ?",
                                  (issue_id,)).fetchone()
        # **每次交件都要不同的 message_id**：平台訊息不可重複使用，
        # 重用會被引擎擋下（receipt.message_reused）——那是對的行為。
        self._issue_counter = getattr(self, "_issue_counter", 0) + 1
        message_id = "msg-%s-%d" % (issue_id, self._issue_counter)
        self.coordinator.submit_issue(task["id"], issue_id, int(issue["version"]),
                                      session_id, stage, payload, message_id)
        self.coordinator.tick()
        return message_id


if __name__ == "__main__":
    unittest.main()
