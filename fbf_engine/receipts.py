"""FBF ENGINE 的**平台回執**核對層（只讀）。

沿用 v1 專案回執核對的規則（同一份規則，不重寫第二套）；本模組是它的移植版。

「回執」指 FBFLOW ENGINE 從執行平台取得的執行紀錄。
Session 自填的 `model_id`、`context_id`、`status` 只能作為**待核對線索**，
不能變成可信 run。平台查不到、或不能證明回執屬於該 Session 時，
該關卡保持原狀態，記錄 `receipt.unverified`。

核對條件（全部成立才算 verified）：
1. 訊息屬於已認領的 Session；
2. 任務 ID 與工單輪次相符（任務 ID 是關聯鍵，不是身分憑證）；
3. assistant 回執**晚於**任務派送，且**與提交時間對得上**
   （實測平台把 `completed` 標在工具收尾之後，會比交件晚不到一秒 → 留 5 秒容差）；
4. 平台回報實際模型（provider/model）；
5. 平台回報 context／執行 ID 與完成狀態。

這個模組只讀平台，不寫任何資料；寫入由 engine 在交易內完成。
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from . import ocserver, hostterm

#: 未核對時統一使用的理由代碼
UNVERIFIED = "receipt.unverified"

#: **平台本身查不到**時的理由（與 `receipt.unverified`＝對不上，分開）。
QUERY_FAILED = "receipt.query_failed"

#: agent 看不見平台編的 message id；它自報的值只當**備援錨點**（對不上
#: 派工訊息時用來交叉檢查），不是回執的關聯鍵（關聯鍵是 `task_id`）。
MESSAGE_FALLBACK = "receipt.message_fallback"

#: 平台時間與資料庫時間的允許誤差（秒）。
#:
#: **實測校正**：assistant 訊息的 `completed` 比 MCP 交件**晚一點點**
#: （工具執行完才收尾，同一次交件的落差 < 1 秒）。
#: 拿「必須早於提交」去卡，會把正確的回執誤殺 → 用容差換取正確性。
TIME_TOLERANCE_SECONDS = 5.0


class ReceiptUnavailable(Exception):
    """平台查不到或回應無法解析。

    呼叫端必須把它當成「查不到」，**不得**當成閒置或已完成。
    """


def _ok(**kwargs: Any) -> Dict[str, Any]:
    result = {"verified": True, "reason": "", "platform": "", "message_id": "",
              "session_id": "", "model_id": "", "context_id": "", "finish": "",
              "detail": {}}
    result.update(kwargs)
    return result


def _fail(reason: str, **kwargs: Any) -> Dict[str, Any]:
    result = {"verified": False, "reason": reason, "platform": "", "message_id": "",
              "session_id": "", "model_id": "", "context_id": "", "finish": "",
              "detail": {}}
    result.update(kwargs)
    return result


def to_epoch(value: Any) -> Optional[float]:
    """把平台時間戳或資料庫時間字串換算成 epoch 秒。

    平台通常給毫秒（> 1e11）；資料庫用 'YYYY-%m-%dT%H:%M:%S' 本地時間。
    換算不了就回 None（呼叫端據此判定無法核對）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e11 else number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return to_epoch(int(text))
        try:
            return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
    return None


def _info(message: Dict[str, Any]) -> Dict[str, Any]:
    """回執訊息的「資訊層」。

    **兩種外殼都吃**（平台改版不能讓核對整條壞掉）：
    - 新版（實測 OpenCode 2.2.5）：**扁平**物件——角色在 `type`、
      使用者文字在 `text`、assistant 內容在 `content[]`、
      模型在 `model:{id, providerID}`。
    - 舊版：`{info: {...}, parts: [...]}` 包裝——角色在 `role`。
    """
    if not isinstance(message, dict):
        return {}
    inner = message.get("info")
    if isinstance(inner, dict):
        merged = dict(inner)
        for key in ("id", "role", "type", "sessionID", "parts", "text", "content", "model"):
            if key in message and key not in merged:
                merged[key] = message[key]
        return merged
    return message


