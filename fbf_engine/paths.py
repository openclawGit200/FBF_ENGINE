"""FBF ENGINE 的**路徑**。

只有一件事：**MCP 自己的根目錄**在哪。

兩個「根」要分清楚：

| 名稱 | 意義 | 位置 |
|---|---|---|
| **MCP 根目錄** | 引擎安裝在哪 | `fbf_engine` 套件的上層目錄 |
| **專案根目錄** | 某個要推動工作流的專案，它的正本在哪 | **那個專案自己的目錄** |

**MCP 根目錄裡沒有任何跟專案有關的東西**——
沒有工單、沒有角色、沒有需求、沒有回執，也**沒有專案註冊表**。

安裝 MCP 只是把「引擎的入口」放進 OpenCode 的設定，
跟任何專案無關（規格第 4 節、第 11 節）。某個專案要用這個 MCP 推動工作流時，
它才**自己**加入這個 MCP（在自己的 `opencode.json` 裡指定 `FBF_PROJECT_ROOT`），
並且**自己**建立角色席位。

所以「交付目錄」永遠是**加入這個 MCP 的那個專案的目錄**，
不是 MCP 根目錄，也不是另外開一個專用目錄。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

#: MCP 伺服器的名稱（OpenCode 設定裡的 key）
SERVER_NAME = "fbf-engine"

#: 專案層設定要帶的環境變數：指向該專案自己的目錄
ENV_PROJECT_ROOT = "FBF_PROJECT_ROOT"


def mcp_root() -> str:
    """MCP 自己的根目錄（安裝位置）。**不是任何專案的根。**"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_under_mcp_root(path: str) -> bool:
    """這個路徑**是不是在** MCP 根目錄之內（**含根目錄本身與所有子目錄**）。

    **引擎的安裝目錄整棵樹都不放專案資料**——不只是根目錄本身，
    底下的子目錄也不行。那棵樹裡放的是引擎的程式與規格；
    放專案正本會讓「安裝狀態」跟「專案」綁在一起（規格第 4、11 節），
    而且 MCP 伺服器解析專案時會把安裝目錄認錯成專案根。

    兩邊都取 `realpath` 再比：macOS 的 `/tmp` 其實是 `/private/tmp` 的 symlink，
    不解析就會跟實際路徑比不相等。比較時用 `root + os.sep` 起頭，
    才不會把 `/引擎bc` 誤判成 `/引擎/bc` 的子目錄。
    """
    if not path:
        return False
    target = os.path.realpath(os.path.expanduser(path))
    root = os.path.realpath(mcp_root())
    if target == root:
        return True
    return target.startswith(root + os.sep)


