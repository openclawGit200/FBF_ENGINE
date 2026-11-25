"""**專案加入**：讓一個新專案自己加入這個 FBF ENGINE。

兩個動作：

1. `join_project(root, ...)`——建立正本（`fbf_engine.db`）、寫專案代碼、
   建三個角色席位，並把這個 MCP 寫進該專案的設定檔（帶 `FBF_PROJECT_ROOT`）。
   **冪等**：已經是專案就只補缺的那一份，不會洗掉既有資料。
2. `bind_session(session_id, root)`／`session_root(session_id)`——記住
   「這個 session 屬於哪個專案」，讓**當下這條 MCP 連線**立刻服務那個專案。

CLI（`fbf.py join`）與 MCP 工具（`fbflow_project_join`）走**同一份**邏輯，
不會有兩套「加入」的定義。

## session→專案的綁定放在哪

放在**全域狀態檔**（家目錄下的 `.fbflow` 資料夾），**不放 MCP 安裝目錄**——
安裝目錄整棵樹都不放專案資料（見 `paths.py`），那是這套系統的硬規則。
這張表記的是「這個 session 現在服務哪個專案」，是執行狀態，不是專案正本；
正本仍然只在**那個專案自己的目錄裡**。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import paths, store

#: 專案代碼用的字元（去掉容易看錯的 0／1／I／O）。
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: 新專案預設建立的三個角色席位。
DEFAULT_SEATS: Tuple[Tuple[str, str], ...] = (
    ("PLANNER", "P1"),
    ("THINKER", "T1"),
    ("WORKER", "W1"),
)

#: session→專案的綁定檔（全域狀態，**不是**專案正本）。
SESSION_BINDINGS_FILE = "session_projects.json"


class JoinError(Exception):
    """加入失敗。`code` 是穩定代碼，`message` 是人可讀說明。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 綁定

def global_state_dir() -> str:
    """全域狀態資料夾（家目錄下的 `.fbflow`）。"""
    return os.path.join(os.path.expanduser("~"), ".fbflow")


def session_bindings_path() -> str:
    """session→專案的綁定檔路徑。"""
    return os.path.join(global_state_dir(), SESSION_BINDINGS_FILE)


def _load_bindings() -> Dict[str, str]:
    """讀綁定表。讀不到或壞掉 → 當作空的（不因為壞掉就讓整個引擎不能用）。"""
    path = session_bindings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()}


def _save_bindings(data: Dict[str, str]) -> None:
    path = session_bindings_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def bind_session(session_id: str, root: str) -> str:
    """把「這個 session 服務哪個專案」記下來。回傳綁定的專案根。

    冪等：同一個 session 再綁一次，就是改指向新的專案。
    """
    if not session_id:
        return ""
    root = os.path.abspath(os.path.expanduser(root))
    data = _load_bindings()
    data[session_id] = root
    _save_bindings(data)
    return root


def session_root(session_id: str) -> str:
    """這個 session 綁的專案根；沒綁過 → 空字串。"""
    if not session_id:
        return ""
    return _load_bindings().get(session_id, "")


def unbind_session(session_id: str) -> None:
    """解除綁定（專案被刪掉或 session 換工單時用）。"""
    if not session_id:
        return
    data = _load_bindings()
    if data.pop(session_id, None) is not None:
        _save_bindings(data)


# ---------------------------------------------------------------- 加入

def generate_code(conn: sqlite3.Connection) -> str:
    """產生一個沒被用過的專案代碼。"""
    for _ in range(50):
        suffix = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        code = "PRJ-%s" % suffix
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'project_code'").fetchone()
        if row is None or row["value"] != code:
            return code
    raise JoinError("project.code-failed", "產生專案代碼失敗")


def resolve_join_target(cwd: str, requested: str = "") -> str:
    """決定要加入的專案根，並擋掉不該加入的位置。

    規則（都是為了讓「session 不能亂指一個地方就把正本寫進去」）：

    - **引擎的安裝目錄（含子目錄）不行**——那裡不放專案資料。
    - **只能加入目前工作目錄，或它的子目錄**——session 宣告的是
      「我現在就在這個專案裡工作」。不給路徑就用目前工作目錄。
    """
    base = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    root = os.path.abspath(os.path.expanduser(requested)) if requested else base
    if paths.is_under_mcp_root(root):
        raise JoinError("project.root-is-mcp",
                        "引擎的安裝目錄不存放專案資料（含其子目錄），"
                        "不能在這裡建立正本：%s" % root)
    if root != base and not root.startswith(base + os.sep):
        raise JoinError("project.root-outside-cwd",
                        "只能加入目前工作目錄或它的子目錄（現在是 %s）。"
                        "請先 cd 到那個專案再呼叫。" % base)
    return root


def join_project(root: str, name: str = "", endpoint: str = "",
                 python_command: str = "", write_config: bool = True,
                 seats: Optional[List[Tuple[str, str]]] = None,
                 agent: str = "opencode", host_root: str = "") -> Dict[str, Any]:
    """讓一個目錄成為 FBF 專案，並把這個 MCP 寫進它的設定檔。

    **冪等**：已經是專案 → 不重建資料庫、不改專案代碼，只補缺的角色席位
    與設定檔。

    `host_root`：見 `paths.to_host_path`（設定檔在別台機器讀時的路徑轉換）。

    回傳：專案根、專案代碼、正本路徑、新建的席位、寫進去的設定檔。
    """
    root = os.path.abspath(os.path.expanduser(root))
    if paths.is_under_mcp_root(root):
        raise JoinError("project.root-is-mcp",
                        "引擎的安裝目錄不存放專案資料（含其子目錄），"
                        "不能在這裡建立正本：%s" % root)
    os.makedirs(root, exist_ok=True)
    existed = os.path.isfile(store.db_path(root))

    conn = store.connect(root)
    try:
        store.init_db(conn)
        code = store.get_setting(conn, "project_code", "")
        if not code:
            code = generate_code(conn)
        with store.tx(conn):
            store.set_setting(conn, "project_code", code)
            # **不覆蓋既有的專案名**——再次 join 是冪等的，不是重新初始化。
            if name or not store.get_setting(conn, "name", ""):
                store.set_setting(conn, "name", name or os.path.basename(root))
            if endpoint:
                store.set_setting(conn, "opencode_endpoint", endpoint)
        created: List[str] = []
        for role, short in (seats or list(DEFAULT_SEATS)):
            with store.tx(conn):
                row = conn.execute(
                    "SELECT session_id FROM roles WHERE role = ? AND short_name = ?",
                    (role, short)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO roles(role, short_name, session_id, agent, status,"
                        " project_code, workdir, created_at)"
                        " VALUES (?,?,NULL,?, 'active', ?, '', ?)",
                        (role, short, agent, code, store.now()))
                    created.append("%s／%s" % (role, short))
        store.journal(conn, "project.join", code,
                       detail={"root": root, "created": not existed})
        effective_endpoint = store.get_setting(conn, "opencode_endpoint", "")
    finally:
        conn.close()

    config = ""
    claude_config = ""
    if write_config:
        command = python_command or sys.executable
        config = paths.merge_server(os.path.join(root, "opencode.json"),
                                    project_root=root, host_root=host_root,
                                    python_command=command)
        claude_config = paths.merge_claude(os.path.join(root, ".mcp.json"),
                                           project_root=root, host_root=host_root,
                                           python_command=command)

    return {"project_root": root, "project_code": code,
            "db": store.db_path(root), "created": not existed,
            "roles_created": created, "config": config,
            "claude_config": claude_config,
            "opencode_endpoint": effective_endpoint}
