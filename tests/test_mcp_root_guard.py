"""**MCP 根目錄不記任何專案**——三道閘門的守門測試。

規格第 4 節、第 11 節：安裝 MCP 只是把引擎的入口放進設定，
跟任何專案無關。所以引擎的**安裝目錄**永遠不能變成一個專案根。

這個檔案守三件事：

1. `store.connect()` 拒絕 MCP 根目錄 → **正本不會建立在安裝目錄**。
   （曾經在引擎目錄裡跑過一次指令，就留下一個 `fbf_engine.db`；
    那個檔案會讓 MCP 伺服器把安裝目錄認錯成專案。）
2. `MCPServer.resolve_project()` 不把 MCP 根目錄當專案 → 無論用
   `FBF_PROJECT_ROOT` 還是啟動目錄都一樣。
3. 真實的 MCP 根目錄底下**沒有**專案正本。

**測試不污染真實的 MCP 根目錄**：前兩項把 `paths.mcp_root()`
指到暫存目錄去測；第三項才看真實目錄。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbf_engine import mcp_server, paths, store  # noqa: E402


class TestStoreRefusesMcpRoot(unittest.TestCase):
    """第 1 道：正本不會建立在 MCP 安裝目錄。"""

    def test_connect_refuses_mcp_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = os.path.join(tmp, "engine")
            os.makedirs(fake_root)
            with mock.patch.object(paths, "mcp_root", return_value=fake_root):
                with self.assertRaises(store.McpRootProject):
                    store.connect(fake_root)
                # **連檔案都不該被建立**
                self.assertFalse(
                    os.path.exists(os.path.join(fake_root, store.DB_FILENAME)),
                    "拒絕了也不該留下正本檔")

    def test_connect_refuses_subdirectory_of_mcp_root(self):
        """**安裝目錄整棵樹都不放專案資料**——子目錄也不行。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = os.path.join(tmp, "engine")
            os.makedirs(fake_root)
            with mock.patch.object(paths, "mcp_root", return_value=fake_root):
                inside = os.path.join(fake_root, "projects", "demo")
                with self.assertRaises(store.McpRootProject):
                    store.connect(inside)
                self.assertFalse(
                    os.path.exists(os.path.join(inside, store.DB_FILENAME)),
                    "安裝目錄的子目錄不該有正本")

    def test_sibling_with_shared_prefix_is_not_blocked(self):
        """`/引擎bc` 不是 `/引擎/bc` 的子目錄 → **不該被擋**。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = os.path.join(tmp, "engine")
            os.makedirs(fake_root)
            sibling = os.path.join(tmp, "engine-demo")
            os.makedirs(sibling)
            with mock.patch.object(paths, "mcp_root", return_value=fake_root):
                conn = store.connect(sibling)
                store.init_db(conn)
                conn.close()
            self.assertTrue(
                os.path.exists(os.path.join(sibling, store.DB_FILENAME)))

    def test_connect_still_allows_normal_project(self):
        """閘門只擋安裝目錄，普通專案照常開。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = os.path.join(tmp, "engine")
            os.makedirs(fake_root)
            with mock.patch.object(paths, "mcp_root", return_value=fake_root):
                project = os.path.join(tmp, "project")
                conn = store.connect(project)
                store.init_db(conn)
                conn.close()
            self.assertTrue(
                os.path.exists(os.path.join(project, store.DB_FILENAME)))


