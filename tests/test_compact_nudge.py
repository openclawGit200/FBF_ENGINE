"""**上下文收合提醒**（compact nudge）的守門測試。

使用者裁決（2026-11-25）：session 沒有工作可以做、而且離上一次 compact
已經超過半小時 → 引擎發提示詞叫它 /compact。

這個檔案守四件事：

1. **「沒有工作可以做」是看單據狀態**：等這個角色、還沒人做的單 → 不算閒置；
   有開著的 task → 不算閒置。
2. **不會打斷正在做事的 session**：閒置落定期（剛交完件、剛被派過單不算閒置）。
3. **不會一直叫**：距上一次 compact 未滿半小時 → 不叫；叫過之後也是半小時內不再叫。
4. **提醒走既有 outbox 通道**，用**獨立錨點**（帶真 task_id 會造成假回執）。

測試不碰真的宿主：用替身取代 compact 的偵測，離開正本與提示詞本身不變。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import compact, engine, platform, store, workflow  # noqa: E402

PLANNER = "ses_planner"
THINKER = "ses_thinker"
WORKER = "ses_worker"


class CompactHarness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-compact-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seed = store.connect(self.tmp)
        store.init_db(seed)
        with store.tx(seed):
            for role, short, sid in (("PLANNER", "P1", PLANNER),
                                     ("THINKER", "T1", THINKER),
                                     ("WORKER", "W1", WORKER)):
                seed.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at)"
                    " VALUES (?,?,?,?, 'active', '', '', ?)",
                    (role, short, sid, "opencode", store.now()))
            seed.execute(
                "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                " created_at) VALUES ('R-001','做東西','做出來',?,?)",
                (store.now(), store.now()))
        seed.close()

        self.coordinator = engine.Coordinator(self.tmp)
        self.addCleanup(self.coordinator.close)
        self.conn = self.coordinator.conn

        # 宿主掃描：測試裡不要真的去敲宿主 CLI
        self._real_scan = platform.hostterm.scan_sessions
        platform.hostterm.scan_sessions = lambda cli="", timeout=20: []
        self.addCleanup(self._restore_scan)

        # compact 偵測：替身（不碰真的 transcript／API）
        self.detected = {"has_history": True, "last_compact": None}
        self._real_inspect = compact.inspect_session
        self.patch = mock.patch.object(
            compact, "inspect_session",
            side_effect=lambda *a, **k: dict(self.detected))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _restore_scan(self):
        platform.hostterm.scan_sessions = self._real_scan

    # ------------------------------------------------------------ 工具

    def nudge(self):
        report = {"dispatched": [], "errors": []}
        self.coordinator._nudge_compact(report)
        return report

    def compact_rows(self, session_id):
        return self.conn.execute(
            "SELECT * FROM outbox WHERE kind = 'compact' AND recipient_session = ?"
            " ORDER BY id", (session_id,)).fetchall()

    def make_work(self, state=workflow.BACKLOG, requirement_id="R-001"):
        work_id = self.coordinator.create_work("一張單", requirement_id, "內容", [])
        if state != workflow.BACKLOG:
            with store.tx(self.conn):
                self.conn.execute("UPDATE works SET state = ? WHERE id = ?",
                                  (state, work_id))
        return work_id

    def touch(self, session_id, when=None):
        """這個 session 最近有動作（journal 一筆）。"""
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO journal(ts, op, actor, session_id, target_id, detail)"
                " VALUES (?,?,?,?,?,?)",
                (when or store.now(), "test.touch", "human", session_id, "", "{}"))


class TestNudgeConditions(CompactHarness):

    def test_idle_session_gets_nudged(self):
        """沒有工作可做的 session → 收到收合提醒。"""
        report = self.nudge()
        sessions = [d["session"] for d in report["dispatched"]]
        self.assertIn(PLANNER, sessions, report)
        rows = self.compact_rows(PLANNER)
        self.assertEqual(len(rows), 1)
        self.assertIn("/compact", rows[0]["payload"])

    def test_nudge_uses_anchor_not_real_task_id(self):
        """**帶真 task_id 會造成假回執** → 用獨立錨點。"""
        report = self.nudge()
        anchor = [d["anchor"] for d in report["dispatched"]
                  if d["session"] == PLANNER][0]
        self.assertTrue(anchor.startswith("CP"), anchor)
        rows = self.compact_rows(PLANNER)
        self.assertEqual(rows[0]["task_id"], anchor)
        # **不能出現真的 task 編號**（T-000001 這種）——會被回執核對
        # 當成「這一棒已交件」→ 假回執。（"RECEIPT-PENDING" 裡的 "T-" 不算。）
        self.assertIsNone(re.search(r"T-\d{6}", rows[0]["payload"]),
                          rows[0]["payload"])

    def test_session_with_open_task_is_not_nudged(self):
        """有開著的 task → 有工作，不叫。"""
        work_id = self.make_work()
        with store.tx(self.conn):
            self.conn.execute(
                "INSERT INTO tasks(id, work_id, issue_id, milestone_id, stage,"
                " assigned_session, assigned_role, status, created_at, updated_at)"
                " VALUES ('T-000001',?,NULL,NULL,?,?,?, 'open', ?, ?)",
                (work_id, workflow.DESIGN, THINKER, "THINKER",
                 store.now(), store.now()))
        report = self.nudge()
        self.assertNotIn(THINKER, [d["session"] for d in report["dispatched"]],
                         "正在做事的 session 不該被叫")
        self.assertEqual(self.compact_rows(THINKER), [])

    def test_work_waiting_for_this_role_counts_as_work(self):
        """**看單據狀態**：等這個角色、還沒人做的單 → 它等一下就有事做 → 不叫。"""
        self.make_work(state=workflow.DESIGN)   # design 等 THINKER
        report = self.nudge()
        sessions = [d["session"] for d in report["dispatched"]]
        self.assertNotIn(THINKER, sessions,
                         "有等 THINKER 的單還沒人做 → 它不算閒置")
        self.assertIn(PLANNER, sessions)

    def test_recently_active_session_is_not_nudged(self):
        """閒置落定期：剛有動作的 session 不叫（派單／交件剛過不算閒）。"""
        self.touch(PLANNER)
        report = self.nudge()
        sessions = [d["session"] for d in report["dispatched"]]
        self.assertNotIn(PLANNER, sessions,
                         "剛有動作的 session 不該被叫去 compact")

    def test_session_without_history_is_not_nudged(self):
        """沒有對話紀錄的 session 沒東西可收合。"""
        self.detected = {"has_history": False, "last_compact": None}
        report = self.nudge()
        self.assertEqual(report["dispatched"], [])

    def test_compact_less_than_half_hour_ago_is_not_nudged(self):
        """距上一次 compact 不到半小時 → 不叫。"""
        self.detected = {"has_history": True,
                         "last_compact": store.now(time.time() - 10 * 60)}
        report = self.nudge()
        self.assertEqual(report["dispatched"], [])

    def test_compact_more_than_half_hour_ago_is_nudged(self):
        """距上一次 compact 超過半小時 → 叫。"""
        self.detected = {"has_history": True,
                         "last_compact": store.now(time.time() - 40 * 60)}
        report = self.nudge()
        self.assertIn(PLANNER, [d["session"] for d in report["dispatched"]], report)

    def test_ledger_is_used_when_detection_finds_nothing(self):
        """偵測不到 → 用引擎自己記的「上次要求」時間當保守值。"""
        self.detected = {"has_history": True, "last_compact": None}
        with store.tx(self.conn):
            compact.record(self.conn, PLANNER, store.now(time.time() - 5 * 60),
                           "requested")
        report = self.nudge()
        self.assertNotIn(PLANNER, [d["session"] for d in report["dispatched"]])

    def test_not_nudged_twice_within_half_hour(self):
        """叫過之後半小時內不再叫（避免一直轟炸）。"""
        first = self.nudge()
        self.assertIn(PLANNER, [d["session"] for d in first["dispatched"]])
        second = self.nudge()
        self.assertNotIn(PLANNER, [d["session"] for d in second["dispatched"]],
                         "剛叫過就要再叫 → 會一直轟炸 session")

    def test_nudge_is_recorded_in_the_ledger(self):
        self.nudge()
        row = self.conn.execute(
            "SELECT * FROM session_compacts WHERE session_id = ?",
            (PLANNER,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "requested")


class TestTickIntegration(CompactHarness):

    def test_tick_triggers_the_nudge(self):
        """整趟 tick 也會帶出收合提醒（常駐協調器靠這條）。"""
        report = self.coordinator.tick()
        notes = [d.get("note") for d in report.get("dispatched", [])]
        self.assertIn("compact", notes, report)

    def test_dispatched_session_is_not_nudged_in_the_same_tick(self):
        """這一趟才被派到單的 session，不會在這一趟被叫去 compact。"""
        work_id = self.make_work()
        self.coordinator.tick()
        # 派工後 THINKER 有一張開著的 task → 不該被收合提醒
        sessions = [d.get("session") for d in self.coordinator.tick()["dispatched"]
                    if d.get("note") == "compact"]
        self.assertNotIn(THINKER, sessions)
        self.assertEqual(self.compact_rows(THINKER), [])


class TestCompactModes(CompactHarness):
    """兩種收合方式：compact（壓縮）／reset（清空對話、session 不變）。

    **不提供「開新 session」**：新 session 會用預設值重新開始，
    使用者配給那個 session 的模型與設定會不見（使用者裁決 2026-11-25）。
    """

    def set_mode(self, mode):
        with store.tx(self.conn):
            compact.set_mode(self.conn, mode)

    def set_agent(self, role, agent):
        with store.tx(self.conn):
            self.conn.execute("UPDATE roles SET agent = ? WHERE role = ?",
                              (agent, role))

    def test_default_mode_is_reset(self):
        """**能 reset 就 reset**（使用者裁決 2026-11-25）→ 預設是 reset。"""
        self.assertEqual(compact.mode_of(self.conn), "reset")

    def test_set_mode_is_persisted(self):
        self.set_mode("compact")
        self.assertEqual(compact.mode_of(self.conn), "compact")

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            compact.set_mode(self.conn, "nope")

    def test_reset_mode_asks_the_host_to_clear(self):
        """reset → Claude Code 的 `/clear`（清空對話，session 不變）。"""
        self.set_mode("reset")
        self.set_agent("PLANNER", "claude")
        self.nudge()
        rows = self.compact_rows(PLANNER)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["payload"].startswith("/clear"), rows[0]["payload"])
        self.assertIn("fbflow_register", rows[0]["payload"],
                      "真的拿不到席位時要有重新認領的路")
        # **不能嚇唬人**：席位綁在終端上，/clear 不會改掉它——
        # 提示詞不能說「UUID 跟著對話一起沒了」（那是不實的）。
        self.assertNotIn("UUID 隨著對話一起沒了", rows[0]["payload"])
        self.assertIn("不會", rows[0]["payload"])

    def test_reset_does_not_release_the_seat(self):
        """reset 清掉對話，但**席位不能被釋放**——釋放就會變成「換 session」。"""
        self.set_mode("reset")
        self.set_agent("PLANNER", "claude")
        self.nudge()
        row = self.conn.execute(
            "SELECT session_id, status FROM roles WHERE role = 'PLANNER'").fetchone()
        self.assertEqual(row["session_id"], PLANNER)
        self.assertEqual(row["status"], "active")

    def test_reset_mode_falls_back_when_agent_has_no_clear(self):
        """這個 agent 沒有清空指令 → 退回壓縮，並且**講清楚**。"""
        self.set_mode("reset")
        self.set_agent("PLANNER", "opencode")
        self.nudge()
        rows = self.compact_rows(PLANNER)
        self.assertTrue(rows[0]["payload"].startswith("/compact"), rows[0]["payload"])
        self.assertIn("沒有", rows[0]["payload"])
        # **退化成壓縮時，文字不能還在叫它「清空對話」**——
        # 說法要跟它實際該做的動作一致。
        self.assertNotIn("請**清空目前的對話**", rows[0]["payload"])
        self.assertNotIn("重新認領一次席位", rows[0]["payload"])



class TestCompactModule(unittest.TestCase):

    def test_to_epoch_reads_engine_and_host_formats(self):
        self.assertIsNotNone(compact.to_epoch("2026-11-25T08:58:26"))
        self.assertIsNotNone(compact.to_epoch("2026-11-25T08:58:26.516Z"))
        self.assertIsNone(compact.to_epoch(""))

    def test_newest_picks_the_latest(self):
        latest = compact.newest("2026-11-25T08:00:00", "2026-11-25T09:00:00")
        self.assertIsNotNone(latest)
        self.assertIsNone(compact.newest(None, ""))

    def test_prompt_mentions_compact_command(self):
        text = compact.prompt_text("compact", "claude", "PLANNER", 30, "CP123")
        self.assertIn("/compact", text)
        self.assertIn("CP123", text)
        self.assertNotIn("task_id", text)

    def test_command_for_per_agent(self):
        self.assertEqual(compact.command_for("claude", "compact")[0], "/compact")
        self.assertEqual(compact.command_for("claude", "reset")[0], "/clear")
        # **沒有開新 session 這種模式**（會用掉 session 的模型設定）
        self.assertNotIn("new", compact.MODES)
        # 沒有清空指令時 → 退回壓縮，並且有說明
        command, note = compact.command_for("opencode", "reset")
        self.assertEqual(command, "/compact")
        self.assertTrue(note)


class TestCompactCLI(unittest.TestCase):
    """CLI 守門：`fbf.py compact mode/status` 要能在真的專案目錄跑。

    守的是「命令列那條路跟模組那條路一致」——
    模組測試綠了，但 CLI 還在查一張已經拿掉的表，整條路就整個壞掉。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-compact-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        conn = store.connect(self.tmp)
        store.init_db(conn)
        conn.close()
        self.fbf = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fbf.py")

    def run_cli(self, *args):
        import json
        import subprocess
        proc = subprocess.run(
            [sys.executable, self.fbf, "--project", self.tmp] + list(args),
            capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def test_compact_status_works(self):
        code, out, err = self.run_cli("compact", "status")
        self.assertEqual(code, 0, err or out)
        import json
        payload = json.loads(out)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["mode"], "reset")
        self.assertIn("claude", payload["commands"])
        self.assertNotIn("retired_sessions", payload)

    def test_compact_mode_switches_to_compact(self):
        code, out, err = self.run_cli("compact", "mode", "--mode", "compact")
        self.assertEqual(code, 0, err or out)
        import json
        self.assertEqual(json.loads(out)["mode"], "compact")
        code, out, err = self.run_cli("compact", "status")
        self.assertEqual(code, 0, err or out)
        import json
        self.assertEqual(json.loads(out)["mode"], "compact")


if __name__ == "__main__":
    unittest.main()