def _role_of(info: Dict[str, Any]) -> str:
    """角色欄位：**平台可能用 `role` 或 `type`**（實測 2.2.5 用 `type` ✓）。"""
    return str(info.get("role") or info.get("type") or "")


def _finish_of(info: Dict[str, Any]) -> str:
    """平台回報的結束原因（`stop` / `length` / `tool-calls` / `error` …）。

    **只看平台明講的 `finish`**——不能拿「有 completed 時間戳」推論完成：
    實測 2.2.5 的資料裡，帶 tool call 的**中間步驟**訊息也帶 `time.completed`，
    拿它推論會把「還在跑」誤判成「已完成」。
    """
    finish = info.get("finish")
    return finish if isinstance(finish, str) else ""


def _created(message: Dict[str, Any]) -> Optional[float]:
    info = _info(message)
    stamp = info.get("time")
    if isinstance(stamp, dict):
        return to_epoch(stamp.get("created"))
    return to_epoch(info.get("created"))


def _completed_at(info: Dict[str, Any]) -> Optional[float]:
    """assistant 訊息的「生成結束」時間。

    平台註解：`completed` 是「供應商回應本文結束、工具還沒收尾」的時間點
    → **早於**工具執行，也就是早於 agent 呼叫回報工具的那一刻。
    這一點就是回執時序比對的錨點。
    """
    stamp = info.get("time")
    if isinstance(stamp, dict):
        return to_epoch(stamp.get("completed"))
    return None


def _text_of(message: Dict[str, Any]) -> str:
    """把訊息的文字攤平。

    - 使用者訊息：文字在 `text`（新版）／`parts[].text`（舊版）。
    - assistant 訊息：文字在 `content[]` 裡 `type == "text"` 的項目。
    """
    chunks: List[str] = []
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    info = _info(message)
    content = info.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "text"):
                continue
            if isinstance(item.get("text"), str):
                chunks.append(item["text"])
    for key in ("text", "prompt"):
        value = info.get(key)
        if isinstance(value, str):
            chunks.append(value)
    if isinstance(info.get("content"), str):
        chunks.append(info["content"])
    return "\n".join(chunks)


def _model_of(info: Dict[str, Any]) -> str:
    """平台回報的實際模型。

    新版（2.2.5）回 `model: {id, providerID, variant?}`；
    舊版回平坦的 `providerID` + `modelID`。
    """
    provider = info.get("providerID") or info.get("provider") or ""
    model = info.get("modelID") or ""
    ref = info.get("model")
    if isinstance(ref, dict):
        provider = provider or ref.get("providerID") or ""
        model = model or ref.get("id") or ref.get("modelID") or ""
    elif isinstance(ref, str):
        model = model or ref
    if provider and model:
        return "%s/%s" % (provider, model)
    return model or ""