class TestResolveProjectIgnoresMcpRoot(unittest.TestCase):
    """第 2 道：解析專案根時，MCP 根目錄一律不算專案。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_root = os.path.join(self.tmp, "engine")
        os.makedirs(self.fake_root)
        # 放一份正本在安裝目錄裡：**正是那個會造成誤認的檔案**
        with open(os.path.join(self.fake_root, store.DB_FILENAME), "wb") as fh:
            fh.write(b"")
        self.patch = mock.patch.object(paths, "mcp_root",
                                       return_value=self.fake_root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))

    def test_cwd_at_mcp_root_is_not_a_project(self):
        """啟動目錄就是安裝目錄 → **不認**（即使那裡有正本檔）。"""
        os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        with mock.patch.object(os, "getcwd", return_value=self.fake_root):
            root, error = mcp_server.MCPServer().resolve_project()
        self.assertIsNone(root)
        self.assertIsNotNone(error)
        self.assertFalse(error.get("ok", True))

    def test_env_pointing_at_mcp_root_is_refused(self):
        """`FBF_PROJECT_ROOT` 指向安裝目錄 → **不認**。"""
        os.environ[mcp_server.ENV_PROJECT_ROOT] = self.fake_root
        self.addCleanup(os.environ.pop, mcp_server.ENV_PROJECT_ROOT, None)
        root, error = mcp_server.MCPServer().resolve_project()
        self.assertIsNone(root)
        self.assertIsNotNone(error)
        self.assertFalse(error.get("ok", True))

    def test_coordinator_is_not_created_for_mcp_root(self):
        """不替安裝目錄開協調器（那等於承認它是專案）。"""
        server = mcp_server.MCPServer(resident=False)
        self.assertIsNone(server.coordinator_for(self.fake_root))

    def test_subdirectory_of_mcp_root_is_not_a_project(self):
        """啟動目錄在安裝目錄底下 → **不認**。

        底下刻意放一份正本：沒有這份檔，「啟動目錄必須有正本」
        這個條件本來就會擋掉，測不出防護有沒有生效。
        """
        inside = os.path.join(self.fake_root, "projects", "demo")
        os.makedirs(inside)
        with open(os.path.join(inside, store.DB_FILENAME), "wb") as fh:
            fh.write(b"")
        os.environ.pop(mcp_server.ENV_PROJECT_ROOT, None)
        with mock.patch.object(os, "getcwd", return_value=inside):
            root, error = mcp_server.MCPServer().resolve_project()
        self.assertIsNone(root)
        self.assertIsNotNone(error)
        self.assertFalse(error.get("ok", True))


class TestRealMcpRootHasNoProject(unittest.TestCase):
    """第 3 道：真實的安裝目錄底下沒有專案正本。"""

    def test_no_project_database_in_real_mcp_root(self):
        path = os.path.join(paths.mcp_root(), store.DB_FILENAME)
        self.assertFalse(
            os.path.exists(path),
            "MCP 根目錄不該有專案正本：%s"
            "（這是殘留檔，會讓 MCP 伺服器把安裝目錄認成專案）" % path)

    def test_no_project_database_anywhere_under_real_mcp_root(self):
        """**整棵樹**都不該有專案正本（含子目錄）。"""
        root = paths.mcp_root()
        found = []
        for dirpath, _dirnames, filenames in os.walk(root):
            if "__pycache__" in dirpath or ".pytest_cache" in dirpath:
                continue
            for name in filenames:
                if name == store.DB_FILENAME or name.startswith(
                        store.DB_FILENAME + "-"):
                    found.append(os.path.join(dirpath, name))
        self.assertEqual(found, [],
                         "安裝目錄底下不該有專案正本：%s" % found)


class TestJoinRefusesInstallingDirectory(unittest.TestCase):
    """**加入的專案**也不能是安裝目錄（或它的子目錄）。

    「未來也不存放加入的專案，所有資料都跑在專案自己的資料夾」——
    `join` 是「建立正本 ＋ 寫 MCP 設定」的那道指令，必須擋在門外。
    """

    def test_join_into_mcp_root_is_refused(self):
        import argparse
        import contextlib
        import io
        import json
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import fbf

        with tempfile.TemporaryDirectory() as tmp:
            fake_root = os.path.join(tmp, "engine")
            os.makedirs(fake_root)
            target = os.path.join(fake_root, "projects", "demo")
            with mock.patch.object(paths, "mcp_root", return_value=fake_root):
                args = argparse.Namespace(
                    root=target, name="", endpoint="", host_root="", python="")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = fbf.cmd_join(args)
            self.assertNotEqual(code, 0, "join 進安裝目錄必須被拒")
            # 必須是**明確的拒絕**，不是碰巧丟例外。
            payload = json.loads(stderr.getvalue())
            self.assertEqual(payload.get("code"), "project.root-is-mcp", payload)
            self.assertFalse(
                os.path.exists(os.path.join(target, store.DB_FILENAME)),
                "被拒了就不該留下正本")
            self.assertFalse(os.path.exists(os.path.join(target, "opencode.json")),
                             "被拒了就不該寫 MCP 設定")


if __name__ == "__main__":
    unittest.main()
