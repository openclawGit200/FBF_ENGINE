"""**上下文收合（compact）**——「離上一次 compact 多久了」。

引擎會在 session **沒有工作可以做**的時候提醒它收合上下文。
要判斷「離上一次 compact 超過半小時」，就要知道**上一次 compact 的時間**，
來源有兩個：

1. **宿主紀錄**（真的 compact 過）
   - **Claude Code**：transcript（`~/.claude/projects/*/<session>.jsonl`）裡
     `isCompactSummary: true` 的最後一筆時間。
   - **OpenCode**：session 訊息裡 `type == "compaction"` 的最後一筆時間。
2. **引擎自己的紀錄**（我們要求過 compact）——偵測不到時的保守值：
   假設它照我們的要求做完了，於是「上次 compact」＝「上次要求」的時間。

**偵測不到就回 None，不要猜**——猜錯會讓引擎以為「剛 compact 過」而不再提醒。

**這個模組只讀不寫**：正本裡的紀錄由呼叫端（`engine`）寫。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from . import hostact, receipts

#: 多久算「離上一次 compact 超過半小時」（秒）。
COMPACT_REPEAT_SECONDS = 30 * 60

#: **兩種收合方式**（專案層設定 `compact_mode`）：
#:
#: - `reset`——**清空對話、session 不變**（預設）：上下文歸零，但 session 的設定
#:   （模型、權限、工具、工作目錄）**全部保留**。
#: - `compact`——壓縮成摘要：保留脈絡，繼續用同一個 session。
#:
#: **優先序（使用者裁決 2026-11-25）**：**能 reset 就 reset，不行就 compact。
#: 某個 agent 沒有清空指令（OpenCode 沒有 `/clear`）→ 自動退回 compact。
#:
#: **不做「開新 session」**：新 session 會用預設值重新開始——
#: 使用者配給那個 session 的模型、權限、工具設定會全部不見
#: （使用者裁決 2026-11-25）。要換模型請另外用 `fbflow.py role`／session 設定。
MODES = ("reset", "compact")
DEFAULT_MODE = "reset"

#: 設定鍵（在專案正本的 settings 表）。
MODE_SETTING = "compact_mode"

#: 每個 agent 的收合指令。
#:
#: **對照過實際來源**：
#: - **Claude Code**：`/compact`（把對話壓縮成摘要）與 `/clear`
#:   （**清空對話歷史、保留這個 session 本身**）都是內建斜線指令。
#: - **OpenCode**：官方文件的常用指令裡**沒有** `/clear` →
#:   reset 模式在 OpenCode 上**退回用 compact**（不假裝有 `/clear`）。
AGENT_COMMANDS = {
    "claude": {"compact": "/compact", "reset": "/clear"},
    "opencode": {"compact": "/compact", "reset": ""},
}


# ---------------------------------------------------------------- 時間

def to_epoch(value: Any) -> Optional[float]:
    """把時間字串轉成 epoch 秒。

    支援兩種格式：引擎自己的 `YYYY-MM-DDTHH:MM:SS`，
    與宿主寫的 ISO 8601（`2026-11-25T08:58:26.516Z`）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # 毫秒時間戳（OpenCode 的 time.created 是毫秒）
        number = float(value)
        return number / 1000.0 if number > 1e11 else number
    text = str(value).strip()
    if not text:
        return None
    # **先把時區拆出來**再做格式比對——不然 `…26.516Z` 的尾端
    # 會被當成分秒的小數位，解析直接失敗。
    tz = ""
    if text.endswith("Z"):
        tz = "+0000"
        text = text[:-1]
    else:
        match = re.search(r"([+-]\d{2}:?\d{2})$", text)
        if match:
            tz = match.group(1).replace(":", "")
            text = text[:match.start()]
    if tz:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S%z"):
            try:
                return time.mktime(time.strptime(text + tz, fmt))
            except ValueError:
                continue
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


def newest(*values: Any) -> Optional[float]:
    """取最新的一個時間（epoch 秒）；沒有就 None。"""
    epochs = [e for e in (to_epoch(v) for v in values) if e is not None]
    return max(epochs) if epochs else None


# ---------------------------------------------------------------- 正本紀錄

