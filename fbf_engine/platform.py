"""FBF ENGINE 的**平台層**：OpenCode 派工（INFORM）與 presence 查詢。

這一層只做兩件事：**把訊息送到指定的 OpenCode Session**、**查某個 Session 現在忙不忙**。
**不知道工單、不知道狀態機**——那是 `engine.py` 的事。

沿用既有、已驗證的 INFORM 通道（`opencode run --session` 指定 Session 通知，
與 `GET /api/session/active` presence 端點）。該通道來自 v1 專案，
本模組是它的移植版——**不得另造派工通道**（規格第 10 節第 2 階段）。

presence 的契約（規格第 6 節）：
- 查得到且在 active 清單裡 → `busy`
- 查得到但不在 active 清單裡 → `idle`
- **查不到 → `unknown`**（不是離線）

`unknown` 的處理是「暫緩派送」，不是「當成離線」。
把「查不到」當成「不在」會讓系統在平台故障時以為所有人都在線，反而更危險。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from . import hostterm, ocserver, prompts

#: presence 三態
BUSY = "busy"
IDLE = "idle"
UNKNOWN = "unknown"

#: 受派者的執行平台（roles.agent 的值；`claude` 走宿主終端 session）
agent_opencode = "opencode"
agent_claude = "claude"

#: 宿主終端 handle 的慣用前綴（與 hostterm 一致）。
#: 角色表裡的 `session_id` 以這些開頭 → 走宿主終端送達；否則走 OpenCode 原生。
HOST_HANDLE_PREFIXES = hostterm.HOST_HANDLE_PREFIXES


def is_host_handle(session_id: str) -> bool:
    """角色表裡的 session_id 以宿主終端 handle 開頭（`term_…`）？

    那代表這個 session 不是 OpenCode 自己管理的，而是在宿主（host CLI）
    裡開著的終端；送達就用 `host terminal send`，沒有 OpenCode server 介入。
    """
    return bool(session_id) and session_id.startswith(HOST_HANDLE_PREFIXES)


def scan_host_sessions(cli: str = "") -> List[Dict[str, Any]]:
    """查目前 host CLI 管理的所有終端 session（供 auto-enroll 用）。

    與 `hostterm.scan_sessions` 同義；這裡包一層讓 engine 只倚賴 platform。
    查不到 → 空清單，不丟例外（「查不到」不是錯）。
    """
    return hostterm.scan_sessions(cli=cli)


def seat_title(role: str, short_name: str, project: str = "") -> str:
    """席位標題格式（`PLANNER/P1`；帶專案名時是 `<專案>/PLANNER/P1`）。

    **這是席位對位的唯一依據**，engine 與 hostterm 都用這一個函式，
    別各自拼字串——格式一漂移，重啟後就對不上位了。

    **帶專案名**：標題是宿主裡跨專案共用的對位依據，不帶專案名就會撞。
    """
    return hostterm.seat_title(role, short_name, project)


def parse_seat_title(title: str) -> Dict[str, str]:
    """把 tab 標題解析成 `{project, role, short_name}`（`hostterm` 的薄包裝）。"""
    return hostterm.parse_seat_title(title)


def sanitize_project_name(name: str) -> str:
    """專案名 → 可放進標題的片段（`hostterm` 的薄包裝）。"""
    return hostterm.sanitize_project_name(name)


def rename_host_session(handle: str, title: str, cli: str = "") -> bool:
    """把 host 終端的 tab 標題改成席位標題（失敗回 False，不丟例外）。"""
    return hostterm.rename(handle, title, cli=cli)


class PresenceUnknown(Exception):
    """平台查不到 presence。這**不是**離線。"""


class DispatchError(Exception):
    """派送失敗。`state` 說明是「明確失敗」還是「狀態不明」。"""

    def __init__(self, message: str, state: str = "failed"):
        super().__init__(message)
        self.state = state


# ---------------------------------------------------------------- API

def _api(endpoint: str, path: str, binary: str = "opencode", timeout: int = 30) -> Any:
    """呼叫 OpenCode 官方 API。

    官方語法（實測 v2.2.5）：
        opencode --server <url> api GET <path>

    **一定帶 `--server`（上游原始碼 v2.2.5
    `packages/cli/src/services/server-connection.ts`）**：

    - 帶了 `--server` → CLI 只做連線，**不會啟動任何東西** ✓
    - 沒帶 `--server` → CLI 會 `Service.ensure()` → **自動啟動一個常駐背景 server**
      （連帶它裡面的 MCP）——那不是這個套件該做的事 ✗

    端點由 `ocserver` **唯讀**探索既有 server（OpenCode 自己寫的註冊檔），
    連不到就明確報錯（`ServerMissing`），**不要**讓 CLI 去開一個。
    """
    try:
        url, password = ocserver.require(endpoint)
    except ocserver.ServerMissing as exc:
        raise PresenceUnknown(str(exc))
    argv = ocserver.cli_argv(binary, url, ["api", "GET", path])
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace",
                                env=ocserver.env_for(password))
    except subprocess.TimeoutExpired:
        raise PresenceUnknown("OpenCode API 查詢逾時：%s" % path)
    except OSError as exc:
        raise PresenceUnknown("OpenCode API 查詢失敗：%s" % exc)
    if result.returncode != 0:
        raise PresenceUnknown("OpenCode API 失敗（退出碼 %d）：%s"
                              % (result.returncode, (result.stderr or result.stdout)[-300:]))
    raw = (result.stdout or "").strip()
    if not raw:
        raise PresenceUnknown("OpenCode API 回應為空：%s" % path)
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise PresenceUnknown("OpenCode API 回應非合法 JSON：%s" % exc)


def active_sessions(endpoint: str, binary: str = "opencode",
                    timeout: int = 5) -> List[str]:
    """回傳目前 active 的 session_id 清單。

    **只在 API 成功且格式符合契約時回傳清單**；
    查不到就丟 `PresenceUnknown`，不可用空清單代替（會把「查不到」誤判成「都沒在跑」）。

    逾時刻意設短（5 秒）：presence 只是「現在打擾它好不好」的參考，
    查不到也照送（見 `engine._flush_outbox`）——**不該為了它拖住整趟 tick**。
    """
    data = _api(endpoint, "/api/session/active", binary, timeout=timeout)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = None
        for key in ("data", "sessions", "active"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
            if isinstance(value, dict):
                # 實測 2.2.5：`{"data": {"ses_xxx": {"type": "running"}, ...}}`
                # → 這支端點回的是「以 session id 為鍵的物件」，不是陣列。
                items = list(value.keys())
                break
        if items is None:
            raise PresenceUnknown("OpenCode API 回應格式不符（找不到 active 清單）")
    else:
        raise PresenceUnknown("OpenCode API 回應格式不符（不是陣列也不是物件）")

    out: List[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            sid = item.get("id") or item.get("sessionID") or item.get("session_id")
            if sid:
                out.append(str(sid))
    return out


def presence_of(endpoint: str, session_id: str, binary: str = "opencode") -> str:
    """某個 Session 的 presence：`busy` / `idle` / `unknown`。"""
    return presence_detail(endpoint, session_id, binary)[0]


def presence_in(active: Optional[List[str]], session_id: str) -> str:
    """用**已經查好的** active 清單判斷 presence。

    一次 tick 裡所有 outbox 指的是同一個 server → 查一次就夠，
    不用每一列都去敲一次 CLI。
    """
    if active is None or not session_id:
        return UNKNOWN
    return BUSY if session_id in active else IDLE


def presence_detail(endpoint: str, session_id: str,
                   binary: str = "opencode") -> Tuple[str, str]:
    """presence ＋ **失敗原因**（給人看的）。

    只有 `unknown` 沒說明為什麼，是最難查的一種狀況——
    「查不到」跟「平台在忙」外觀完全一樣。所以原因要帶回來。

    `endpoint` 空沒關係——`ocserver` 會去**唯讀**探索既有 server；
    探索不到才會回 unknown（並說明原因）。
    """
    if not session_id:
        return UNKNOWN, "沒有 session id"
    try:
        active = active_sessions(endpoint, binary)
    except PresenceUnknown as exc:
        return UNKNOWN, str(exc)
    return (BUSY if session_id in active else IDLE), ""


# ---------------------------------------------------------------- 派工

def send_hostterm(root: str, row: Dict[str, Any], text: str,
                  cli: str = "", wait_submit: int = 30) -> Dict[str, Any]:
    """把提示詞送進宿主（host CLI）管理的終端 session。

    角色表的 `session_id` 就是宿主終端 handle（`term_…`）。
    送達語義與 OpenCode 原生相容（delivered / submitted），但**沒有
    OpenCode server 介入**——draw from `hostterm` 模組。
    """
    session_id = row.get("session_id") or ""
    handle = row.get("delivery_handle") or session_id
    if not handle:
        raise hostterm.HostSendError("沒有終端 handle，無法派工", "failed")
    try:
        result = hostterm.send(handle, text, cli=cli, wait_submit=wait_submit)
    except hostterm.HostSendError as exc:
        state = exc.state if exc.state in ("failed", "unknown") else "failed"
        raise DispatchError(str(exc), state)
    return {"state": result["state"], "channel": "host_terminal",
            "detail": result.get("detail", "")}


def send_opencode(root: str, session_id: str, text: str, workdir: str = "",
                  binary: str = "opencode", wait: bool = False,
                  notif_id: str = "", timeout: int = 900,
                  endpoint: str = "") -> Dict[str, Any]:
    """把提示詞送到指定的 OpenCode Session（既有 INFORM 通道）。

    回傳：
    - `{"state": "queued", ...}` —— 原生 CLI 已啟動，尚未確認接收。
    - `{"state": "submitted", ...}` —— 同步送出成功。

    **送達狀態不明時不要重送**（規格第 6 節）：
    「通知可能已送達而回寫失敗」→ 狀態不明時先查 task／session。

    **一定帶 `--server`**（見 `_api` 的說明）：CLI 沒帶 `--server` 會自動
    啟動一個常駐背景 server——我們不要那種東西。找不到既有 server 就
    **明確失敗**（`DispatchError` state=`failed`）→ 走退避重試、最後標待人處理 ✓。
    """
    if not session_id:
        raise DispatchError("沒有 session_id，無法派工", "failed")
    if not isinstance(text, str) or not text.strip():
        raise DispatchError("提示詞是空的", "failed")

    try:
        url, password = ocserver.require(endpoint)
    except ocserver.ServerMissing as exc:
        raise DispatchError(str(exc), "failed")

    argv = ocserver.cli_argv(binary, url, ["run", "--session", session_id, text])
    env = ocserver.env_for(password)
    cwd = workdir or root

    if not wait:
        logs = os.path.join(root, "inbox", ".opencode")
        os.makedirs(logs, exist_ok=True)
        logfile = os.path.join(logs, "%s.log" % (notif_id or "dispatch"))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with open(logfile, "ab") as output:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                cwd=cwd, creationflags=flags, env=env,
                start_new_session=(os.name != "nt"))
        return {"state": "queued", "channel": "opencode_native", "pid": proc.pid,
                "detail": "原生 CLI 已啟動，尚未確認接收；不自動重送。記錄：%s" % logfile}

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd, encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        raise DispatchError("OpenCode 原生 CLI 逾時（狀態不明，不重送）", "unknown")
    except OSError as exc:
        raise DispatchError("OpenCode 原生 CLI 啟動失敗：%s" % exc, "failed")
    if proc.returncode:
        raise DispatchError("OpenCode 原生 CLI 失敗（退出碼 %d）：%s"
                            % (proc.returncode, (proc.stderr or proc.stdout)[-500:]), "failed")
    return {"state": "submitted", "channel": "opencode_native",
            "detail": "已透過原生 CLI 提交指定 Session"}


def send_to_session(root: str, row: Dict[str, Any], text: str,
                    notif_id: str = "", wait: bool = False,
                    binary: str = "opencode", endpoint: str = "") -> Dict[str, Any]:
    """依角色列（含 `session_id`／`delivery_handle`）送出提示詞。

    `endpoint` 空 → 由 `ocserver` 唯讀探索既有 server（**不會**自己開一個）。

    路由規則：`session_id`（或 `delivery_handle`）以宿主終端 handle
    開頭（`term_…`）→ `send_hostterm`（host CLI 終端）；否則走 OpenCode 原生
    `send_opencode`。這是**同一份 outbox、同一套狀態語義**，只是投遞媒體不同。
    """
    session_id = row.get("session_id") or ""
    handle = row.get("delivery_handle") or session_id
    if is_host_handle(handle):
        # cli 留空 → resolve_cli 走 PATH／環境變數／macOS 應用程式內附 CLI。
        # 寫死命令名會繞過後兩層——宿主 CLI 不在 PATH 時派工必失敗。
        return send_hostterm(root, row, text,
                             cli="", wait_submit=30)
    return send_opencode(root, session_id, text,
                         workdir=row.get("workdir") or "", binary=binary,
                         wait=wait, notif_id=notif_id, endpoint=endpoint)


# ---------------------------------------------------------------- 提示詞

def dispatch_message_id(task: Dict[str, Any], work: Optional[Dict[str, Any]] = None) -> str:
    """**這一輪的 message_id**——引擎產生，角色照抄。

    **為什麼要引擎產生**（實測 2026-11-25批次處理）：
    規則明明寫在提示詞裡（「每次交件都要新的，不可沿用上一輪」），
    agent 還是重複用了同一個 → 被引擎以 `receipt.message_reused` 退件。
    原因是**它手上沒有「新的 id」的來源**——那個 id 該來自宿主平台的訊息，
    agent 只能自己編，編錯就重複。

    所以改成：**引擎在派工時就發一個本輪 nonce**，寫進提示詞，
    agent 照抄 → 天然唯一、天然是「這一輪新產生的」。
    引擎的去重守衛照留（防偽造與重放）——但 agent 不再需要靠記憶力。

    值由 `task_id ＋ expected_version ＋ task 建立時間` 決定：
    同一棒重送 → 同一個（那是同一輪）；退件重交（版本 +1）→ 不同。
    """
    raw = "%s:%s:%s" % (
        task.get("id", ""),
        (work or {}).get("version", ""),
        task.get("created_at", ""))
    return "msg-%s" % hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def build_dispatch_text(task: Dict[str, Any], work: Optional[Dict[str, Any]],
                        contract: Dict[str, Any], role: str,
                        extra: str = "", *,
                        skill: str = "", output_dir: str = "",
                        method_notes: Optional[List[str]] = None) -> str:
    """組派工訊息。

    **必須包含 `task_id`**——回執核對靠它把平台訊息與這次任務對上。

    **只給「通知內容」**——哪一張單、搭配哪個專案 SKILL、產出放哪，
    以及**方法**（那些 pstack 工具真正在做的事）。

    **交件清單直接寫在訊息裡**：來源是 `workflow.stage_contract`（與守衛同一份），
    角色不必再讀別的地方才知道要交什麼。工單的**現況**（內容、驗收方式、
    既有解法）由角色自己呼叫 `fbflow_read` 查正本——提示詞不帶工作狀態。

    **本輪的 `message_id` 也由引擎發**（`dispatch_message_id()`）——
    agent 照抄就好，不必自己編（自己編會撞車，實測被退過一次）。
    """
    lines: List[str] = []
    lines.append("【FBF ENGINE 派工】")
    lines.append("task_id: %s" % task.get("id", ""))
    if work:
        lines.append("work_id: %s" % work.get("id", ""))
        lines.append("expected_version: %s" % work.get("version", ""))
    lines.append("message_id: %s" % dispatch_message_id(task, work))
    lines.append("專案 SKILL：%s" % (skill or "（未指定）"))
    if output_dir:
        lines.append("產出資料夾：%s" % output_dir)
    lines.append("")
    lines.append("先讀這張工單的**正本快照**（內容、驗收方式、既有解法）："
                 "fbflow_read(target_kind=work, target_id=%s)。"
                 % (work or {}).get("id", ""))
    lines.append("")
    lines.append("## 現在這一棒：要交的東西")
    lines.append("")
    lines.extend(prompts.contract_lines(contract, brief=True))
    lines.append("")
    lines.append("**每一欄的標準與範例**在 `fbflow_read(target_kind=work)` 的"
                 "`contract` → `field_specs`（規格留在磁碟上，不要靠猜）。")
    if method_notes:
        lines.append("")
        lines.append("## 方法（怎麼做這件事）")
        # **壓成一行**（使用者裁決 2026-11-25：送進 session 的只有提示詞，
        # 工作狀態留在磁碟上）。完整方法精神在專案 SKILL 的對應段落裡。
        lines.append("- %s" % method_notes[0])
        lines.append("- 其餘方法要點見專案 SKILL 的〈方法〉段（完整內容在磁碟上）。")
    lines.append("")
    lines.append("## 怎麼交")
    lines.append("用 MCP 工具回報（不是 CLI）。")
    lines.append("  fbflow_report_work(task_id, work_id, expected_version, payload, message_id)")
    lines.append("  - payload 要含上面「現在這一棒」列出的每一個欄位，外加 `handoff`。")
    lines.append("  - stage 可留空：引擎依 task_id 判斷這一棒。")
    lines.append("  - message_id **照抄上面派工訊息裡那個**（引擎為這一輪發的）。"
                 "不要自己編，**不可沿用上一輪的**——"
                 "沿用／自編會被引擎以 receipt.message_reused 退件。")
    lines.append("  - 收到退件時版本會 +1：先用 fbflow_read 重讀再重交。")
    lines.append("")
    lines.append("讀取：")
    lines.append("  fbflow_read(target_kind=work, target_id=%s)" % (work or {}).get("id", ""))
    lines.append("  fbflow_status()")
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)
