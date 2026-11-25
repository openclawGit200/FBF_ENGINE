"""**宿主活動判型**與「只有完全沒動靜才催」的守門測試。

**為什麼要有這條線**（使用者裁決 2026-11-25，實測一個批次處理專案）：
WORKER 跑 長時間批次工作，被引擎判了 **15 次逾時**並重派——
但每一分鐘它的 Claude Code transcript 都還有紀錄，最後一筆全是
**模型請求**（`api-request`），其中 4 次是模型端直接回錯
（`API Error: 422 … content safety screening`），另有 5 次 context 被壓縮。

引擎把「在等模型」當「失聯」→ 殺掉 task、重派，
還在同一個回合裡一次插進 4 張新派工打斷它。

**所以**：
1. 只有「回合已結束、什麼都沒在動」（`idle`）才催、才收掉。
2. 在等模型／在跑工具／剛被壓縮 → **一律不催、不收**。
3. 查不到宿主紀錄 → **不知道** → 門檻加倍（寧可晚點催，也不要誤殺）。

這個檔守三件事：
- `hostact.classify()` 的型態判斷（純函式）。
- 引擎層的行為：活著的不催不收、idle 的照舊、unknown 的門檻加倍。
- 模型端卡很久 → **只記健康事件，不收掉 task**（不是 agent 的錯）。
"""
from __future__ import annotations

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import engine, hostact, store, workflow  # noqa: E402
from tests.test_fbf_engine import Harness  # noqa: E402


class TestClassify(unittest.TestCase):
    """型態判斷（純函式）。"""

    def test_tool_use_without_result_is_running(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash"}]}}]
        self.assertEqual(hostact.classify(events), hostact.STATE_RUNNING_TOOL)

    def test_tool_result_makes_it_idle(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash"}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1"}]}},
        ]
        self.assertEqual(hostact.classify(events), hostact.STATE_IDLE)

    def test_api_request_is_waiting_for_the_model(self):
        self.assertEqual(hostact.classify([{"type": "api-request"}]),
                         hostact.STATE_WAITING_MODEL)
        self.assertEqual(hostact.classify([{"type": "queue-operation"}]),
                         hostact.STATE_WAITING_MODEL)

    def test_compacted_is_compacting(self):
        events = [{"type": "system", "content": "Conversation compacted"}]
        self.assertEqual(hostact.classify(events), hostact.STATE_COMPACTING)

    def test_turn_end_is_idle(self):
        events = [{"type": "system", "content": "turn_duration"}]
        self.assertEqual(hostact.classify(events), hostact.STATE_IDLE)

    def test_empty_is_unknown(self):
        self.assertEqual(hostact.classify([]), hostact.STATE_UNKNOWN)

    def test_alive_states(self):
        self.assertTrue(hostact.is_alive(hostact.STATE_WAITING_MODEL))
        self.assertTrue(hostact.is_alive(hostact.STATE_RUNNING_TOOL))
        self.assertTrue(hostact.is_alive(hostact.STATE_COMPACTING))
        self.assertFalse(hostact.is_alive(hostact.STATE_IDLE))
        self.assertFalse(hostact.is_alive(hostact.STATE_UNKNOWN))


class TestEngineOnlyNudgesWhenReallyIdle(Harness):

    def _backdate_task(self, task_id, minutes=20):
        old = (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat(
            timespec="seconds")
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                              (old, old, task_id))

    def _wakeup_rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM outbox WHERE kind = 'wakeup' ORDER BY id")]

    def _stalled_rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM journal WHERE op = 'task.stalled' ORDER BY rowid")]

    def _prepare(self):
        self.make_work()
        self.coordinator.tick()
        task = self.open_task("W-001", workflow.DESIGN)
        self.assertIsNotNone(task)
        self._backdate_task(task["id"], minutes=40)
        return task

    def test_waiting_for_the_model_is_not_woken_up(self):
        """**在等模型 → 不催**（插訊息會打斷它正在做的事）。"""
        task = self._prepare()
        self._set_host_state(hostact.STATE_WAITING_MODEL)
        self.coordinator.tick()
        self.assertEqual(self._wakeup_rows(), [], "在等模型卻被叫了")

    def test_waiting_for_the_model_is_not_cancelled(self):
        """**在等模型 → 不收掉 task**（那不是 agent 的錯）。"""
        task = self._prepare()
        self._set_host_state(hostact.STATE_WAITING_MODEL)
        self.coordinator.tick()
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "open", "在等模型卻被收掉重派")

    def test_running_tool_is_not_cancelled(self):
        task = self._prepare()
        self._set_host_state(hostact.STATE_RUNNING_TOOL)
        self.coordinator.tick()
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "open", "在跑工具卻被收掉重派")

    def test_compacting_is_not_cancelled(self):
        task = self._prepare()
        self._set_host_state(hostact.STATE_COMPACTING)
        self.coordinator.tick()
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "open", "剛被壓縮卻被收掉重派")

    def test_idle_is_still_woken_up_and_cancelled(self):
        """**真的沒動靜 → 照舊**：先喚醒，逾時才收掉重派。"""
        task = self._prepare()
        self._set_host_state(hostact.STATE_IDLE)
        self.coordinator.tick()
        self.assertTrue(self._wakeup_rows(), "真的沒動靜卻沒被叫")
        self._backdate_task(task["id"], minutes=40)
        self.coordinator.tick()
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled", "真的沒動靜卻沒收掉")

    def test_unknown_host_record_doubles_the_threshold(self):
        """**查不到宿主紀錄 → 門檻加倍**（寧可晚點催，也不要誤殺）。"""
        task = self._prepare()
        self._set_host_state(hostact.STATE_UNKNOWN)
        self.coordinator.tick()
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "open",
                         "查不到宿主紀錄就照 30 分鐘收掉 → 可能誤殺")
        # 超過加倍門檻才收
        self._backdate_task(task["id"], minutes=80)
        self.coordinator.tick()
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled",
                         "查不到宿主紀錄又真的沒動靜太久，卻一直不收")

    def test_model_stuck_is_recorded_but_not_cancelled(self):
        """**模型端卡很久 → 記健康事件，但不收掉 task**。"""
        task = self._prepare()
        stale = datetime.datetime.now() - datetime.timedelta(
            minutes=engine.MODEL_STALL_SECONDS // 60 + 30)
        self._set_host_state(hostact.STATE_WAITING_MODEL, at=stale.timestamp())
        self.coordinator.tick()
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (task["id"],)).fetchone()
        self.assertEqual(row["status"], "open", "模型端卡住不是 agent 的錯")
        events = [dict(r) for r in self.conn.execute(
            "SELECT * FROM journal WHERE op = 'health.model_stalled'")]
        self.assertTrue(events, "模型端停滯沒有留下可見的紀錄")


if __name__ == "__main__":
    unittest.main()
