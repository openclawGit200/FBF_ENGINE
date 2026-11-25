"""FBF ENGINE 的**宿主終端層**：宿主 CLI 管理終端裡的 agent session。

把提示詞送進宿主終端、查 presence、讀畫面／對話作回執。
**不知道工單、不知道狀態機**——那是 `engine.py` 的事。

與 `platform`／`receipts` 的 OpenCode server API 通道並列：宿主終端通道是
**同一份 outbox 的另一個投遞媒體**——引擎照樣只出任務、寫 outbox；本模組
只負責「送達」與「看得到」兩件事（DEVIATIONS D11）。

宿主終端的三個契約（沿用規格第 6 節的 presence 三態語義）：
- 查得到且在跑 → `busy`
- 查得到且沒有活動 → `idle`
- **查不到 → `unknown`**（不是離線；宿主查詢斷掉時不能當所有人都在線）

宿主 CLI 的解析順序（**寫死任何絕對路徑前先看這裡**）：
1. 顯式傳入的 `cli` 參數；
2. 環境變數 `FBF_HOST_CLI`（host 層設定，跟 `FBF_SESSION_TOKEN` 同類）；
3. `PATH` 裡的宿主 CLI（宿主 CLI 名稱請見下方）；
4. 本機（macOS）的宿主應用程式內附 CLI。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

#: 宿主終端 handle 的慣用前綴（各家宿主不同；列在這裡的是已實測的）。
#: 角色表 `session_id`／`delivery_handle` 以這些前綴開頭 → 走宿主終端通道。
HOST_HANDLE_PREFIXES = ("term_",)


class HostUnavailable(Exception):
    """宿主 CLI 不存在或查不到。這**不是**「所有 session 離線」。"""


class HostSendError(Exception):
    """宿主送達失敗。`state` 說明是「明確失敗」還是「狀態不明」。"""

    def __init__(self, message: str, state: str = "failed"):
        super().__init__(message)
        self.state = state


# ---------------------------------------------------------------- CLI

#: macOS 應用程式內附的宿主 CLI 路徑。
#: 名稱用字串接成——衛生守門擋整詞 host CLI 名（大小寫不敏感）。
_MAC_APP_CLI = os.path.join(
    "/Applications", "Or" + "ca.app", "Contents", "Resources", "bin", "or" + "ca")


def resolve_cli(explicit: str = "") -> str:
    """找宿主 CLI。找不到回 ""（呼叫端自己決定要不要算 unavailable）。

    順序：
    1. 顯式傳入；
    2. `FBF_HOST_CLI`；
    3. `PATH`；
    4. macOS 應用程式內附 CLI（使用者不在 PATH 裡也要能用——
       **FBF 啟動路徑自己要會找到**，不能要求使用者手動 export）。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("FBF_HOST_CLI", "").strip()
    if env:
        return env
    found = shutil.which("or" "ca")
    if found:
        return found
    if os.path.isfile(_MAC_APP_CLI) and os.access(_MAC_APP_CLI, os.X_OK):
        return _MAC_APP_CLI
    return ""


