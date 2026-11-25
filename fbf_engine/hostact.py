"""**宿主活動判型**——「這個 session 到底還活著嗎？」

引擎要判「這一棒是不是卡住了」，光看正本不夠：
正本只看得到**交件**，看不到 agent 正在幹什麼。

現場（2026-11-25一個批次處理專案，實測）：
WORKER 跑 長時間批次工作，被引擎判了 **15 次逾時**並重派，
但每一分鐘它的 Claude Code transcript 都還有紀錄——
**最後一筆全是模型請求（`api-request`），不是工具執行**。
真相是：它在等模型回應（有 4 次甚至是模型端直接回錯
`API Error: 422 … content safety screening`），
以及 5 次 context 被壓縮後重建。引擎卻把它當「失聯」殺掉重派，
還在同一個回合裡一次插進 4 張新派工。

**所以「逾時」要分型**，只有「回合已結束、什麼都沒在動」才叫卡住：

| 型態 | 意義 | 該不該催 |
|---|---|---|
| `waiting_model` | 請求已送出，模型還沒回 | **不催**（可能在等，也可能在網路上卡） |
| `running_tool` | 工具執行中／等工具回來 | **不催** |
| `compacting` | context 被壓縮，正在重建 | **不催** |
| `idle` | 回合已結束，什麼都沒在動 | **這才該催** |
| `unknown` | 查不到宿主紀錄 | 保守照舊（正本判斷） |

**這個模組只讀不寫**：讀 transcript 的**尾端**（不整份讀——
實測有 73 MB 的檔案），回傳型態與最後活動時間。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from . import hostterm, receipts

#: Claude Code 的對話紀錄（transcript）放在這裡。
CLAUDE_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

#: Claude Code 的 session id 是 UUID；OpenCode 的是 `ses_…`。
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: 讀 transcript 尾端多少位元組（**不整份讀**：實測有 73 MB 的檔案）。
TAIL_BYTES = 512 * 1024

#: 型態。
STATE_WAITING_MODEL = "waiting_model"
STATE_RUNNING_TOOL = "running_tool"
STATE_COMPACTING = "compacting"
STATE_IDLE = "idle"
STATE_UNKNOWN = "unknown"

#: **算「有意義」的事件型態**（其餘是雜訊：附件、檔案歷史快照、標題…）。
_NOISE = {
    "attachment", "file-history-snapshot", "file-history-delta",
    "last-prompt", "mode", "permission-mode", "ai-title", "atis-latch",
    "api-request-shape", "api-request-blob", "tools-used",
}


def classify(events: List[Dict[str, Any]]) -> str:
    """把一段事件（由舊到新）判成一個型態。

    **純函式**——好測試。事件是 transcript 的行（已解析成 dict）。
    """
    if not events:
        return STATE_UNKNOWN

    # 1) 最後一個 assistant 的工具呼叫有沒有回來？
    #    沒有 → 它還在等工具（或工具還在跑）。
    pending = None
    for event in events:
        etype = str(event.get("type") or "")
        if etype == "assistant":
            message = event.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("id")):
                        pending = block["id"]
        elif etype == "user":
            message = event.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "tool_result"
                            and block.get("tool_use_id")):
                        if pending == block["tool_use_id"]:
                            pending = None
    if pending:
        return STATE_RUNNING_TOOL

    # 2) 從尾端往前找第一個有意義的事件。
    for event in reversed(events):
        etype = str(event.get("type") or "")
        if etype in _NOISE:
            continue
        if etype in ("api-request", "queue-operation"):
            return STATE_WAITING_MODEL
        if etype == "system":
            content = str(event.get("content") or "").lower()
            if "compacted" in content or "compact" in content:
                return STATE_COMPACTING
            # stop_hook_summary／turn_duration＝回合結束 → 閒置
            return STATE_IDLE
        if etype == "assistant":
            return STATE_IDLE
        if etype == "user":
            return STATE_IDLE
        return STATE_UNKNOWN
    return STATE_UNKNOWN


def read_tail_events(path: str, limit: int = 400) -> List[Dict[str, Any]]:
    """讀 transcript 檔案的**尾端**事件（只讀最後 `TAIL_BYTES`）。

    讀不到（檔案不存在／不是 JSONL／權限）→ 回空清單。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()          # 丟掉被截斷的那一行
            raw = fh.read()
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events[-limit:]


