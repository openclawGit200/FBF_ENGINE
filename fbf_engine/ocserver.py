"""OpenCode server 的**唯讀探索**——**引擎永遠不啟動 OpenCode server**。

這個模組存在的唯一理由，就是把「不要自己開 server」這件事變成程式保證，
而不是靠記得不要那樣做。

## 為什麼要這樣

上游 CLI 的行為（實測 v2.2.5 原始碼 `packages/cli/src/services/server-connection.ts`）：

- **帶 `--server <url>`** → 只做連線與健康檢查；連不上就報錯，**不會啟動任何東西**。
- **沒帶 `--server`** → `Service.ensure()` → **會自動啟動一個背景 server 並讓它常駐**。

也就是說：只要呼叫 `opencode api …` 沒帶 `--server`，
就等於在別人的機器上偷偷開了一個 OpenCode server（連帶它裡面的 MCP）。
那不是這個套件該做的事。

## 端點從哪來

OpenCode 自己會把「現在跑著的 server」的註冊檔寫出來
（實測 macOS：`~/.local/state/opencode/service.json`，欄位 `url`／`password`／`pid`／`version`）。
這裡**唯讀**取用它——不寫、不刪、不啟動任何東西。

取不到（OpenCode 沒在跑、或使用者把它關了）→
**明確報錯**，不要讓 CLI 去開一個。呼叫端各自決定怎麼處理：
presence 當成「查不到」、回執當成「未核對」、派工當成「確定失敗」。

## 密碼

上游讀 `OPENCODE_PASSWORD`（舊名 `OPENCODE_SERVER_PASSWORD` 仍支援）。
這裡兩個都設定，避免版本差異。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple


class ServerMissing(Exception):
    """找不到可連線的 OpenCode server。

    **這不是「平台離線」，是「沒有 server 可連」**——而且我們**不會自己開一個**。
    """


def _state_folders() -> List[str]:
    """OpenCode 可能放註冊檔的資料夾（依序）。"""
    home = os.path.expanduser("~")
    folders: List[str] = []
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        folders.append(os.path.join(xdg, "opencode"))
    folders.append(os.path.join(home, ".local", "state", "opencode"))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        folders.append(os.path.join(local, "opencode"))
    return folders


def registration_paths() -> List[str]:
    """註冊檔（server 位址 ＋ 密碼）的可能位置，依序。"""
    paths: List[str] = []
    for folder in _state_folders():
        for name in ("service.json", "service-local.json"):
            path = os.path.join(folder, name)
            if path not in paths:
                paths.append(path)
    return paths


def _read_json(path: str) -> Dict[str, Any]:
    """讀一個 JSON 檔；讀不到就回空 dict（**不拋例外**——探索不該弄壞流程）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def discover(explicit: str = "") -> Tuple[str, str]:
    """找出既有 OpenCode server 的 `(url, password)`；找不到就回 `("", "")`。

    **唯讀**：只讀 OpenCode 自己寫的註冊檔，絕不啟動任何東西。

    優先序：明確指定的 endpoint → `FBF_OPENCODE_SERVER` 環境變數 → 註冊檔。
    """
    url = (explicit or os.environ.get("FBF_OPENCODE_SERVER", "")).strip()
    password = (os.environ.get("OPENCODE_PASSWORD", "")
                or os.environ.get("OPENCODE_SERVER_PASSWORD", "")).strip()

    for path in registration_paths():
        info = _read_json(path)
        if not url:
            url = str(info.get("url") or "").strip()
            if not url:
                # 設定檔形式：使用者自己指定了 hostname/port。
                hostname = str(info.get("hostname") or "").strip()
                port = info.get("port")
                if hostname and port:
                    url = "http://%s:%d" % (hostname, int(port))
        if not password:
            password = str(info.get("password") or "").strip()
        if url and password:
            break
    return url, password


def require(explicit: str = "") -> Tuple[str, str]:
    """取得 `(url, password)`；找不到就丟 `ServerMissing`。

    **不要在這裡「想辦法找一個 server」**——要嘛用既有的，
    要嘛讓呼叫端知道「現在沒有 server 可連」。
    """
    url, password = discover(explicit)
    if not url:
        where = "、".join(registration_paths()[:2]) or "OpenCode 的 state 目錄"
        raise ServerMissing(
            "找不到可連線的 OpenCode server，而且這個引擎**不會自己開一個**。"
            "請先讓 OpenCode 跑起來（它會把位址寫在 %s），"
            "或用 --endpoint／FBF_OPENCODE_SERVER 指定位址。" % where)
    return url, password


def cli_argv(binary: str, endpoint: str, args: List[str]) -> List[str]:
    """組 CLI 命令：**一定**帶 `--server`。

    帶了 `--server`，上游 CLI 就只會連線——**不會啟動 server** ✓。
    """
    return [binary, "--server", endpoint] + list(args)


def env_for(password: str) -> Dict[str, str]:
    """給 CLI 子行程用的環境變數（含密碼；兩個變數名都給，避開版本差異）。"""
    env = dict(os.environ)
    if password:
        env["OPENCODE_PASSWORD"] = password
        env["OPENCODE_SERVER_PASSWORD"] = password
    return env