def resolve_python_command(explicit: str = "", host_root: str = "") -> str:
    """設定檔裡要寫的 **Python 命令**。

    寫死 `python3` 會在沒有這個命令的機器上直接壞掉（Windows 通常只有
    `python`），所以：

    - **本機安裝**（沒有 `host_root`）→ 用**正在跑這個安裝器的直譯器**
      （`sys.executable`）。那個直譯器一定存在，而且版本一定對。
    - **替另一台主機產生設定**（有 `host_root`）→ 用**目標主機**的命令。
      本機的 `sys.executable` 在那台機器上不存在，所以**必須明確指定**——
      寧可拒絕，也不寫一份跑不起來的設定。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    if host_root:
        raise ValueError(
            "替另一台主機產生設定時，必須用 --python 指定**目標主機**的 Python 命令"
            "（本機的直譯器在那台機器上不存在）")
    executable = sys.executable
    if not executable:
        raise ValueError("拿不到目前 Python 的路徑，請用 --python 指定要寫進設定的命令")
    return executable


def server_entry(project_root: str = "", host_root: str = "",
                 python_command: str = "") -> dict:
    """OpenCode 設定裡的這個 MCP 設定物件（**v2 格式**：`mcp.servers`）。

    - **全域（安裝範圍）**：不給 `project_root` → 只有程式碼路徑，
      **不跟任何專案綁定**。
    - **專案層（加入範圍）**：給 `project_root` → 額外帶 `FBF_PROJECT_ROOT`，
      這個 MCP 連線就只服務那個專案。

    格式用 OpenCode v2 的 `mcp.servers`（v2.2.0 起）。
    `host_root` 見 `to_host_path`；`python_command` 見 `resolve_python_command`。
    """
    environment = {"PYTHONPATH": to_host_path(mcp_root(), host_root)}
    if project_root:
        environment[ENV_PROJECT_ROOT] = to_host_path(project_root, host_root)
    return {
        "type": "local",
        "command": [python_command or sys.executable, "-m", "fbf_engine.mcp_server"],
        "environment": environment,
        "disabled": False,
    }


def claude_entry(project_root: str = "", host_root: str = "",
                  python_command: str = "") -> dict:
    """Claude Code **專案層** `.mcp.json` 裡的 server 設定。

    格式是 Claude Code 的 `mcpServers`（command／args／env），
    與 OpenCode 的 `mcp.servers` **不同**——兩邊都要寫，缺一邊就有一邊的
    session 看不到工具（上一輪實測：`join` 只寫了 OpenCode 設定）。
    """
    environment = {"PYTHONPATH": to_host_path(mcp_root(), host_root)}
    if project_root:
        environment[ENV_PROJECT_ROOT] = to_host_path(project_root, host_root)
    return {
        "command": python_command or sys.executable,
        "args": ["-m", "fbf_engine.mcp_server"],
        "env": environment,
    }


def merge_claude(config_file: str, project_root: str = "",
                 host_root: str = "", python_command: str = "") -> str:
    """把這個 MCP 合併進專案的 `.mcp.json`（Claude Code 專案層 MCP）。

    合併不覆蓋別的 server。回傳實際寫入的路徑。
    """
    data = {}
    if os.path.isfile(config_file):
        try:
            with open(config_file, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = claude_entry(project_root, host_root, python_command)
    data["mcpServers"] = servers
    parent = os.path.dirname(config_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return config_file


def config_path(explicit: str = "") -> str:
    """OpenCode 設定檔路徑（全域）。"""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json")


def to_host_path(path: str, host_root: str = "") -> str:
    """把本機路徑轉成「**設定檔那台機器**」看得懂的絕對路徑。

    設定檔是給別台機器讀的（常見：本機寫、另一台跑），所以裡面的路徑必須是
    **對方**的形式。

    `host_root` 是「本機哪個前綴對應到對方的 `/`」。
    例：對方的根掛載在本機的 `<掛載點>` → `host_root="<掛載點>"` →
    `<掛載點>/專案/資料` → `/專案/資料`。

    **不給就不轉換**（本機自己讀自己的設定時就是對的）。
    """
    if not host_root:
        return os.path.abspath(path)
    local = os.path.abspath(path)
    root = os.path.abspath(host_root)
    try:
        relative = os.path.relpath(local, root)
    except ValueError:
        return local
    if relative.startswith(".."):
        return local
    return "/" + relative.replace("\\", "/")


def merge_server(config_file: str, project_root: str = "",
                 host_root: str = "", python_command: str = "") -> str:
    """把這個 MCP 寫進某個 OpenCode 設定檔（合併，不覆蓋別的設定）。

    - `project_root` 為空 → **全域安裝**（不跟任何專案綁定）。
    - 有給 → **某個專案加入這個 MCP**（帶 `FBF_PROJECT_ROOT`）。

    `host_root`：見 `to_host_path`；`python_command`：見 `resolve_python_command`。

    回傳實際寫入的設定檔路徑。
    """
    data = {}
    if os.path.isfile(config_file):
        try:
            with open(config_file, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
    servers = mcp.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = server_entry(project_root, host_root, python_command)
    mcp["servers"] = servers
    data["mcp"] = mcp
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return config_file
