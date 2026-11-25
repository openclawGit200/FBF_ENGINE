"""2026-11-25 專案結案復盤（PRJ-001）四項引擎改善的守門測試。

每一組測試對應一條 `engine_improvements` 紀錄：

1. threshold 比對邏輯——描述性中文門檻加嚴不得被誤判為「門檻被降低」。
2. message_id 唯一性——退件要附上撞到的提交編號，派工提示要寫明取用規則。
3. adjudicate action 欄位驗證——提示與守衛必須講同一套合法值。
4. review remedy 驗證——`review.revise_not_allowed` 等缺口要有可讀的說明。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from tests.test_fbf_engine import HANDOFF, Harness  # noqa: F401
from fbf_engine import engine, platform, prompts, store, workflow


# ---------------------------------------------------------------- 1. threshold

class TestDescriptiveThreshold(Harness):
    """敘述型中文門檻：加嚴要能過，放寬要能擋。

    實測（2026-11-24 目錄拆產品圖3）：PLANNER 在 rework_acceptance_design
    把門檻加嚴（用 `;` / `，` 串多條要求），引擎卻判 `check.threshold_lowered`
    連退 13 次，最後那次成功的提交是把門檻**改回原樣**——加嚴白做。
    """

    def test_descriptive_threshold_can_be_tightened(self):
        self.assertTrue(workflow._threshold_not_lowered(
            "平衡等式成立", "平衡等式成立；三本逐列統計數字可獨立重算吻合"))

    def test_descriptive_threshold_tightened_with_comma(self):
        self.assertTrue(workflow._threshold_not_lowered(
            "平衡等式成立", "平衡等式成立, 三本逐列統計數字可獨立重算吻合"))

    def test_descriptive_threshold_with_ascii_semicolon(self):
        self.assertTrue(workflow._threshold_not_lowered(
            "100% 收錄列三欄齊全且 pass",
            "100% 收錄列三欄齊全且 pass; 逐列 CSV 核對無例外"))

    def test_descriptive_threshold_still_blocks_weakening(self):
        """加嚴放行 ≠ 放寬也放行。"""
        self.assertFalse(workflow._threshold_not_lowered(
            "平衡等式成立", "平衡等式大概成立就好"))

    def test_percentage_cannot_sneak_down_in_text(self):
        """「100% 卡片五欄齊全」→「100% 卡片五欄齊全率 80%」要擋。"""
        self.assertFalse(workflow._threshold_not_lowered(
            "100% 卡片五欄齊全", "100% 卡片五欄齊全率 80%"))

    def test_loosener_wording_is_still_blocked(self):
        for loosener in ("at least 95% or manual override",
                         "at least 95% is optional",
                         "at least 95% 可選"):
            self.assertFalse(workflow._threshold_not_lowered("at least 95%", loosener),
                             "放寬寫法卻通過了：%s" % loosener)

    def test_no_exception_is_not_treated_as_loosener(self):
        """「核對無例外」是**收緊**，不是放行字樣（實測被誤判成放寬）。"""
        self.assertFalse(workflow._threshold_has_loosener("逐列 CSV 核對無例外"))
        self.assertTrue(workflow._threshold_has_loosener("逐列 CSV 核對有例外"))


# ---------------------------------------------------------------- 3. adjudicate

class TestAdjudicateFieldValidation(Harness):
    """adjudicate 欄位：提示寫的合法值，守衛就必須收那些值。

    實測：提示的「標準／範例」寫 `build／acceptance／solution`，
    守衛只認「施工錯／驗收錯／解法錯」→ 角色照提示填英文 → 連退 7 次。
    """

    def _to_adjudicate(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(), "ses_planner")
        self.submit(workflow.BUILD, self.build_payload(), "ses_worker")
        self.submit(workflow.VERIFY, self.verify_payload(), "ses_thinker")

    def test_english_rework_type_is_accepted(self):
        """照提示填英文 rework_type 也要過。"""
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "做得不對",
                   "rework_type": "build", "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertEqual(self.state(), workflow.REWORK_DESIGN)

    def test_english_rework_type_is_normalized_to_chinese(self):
        """存進正本的是正本中文值（下游分流靠它）。"""
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "做得不對",
                   "rework_type": "build", "handoff": HANDOFF}
        self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        row = self.conn.execute(
            "SELECT rework_type FROM works WHERE id = 'W-001'").fetchone()
        self.assertEqual(row["rework_type"], workflow.REWORK_TYPE_BUILD)

    def test_illegal_rework_type_has_its_own_gap_code(self):
        """rework_type 填錯要用**專屬**缺口代碼——不能說成 action 不合法。"""
        self._to_adjudicate()
        payload = {"action": "rework", "rework_reason": "做得不對",
                   "rework_type": "施工", "handoff": HANDOFF}
        _, report = self.submit(workflow.ADJUDICATE, payload, "ses_planner")
        self.assertTrue(report["rejected"], report)
        row = self.conn.execute(
            "SELECT gaps FROM submissions WHERE status = 'rejected'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn(workflow.GAP_REWORK_TYPE_INVALID, row["gaps"])
        self.assertNotIn(workflow.GAP_ACTION_INVALID, row["gaps"])

    def test_illegal_rework_type_hint_lists_allowed_values(self):
        """退件說明要列出允許值——角色才知道要改成什麼。"""
        self.assertIn("施工錯", workflow.GAP_HINTS[workflow.GAP_REWORK_TYPE_INVALID])
        self.assertIn("close", workflow.GAP_HINTS[workflow.GAP_ACTION_INVALID])

    def test_prompt_states_the_same_legal_values_as_the_guard(self):
        """派工提示列出的 rework_type 合法值必須是守衛真的收的那些。"""
        contract = workflow.stage_contract(workflow.ADJUDICATE)
        standard = workflow.standard_of("rework_type", contract["field_specs"]["rework_type"])
        for value in workflow.REWORK_TYPES:
            self.assertIn(value, standard)
        for alias in workflow.REWORK_TYPE_ALIASES:
            self.assertIn(alias, standard)


# ---------------------------------------------------------------- 4. review remedy

class TestReviewRemedyHints(Harness):
    """`review` 那一棒的缺口說明不能缺——缺了角色只看到一個代碼。"""

    def test_review_gaps_all_have_human_readable_hints(self):
        for code in (workflow.GAP_REVIEW_DIAGNOSIS_INVALID,
                     workflow.GAP_REVIEW_REMEDY_INVALID,
                     workflow.GAP_REVIEW_REVISE_NOT_ALLOWED):
            self.assertIn(code, workflow.GAP_HINTS,
                          "%s 沒有可讀的退件說明" % code)

    def test_revise_hint_explains_when_revise_is_forbidden(self):
        hint = workflow.GAP_HINTS[workflow.GAP_REVIEW_REVISE_NOT_ALLOWED]
        self.assertIn("revise", hint)
        self.assertIn("halt", hint)

    def test_prompt_explains_revise_condition(self):
        """派工提示要講清楚 revise 什麼時候不能用。"""
        contract = workflow.stage_contract(workflow.ADJUDICATE)
        note = contract["field_specs"]["remedy"]["note"]
        self.assertIn("revise", note)
        self.assertIn("halt", note)


# ---------------------------------------------------------------- 2. message_id

class TestMessageReuseFeedback(Harness):
    """message_id 被撞到時，退件要說清楚**撞到哪一筆**。"""

    def test_rejection_note_carries_the_conflicting_submission(self):
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker",
                    message_id="msg-shared")
        self.submit(workflow.ACCEPTANCE_DESIGN, self.acceptance_payload(),
                    "ses_planner", message_id="msg-shared")
        row = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'dispatch'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        text = row["payload"] or ""
        self.assertIn("msg-shared", text)
        self.assertIn("S-0000", text)

    def test_dispatch_prompt_states_message_id_rule(self):
        """派工提示要寫明 message_id 每次都要新的、不可沿用。"""
        self.make_work()
        self.coordinator.tick()
        text = self.conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'dispatch'"
            " ORDER BY id DESC LIMIT 1").fetchone()["payload"]
        self.assertIn("message_id", text)
        self.assertIn("不可沿用", text)

    def test_session_prompt_states_message_id_rule(self):
        """session 私有提示詞（角色自己讀的）也要有取用規則。"""
        self.make_work()
        self.coordinator.tick()
        self.submit(workflow.DESIGN, self.design_payload(), "ses_thinker")
        out = prompts.render_session_prompt(self.conn, "ses_planner", "PRJ-001")
        self.assertIn("message_id", out)
        self.assertIn("不可沿用", out)


# ---------------------------------------------------------------- 5. status 欄位

class TestImprovementStatus(Harness):
    """`engine_improvements` 要能標記「已處理／不採用」，否則復盤無進度。"""

    def test_status_column_exists_and_defaults_to_open(self):
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO engine_improvements(review_id, target, suggestion, reason,"
                " created_at) VALUES ('PRJ-001', '引擎', '測試建議', '因為', ?)",
                (store.now(),))
        row = self.conn.execute(
            "SELECT status FROM engine_improvements ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], "open")

    def test_status_can_be_marked_done(self):
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO engine_improvements(review_id, target, suggestion, reason,"
                " created_at) VALUES ('PRJ-001', '引擎', '測試建議', '因為', ?)",
                (store.now(),))
            row = self.conn.execute(
                "SELECT id FROM engine_improvements ORDER BY id DESC LIMIT 1").fetchone()
            self.conn.execute(
                "UPDATE engine_improvements SET status = 'done' WHERE id = ?",
                (row["id"],))
        after = self.conn.execute(
            "SELECT status FROM engine_improvements WHERE id = ?", (row["id"],)).fetchone()
        self.assertEqual(after["status"], "done")


if __name__ == "__main__":
    unittest.main()
