"""**新專案自己加入這個引擎**——守門測試。

守五件事：

1. `fbflow_project_join` 真的把一個目錄變成 FBF 專案：正本、三個角色席位、
   專案層 MCP 設定檔。
2. **冪等**：再 join 一次不會洗掉既有資料（專案代碼、工單都不變）。
3. **護欄**：引擎的安裝目錄拒絕；目前工作目錄以外的路徑拒絕。
4. join 之後**當下這條 MCP 連線**就服務那個專案（session 綁定），
   而且綁定是**按 session 分開**的——一個 session 加入，不代表別人的也加入。
5. 還沒綁專案時，錯誤訊息要**告訴人（與 session）下一步怎麼走**。

**測試不碰真的全域狀態檔**：session 綁定檔的位置在測試裡被指到暫存目錄。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import mcp_server, paths, project, store  # noqa: E402

SESSION = "ses_join_test"


class JoinHarness(unittest.TestCase):
    """一個暫存的工作目錄 ＋ 一個沒有綁專案的 MCP 伺服器。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fbf-join-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)

        # session 綁定檔 → 暫存目錄（不碰真的全域狀態）
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        patch = mock.patch.object(project, "global_state_dir", return_value=state)
        patch.start()
        self.addCleanup(patch.stop)

        # 這個連線**沒有**綁任何專案（那是本檔要驗的情境）
        self._old_root = os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        self.addCleanup(self._restore_env)

        self.server = mcp_server.MCPServer(resident=False)
        self.addCleanup(self.server.close)

    def _restore_env(self):
        if self._old_root is None:
            os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        else:
            os.environ[mcp_server.ENV_PROJECT_ROOT] = self._old_root

    def join(self, args=None, session=SESSION, cwd=None):
        """呼叫 fbflow_project_join（工作目錄預設是暫存的工作資料夾）。"""
        params = {"_meta": {mcp_server.META_SESSION_KEY: session}}
        with mock.patch.object(os, "getcwd", return_value=cwd or self.work):
            return self.server.call_tool("fbflow_project_join", args or {}, params)


class TestJoinCreatesProject(JoinHarness):

    def test_join_creates_database_and_seats(self):
        result = self.join()
        self.assertTrue(result.get("ok"), result)
        data = result["data"]
        self.assertTrue(os.path.isfile(data["db"]), "應該建立正本：%s" % data["db"])
        conn = store.connect(self.work)
        try:
            roles = conn.execute(
                "SELECT role, short_name FROM roles ORDER BY role").fetchall()
        finally:
            conn.close()
        self.assertEqual([(r["role"], r["short_name"]) for r in roles],
                         [("PLANNER", "P1"), ("THINKER", "T1"), ("WORKER", "W1")])

    def test_join_writes_project_scoped_mcp_settings(self):
        result = self.join()
        self.assertTrue(result.get("ok"), result)
        targets = (("opencode.json", ("mcp", "servers")),
                   (".mcp.json", ("mcpServers",)))
        for name, keys in targets:
            path = os.path.join(self.work, name)
            self.assertTrue(os.path.isfile(path), "應該寫出 %s" % name)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            node = data
            for key in keys:
                node = node[key]
            server = node["fbf-engine"]
            env = server.get("environment") or server.get("env") or {}
            self.assertEqual(
                os.path.realpath(env["FBF_PROJECT_ROOT"]),
                os.path.realpath(self.work),
                "%s 必須指向這個專案" % name)

    def test_join_binds_this_session_to_the_project(self):
        result = self.join()
        self.assertTrue(result.get("ok"), result)
        root, problem = self.server.resolve_project("", SESSION)
        self.assertIsNone(problem)
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.work))

    def test_other_tools_work_right_after_join(self):
        """join 之後**不用重開連線**，其他工具立刻能用。"""
        self.assertTrue(self.join().get("ok"))
        params = {"_meta": {mcp_server.META_SESSION_KEY: SESSION}}
        status = self.server.call_tool("fbflow_status", {}, params)
        self.assertTrue(status.get("ok"), status)

    def test_join_is_idempotent(self):
        first = self.join()
        self.assertTrue(first.get("ok"), first)
        code = first["data"]["project_code"]
        # 改掉專案名，證明第二次 join 不得把它洗回目錄名
        conn = store.connect(self.work)
        try:
            with store.tx(conn):
                store.set_setting(conn, "name", "原本的名字")
        finally:
            conn.close()

        second = self.join()
        self.assertTrue(second.get("ok"), second)
        self.assertEqual(second["data"]["project_code"], code,
                         "再次 join 不得改掉專案代碼")
        self.assertFalse(second["data"]["created"],
                         "再次 join 不該說自己建立了新專案")
        conn = store.connect(self.work)
        try:
            name = store.get_setting(conn, "name", "")
            seats = conn.execute("SELECT COUNT(*) AS n FROM roles").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(name, "原本的名字", "再次 join 把既有資料洗掉了")
        self.assertEqual(seats, 3, "再次 join 建立了重複的席位")