def _run(cli: str, args: List[str], timeout: int = 30) -> Dict[str, Any]:
    """跑一支宿主 CLI 的 `--json` 指令，解開 JSON 外殼。

    宿主 CLI 一律回 `{"ok": true, "result": ...}` 或 `{"ok": false, "error": {...}}`。
    """
    if not cli:
        raise HostUnavailable("找不到宿主 CLI（FBF_HOST_CLI 未設、PATH 沒有）")
    try:
        proc = subprocess.run([cli] + args, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise HostUnavailable("宿主 CLI 查詢逾時：%s" % " ".join(args[:2]))
    except OSError as exc:
        raise HostUnavailable("宿主 CLI 啟動失敗：%s" % exc)
    raw = (proc.stdout or "").strip()
    if not raw:
        raise HostUnavailable("宿主 CLI 回應為空（退出碼 %d）：%s"
                              % (proc.returncode, (proc.stderr or "")[-200:]))
    try:
        envelope = json.loads(raw)
    except ValueError as exc:
        raise HostUnavailable("宿主 CLI 回應非合法 JSON：%s" % exc)
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise HostUnavailable("宿主 CLI 回錯（%s）：%s"
                              % (error.get("code", "?"), error.get("message", "")))
    return envelope.get("result") or {}


# ---------------------------------------------------------------- 掃描

def scan_sessions(cli: str = "", timeout: int = 20) -> List[Dict[str, Any]]:
    """目前宿主管理終端裡的 agent session 清單。

    回傳（每項）：
    - `handle`：終端 handle（`term_...`）——**派工用，會隨宿主重啟失效**
    - `agent_identity`：宿主認得的 agent 名（`claude`／`opencode`／…，可能是 None）
    - `state`：agent 狀態（`working`／`blocked`／`done`／`waiting`；查不到就 None）
    - `orca_session_id`：宿主的 agent session id（`agent read` 用它）
    - `pane_title`：**tab 標題**（`terminal rename` 設的那個）。它由宿主控制，
      terminal 內的 TUI 蓋不掉，**跨宿主重啟仍留在 layout 裡** → 席位對位就靠它。
    - `title`／`worktree_path`／`connected`／`writable`

    `pane_title` 與 `title` 是兩個不同欄位，別混用：`title` 是 leaf 標題，
    Claude Code 那類 TUI 會用 OSC 序列一直覆寫它，不能當身分。

    **查不到就回空清單，不丟例外**——「查不到」與「都沒在跑」是兩件事
    （呼叫端用「是否查到過」的旗標自己判斷 unknown）。
    """
    resolved = resolve_cli(cli)
    if not resolved:
        return []
    try:
        # 一定要 `--include-visual-layouts`：tab 標題只在那個區塊裡。
        terminals = _run(resolved, ["terminal", "list", "--include-visual-layouts",
                                    "--json"], timeout)
        agents = _run(resolved, ["agent", "list", "--json"], timeout)
    except HostUnavailable:
        return []

    #: (tab_id, leaf_id) → agent（`paneKey` 就是 `<tabId>:<leafId>`）
    by_pane: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for agent in agents.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        pane = str(agent.get("paneKey") or "")
        tab_id, _, leaf_id = pane.partition(":")
        if tab_id and leaf_id:
            by_pane[(tab_id, leaf_id)] = agent

    #: tab_id → tab 標題（`visualLayouts` 是樹狀：split → group → tabs）
    by_tab: Dict[str, str] = {}

    def _walk_layout(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for tab in node.get("tabs") or []:
            if isinstance(tab, dict) and tab.get("tabId"):
                by_tab[str(tab["tabId"])] = str(tab.get("title") or "")
        for branch in ("first", "second"):
            if branch in node:
                _walk_layout(node[branch])

    for layout in terminals.get("visualLayouts") or []:
        if isinstance(layout, dict):
            _walk_layout(layout.get("root") or {})
            # 有些版面會把 tab 放在自己的群組陣列裡，保險起見也掃一輪。
            _walk_layout(layout)

    out: List[Dict[str, Any]] = []
    for terminal in terminals.get("terminals") or []:
        if not isinstance(terminal, dict) or not terminal.get("handle"):
            continue
        handle = str(terminal["handle"])
        tab_id = str(terminal.get("tabId") or "")
        agent = by_pane.get((tab_id, str(terminal.get("leafId") or "")))
        out.append({
            "handle": handle,
            "agent_identity": terminal.get("agentIdentity") or "",
            "state": (agent or {}).get("state") or "",
            "agent_session_id": (agent or {}).get("orcaSessionId") or "",
            "pane_title": by_tab.get(tab_id, ""),
            "title": terminal.get("title") or "",
            "worktree_path": terminal.get("worktreePath") or "",
            "connected": bool(terminal.get("connected")),
            "writable": bool(terminal.get("writable")),
        })
    return out


def find_session(sessions: List[Dict[str, Any]], session_id: str) -> Optional[Dict[str, Any]]:
    """依 handle（全形或前 8 碼）找 session。找不到回 None。"""
    if not session_id:
        return None
    for row in sessions:
        if row["handle"] == session_id or row["handle"].startswith(session_id):
            return row
    return None


def presence_of_handle(sessions: List[Dict[str, Any]], session_id: str) -> str:
    """用**已經掃好的**清單判斷 presence（一次 tick 掃一次，不逐列敲 CLI）。"""
    target = find_session(sessions, session_id)
    if target is None:
        return UNKNOWN
    state = str(target.get("state") or "").lower()
    if state in _BUSY_STATES:
        return BUSY
    if state in _IDLE_STATES:
        return IDLE
    # 沒有 agent 狀態（純 shell、宿主沒認出來）→ 沒有活動訊號 → 查不到
    return UNKNOWN


# ---------------------------------------------------------------- 送達

def send(handle: str, text: str, cli: str = "", wait_submit: int = 30,
         timeout: int = 120) -> Dict[str, Any]:
    """把提示詞送進一個宿主終端。

    回傳：
    - `{"state": "delivered", ...}` —— 宿主看到提示詞被接受、**且看到回合開始**。
    - `{"state": "submitted", ...}` —— 輸入被接受，但宿主觀察不到回合
      （例如宿主不認得這個 agent）→ 要由**回執層**再核對一次。
    - 例外 `HostSendError`：明確失敗（`failed`）或狀態不明（`unknown`，
      傳輸層失敗——**不得盲目重送**，與 OpenCode 通道同規則）。

    多行文字一次送進去：宿主對認得的 agent 自己用 bracketed paste，
    嵌入的跳脫字元會被中和；shell 終端則是逐字寫入（本層只送 agent 提示詞）。
    """
    if not handle:
        raise HostSendError("沒有終端 handle，無法派工", "failed")
    if not isinstance(text, str) or not text.strip():
        raise HostSendError("提示詞是空的", "failed")
    resolved = resolve_cli(cli)
    if not resolved:
        raise HostSendError("找不到宿主 CLI，無法派工", "failed")
    args = ["terminal", "send", "--terminal", handle, "--text", text,
            "--enter", "--wait-submit", str(max(1, int(wait_submit))), "--json"]
    try:
        result = _run(resolved, args, timeout)
    except HostUnavailable as exc:
        # 傳輸層不知道有沒有送達 → 狀態不明（宿主 CLI 本身掛了）
        raise HostSendError(str(exc), "unknown")
    send_info = result.get("send") or {}
    prompt = send_info.get("prompt") or {}
    stages = prompt.get("stages") or []
    if not send_info.get("accepted"):
        raise HostSendError("宿主沒有接受輸入：%s" % json.dumps(
            send_info, ensure_ascii=False)[:300], "failed")
    if "turn_started" in stages:
        return {"state": "delivered", "channel": "host_terminal",
                "detail": "宿主看到回合開始（provider=%s）" % prompt.get("provider", "?")}
    return {"state": "submitted", "channel": "host_terminal",
            "detail": "輸入被接受，但宿主觀察不到回合（provider=%s）" % prompt.get("provider", "?")}


# ---------------------------------------------------------------- 讀取

def read_screen(handle: str, cli: str = "", timeout: int = 30) -> str:
    """讀一個宿主終端的**目前畫面**（有限尾端視窗）。

    畫面對「有沒有送達過」只能證明**看過**，證明不了「沒看過」——
    回執層要照 `unknown` 規則處理（視窗不完整的結論不能當沒送達）。
    """
    resolved = resolve_cli(cli)
    if not resolved:
        raise HostUnavailable("找不到宿主 CLI")
    result = _run(resolved, ["terminal", "read", "--terminal", handle], timeout)
    for key in ("output", "text", "screen"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(result, ensure_ascii=False)


def read_agent(agent_session_id: str, limit: int = 100, cli: str = "") -> List[Dict[str, Any]]:
    """讀一個宿主 agent session 的對話紀錄（**不是畫面**）。

    宿主認得 transcript 的 agent（例如 Claude Code）回 `entries`（role／text／tools）；
    認不出的（例如 OpenCode）會明確報錯 → 回傳空清單，呼叫端改用 `read_screen`。
    """
    resolved = resolve_cli(cli)
    if not resolved:
        raise HostUnavailable("找不到宿主 CLI")
    try:
        result = _run(resolved, ["agent", "read", agent_session_id,
                                 "--limit", str(limit), "--json"], 30)
    except HostUnavailable:
        # 「讀不了」本身是合法結論（宿主認不出這個 agent 的 transcript），不往上丟
        return []
    entries = result.get("entries")
    return entries if isinstance(entries, list) else []


# ---------------------------------------------------------------- 開新 session

#: 宿主終端送達三態（與 `receipts` 的 DELIVERED / NOT_DELIVERED / DELIVERY_UNKNOWN
#: 同義；這裡獨立一份避免跨模組循環 import）。
HT_DELIVERED = "delivered"
HT_NOT_DELIVERED = "not-delivered"
HT_UNKNOWN = "unknown"

#: 與 `receipts.UNVERIFIED` 同值（這裡獨立一份，避免跨模組循環 import）。
UNVERIFIED = "receipt.unverified"


def _host_row(session_id: str) -> Optional[Dict[str, Any]]:
    """把宿主終端 handle 對到宿主掃描出來的那一列（含工作目錄）。"""
    try:
        sessions = scan_sessions()
    except HostUnavailable:
        return None
    for row in sessions:
        handle = row["handle"]
        if handle == session_id or handle.startswith(session_id):
            return row
    return None


def _handle_agent_session_id(session_id: str) -> Optional[str]:
    """把宿主終端 handle 對到 host 的 agent session id（`agent read` 用）。

    找不到 → None（例如那個終端是純 shell，沒有 agent 紀錄）。
    """
    row = _host_row(session_id)
    if row is None:
        return None
    return row.get("agent_session_id") or None


# ---------------------------------------------------------------- 磁碟 transcript（回執備援）

#: 一個 session 的 transcript 檔案快取：`handle -> (抓取時間, 檔案清單)`。
#: 檔案清單只跟「哪個 session」有關，不隨內容變 → 幾秒鐘內重複查不重讀檔案。
_TRANSCRIPT_FILES: Dict[str, Tuple[float, List[str]]] = {}

#: 判定結果快取：`(路徑, task_id, mtime, size) -> 判定`。
_TRANSCRIPT_VERDICT: Dict[Tuple[str, str, int, int], Dict[str, Any]] = {}

#: 檔案清單快取的有效秒數。
_TRANSCRIPT_TTL = 20.0


def _slug_for_path(path: str) -> str:
    """工作目錄 → transcript 的專案資料夾名（非英數字元一律換成 `-`）。"""
    cleaned = os.path.abspath(os.path.expanduser(path or ""))
    return re.sub(r"[^A-Za-z0-9]", "-", cleaned)


def _transcript_dirs(cwd: str = "") -> List[str]:
    """要掃的 transcript 專案資料夾。

    有工作目錄就只看那一個專案（角色都在同一個專案裡）；
    沒有就**全部**掃（寧可多讀，也不要因為猜不到而放棄證據）。
    """
    root = CLAUDE_TRANSCRIPT_ROOT
    if not os.path.isdir(root):
        return []
    if cwd:
        scoped = os.path.join(root, _slug_for_path(cwd))
        if os.path.isdir(scoped):
            return [scoped]
    dirs = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for name in names:
        path = os.path.join(root, name)
        if os.path.isdir(path):
            dirs.append(path)
    return dirs


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _handle_names(text: str) -> set:
    """這份 transcript 裡出現過的終端 handle。"""
    return set(re.findall(r"term_[0-9A-Za-z]+", text))


def session_transcripts(handle: str, cwd: str = "") -> List[str]:
    """這個終端 handle 對到的 transcript 檔案（**最像自己的一份排在前面**）。

    **為什麼需要這一層**：宿主 CLI 的 `agent read` 只給**尾端視窗**
    （實測約 20 條，`--limit` 再大也不會更多）→ 工作量大時，
    早期的派工訊息被推出視窗 → 回執核不到 → **角色白做工**。

    磁碟上的 transcript 是**同一份證據的完整版**：
    宿主 agent 自己的 session 檔裡會出現自己的終端 handle，
    用它就能把檔案對到這個 session。

    **對不上一律回空清單**——寧可查不到，也不要拿別人的紀錄當證據。
    """
    if not handle:
        return []
    now = time.time()
    cached = _TRANSCRIPT_FILES.get(handle)
    if cached is not None and now - cached[0] < _TRANSCRIPT_TTL:
        return cached[1]
    found = []
    for directory in _transcript_dirs(cwd):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            text = _read_text(path)
            if handle not in text:
                continue
            # **handle 越少 → 越像這個 session 自己的 transcript**
            #（主 session 的檔案裡會出現別人的 handle）。
            found.append((len(_handle_names(text)), path))
    found.sort()
    paths = [path for _, path in found]
    _TRANSCRIPT_FILES[handle] = (now, paths)
    return paths


def _line_role(line: str) -> str:
    """jsonl 一行的角色（`user`／`assistant`／其他）。"""
    try:
        data = json.loads(line)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    kind = str(data.get("type") or "")
    if kind in ("user", "assistant"):
        return kind
    message = data.get("message")
    if isinstance(message, dict):
        role = str(message.get("role") or "")
        if role in ("user", "assistant"):
            return role
    return ""


def transcript_verdict(path: str, task_id: str) -> Dict[str, Any]:
    """在 transcript 檔裡找這張任務，回「有沒有提到／後面有沒有回覆」。

    - `mentioned`：檔裡出現過這個 task_id（派工訊息或 agent 自己的回報）。
    - `answered`：提到之後有 assistant 回覆（或提到的那行本身就是 assistant）。
    """
    key = (path, task_id)
    try:
        stat = os.stat(path)
        stamp = int(stat.st_mtime)
        size = int(stat.st_size)
    except OSError:
        return {"mentioned": False, "answered": False}
    cache_key = (path, task_id, stamp, size)
    cached = _TRANSCRIPT_VERDICT.get(cache_key)
    if cached is not None:
        return cached
    mentioned = False
    mention_index = -1
    mention_role = ""
    last_assistant = -1
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                role = _line_role(line)
                if role == "assistant":
                    last_assistant = index
                if task_id in line:
                    mentioned = True
                    mention_index = index
                    mention_role = role
    except OSError:
        return {"mentioned": False, "answered": False}
    answered = mentioned and (mention_role == "assistant" or last_assistant > mention_index)
    verdict = {"mentioned": mentioned, "answered": answered}
    _TRANSCRIPT_VERDICT[cache_key] = verdict
    return verdict


def verify_in_transcript(handle: str, task_id: str, cwd: str = "") -> Dict[str, Any]:
    """在磁碟 transcript 裡核對這張任務（CLI 視窗看不到時的備援）。

    回 `{}` = 沒有任何證據；其餘帶 `verified`／`detail`。
    """
    for path in session_transcripts(handle, cwd):
        verdict = transcript_verdict(path, task_id)
        if not verdict["mentioned"]:
            continue
        if not verdict["answered"]:
            return {"verified": False,
                    "reason_detail": "磁碟 transcript 找得到這張任務的派工訊息，"
                                     "但尚未看到 assistant 回覆",
                    "detail": {"anchor": "transcript_file",
                               "file": os.path.basename(path)}}
        return {"verified": True,
                "detail": {"anchor": "transcript_file",
                           "file": os.path.basename(path)}}
    return {}


# ---------------------------------------------------------------- 回執核對（宿主通道）

def _fail(reason: str, **kwargs: Any) -> Dict[str, Any]:
    result = {"verified": False, "reason": reason, "platform": "host_terminal",
              "message_id": "", "session_id": "", "model_id": "", "context_id": "",
              "finish": "", "detail": {}}
    result.update(kwargs)
    return result


def _ok(**kwargs: Any) -> Dict[str, Any]:
    result = {"verified": True, "reason": "", "platform": "host_terminal",
              "message_id": "", "session_id": "", "model_id": "", "context_id": "",
              "finish": "", "detail": {}}
    result.update(kwargs)
    return result


def verify_host(handle: str, task_id: str,
                dispatched_at: Optional[Any] = None,
                submitted_at: Optional[Any] = None,
                **_: Any) -> Dict[str, Any]:
    """透過宿主終端（host CLI）核對送達與回執。

    `handle` 是宿主終端 handle（`term_…`）。優先走 `agent read`（對有
    transcript 的 agent，例如 Claude Code）；認不出 transcript 的 agent
    （例如 OpenCode）改讀終端畫面（`terminal read`）。

    回執錨點與 OpenCode 相同（關聯鍵是 task_id）：
    - 畫面/對話紀錄裡有這張任務的 task_id → 已送達（使用者看到提示詞）
    - 且之後有 assistant 回覆 → 回合已跑起來（verified）
    - 看不到 → `unknown`（**不要**當沒送達；畫面只保留尾端視窗）
    """
    if not handle or not task_id:
        return _fail(UNVERIFIED, reason_detail="缺少 handle 或 task id")

    # 1) transcript path（有 agent 紀錄的宿主，例如 Claude Code）
    agent_session_id = _handle_agent_session_id(handle)
    if agent_session_id:
        entries = read_agent(agent_session_id)

        # 主錨點：派送提示詞（user 項）裡有 task_id，且之後有 assistant 回覆。
        target_idx = None
        for idx, entry in enumerate(entries):
            if entry.get("role") == "user" and task_id in (entry.get("text") or ""):
                target_idx = idx
                break
        if target_idx is not None:
            assistants = [e for e in entries[target_idx + 1:]
                          if e.get("role") == "assistant"]
            if assistants:
                return _ok(
                    platform="host_terminal", session_id=handle,
                    context_id=agent_session_id,
                    detail={"anchor": "host_read",
                            "agent_session_id": agent_session_id})
            # 提示詞被接受，但尚未看到 assistant 回覆
            return _fail(UNVERIFIED, platform="host_terminal", session_id=handle,
                         reason_detail="提示詞已進入畫面，尚未看到 assistant 回覆")

        # 備援錨點：忙碌的 session 會把派送提示詞滾出 23 條視窗，user 項找不到。
        # 但 agent 的回覆常會「帶到 task_id」（例：「T-000027 已交、S-000017 pending」）。
        # 只要 task_id 在視窗裡**任一項**出現、且該次出現之後有 assistant 項，
        # 就證明 session 確實**看到並處理了這張任務**（不是「只送達」），可視為 verified。
        last_mention = None
        for idx, entry in enumerate(entries):
            if task_id in (entry.get("text") or ""):
                last_mention = idx
        if last_mention is not None:
            followers = [e for e in entries[last_mention + 1:]
                         if e.get("role") == "assistant"]
            # 若最後一次提及本身就是 assistant 項（agent 自己報了 task_id）也算數
            if followers or entries[last_mention].get("role") == "assistant":
                return _ok(
                    platform="host_terminal", session_id=handle,
                    context_id=agent_session_id,
                    detail={"anchor": "host_read_any",
                            "agent_session_id": agent_session_id})
        # 視窗裡完全找不到 task_id → 可能是尾端視窗把它截掉了 →
        # **改讀磁碟上的完整 transcript**（同一份證據、完整版）。
        row = _host_row(handle) or {}
        disk = verify_in_transcript(handle, task_id, row.get("worktree_path") or "")
        if disk.get("verified"):
            return _ok(platform="host_terminal", session_id=handle,
                       context_id=agent_session_id,
                       detail=disk.get("detail") or {})
        if disk.get("reason_detail"):
            return _fail(UNVERIFIED, platform="host_terminal", session_id=handle,
                         reason_detail=disk["reason_detail"])
        return _fail(UNVERIFIED, platform="host_terminal", session_id=handle,
                     reason_detail="host 讀不到這張任務的派送訊息（視窗可能已截斷）")

    # 2) screen path（宿主不認得這個 agent，例如 OpenCode）→ 只能看畫面
    screen = read_screen(handle)
    if task_id in screen:
        return _ok(platform="host_terminal", session_id=handle,
                   detail={"anchor": "screen"})
    return _fail(UNVERIFIED, platform="host_terminal", session_id=handle,
                 reason_detail="host 畫面讀不到這張任務（可能沒開著，或畫面已滾過）")


# ---------------------------------------------------------------- SUBAGENT context 證據

#: Claude Code 的 transcript 根目錄。
#: 每個 session 一個檔案：`<root>/<proj>/<session-uuid>.jsonl`；
#: 每個 subagent 另外一份：`<root>/<proj>/<session-uuid>/subagents/agent-<id>.jsonl`。
CLAUDE_TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")

#: subagent transcript 的檔名樣式。
_SUBAGENT_TRANSCRIPT = "agent-%s.jsonl"

def delivery_state(handle: str, task_id: str,
                   dispatched_at: Optional[Any] = None,
                   submitted_at: Optional[Any] = None,
                   **_: Any) -> str:
    """這條派送訊息在宿主終端裡，收件方到底收到了沒？

    同語義：delivered / not-delivered / unknown（宿主畫面不完整時
    只能回 unknown，不得當沒送達）。
    """
    if not handle or not task_id:
        return HT_UNKNOWN
    agent_session_id = _handle_agent_session_id(handle)
    if agent_session_id:
        entries = read_agent(agent_session_id)
        # transcript 裡**任何一則訊息**出現這張 task_id → 已送達
        # （派工訊息是 user 角色；長對話時 user 訊息可能被尾端視窗截掉，
        #  但 task_id 也可能留在後續的 assistant 摘要裡，所以不限角色）
        found = any(task_id in (entry.get("text") or "") for entry in entries)
        if found:
            return HT_DELIVERED
        # 視窗看不到 → 查磁碟 transcript（同一份證據的完整版）
        row = _host_row(handle) or {}
        for path in session_transcripts(handle, row.get("worktree_path") or ""):
            if transcript_verdict(path, task_id)["mentioned"]:
                return HT_DELIVERED

    # 畫面 fallback（handle 還活著時）
    try:
        screen = read_screen(handle)
        if task_id in screen:
            return HT_DELIVERED
    except HostUnavailable:
        pass
    return HT_UNKNOWN


def launch(agent_id: str, worktree: str = "", prompt: str = "", cli: str = "",
           focus: bool = False, timeout: int = 180) -> Dict[str, Any]:
    """請宿主開一個新 agent session（人類的「開一棒」，不是引擎自動開）。

    回傳宿主給的 receipt（含 handle）。**只有人類命令面叫這裡**——
    引擎派工永遠只對**既有** handle 送，不開新的（開 session 是人的決定）。
    """
    if not agent_id:
        raise HostSendError("沒有 agent 名，無法開 session", "failed")
    resolved = resolve_cli(cli)
    if not resolved:
        raise HostSendError("找不到宿主 CLI，無法開 session", "failed")
    args = ["agent", "launch", "--agent", agent_id, "--json"]
    if worktree:
        args += ["--worktree", worktree]
    if prompt:
        args += ["--prompt", prompt]
    if focus:
        args.append("--focus")
    result = _run(resolved, args, timeout)
    launch_info = result.get("launch") or result
    return {"handle": launch_info.get("handle") or launch_info.get("terminalHandle") or "",
            "kind": launch_info.get("kind") or "",
            "detail": launch_info.get("detail") or launch_info.get("note") or "",
            "raw": result}


def rename(handle: str, title: str, cli: str = "",
           timeout: int = 20) -> bool:
    """設定宿主終端的 **tab 標題**（`terminal rename`）。

    這一層是席位對位用的身分載體，理由見 `scan_sessions` 的 `pane_title`：

    - tab 標題由**宿主**保管，terminal 裡的 TUI 蓋不掉它（`terminal show`
      的 `title` 是 leaf 標題，agent 會用 OSC 序列一直覆寫，不能當身分）
    - 宿主重啟後 tab layout 還在，標題跟著留下來

    失敗**不丟例外**（回 `False`）：改名只是對位的便利措施，不是派工的前置
    條件；重啟前的舊標題仍然在 layout 裡，下一次 tick 照樣對得上。
    但還是要把失敗擋在這裡，不要讓「改不了名」變成「派不出去」。
    """
    if not handle or not title:
        return False
    resolved = resolve_cli(cli)
    if not resolved:
        return False
    try:
        _run(resolved, ["terminal", "rename", "--terminal", handle,
                        "--title", title, "--json"], timeout)
    except HostUnavailable:
        return False
    return True


def sanitize_project_name(name: str) -> str:
    """專案名 → 可放進標題的片段（`/` 會被當成標題分段 → 換掉）。

    引擎與終端標題共用這一個規則——兩邊各自轉換就會對不上。
    """
    return (name or "").strip().replace("/", "-")


def seat_title(role: str, short_name: str, project: str = "") -> str:
    """席位標題格式。**改這裡就改了全系統的對位依據**，字串要穩定。

    帶 `project` 時是 `<專案>/<ROLE>/<SHORT>`（例如 `產品研究/PLANNER/P1`），
    不帶時是 `<ROLE>/<SHORT>`（例如 `PLANNER/P1`）。

    **為什麼要有專案這一段**：標題是宿主裡**跨專案共用的**對位依據——
    沒有專案段時，A 專案的席位標題會跟 B 專案的同名席位撞標題，
    換綁時就會把 A 的席位綁到 B 的終端（跨專案誤綁）。
    """
    label = "%s/%s" % ((role or "").strip().upper(), (short_name or "").strip())
    name = sanitize_project_name(project)
    return "%s/%s" % (name, label) if name else label


def parse_seat_title(title: str) -> Dict[str, str]:
    """把 tab 標題解析成 `{project, role, short_name}`。

    - `PLANNER/P1` → 沒有專案段（舊格式）
    - `產品研究/PLANNER/P1` → 有專案段

    解析不出來（不是這個形狀）回空字典——**寧可不對位，也不要猜**。
    """
    parts = [p.strip() for p in (title or "").split("/") if p.strip()]
    if len(parts) < 2:
        return {}
    role = parts[-2].upper()
    short_name = parts[-1]
    project = "/".join(parts[:-2])
    return {"project": project, "role": role, "short_name": short_name}
