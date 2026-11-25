"""**送進 session 的只有提示詞**——兩道守門。

**一、提示詞要薄**（使用者裁決 2026-11-25）：
「不要把任何文件送進 SESSION，只有提示詞可以送進 SESSION，
所有的工作狀態都留在磁碟上。」

實測（2026-11-25一個批次處理專案）：派工 payload 中位 1,843 字元，
**八成是欄位的標準與範例**——那是規格文件，角色自己用 `fbflow_read`
的 `contract` → `field_specs` 就查得到，不必每則派工重複貼一份。
貼太多會擠爆對話上下文（實測 WORKER 的 context 被壓縮 5 次），
也讓「模型端內容被安全篩選擋下」更容易發生。

**二、同一個 session 這一趟只送一則**：
實測引擎在 90 秒內把 4 張重派工單一起插進正在跑批次的對話。

這個檔守三件事：
- 派工提示詞**不含範例長文**，但**保留關鍵規則**（如「solutions 至少 2 案」——
  實測 agent 就是因為沒被告知才只交 1 案而被退）。
- 提示詞要指路「規格在 fbflow_read 的 contract 裡」。
- 同一 session 這一趟只收到一則提示詞，其餘延後（不丟）。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import engine, platform, prompts, workflow  # noqa: E402
from tests.test_fbf_engine import Harness  # noqa: E402


class TestDispatchPromptIsThin(Harness):

    def _first_dispatch(self):
        self.make_work()
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'dispatch' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row, "沒有派工")
        return row["payload"]

    def test_prompt_drops_the_examples(self):
        payload = self._first_dispatch()
        self.assertNotIn("範例：", payload,
                         "派工提示詞還在貼範例長文 → 規格文件留在磁碟就好")

    def test_prompt_keeps_the_rules_that_agents_get_wrong(self):
        """**規則要留**——實測 agent 因為沒被告知「至少 2 案」而被退件。"""
        payload = self._first_dispatch()
        self.assertIn("至少 2 案", payload,
                      "design 棒必交的 solutions 有「至少 2 案」的規則，不能被刪掉")

    def test_prompt_points_at_the_contract(self):
        payload = self._first_dispatch()
        self.assertIn("field_specs", payload,
                      "提示詞要指路規格在哪（fbflow_read 的 contract）")

    def test_prompt_stays_small(self):
        """提示詞要薄，但**薄不是唯一指標**：
        這一版比原始（1,843 字）省了兩成，換來的是多帶了兩樣東西——
        **本輪 message_id**（消滅 `receipt.message_reused` 退回）與
        **design 棒的「至少兩案」規則**（消滅 `solutions.count` 退回）。
        這兩樣值 60 字，不砍。
        """
        payload = self._first_dispatch()
        self.assertLess(len(payload), 1500,
                         "派工提示詞 %d 字 → 還是太肥" % len(payload))
        self.assertLess(len(payload), 1843,
                         "比原始版（1,843 字）還肥 → 瘦身沒做")


class TestOnePromptPerSessionPerTick(Harness):

    def test_two_works_for_one_session_send_one_prompt_per_tick(self):
        """兩張單都等同一個 session → **這一趟只插一則**，另一則延後。"""
        self.make_work()
        self.coordinator.create_work("第二張", "R-001", "內容", [])
        sent = []
        real_send = platform.send_to_session
        platform.send_to_session = lambda root, row, text, **kwargs: (
            sent.append((row.get("session_id"), text)) or
            {"state": "queued", "channel": "test"})
        try:
            self.coordinator.tick()
        finally:
            platform.send_to_session = real_send

        targets = [s for s, _ in sent if s == "ses_thinker"]
        self.assertEqual(len(targets), 1,
                         "同一個 session 這一趟被插了 %d 則 → 一次塞太多" % len(targets))
        # 沒送的那一則**沒有被丟掉**，只是延後。
        queued = self.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE kind = 'dispatch'"
            " AND status = 'queued'").fetchone()["n"]
        self.assertGreaterEqual(queued, 1, "延後的那一則不該被丟掉")


class TestDispatchNonce(Harness):
    """**這一輪的 message_id 由引擎發**——agent 照抄，不必自己編。

    實測 2026-11-25：規則明明寫在提示詞裡（「每次交件都要新的」），
    agent 還是重複用了同一個 → 被 `receipt.message_reused` 退件。
    原因是**它手上沒有「新的 id」的來源**——只能自己編，編錯就重複。
    """

    def _dispatch_payload(self):
        self.make_work()
        self.coordinator.tick()
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'dispatch' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row, "沒有派工")
        return row["payload"]

    def test_dispatch_carries_a_message_id(self):
        payload = self._dispatch_payload()
        self.assertIn("message_id: msg-", payload,
                      "派工沒帶本輪 message_id → agent 只能自己編")

    def test_prompt_says_copy_it(self):
        payload = self._dispatch_payload()
        self.assertIn("照抄", payload)
        self.assertIn("不可沿用", payload)

    def test_nonce_changes_with_the_round(self):
        """**每一輪不同**——版本 +1（退件重交）就是新的一輪。"""
        first = platform.dispatch_message_id(
            {"id": "T-000001", "created_at": "2026-11-25T10:00:00"}, {"version": 2})
        same = platform.dispatch_message_id(
            {"id": "T-000001", "created_at": "2026-11-25T10:00:00"}, {"version": 2})
        later = platform.dispatch_message_id(
            {"id": "T-000001", "created_at": "2026-11-25T10:00:00"}, {"version": 3})
        self.assertEqual(first, same, "同一輪要給同一個 id")
        self.assertNotEqual(first, later, "新的一輪要用新的 id")

    def test_nonce_does_not_leak_the_task_id(self):
        """nonce 是雜湊——**不含 task id**（帶了會造成假回執）。"""
        value = platform.dispatch_message_id(
            {"id": "T-000042", "created_at": "2026-11-25T10:00:00"}, {"version": 1})
        self.assertNotIn("T-000042", value)
        self.assertTrue(value.startswith("msg-"))


class TestDesignRuleIsStatedUpfront(Harness):
    """**規則要事前講清楚**——實測 THINKER 只交 1 案被退。"""

    def test_design_prompt_says_two_solutions(self):
        self.make_work()
        self.coordinator.tick()
        payload = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'dispatch' ORDER BY id LIMIT 1"
        ).fetchone()["payload"]
        self.assertIn("至少兩案", payload,
                      "design 棒沒講『至少兩案』→ agent 會只交一案")

    def test_design_contract_keeps_the_two_solution_rule(self):
        contract = workflow.stage_contract(workflow.DESIGN)
        self.assertIn("至少兩案", contract.get("required_note") or "")


class TestNonceRoundTrip(Harness):
    """**agent 照抄 nonce 交件 → 引擎要接受**（端到端）。"""

    def _nonce_from(self, text):
        for line in text.splitlines():
            if line.startswith("message_id: "):
                return line.split(":", 1)[1].strip()
        return ""

    def test_copying_the_nonce_is_accepted(self):
        self.make_work()
        self.coordinator.tick()
        payload = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'dispatch' ORDER BY id LIMIT 1"
        ).fetchone()["payload"]
        nonce = self._nonce_from(payload)
        self.assertTrue(nonce, "派工沒帶 message_id")

        task = self.open_task("W-001", workflow.DESIGN)
        result = self.coordinator.submit_work(
            task["id"], "W-001", self.version("W-001"), "ses_thinker",
            workflow.DESIGN, self.design_payload(), nonce)
        self.assertEqual(result.get("status"), "pending", result)
        self.assertEqual(result.get("gaps"), [], result)

    def test_the_same_nonce_on_another_task_is_refused(self):
        """**同一個 nonce 用在另一張單 → 回執核對要擋**（守衛不能因此失效）。

        去重發生在**回執核對**層（提交時不擋）——
        因為「同一輪重試」是合法的，只有「用在別的提交上」才不可信。
        """
        self.make_work()
        self.coordinator.tick()
        payload = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'dispatch' ORDER BY id LIMIT 1"
        ).fetchone()["payload"]
        nonce = self._nonce_from(payload)
        task = self.open_task("W-001", workflow.DESIGN)
        self.coordinator.submit_work(
            task["id"], "W-001", self.version("W-001"), "ses_thinker",
            workflow.DESIGN, self.design_payload(), nonce)

        # 第二張單也用同一個 nonce 交件
        self.coordinator.create_work("第二張", "R-001", "內容", [])
        self.coordinator.tick()
        other = self.open_task("W-002", workflow.DESIGN)
        if other is None:
            self.skipTest("這張單還沒派工")
        self.coordinator.submit_work(
            other["id"], "W-002", self.version("W-002"), "ses_thinker",
            workflow.DESIGN, self.design_payload(), nonce)

        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, message_id FROM submissions ORDER BY id")]
        self.assertEqual(len(rows), 2)
        result = self.coordinator._verify_receipt(rows[1])
        self.assertIsNotNone(result)
        self.assertFalse(result.get("verified"), "同一個 nonce 用在別的提交上卻被當成可信回執")
        self.assertEqual(result.get("reason"), "receipt.message_reused")
        self.assertTrue(result.get("permanent"))


if __name__ == "__main__":
    unittest.main()