def record(conn: sqlite3.Connection, session_id: str, when: str,
           source: str) -> None:
    """記下「這個 session 上一次 compact 的時間」。

    `source`：`detected`（宿主紀錄裡真的 compact 過）／`requested`（我們要求過）。
    """
    if not session_id or not when:
        return
    conn.execute(
        "INSERT INTO session_compacts(session_id, compacted_at, source, updated_at)"
        " VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
        " compacted_at = excluded.compacted_at, source = excluded.source,"
        " updated_at = excluded.updated_at",
        (session_id, when, source, when))


def last_recorded(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    """正本裡記的上一次 compact 時間；沒有就 None。"""
    if not session_id:
        return None
    row = conn.execute(
        "SELECT compacted_at FROM session_compacts WHERE session_id = ?",
        (session_id,)).fetchone()
    return row["compacted_at"] if row else None


def ledger_snapshot(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """整份 compact 紀錄（給人看：哪個 session 什麼時候收合過）。"""
    rows = conn.execute(
        "SELECT session_id, compacted_at, source, updated_at FROM session_compacts"
        " ORDER BY compacted_at DESC").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- Claude Code

def claude_last_compact(claude_session_id: str) -> Optional[str]:
    """Claude Code：最後一次 compact 的時間（transcript 裡的 `isCompactSummary`）。"""
    path = hostact.claude_transcript_path(claude_session_id)
    if not path:
        return None
    latest = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"isCompactSummary"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not entry.get("isCompactSummary"):
                    continue
                stamp = str(entry.get("timestamp") or "")
                if stamp > latest:
                    latest = stamp
    except OSError:
        return None
    return latest or None


# ---------------------------------------------------------------- OpenCode

def opencode_last_compact(endpoint: str, session_id: str,
                          binary: str = "opencode") -> Optional[str]:
    """OpenCode：最後一次 compact 的時間（訊息型別 `compaction`）。"""
    if not session_id:
        return None
    try:
        messages = receipts._session_messages(endpoint, session_id, binary)
    except Exception:
        # **查不到就當查不到**——不要猜，也不要讓偵測失敗拖垮 tick。
        return None
    latest = ""
    for message in messages:
        if str(message.get("type") or "") != "compaction":
            continue
        stamp = message.get("time") or {}
        created = stamp.get("created") if isinstance(stamp, dict) else None
        if created:
            value = time.strftime("%Y-%m-%dT%H:%M:%S",
                                  time.localtime(to_epoch(created) or 0))
            if value > latest:
                latest = value
    return latest or None


def opencode_has_messages(endpoint: str, session_id: str,
                           binary: str = "opencode") -> Optional[bool]:
    """OpenCode：這個 session 有沒有對話紀錄（沒有＝剛開的空 session）。"""
    if not session_id:
        return None
    try:
        messages = receipts._session_messages(endpoint, session_id, binary)
    except Exception:
        return None
    return bool(messages)


# ---------------------------------------------------------------- 統一入口

def inspect_session(session_id: str, agent: str = "",
                    host_sessions: Optional[List[Dict[str, Any]]] = None,
                    endpoint: str = "", binary: str = "opencode") -> Dict[str, Any]:
    """查一個 session 的「有沒有對話紀錄」與「上一次 compact 的時間」。

    回傳 `{"has_history": bool|None, "last_compact": str|None}`。

    - `has_history` 是 **None** 表示**查不到**（不是「沒有」）——
      呼叫端要自己決定怎麼處理（不要把「查不到」當成「沒事做」）。
    - `agent` 是 `roles.agent` 的值（`claude`／`opencode`／…）。
    """
    info: Dict[str, Any] = {"has_history": None, "last_compact": None}
    agent = (agent or "").strip().lower()

    if agent == "claude":
        claude_session_id = hostact.claude_session_id_for(session_id, host_sessions)
        if not claude_session_id:
            return info
        path = hostact.claude_transcript_path(claude_session_id)
        if not path:
            return info
        info["has_history"] = True
        info["last_compact"] = claude_last_compact(claude_session_id)
        return info

    if agent == "opencode":
        has_messages = opencode_has_messages(endpoint, session_id, binary)
        if has_messages is None:
            return info
        info["has_history"] = has_messages
        info["last_compact"] = opencode_last_compact(endpoint, session_id, binary)
        return info

    # 其他宿主（pi／mcode／codex…）：本引擎沒有它們的對話紀錄格式。
    return info



# ---------------------------------------------------------------- 模式

def mode_of(conn: sqlite3.Connection) -> str:
    """這個專案現在用哪一種收合方式。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (MODE_SETTING,)).fetchone()
    value = (row["value"] if row else "") or ""
    return value if value in MODES else DEFAULT_MODE


def set_mode(conn: sqlite3.Connection, mode: str) -> str:
    """設定收合方式；回傳實際生效的值。"""
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError("收合方式只能是：%s" % "／".join(MODES))
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (MODE_SETTING, mode))
    return mode


def command_for(agent: str, mode: str) -> Tuple[str, str]:
    """這個 agent 這種模式要用哪個指令。

    回傳 `(指令, 說明)`；指令是空字串代表**引擎自己處理**（釋放席位）。
    說明非空代表**有退化**（例如這個 agent 沒有清空指令 → 改用壓縮）。
    """
    table = AGENT_COMMANDS.get((agent or "").strip().lower(), {})
    if mode == "reset":
        command = table.get("reset", "")
        if not command:
            return table.get("compact", "/compact"), \
                "這個 agent 沒有『清空對話』的指令 → 改用壓縮"
        return command, ""
    return table.get("compact", "/compact"), ""


# ---------------------------------------------------------------- 提示詞

def prompt_text(mode: str = DEFAULT_MODE, agent: str = "", role: str = "",
                minutes: int = 30, anchor: str = "") -> str:
    """組「請收合上下文」的提示詞。

    **不含真的 task_id**——回執核對會把「user 項含 task_id、且之後有
    assistant 回覆」當成這一棒已交件 → 帶 task_id 會造成**假回執**。
    所以用**獨立錨點**（與喚醒同一套做法）。

    **第一行是宿主自己的指令**：宿主終端那條通道是把文字貼進 TUI
    （bracketed paste），有些宿主會把開頭的斜線指令當指令執行 → 直接收合；
    當成訊息送進去時，agent 也讀得懂下面這段說明。
    """
    command, fallback_note = command_for(agent, mode)
    # **退化時用壓縮的說法**——這個 agent 沒有清空指令，卻照 reset 的
    # 文字寫「請清空目前的對話」，會跟它實際該做的動作對不上。
    effective_mode = mode
    if mode == "reset" and fallback_note:
        effective_mode = "compact"
    lines = []
    if command:
        lines.append(command)
        lines.append("")
    lines += [
        "【FBF ENGINE 上下文收合提醒】",
        "你現在**沒有待交的工作**（單據都在別的狀態、或已經有人在做）。",
        "距離你上一次收合已經超過 %d 分鐘——" % minutes,
    ]
    if effective_mode == "reset":
        lines.append("請**清空目前的對話**（保留這個 session 本身），從乾淨的上下文開始。")
        lines.append("")
        lines.append("清空**不會**改掉你的 session 設定（模型、權限、工具都留著），"
                     "席位也還在——引擎認的是這個 session，不是剛才那段對話。")
        lines.append("若清空之後你發現自己拿不到席位或 UUID 不認了，"
                     "再重新認領一次：")
        lines.append("  fbflow_register(role=%s, short_name=<你的簡稱>)" % (role or "你的角色"))
    else:
        lines.append("請收合上下文（把目前對話壓縮成摘要，保留任務脈絡），"
                     "讓這個 session 保持乾淨、隨時可以接新的工作。")
    if fallback_note:
        lines.append("")
        lines.append("（注意：%s）" % fallback_note)
    lines += [
        "",
        "怎麼做：",
    ]
    if command:
        lines.append("1. 執行 %s。" % command)
    else:
        lines.append("1. 依照上面的說明處理（你的環境沒有對應的指令）。")
    lines.append("2. 完成後**結束本輪回覆**——不要 sleep、不要輪詢、不要自己開新工作。")
    lines.append("3. 之後有派工會再叫你。")
    if anchor:
        lines.append("")
        lines.append("錨點：%s（請在回覆裡帶上這段，引擎才知道有送達）" % anchor)
    return "\n".join(lines)