def _tool_calls(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """assistant 訊息裡的**工具呼叫**。

    實測（OpenCode 2.2.5 + MCP）：agent 是用 `execute` 工具跑一段 JavaScript
    去呼叫 MCP 工具——
    `tools["fbf-engine"].fbflow_report_work({task_id: "T-000001", ...})`
    → **參數在程式碼文字裡**，不在結構化的 input 欄位。
    所以每個呼叫都附一份「可搜尋的文字」。
    """
    calls: List[Dict[str, Any]] = []
    content = info.get("content")
    if not isinstance(content, list):
        return calls
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool":
            continue
        name = item.get("name")
        state = item.get("state")
        args: Dict[str, Any] = {}
        if isinstance(state, dict):
            raw = state.get("input")
            if isinstance(raw, dict):
                args = raw
            elif isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    args = parsed
        calls.append({"name": name if isinstance(name, str) else "",
                      "args": args, "text": _call_text(args)})
    return calls


def _call_text(args: Dict[str, Any]) -> str:
    """工具呼叫裡可搜尋的文字（原始碼 ＋ 各參數值）。"""
    chunks: List[str] = []
    code = args.get("code")
    if isinstance(code, str):
        chunks.append(code)
    for key, value in args.items():
        if key == "code":
            continue
        if isinstance(value, str):
            chunks.append("%s: %s" % (key, value))
        else:
            try:
                chunks.append("%s: %s" % (key, json.dumps(value, ensure_ascii=False)))
            except (TypeError, ValueError):
                continue
    return "\n".join(chunks)


#: 交件用的工具名（agent 用它把成果回報回來）
REPORT_TOOLS = ("report_work", "report_issue")


def _reports_task(info: Dict[str, Any], task_id: str) -> bool:
    """這則 assistant 訊息有沒有「用交件工具交出這個 task_id」的呼叫。

    **同一個工具呼叫裡**必須同時出現交件工具名與 task_id 才算數——
    只出現工具名不夠（可能是別的工單），只出現 task_id 也不夠。
    """
    if not task_id:
        return False
    pattern = re.compile(r"task_id[\"']?\s*[:=]\s*[\"']?%s" % re.escape(task_id))
    for call in _tool_calls(info):
        text = call["text"]
        if not any(name in text for name in REPORT_TOOLS):
            continue
        if pattern.search(text):
            return True
    return False


# ---------------------------------------------------------------- OpenCode

def _api(endpoint: str, path: str, binary: str = "opencode") -> Any:
    """呼叫 OpenCode 官方 API。

    **一定帶 `--server`**（上游原始碼 v2.2.5
    `packages/cli/src/services/server-connection.ts`）：沒帶 `--server` 時，
    CLI 會 `Service.ensure()` → **自動啟動一個常駐背景 server**（連帶它裡面的 MCP）
    ——我們不要那種東西。

    端點由 `ocserver` **唯讀**探索既有 server；連不到就明確報錯，
    **不要**讓 CLI 去開一個（`ReceiptUnavailable` → 呼叫端標 `unverified`）✓。
    """
    try:
        url, password = ocserver.require(endpoint)
    except ocserver.ServerMissing as exc:
        raise ReceiptUnavailable(str(exc))
    argv = ocserver.cli_argv(binary, url, ["api", "GET", path])
    try:
        # 大型 JSON 寫入一般檔案，避免 CLI 結束時 pipe 尚未排空。
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run(argv, stdout=output, stderr=subprocess.PIPE,
                                    text=True, timeout=30, encoding="utf-8",
                                    errors="replace", env=ocserver.env_for(password))
            output.seek(0)
            raw = output.read().decode("utf-8")


    except subprocess.TimeoutExpired:
        raise ReceiptUnavailable("OpenCode API 查詢逾時：%s" % path)
    except OSError as exc:
        raise ReceiptUnavailable("OpenCode API 查詢失敗：%s" % exc)
    if result.returncode != 0:
        raise ReceiptUnavailable("OpenCode API 查詢失敗（退出碼 %d）：%s"
                                 % (result.returncode, (result.stderr or result.stdout)[-300:]))
    raw = raw.strip()
    if not raw:
        raise ReceiptUnavailable("OpenCode API 回應為空：%s" % path)
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ReceiptUnavailable("OpenCode API 回應非合法 JSON：%s" % exc)


#: 平台單頁訊息數（`limit` 參數上限 200）
PAGE_LIMIT = 200

#: 最多翻幾頁（長 session 也不會無限翻）
MAX_PAGES = 20


def _session_messages(endpoint: str, session_id: str,
                      binary: str = "opencode") -> List[Dict[str, Any]]:
    """讀取某個 Session 的訊息清單（只讀）。

    官方端點：`GET /api/session/{sessionID}/message`
    回應：`{"data": [...], "cursor": {"previous": ..., "next": ...}}`

    **預設只回 50 筆**（尾端視窗），長 session 的派工訊息會被截掉 →
    必須照 `cursor.next` 往前翻頁，否則核對一定失敗。
    """
    messages: List[Dict[str, Any]] = []
    path = "/api/session/%s/message?limit=%d&order=asc" % (session_id, PAGE_LIMIT)
    for _ in range(MAX_PAGES):
        data = _api(endpoint, path, binary)
        items = _extract_items(data)
        if not items:
            break
        messages.extend(items)
        cursor = _next_cursor(data)
        if not cursor:
            break
        path = "/api/session/%s/message?limit=%d&cursor=%s" % (
            session_id, PAGE_LIMIT, quote(cursor, safe=""))
    return messages


def _extract_items(data: Any) -> List[Dict[str, Any]]:
    """從平台回應取出訊息陣列。

    平台統一用 `{"data": ...}` 包：列表端點的 `data` 是陣列；
    export 端點的 `data` 是 `{info, messages}`。
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "messages", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("messages", "items"):
                value = inner.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
    return []


def _next_cursor(data: Any) -> str:
    """下一頁游標（沒有就是最後一頁）。"""
    if not isinstance(data, dict):
        return ""
    cursor = data.get("cursor")
    if isinstance(cursor, dict):
        value = cursor.get("next")
        if isinstance(value, str):
            return value
    return ""


def _session_payload(raw: Any) -> Dict[str, Any]:
    """`GET /api/session/{id}` 的內容（平台用 `{"data": {...}}` 包）。"""
    if isinstance(raw, dict):
        inner = raw.get("data")
        if isinstance(inner, dict):
            return inner
        return raw
    return {}


def _tool_output_text(info: Dict[str, Any]) -> str:
    """工具呼叫的**回應文字**。

    平台把工具輸出放在 `state.content[].text`（或 `state.output`）。
    實測：交件工具的回應裡有 `"submission_id": "S-000003"` →
    **交件真的落地了**的證據（語法錯誤的嘗試只會得到錯誤訊息）。
    """
    chunks: List[str] = []
    content = info.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool":
            continue
        state = item.get("state")
        if not isinstance(state, dict):
            continue
        output = state.get("output")
        if isinstance(output, str):
            chunks.append(output)
        items = state.get("content")
        if isinstance(items, list):
            for one in items:
                if isinstance(one, dict) and isinstance(one.get("text"), str):
                    chunks.append(one["text"])
    return "\n".join(chunks)


def _at_or_before(info: Dict[str, Any], filed_at: Optional[float]) -> bool:
    """這則訊息是不是在「提交時間（含誤差容忍）」之前就完成。

    **實測校正**：assistant 訊息的 `completed` 比 MCP 交件**晚一點點**
    （工具執行完才收尾，同一次交件的落差 < 1 秒）。
    所以容差是必要的——拿「必須早於提交」去卡，會把正確的回執誤殺。
    """
    if filed_at is None:
        return True
    stamp = _completed_at(info)
    if stamp is None:
        stamp = _created(info)
    if stamp is None:
        return True
    return stamp <= filed_at + TIME_TOLERANCE_SECONDS


def _pick_closest(candidates: List[Tuple[Any, Dict[str, Any]]],
                  filed_at: Optional[float]) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """挑離提交時間**最近**的那一則（沒有提交時間就挑最早的）。

    agent 交件失敗會重試（實測同一工單交三次，前兩次是語法錯誤沒交成），
    所以「有交件呼叫」不夠——要挑**最接近成功那次**的那一則。
    """
    if not candidates:
        return None
    if filed_at is None:
        return min(candidates,
                   key=lambda pair: _completed_at(pair[1]) or _created(pair[1]) or 0)

    def distance(pair: Tuple[Any, Dict[str, Any]]) -> float:
        stamp = _completed_at(pair[1])
        if stamp is None:
            stamp = _created(pair[1])
        if stamp is None:
            return float("inf")
        return abs(stamp - filed_at)

    return min(candidates, key=distance)


def verify_opencode(endpoint: str, session_id: str, message_id: str, task_id: str,
                    dispatched_at: Any = None, submitted_at: Any = None,
                    binary: str = "opencode") -> Dict[str, Any]:
    """核對 OpenCode 的平台回執。回傳 verified／reason 結構。

    任何一步查不到或對不上，都回 verified=False 並附上理由代碼；
    **不會**因為查不到就當成完成。

    **沒有 endpoint 也照查**：`opencode api …` 不帶 `--server` 時會自己找到
    本機正在跑的 server（埠號每次重開都會變，寫死一定會壞）✓。

    **關聯鍵是 task_id，不是 agent 自報的 message_id**——
    agent 收得到的是「注入給它的那段提示詞」，**看不到平台給訊息編的 id**，
    所以它自報的 id 必然是編的，拿它去平台比對只會永遠失敗。
    派工提示詞裡一定帶 task_id → 那才是把平台訊息與這次任務對上的鍵 ✓。

    **回執錨點**：OpenCode 的一輪對話會拆成多則 assistant 訊息——
    產生回覆 → 跑工具 → 再產生下一則。**最後**那則 `finish=stop` 的訊息
    反而**晚於**交件（實測晚不到一秒）。所以回執要抓「**用交件工具交出這個
    task_id**」的那一則，不是抓最後一則；agent 交件失敗會重試
    （實測同一工單交三次，前兩次是語法錯誤），要挑**真的交成**的那次。
    """
    if not session_id:
        return _fail(UNVERIFIED, reason_detail="缺少 Session ID")
    if not message_id:
        return _fail(UNVERIFIED, reason_detail="缺少平台 message ID")

    # 1) Session 必須還在，而且屬於這個 server
    try:
        session = _api(endpoint, "/api/session/%s" % session_id, binary)
    except ReceiptUnavailable as exc:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id, reason_detail=str(exc))
    if not _session_payload(session):
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id, reason_detail="平台查無此 Session")

    # 2) 讀這個 Session 的全部訊息
    try:
        messages = _session_messages(endpoint, session_id, binary)
    except ReceiptUnavailable as exc:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id, reason_detail=str(exc))
    if not messages:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id, reason_detail="平台回不到這個 Session 的訊息")

    # 3) 找出這次的**派工訊息**（含 task_id 的那一則使用者訊息）
    target = None
    for message in messages:
        info = _info(message)
        if _role_of(info) != "user":
            continue
        if task_id and task_id in _text_of(message):
            target = info
            break
    if target is None:
        # agent 自報的 message id 若**正好就在平台上**，用它當交叉檢查。
        # **找不到不代表沒交件**——agent 看不到平台給訊息編的 id，
        # 自報值通常是編的（附則五）。這種情況下不在此回 unverified，
        # 讓下面的 5a 直接用「交件工具呼叫裡出現 task_id」的錨點核對；
        # 核對通過時用**平台自己的訊息 id** 記回執（`context_id`＝assistant 訊息 id）。
        for message in messages:
            info = _info(message)
            if str(info.get("id", "")) == str(message_id):
                target = info
                break
    if target is not None and target.get("sessionID") \
            and str(target["sessionID"]) != str(session_id):
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id,
                     reason_detail="message 不屬於已認領的 Session")
    if target is not None:
        target_role = _role_of(target)
        if target_role and target_role != "user":
            return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                         message_id=message_id,
                         reason_detail="引用的 message 不是使用者訊息")

        # 4) 任務 ID 必須在派送訊息裡（關聯鍵，不是身分憑證）
        if task_id and task_id not in _text_of(target):
            return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                         message_id=message_id,
                         reason_detail="派送訊息裡沒有這次任務的 task ID")

    # 5) 找出對應的 assistant 回執
    sent_at = to_epoch(dispatched_at)
    filed_at = to_epoch(submitted_at)
    # **派工訊息找不到時 anchor 為 None**：錨點改用 5a 的「交件工具呼叫裡出現
    # task_id」——那是更強的證據（派工訊息只證明「我們送過」，工具呼叫證明
    # 「它真的交了這張單」）。**不能**因此判 unverified，那會讓回執永遠核不過。
    anchor_at = _created(target) if target is not None else None

    assistants = []
    for message in messages:
        info = _info(message)
        if _role_of(info) != "assistant":
            continue
        created = _created(info)
        if anchor_at is not None and created is not None and created < anchor_at:
            continue
        if sent_at is not None and created is not None and created < sent_at:
            continue
        assistants.append((created, info))
    if not assistants:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id, reason_detail="找不到對應的 assistant 回執")

    # 5a) **最強的錨點**：assistant 用交件工具交出這個 task_id 的那一則
    candidates = [(created, info) for created, info in assistants
                  if _reports_task(info, task_id)]
    picked = None
    how = ""
    if candidates:
        # 交件**真的落地**的那一則（工具回應裡有 submission_id）優先
        landed = [pair for pair in candidates
                  if "submission_id" in _tool_output_text(pair[1])]
        pool = landed or candidates
        # 重試過 → 挑最接近提交時間的那一則
        picked = _pick_closest(pool, filed_at)
        how = "report_tool"

    # 5b) 沒有工具錨點 → 退到「時間序」：離提交時間最近的那一則
    if picked is None:
        # **派工訊息與交件工具錨點都沒有 → 不准用純時間錨點過關**
        # （那等於「查不到就當完成」）。只有在**派工訊息找得到**、只是工具呼叫
        # 沒被解析到時，才允許時間序備援。
        if target is None:
            return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                         message_id=message_id,
                         reason_detail="平台查不到派工訊息，也查不到交出這個 task_id 的"
                                       "工具呼叫——證明不了這張單交過")
        pool = [pair for pair in assistants if _at_or_before(pair[1], filed_at)]
        if not pool:
            pool = assistants
        picked = _pick_closest(pool, filed_at)
        how = "time_order"
    if picked is None:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id,
                     reason_detail="assistant 回執晚於提交")

    created, assistant = picked
    if not _at_or_before(assistant, filed_at):
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id,
                     reason_detail="assistant 回執晚於提交")

    model_id = _model_of(assistant)
    if not model_id:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id, reason_detail="平台沒有回報實際模型")
    finish = _finish_of(assistant)
    if not finish:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id,
                     reason_detail="平台沒有回報完成狀態")

    context_id = str(assistant.get("id") or "")
    if not context_id:
        return _fail(UNVERIFIED, platform="opencode", session_id=session_id,
                     message_id=message_id, reason_detail="平台沒有回報 context／執行 ID")

    return _ok(platform="opencode", session_id=session_id, message_id=message_id,
               model_id=model_id, context_id=context_id, finish=finish,
               detail={"assistant_created": created,
                       "assistant_completed": _completed_at(assistant),
                       "dispatched_at": sent_at,
                       "submitted_at": filed_at,
                       "anchor": how})


def verify_unavailable(platform: str = "", **_: Any) -> Dict[str, Any]:
    """沒有可用平台查詢時的結果：未核對。

    「查不到」永遠不能變成「已完成」。
    """
    return _fail(UNVERIFIED, platform=platform,
                 reason_detail="這個 agent 沒有可用的平台回執查詢")


# ---------------------------------------------------------------- 送達狀態

#: 派送訊息對方的收件狀態
DELIVERED = "delivered"
NOT_DELIVERED = "not-delivered"
DELIVERY_UNKNOWN = "unknown"


def _classify_delivery(messages: List[Dict[str, Any]], task_id: str,
                       since: Optional[float]) -> str:
    """看完一批訊息，判斷這條派送到底送達了沒。

    **關鍵：沒看到 task_id 不等於沒送達。**
    訊息端點只回尾端視窗，舊訊息會被截掉——所以「查不到」只有在
    **視窗涵蓋了派送時刻**時才能當成沒送達。
    """
    for message in messages:
        if task_id in _text_of(message):
            return DELIVERED
    if since is None:
        return DELIVERY_UNKNOWN
    oldest = None
    for message in messages:
        created = _created(message)
        if created is None:
            continue
        if oldest is None or created < oldest:
            oldest = created
    if oldest is None:
        # 拿不到時間 → 判斷不了視窗有沒有涵蓋
        return DELIVERY_UNKNOWN
    if oldest <= since:
        # 視窗從派送之前就開始了 → 這段期間是完整的，沒找到就是沒送達
        return NOT_DELIVERED
    # 視窗被截斷 → 不能證明沒送達
    return DELIVERY_UNKNOWN


def delivery_state(endpoint: str, session_id: str, task_id: str,
                   since: Optional[float] = None, binary: str = "opencode") -> str:
    """這條派送訊息，收件的 session 到底收到了沒？

    規格第 6 節：
    > 通知可能已送達而回寫失敗：狀態不明時**先查 task／session，不得盲目重送**。

    回傳三種，語意要分清楚：
    - `delivered`：對方確實收到了（**不要再送**）。
    - `not-delivered`：**已證明**沒送達（可以重送）。
    - `unknown`：證明不了（**不要重送**，等下一輪再查）。

    `since` 是派送時刻（epoch 秒）。**沒帶就一律回 unknown**——
    沒有派送時刻就無從判斷視窗是否完整，而「不知道」不能當成「沒送達」。

    **按通道路由**：`term_…` 是宿主終端的 handle，送達要用
    `hostterm.delivery_state`（`agent read`／`terminal read`）查，不能拿去
    問 OpenCode API——OpenCode 不認得這個 id，只會回 400，把「明明送達了」
    誤判成 unknown。沒有宿主 server 的 OpenCode session 才走下面的 API。

    **沒有 endpoint 也照查**：`opencode api …` 不帶 `--server` 時會自己找到
    本機正在跑的 server（埠號每次重開都會變，寫死一定會壞）✓。
    """
    if not session_id or not task_id:
        return DELIVERY_UNKNOWN
    if session_id.startswith(hostterm.HOST_HANDLE_PREFIXES):
        return hostterm.delivery_state(session_id, task_id,
                                       dispatched_at=since)
    try:
        messages = _session_messages(endpoint, session_id, binary)
    except ReceiptUnavailable:
        return DELIVERY_UNKNOWN
    state = _classify_delivery(messages, task_id, since)
    if state != DELIVERY_UNKNOWN:
        return state

    # 視窗可能不完整 → 試一次完整匯出
    try:
        export = _api(endpoint, "/api/experimental/session/%s/export" % session_id, binary)
    except ReceiptUnavailable:
        return DELIVERY_UNKNOWN
    items = _extract_items(export)
    if not items:
        return DELIVERY_UNKNOWN
    return _classify_delivery(items, task_id, since)


# ---------------------------------------------------------------- 宿主（host CLI）通道的回執核對器

def _verify_host(endpoint: str, session_id: str, message_id: str, task_id: str,
                 dispatched_at: Any = None, submitted_at: Any = None,
                 binary: str = "", **_: Any) -> Dict[str, Any]:
    """把 engine 的 verifier 呼叫約定對到 `hostterm.verify_host`。

    `session_id` 就是 host 終端 handle（`term_…`）；回執錨點是 task_id，
    與 OpenCode 相同。`message_id`/`endpoint`/`binary` 對 host CLI 沒意義，
    直接忽略（`**_`）。
    """
    return hostterm.verify_host(session_id, task_id,
                                dispatched_at=dispatched_at,
                                submitted_at=submitted_at)


#: agent 名稱 → 回執核對器（claude 走 host terminal，opencode 走原生 API）
VERIFIERS = {
    "opencode": verify_opencode,
    "claude": _verify_host,
}


def verifier_for(agent: str) -> Any:
    return VERIFIERS.get((agent or "").lower(), verify_unavailable)


# ---------------------------------------------------------------- 送達狀態