class TestJoinGuards(JoinHarness):

    def test_join_refuses_the_engine_install_directory(self):
        """引擎的安裝目錄不存放專案資料（含其子目錄）。"""
        engine = os.path.join(self.tmp, "engine")
        os.makedirs(engine)
        with mock.patch.object(paths, "mcp_root", return_value=engine):
            result = self.join(cwd=engine)
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], "project.root-is-mcp", result)
        self.assertFalse(os.path.exists(os.path.join(engine, store.DB_FILENAME)),
                         "被拒了就不該留下正本")

    def test_join_refuses_a_path_outside_the_working_directory(self):
        """session 只能宣告「我現在就在這個專案裡工作」。"""
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        result = self.join({"project_root": elsewhere})
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], "project.root-outside-cwd", result)
        self.assertFalse(os.path.exists(os.path.join(elsewhere, store.DB_FILENAME)),
                         "被拒了就不該留下正本")

    def test_join_accepts_a_subdirectory_of_the_working_directory(self):
        sub = os.path.join(self.work, "sub")
        os.makedirs(sub)
        result = self.join({"project_root": sub})
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(os.path.isfile(os.path.join(sub, store.DB_FILENAME)))

    def test_unknown_field_is_refused(self):
        result = self.join({"nope": "1"})
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], mcp_server.CODE_UNKNOWN_FIELD, result)


class TestSessionBinding(JoinHarness):

    def test_binding_is_per_session(self):
        """一個 session 加入，不代表另一個 session 也加入。"""
        self.join(session="ses_a")
        root_a, _ = self.server.resolve_project("", "ses_a")
        self.assertIsNotNone(root_a)
        root_b, problem = self.server.resolve_project("", "ses_b")
        self.assertIsNone(root_b)
        self.assertIsNotNone(problem, "別的 session 不該被順手綁到")

    def test_binding_survives_a_new_server_instance(self):
        """綁定存在檔案裡——**重啟 MCP 也要記得**（設計要求重啟不丟狀態）。"""
        self.join(session="ses_keep")
        fresh = mcp_server.MCPServer(resident=False)
        self.addCleanup(fresh.close)
        root, problem = fresh.resolve_project("", "ses_keep")
        self.assertIsNone(problem)
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.work))

    def test_bindings_are_not_stored_in_the_engine_directory(self):
        """綁定是全域執行狀態——**不得寫進引擎的安裝目錄**。"""
        self.join()
        path = project.session_bindings_path()
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(paths.is_under_mcp_root(path),
                         "綁定檔不該落在引擎安裝目錄底下")
        self.assertTrue(os.path.isfile(path))

    def test_unbound_error_says_how_to_join(self):
        """還沒綁專案時，錯誤訊息要**指路**，不是只說「不行」。"""
        root, problem = self.server.resolve_project("", "ses_nobody")
        self.assertIsNone(root)
        self.assertIsNotNone(problem)
        self.assertEqual(problem["code"], mcp_server.CODE_PROJECT_UNRESOLVED)
        self.assertIn("fbflow_project_join", problem["message"],
                      "錯誤訊息要告訴 session 怎麼加入")


class TestCliAndMcpShareOneJoin(JoinHarness):
    """CLI 與 MCP 走同一份「加入」邏輯。"""

    def test_cli_join_and_mcp_join_agree(self):
        import argparse
        import contextlib
        import io

        import fbf

        cli_root = os.path.join(self.tmp, "cli-project")
        os.makedirs(cli_root)
        args = argparse.Namespace(root=cli_root, name="", endpoint="",
                                  host_root="", python="")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = fbf.cmd_join(args)
        self.assertEqual(code, 0, stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload.get("ok"), payload)

        # 同一個目錄再用 MCP 工具 join → 冪等，代碼不變
        result = self.join({"project_root": cli_root}, cwd=cli_root)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["data"]["project_code"], payload["project_code"])
        self.assertFalse(result["data"]["created"])


if __name__ == "__main__":
    unittest.main()