def transcript_activity(path: str) -> Dict[str, Any]:
    """一個 transcript 檔案的最後活動時間與型態。"""
    state = STATE_UNKNOWN
    at: Optional[float] = None
    try:
        at = os.path.getmtime(path)
    except OSError:
        at = None
    events = read_tail_events(path)
    if events:
        state = classify(events)
        # **事件時間比 mtime 準**：mtime 是最後一次寫入（含壓縮快照），
        # 事件時間是「最後一次真的做事」。
        for event in reversed(events):
            stamp = event.get("timestamp")
            if stamp:
                epoch = _to_epoch(stamp)
                if epoch is not None:
                    at = epoch
                    break
    return {"state": state, "at": at, "source": "transcript"}


def _to_epoch(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


def claude_transcript_path(claude_session_id: str) -> str:
    """Claude Code 的 transcript 檔路徑；找不到就空字串。

    transcript 放在 `~/.claude/projects/<把路徑編碼過的工作目錄>/<session id>.jsonl`。
    **只列目錄、不讀內容**——實測有 73 MB 的檔案，讀內容會拖垮 tick。
    """
    if not claude_session_id or not _UUID_RE.match(claude_session_id):
        return ""
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return ""
    for entry in sorted(os.listdir(CLAUDE_PROJECTS_DIR)):
        candidate = os.path.join(CLAUDE_PROJECTS_DIR, entry, claude_session_id + ".jsonl")
        if os.path.isfile(candidate):
            return candidate
    return ""


def claude_session_id_for(session_id: str,
                          host_sessions: Optional[List[Dict[str, Any]]] = None) -> str:
    """把「席位上的 session id」對到 Claude Code 的 session UUID。

    兩種情況：
    1. 席位上的 id **本身就是** Claude 的 UUID → 直接用。
    2. 席位上的 id 是**宿主終端 handle**（`term_…`）→ 從宿主掃描結果
       找出那個終端對應的 agent session id。
    """
    if not session_id:
        return ""
    if _UUID_RE.match(session_id):
        return session_id
    for row in (host_sessions or []):
        if str(row.get("handle") or "") == session_id:
            candidate = str(row.get("agent_session_id") or "")
            if candidate and _UUID_RE.match(candidate):
                return candidate
    return ""


def opencode_activity(endpoint: str, session_id: str,
                      binary: str = "opencode") -> Dict[str, Any]:
    """OpenCode：這個 session 最後有沒有訊息（**只問時間，不看內容**）。"""
    if not session_id:
        return {"state": STATE_UNKNOWN, "at": None, "source": "none"}
    try:
        messages = receipts._session_messages(endpoint, session_id, binary)
    except Exception:
        return {"state": STATE_UNKNOWN, "at": None, "source": "none"}
    latest: Optional[float] = None
    for message in messages:
        stamp = message.get("time") or {}
        created = stamp.get("created") if isinstance(stamp, dict) else None
        if created is None:
            continue
        epoch = _to_epoch(created)
        if epoch is None:
            epoch = float(created) / 1000.0 if float(created) > 1e11 else float(created)
        if epoch is not None and (latest is None or epoch > latest):
            latest = epoch
    if latest is None:
        return {"state": STATE_UNKNOWN, "at": None, "source": "none"}
    return {"state": STATE_IDLE, "at": latest, "source": "messages"}


def session_activity(session_id: str, agent: str = "", cwd: str = "",
                     endpoint: str = "", binary: str = "opencode",
                     host_sessions: Optional[List[Dict[str, Any]]] = None
                     ) -> Dict[str, Any]:
    """這個 session 最後在幹嘛。

    回傳 `{"state": …, "at": epoch|None, "source": …}`。

    - `agent` 是 `roles.agent` 的值。
    - **查不到就回 `unknown`**——不要猜。呼叫端自己決定怎麼處理
      （「查不到」不等於「沒在動」）。
    """
    agent = (agent or "").strip().lower()
    if agent == "opencode":
        return opencode_activity(endpoint, session_id, binary)
    if agent in ("claude", "claude-code"):
        claude_session_id = claude_session_id_for(session_id, host_sessions)
        if not claude_session_id:
            return {"state": STATE_UNKNOWN, "at": None, "source": "none"}
        path = claude_transcript_path(claude_session_id)
        if not path:
            return {"state": STATE_UNKNOWN, "at": None, "source": "none"}
        return transcript_activity(path)
    return {"state": STATE_UNKNOWN, "at": None, "source": "none"}


def is_alive(state: str) -> bool:
    """這個型態算「還活著」嗎（→ 不該催、不該殺 task）。"""
    return state in (STATE_WAITING_MODEL, STATE_RUNNING_TOOL, STATE_COMPACTING)
