"""FBF ENGINE 的**協調層**：對照正本、套用提交、佇列派工、重試與計數。

這一層是**常駐進程的心臟**。它持有 timer、持有租約、持有狀態機的唯一寫入權。
MCP 只是它的介面（規格第 6 節）。

**最重要的一條**：同一個 submission 只能被套用一次。
做到這件事靠三件事同時成立：
1. `submissions` 對 (task_id, session_id, stage, expected_version) 有 UNIQUE 約束；
2. 狀態更新用 `WHERE id=? AND version=?` 的樂觀鎖，更新筆數 0 → 標 `stale`；
3. 租約世代在每次寫入前驗證——舊進程不能寫進新進程的世代。

交易順序（規格第 6 節，逐字照做）：
    取候選 submission → **交易外**查平台回執
    BEGIN IMMEDIATE
        重讀 task、work.version、指派、submission.status、租約世代
        不符      → submission=stale
        回執不足  → submission=unverified
        守衛不過  → submission=rejected
        全通過    → 寫內容／結果、version+1、下一 task/outbox、decision、applied
    COMMIT
    **交易外**送 outbox；送後回寫結果

**平台呼叫一律不得在 SQLite 交易內**（會把寫鎖握住不放）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import compact, hostact, platform, prompts, receipts, store, workflow

#: outbox 狀態。詞彙沿用既有 INFORM 通道（v1 專案的 `workflow_outbox` 表），
#: 讓兩套系統對同一件事的描述一致：
#: `queued`（待送）→ `sending`（送出中）→ `submitted`（平台已接受，未確認）
#: → `delivered`（已確認收到）；失敗 `failed`；狀態不明 `unknown`。
OUT_QUEUED = "queued"
OUT_SENDING = "sending"
OUT_SUBMITTED = "submitted"
OUT_DELIVERED = "delivered"
OUT_FAILED = "failed"
OUT_UNKNOWN = "unknown"

#: submission 狀態
SUB_PENDING = "pending"
SUB_APPLIED = "applied"
SUB_STALE = "stale"
SUB_UNVERIFIED = "unverified"
SUB_REJECTED = "rejected"
#: 回執等到截止仍未核對 → 這筆提交**到期了**（終態，不再被 tick 重查）。
#: 跟 `rejected`（內容退件、可補件重交）不同：平台回執對不上就證明不了任何事，
#: 留著每輪重查會**無限重查、卡死流程**（W-004／W-013 的 `receipt.unverified`
#: 就是這樣卡住的）→ 標 `expired`、作廢舊 task、換新 task 重試；
#: 重試達上限就標待人處理，留**可見的人工出口**。
SUB_EXPIRED = "expired"

#: 派送退避（秒）。指數退避，最多 5 次。
BACKOFF_SECONDS = (5, 15, 45, 135, 300)

#: presence 暫緩（忙碌／查不到）時的最大暫緩次數。
#: 超過就不必再等，直接標待人處理——**不允許無限等待**（規格第 6 節）。
#: 預設 240 × 30 秒 = 2 小時。本地模型比雲端慢得多，「忙碌」是常態，
#: 所以這個上限要寬鬆；但它必須存在，否則沒人會知道有張單卡在派不出去。
MAX_DEFERRALS = 240

#: unverified 提交的重查期限（秒）
UNVERIFIED_DEADLINE_SECONDS = 600

#: 回執被別的提交用過（永久性問題 → 退回要求重交，不是重試）
REASON_RECEIPT_MESSAGE_REUSED = "receipt.message_reused"

#: 席位被占住卻超過這個小時數沒有活動 → 標成 `stale`（孤兒席位的線索）。
#: 這是**線索**，不是自動釋放——釋放是人的決定。
SEAT_STALE_HOURS = 12


def _idle_hours(timestamp: str) -> float:
    """從時間字串算出「距今幾小時」。沒有紀錄 → 回 -1（= 從來沒有活動）。"""
    if not timestamp:
        return -1.0
    try:
        moment = time.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return -1.0
    return max(0.0, (time.time() - time.mktime(moment)) / 3600.0)

#: 一次 tick 的最大提交處理數（避免單趟卡住整個循環）
MAX_SUBMISSIONS_PER_TICK = 50


class _StaleSubmission(Exception):
    """套用時樂觀鎖沒中（版本被別人改過）→ **整筆交易回滾**、標 `stale`。

    不能就地 `return`：`apply_stage`／`record_handoff` 已經寫進去了，
    留著就是半套用的狀態。丟例外讓 `store.tx` 回滾，再在交易外標 stale。
    """

#: 同一個階段最多退件幾次（超過就標待人處理，不允許無限繞圈）。
#:
#: 退件是**流程迴圈**不是資源配額——它數的是「這張單在同一棒上被退幾次」。
#: 三次還改不過來，通常是契約理解錯了，要人介入。
MAX_STAGE_REJECTIONS = 3

#: **角色完全沒動靜多久**（秒）→ 引擎收掉那一棒並重派。
#:
#: 這是「最後一個會讓單子靜靜卡住的洞」：其他重派路徑都假設角色會回應。
#: 計時基準是**角色最近一次交件**，不是某一棒的建立時間——
#: 同一個 session 排了多棒時，後面幾棒是「在排隊」而不是「失聯」。
ROLE_STALL_SECONDS = 30 * 60

#: **模型端停滯多久才記一個健康事件**（秒）。
#:
#: 「等模型回應」**不算**「沒在動」——實測（2026-11-25批次處理）：
#: WORKER 被判了 15 次逾時，但每一分鐘它的 transcript 都有紀錄，
#: 最後一筆全是模型請求（`api-request`）；其中 4 次是模型端直接回錯
#: （`API Error: 422 … content safety screening`）。
#: 引擎把「等模型」當「失聯」→ 殺掉 task、重派，還把 4 張新派工
#: 一次插進它正跑的對話裡。
#:
#: 但模型端**真的卡死**（很久都不回）時也要讓人知道 → 記健康事件，
#: **但不收掉 task**（不是 agent 的錯，不該殺掉別人正在做的事）。
MODEL_STALL_SECONDS = 3 * 60 * 60

#: **查不到宿主紀錄時，門檻乘幾倍**——保守：寧可晚點催，也不要誤殺。
UNKNOWN_ACTIVITY_MULTIPLIER = 2

#: 一張單多久沒有任何提交就算「停滯」（分鐘）——巡檢用。
STUCK_MINUTES = 45

#: 待人處理停多久算「等人太久」（分鐘）——巡檢用。
AWAITING_TOO_LONG_MINUTES = 240

#: 派工後多久還沒交件就**喚醒**一次（秒）。
WAKE_UP_SECONDS = 10 * 60

#: 同一棒最多喚醒幾次（避免一直叫）。
MAX_WAKE_UPS = 3

#: 兩次喚醒之間至少隔多久（秒）。
WAKE_UP_REPEAT_SECONDS = 10 * 60

#: **閒置落定期**（秒）：session 多久沒有任何活動，才算「真的閒下來了」。
#:
#: 為什麼要有這一道：使用者提醒——「MCP 一派單，session 就去 compact」
#: 是壞的。剛交完件、剛被派過單的 session **此刻沒有開著的 task**，
#: 但它一點都不閒——它正要接下一棒、或正在等回執。
#: 落定期讓提醒只落在**真的閒置一陣子**的 session 上。
COMPACT_IDLE_SECONDS = 10 * 60

#: 回執核對到期（`UNVERIFIED_DEADLINE_SECONDS`）之後，**換新 task 重試**的次數上限。
#: 超過 → 工單標 `awaiting_human`（reason=`receipt_timeout`），留可見的人工出口。
#: 不做上限就會無限「過期 → 重試 → 過期」，流程原地打轉且沒有人知道。
MAX_RECEIPT_EXPIRIES = 3

#: SUBAGENT context 核對到期後，換新 task 重試的次數上限。
#: 超過 → 工單標 `awaiting_human`（reason=`context_timeout`）。
#: 這一組是**獨立的**：回執到期與 context 到期是兩件事，各算各的。
MAX_CONTEXT_EXPIRIES = 3

#: ISSUE 未結案的退回上限（spec 第 4(b) 步：「**連續兩次** ISSUE 單未能結案，
#: 流程停下來等人類裁決」）。
MAX_ISSUE_RETURNS = 2

#: **專案結案檢視**用的固定 ID。
#:
#: 一個專案一輪接一輪（補工單 → 再檢視），所以用固定 ID ＋ `version` 樂觀鎖，
#: 不用流水號（流水號會讓「這一輪是第幾輪」散在多列裡）。
PROJECT_REVIEW_ID = "PRJ-001"

#: 派工提示詞裡帶多少筆退件紀錄給 PLANNER 復盤（太多就看不清重點）。
MAX_REJECTION_SUMMARY = 30


#: 協調器版本。**會寫進租約的 owner**——
#: 這樣「現在跑的是不是新版」可以直接從正本看出來，不用猜
#: （實務上踩過：舊行程活著，新程式碼改了也沒人重載 → 整包卡住）。
#:
#: 格式：**只能用數字與底線**（不含 `-`）——owner 是用 `-` 分段解析的。
#:
#: **改了引擎就要換版本號**：新版靠它判斷「持有者是不是舊版」並強制接手 ✓
#: （同版本不會搶 → 舊行程會一直霸著租約，新程式碼永遠載入不了 ✓）。
ENGINE_VERSION = "20261125i"


class Coordinator:
    """常駐協調器。持有正本連線與租約。"""

    def __init__(self, project_root: str, owner: str = "",
                 endpoint: str = "", binary: str = "opencode",
                 verifier: Optional[Any] = None):
        self.root = os.path.abspath(os.path.expanduser(project_root))
        self.owner = owner or ("fbf-%d-%s-%s" % (os.getpid(), uuid.uuid4().hex[:8],
                                                  ENGINE_VERSION))
        self.binary = binary
        #: 回執核對器。**預設走真實平台**；測試才注入替身。
        #: 注入替身不影響正式路徑，但**不得**用它宣稱真實平台已驗收。
        self.verifier = verifier
        #: **每個執行緒一條連線**（見 `conn` 屬性）。
        self._local = threading.local()
        self._conns: List[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._closed = False
        store.init_db(self.conn)
        self.generation = 0
        #: 明確指定的 endpoint（CLI 傳入的）優先於設定值。
        self._endpoint = endpoint or ""
        #: **背景續約**（見 `_start_lease_renewer`）——tick 可能比 TTL 還久。
        self._renew_thread: Optional[threading.Thread] = None
        self._renew_stop = threading.Event()
        self.lease_renew_interval = max(1.0, store.LEASE_TTL_SECONDS / 3.0)

    @property
    def endpoint(self) -> str:
        """OpenCode server 位址。

        **每輪重讀設定**——人在專案裡補上 endpoint 之後，
        正在跑的 MCP 伺服器**不必重啟**就會用新的。
        （之前是啟動時讀一次，改了設定要重啟整個 OpenCode 才知道。）
        """
        if self._endpoint:
            return self._endpoint
        return store.get_setting(self.conn, "opencode_endpoint", "")

    @endpoint.setter
    def endpoint(self, value: str) -> None:
        self._endpoint = value or ""

    @property
    def conn(self) -> sqlite3.Connection:
        """**這個執行緒自己的**連線。

        **背景 tick 執行緒與 MCP 請求執行緒不能共用同一條 sqlite3 連線。**
        CPython 用 `SQLITE_OPEN_NOMUTEX` 開連線——底層不再替這條連線序列化，
        兩個執行緒同時用它是**未定義行為**：實測在 macOS 上是 SIGSEGV（沒有
        Python traceback），輕一點的變形是「file is not a database」（正本被寫壞）。

        每個執行緒各開一條連線指向**同一個檔案**：跨連線的並行交給 SQLite
        自己的檔案鎖（WAL ＋ `busy_timeout`）✓；邏輯層的保護照舊是版本號與租約。
        """
        if self._closed:
            raise RuntimeError("協調器已關閉")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = store.connect(self.root)
            with self._conn_lock:
                self._conns.append(conn)
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ 租約

    def acquire(self) -> bool:
        """取得租約。拿不到就回 False（不要靜默搶）。

        **例外：持有者跑的是舊版引擎 → 強制接手。**
        否則舊行程會一直霸著租約，新版的修正永遠載入不了
        （實測：改了程式碼、舊行程還活著 → 整包卡住）。
        世代 +1 會讓舊行程的續約立刻失敗 → 舊行程自動收手 ✓。

        **「持有者是舊版」要在正常取得之前就先看**：正常取得的路徑
        （租約到期 → 誰先來誰拿到）會讓**舊行程在空窗期搶走租約** →
        這個專案又被舊版程式碼驅動（實測：23:01 舊版 MCP server 搶走，
        23:02 才被新版搶回來——中間那一分鐘跑的是舊邏輯）。
        """
        with store.tx(self.conn):
            generation = None
            previous = self._current_holder()
            if previous and self._holder_is_older():
                generation = store.acquire_lease(self.conn, self.owner, force=True)
                if generation is not None:
                    store.journal(self.conn, "coordinator.takeover", actor=self.owner,
                                  detail={"previous": previous,
                                          "generation": generation})
            if generation is None:
                generation = store.acquire_lease(self.conn, self.owner)
            if generation is None:
                self.generation = 0
                return False
            self.generation = generation
            store.journal(self.conn, "coordinator.acquire", actor=self.owner,
                          detail={"generation": generation})
            self._start_lease_renewer()
            return True

    # ------------------------------------------------------------ 背景續約

    def _start_lease_renewer(self) -> None:
        """**背景續約**——tick 可能比租約 TTL 還久。

        live 實測（2026-11-23 23:00~23:05）：派工要等宿主 CLI 提交（最多 30 秒）、
        回執核對與 delivery_state 也會等 → **一趟 tick 可能超過 30 秒的 TTL**。
        續約只在 tick 之間做 → 租約在 tick 中途過期 → 另一個行程（常常是
        **舊版**的 MCP server）搶走租約 → 同一個專案被兩個版本同時驅動，
        新版的修正被舊版蓋掉（實測：新版修正只在持有租約的那幾十秒有效）。

        **持有者活著就應該續約**——這正是租約的意思。續約失敗（別人接手）
        就安靜放棄，主迴圈下一輪會重新搶。
        """
        if self._renew_thread is not None:
            return
        self._renew_stop.clear()
        self._renew_thread = threading.Thread(target=self._renew_loop,
                                              name="fbf-lease-renew", daemon=True)
        self._renew_thread.start()

    def _renew_loop(self) -> None:
        while not self._closed and not self._renew_stop.is_set():
            try:
                if self.generation:
                    with store.tx(self.conn):
                        store.renew_lease(self.conn, self.owner, self.generation)
            except sqlite3.Error:
                pass
            self._renew_stop.wait(self.lease_renew_interval)

    def _stop_lease_renewer(self) -> None:
        self._renew_stop.set()
        thread = self._renew_thread
        self._renew_thread = None
        if thread is not None:
            thread.join(timeout=2)

    def _current_holder(self) -> str:
        lease = store.read_lease(self.conn)
        return lease["owner"] if lease else ""

    def _holder_is_older(self) -> bool:
        """現在的租約持有者是不是跑著**比較舊**的引擎版本？

        **沒有版本字樣 → 算舊的**（那是加版本標記之前的行程 ✓）。
        """
        holder = self._current_holder()
        if not holder:
            return False
        parts = holder.rsplit("-", 1)
        if len(parts) != 2:
            return True           # 沒有版本 → 版本標記之前 → 讓它退位
        try:
            return parts[1] < ENGINE_VERSION
        except TypeError:
            return False

    def renew(self) -> bool:
        if not self.generation:
            return False
        with store.tx(self.conn):
            return store.renew_lease(self.conn, self.owner, self.generation)

    def _lease_ok(self) -> bool:
        return store.lease_valid(self.conn, self.owner, self.generation)

    def close(self) -> None:
        """關掉**所有**執行緒的連線（每個執行緒一條）。"""
        self._stop_lease_renewer()
        with self._conn_lock:
            conns = list(self._conns)
            self._conns = []
            self._closed = True
        for index, conn in enumerate(conns):
            if index == 0:
                try:
                    with store.tx(conn):
                        store.release_lease(conn, self.owner, self.generation)
                except sqlite3.Error:
                    pass
            try:
                conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------ 查詢

    def work(self, work_id: str) -> Optional[Dict[str, Any]]:
        return workflow.load_work(self.conn, work_id)

    def issue(self, issue_id: str) -> Optional[Dict[str, Any]]:
        return workflow.load_issue(self.conn, issue_id)

    def release_role(self, role: str, short_name: str, reason: str = "",
                     actor: str = "human") -> Dict[str, Any]:
        """**釋放席位**，讓另一個 session 可以接管。

        為什麼需要這條路：席位的綁定本來**沒有生命週期**——
        session 意外中斷（機器重開、程式崩了、被關掉）之後，席位就永遠被占著，
        沒有任何 session 能認領它，整條流程就此卡住，而且**沒有任何地方會顯示
        為什麼卡住**。`session revoke` 以前只改 `status`、留著 `session_id`，
        席位一樣占著——那個指令等於沒有出口。

        這是**人類控制面**的動作（規格第 7 節：人類命令不暴露給 session）：
        由人確認「那個 session 真的不行了」之後才釋放。
        釋放會一併清掉該 session 的 UUID 雜湊——新主人會拿到新的 UUID。

        **釋放會一併作廢該 session 名下所有 open task**。
        為什麼：session 已中斷，它名下「正在做的那一棒」不會再有 session
        接手（派工是照 roles 表的 session 派的；席位釋放後新主人是**新的**
        session，不會收到那個舊 task）。留着 open task 的話，
        `_dispatch_pending` 看到「已有開啟任務」就跳過那張工單 → **工單靜靜卡住**。
        作廢後，下一趟 tick 的 `_dispatch_pending` 會為該工單的下一棒
        建**新的** task、派給新認領的 session → 流程接回去。
        """
        role = (role or "").strip().upper()
        short_name = (short_name or "").strip()
        if not role or not short_name:
            return {"ok": False, "code": "CONTENT-MISSING",
                    "message": "角色與簡稱都要有"}
        cancelled = 0
        with store.tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM roles WHERE role = ? AND short_name = ?",
                (role, short_name)).fetchone()
            if row is None:
                return {"ok": False, "code": "ROLE-SLOT-NOT-FOUND",
                        "message": "沒有這個角色席位：%s／%s" % (role, short_name)}
            previous = row["session_id"] or ""
            self.conn.execute(
                "UPDATE roles SET session_id = NULL, uuid_sha256 = NULL,"
                " status = 'active', updated_at = ? WHERE role = ? AND short_name = ?",
                (store.now(), role, short_name))
            dropped = 0
            if previous:
                cur = self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', updated_at = ?"
                    " WHERE assigned_session = ? AND status = 'open'",
                    (store.now(), previous))
                cancelled = cur.rowcount
                # **沒送出去的派工也要清**（與 `_rebind_by_title` 同一條路）：
                # 收件人是已死的 session → 送不出去，只會每 30 秒重查一次、
                # 一直累積 `unknown`，還可能誤把工單標成待人處理。
                # 喚醒類的 outbox **查不到對應 task** → 不會被「task 已關閉」
                # 那條孤兒檢查撿到 → 只能在這裡清。
                dropped = self.conn.execute(
                    "UPDATE outbox SET status = ?, last_error = ?, updated_at = ?"
                    " WHERE recipient_session = ? AND status IN (?,?,?)",
                    (OUT_FAILED, "session 已釋放，遺留派工", store.now(), previous,
                     OUT_QUEUED, OUT_UNKNOWN, OUT_SENDING)).rowcount
            store.journal(self.conn, "role.release", "%s/%s" % (role, short_name),
                          actor=actor, session_id=previous,
                          detail={"reason": reason, "previous_session": previous,
                                  "cancelled_open_tasks": cancelled,
                                  "dropped_outbox": dropped})
        note = "席位已釋放。新的 session 現在可以呼叫 fbflow_register 認領它。"
        if cancelled:
            note += " 同時作廢了該 session 名下 %d 個 open task；" \
                   "下一趟 tick 會為受影響的工單重派新 task。" % cancelled
        if dropped:
            note += " 另外作廢了 %d 筆還沒送出的派工（收件人已死）。" % dropped
        if not previous:
            # **席位本來就空著** → 說清楚，不要看起來像「釋放了什麼」。
            # （實測：指令讀寫到錯的正本時，席位列存在但沒綁 session，
            #   回報「ok」＋`released_session: ""` → 人以為釋放了，其實沒動到。）
            note = ("這個席位本來就沒有 session 占著，**沒有東西可釋放**"
                    "（若你預期它被某個 session 占著，請確認這道指令讀的是"
                    "同一份專案正本）。")
        return {"ok": True, "role": role, "short_name": short_name,
                "released_session": previous, "was_occupied": bool(previous),
                "cancelled_tasks": cancelled, "dropped_outbox": dropped,
                "note": note}

    def seat_status(self) -> List[Dict[str, Any]]:
        """每個席位現在是誰占著、最後一次活動是什麼時候。

        **孤兒席位要看得見**：session 中斷後席位還被占著，流程會**靜靜地卡住**
        （沒有錯誤、沒有待人處理項，只是不再推進）。這裡把「被占住卻很久沒活動」
        標成 `stale`，讓人知道該釋放它。
        """
        threshold = store.limit_of(self.conn, "seat_stale_hours", 12)
        rows = self.conn.execute(
            "SELECT * FROM roles ORDER BY role, short_name").fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            session_id = row["session_id"] or ""
            last_activity = ""
            if session_id:
                last_activity = self.conn.execute(
                    "SELECT MAX(ts) AS ts FROM journal WHERE session_id = ?",
                    (session_id,)).fetchone()["ts"] or ""
            idle_hours = _idle_hours(last_activity)
            out.append({
                "role": row["role"], "short_name": row["short_name"],
                "session_id": session_id, "status": row["status"],
                "claimed": bool(session_id),
                "bound_at": row["updated_at"] or "",
                "last_activity": last_activity,
                "idle_hours": idle_hours,
                "stale": bool(session_id) and idle_hours >= threshold,
            })
        return out

    def role_rows(self, role: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM roles WHERE role = ? AND status = 'active' "
            "AND session_id IS NOT NULL AND session_id <> '' ORDER BY short_name",
            (role,)).fetchall()
        return [dict(r) for r in rows]

    def _current_milestone(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM milestones WHERE state <> 'done' ORDER BY seq LIMIT 1").fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 席位對位

    def _project_name(self) -> str:
        """這個專案的名字（`settings.name`，沒有就用專案資料夾名）。

        **經過 `sanitize_project_name`**——標題裡放的就是這個形式，
        比較時兩邊才會一致。
        """
        name = store.get_setting(self.conn, "name", "").strip()
        return platform.sanitize_project_name(name or os.path.basename(self.root))

    def _seat_title(self, role: str, short_name: str) -> str:
        """這個專案的席位標題：`<專案>/<ROLE>/<SHORT>`。

        **帶專案名是為了跨專案不撞**：宿主裡所有專案的終端都在同一份
        終端清單裡，標題沒有專案段就會把 A 專案的席位綁到 B 專案的終端。
        """
        return platform.seat_title(role, short_name, self._project_name())

    def _session_in_project(self, row: Dict[str, Any],
                            parsed: Optional[Dict[str, str]] = None) -> bool:
        """這個 host session 是不是**本專案**的？

        **跨專案誤綁的防線**（實測：兩個專案各開一組終端，舊專案的席位
        換綁時只看標題，會把席位綁到**別的專案**的終端 → 提示詞送進
        錯目錄的工作階段）。

        兩種證據，**缺一不可**：

        1. **專案名**（標題的專案段）：必須等於本專案名。標題有專案段時，
           這是專案身分；再對照終端路徑（必須是本專案或其上層工作區）。
        2. **終端路徑**（標題**沒有**專案段＝舊格式時的唯一證據）：
           必須是本專案根目錄**或其上層工作區**。

        兩個證據都缺 → **不能證明 → 不算本專案**（寧可不綁，也不要誤綁）。
        """
        if parsed is None:
            parsed = platform.parse_seat_title(row.get("pane_title") or "")
        proj = (parsed.get("project") or "").strip()
        worktree = str(row.get("worktree_path") or "").strip()

        if proj:
            # 標題已指明專案 → 名稱必須對得上
            if proj.casefold() != self._project_name().casefold():
                return False
            if worktree and not self._path_in_project(worktree):
                return False
            return True

        # 舊格式標題（沒有專案段）→ 只能靠終端路徑認，而且必須**正好等於**本專案
        # （放寬到上層會認錯：同一個工作區裡可能同時有別的專案）
        if not worktree:
            return False
        try:
            wp = os.path.realpath(os.path.expanduser(worktree))
            root = os.path.realpath(self.root)
        except OSError:
            return False
        return wp == root

    def _path_in_project(self, worktree: str) -> bool:
        """終端所在的路徑是不是本專案（或其上層資料夾）？"""
        try:
            wp = os.path.realpath(os.path.expanduser(worktree))
            root = os.path.realpath(self.root)
        except OSError:
            return False
        return wp == root or root.startswith(wp + os.sep)

    def _enroll_host_sessions(self) -> None:
        """把 host CLI 裡現在開著的 session 對到 roles 表的席位。

        兩件事，順序不能顛倒：

        **1. 先依標題重新對位（重啟後修復）**

        宿主重啟後 terminal handle 全部換新，但 `roles.session_id` 還是舊的
        ——舊 handle 在宿主裡已經不存在，派工永遠送不到，而 auto-enroll
        只填空席位、不會覆蓋被死 handle 占著的席位，流程就此**靜靜卡住**
        （實測：8 小時、151 次驗證、deferral 累積到 65，沒有任何錯誤訊息）。

        修法：席位標題 `<專案>/<ROLE>/<SHORT_NAME>`（例如
        `產品研究/PLANNER/P1`）由宿主保管在 tab layout 裡，**跨重啟存活**
        （用 `terminal rename` 設；leaf 標題會被 agent 的 TUI 用 OSC 覆寫，
        不能當身分）。tick 掃到「標題對得上、且該席位綁的 handle 已經不在
        宿主清單」→ 換綁到新 handle，並作廢該 session 名下的 open task /
        未送出的 outbox，讓 `_dispatch_pending` 為下一棒重派。

        **標題比對有兩層**：角色／簡稱（大小寫不拘，`thinker/t1` 也算）
        ＋ **專案名**（跨專案不撞）。

        **2. 再填空席位（首次入席）**

        session 不在角色表裡永遠不會收到提示詞（`_pick_session` 只從 roles
        讀）→ 這裡只加，不會誤派。我們不主動建席位——人類先用
        `fbf.py role add` 建好，這裡只把 session 填進 `session_id IS NULL` 的席位。

        **填空也依標題優先**：標題對得上某個空席位就先綁那個；只有連
        標題都讀不到的 session 才依序填空——否則會依列舉順序亂綁
        （實測：thinker 的終端被綁成 PLANNER）。
        """
        try:
            sessions = platform.scan_host_sessions(cli="")
        except Exception:
            return
        if not sessions:
            return

        live_handles = {row.get("handle") or "" for row in sessions}
        self._rebind_by_title(sessions, live_handles)

        for row in sessions:
            handle = row.get("handle") or ""
            if not handle:
                continue
            parsed = platform.parse_seat_title(row.get("pane_title") or "")
            # **不是本專案的 session → 不碰**（跨專案誤綁的防線）
            if not self._session_in_project(row, parsed):
                continue
            # 已經綁到別的角色 → 不改綁，但**把 tab 標題補成新格式**
            #（帶專案名），這樣宿主重啟後才有專案段的證據可對位。
            existing = self.conn.execute(
                "SELECT * FROM roles WHERE session_id = ?", (handle,)).fetchone()
            if existing:
                wanted = self._seat_title(existing["role"], existing["short_name"])
                if wanted != str(row.get("pane_title") or "").strip():
                    platform.rename_host_session(handle, wanted)
                continue
            # **標題指定了席位 → 就綁那個席位**（大小寫不拘）。
            # 標題指定了但沒有這個席位 → **不綁**（寧可空著，也不要猜錯角色）。
            if parsed.get("role") and parsed.get("short_name"):
                slot = self.conn.execute(
                    "SELECT role, short_name FROM roles"
                    " WHERE (session_id IS NULL OR session_id = '')"
                    "   AND UPPER(role) = ? AND UPPER(short_name) = ? LIMIT 1",
                    (parsed["role"].upper(), parsed["short_name"].upper())).fetchone()
                if slot is None:
                    continue
            else:
                # 連標題都讀不出來 → 只能依序填空（沒有別的線索）
                slot = self.conn.execute(
                    "SELECT role, short_name FROM roles"
                    " WHERE session_id IS NULL OR session_id = ''"
                    " ORDER BY role, short_name LIMIT 1").fetchone()
            if slot is None:
                continue
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE roles SET session_id = ?, agent = ?, status = 'active',"
                    " updated_at = ? WHERE role = ? AND short_name = ?",
                    (handle, platform.agent_claude, store.now(), slot["role"], slot["short_name"]))
                store.journal(self.conn, "role.bind", "%s/%s" % (slot["role"], slot["short_name"]),
                              session_id=handle, detail={"auto_enroll": "host_cli",
                                                         "pane_title": row.get("pane_title") or ""})
            # 設好標題，宿主重啟後這個席位才認得出來（失敗無害，不影響綁定）。
            platform.rename_host_session(handle,
                                         self._seat_title(slot["role"], slot["short_name"]))

    def _rebind_by_title(self, sessions: List[Dict[str, Any]],
                         live_handles: set) -> int:
        """依席位標題把死掉的 handle 換成宿主裡現在活著的 handle。

        指到孤兒席位的代價：那些席位會**一直**是個死 id，每次 tick 都派不出去。
        所以判準要保守——四件事同時成立才換綁：

        1. 該席位現在綁的 handle 不在宿主清單裡（真的死了）
        2. 宿主裡有一個 session 的 tab 標題，**角色／簡稱**對得上
           （大小寫不拘：`thinker/t1` 與 `THINKER/T1` 視為同一個）
        3. 該 session 的**專案**對得上本專案（標題的專案段，或終端所在路徑）
        4. 那個 session 還沒有被別的席位佔走

        任何一項不成立就不動。回傳實際換綁的席位數。
        """
        #: (專案, 角色, 簡稱) → 該標題的 session 清單（多個專案可能同名）
        by_title: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for row in sessions:
            parsed = platform.parse_seat_title(row.get("pane_title") or "")
            if not parsed.get("role") or not parsed.get("short_name"):
                continue
            key = (parsed["project"].casefold(), parsed["role"].upper(),
                   parsed["short_name"].upper())
            by_title.setdefault(key, []).append(row)

        if not by_title:
            return 0

        bound = 0
        rows = self.conn.execute(
            "SELECT role, short_name, session_id FROM roles"
            " WHERE session_id IS NOT NULL AND session_id <> ''").fetchall()
        project_name = self._project_name().casefold()
        for slot in rows:
            current = slot["session_id"] or ""
            if current in live_handles:
                continue                      # 還活著，不動
            label = self._seat_title(slot["role"], slot["short_name"])
            # 候選：先看「本專案」的標題，再看舊格式（沒有專案段）。
            # **每個候選都要過 `_session_in_project`**——跨專案同名標題時，
            # 別讓別的專案的終端被選中。
            candidate = None
            for proj in (project_name, ""):
                for row in by_title.get((proj, slot["role"].upper(),
                                         slot["short_name"].upper()), []):
                    if self._session_in_project(row):
                        candidate = row
                        break
                if candidate is not None:
                    break
            if candidate is None:
                continue                      # 宿主裡沒有本專案的這個席位
            new_handle = candidate.get("handle") or ""
            if not new_handle or new_handle == current:
                continue
            # 那個 handle 已經被別的席位認走了 → 讓它去，別搶
            if self.conn.execute(
                    "SELECT 1 FROM roles WHERE session_id = ? AND NOT (role = ? AND short_name = ?)",
                    (new_handle, slot["role"], slot["short_name"])).fetchone():
                continue
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE roles SET session_id = ?, agent = ?, uuid_sha256 = NULL,"
                    " status = 'active', updated_at = ? WHERE role = ? AND short_name = ?",
                    (new_handle, platform.agent_claude, store.now(),
                     slot["role"], slot["short_name"]))
                # 舊 session 名下的 open task 不會再有 session 接手（派工照 roles
                # 的 session 派，新主人是新 session）→ 留著會讓 `_dispatch_pending`
                # 以為工單已經有人在辦，永遠不再派。作廢它，讓下一趟重派。
                cancelled = self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', updated_at = ?"
                    " WHERE assigned_session = ? AND status = 'open'",
                    (store.now(), current)).rowcount
                # 還沒送出的派工同樣作廢（收件人已經不在了，硬送只會累積錯誤）
                dropped = self.conn.execute(
                    "UPDATE outbox SET status = ?, last_error = ?, updated_at = ?"
                    " WHERE recipient_session = ? AND status IN (?,?,?)",
                    (OUT_FAILED, "session 已隨宿主重啟消失", store.now(), current,
                     OUT_QUEUED, OUT_UNKNOWN, OUT_SENDING)).rowcount
                # 把 still-pending 的派工收件人換成新 handle——否則重試時仍用死 handle。
                self.conn.execute(
                    "UPDATE outbox SET recipient_session = ?, updated_at = ?"
                    " WHERE recipient_session = ? AND status IN (?,?)",
                    (new_handle, store.now(), current, OUT_FAILED, OUT_UNKNOWN))
                store.journal(self.conn, "role.rebind", label,
                              session_id=new_handle,
                              detail={"previous_session": current,
                                      "pane_title": label,
                                      "cancelled_open_tasks": cancelled,
                                      "dropped_outbox": dropped})
            bound += 1
        return bound

    def _pick_session(self, role: str) -> Optional[Dict[str, Any]]:
        """派工對象選擇（規格第 6 節）。

        優先序：**未完成工單數最少 → 最久未被指派 → session ID 字典序**。
        """
        rows = self.conn.execute(
            "SELECT r.session_id AS session_id, r.role AS role, r.short_name AS short_name,"
            "       r.workdir AS workdir,"
            "       (SELECT COUNT(*) FROM works w WHERE w.assigned_session = r.session_id"
            "          AND w.state <> 'closed' AND w.awaiting_human = 0) AS load,"
            "       (SELECT MAX(w.updated_at) FROM works w WHERE w.assigned_session = r.session_id)"
            "         AS last_assigned"
            " FROM roles r WHERE r.role = ? AND r.status = 'active'"
            "   AND r.session_id IS NOT NULL AND r.session_id <> ''", (role,)).fetchall()
        if not rows:
            return None
        ordered = sorted(rows, key=lambda r: (int(r["load"]),
                                             (r["last_assigned"] or ""),
                                             str(r["session_id"])))
        return dict(ordered[0])

    # ------------------------------------------------------------ 建立

    def create_milestone(self, seq: int, name: str, outcome: str) -> str:
        """建立里程碑。**outcome 必須在派第一張單前寫定**。"""
        with store.tx(self.conn):
            milestone_id = store.next_id(self.conn, "milestones", "MS", 3)
            self.conn.execute(
                "INSERT INTO milestones(id, seq, name, outcome, state, review_count,"
                " created_at, updated_at) VALUES (?,?,?,?,?,0,?,?)",
                (milestone_id, int(seq), name, outcome, workflow.MS_OPEN,
                 store.now(), store.now()))
            store.journal(self.conn, "milestone.create", milestone_id, detail={"seq": seq})
            return milestone_id

    def create_work(self, title: str, requirement_id: str, content: str,
                    expected_outputs: List[Any], milestone_id: str = "",
                    role: str = "", skill: str = "") -> str:
        """建立工單（狀態 `backlog`）。

        `skill`＝**搭配的專案 SKILL**（spec：PLANNER 填寫工作名稱、內容、解法、
        驗收方式、搭配的專案 SKILL）。它會寫進工單檔案與每一棒派工提示詞。
        """
        with store.tx(self.conn):
            requirement = self.conn.execute(
                "SELECT * FROM requirements WHERE id = ?", (requirement_id,)).fetchone()
            if requirement is None:
                raise ValueError("找不到需求 %s" % requirement_id)
            if not requirement["confirmed_at"]:
                raise ValueError("需求 %s 尚未經人類確認，不能建工單" % requirement_id)
            has_milestone = self.conn.execute(
                "SELECT COUNT(*) AS n FROM milestones").fetchone()["n"]
            if has_milestone and not milestone_id:
                raise ValueError("此專案有里程碑，工單必須指定 milestone_id")
            work_id = store.next_id(self.conn, "works", "W", 3)
            self.conn.execute(
                "INSERT INTO works(id, title, requirement_id, milestone_id, state, version,"
                " assigned_role, content, expected_outputs, skill, created_at, updated_at)"
                " VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
                (work_id, title, requirement_id, milestone_id or None, workflow.BACKLOG,
                 role or None, content, json.dumps(expected_outputs or [], ensure_ascii=False),
                 skill or None, store.now(), store.now()))
            store.journal(self.conn, "work.create", work_id,
                          detail={"requirement_id": requirement_id,
                                  "milestone_id": milestone_id})
        return work_id

    def supersede_work(self, work_id: str, replacement_work_ids: List[str],
                       reason: str, session_id: str = "") -> Dict[str, Any]:
        """**把一張已發出的工單標成「已被取代」**——需求變了或做法要改。

        **已發出的工單不改內容、不改狀態**（使用者裁決 2026-11-25：
        改已發出的工單太麻煩，改成「照新需求開新工單」）。
        這裡只做三件事：

        1. **標記**：寫下被哪些新工單取代、為什麼（內容與狀態不動）。
        2. **停工**：取消這張單開著的 task——角色不該再做一張已作廢的單。
        3. **退場**：之後不再被派工；交件也會被擋（訊息說明為什麼）。

        `replacement_work_ids`＝**新開的工單**（必須存在、不能已結案、
        不能自己已被取代——取代鏈只留一層）。

        **版本號不動**：bump 會讓還在施工的角色收到看不懂的
        `TASK-STALE`；交件路徑另有專門的「已被取代」訊息。
        """
        work_id = str(work_id or "").strip()
        reason = str(reason or "").strip()
        replacements = [str(x).strip() for x in (replacement_work_ids or [])
                        if str(x).strip()]
        if not work_id:
            raise ValueError("work_id 不能空白")
        if not reason:
            raise ValueError("必須說明為什麼被取代（需求變了／做法變了）")
        if not replacements:
            raise ValueError("必須指定取代它的新工單（replacement_work_ids）")

        with store.tx(self.conn):
            work = self.conn.execute(
                "SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
            if work is None:
                raise ValueError("找不到工單 %s" % work_id)
            if work["state"] == workflow.CLOSED:
                raise ValueError("工單 %s 已結案——結案是歷史，不能被取代" % work_id)
            if work["superseded_by"]:
                raise ValueError("工單 %s 已經被取代了（%s）"
                                 % (work_id, work["superseded_by"]))
            for rid in replacements:
                if rid == work_id:
                    raise ValueError("工單不能取代自己")
                row = self.conn.execute(
                    "SELECT * FROM works WHERE id = ?", (rid,)).fetchone()
                if row is None:
                    raise ValueError("找不到取代者工單 %s" % rid)
                if row["state"] == workflow.CLOSED:
                    raise ValueError("取代者 %s 已結案，不能當取代者" % rid)
                if row["superseded_by"]:
                    raise ValueError("取代者 %s 本身已被取代——取代鏈只留一層" % rid)

            cancelled = self.conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ?"
                " WHERE work_id = ? AND status = 'open'",
                (store.now(), work_id)).rowcount
            self.conn.execute(
                "UPDATE works SET superseded_by = ?, supersede_reason = ?,"
                " updated_at = ? WHERE id = ?",
                (json.dumps(replacements, ensure_ascii=False), reason,
                 store.now(), work_id))
            store.journal(self.conn, "work.supersede", work_id, session_id=session_id,
                          detail={"replaced_by": replacements, "reason": reason,
                                  "cancelled_tasks": cancelled})
        return {"work_id": work_id, "superseded_by": replacements, "reason": reason,
                "cancelled_tasks": cancelled}

    def create_issue(self, work_id: str, initiator_session: str, initiator_role: str,
                      trigger_kind: str, problem: str, location: str, tried: str,
                      impact: str, question: str,
                      completion_criteria: str = "") -> str:
        """建立 ISSUE（狀態 `raised`）。

        `completion_criteria`＝**ISSUE 完成要求**（spec 的 ISSUE 單模板）：
        達成什麼效果才算解決。
        """
        with store.tx(self.conn):
            issue_id = store.next_id(self.conn, "issues", "IS", 3)
            self.conn.execute(
                "INSERT INTO issues(id, work_id, initiator_session, initiator_role, state,"
                " trigger_kind, problem, location, tried, impact, question,"
                " completion_criteria, linked_work_ids,"
                " payload, version, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (issue_id, work_id or None, initiator_session, initiator_role,
                 workflow.ISSUE_RAISED, trigger_kind, problem, location, tried, impact,
                 question, completion_criteria or None, "[]", "{}", store.now(), store.now()))
            store.journal(self.conn, "issue.create", issue_id,
                          detail={"work_id": work_id, "trigger_kind": trigger_kind})
        return issue_id

    def bind_role(self, role: str, short_name: str, session_id: str,
                  agent: str = "opencode", project_code: str = "",
                  workdir: str = "") -> Dict[str, Any]:
        """把一個**角色席位**認領給某個 session。

        流程（管理員先確認簡稱，Session 只認領既有席位）：
        1. 人類先建席位：`fbf.py role add --role PLANNER --short-name P1`
           → `roles` 表一列，`session_id` 為 NULL（席位空著）。
        2. session 呼叫 `fbflow_register` → 這裡把席位綁到它的 session id。

        **空席位不能被亂認領**：席位必須先由人類建好，否則任何 session
        都能自稱 PLANNER。席位已被別的 session 綁走 → 拒絕。
        """
        role = (role or "").strip().upper()
        short_name = (short_name or "").strip()
        session_id = (session_id or "").strip()
        if not role or not short_name or not session_id:
            return {"ok": False, "code": "CONTENT-MISSING",
                    "message": "角色、簡稱、session id 都要有"}
        with store.tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM roles WHERE role = ? AND short_name = ?",
                (role, short_name)).fetchone()
            if row is None:
                return {"ok": False, "code": "ROLE-SLOT-NOT-FOUND",
                        "message": "沒有這個角色席位：%s／%s（請先讓管理員用 "
                                   "`fbf.py role add` 建起來）" % (role, short_name)}
            if row["session_id"] and row["session_id"] != session_id:
                return {"ok": False, "code": "ROLE-SLOT-TAKEN",
                        "message": "這個席位已經被 session %s 認領，無法搶走。"
                                   "若那個 session 已經中斷（機器重開、程式崩了），"
                                   "請讓人跑 `fbf.py role release --role %s "
                                   "--short-name %s` 釋放席位，再用新的 session 認領。"
                                   % (row["session_id"], role, short_name)}
            self.conn.execute(
                "UPDATE roles SET session_id = ?, agent = ?, status = 'active',"
                " project_code = ?, workdir = COALESCE(NULLIF(workdir,''), ?),"
                " updated_at = ? WHERE role = ? AND short_name = ?",
                (session_id, agent or "opencode", project_code or "", workdir or "",
                 store.now(), role, short_name))
            store.journal(self.conn, "role.bind", "%s/%s" % (role, short_name),
                          session_id=session_id, detail={"project_code": project_code})
        # 把 tab 標題設成席位名，重啟後才有東西可以對位（失敗不影響綁定）。
        if platform.is_host_handle(session_id):
            platform.rename_host_session(session_id,
                                         self._seat_title(role, short_name))
        return {"ok": True, "role": role, "short_name": short_name,
                "session_id": session_id, "project_code": project_code}

    def role_slot(self, role: str, short_name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM roles WHERE role = ? AND short_name = ?",
            (role, short_name)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 提交

    def _stage_of_task(self, task_id: str, stage: str) -> str:
        """這一棒是什麼？

        **提示詞不帶工作狀態**（spec 第 70 行），所以角色可能不知道 stage；
        留空就**依 task_id 從任務表查出來**——task 本身就是「這一棒」，
        不需要另外問角色。帶錯 stage 的機會因此少一個。
        """
        if stage:
            return stage
        row = self.conn.execute("SELECT stage FROM tasks WHERE id = ?",
                                (task_id,)).fetchone()
        return (row["stage"] if row else "") or ""

    def submit_work(self, task_id: str, work_id: str, expected_version: int,
                    session_id: str, stage: str, payload: Dict[str, Any],
                    message_id: str = "") -> Dict[str, Any]:
        """session 交件。**這裡只寫 submission，不改狀態。**

        同一組 (task_id, session_id, stage, expected_version) 重送 →
        回傳原本的 submission ID（冪等）。

        `stage` 可以留空 → 依 `task_id` 查出這一棒（提示詞不帶狀態）。
        """
        stage = self._stage_of_task(task_id, stage)
        if not stage:
            raise ValueError("查不到 task %s 的 stage，請帶 stage 或確認 task_id" % task_id)
        with store.tx(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM submissions WHERE task_id = ? AND session_id = ?"
                " AND stage = ? AND expected_version = ?",
                (task_id, session_id, stage, int(expected_version))).fetchone()
            if existing is not None:
                return {"submission_id": existing["id"], "status": existing["status"],
                        "idempotent": True,
                        "gaps": json.loads(existing["gaps"] or "[]")}
            submission_id = store.next_id(self.conn, "submissions", "S", 6)
            self.conn.execute(
                "INSERT INTO submissions(id, task_id, work_id, issue_id, expected_version,"
                " session_id, stage, payload, message_id, status, gaps, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (submission_id, task_id, work_id, None, int(expected_version), session_id,
                 stage, json.dumps(payload or {}, ensure_ascii=False), message_id,
                 SUB_PENDING, "[]", store.now(), store.now()))
            store.journal(self.conn, "submission.create", submission_id,
                          session_id=session_id,
                          detail={"task_id": task_id, "stage": stage,
                                  "expected_version": expected_version})
            return {"submission_id": submission_id, "status": SUB_PENDING,
                    "idempotent": False, "gaps": []}

    def submit_issue(self, task_id: str, issue_id: str, expected_version: int,
                     session_id: str, stage: str, payload: Dict[str, Any],
                     message_id: str = "") -> Dict[str, Any]:
        stage = self._stage_of_task(task_id, stage)
        if not stage:
            raise ValueError("查不到 task %s 的 stage，請帶 stage 或確認 task_id" % task_id)
        with store.tx(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM submissions WHERE task_id = ? AND session_id = ?"
                " AND stage = ? AND expected_version = ?",
                (task_id, session_id, stage, int(expected_version))).fetchone()
            if existing is not None:
                return {"submission_id": existing["id"], "status": existing["status"],
                        "idempotent": True,
                        "gaps": json.loads(existing["gaps"] or "[]")}
            submission_id = store.next_id(self.conn, "submissions", "S", 6)
            self.conn.execute(
                "INSERT INTO submissions(id, task_id, work_id, issue_id, expected_version,"
                " session_id, stage, payload, message_id, status, gaps, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (submission_id, task_id, None, issue_id, int(expected_version), session_id,
                 stage, json.dumps(payload or {}, ensure_ascii=False), message_id,
                 SUB_PENDING, "[]", store.now(), store.now()))
            store.journal(self.conn, "submission.create", submission_id,
                          session_id=session_id, detail={"task_id": task_id, "stage": stage})
            return {"submission_id": submission_id, "status": SUB_PENDING,
                    "idempotent": False, "gaps": []}

    # ------------------------------------------------------------ tick

    def tick(self) -> Dict[str, Any]:
        """一趟協調。回傳這一趟做了什麼（給 CLI 與測試看）。"""
        report: Dict[str, Any] = {"applied": [], "stale": [], "unverified": [],
                                  "expired": [], "rejected": [], "dispatched": [],
                                  "deferred": [], "milestones": [], "awaiting": [],
                                  "project_reviews": [], "errors": []}
        # **宿主終端快照每趟重掃**（終端會開關，handle 會變）。
        self._host_sessions_cache = None
        if not self.generation:
            if not self.acquire():
                report["errors"].append("lease-held-by-other")
                return report

        # host 裡現在開著的 session，都登入 roles 表（auto-enroll）——
        # 這樣 host 裡的 Claude Code / OpenCode session 都能當執行平台用。
        self._enroll_host_sessions()

        # 0) 專案啟動三段（第 2、3 段；第 1 段是人類 join 三個席位）：
        #    沒已確認需求 → 派 GRILL（釐清＋複述＋問同意），**不建工單**；
        #    有已確認需求 → 收掉 grill，再派 DECOMPOSE（分解並建工單），
        #    第一張工單一建出來 DECOMPOSE 自動收掉。
        self._dispatch_grill(report)
        self._dispatch_decompose(report)

        # 1) 候選提交：pending 與 unverified 都要看（unverified 是等回執）
        #    expired 是終態（回執等到期、已換新 task 重試）→ 不再重查。
        rows = self.conn.execute(
            "SELECT * FROM submissions WHERE status IN (?,?) ORDER BY created_at LIMIT ?",
            (SUB_PENDING, SUB_UNVERIFIED, MAX_SUBMISSIONS_PER_TICK)).fetchall()
        for row in rows:
            try:
                self._process_submission(dict(row), report)
            except sqlite3.DatabaseError:
                # **正本損毀**不是「這一筆失敗」——往上報，讓常駐迴圈停下來，
                # 不要埋在逐筆錯誤裡繼續往壞掉的正本寫。
                raise
            except _StaleSubmission:
                # 樂觀鎖沒中（套用前版本被改過）→ 交易已回滾 → 標 stale，
                # **不重試套用**（規格第 4 節）。
                with store.tx(self.conn):
                    self._mark_submission(row["id"], SUB_STALE, ["work.version_mismatch"])
                report["stale"].append(row["id"])
            except Exception as exc:  # 單筆失敗不拖垮整趟
                report["errors"].append("%s: %s" % (row["id"], exc))
            # **中途續租約**：一趟 tick 可能包含大量平台 I/O（回執查核、送達查核），
            # 時間可能超過租約 TTL（30 秒）。不續約 → 另一個行程接手 →
            # 舊行程還在跑 → 雙寫入者。續不到就收手（舊的世代已不屬於我們）。
            if not self.renew():
                self.generation = 0
                report["errors"].append("lease-lost-mid-tick")
                return report

        # 1b) 「該關該填的沒填，就不離開那關」：來源欄位沒建過的單退回源頭。
        self._enforce_source_prerequisites(report)

        # 1c) **退件卻沒有重新派工**的舊 task → 收掉，讓這張單重新被派。
        self._heal_rejected_tasks(report)

        # 1d) **角色不回應** → 先喚醒（再推一把），逾時才收掉重派。
        self._wake_up_idle_roles(report)
        self._expire_stalled_tasks(report)

        # 1e) **巡檢**：哪張單有問題 → 引擎自己說（journal＋報告）。
        self._run_health_check(report)

        # 2) 派工（里程碑順序）
        self._dispatch_pending(report)

        # 2b) ISSUE 派工（PLANNER 處理 → 關聯工單 → 發起者驗證／退回）
        self._dispatch_issues(report)

        # 3) 里程碑檢視
        self._review_milestones(report)

        # 3b) **專案結案檢視**（與里程碑無關）：
        #     所有單據都結案了 → 通知 PLANNER 對照產出與任務目標 → 判定結案。
        self._maybe_review_project(report)

        # 3c) **上下文收合提醒**：沒有工作可以做的 session，
        #     離上一次 compact 已經有一段時間 → 叫它 /compact 收合上下文。
        #     放在派工之後 → 這一趟才剛被派到單的 session 不會被叫去 compact。
        self._nudge_compact(report)

        # 4) 送出 outbox（交易外）——**派工前再驗一次租約**：
        # outbox 派送不經過 `_lease_ok()`，租約中途被接手時會變成**兩個行程
        # 同時向 session 發提示詞**（重複派工）。
        if not self._lease_ok():
            report["errors"].append("lease-lost-before-flush")
            return report
        self._flush_outbox(report)

        return report

    def _process_submission(self, submission: Dict[str, Any],
                            report: Dict[str, Any]) -> None:
        """處理一筆提交。"""
        scan_id = uuid.uuid4().hex[:12]

        # --- 交易外：查平台回 -----------------------------------------
        receipt = self._verify_receipt(submission)
        if receipt is not None:
            with store.tx(self.conn):
                store.journal(self.conn, "receipt.verify", submission["work_id"] or submission["issue_id"],
                              session_id=submission["session_id"],
                              detail={"submission_id": submission["id"], "task_id": submission["task_id"],
                                      "message_id": submission.get("message_id"),
                                      "source": "injected" if self.verifier else "opencode",
                                      "verified": bool(receipt.get("verified")),
                                      "reason": receipt.get("reason"),
                                      "platform": receipt.get("platform"),
                                      "model_id": receipt.get("model_id"),
                                      "context_id": receipt.get("context_id"),
                                      "finish": receipt.get("finish"),
                                      "platform_detail": receipt.get("detail") or {}})
        if receipt is not None and not receipt.get("verified"):
            if receipt.get("permanent"):
                # 永久性問題（回執被別的提交用過等）：重試沒用，退回要求重交。
                kind, target_id, from_state = self._target_of(submission)
                gaps = [receipt.get("reason") or "receipt.unverified"]
                with store.tx(self.conn):
                    self._mark_submission(submission["id"], SUB_REJECTED, gaps)
                    store.record_decision(self.conn, scan_id, "rejected",
                                          target_id=target_id,
                                          from_state=from_state,
                                          submission_id=submission["id"],
                                          gaps=gaps,
                                          detail={"reason": receipt.get("reason_detail", "")})
                    # 退回 → 版本 +1，讓角色重讀後可以用新的 message_id 再交
                    self._bump_work_version(submission)
                report["rejected"].append(submission["id"])
                # **重新派一棒**：角色交完就結束回合，沒人通知就會靜靜卡住。
                # `reason_detail` 帶上「撞到哪個提交／任務」——角色才知道要換成哪一個。
                self._redispatch_rejected(submission, gaps, scan_id, report,
                                          detail=receipt.get("reason_detail", ""))
                return
            # 回執還沒到（或對不上）：
            # - 未到截止 → 標 unverified，下一輪再查
            # - **到了截止** → 標 `awaiting_human`（reason=`receipt_timeout`），
            #   留**可見的人工出口**（規格第 11 節：「逾時標 awaiting_human 並保存原
            #   submission，不自動宣稱失敗」）。
            #   **不叫角色重做**：重做＝角色那幾十分鐘的工全部白花
            #   （實測：`receipt.unverified` 退回補齊，角色重交又被退，純浪費）。
            #   這筆提交與它的 task 都**留著**——證據晚到就自動套用，
            #   角色不用再交一次。
            deadline = self._deadline_of(submission)
            if deadline is not None and deadline < time.time():
                self._receipt_timeout(submission, receipt, scan_id, report)
                return
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE submissions SET status = ?, gaps = ?, updated_at = ? WHERE id = ?",
                    (SUB_UNVERIFIED,
                     json.dumps([receipt.get("reason") or "receipt.unverified"]),
                     store.now(), submission["id"]))
                store.record_decision(self.conn, scan_id, "unverified",
                                      target_id=submission["work_id"] or submission["issue_id"],
                                      submission_id=submission["id"],
                                      gaps=[receipt.get("reason") or "receipt.unverified"],
                                      detail={"reason": receipt.get("reason_detail", "")})
            report["unverified"].append(submission["id"])
            return

        # **回執核對通過 → 解除「回執逾時」的停等人標記**。
        # 那個標記是「等回執等到放棄」；回執既然到了，就沒有東西需要人處理了。
        # 不解除的話：下一棒（PLANNER）的 task 建不出來、退件後的重交也派不出去
        # → 工單靜靜卡住、沒有錯誤訊息（最難查的那種）。
        if receipt is not None and receipt.get("verified") and submission.get("work_id"):
            with store.tx(self.conn):
                self._clear_awaiting(submission["work_id"])

        # --- 交易內：重讀、核對、套用 ---------------------------------
        with store.tx(self.conn):
            if not self._lease_ok():
                report["errors"].append("lease-lost")
                return
            row = self.conn.execute("SELECT * FROM submissions WHERE id = ?",
                                    (submission["id"],)).fetchone()
            if row is None or row["status"] != submission["status"]:
                return  # 別的 tick 已經處理過
            task = self.conn.execute("SELECT * FROM tasks WHERE id = ?",
                                     (submission["task_id"],)).fetchone()
            if task is None or task["status"] != "open":
                self._mark_submission(submission["id"], SUB_STALE, ["task.not_open"])
                report["stale"].append(submission["id"])
                return
            if task["assigned_session"] != submission["session_id"]:
                self._mark_submission(submission["id"], SUB_STALE, ["task.not_assigned"])
                report["stale"].append(submission["id"])
                return

            if submission["issue_id"]:
                self._apply_issue_submission(submission, task, receipt, scan_id, report)
            else:
                self._apply_work_submission(submission, task, receipt, scan_id, report)

    def _bump_expiry_count(self, target_id: str) -> int:
        """這張單的「回執到期」次數 +1（回傳新次數）。

        計數器名稱：`receipt_expires:<work_id>` / `receipt_expires:<issue_id>`——
        跟清停等人時重置的那個名字一致。
        """
        name = "receipt_expires:%s" % target_id
        self.conn.execute("INSERT INTO counters(name, value) VALUES (?, 0) "
                          "ON CONFLICT(name) DO NOTHING", (name,))
        self.conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
        row = self.conn.execute("SELECT value FROM counters WHERE name = ?",
                                (name,)).fetchone()
        return int(row["value"]) if row else 0

    def _receipt_timeout(self, submission: Dict[str, Any],
                         receipt: Dict[str, Any], scan_id: str,
                         report: Dict[str, Any]) -> None:
        """回執等到截止仍未核對 → **停等人，不要叫角色重做**。

        **為什麼不再自動重派**（舊行為：作廢 task → 換新 task → 叫角色「退回補齊、
        用新的 message_id 重交」）：那等於把角色那幾十分鐘的工全部丟掉，
        而且常常連續丟三次（實測 W-002／W-003／W-004 都這樣）——
        **純浪費時間，不是在工作**。

        現在（規格第 11 節）：
        1. 這筆 submission **原封不動留著**（還在 `unverified`），
           證據晚到就自動套用 → **角色不用再交一次**；
        2. task 也留著開著 → 不會被「角色沒動靜」那條路收掉重派；
        3. 工單（或 ISSUE）標 `awaiting_human=receipt_timeout` →
           進 `awaiting list`，**人看得見**；
        4. 人裁決：`grant_more`（繼續等證據）／`revise`（真的要重做）／
           `abort`（收掉）。
        """
        target_id = submission.get("work_id") or submission.get("issue_id") or ""
        is_issue = bool(submission.get("issue_id"))
        count = self._bump_expiry_count(target_id)
        limit = store.limit_of(self.conn, "max_receipt_expires", MAX_RECEIPT_EXPIRIES)
        reason = receipt.get("reason") or "receipt.unverified"
        detail = receipt.get("reason_detail") or ""
        with store.tx(self.conn):
            store.record_decision(self.conn, scan_id, "receipt_timeout",
                                  target_id=target_id,
                                  submission_id=submission["id"],
                                  gaps=[reason],
                                  detail={"reason": detail,
                                          "timeout_count": count,
                                          "max_receipt_expires": limit})
            # **停等人**：這張單在等人裁決，不是「忘了回應」。
            if is_issue:
                self.conn.execute(
                    "UPDATE issues SET awaiting_human = 1, awaiting_reason = ?,"
                    " updated_at = ? WHERE id = ?",
                    ("receipt_timeout", store.now(), submission["issue_id"]))
            else:
                self.conn.execute(
                    "UPDATE works SET awaiting_human = 1, awaiting_reason = ?,"
                    " updated_at = ? WHERE id = ?",
                    ("receipt_timeout", store.now(), submission["work_id"]))
            report["awaiting"].append({"target": target_id,
                                       "reason": "receipt_timeout",
                                       "receipts": count,
                                       "detail": detail})
        report["expired"].append(submission["id"])

    def _clear_awaiting_flags(self, target_id: str, is_issue: bool) -> None:
        if is_issue:
            self.conn.execute(
                "UPDATE issues SET awaiting_human = 0, awaiting_reason = NULL,"
                " updated_at = ? WHERE id = ?", (store.now(), target_id))
        else:
            self._clear_awaiting(target_id)

    # ------------------------------------------------------------ 工作單套用

    def _apply_work_submission(self, submission: Dict[str, Any], task: sqlite3.Row,
                               receipt: Optional[Dict[str, Any]], scan_id: str,
                               report: Dict[str, Any]) -> None:
        work = workflow.load_work(self.conn, submission["work_id"])
        if work is None:
            self._mark_submission(submission["id"], SUB_STALE, ["work.missing"])
            report["stale"].append(submission["id"])
            return

        # 版本不符 → stale，不重試套用
        if int(work["version"]) != int(submission["expected_version"]):
            self._mark_submission(submission["id"], SUB_STALE, ["work.version_mismatch"])
            store.record_decision(self.conn, scan_id, "stale", target_id=work["id"],
                                  from_state=work["state"], submission_id=submission["id"],
                                  gaps=["work.version_mismatch"],
                                  detail={"expected": submission["expected_version"],
                                          "actual": work["version"]})
            report["stale"].append(submission["id"])
            return

        # 角色不符 → 退回
        state_role = workflow.WORK_STATES.get(work["state"], {}).get("role")
        if state_role and state_role != task["assigned_role"]:
            self._mark_submission(submission["id"], SUB_REJECTED, ["role.denied"])
            store.record_decision(self.conn, scan_id, "rejected", target_id=work["id"],
                                  from_state=work["state"], submission_id=submission["id"],
                                  gaps=["role.denied"])
            report["rejected"].append(submission["id"])
            return

        payload = json.loads(submission["payload"] or "{}")
        gaps, data = workflow.check_stage(self.conn, work, work["state"], payload,
                                          self.root, submission["session_id"])

        if gaps:
            # **逐欄位解說**：哪個欄位不標準、為什麼、標準是什麼、範例。
            details = workflow.explain_form(work["state"], payload)
            self._mark_submission(submission["id"], SUB_REJECTED, gaps, details)
            store.record_decision(self.conn, scan_id, "rejected", target_id=work["id"],
                                  from_state=work["state"], submission_id=submission["id"],
                                  gaps=gaps, detail={"gap_details": details})
            # 記錄一次退回 → 工單版本 +1，讓角色重讀後再交（避免同一版本無限重送）
            store.bump_version(self.conn, "works", work["id"], int(work["version"]))
            report["rejected"].append(submission["id"])
            # **退件不等於卡住**：要重新派一棒並把缺什麼講清楚，
            # 否則沒有人知道要重交 → 工單原地不動、沒有錯誤訊息。
            self._redispatch_after_rejection(work, task, gaps, scan_id, report,
                                             details=details)
            return

        # 全通過 → 套用
        try:
            workflow.apply_stage(self.conn, work, work["state"], data)
        except ValueError as exc:
            # **套用階段被擋 → 也要重新派工**。
            # 原本只標 rejected、升版本就 return → 角色交完就結束回合、
            # 沒有人通知它要重交 → **這張單靜靜卡住**（live 實測：W-002／W-003
            # 卡在 rework_design 兩小時，缺口是 `apply.rejected`）。
            gaps = [workflow.GAP_APPLY_REJECTED]
            self._mark_submission(submission["id"], SUB_REJECTED, gaps)
            store.record_decision(self.conn, scan_id, "rejected", target_id=work["id"],
                                  from_state=work["state"], submission_id=submission["id"],
                                  gaps=gaps, detail={"error": str(exc)})
            store.bump_version(self.conn, "works", work["id"], int(work["version"]))
            report["rejected"].append(submission["id"])
            self._redispatch_after_rejection(work, task, gaps, scan_id, report,
                                             detail=str(exc))
            return

        target, extra = workflow.next_state_for(self.conn, work, work["state"], data)

        # 交接檔
        handoff = payload.get("handoff") if isinstance(payload, dict) else None
        workflow.record_handoff(self.conn, work["id"], work["state"], target or work["state"],
                                task["assigned_role"] or "",
                                (workflow.WORK_STATES.get(target or "", {}) or {}).get("role") or "",
                                handoff if isinstance(handoff, dict) else {})

        updates = dict(extra)
        if target:
            updates["state"] = target
        if target == workflow.CLOSED:
            updates["verdict"] = workflow.compute_verdict(self.conn, work["id"])
        # 樂觀鎖：**更新 0 筆 = 這一筆提交過期了**（有人在守衛跑的同時改過版本）。
        # 之前沒檢查 rowcount → 競爭時仍照樣 mark applied、建下一棒、寫回執
        # → 兩個來源的更新疊在一起，狀態機被推歪。ISSUE 路徑本來有檢查，這裡補上。
        if updates:
            assignments = ", ".join("%s = ?" % k for k in updates)
            cursor = self.conn.execute(
                "UPDATE works SET %s, version = version + 1, updated_at = ?"
                " WHERE id = ? AND version = ?" % assignments,
                tuple(updates.values()) + (store.now(), work["id"], int(work["version"])))
        else:
            cursor = self.conn.execute(
                "UPDATE works SET version = version + 1, updated_at = ?"
                " WHERE id = ? AND version = ?",
                (store.now(), work["id"], int(work["version"])))
        if cursor.rowcount != 1:
            # 競爭或過期提交 → 標 stale，**不重試套用**（規格第 4 節）。
            # 注意：上面的 apply_stage／record_handoff 可能已寫進去了——
            # 它們只在「內容是這一棒的產出」時改動，且由同一交易包著；
            # 回到交易開頭就會 ROLLBACK（store.tx 的例外路徑不適用這裡，
            # 所以用 mark stale 標記，讓下一棒重讀正本後再做）。
            raise _StaleSubmission("works version mismatch at apply time")

        self.conn.execute("UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                          (store.now(), task["id"]))
        self._mark_submission(submission["id"], SUB_APPLIED, [])

        receipt_id = ""
        if receipt:
            receipt_id = self._record_receipt(submission, receipt)
        store.record_decision(self.conn, scan_id, "applied", target_id=work["id"],
                              from_state=work["state"], to_state=target or work["state"],
                              submission_id=submission["id"], receipt_id=receipt_id,
                              detail={"stage": work["state"]})
        store.journal(self.conn, "work.advance", work["id"], session_id=submission["session_id"],
                      detail={"from": work["state"], "to": target})

        report["applied"].append({"submission": submission["id"], "work": work["id"],
                                  "from": work["state"], "to": target})

        # 這一棒成功推進 → 下一棒拿到**完整**的回執重試配額。
        # `receipt_expires` 是「這張單累计到期幾次」，若跨棒帶走，上一棒的
        # 到期會把下一棒直接逼進 `awaiting_human`（W-001 实测：已推進到
        # rework_build、計數卻仍是 3，下一次到期即卡死）。推進就清零，讓每棒
        # 都有 `max_receipt_expires` 次補齊機會，而不是被上一棒吃掉。
        if target and target != workflow.CLOSED:
            self.conn.execute(
                "UPDATE counters SET value = 0 WHERE name = ?",
                ("receipt_expires:%s" % work["id"],))

        # 先保留下一棒的狀態，席位尚未認領時由後續 tick 補派。
        if target and target != workflow.CLOSED:
            self._create_task_for_state(work["id"], target, self.conn, bump=False)
        elif target == workflow.CLOSED:
            self._maybe_review_milestone(work.get("milestone_id"))

    # ------------------------------------------------------------ ISSUE 套用

    def _apply_issue_submission(self, submission: Dict[str, Any], task: sqlite3.Row,
                                receipt: Optional[Dict[str, Any]], scan_id: str,
                                report: Dict[str, Any]) -> None:
        issue = workflow.load_issue(self.conn, submission["issue_id"])
        if issue is None:
            self._mark_submission(submission["id"], SUB_STALE, ["issue.missing"])
            report["stale"].append(submission["id"])
            return
        if int(issue["version"]) != int(submission["expected_version"]):
            self._mark_submission(submission["id"], SUB_STALE, ["issue.version_mismatch"])
            report["stale"].append(submission["id"])
            return

        # 角色核對：ISSUE 每一棒都有指定責任角色。
        # `adjudicating` 的責任人是**原發起者**，不是 PLANNER。
        expected_role = (workflow.ISSUE_STATES.get(issue["state"]) or {}).get("role")
        if expected_role == "initiator":
            expected_role = issue["initiator_role"] or ""
        if expected_role and (task["assigned_role"] or "") != expected_role:
            self._mark_submission(submission["id"], SUB_REJECTED, ["role.denied"])
            store.record_decision(self.conn, scan_id, "rejected", target_id=issue["id"],
                                  from_state=issue["state"], submission_id=submission["id"],
                                  gaps=["role.denied"])
            report["rejected"].append(submission["id"])
            return

        payload = json.loads(submission["payload"] or "{}")
        gaps, data = self._check_issue_stage(issue, payload)
        if gaps:
            details = workflow.explain_form(issue["state"], payload, issue=True)
            self._mark_submission(submission["id"], SUB_REJECTED, gaps, details)
            store.record_decision(self.conn, scan_id, "rejected", target_id=issue["id"],
                                  from_state=issue["state"], submission_id=submission["id"],
                                  gaps=gaps, detail={"gap_details": details})
            # 退回 → ISSUE 版本 +1，讓角色重讀後可以再交（避免同一版本無限重送）
            store.bump_version(self.conn, "issues", issue["id"], int(issue["version"]))
            report["rejected"].append(submission["id"])
            # 同樣要重新派一棒（見 `_redispatch_after_rejection`）
            self._redispatch_issue_after_rejection(issue, task, gaps, scan_id, report,
                                                   details=details)
            return

        target = data["next"]
        returned = bool(data.get("returned"))

        # 轉位必須在轉移表裡（workflow.py 是唯一的轉移表）
        if target not in (workflow.ISSUE_TRANSITIONS.get(issue["state"]) or ()):
            self._mark_submission(submission["id"], SUB_REJECTED, ["issue.transition_invalid"])
            store.record_decision(self.conn, scan_id, "rejected", target_id=issue["id"],
                                  from_state=issue["state"], submission_id=submission["id"],
                                  gaps=["issue.transition_invalid"])
            report["rejected"].append(submission["id"])
            return

        # 交件內容寫回正本。**只寫 ISSUE 自己的欄位**——
        # ISSUE 不得暗中改母工單的驗收條件。
        # **合併，不要整份蓋掉**：ISSUE 的解法清單在 designing 棒交上來，
        # 蓋掉之後下一棒就看不到「案號」對照表了。
        merged_payload = dict(json.loads(issue.get("payload") or "{}") or {})
        merged_payload.update(payload or {})
        updates: Dict[str, Any] = {"payload": json.dumps(merged_payload, ensure_ascii=False)}
        if str(payload.get("completion_criteria") or "").strip():
            updates["completion_criteria"] = str(payload["completion_criteria"])
        if str(payload.get("countermeasure") or "").strip():
            updates["countermeasure"] = str(payload["countermeasure"])
        if payload.get("selection"):
            updates["selection"] = json.dumps(payload["selection"], ensure_ascii=False)
        if str(payload.get("answer") or "").strip():
            updates["answer"] = str(payload["answer"])
        if payload.get("linked_work_ids"):
            updates["linked_work_ids"] = json.dumps(payload["linked_work_ids"],
                                                     ensure_ascii=False)
        if str(payload.get("return_reason") or "").strip():
            updates["return_reason"] = str(payload["return_reason"])
        if returned:
            updates["return_count"] = int(issue["return_count"] or 0) + 1
        updates["state"] = target

        assignments = ", ".join("%s = ?" % k for k in updates)
        cursor = self.conn.execute(
            "UPDATE issues SET %s, version = version + 1, updated_at = ?"
            " WHERE id = ? AND version = ?" % assignments,
            tuple(updates.values()) + (store.now(), issue["id"], int(issue["version"])))
        if cursor.rowcount != 1:
            self._mark_submission(submission["id"], SUB_STALE, ["issue.version_mismatch"])
            report["stale"].append(submission["id"])
            return

        self.conn.execute("UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                          (store.now(), task["id"]))
        self._mark_submission(submission["id"], SUB_APPLIED, [])
        store.record_decision(self.conn, scan_id, "applied", target_id=issue["id"],
                              from_state=issue["state"], to_state=target,
                              submission_id=submission["id"],
                              detail={"returned": returned,
                                      "return_count": updates.get("return_count")})
        report["applied"].append({"submission": submission["id"], "issue": issue["id"],
                                  "from": issue["state"], "to": target})

        if returned:
            # 退回：累計次數；還能退就交回處理中，退到上限就標待人處理。
            self._after_issue_return(issue, int(updates["return_count"]), scan_id, report)
        else:
            # 向前推進（不是退回）＝換新一棒 → 重置回執重試配額。
            # 退回是同一棒內的重試，保留原配額；推進才給全新機會。
            self.conn.execute(
                "UPDATE counters SET value = 0 WHERE name = ?",
                ("receipt_expires:%s" % issue["id"],))
        # 其他轉態的下一棒由 `_dispatch_issues` 派（同一趟 tick 就會派）。

    def _check_issue_stage(self, issue: Dict[str, Any],
                           payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
        """ISSUE 的交件守衛（spec 第 4(b) 步的流程）。

        - **發起中**：問題、位置、已試方法、影響、要誰決定什麼、**完成要求**。
        - **解法設計中**：兩個**獨立** SUBAGENT 各提一案（跟工單 design 棒同一條硬約束）。
        - **對策中**：PLANNER 選哪一案 ＋ **對策**（要怎麼修）。
        - **依對策施工中**：WORKER 的產物與自檢。
        - **結案中**：原發起者附 `resolution` 結案，或附 `return_reason` **退回**
          （回到解法設計，重複整個流程）；退回次數累計，達上限標待人處理。
        """
        gaps: List[str] = []
        state = issue["state"]
        if state == workflow.ISSUE_RAISED:
            for field in ("trigger_kind", "problem", "location", "tried", "impact",
                          "question", "completion_criteria"):
                if not str(payload.get(field) or "").strip():
                    gaps.append(workflow.GAP_ISSUE_FIELD_MISSING)
        elif state == workflow.ISSUE_DESIGNING:
            gaps.extend(workflow.issue_solution_gaps(payload))
        elif state == workflow.ISSUE_HANDLING:
            # `selection` 可能是**字串**（角色交錯型別）→ 正規化成 dict 才不會
            # 在 `.get()` 上炸掉整趟 tick。
            selection = workflow.normalize_selection(payload.get("selection"))
            selected = str((selection or {}).get("solution_id") or "").strip()
            if not selected:
                gaps.append(workflow.GAP_ISSUE_FIELD_MISSING)
            else:
                # **選案用「案號」**（見 ISSUE 檔案的解法表）——指不到就是錯的。
                issue_solutions = (json.loads(issue.get("payload") or "{}") or {}).get("solutions")
                selectors = workflow.solution_selectors(
                    [item for item in (issue_solutions or []) if isinstance(item, dict)])
                if selectors and selected not in selectors:
                    gaps.append(workflow.GAP_ISSUE_FIELD_MISSING)
            if not str(payload.get("countermeasure") or "").strip():
                gaps.append(workflow.GAP_ISSUE_FIELD_MISSING)
        elif state == workflow.ISSUE_BUILDING:
            if not (payload.get("outputs") or []):
                gaps.append(workflow.GAP_ISSUE_FIELD_MISSING)
            if not str(payload.get("selfcheck") or "").strip():
                gaps.append(workflow.GAP_ISSUE_FIELD_MISSING)
        elif state == workflow.ISSUE_ADJUDICATING:
            has_resolution = bool(str(payload.get("resolution") or "").strip())
            has_return = bool(str(payload.get("return_reason") or "").strip())
            if not has_resolution and not has_return:
                gaps.append(workflow.GAP_ISSUE_FIELD_MISSING)

        max_return = store.limit_of(self.conn, "max_return", MAX_ISSUE_RETURNS)
        if state == workflow.ISSUE_DESIGNING and int(issue["return_count"] or 0) >= max_return:
            gaps.append(workflow.GAP_ISSUE_RETURN_LIMIT)

        if gaps:
            return gaps, {}
        if state == workflow.ISSUE_RAISED:
            return [], {"next": workflow.ISSUE_DESIGNING}
        if state == workflow.ISSUE_DESIGNING:
            return [], {"next": workflow.ISSUE_HANDLING,
                        "solutions": payload.get("solutions") or []}
        if state == workflow.ISSUE_HANDLING:
            return [], {"next": workflow.ISSUE_BUILDING,
                        "selection": payload.get("selection") or {},
                        "countermeasure": str(payload.get("countermeasure") or "")}
        if state == workflow.ISSUE_BUILDING:
            return [], {"next": workflow.ISSUE_ADJUDICATING}
        # 結案中：附了原因 → 退回（重複整個流程）；否則 → 結案
        if str(payload.get("return_reason") or "").strip():
            return [], {"next": workflow.ISSUE_DESIGNING, "returned": True}
        return [], {"next": workflow.ISSUE_CLOSED}

    # ------------------------------------------------------------ ISSUE 派工

    def _after_issue_return(self, issue: Dict[str, Any], new_count: int,
                             scan_id: str, report: Dict[str, Any]) -> None:
        """退回之後：**還能退就交回處理中，退到上限就標待人處理**（規格第 8 節）。

        「標待人處理」是這條路唯一的出口——流程不會自己繞下去。
        """
        max_return = store.limit_of(self.conn, "max_return", MAX_ISSUE_RETURNS)
        if new_count >= max_return:
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE issues SET awaiting_human = 1, awaiting_reason = ?,"
                    " updated_at = ? WHERE id = ?",
                    ("issue_return_limit", store.now(), issue["id"]))
                store.record_decision(self.conn, scan_id, "issue.awaiting_human",
                                      target_id=issue["id"],
                                      detail={"return_count": new_count,
                                              "max_return": max_return})
            report["awaiting"].append({"issue": issue["id"], "return_count": new_count,
                                       "reason": "issue_return_limit"})
            return
        # **重派到「退回後要去的那一棒」**（ISSUE_DESIGNING：重複整個流程——
        # spec 第 4(b) 步「未生效就在同一張單上重複上一個流程」）。
        # 寫死 ISSUE_HANDLING 會讓 task 的 stage 跟 issue 的 state 不一致
        # → 角色交的那一棒跟引擎等的不是同一棒 → 退件、流程卡住。
        task_id = self._create_task_for_issue_state(issue["id"], workflow.ISSUE_DESIGNING)
        if task_id:
            report["dispatched"].append({"issue": issue["id"], "task": task_id})

    def _create_task_for_issue_state(self, issue_id: str, state: str,
                                     note: str = "") -> Optional[str]:
        """為 ISSUE 的下一棒建立 task 與 outbox。

        誰接（規格第 8 節）：
        - `handling`／`building` → PLANNER（回答或建立關聯工單）。
        - `adjudicating` → **原發起者**（驗證結果，結案或附原因退回）。
        """
        issue = workflow.load_issue(self.conn, issue_id)
        if issue is None:
            return None
        contract = workflow.ISSUE_STATES.get(state) or {}
        role = contract.get("role")
        if not role:
            return None
        if role == "initiator":
            session_id = issue["initiator_session"] or ""
            assigned_role = issue["initiator_role"] or ""
        else:
            assigned = self._pick_session(role)
            if assigned is None:
                return None
            session_id = assigned["session_id"]
            assigned_role = role
        if not session_id:
            return None
        with store.tx(self.conn):
            task_id = store.next_id(self.conn, "tasks", "T", 6)
            self.conn.execute(
                "INSERT INTO tasks(id, work_id, issue_id, milestone_id, stage,"
                " assigned_session, assigned_role, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?, 'open', ?, ?)",
                (task_id, None, issue_id, None, state, session_id, assigned_role,
                 store.now(), store.now()))
            issue = workflow.load_issue(self.conn, issue_id) or issue
            text = self._issue_prompt(
                issue, state, task_id,
                note or self._pending_rejection_note(self.conn, "issue", issue_id))
            self._enqueue_outbox(self.conn, task_id, "dispatch", session_id,
                                 assigned_role, issue_id, text)
        return task_id

    def _skill_of_issue(self, issue: Dict[str, Any]) -> str:
        """ISSUE 搭配的專案 SKILL＝**母工單的**那個（spec：依專案 SKILL 解法）。"""
        work_id = issue.get("work_id")
        if not work_id:
            return ""
        work = workflow.load_work(self.conn, str(work_id))
        return (work or {}).get("skill") or ""

    def _issue_prompt(self, issue: Dict[str, Any], state: str,
                      task_id: str = "", note: str = "") -> str:
        """組 ISSUE 的派工提示詞。

        **必須帶 `task_id`**——回執核對靠它把平台訊息與這次任務對上
        （規格第 7 節）。少了它，ISSUE 的提交**永遠核不過回執**、
        流程會卡在第一筆，而且沒有錯誤訊息。

        **不含工作狀態**：問題、已試方法、完成要求由角色自己查
        `fbflow_read(target_kind=issue)`；這一棒要交的欄位直接列在訊息裡。
        """
        lines = [
            "【FBF ENGINE ISSUE 派工】",
            "task_id: %s" % task_id,
            "issue_id: %s" % issue["id"],
            "expected_version: %d" % int(issue.get("version") or 1),
            "專案 SKILL：%s" % (self._skill_of_issue(issue) or "（未指定）"),
            "",
            "先讀這張 ISSUE 的**正本快照**（問題、已試方法、完成要求）："
            "fbflow_read(target_kind=issue, target_id=%s)。" % issue["id"],
            "",
            "## 現在這一棒：要交的東西",
            "",
        ]
        lines.extend(prompts.contract_lines(workflow.issue_stage_contract(state), brief=True))
        method_notes = prompts.issue_method_note(state)
        if method_notes:
            lines.append("")
            lines.append("## 方法（怎麼做這件事）")
            for item in method_notes:
                lines.append("- %s" % item)
        lines += [
            "",
            "## 怎麼交",
            "用 MCP 工具回報（不是 CLI）。",
            "  fbflow_report_issue(task_id, issue_id, expected_version, payload, message_id)",
            "  - payload 要含上面「現在這一棒」列出的每一個欄位。",
            "  - stage 可留空：引擎依 task_id 判斷這一棒。",
            "",
            "讀取：",
            "  fbflow_read(target_kind=issue, target_id=%s)" % issue["id"],
            "  fbflow_status()",
        ]
        if note:
            lines += ["", note]
        return "\n".join(lines)

    def _linked_works_closed(self, issue: Dict[str, Any]) -> bool:
        """關聯工單是不是都結案了。

        規格第 8 節：「關聯工單全結案後，原發起者驗證結果」——
        還沒結案就交給發起者驗收，等於讓他驗一個沒做完的東西。
        """
        try:
            linked = json.loads(issue.get("linked_work_ids") or "[]")
        except (TypeError, ValueError):
            linked = []
        if not isinstance(linked, list) or not linked:
            return True
        for work_id in linked:
            row = self.conn.execute(
                "SELECT state FROM works WHERE id = ?", (str(work_id),)).fetchone()
            if row is None or row["state"] != workflow.CLOSED:
                return False
        return True

    def _dispatch_issues(self, report: Dict[str, Any]) -> None:
        """把沒有在跑的 ISSUE 派出去（spec 第 4(b) 步）。

        - `raised`：發起者已經把問題交上來 → 引擎推進到**解法設計中**，交給 THINKER。
        - `designing`：THINKER 出兩個獨立 SUBAGENT 的解法。
        - `handling`：PLANNER 選一案並填**對策**。
        - `building`：WORKER 依對策執行並自檢。
        - `adjudicating`：**等關聯工單全部結案**才交給原發起者驗證。
        - 已標待人處理（退回達上限）→ 不派工，等人裁決。
        """
        rows = self.conn.execute(
            "SELECT * FROM issues WHERE state <> ? AND awaiting_human = 0 ORDER BY id",
            (workflow.ISSUE_CLOSED,)).fetchall()
        for row in rows:
            issue = dict(row)
            if self.conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks WHERE issue_id = ? AND status = 'open'",
                    (issue["id"],)).fetchone()["n"]:
                continue
            state = issue["state"]
            if state == workflow.ISSUE_RAISED:
                # 發起者已交問題 → 引擎推進到解法設計中（這一棒沒有要交件的內容）
                with store.tx(self.conn):
                    self.conn.execute(
                        "UPDATE issues SET state = ?, version = version + 1, updated_at = ?"
                        " WHERE id = ? AND version = ?",
                        (workflow.ISSUE_DESIGNING, store.now(), issue["id"],
                         int(issue["version"])))
                state = workflow.ISSUE_DESIGNING
            elif state == workflow.ISSUE_ADJUDICATING and not self._linked_works_closed(issue):
                continue
            task_id = self._create_task_for_issue_state(issue["id"], state)
            if task_id:
                report["dispatched"].append({"issue": issue["id"], "task": task_id})
            else:
                role = (workflow.ISSUE_STATES.get(state) or {}).get("role") or ""
                if role and role != "initiator":
                    self._notify_unassigned("issue", issue["id"], state, role, report)

    # ------------------------------------------------------------ 回執

    def _verify_receipt(self, submission: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """查平台回執。**查不到不等於完成。**

        回執核對要五項全中（規格第 7 節）：task_id 對得上、message 由被指派 session 產生、
        時間在派工後、平台標示完成、**message_id 未被其他 submission 使用**。
        缺任一項 → `unverified`，不建立可信 run，不推進。

        **沒有 message_id 也算缺項**（回執無從核對），不是「免驗」。

        回傳的 dict 帶一個 `permanent` 旗標：
        - `permanent=False`（預設）→ 暫時性，標 `unverified`，下一輪再查。
        - `permanent=True` → 重試沒用（回執被別的提交用過），要退回要求重交。
        """
        message_id = submission.get("message_id") or ""
        if not message_id:
            return {"verified": False, "reason": "receipt.message_id_missing",
                    "permanent": True,
                    "reason_detail": "提交沒有帶平台 message ID，無法核對回執"}

        # 這條回執必須是「這一筆提交專屬」的。
        # message_id 被別的 submission 用過 → 這不是可信回執（規格第 7 節）。
        reused = self.conn.execute(
            "SELECT id FROM submissions WHERE message_id = ? AND id <> ? LIMIT 1",
            (message_id, submission["id"])).fetchone()
        if reused is not None:
            return {"verified": False, "reason": REASON_RECEIPT_MESSAGE_REUSED,
                    "permanent": True,
                    "reason_detail": "message %s 已被提交 %s 用過"
                                      % (message_id, reused["id"])}
        existing = self.conn.execute(
            "SELECT id, task_id FROM receipts WHERE session_id = ? AND message_id = ?",
            (submission["session_id"], message_id)).fetchone()
        if existing is not None and existing["task_id"] != submission["task_id"]:
            return {"verified": False, "reason": REASON_RECEIPT_MESSAGE_REUSED,
                    "permanent": True,
                    "reason_detail": "message %s 已被任務 %s 的回執用過"
                                      % (message_id, existing["task_id"])}

        task = self.conn.execute("SELECT * FROM tasks WHERE id = ?",
                                 (submission["task_id"],)).fetchone()
        if task is None:
            return None
        role_row = self.conn.execute(
            "SELECT * FROM roles WHERE session_id = ?", (submission["session_id"],)).fetchone()
        agent = (role_row["agent"] if role_row else "opencode") or "opencode"
        # 路由跟派工同一套規則：看 handle 前綴，不看 roles.agent
        # （THINKER 席位 agent 欄記 opencode、實際卻是 host 終端裡的 Claude——
        #  照 agent 欄路由會拿 term_ handle 去問 OpenCode，永遠 unverified）。
        if platform.is_host_handle(submission["session_id"]):
            agent = "claude"
        if self.verifier is not None:
            verify = self.verifier
        else:
            verify = receipts.verifier_for(agent)
        dispatched_at = self._dispatched_at(task["id"])
        submitted_at = submission["created_at"]
        try:
            result = verify(self.endpoint, submission["session_id"], message_id,
                             task["id"], dispatched_at, submitted_at, self.binary)
        except Exception:
            return {"verified": False, "reason": receipts.UNVERIFIED,
                    "reason_detail": "回執查詢失敗"}
        if isinstance(result, dict):
            result.setdefault("permanent", False)
        return result

    def _dispatched_at(self, task_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT created_at FROM outbox WHERE task_id = ? ORDER BY id LIMIT 1",
            (task_id,)).fetchone()
        return row["created_at"] if row else None

    def _record_receipt(self, submission: Dict[str, Any],
                        receipt: Dict[str, Any]) -> str:
        """寫下可信回執。

        **唯一鍵衝突時要回傳既有那筆的真實 ID**——
        回傳剛產生但根本沒寫進去的 ID，會讓裁決紀錄指向一筆不存在的回執。
        （重用情形已經在 `_verify_receipt` 擋掉；這裡是防禦性處理。）
        """
        receipt_id = "R-" + uuid.uuid4().hex[:12]
        message_id = receipt.get("message_id") or ""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO receipts(id, task_id, session_id, message_id, model_id,"
            " context_id, completed_at, verified_at) VALUES (?,?,?,?,?,?,?,?)",
            (receipt_id, submission["task_id"], submission["session_id"],
             message_id, receipt.get("model_id") or "",
             receipt.get("context_id") or "", receipt.get("finish") or "",
             store.now()))
        if cur.rowcount == 0:
            row = self.conn.execute(
                "SELECT id FROM receipts WHERE session_id = ? AND message_id = ?",
                (submission["session_id"], message_id)).fetchone()
            return row["id"] if row else ""
        return receipt_id

    def _bump_work_version(self, submission: Dict[str, Any]) -> None:
        """退回時讓**目標單**版本 +1——角色才能用新的 message_id / 新版本重交。

        **ISSUE 也必須 +1**（原本只處理 works）：ISSUE 退件後版本不動 →
        角色用同一個版本重交 → 撞上 `(task_id, session_id, stage, expected_version)`
        的唯一約束 → `submit_issue` 回傳原本那筆（已退件的）submission →
        **這張 ISSUE 再也推不動**（實測會卡死）。
        """
        work_id = submission.get("work_id")
        if work_id:
            row = self.conn.execute("SELECT version FROM works WHERE id = ?",
                                    (work_id,)).fetchone()
            if row is not None:
                store.bump_version(self.conn, "works", work_id, int(row["version"]))
            return
        issue_id = submission.get("issue_id")
        if issue_id:
            row = self.conn.execute("SELECT version FROM issues WHERE id = ?",
                                    (issue_id,)).fetchone()
            if row is not None:
                store.bump_version(self.conn, "issues", issue_id, int(row["version"]))

    def _deadline_of(self, submission: Dict[str, Any]) -> Optional[float]:
        row = self.conn.execute("SELECT created_at FROM submissions WHERE id = ?",
                                (submission["id"],)).fetchone()
        if row is None:
            return None
        try:
            started = time.mktime(time.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
        return started + UNVERIFIED_DEADLINE_SECONDS

    def _mark_submission(self, submission_id: str, status: str,
                         gaps: List[str],
                         details: Optional[List[Dict[str, Any]]] = None) -> None:
        self.conn.execute(
            "UPDATE submissions SET status = ?, gaps = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(gaps, ensure_ascii=False), store.now(), submission_id))
        if status == SUB_REJECTED:
            # **每一次退件都留下 LOG**（使用者規則 2026-11-24）：
            # 退件單據的完整副本、退件原因、為了哪個欄位退件、退的是誰的件。
            self._write_rejection_log(submission_id, gaps, details or [])

    def _write_rejection_log(self, submission_id: str, gaps: List[str],
                             details: List[Dict[str, Any]]) -> None:
        """寫一筆**退件 LOG**（`rejection_log` 表）。

        **為什麼要這張表**：退件紀錄原本只散在 `decisions.detail` 與 `submissions.gaps`，
        「退的是誰的件、為了哪個欄位退、當下那份單據長什麼樣」都要自己拼 →
        拼不出來就沒辦法復盤（復盤要拿退件紀錄當證據）。

        欄位對應（使用者指定）：
        - **退件單據的完整副本** → `target_snapshot`（工單／ISSUE 當下的整列）
          ＋ `payload`（被退的那次交件內容）。
        - **退件原因** → `reason`（缺口代碼的人可讀說明）＋ `gaps`（原始代碼）。
        - **為了哪個欄位退件** → `fields`（逐欄位解說裡的欄位名）。
        - **退的是誰的件** → `session_id` ＋ `role` ＋ `short_name`。
        """
        row = self.conn.execute("SELECT * FROM submissions WHERE id = ?",
                                (submission_id,)).fetchone()
        if row is None:
            return
        target_kind = "issue" if row["issue_id"] else "work"
        target_id = row["work_id"] or row["issue_id"] or ""
        if target_kind == "work":
            snapshot = workflow.load_work(self.conn, target_id) or {}
        else:
            snapshot = workflow.load_issue(self.conn, target_id) or {}
        seat = self.conn.execute(
            "SELECT role, short_name FROM roles WHERE session_id = ?",
            (row["session_id"],)).fetchone()
        fields: List[str] = []
        for item in details:
            if isinstance(item, dict) and item.get("field"):
                fields.append(str(item["field"]))
        reason = "；".join(workflow.GAP_HINTS.get(gap, gap) for gap in gaps)
        self.conn.execute(
            "INSERT INTO rejection_log(target_kind, target_id, submission_id, stage,"
            " session_id, role, short_name, gaps, fields, reason, payload,"
            " target_snapshot, details, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (target_kind, target_id, submission_id, row["stage"] or "",
             row["session_id"] or "",
             (seat["role"] if seat else ""),
             (seat["short_name"] if seat else ""),
             json.dumps(list(gaps), ensure_ascii=False),
             json.dumps(fields, ensure_ascii=False),
             reason, row["payload"] or "{}",
             json.dumps(snapshot, ensure_ascii=False, default=str),
             json.dumps(details, ensure_ascii=False, default=str),
             store.now()))

    def _clear_awaiting(self, work_id: str) -> None:
        """解除工單的「停等人」標記。

        **為什麼套用成功就要解除**：那個標記是「這一棒卡住了，等人來處理」。
        這一棒既然已經收件、驗過回執、並往前推進，就沒有東西需要人處理了。

        不解除的後果很靜默：下一棒（例如 PLANNER）的 task 建不出來
        （`_create_task_for_state` 看到 `awaiting_human` 就回頭），
        工單就停在原地、沒有錯誤訊息、也沒有下一個人接手——最難查的那種卡住。
        """
        self.conn.execute(
            "UPDATE works SET awaiting_human = 0, awaiting_reason = NULL, updated_at = ?"
            " WHERE id = ? AND awaiting_human <> 0",
            (store.now(), work_id))

    def _rejection_note(self, gaps: List[str], kind: str = "work",
                        detail: str = "",
                        details: Optional[List[Dict[str, str]]] = None,
                        task_hint: str = "") -> str:
        """退件說明：**哪個欄位不標準、為什麼、標準是什麼、範例長什麼樣**。

        光丟缺口代碼不夠——角色看不懂就改不對（規格第 5 節：
        穩定缺口代碼、缺哪個欄位與可行下一步）。

        `details` 來自 `workflow.explain_form()`：它照**同一份契約**逐欄位檢查
        這一筆交件，所以講的就是守衛真正擋下的那一格。

        `task_hint` 說明 task_id 從哪裡來——派工訊息裡有 task_id，
        但**喚醒訊息刻意不帶 task_id**（帶了會被回執核對當成假回執），
        那邊要改成「用 fbflow_status() 查」。
        """
        noun = "ISSUE" if kind == "issue" else "工單"
        hint = task_hint or "用上面那個 task_id"
        lines = ["【退件】這一棒被退回，請依下面的缺口重新交件（%s）：" % hint]
        explained = set()
        # **總判類的缺口**（verdict.fail／partial）：不是表單欄位問題，
        # 但同樣要講清楚「哪個欄位、為什麼、標準是什麼」。
        verdict_details = []
        for gap in gaps:
            item = workflow.explain_verdict(gap)
            if item is not None:
                verdict_details.append(item)
        for item in (verdict_details or []) + list(details or []):
            explained.add(item.get("code") or "")
            lines.append("  - 欄位 `%s`：%s"
                         % (item.get("field") or "?", item.get("problem") or "不標準"))
            standard = item.get("standard") or ""
            if standard:
                lines.append("      標準：%s" % standard)
            example = item.get("example") or ""
            if example:
                lines.append("      範例：%s" % example)
        for gap in gaps:
            if gap in explained:
                continue
            hint = workflow.GAP_HINTS.get(gap, "")
            lines.append("  - %s%s" % (gap, ("｜" + hint) if hint else ""))
        if detail:
            lines.append("  引擎實際擋下的原因：%s" % detail)
        lines.append("%s 版本已 +1；請先用 fbflow_read 重新讀%s，"
                     "再照「現在某一棒」段落列的欄位重交。"
                     % (noun, noun))
        lines.append("**表單以外的欄位是附加項**——引擎不檢查它們，"
                     "也不會因為它們退件；把表單裡的欄位照標準填好就行。")
        return "\n".join(lines)

    def _target_of(self, submission: Dict[str, Any]) -> Tuple[str, str, str]:
        """這一筆提交屬於哪一張單、它現在是什麼狀態。"""
        if submission.get("work_id"):
            work = workflow.load_work(self.conn, submission["work_id"])
            return "work", submission["work_id"], (work or {}).get("state", "")
        issue_id = submission.get("issue_id")
        if issue_id:
            issue = workflow.load_issue(self.conn, issue_id)
            return "issue", issue_id, (issue or {}).get("state", "")
        return "", "", ""

    def _redispatch_rejected(self, submission: Dict[str, Any], gaps: List[str],
                              scan_id: str, report: Dict[str, Any],
                              detail: str = "") -> None:
        """**退回不等於卡住**：永久性問題被退件後，要重新派一棒並講清楚缺什麼。

        沒有這一步：角色交完就結束回合（它只收到「已收到」），
        沒有人告訴它「要重交」→ 單子靜靜停住、沒有錯誤訊息。
        守衛退件走的 `_redispatch_after_rejection` 就是這件事；
        **回執／context 永久失敗這一條路原本漏了**（實測會卡死）。

        `detail` 是引擎實際擋下的原因（例：message X 已被提交 S-000016 用過）
        ——**要帶給角色**，否則它只知道「換一個 id」卻不知道原來那個撞到誰。
        """
        task = self.conn.execute("SELECT * FROM tasks WHERE id = ?",
                                 (submission["task_id"],)).fetchone()
        if task is None:
            return
        kind, target_id, _state = self._target_of(submission)
        if kind == "work":
            work = workflow.load_work(self.conn, target_id)
            if work is not None:
                self._redispatch_after_rejection(work, task, gaps, scan_id, report,
                                                 detail=detail)
        elif kind == "issue":
            issue = workflow.load_issue(self.conn, target_id)
            if issue is not None:
                self._redispatch_issue_after_rejection(issue, task, gaps, scan_id, report)

    def _redispatch_after_rejection(self, work: Dict[str, Any], task: sqlite3.Row,
                                    gaps: List[str], scan_id: str,
                                    report: Dict[str, Any],
                                    detail: str = "",
                                    details: Optional[List[Dict[str, str]]] = None
                                    ) -> None:
        """退件後**重新派一棒**（同一階段、同一角色），並把缺什麼講清楚。

        **為什麼一定要有這一步**：守衛是在 tick 裡跑的，角色交件當下只會收到
        「已收到，狀態由協調器推進」。退件結果是**事後**才有的——
        沒有重新派工，沒有人知道要重交，工單就靜靜停在原地、沒有錯誤訊息。

        退件次數有上限（不允許無限繞圈）：超過就標待人處理。
        """
        count = int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE action = 'rejected'"
            " AND target_id = ? AND from_state = ?",
            (work["id"], work["state"])).fetchone()["n"])
        if count > MAX_STAGE_REJECTIONS:
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE works SET awaiting_human = 1, awaiting_reason = ?,"
                    " updated_at = ? WHERE id = ?",
                    ("submit_rejected", store.now(), work["id"]))
            report["errors"].append(
                "工單 %s 在 %s 階段退件 %d 次，標待人處理"
                % (work["id"], work["state"], count))
            return

        # 舊的那一棒收掉（角色改交新的 task）
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (store.now(), task["id"]))

        task_id = self._create_task_for_state(
            work["id"], work["state"], self.conn, bump=False,
            note=self._rejection_note(gaps, "work", detail, details))
        if task_id:
            report["dispatched"].append({"work": work["id"], "task": task_id,
                                         "note": "rejected"})

    def _redispatch_issue_after_rejection(self, issue: Dict[str, Any], task: sqlite3.Row,
                                          gaps: List[str], scan_id: str,
                                          report: Dict[str, Any],
                                          details: Optional[List[Dict[str, str]]] = None
                                          ) -> None:
        """ISSUE 退件後重新派一棒（理由與工單同一套）。"""
        count = int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE action = 'rejected'"
            " AND target_id = ? AND from_state = ?",
            (issue["id"], issue["state"])).fetchone()["n"])
        if count > MAX_STAGE_REJECTIONS:
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE issues SET awaiting_human = 1, awaiting_reason = ?,"
                    " updated_at = ? WHERE id = ?",
                    ("submit_rejected", store.now(), issue["id"]))
            report["errors"].append(
                "ISSUE %s 退件 %d 次，標待人處理" % (issue["id"], count))
            return

        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (store.now(), task["id"]))
        task_id = self._create_task_for_issue_state(
            issue["id"], issue["state"],
            note=self._rejection_note(gaps, "issue", "", details))
        if task_id:
            report["dispatched"].append({"issue": issue["id"], "task": task_id,
                                         "note": "rejected"})

    # ------------------------------------------------------------ 派工

    def _pending_rejection_note(self, conn: sqlite3.Connection, kind: str,
                                target_id: str, task_hint: str = "") -> str:
        """這張單有沒有「退件了、還沒重交過」？有的話把缺什麼帶進派工提示詞。

        **為什麼要在派工時講**：角色讀不到上一輪的對話，提示詞又不帶狀態
        （spec 第 70 行）——沒有這段，它會以為自己還沒交過，然後交出同樣的東西。
        live 實測：THINKER 收到重派後回「引擎壞了，我不重交」，
        因為派工裡沒有說清楚「上次被退是因為重用上一輪的 context id」。
        """
        info = workflow.last_rejection(conn, kind, target_id)
        if not info:
            return ""
        return self._rejection_note(info.get("gaps") or [], kind,
                                    info.get("error") or "",
                                    info.get("gap_details") or [],
                                    task_hint=task_hint)

    def _create_task_for_state(self, work_id: str, state: str,
                               conn: sqlite3.Connection,
                               bump: bool = True,
                               note: str = "") -> Optional[str]:
        """為下一個狀態建立 task 與 outbox，並把工單轉到該狀態。

        `bump=True` 會一併把工單版本 +1（派工本身就是一次狀態變更）；
        `bump=False` 用在「套用提交的同一個交易裡」，那裡版本已經 +1 過了。

        `note` 是附加在派工提示詞尾端的說明（退件時用來講清楚缺什麼）。
        """
        role = (workflow.WORK_STATES.get(state) or {}).get("role")
        if not role:
            return None
        work = workflow.load_work(conn, work_id)
        if work is None:
            return None
        if int(work.get("awaiting_human") or 0):
            return None

        assigned = self._pick_session(role)
        if assigned is None:
            return None
        task_id = store.next_id(conn, "tasks", "T", 6)
        conn.execute(
            "INSERT INTO tasks(id, work_id, issue_id, stage, assigned_session, assigned_role,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?, 'open', ?, ?)",
            (task_id, work_id, None, state, assigned["session_id"], role,
             store.now(), store.now()))
        conn.execute(
            "UPDATE works SET state = ?, assigned_session = ?, assigned_role = ?,"
            " version = version + ?, updated_at = ? WHERE id = ?",
            (state, assigned["session_id"], role, 1 if bump else 0, store.now(), work_id))

        # 重讀工單：版本剛 +1，提示詞要帶的是**新版本**。
        work = workflow.load_work(conn, work_id) or work

        # WORKER 的產出放在**自己簡稱**命名的資料夾（spec 第 71 行）。
        output_dir = ""
        if role == "WORKER":
            short_name = assigned.get("short_name") or ""
            output_dir = ("%s/" % short_name) if short_name else ""

        contract = workflow.stage_contract(state)
        text = platform.build_dispatch_text(
            {"id": task_id, "stage": state}, work, contract, role,
            note or self._pending_rejection_note(conn, "work", work_id),
            skill=work.get("skill") or "",
            output_dir=output_dir,
            method_notes=prompts.method_note(state))
        self._enqueue_outbox(conn, task_id, "dispatch", assigned["session_id"], role,
                             work_id, text)
        return task_id

    def _enqueue_outbox(self, conn: sqlite3.Connection, task_id: str, kind: str,
                        session_id: str, role: str, target_id: str, text: str) -> int:
        text += "\n\n" + workflow.SESSION_IDLE_RULE
        cur = conn.execute(
            "INSERT INTO outbox(task_id, kind, recipient_session, recipient_role, target_id,"
            " payload, status, attempts, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,0,?,?)",
            (task_id, kind, session_id, role, target_id, text, OUT_QUEUED,
             store.now(), store.now()))
        return int(cur.lastrowid)

    def _grill_needed(self) -> bool:
        """專案是否還需要 GRILL（尚無**已確認**需求）。

        人跑 `requirement confirm` 就是同意 → 這段結束。
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM requirements WHERE confirmed_at IS NOT NULL"
        ).fetchone()
        return int(row["n"]) == 0

    def _dispatch_grill(self, report: Dict[str, Any]) -> None:
        """三段啟動的第 2 段：派 GRILL 給 PLANNER（分析→複述→問同意）。

        - 已有 open 的 grill task → 不重派。
        - 沒有已確認需求才派（有人類同意後這段就結束）。
        - PLANNER 席位沒綁 session → 等下一輪（`_pick_session` 回 None）。
        - 這是 task 不是 work——`_dispatch_pending` 只掃 works，不會重複。
        """
        if not self._grill_needed():
            # 人類已確認 → 收掉還開著的 grill task（同意就是這段的完成訊號）
            with store.tx(self.conn):
                open_ids = [r["id"] for r in self.conn.execute(
                    "SELECT id FROM tasks WHERE stage = ? AND status = 'open'",
                    (workflow.STAGE_GRILL,)).fetchall()]
                for task_id in open_ids:
                    self.conn.execute(
                        "UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                        (store.now(), task_id))
                    store.journal(self.conn, "grill.close", task_id,
                                  detail={"reason": "requirement.confirmed"})
            return
        if self.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE stage = ? AND status = 'open'",
                (workflow.STAGE_GRILL,)).fetchone()["n"]:
            return
        assigned = self._pick_session("PLANNER")
        if assigned is None:
            return  # PLANNER 還沒綁上 → 下一輪再試
        with store.tx(self.conn):
            task_id = store.next_id(self.conn, "tasks", "T", 6)
            self.conn.execute(
                "INSERT INTO tasks(id, work_id, issue_id, milestone_id, stage,"
                " assigned_session, assigned_role, status, created_at, updated_at)"
                " VALUES (?,?,NULL,NULL,?,?,?, 'open', ?, ?)",
                (task_id, None, workflow.STAGE_GRILL,
                 assigned["session_id"], "PLANNER", store.now(), store.now()))
            text = prompts.render_grill_prompt(task_id, self.root)
            self._enqueue_outbox(self.conn, task_id, "dispatch",
                                 assigned["session_id"], "PLANNER", task_id, text)
            store.journal(self.conn, "grill.dispatch", task_id,
                          session_id=assigned["session_id"])
        report["dispatched"].append({"grill": task_id, "task": task_id})

    def _dispatch_decompose(self, report: Dict[str, Any]) -> None:
        """三段啟動的第 3 段：人類同意（需求已確認）→ 提示 PLANNER 分解建工單。

        守衛（全部機器可查，不靠提示詞自覺）：
        - 沒有已確認需求 → 不派（第 2 段還沒完成）。
        - 已有**任何**工單 → 收掉開著的 decompose task（流水線接管），不重派。
        - 已有 open 的 decompose task → 不重派。
        - PLANNER 席位沒綁 session → 等下一輪。
        """
        confirmed = self.conn.execute(
            "SELECT COUNT(*) AS n FROM requirements WHERE confirmed_at IS NOT NULL"
        ).fetchone()["n"]
        if not confirmed:
            return
        with store.tx(self.conn):
            works = self.conn.execute(
                "SELECT COUNT(*) AS n FROM works").fetchone()["n"]
            if works:
                # 第一張工單已建 → 收掉 decompose task（流水線接管）
                open_ids = [r["id"] for r in self.conn.execute(
                    "SELECT id FROM tasks WHERE stage = ? AND status = 'open'",
                    (workflow.STAGE_DECOMPOSE,)).fetchall()]
                for task_id in open_ids:
                    self.conn.execute(
                        "UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                        (store.now(), task_id))
                    store.journal(self.conn, "decompose.close", task_id,
                                  detail={"reason": "first.work.created"})
                return
        if self.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE stage = ? AND status = 'open'",
                (workflow.STAGE_DECOMPOSE,)).fetchone()["n"]:
            return
        assigned = self._pick_session("PLANNER")
        if assigned is None:
            return  # PLANNER 還沒綁上 → 下一輪再試
        with store.tx(self.conn):
            task_id = store.next_id(self.conn, "tasks", "T", 6)
            self.conn.execute(
                "INSERT INTO tasks(id, work_id, issue_id, milestone_id, stage,"
                " assigned_session, assigned_role, status, created_at, updated_at)"
                " VALUES (?,?,NULL,NULL,?,?,?, 'open', ?, ?)",
                (task_id, None, workflow.STAGE_DECOMPOSE,
                 assigned["session_id"], "PLANNER", store.now(), store.now()))
            text = prompts.render_decompose_prompt(task_id, self.root)
            self._enqueue_outbox(self.conn, task_id, "dispatch",
                                 assigned["session_id"], "PLANNER", task_id, text)
            store.journal(self.conn, "decompose.dispatch", task_id,
                          session_id=assigned["session_id"])
        report["dispatched"].append({"decompose": task_id, "task": task_id})

    def unassigned_targets(self) -> List[Dict[str, Any]]:
        """**派不出去、沒有人接手的單子**——由引擎自己說出來。

        live 實測（2026-11-23 00:43~10:41）：角色沒認領席位 → 派工送不出去、
        10 小時零提交，而引擎**完全沒有留下任何訊息**（journal 只有
        dispatch.verify）。沒有訊息就沒有人知道要開 session → 靜靜卡到天亮。

        這份清單是**引擎的聲音**：MCP 的 `fbflow_status` 會帶出去，
        人只要問引擎就知道哪張單在等人。
        """
        out: List[Dict[str, Any]] = []
        works = self.conn.execute(
            "SELECT id, state FROM works WHERE state <> ? AND awaiting_human = 0"
            " AND COALESCE(superseded_by, '') = ''"
            " AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.work_id = works.id"
            " AND t.status = 'open') ORDER BY id", (workflow.CLOSED,)).fetchall()
        for row in works:
            # 待派工的單子下一棒是「解法設計中」（跟 `_dispatch_pending` 同一套算法）。
            stage = workflow.DESIGN if row["state"] == workflow.BACKLOG else row["state"]
            role = (workflow.WORK_STATES.get(stage) or {}).get("role") or ""
            if not role or self._pick_session(role) is not None:
                continue
            out.append({"kind": "work", "id": row["id"], "state": row["state"],
                        "role": role,
                        "message": "工單 %s 在 %s 沒有可用席位（%s 沒人認領），派不出去"
                                   % (row["id"], row["state"], role)})
        issues = self.conn.execute(
            "SELECT id, state FROM issues WHERE state <> ? AND awaiting_human = 0"
            " AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.issue_id = issues.id"
            " AND t.status = 'open') ORDER BY id", (workflow.ISSUE_CLOSED,)).fetchall()
        for row in issues:
            role = (workflow.ISSUE_STATES.get(row["state"]) or {}).get("role") or ""
            if not role or role == "initiator" or self._pick_session(role) is not None:
                continue
            out.append({"kind": "issue", "id": row["id"], "state": row["state"],
                        "role": role,
                        "message": "ISSUE %s 在 %s 沒有可用席位（%s 沒人認領），派不出去"
                                   % (row["id"], row["state"], role)})
        return out

    def health_check(self) -> List[Dict[str, Any]]:
        """**巡檢：哪張單有問題**——由引擎自己判斷、自己說。

        live 實測（2026-11-23 一天）：四段空白合計 20.2 小時（佔 89%），
        每一段的樣子都不一樣，但共同點是**沒有人知道**：
        沒席位、session 死掉、退件沒重派、停滯、等裁決。
        引擎不能只會派工與收件——**它要會看**。

        回傳每個問題一筆：code／kind／id／message／age_minutes。
        """
        problems: List[Dict[str, Any]] = []
        now = time.time()

        def _age(ts: Optional[str]) -> float:
            if not ts:
                return 0.0
            try:
                return (now - time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))) / 60.0
            except ValueError:
                return 0.0

        # 1) 派不出去：沒有席位
        for item in self.unassigned_targets():
            problems.append({"code": "no_seat", "kind": item["kind"], "id": item["id"],
                             "message": item["message"], "age_minutes": 0})

        # 2) 有開著的 task，但派工送不到（session 不在角色表 → 換過人或死掉了）
        rows = self.conn.execute(
            "SELECT t.id AS task_id, t.work_id, t.issue_id, t.stage, t.assigned_session,"
            "       t.assigned_role, t.created_at"
            " FROM tasks t WHERE t.status = 'open'").fetchall()
        for row in rows:
            bound = self.conn.execute(
                "SELECT session_id FROM roles WHERE role = ? AND session_id IS NOT NULL"
                " AND session_id <> ''", (row["assigned_role"] or "",)).fetchall()
            handles = {r["session_id"] for r in bound}
            if row["assigned_session"] not in handles:
                target = row["work_id"] or row["issue_id"] or ""
                problems.append({
                    "code": "undeliverable", "kind": "work" if row["work_id"] else "issue",
                    "id": target, "task_id": row["task_id"],
                    "message": "%s 的 %s 棒派給了 %s，但這個 session 不在角色表上"
                               "（session 換掉或死掉）→ 派工送不到"
                               % (target, row["stage"], row["assigned_session"] or "（無）"),
                    "age_minutes": int(_age(row["created_at"]))})

        # 3) 停滯：很久沒有任何提交（也不在等人裁決、沒有在途提交）
        works = self.conn.execute(
            "SELECT w.id, w.state, w.awaiting_human, w.created_at, w.updated_at,"
            " (SELECT MAX(s.created_at) FROM submissions s WHERE s.work_id = w.id) AS last_sub,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.work_id = w.id"
            "  AND s.status IN (?,?)) AS inflight"
            " FROM works w WHERE w.state <> ? AND COALESCE(w.superseded_by, '') = ''",
            (SUB_PENDING, SUB_UNVERIFIED, workflow.CLOSED)).fetchall()
        for row in works:
            if int(row["awaiting_human"] or 0):
                age = _age(row["updated_at"])
                if age >= AWAITING_TOO_LONG_MINUTES:
                    problems.append({
                        "code": "awaiting_too_long", "kind": "work", "id": row["id"],
                        "message": "工單 %s 標了待人處理但已經 %.0f 分鐘沒人裁決"
                                   % (row["id"], age),
                        "age_minutes": int(age)})
                continue
            if int(row["inflight"] or 0):
                continue          # 引擎自己還在處理這張單的提交
            age = _age(row["last_sub"] or row["updated_at"] or row["created_at"])
            if age >= STUCK_MINUTES:
                problems.append({
                    "code": "stuck", "kind": "work", "id": row["id"],
                    "message": "工單 %s 停在 %s 已 %.0f 分鐘沒有任何提交"
                               "（這一棒輪到 %s；席位很久沒動靜的話可能是 session 關了）"
                               % (row["id"], row["state"], age,
                                  (workflow.WORK_STATES.get(row["state"]) or {}).get("role") or "?"),
                    "age_minutes": int(age)})

        issues = self.conn.execute(
            "SELECT i.id, i.state, i.awaiting_human, i.created_at, i.updated_at,"
            " (SELECT MAX(s.created_at) FROM submissions s WHERE s.issue_id = i.id) AS last_sub,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.issue_id = i.id"
            "  AND s.status IN (?,?)) AS inflight"
            " FROM issues i WHERE i.state <> ?",
            (SUB_PENDING, SUB_UNVERIFIED, workflow.ISSUE_CLOSED)).fetchall()
        for row in issues:
            if int(row["awaiting_human"] or 0):
                age = _age(row["updated_at"])
                if age >= AWAITING_TOO_LONG_MINUTES:
                    problems.append({
                        "code": "awaiting_too_long", "kind": "issue", "id": row["id"],
                        "message": "ISSUE %s 標了待人處理但已經 %.0f 分鐘沒人裁決"
                                   % (row["id"], age),
                        "age_minutes": int(age)})
                continue
            if int(row["inflight"] or 0):
                continue
            age = _age(row["last_sub"] or row["updated_at"] or row["created_at"])
            if age >= STUCK_MINUTES:
                problems.append({
                    "code": "stuck", "kind": "issue", "id": row["id"],
                    "message": "ISSUE %s 停在 %s 已 %.0f 分鐘沒有任何提交"
                               % (row["id"], row["state"], age),
                    "age_minutes": int(age)})
        return problems

    def _run_health_check(self, report: Dict[str, Any]) -> None:
        """每趟 tick 巡檢一次；**新問題寫進正本**（journal），其餘只更新狀態。

        寫 journal 是為了「事後查得到」；不重複寫同一個問題是為了不灌爆正本。
        """
        problems = self.health_check()
        report["health"] = problems
        for problem in problems:
            flag = "health:%s:%s:%s" % (problem["code"], problem["kind"], problem["id"])
            with store.tx(self.conn):
                seen = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM counters WHERE name = ?", (flag,)).fetchone()["n"]
                if seen:
                    continue
                self.conn.execute("INSERT INTO counters(name, value) VALUES (?, 1)"
                                  " ON CONFLICT(name) DO NOTHING", (flag,))
                store.journal(self.conn, "health.problem", problem["id"],
                              detail={"code": problem["code"], "kind": problem["kind"],
                                      "message": problem["message"],
                                      "age_minutes": problem.get("age_minutes", 0)})
            report["errors"].append(problem["message"])

    def _notify_unassigned(self, kind: str, target_id: str, state: str,
                           role: str, report: Dict[str, Any]) -> None:
        """把「沒有人接手」**寫進正本**（journal）——每個 (單, 狀態) 只記一次。

        引擎的聲音要留得下來：tick 報告會被下一次覆蓋，只有 journal 會留著，
        事後查得到「這張單在這個狀態上等過人、而且當時沒人接手」。
        """
        message = ("工單 %s 在 %s 沒有可用席位（%s 沒人認領），派不出去"
                   % (target_id, state, role))
        if kind == "issue":
            message = ("ISSUE %s 在 %s 沒有可用席位（%s 沒人認領），派不出去"
                       % (target_id, state, role))
        flag = "no_seat_notified:%s:%s:%s" % (kind, target_id, state)
        with store.tx(self.conn):
            seen = self.conn.execute(
                "SELECT COUNT(*) AS n FROM counters WHERE name = ?", (flag,)).fetchone()["n"]
            if seen:
                return
            self.conn.execute("INSERT INTO counters(name, value) VALUES (?, 1)"
                              " ON CONFLICT(name) DO NOTHING", (flag,))
            store.journal(self.conn, "dispatch.no_seat", target_id,
                          detail={"kind": kind, "state": state, "role": role,
                                  "message": message})
        report["errors"].append(message)

    def _dispatch_pending(self, report: Dict[str, Any]) -> None:
        """派發目前里程碑中尚無開啟任務的工單。"""
        rows = self.conn.execute(
            "SELECT * FROM works w WHERE state <> ? AND awaiting_human = 0"
            " AND COALESCE(w.superseded_by, '') = ''"
            " AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.work_id = w.id"
            " AND t.status = 'open') ORDER BY w.id",
            (workflow.CLOSED,)).fetchall()
        for row in rows:
            work = dict(row)
            with store.tx(self.conn):
                current = self._current_milestone()
                if current is not None:
                    mine = work.get("milestone_id")
                    if mine and mine != current["id"]:
                        continue  # 還沒輪到這一段
                backlog = work["state"] == workflow.BACKLOG
                stage = workflow.DESIGN if backlog else work["state"]
                task_id = self._create_task_for_state(work["id"], stage, self.conn, bump=backlog)
            if task_id:
                report["dispatched"].append({"work": work["id"], "task": task_id})
            else:
                # **沒有人接手要出聲**——不能靜靜卡住。
                # live 實測（2026-11-23 00:43~10:41）：角色沒認領席位 →
                # 派工送不出去、10 小時零提交，**引擎完全沒有留下任何訊息**
                # （journal 只有 dispatch.verify）。沒有訊息就沒有人知道要開 session。
                role = (workflow.WORK_STATES.get(stage) or {}).get("role") or ""
                self._notify_unassigned("work", work["id"], stage, role or "該角色", report)

    def _heal_rejected_tasks(self, report: Dict[str, Any]) -> None:
        """**退件卻沒有重新派工**的舊 task → 收掉，讓這張單重新被派。

        為什麼需要這條自癒：正常的退件路徑會「收掉舊 task ＋ 建新 task」，
        所以「task 還開著、而且它自己的提交被退件」= 異常
        （退件路徑漏了重新派工時就長這樣）。

        live 實測（2026-11-23 20:18）：W-002／W-003 的 rework_design 提交被
        `apply.rejected` 擋下，舊路徑沒有重新派工 → task 一直開著、
        角色沒人通知 → 兩小時零動作。有了這條，引擎自己就會收掉重派。

        **已標待人處理的不動**：那時單子是在等人裁決，不是「忘了重派」。
        """
        rows = self.conn.execute(
            "SELECT t.id AS task_id FROM tasks t"
            " LEFT JOIN works w ON w.id = t.work_id"
            " LEFT JOIN issues i ON i.id = t.issue_id"
            " WHERE t.status = 'open'"
            " AND COALESCE(w.awaiting_human, 0) = 0"
            " AND COALESCE(i.awaiting_human, 0) = 0"
            " AND EXISTS (SELECT 1 FROM submissions s WHERE s.task_id = t.id"
            "             AND s.status = 'rejected')").fetchall()
        for row in rows:
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (store.now(), row["task_id"]))
            report["dispatched"].append({"task": row["task_id"],
                                         "note": "healed-rejected"})

    def _counter_value(self, name: str) -> int:
        row = self.conn.execute("SELECT value FROM counters WHERE name = ?",
                                (name,)).fetchone()
        return int(row["value"]) if row else 0

    def _counter_set(self, name: str, value: int) -> None:
        self.conn.execute("INSERT INTO counters(name, value) VALUES (?, ?)"
                          " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                          (name, int(value)))

    # ------------------------------------------------------------ 宿主活動

    def _host_sessions(self) -> List[Dict[str, Any]]:
        """這一趟的宿主終端快照（**每趟只掃一次**）。"""
        cached = getattr(self, "_host_sessions_cache", None)
        if cached is None:
            try:
                cached = platform.scan_host_sessions()
            except Exception:
                cached = []
            self._host_sessions_cache = cached
        return cached

    def _host_activity(self, session_id: str) -> Dict[str, Any]:
        """**這個 session 最後在幹嘛**（在等模型？在跑工具？回合已結束？查不到？）

        查不到就回 `unknown`——**不要猜**。呼叫端自己決定怎麼處理
        （「查不到」不等於「沒在動」）。
        """
        if not session_id:
            return {"state": hostact.STATE_UNKNOWN, "at": None, "source": "none"}
        row = self.conn.execute(
            "SELECT agent FROM roles WHERE session_id = ?", (session_id,)).fetchone()
        agent = (row["agent"] if row else "") or ""
        return hostact.session_activity(
            session_id, agent, endpoint=self.endpoint, binary=self.binary,
            host_sessions=self._host_sessions())

    def _activity_baseline(self, baseline: str, activity: Dict[str, Any]) -> float:
        """計時基準：**正本的交件時間**與**宿主的最後活動時間**取較晚者。

        為什麼要取較晚者：agent 可能一直有動作（跑批次、跟模型往返）
        卻還沒交件——這時正本的交件時間很舊，但**它沒有卡住**。
        用較晚者當起點 → 不會把「正在做事」誤判成「失聯」。
        """
        epoch = 0.0
        if baseline:
            try:
                epoch = time.mktime(time.strptime(baseline[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                epoch = 0.0
        at = activity.get("at")
        if at:
            epoch = max(epoch, float(at))
        return epoch

    def _wake_up_idle_roles(self, report: Dict[str, Any]) -> None:
        """**依照單據狀態判斷現在是誰該做；沒有人在做就喚醒他。**

        角色收到派工就結束回合（規格：交件收到確認就收工）→ 引擎只能靠
        **再推一把**把它叫回來。這就是「喚醒」。

        live 實測（2026-11-23 一天）：三張單有很長一段時間是「派了、沒人回」，
        角色不再動作、單子就停在那裡（健康巡檢抓得到，但**光說不叫沒用**）。

        **喚醒提示詞裡不能出現 task_id**：回執核對（`hostterm.verify_host`）
        會把「user 項含 task_id、且之後有 assistant 回覆」當成這一棒已交件
        → 喚醒帶 task_id 會造成**假回執**，引擎會誤以為有人交了。
        所以喚醒用**獨立錨點**（outbox 的 task_id 欄放這個錨點），
        送達狀態靠它核對，不會碰到真的 task_id。
        """
        now = time.time()
        rows = self.conn.execute(
            "SELECT t.id AS task_id, t.work_id, t.issue_id, t.stage, t.assigned_session,"
            "       t.assigned_role, t.created_at, t.updated_at"
            " FROM tasks t WHERE t.status = 'open'").fetchall()
        for row in rows:
            task_id = row["task_id"]
            # 這一棒自己已經有提交在途 → 角色有在做事，不用叫
            inflight = self.conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE task_id = ?"
                " AND status IN (?,?)", (task_id, SUB_PENDING, SUB_UNVERIFIED)).fetchone()["n"]
            if inflight:
                continue
            count = self._counter_value("wakeups:%s" % task_id)
            if count >= MAX_WAKE_UPS:
                continue
            last = self._counter_value("wakeup_at:%s" % task_id)
            if last and now - float(last) < WAKE_UP_REPEAT_SECONDS:
                continue
            # 這個角色最近有沒有別的動作？（同一個 session 可能在忙別的單）
            role_row = self.conn.execute(
                "SELECT MAX(s.created_at) AS t FROM submissions s JOIN tasks t2"
                " ON t2.id = s.task_id WHERE t2.assigned_role = ?",
                (row["assigned_role"] or "",)).fetchone()
            last_activity = max([x for x in [
                (row["updated_at"] or ""), (row["created_at"] or ""),
                (role_row["t"] or "")] if x] or [""])
            # **宿主側判型**（使用者裁決 2026-11-25）：
            # 只有「完全沒動靜」才催——**在等模型、在跑工具、剛被壓縮**
            # 都不算沒動靜（實測：15 次誤判，agent 每一分鐘都還有紀錄，
            # 最後一筆全是模型請求，其中 4 次是模型端直接回錯）。
            activity = self._host_activity(row["assigned_session"] or "")
            state = str(activity.get("state") or hostact.STATE_UNKNOWN)
            if hostact.is_alive(state):
                # 還活著 → **不催**（插訊息會打斷它正在做的事）。
                # 但模型端卡太久 → 記一個健康事件（**不收掉 task**）。
                at = activity.get("at")
                if at and now - float(at) > MODEL_STALL_SECONDS:
                    with store.tx(self.conn):
                        store.journal(self.conn, "health.model_stalled", task_id,
                                      session_id=row["assigned_session"],
                                      detail={"role": row["assigned_role"], "state": state,
                                              "idle_minutes": int((now - float(at)) // 60)})
                continue
            since = self._activity_baseline(last_activity, activity)
            if not since:
                continue
            if now - since < WAKE_UP_SECONDS:
                continue
            # **查不到宿主紀錄** → 門檻加倍（保守：寧可晚點催，也不要誤殺）。
            if state == hostact.STATE_UNKNOWN:
                if now - since < WAKE_UP_SECONDS * UNKNOWN_ACTIVITY_MULTIPLIER:
                    continue

            # 目標單現在是什麼狀態、該誰做 → 喚醒訊息要講清楚
            target_id = row["work_id"] or row["issue_id"] or ""
            state = ""
            title = ""
            if row["work_id"]:
                work = workflow.load_work(self.conn, row["work_id"])
                if work:
                    state = work.get("state") or ""
                    title = work.get("title") or ""
            else:
                issue = workflow.load_issue(self.conn, row["issue_id"])
                if issue:
                    state = issue.get("state") or ""
            stage_title = (workflow.WORK_STATES.get(row["stage"]) or {}).get("title") or row["stage"]

            count += 1
            anchor = "WU%s" % hashlib.sha1(
                ("%s:%d" % (task_id, count)).encode("utf-8")).hexdigest()[:12]
            # **這一輪的 message_id 也由引擎發**（agent 照抄就不會撞車——
            # 實測 2026-11-25：agent 自己編的 id 重複 → 被 receipt.message_reused 退件）。
            # 它是雜湊，**不含真的 task_id** → 不會造成假回執。
            version = ""
            if row["work_id"]:
                wrow = self.conn.execute(
                    "SELECT version FROM works WHERE id = ?", (row["work_id"],)).fetchone()
                version = wrow["version"] if wrow else ""
            message_id = platform.dispatch_message_id(
                {"id": task_id, "created_at": row["created_at"]}, {"version": version})
            # **喚醒要帶三樣東西**：這一棒要交什麼（契約）、現況從哪裡查
            # （fbflow_read）、上一次被退的話為什麼被退、要怎麼改。
            # 舊版只叫角色「照工單檔案的『現在這一棒』交件」——檔案層取消後
            # 那句話指向一份**不再更新**的舊檔，角色會照著舊狀態做事
            # （live 實測 2026-11-24：PLANNER 收到喚醒後去讀舊工單檔，
            #  搞不清楚自己要交什麼，最後什麼都沒交）。
            if row["work_id"]:
                contract = workflow.stage_contract(row["stage"] or "")
            else:
                contract = workflow.issue_stage_contract(row["stage"] or "")
            text = ("【FBF ENGINE 喚醒】這一棒還沒交件，現在**輪到 %s** 做。\n"
                    "單據：%s（%s）\n"
                    "這一棒：%s\n"
                    "message_id：%s（交件時用這個）\n"
                    "錨點：%s（請在回覆裡帶上這段，引擎才知道有送達）\n"
                    % (row["assigned_role"] or "該角色", target_id,
                       title or state, stage_title, message_id, anchor))
            if contract:
                text += "\n## 現在這一棒：要交的東西\n\n"
                text += "\n".join(prompts.contract_lines(contract, brief=True)) + "\n"
            kind = "work" if row["work_id"] else "issue"
            target_kind = "work" if row["work_id"] else "issue"
            text += ("\n## 怎麼交\n"
                     "1. fbflow_status() 看你的待辦（裡面有這一棒的 task_id）。\n"
                     "2. fbflow_read(target_kind=%s, target_id=%s) 讀正本快照"
                     "（內容、驗收方式、既有解法）。\n"
                     "3. 用 fbflow_report_%s 回報（task_id 用步驟 1 的那個）。\n"
                     % (target_kind, target_id, "work" if row["work_id"] else "issue"))
            rejection = self._pending_rejection_note(
                self.conn, kind, target_id,
                task_hint="task_id 用 fbflow_status() 查你的待辦（喚醒訊息不帶 task_id）")
            if rejection:
                text += "\n" + rejection + "\n"
            with store.tx(self.conn):
                self._counter_set("wakeups:%s" % task_id, count)
                self._counter_set("wakeup_at:%s" % task_id, int(now))
                self._enqueue_outbox(self.conn, anchor, "wakeup",
                                     row["assigned_session"], row["assigned_role"] or "",
                                     target_id, text)
                store.journal(self.conn, "wakeup.send", target_id,
                              session_id=row["assigned_session"],
                              detail={"task_id": task_id, "stage": row["stage"],
                                      "role": row["assigned_role"], "anchor": anchor,
                                      "count": count})
            report["dispatched"].append({"task": task_id, "note": "wakeup",
                                         "anchor": anchor})

    # ------------------------------------------------------------ 收合上下文

    def _role_of_work(self, state: str) -> str:
        """這張單現在**等誰**做。

        `backlog` 的下一棒是「解法設計」→ 等 THINKER（派工時就是這樣算的）。
        """
        stage = workflow.DESIGN if state == workflow.BACKLOG else state
        return (workflow.WORK_STATES.get(stage) or {}).get("role") or ""

    def _seat_is_idle(self, role: str, session_id: str) -> bool:
        """**看單據狀態**判定這個 session 現在有沒有工作可以做。

        1. 這個 session 自己有開著的 task → 有工作（不算閒置）。
        2. 有單據正處於「等這個角色做」的狀態，而且**還沒有人做**
           → 引擎下一趟 tick 就會派給它（或同角色的別人）→ 不算閒置。
        3. 都沒有 → 它現在沒事做（單據都在別人手上、或都結束了）。
        """
        if not role or not session_id:
            return False
        inflight = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE assigned_session = ?"
            " AND status = 'open'", (session_id,)).fetchone()["n"]
        if inflight:
            return False
        for row in self.conn.execute(
                "SELECT id, state FROM works WHERE state <> ?"
                " AND COALESCE(superseded_by, '') = '' AND awaiting_human = 0",
                (workflow.CLOSED,)).fetchall():
            if self._role_of_work(row["state"]) != role:
                continue
            taken = self.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE work_id = ? AND status = 'open'",
                (row["id"],)).fetchone()["n"]
            if not taken:
                # 這張單等這個角色、而且還沒有人做 → 它等一下就有事做。
                return False
        for row in self.conn.execute(
                "SELECT id, state FROM issues WHERE state <> ? AND awaiting_human = 0",
                (workflow.ISSUE_CLOSED,)).fetchall():
            issue_role = (workflow.ISSUE_STATES.get(row["state"]) or {}).get("role") or ""
            if issue_role != role:
                continue
            taken = self.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE issue_id = ? AND status = 'open'",
                (row["id"],)).fetchone()["n"]
            if not taken:
                return False
        return True

    def _session_last_activity(self, session_id: str) -> str:
        """這個 session 最近一次有動作的時間（journal／派工／任務）。"""
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM journal WHERE session_id = ?",
            (session_id,)).fetchone()
        stamps = [row["ts"] or ""]
        for sql, params in (
                ("SELECT MAX(updated_at) AS ts FROM outbox WHERE recipient_session = ?",
                 (session_id,)),
                ("SELECT MAX(updated_at) AS ts FROM tasks WHERE assigned_session = ?",
                 (session_id,))):
            other = self.conn.execute(sql, params).fetchone()
            stamps.append(other["ts"] or "")
        return max(stamps)

    def _nudge_compact(self, report: Dict[str, Any]) -> None:
        """**session 沒有工作可以做 → 叫它收合上下文（/compact）。**

        使用者裁決（2026-11-25）：離上一次 compact 超過半小時就提醒，
        **不要改已發出的工單**那套之外的另一個「不要一直叫」的紅線：
        剛被派單、剛交完件的 session **不可以**被叫去 compact。

        四道條件都成立才提醒：

        1. **沒有工作可以做**（`_seat_is_idle`：看**單據狀態**，不是看 task 數）。
        2. **閒置落定期**（`COMPACT_IDLE_SECONDS`）：最後一次活動已經有一段時間
           ——剛交完件、剛被派過單的 session 一點都不閒。
        3. **距上一次 compact 超過半小時**（`compact.COMPACT_REPEAT_SECONDS`）：
           上一次 compact 以**宿主紀錄**為準（Claude Code 的
           `isCompactSummary`／OpenCode 的 `compaction` 訊息），
           偵測不到就用引擎自己記的「上次要求」時間。
        4. **它有對話紀錄**：剛開的空 session 沒有東西可收合。

        提醒走既有的 outbox 通道，**用獨立錨點**（與喚醒同一套做法）：
        帶真 task_id 會被回執核對當成「這一棒已交件」→ 假回執。
        """
        now = time.time()
        seats = self.conn.execute(
            "SELECT session_id, role, short_name, agent FROM roles"
            " WHERE status = 'active' AND session_id IS NOT NULL AND session_id <> ''"
        ).fetchall()
        if not seats:
            return
        host_sessions = platform.scan_host_sessions()
        for seat in seats:
            session_id = seat["session_id"] or ""
            role = seat["role"] or ""
            if not self._seat_is_idle(role, session_id):
                continue
            # 條件 2：閒置落定期（剛有動作的 session 不叫）
            last_activity = self._session_last_activity(session_id)
            epoch = compact.to_epoch(last_activity)
            if epoch is not None and now - epoch < COMPACT_IDLE_SECONDS:
                continue
            # 條件 3：距上一次 compact
            info = compact.inspect_session(
                session_id, seat["agent"] or "", host_sessions=host_sessions,
                endpoint=self.endpoint, binary=self.binary)
            recorded = compact.last_recorded(self.conn, session_id)
            last_compact = compact.newest(info.get("last_compact"), recorded)
            if last_compact is not None and now - last_compact < compact.COMPACT_REPEAT_SECONDS:
                continue
            # 條件 4：有對話紀錄才值得收合
            has_history = info.get("has_history")
            if has_history is None:
                # **查不到就不叫**——寧可少叫，也不要對空 session 亂叫。
                has_history = self._dispatched_before(session_id)
            if not has_history:
                continue

            anchor = "CP%s" % hashlib.sha1(
                ("%s:%d" % (session_id, int(now))).encode("utf-8")).hexdigest()[:12]
            mode = compact.mode_of(self.conn)
            command, note = compact.command_for(seat["agent"] or "", mode)
            text = compact.prompt_text(mode, seat["agent"] or "", role,
                                       minutes=compact.COMPACT_REPEAT_SECONDS // 60,
                                       anchor=anchor)
            with store.tx(self.conn):
                if command:
                    # 先排送指令，再釋放席位（釋放不會丟掉這筆 compact 派工）。
                    self._enqueue_outbox(self.conn, anchor, "compact", session_id, role,
                                         session_id, text)
                compact.record(self.conn, session_id, store.now(), "requested")
                store.journal(self.conn, "compact.request", session_id,
                              session_id=session_id,
                              detail={"role": role, "anchor": anchor, "mode": mode,
                                      "command": command, "note": note,
                                      "last_compact": info.get("last_compact") or "",
                                      "last_activity": last_activity})
            report["dispatched"].append({"note": "compact", "session": session_id,
                                         "role": role, "anchor": anchor,
                                         "mode": mode, "command": command})

    def _dispatched_before(self, session_id: str) -> bool:
        """引擎跟這個 session 講過話嗎（它的對話不是空的）。"""
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE recipient_session = ?",
            (session_id,)).fetchone()["n"]
        return int(count) > 0

    def _expire_stalled_tasks(self, report: Dict[str, Any]) -> None:
        """**角色不回應 → 引擎自己收掉那一棒並重派**（最後一個「靜靜卡住」的洞）。

        其他所有重派路徑都假設「角色會看到派工並重交」；但角色可能根本不理
        （人不在、session 卡住、指令壞掉、忘記了）→ task 一直開著、
        沒有人回報、單子原地不動、沒有錯誤訊息。

        **計時基準是「這個角色最近一次交件」，不是某一棒的建立時間**：
        同一個 session 排了三棒時，後面兩棒是「在排隊」不是「失聯」，
        用建立時間算會把它們誤判成逾時、連環收掉。
        而且一次只收**最早的那一棒** → 不會把整排一次砍光。

        已經有提交在途（pending／unverified）的那棒不算失聯——它有回應。
        """

        now = time.time()
        roles = self.conn.execute(
            "SELECT DISTINCT assigned_role FROM tasks WHERE status = 'open'"
            " AND assigned_role IS NOT NULL AND assigned_role <> ''").fetchall()
        for row in roles:
            role = row["assigned_role"]
            tasks = self.conn.execute(
                "SELECT * FROM tasks WHERE status = 'open' AND assigned_role = ?"
                " ORDER BY created_at, id", (role,)).fetchall()
            if not tasks:
                continue
            task = tasks[0]
            # 等人裁決的單子不動（那不是「忘了回應」）。
            if task["work_id"]:
                work = workflow.load_work(self.conn, task["work_id"])
                if work is None or int(work.get("awaiting_human") or 0):
                    continue
            if task["issue_id"]:
                issue = workflow.load_issue(self.conn, task["issue_id"])
                if issue is None or int(issue.get("awaiting_human") or 0):
                    continue
            # 這一棒自己已經有提交在途 → 它有回應，不算失聯。
            inflight = self.conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE task_id = ?"
                " AND status IN (?,?)", (task["id"], SUB_PENDING, SUB_UNVERIFIED)).fetchone()["n"]
            if inflight:
                continue
            # 這個角色最近一次交件；從沒交過就用最早那一棒的建立時間當起點。
            last = self.conn.execute(
                "SELECT MAX(s.created_at) AS t FROM submissions s"
                " JOIN tasks t2 ON t2.id = s.task_id WHERE t2.assigned_role = ?",
                (role,)).fetchone()["t"]
            # **起點取「角色最後一次交件」與「這一棒建立時間」的較晚者。**
            # 只看角色最後一次交件會誤殺**剛派出來**的一棒：
            # 角色上一棒交完後閒置很久 → 新派的那一棒派出 1 分鐘就被當「失聯」收掉
            # （實測 2026-11-24：PLANNER 閒置 162 分鐘 → 剛派出的專案結案檢視單
            #   1 分鐘後被收掉，結案判定卡死）。
            # 排隊中的後面幾棒也一樣：它們的建立時間比較晚，不該被連環收掉。
            baseline = last or task["created_at"]
            if (task["created_at"] or "") > baseline:
                baseline = task["created_at"]
            # **宿主側判型**（使用者裁決 2026-11-25）：
            # 只有「回合已結束、什麼都沒在動」才收掉重派。
            # 在等模型／在跑工具／剛被壓縮 → **還活著**，不收。
            activity = self._host_activity(task["assigned_session"] or "")
            state = str(activity.get("state") or hostact.STATE_UNKNOWN)
            if hostact.is_alive(state):
                at = activity.get("at")
                if at and now - float(at) > MODEL_STALL_SECONDS:
                    with store.tx(self.conn):
                        store.journal(self.conn, "health.model_stalled", task["id"],
                                      session_id=task["assigned_session"],
                                      detail={"role": role, "state": state,
                                              "idle_minutes": int((now - float(at)) // 60)})
                continue
            since = self._activity_baseline(baseline, activity)
            if not since:
                continue
            idle = now - since
            # **查不到宿主紀錄** → 門檻加倍（保守：寧可晚點收，也不要誤殺）。
            limit = ROLE_STALL_SECONDS
            if state == hostact.STATE_UNKNOWN:
                limit = ROLE_STALL_SECONDS * UNKNOWN_ACTIVITY_MULTIPLIER
            if idle < limit:
                continue
            minutes = int(idle // 60)
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (store.now(), task["id"]))
                store.journal(self.conn, "task.stalled", task["id"],
                              session_id=task["assigned_session"],
                              detail={"role": role, "idle_minutes": minutes})
            note = ("【逾時】這一棒派了 %d 分鐘都沒交件（角色 %s 沒有動靜），"
                    "引擎把它收掉、重新派一棒。請用上面的 task_id 重新交件，"
                    "先 fbflow_read 重讀工單再照「現在這一棒」的欄位交。"
                    % (minutes, role))
            new_task = ""
            if task["work_id"]:
                work = workflow.load_work(self.conn, task["work_id"])
                if work is not None:
                    new_task = self._create_task_for_state(
                        work["id"], work["state"], self.conn, bump=False, note=note) or ""
            elif task["issue_id"]:
                issue = workflow.load_issue(self.conn, task["issue_id"])
                if issue is not None:
                    new_task = self._create_task_for_issue_state(
                        issue["id"], issue["state"], note=note) or ""
            report["dispatched"].append({"task": new_task or task["id"],
                                         "note": "stalled-%dm" % minutes})

    def _enforce_source_prerequisites(self, report: Dict[str, Any]) -> None:
        """「該關該填的沒填，就不離開那關」的執行點。

        工單已經走到 verify／adjudicate／重工系列，卻發現方案來源
        （`solutions`）或答案卷（`checks`）從來沒建過——重工路
        `rework_build → verify` 會把 design 與 acceptance_design 整關跳過，
        到了驗收棒才發現題目不存在。這時**退回最早缺欄的那個關卡**、
        收掉該單已開的 task，下一棒自動派給該關角色補欄：

        - `solutions` 缺 → 退回 design（沒有案子，
          acceptance_design 的「選哪一案、為什麼」也填不出來）。
        - 只有 `checks` 缺 → 退回 acceptance_design。

        這是**補件不是重工**：`rework_count` 不動、不計入重工上限。
        退回時一併清空缺的那兩張來源表（正常情況它們本來就是空的，
        清空是防「半套資料」：有 solutions 沒 checks 之類的殘局）。
        """
        rows = self.conn.execute(
            "SELECT id, state FROM works WHERE state IN (?,?,?,?)"
            " AND awaiting_human = 0 AND COALESCE(superseded_by, '') = ''",
            (workflow.VERIFY, workflow.ADJUDICATE,
             workflow.REWORK_DESIGN, workflow.REWORK_BUILD)).fetchall()
        for row in rows:
            work_id, state = row["id"], row["state"]
            solutions = self.conn.execute(
                "SELECT COUNT(*) AS n FROM solutions WHERE work_id = ?",
                (work_id,)).fetchone()["n"]
            checks = self.conn.execute(
                "SELECT COUNT(*) AS n FROM checks WHERE work_id = ?",
                (work_id,)).fetchone()["n"]
            if solutions and checks:
                continue
            target = workflow.DESIGN if not solutions else workflow.ACCEPTANCE_DESIGN
            with store.tx(self.conn):
                cancelled = self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', updated_at = ?"
                    " WHERE work_id = ? AND status = 'open'",
                    (store.now(), work_id)).rowcount
                if not solutions:
                    self.conn.execute(
                        "DELETE FROM solutions WHERE work_id = ?", (work_id,))
                self.conn.execute("DELETE FROM checks WHERE work_id = ?", (work_id,))
                self.conn.execute(
                    "UPDATE works SET state = ?, version = version + 1, updated_at = ?"
                    " WHERE id = ?", (target, store.now(), work_id))
                store.journal(self.conn, "work.gate", work_id,
                              detail={"from": state, "to": target,
                                      "solutions": solutions, "checks": checks,
                                      "cancelled_tasks": cancelled,
                                      "reason": workflow.GAP_ACCEPTANCE_NOT_DESIGNED})
            report["errors"].append(
                "工單 %s 在 %s 關發現來源欄位沒填過（solutions=%d checks=%d）"
                "——退回 %s 補欄，收掉 %d 個已開 task"
                % (work_id, state, solutions, checks, target, cancelled))

    # ------------------------------------------------------------ 專案結案檢視

    def _maybe_review_project(self, report: Dict[str, Any]) -> None:
        """**所有單據都標示結案了 → 通知 PLANNER 做專案層級結案判定**。

        **與里程碑無關**（使用者澄清 2026-11-24）：
        - 里程碑＝防止長施工流程偏移方向的**分段**機制；
        - 專案結案＝**專案層級**「任務目標到底有沒有達成」的判定。

        所以這條路**不看 `milestones` 表**——專案可能一個里程碑都沒有
        （實測：活躍專案的 `milestones` 表是空的，四張單卻全部 closed）。
        舊路徑只認里程碑 → 結案永遠不觸發。

        觸發條件（全部成立才派）：
        - 至少有一張工單；
        - **所有工單都 `closed`**；
        - 沒有任何開著的 task（還有活在跑的棒 → 還沒到結案的時候）；
        - 沒有待人處理的單（等人裁決時不搶話）；
        - 專案本身還沒結案（`closed_at` 未設定）；
        - 這一棒已經有 review 在跑，或上一輪已經結案／停等人。

        **不重複派**：一輪檢視一個 task（`stage='project_review'`）。
        """
        row = self.conn.execute(
            "SELECT * FROM project_reviews WHERE id = ?", (PROJECT_REVIEW_ID,)).fetchone()
        if row is not None and row["state"] in (workflow.PR_CLOSED, workflow.PR_AWAITING):
            return                      # 已結案／停等人 → 不再派
        # **state=reviewing 但沒有開著的檢視單 → 重新派一輪**：
        # 那一棒可能被逾時收掉（`_expire_stalled_tasks`）——沒有重派，
        # 這一輪的結案判定就永遠卡住（實測 2026-11-24）。
        # 重複派用下面「有沒有開著的檢視單」擋。
        if store.get_setting(self.conn, "closed_at", ""):
            return                      # 專案已經結案（人確認或引擎標的）
        total = self.conn.execute("SELECT COUNT(*) AS n FROM works").fetchone()["n"]
        if not total:
            return
        pending = self.conn.execute(
            "SELECT COUNT(*) AS n FROM works WHERE state <> ?",
            (workflow.CLOSED,)).fetchone()["n"]
        if pending:
            return
        if self.conn.execute(
                "SELECT COUNT(*) AS n FROM works WHERE awaiting_human = 1").fetchone()["n"]:
            return
        if self.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status = 'open'").fetchone()["n"]:
            return
        # **段末對照還沒做完 → 不做專案層級判定**：
        # 里程碑是「防止長流程偏移方向」的分段機制，專案結案是「目標有沒有達成」。
        # 段都還沒對完就宣布專案達成，順序是反的。
        if self.conn.execute(
                "SELECT COUNT(*) AS n FROM milestones WHERE state = ?",
                (workflow.MS_REVIEWING,)).fetchone()["n"]:
            return
        if self.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE stage = ? AND status = 'open'",
                (workflow.STAGE_PROJECT_REVIEW,)).fetchone()["n"]:
            return
        assigned = self._pick_session("PLANNER")
        if assigned is None:
            return
        with store.tx(self.conn):
            if row is None:
                self.conn.execute(
                    "INSERT INTO project_reviews(id, state, version, review_count,"
                    " reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (PROJECT_REVIEW_ID, workflow.PR_REVIEWING, 1, 1, "",
                     store.now(), store.now()))
                version = 1
            else:
                version = int(row["version"]) + 1
                self.conn.execute(
                    "UPDATE project_reviews SET state = ?, version = ?,"
                    " review_count = review_count + 1, updated_at = ? WHERE id = ?",
                    (workflow.PR_REVIEWING, version, store.now(), PROJECT_REVIEW_ID))
            task_id = store.next_id(self.conn, "tasks", "T", 6)
            self.conn.execute(
                "INSERT INTO tasks(id, work_id, issue_id, milestone_id, project_review_id,"
                " stage, assigned_session, assigned_role, status, created_at, updated_at)"
                " VALUES (?,NULL,NULL,NULL,?,?,?,?, 'open', ?, ?)",
                (task_id, PROJECT_REVIEW_ID, workflow.STAGE_PROJECT_REVIEW,
                 assigned["session_id"], "PLANNER", store.now(), store.now()))
            text = self._project_review_prompt(version, task_id)
            self._enqueue_outbox(self.conn, task_id, "dispatch",
                                 assigned["session_id"], "PLANNER", PROJECT_REVIEW_ID, text)
            store.journal(self.conn, "project_review.dispatch", PROJECT_REVIEW_ID,
                          session_id=assigned["session_id"],
                          detail={"task_id": task_id, "version": version})
        report["project_reviews"].append({"review": PROJECT_REVIEW_ID, "task": task_id})

    def _project_review_summary(self) -> Dict[str, Any]:
        """派工提示詞要給 PLANNER 的三份東西：**任務目標、產出、退件紀錄**。

        結案判定＝「把產出跟任務目標比一比」；復盤＝「從退件紀錄看引擎哪裡該改」。
        沒有目標就無從比、沒有產出就無從對、沒有退件紀錄就無從復盤。
        """
        requirements = self.conn.execute(
            "SELECT id, text, success_criterion FROM requirements"
            " WHERE confirmed_at IS NOT NULL AND confirmed_at <> '' ORDER BY id").fetchall()
        works = self.conn.execute(
            "SELECT id, title, state, verdict, requirement_id FROM works ORDER BY id").fetchall()
        outputs = []
        for row in works:
            checks = self.conn.execute(
                "SELECT c.id AS check_id, c.required AS required, r.status AS status"
                " FROM checks c LEFT JOIN results r"
                "   ON r.work_id = c.work_id AND r.check_id = c.id"
                " WHERE c.work_id = ? ORDER BY c.id", (row["id"],)).fetchall()
            outputs.append({
                "id": row["id"], "title": row["title"] or "",
                "state": row["state"] or "", "verdict": row["verdict"] or "",
                "requirement_id": row["requirement_id"] or "",
                "checks": [{"id": c["check_id"], "required": int(c["required"] or 0),
                            "status": c["status"] or workflow.NOT_RUN}
                           for c in checks],
            })
        rejections = self.conn.execute(
            "SELECT target_kind, target_id, submission_id, stage, role, short_name,"
            " gaps, fields, reason, created_at FROM rejection_log"
            " ORDER BY id DESC LIMIT ?",
            (MAX_REJECTION_SUMMARY,)).fetchall()
        return {
            "requirements": [dict(r) for r in requirements],
            "outputs": outputs,
            "rejections": [dict(r) for r in rejections],
        }

    def _project_review_prompt(self, version: int, task_id: str) -> str:
        """組專案結案檢視的派工提示詞（**同一棒做兩件事**）。

        使用者裁決（2026-11-24）：結案判定與引擎復盤**同一棒做完**——
        一次派工、一個回報，順便把「引擎該怎麼改」提出來。

        **必須帶 `task_id`**——回執核對靠它對上這次任務（規格第 7 節）。
        """
        summary = self._project_review_summary()
        lines = [
            "【FBF ENGINE 專案結案檢視派工】",
            "task_id: %s" % task_id,
            "review_id: %s" % PROJECT_REVIEW_ID,
            "expected_version: %d" % int(version),
            "",
            "**所有單據都已標示結案。**這一棒做兩件事：",
            "1. **結案判定**：把「產出」跟「任務目標」比一比——目標有沒有達成。",
            "2. **引擎復盤**：從下面的退件紀錄看，引擎哪裡該改（提出建議）。",
            "",
            "## 任務目標（已確認需求）",
        ]
        for item in summary["requirements"]:
            lines.append("- %s：%s" % (item["id"], item["text"]))
            lines.append("  成功判準：%s" % item["success_criterion"])
        if not summary["requirements"]:
            lines.append("（沒有已確認需求）")
        lines += ["", "## 產出（各單的驗收結果）"]
        for item in summary["outputs"]:
            passed = sum(1 for c in item["checks"]
                         if c["required"] and c["status"] == workflow.PASS)
            required = sum(1 for c in item["checks"] if c["required"])
            lines.append("- %s：%s（總判 %s；required check %d/%d PASS）"
                         % (item["id"], item["title"] or "（無標題）",
                            item["verdict"] or "-", passed, required))
        if not summary["outputs"]:
            lines.append("（沒有產出）")
        lines += ["", "## 退件紀錄（復盤依據：引擎哪裡該改）"]
        # **逐欄結構化 + 編號**：實測 2026-11-25，PLANNER 寫改善建議時
        # 把 `design` 階段的退件記成 `acceptance_design`（階段記錯，
        # 建議的方向就整個偏掉）。所以：
        # 1) 階段名寫全（英文代碼 ＋ 中文標題），一眼對得上；
        # 2) 給編號，請它在 reason 裡引用「退件 #N」；
        # 3) 明講「**照抄階段，不要憑印象改寫**」。
        for index, item in enumerate(summary["rejections"], start=1):
            stage_code = str(item.get("stage") or "")
            stage_title = ""
            if stage_code:
                stage_title = ((workflow.WORK_STATES.get(stage_code) or {}).get("title")
                               or (workflow.ISSUE_STATES.get(stage_code) or {}).get("title")
                               or "")
            stage_text = stage_code
            if stage_title:
                stage_text = "%s（%s）" % (stage_code, stage_title)
            lines.append(
                "- 退件 #%d｜階段：**%s**｜工單：%s｜缺口代碼：%s｜缺哪個欄位：%s"
                "｜交件：%s｜退件者：%s"
                % (index, stage_text or "（未標）",
                   item.get("target_id") or "-",
                   item.get("gaps") or "-",
                   item.get("fields") or "-",
                   item.get("submission_id") or "-",
                   item.get("short_name") or item.get("role") or "-"))
            lines.append("  原因：%s" % (item.get("reason") or "-"))
        if not summary["rejections"]:
            lines.append("（沒有退件紀錄——沒有可復盤的東西）")
        lines += [
            "",
            "**引用退件時請寫「退件 #N」，並照抄它的階段**——"
            "不要憑印象改寫階段名（實測：把 design 階段記成 acceptance_design，"
            "整條建議就偏了）。",
            "",
            "## 你要回的東西",
            "",
            "**動作只可選一個**：",
            "  complete     —— 目標達成 → 標專案結案",
            "  add_work     —— 未達成 → 針對未完成的部分開新工單（附 new_works）",
            "  await_human  —— 停下來等人裁決",
            "",
            "**同一個回報也要交引擎改善建議**（improvements）：",
            "  每筆 = {\"target\": 針對哪裡（欄位／單據／引擎哪一段）, "
            "\"suggestion\": 引擎該怎麼改, \"reason\": 依據哪幾筆退件紀錄}",
            "  沒有要改的就交空陣列 []。",
            "",
            "交件方式：用 MCP 工具回報（不是 CLI）。",
            "  fbflow_report_project_review(task_id, expected_version, action, reason,"
            " new_works, improvements)",
            "  task_id 用上面那個；expected_version 也用上面那個。",
            "",
            "讀取：",
            "  fbflow_read(target_kind=work, target_id=<單號>)",
            "  fbflow_status()",
        ]
        return "\n".join(lines)

    def apply_project_review(self, task_id: str, expected_version: int,
                             session_id: str, action: str, reason: str = "",
                             new_works: Optional[List[Dict[str, Any]]] = None,
                             improvements: Optional[List[Dict[str, Any]]] = None
                             ) -> Dict[str, Any]:
        """套用專案結案檢視結果（**PLANNER 判定 + 引擎復盤同一棒**）。

        **四道核對**（跟里程碑檢視同一套）：
        1. 這張單的 `stage` 必須是 `project_review`；
        2. 這張單必須還開著（不得重送已用掉的檢視）；
        3. 必須是這張單的受派者；
        4. `expected_version` 必須等於正本版本。

        **動作**：
        - `complete`：引擎**自動跑雙向追溯**（第 8 節）→ 通過才標專案結案；
          沒通過就不結案（回錯並附缺什麼，讓 PLANNER 改用 `add_work` 補）。
        - `add_work`：針對未完成的部分開新工單 → 這一輪結束，
          新工單完成後**會再觸發一次檢視**（下一輪的結案判定）。
        - `await_human`：停等人裁決。

        **改善建議**（復盤產出）→ 存進 `engine_improvements` 表。
        """
        if action not in workflow.PR_REVIEW_ACTIONS:
            return {"ok": False, "code": "action.invalid",
                    "message": "專案結案動作只可選：%s" % ", ".join(workflow.PR_REVIEW_ACTIONS)}
        with store.tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM project_reviews WHERE id = ?",
                (PROJECT_REVIEW_ID,)).fetchone()
            if row is None:
                return {"ok": False, "code": "project_review.missing",
                        "message": "找不到專案結案檢視"}
            task = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return {"ok": False, "code": "task.missing", "message": "找不到這張檢視單"}
            if task["stage"] != workflow.STAGE_PROJECT_REVIEW:
                return {"ok": False, "code": "task.stage_mismatch",
                        "message": "這張單不是專案結案檢視單（stage=%s）" % task["stage"]}
            if task["status"] != "open":
                return {"ok": False, "code": "TASK-STALE",
                        "message": "這張檢視單已經用掉了（status=%s），不得重送" % task["status"]}
            if task["assigned_session"] != session_id:
                return {"ok": False, "code": "ROLE-DENIED", "message": "不是這張檢視單的受派者"}
            if (task["assigned_role"] or "") != "PLANNER":
                return {"ok": False, "code": "ROLE-DENIED",
                        "message": "專案結案檢視只由 PLANNER 做"}
            if row["state"] != workflow.PR_REVIEWING:
                return {"ok": False, "code": "project_review.not_reviewing",
                        "message": "專案結案檢視現在是 %s，不在檢視中" % row["state"]}
            if int(row["version"]) != int(expected_version):
                return {"ok": False, "code": "TASK-STALE",
                        "message": "版本不符（正本是 %d，你送的是 %d）"
                                   % (int(row["version"]), int(expected_version)),
                        "current_version": int(row["version"])}

            # **復盤產出**：引擎改善建議（存新表，網頁顯示、CLI 可讀）。
            saved_improvements = []
            for item in improvements or []:
                if not isinstance(item, dict):
                    continue
                suggestion = str(item.get("suggestion") or "").strip()
                if not suggestion:
                    continue
                cur = self.conn.execute(
                    "INSERT INTO engine_improvements(review_id, target, suggestion, reason,"
                    " status, created_at) VALUES (?,?,?,?,?,?)",
                    (PROJECT_REVIEW_ID, str(item.get("target") or ""), suggestion,
                     str(item.get("reason") or ""),
                     str(item.get("status") or "open"), store.now()))
                saved_improvements.append(int(cur.lastrowid))

            created_works: List[str] = []
            new_state = workflow.PR_REVIEWING
            if action == workflow.PR_COMPLETE:
                trace = self._traceability_check()
                if not trace["ok"]:
                    # **追溯不通過就不許標結案**——標了就是假的完成。
                    # 回錯誤並附缺什麼，PLANNER 應改用 `add_work` 補工單。
                    return {"ok": False, "code": "traceability.incomplete",
                            "message": "雙向追溯未通過，不能標專案結案；"
                                       "請針對缺的部分用 add_work 補工單。",
                            "details": trace["details"]}
                store.set_setting(self.conn, "closed_at", store.now())
                store.set_setting(self.conn, "closed_by", "PLANNER:%s" % session_id)
                new_state = workflow.PR_CLOSED
            elif action == workflow.PR_ADD_WORK:
                for item in new_works or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        work_id = self.create_work(
                            str(item.get("title") or ""),
                            str(item.get("requirement_id") or ""),
                            str(item.get("content") or ""),
                            list(item.get("expected_outputs") or []),
                            str(item.get("milestone_id") or ""))
                    except ValueError as exc:
                        return {"ok": False, "code": "work.create_failed",
                                "message": str(exc)}
                    created_works.append(work_id)
                # 補了工單 → 這一輪結束；新工單完成後會**再觸發一次**檢視。
                new_state = workflow.PR_REOPENED
            else:
                new_state = workflow.PR_AWAITING

            # **version 不在這裡跳**——version 是「輪次」，由下一次派工時 +1。
            # 同一輪的重送防護靠 task status（用掉就不再 open）。
            self.conn.execute(
                "UPDATE project_reviews SET state = ?,"
                " reason = ?, updated_at = ? WHERE id = ? AND version = ?",
                (new_state, reason, store.now(), PROJECT_REVIEW_ID, int(row["version"])))
            self.conn.execute("UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                              (store.now(), task_id))
            store.record_decision(self.conn, uuid.uuid4().hex[:12],
                                  "project_review.%s" % action,
                                  target_id=PROJECT_REVIEW_ID,
                                  detail={"reason": reason,
                                          "improvements": saved_improvements,
                                          "new_works": created_works})
            store.journal(self.conn, "project_review.%s" % action, PROJECT_REVIEW_ID,
                          session_id=session_id,
                          detail={"task_id": task_id, "new_works": created_works,
                                  "improvements": saved_improvements})
            return {"ok": True, "data": {"review_id": PROJECT_REVIEW_ID,
                                         "state": new_state,
                                         "new_works": created_works,
                                         "improvements": saved_improvements}}

    # ------------------------------------------------------------ 里程碑

    def _maybe_review_milestone(self, milestone_id: Optional[str]) -> None:
        if not milestone_id:
            return
        row = self.conn.execute("SELECT * FROM milestones WHERE id = ?",
                                (milestone_id,)).fetchone()
        if row is None or row["state"] != workflow.MS_OPEN:
            return
        pending = self.conn.execute(
            "SELECT COUNT(*) AS n FROM works WHERE milestone_id = ? AND state <> ?",
            (milestone_id, workflow.CLOSED)).fetchone()["n"]
        if pending:
            return
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE milestones SET state = ?, updated_at = ? WHERE id = ?",
                (workflow.MS_REVIEWING, store.now(), milestone_id))
            store.journal(self.conn, "milestone.review", milestone_id)

    def _review_milestones(self, report: Dict[str, Any]) -> None:
        """把 `reviewing` 的里程碑派給 PLANNER 做段末對照。"""
        rows = self.conn.execute(
            "SELECT * FROM milestones WHERE state = ? ORDER BY seq",
            (workflow.MS_REVIEWING,)).fetchall()
        for row in rows:
            milestone = dict(row)
            if self.conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks WHERE stage = 'milestone_review'"
                    " AND status = 'open'").fetchone()["n"]:
                return
            assigned = self._pick_session("PLANNER")
            if assigned is None:
                return
            summary = self._milestone_summary(milestone["id"])
            with store.tx(self.conn):
                task_id = store.next_id(self.conn, "tasks", "T", 6)
                self.conn.execute(
                    "INSERT INTO tasks(id, work_id, issue_id, milestone_id, stage,"
                    " assigned_session, assigned_role, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?, 'open', ?, ?)",
                    (task_id, None, None, milestone["id"], "milestone_review",
                     assigned["session_id"], "PLANNER", store.now(), store.now()))
                text = self._milestone_prompt(milestone, summary, task_id)
                self._enqueue_outbox(self.conn, task_id, "dispatch",
                                     assigned["session_id"], "PLANNER", milestone["id"], text)
            report["milestones"].append({"milestone": milestone["id"], "task": task_id})

    def _milestone_summary(self, milestone_id: str) -> List[Dict[str, Any]]:
        """「工單 × check × 結果」清單——機器先算好的部分。"""
        rows = self.conn.execute(
            "SELECT w.id AS work_id, w.title AS title, w.state AS state,"
            "       c.id AS check_id, c.required AS required,"
            "       r.status AS status, r.reason AS reason"
            " FROM works w LEFT JOIN checks c ON c.work_id = w.id"
            " LEFT JOIN results r ON r.work_id = w.id AND r.check_id = c.id"
            " WHERE w.milestone_id = ? ORDER BY w.id, c.id", (milestone_id,)).fetchall()
        return [dict(r) for r in rows]

    def _milestone_prompt(self, milestone: Dict[str, Any],
                          summary: List[Dict[str, Any]],
                          task_id: str = "") -> str:
        """組里程碑檢視的派工提示詞。

        **必須帶 `task_id`**——回執核對靠它把平台訊息與這次任務對上
        （規格第 7 節）。少了它，里程碑的提交**永遠核不過回執** ✓。
        """
        lines = [
            "【FBF ENGINE 里程碑檢視派工】",
            "task_id: %s" % task_id,
            "milestone_id: %s" % milestone["id"],
            "name: %s" % milestone["name"],
            "outcome: %s" % milestone["outcome"],
            "expected_version: %d" % int(milestone.get("version") or 1),
            "",
            "本段工單的驗收結果（機器算好的）：",
        ]
        for row in summary:
            lines.append("  - %s / %s / required=%s / %s"
                         % (row["work_id"], row["check_id"] or "-",
                            row["required"], row["status"] or "NOT_RUN"))
        lines += [
            "",
            "請對照 outcome 回報。你**只可**選一個動作：",
            "  continue     —— 達成，進下一段",
            "  add_work     —— 未達成，補工單（留在本段）",
            "  await_human  —— 停止派工，等人裁決",
            "",
            "交件方式：用 MCP 工具回報（不是 CLI）。",
            "  fbflow_report_milestone(task_id, milestone_id, expected_version, action, reason)",
            "  task_id 用上面那個。",
            "",
            "讀取：",
            "  fbflow_read(target_kind=milestone, target_id=%s)" % milestone["id"],
            "  fbflow_status()",
        ]
        return "\n".join(lines)

    def apply_milestone_review(self, task_id: str, milestone_id: str,
                               expected_version: int, session_id: str,
                               action: str, reason: str = "",
                               new_works: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        """套用里程碑檢視結果。

        **四道核對，一道都不能少**（審查發現：原本只核對受派者，別張單就能結案）：

        1. **任務目標**：這張檢視單的 `milestone_id` 必須就是這個里程碑。
        2. **任務狀態**：還沒用掉的單（`status='open'`）才算數——**不得重送已完成的單**。
        3. **里程碑狀態**：必須真的在 `reviewing`（不是別人已經收掉的段）。
        4. **版本**：`expected_version` 必須等於里程碑正本版本。

        套用時用樂觀鎖（`WHERE id=? AND version=?`）；更新 0 筆代表別人先改了，
        這一筆不套用、不重試。
        """
        if action not in workflow.MS_REVIEW_ACTIONS:
            return {"ok": False, "code": "action.invalid",
                    "message": "里程碑動作只可選：%s" % ", ".join(workflow.MS_REVIEW_ACTIONS)}
        with store.tx(self.conn):
            row = self.conn.execute("SELECT * FROM milestones WHERE id = ?",
                                    (milestone_id,)).fetchone()
            if row is None:
                return {"ok": False, "code": "milestone.missing", "message": "找不到里程碑"}
            task = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return {"ok": False, "code": "task.missing", "message": "找不到這張檢視單"}
            if task["stage"] != "milestone_review":
                return {"ok": False, "code": "task.stage_mismatch",
                        "message": "這張單不是里程碑檢視單（stage=%s）" % task["stage"]}
            if (task["milestone_id"] or "") != milestone_id:
                return {"ok": False, "code": "task.target_mismatch",
                        "message": "這張檢視單是給 %s 的，不是 %s"
                                   % (task["milestone_id"] or "?", milestone_id)}
            if task["status"] != "open":
                return {"ok": False, "code": "TASK-STALE",
                        "message": "這張檢視單已經用掉了（status=%s），不得重送" % task["status"]}
            if task["assigned_session"] != session_id:
                return {"ok": False, "code": "ROLE-DENIED", "message": "不是這張檢視單的受派者"}
            if (task["assigned_role"] or "") != "PLANNER":
                return {"ok": False, "code": "ROLE-DENIED",
                        "message": "里程碑檢視只由 PLANNER 做"}
            if row["state"] != workflow.MS_REVIEWING:
                return {"ok": False, "code": "milestone.not_reviewing",
                        "message": "里程碑現在是 %s，不在檢視中" % row["state"]}
            if int(row["version"]) != int(expected_version):
                return {"ok": False, "code": "TASK-STALE",
                        "message": "版本不符（正本是 %d，你送的是 %d）"
                                   % (int(row["version"]), int(expected_version)),
                        "current_version": int(row["version"])}
            max_review = store.limit_of(self.conn, "max_review", 2)
            count = int(row["review_count"] or 0)

            if action == workflow.MS_AWAIT_HUMAN:
                new_state, bump_review = workflow.MS_REVIEWING, False
            elif action == workflow.MS_CONTINUE:
                new_state, bump_review = workflow.MS_DONE, True
            else:
                # add_work：最多兩次補工檢視，之後只能 await_human
                if count >= max_review:
                    return {"ok": False, "code": "review.limit_reached",
                            "message": "補工檢視已達上限，只能 await_human"}
                new_state, bump_review = workflow.MS_OPEN, True

            cursor = self.conn.execute(
                "UPDATE milestones SET state = ?, review_count = review_count + ?,"
                " version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (new_state, 1 if bump_review else 0, store.now(), milestone_id,
                 int(row["version"])))
            if cursor.rowcount != 1:
                return {"ok": False, "code": "TASK-STALE",
                        "message": "里程碑版本已被別人改過，這筆回報不套用"}

            if action == workflow.MS_ADD_WORK:
                for item in new_works or []:
                    self.create_work(item.get("title", ""), item.get("requirement_id", ""),
                                     item.get("content", ""), item.get("expected_outputs") or [],
                                     milestone_id)
            self.conn.execute("UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                              (store.now(), task_id))
            store.record_decision(self.conn, uuid.uuid4().hex[:12], "milestone.%s" % action,
                                  target_id=milestone_id, detail={"reason": reason})
            return {"ok": True, "data": {"milestone_id": milestone_id,
                                         "state": new_state}}

    # ------------------------------------------------------------ outbox

    def _flush_outbox(self, report: Dict[str, Any]) -> None:
        """送出 outbox。**平台呼叫在交易外**。

        待撿的狀態：
        - `queued`：待送
        - `unknown`：送達狀態不明 → **先查收件方，不得盲目重送**
        - `sending` 且超過 120 秒：送途中斷（進程被殺）→ **同樣要先查**，不得直接重送
        - `submitted` 且超過 180 秒：CLI 已啟動但沒確認送達 → **也要查**
          （派工靜默失敗時不會有任何錯誤；不查就永遠停在 submitted，
          工單沒有人接手、也沒有錯誤訊息）
        - `failed`：確定失敗 → 指數退避後重試（上限 `max_dispatch` 次）

        **「送達狀態不明」的路徑有三條**（`unknown`、逾時的 `sending`、
        逾時的 `submitted`），三條都必須先查收件方——送出被中斷不代表沒送達。
        """
        stale_sending = store.now(time.time() - 120)
        stale_submitted = store.now(time.time() - 180)
        # **presence 一次 tick 只查一次**：所有 outbox 指的是同一個 server，
        # 每一列都去敲 CLI 太慢（而且那條路不通時會拖垮整趟 tick）。
        active, presence_error = self._active_sessions()
        if presence_error:
            # 查不到**不影響派工**（照送），但要讓人知道為什麼查不到。
            report["errors"].append("presence 查不到（不影響派送）：%s" % presence_error)
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE (status IN (?,?,?)"
            "   OR (status = ? AND updated_at < ?)"
            "   OR (status = ? AND updated_at < ?))"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            " ORDER BY id LIMIT 50",
            (OUT_QUEUED, OUT_UNKNOWN, OUT_FAILED, OUT_SENDING, stale_sending,
             OUT_SUBMITTED, stale_submitted, store.now())).fetchall()
        # **這一趟已送過的 session**（同一個 session 只送一則，見下面）。
        sent_sessions: set = set()
        for row in rows:
            item = dict(row)
            previous_status = item["status"]
            # **喚醒派工的收件人已經不是任何席位的 session** → 送不出去。
            #
            # 為什麼要這條：喚醒的 `task_id` 是**喚醒錨點 `WU…`**（不是 task 編號）
            # → 下面那條「task 已關閉」的孤兒檢查**撿不到它** → 收件人死後
            # **每 30 秒重查一次、永遠不停**（實測：一個死 handle 累積 27 筆
            # `dispatch.verify unknown`，outbox 也永遠佔著）。
            #
            # **只限喚醒類**：派工類的 `task_id` 是真的 task 編號 → 那條孤兒
            # 檢查會處理（釋放時 task 被作廢 → 派工跟著作廢）。
            # 而 ISSUE 裁決是派給「**發起者 session**」（可能已不在席位上，
            # 那是人的事，不該由這裡判死）→ 不能一併套用。
            #
            # 判準：**roles 表裡沒有任何席位認領這個 session** → 它已經
            # 不是「現在該做這一棒」的人 → 這筆喚醒沒有人會接。
            # **`failed` 也要算進來**：上一輪標 failed 後，下一轮這筆會因為
            # `failed` 可重試而再走一次送出路徑 → 又變回 `unknown` → 每 30 秒
            # 敲一次宿主 CLI（實測：outbox 3 累積 40+ 次 dispatch.verify）。
            # 把 failed 也納入 → 這筆就地停住，不再打擾平台。
            if item["kind"] in ("wakeup", "compact") and item["recipient_session"] \
                    and previous_status in (OUT_QUEUED, OUT_UNKNOWN, OUT_SENDING,
                                            OUT_FAILED):
                holder = self.conn.execute(
                    "SELECT 1 FROM roles WHERE session_id = ? LIMIT 1",
                    (item["recipient_session"],)).fetchone()
                if holder is None:
                    with store.tx(self.conn):
                        self.conn.execute(
                            "UPDATE outbox SET status = ?, last_error = ?,"
                            " next_attempt_at = NULL, updated_at = ? WHERE id = ?",
                            (OUT_FAILED, "收件人已不是任何席位的 session，遺留喚醒",
                             store.now(), item["id"]))
                    continue
            # **孤兒 outbox**：task 已被取消／完成（例如 rebind 時作廢舊派工），
            # 收件人多半是死 handle——撿起來只會累積重試、誤標工單。
            # 查無 task 的（gate／reminder 類）不受影響。
            if item["task_id"]:
                task_row = self.conn.execute(
                    "SELECT status FROM tasks WHERE id = ?",
                    (item["task_id"],)).fetchone()
                if task_row is not None and task_row["status"] != "open":
                    with store.tx(self.conn):
                        self.conn.execute(
                            "UPDATE outbox SET status = ?, last_error = ?,"
                            " next_attempt_at = NULL, updated_at = ? WHERE id = ?",
                            (OUT_FAILED, "task 已關閉，遺留派工",
                             store.now(), item["id"]))
                    continue
            # 送達狀態不明的三條路徑：`unknown`、逾時的 `sending`、逾時的 `submitted`
            needs_delivery_check = previous_status in (OUT_UNKNOWN, OUT_SENDING,
                                                      OUT_SUBMITTED)
            # 先標 sending（避免重複送）
            with store.tx(self.conn):
                self.conn.execute(
                    "UPDATE outbox SET status = ?, updated_at = ? WHERE id = ?"
                    " AND status IN (?,?,?,?,?)",
                    (OUT_SENDING, store.now(), item["id"], OUT_QUEUED, OUT_UNKNOWN,
                     OUT_FAILED, OUT_SENDING, OUT_SUBMITTED))
            try:
                if needs_delivery_check:
                    # **狀態不明 → 先查收件方到底收沒收到，不得盲目重送**
                    # （規格第 6 節）
                    state = receipts.delivery_state(
                        self.endpoint, item["recipient_session"] or "",
                        item["task_id"] or "",
                        receipts.to_epoch(item["created_at"]), self.binary)
                    with store.tx(self.conn):
                        store.journal(self.conn, "dispatch.verify", item.get("target_id") or "",
                                      session_id=item["recipient_session"] or "",
                                      detail={"outbox_id": item["id"], "task_id": item["task_id"],
                                              "previous_status": previous_status, "result": state})
                    if state == receipts.DELIVERED:
                        with store.tx(self.conn):
                            self.conn.execute(
                                "UPDATE outbox SET status = ?, last_error = ?,"
                                " next_attempt_at = NULL, updated_at = ? WHERE id = ?",
                                (OUT_DELIVERED, "先前已送達，不重送",
                                 store.now(), item["id"]))
                        report["dispatched"].append({"outbox": item["id"],
                                                     "state": OUT_DELIVERED,
                                                     "note": "先前已送達"})
                        continue
                    if state == receipts.DELIVERY_UNKNOWN:
                        # 證明不了沒送達 → 不重送，等下一輪再查
                        with store.tx(self.conn):
                            self.conn.execute(
                                "UPDATE outbox SET status = ?, deferrals = deferrals + 1,"
                                " next_attempt_at = ?, last_error = ?, updated_at = ?"
                                " WHERE id = ?",
                                (OUT_UNKNOWN, store.now(time.time() + 30),
                                 "delivery:unknown", store.now(), item["id"]))
                        report["deferred"].append({"outbox": item["id"],
                                                   "presence": "delivery-unknown"})
                        continue
                    # not-delivered → 已證明沒送達，可以重送，往下走

                presence = platform.presence_in(active, item["recipient_session"] or "")
                if presence == platform.BUSY:
                    # **忙碌才暫緩**（規格第 6 節：presence 只影響送出時機）。
                    # 查不到（unknown）**照送**——理由見下面。
                    deferrals = int(item["deferrals"] or 0) + 1
                    max_deferrals = store.limit_of(self.conn, "max_deferrals", MAX_DEFERRALS)
                    if deferrals > max_deferrals:
                        # 暫緩太久 → 不再等，標待人處理（**不允許無限等待**）
                        with store.tx(self.conn):
                            self.conn.execute(
                                "UPDATE outbox SET status = ?, deferrals = ?,"
                                " last_error = ?, next_attempt_at = NULL, updated_at = ?"
                                " WHERE id = ?",
                                (OUT_FAILED, deferrals,
                                 "presence:%s 暫緩超過上限" % presence, store.now(), item["id"]))
                            if item["kind"] == "dispatch" and item.get("target_id"):
                                self.conn.execute(
                                    "UPDATE works SET awaiting_human = 1,"
                                    " awaiting_reason = ?, updated_at = ? WHERE id = ?",
                                    ("dispatch_deferred", store.now(), item["target_id"]))
                        report["errors"].append(
                            "outbox %s: presence 暫緩超過上限，已標待人處理" % item["id"])
                        continue
                    # busy → 暫緩，不算失敗、不重複喚醒
                    with store.tx(self.conn):
                        self.conn.execute(
                            "UPDATE outbox SET status = ?, deferrals = ?,"
                            " next_attempt_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                            (OUT_QUEUED, deferrals,
                             store.now(time.time() + 30),
                             self._dispatch_deferral_reason(presence),
                             store.now(), item["id"]))
                    report["deferred"].append({"outbox": item["id"], "presence": presence})
                    if item["kind"] == "dispatch":
                        self._bump_dispatch_attempt(item["target_id"])
                    continue

                # **查不到 presence 也要送。**
                # 派工的依據是**工單狀態**（哪一棒該誰做），不是平台的 presence——
                # 規格第 10 節說的就是「用 OpenCode 指定 Session 的通知」發提示詞，
                # 讓那個 session 去讀工單。
                # 查不到 presence 只是「我們問不到」，不代表那個 session 不該收到
                # （規格第 6 節：不當成離線）；而且那條路不通時，卡住不送等於
                # 整條流程癱瘓。送不出去會走下面的失敗路徑（有上限的退避 → 標待人處理）✓

                # 確定失敗的重試有上限（指數退避，最多 max_dispatch 次）
                if previous_status == OUT_FAILED:
                    attempts = int(item["attempts"] or 0)
                    max_dispatch = store.limit_of(self.conn, "max_dispatch", 5)
                    if attempts >= max_dispatch:
                        with store.tx(self.conn):
                            self.conn.execute(
                                "UPDATE outbox SET status = ?, next_attempt_at = NULL,"
                                " last_error = ?, updated_at = ? WHERE id = ?",
                                (OUT_FAILED, "重試已達上限，不再重送",
                                 store.now(), item["id"]))
                            if item["kind"] == "dispatch" and item.get("target_id"):
                                self.conn.execute(
                                    "UPDATE works SET awaiting_human = 1,"
                                    " awaiting_reason = ?, updated_at = ? WHERE id = ?",
                                    ("dispatch_failed", store.now(), item["target_id"]))
                        report["errors"].append(
                            "outbox %s: 派送重試已達上限，已標待人處理" % item["id"])
                        continue

                # **同一個 session，這一趟只送一則**（使用者裁決 2026-11-25：
                # 送進 session 的只有提示詞，**不要一次塞多張**。
                # 實測 2026-11-25：引擎在 90 秒內把 4 張重派工單一起插進
                # 正在跑批次的對話 → 一次多份貼上，擠爆上下文）。
                # 其餘的延到下一趟再送（outbox 不丟，只是晚 30 秒）。
                recipient = item.get("recipient_session") or ""
                if recipient and recipient in sent_sessions:
                    with store.tx(self.conn):
                        self.conn.execute(
                            "UPDATE outbox SET status = ?, next_attempt_at = ?,"
                            " last_error = ?, updated_at = ? WHERE id = ?",
                            (OUT_QUEUED, store.now(time.time() + 30),
                             "same-session: 這一趟已送過一則，延到下一趟",
                             store.now(), item["id"]))
                    report["deferred"].append({"outbox": item["id"],
                                               "reason": "same-session-this-tick"})
                    continue

                result = platform.send_to_session(
                    self.root, {"session_id": item["recipient_session"],
                                "workdir": self._workdir_of(item["recipient_session"])},
                    item["payload"], notif_id=str(item["id"]),
                    endpoint=self.endpoint)
                if recipient:
                    sent_sessions.add(recipient)
                status = OUT_DELIVERED if result.get("state") == "submitted" else OUT_SUBMITTED
                with store.tx(self.conn):
                    self.conn.execute(
                        "UPDATE outbox SET status = ?, attempts = attempts + 1,"
                        " last_error = NULL, next_attempt_at = NULL, updated_at = ? WHERE id = ?",
                        (status, store.now(), item["id"]))
                    store.journal(self.conn, "dispatch.send", item.get("target_id") or "",
                                  session_id=item["recipient_session"] or "",
                                  detail={"outbox_id": item["id"], "task_id": item["task_id"],
                                          "previous_status": previous_status, "result": status})
                report["dispatched"].append({"outbox": item["id"], "state": status})
            except platform.DispatchError as exc:
                attempts = int(item["attempts"] or 0) + 1
                state = OUT_UNKNOWN if exc.state == "unknown" else OUT_FAILED
                delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
                with store.tx(self.conn):
                    self.conn.execute(
                        "UPDATE outbox SET status = ?, attempts = ?, next_attempt_at = ?,"
                        " last_error = ?, updated_at = ? WHERE id = ?",
                        (state, attempts, store.now(time.time() + delay), str(exc),
                         store.now(), item["id"]))
                    store.journal(self.conn, "dispatch.send", item.get("target_id") or "",
                                  session_id=item["recipient_session"] or "",
                                  detail={"outbox_id": item["id"], "task_id": item["task_id"],
                                          "previous_status": previous_status, "result": state,
                                          "reason": exc.state})
                if item["kind"] == "dispatch":
                    self._bump_dispatch_attempt(item["target_id"])
                report["errors"].append("outbox %s: %s" % (item["id"], exc))
            except Exception as exc:
                with store.tx(self.conn):
                    self.conn.execute(
                        "UPDATE outbox SET status = ?, updated_at = ? WHERE id = ?",
                        (OUT_FAILED, store.now(), item["id"]))
                report["errors"].append("outbox %s: %s" % (item["id"], exc))

    def _active_sessions(self) -> Tuple[Optional[List[str]], str]:
        """查一次本機 server 的 active session 清單（查不到就回 None ＋ 原因）。

        查不到**不是錯**（那條路可能本來就沒 server）——回 None，
        呼叫端會照送（見 `_flush_outbox` 裡的說明）。
        """
        try:
            return platform.active_sessions(self.endpoint, self.binary), ""
        except platform.PresenceUnknown as exc:
            return None, str(exc)

    def _dispatch_deferral_reason(self, presence: str, detail: str = "") -> str:
        """為什麼這次沒派出去——**寫清楚原因**，不要只寫 `presence:unknown`。

        「查不到」跟「平台在忙」外觀完全一樣，沒原因就查不下去。
        所以平台回的那句話要帶回來（例如「Could not reach server」）。
        """
        if presence == platform.UNKNOWN and detail:
            return "presence:unknown（%s）" % detail
        return "presence:%s" % presence

    def _workdir_of(self, session_id: str) -> str:
        row = self.conn.execute("SELECT workdir FROM roles WHERE session_id = ?",
                                (session_id,)).fetchone()
        return (row["workdir"] if row and row["workdir"] else "") or ""

    def _bump_dispatch_attempt(self, work_id: str) -> None:
        """派送失敗計數。到上限就標待人處理（不無限喚醒）。

        **只看最新一棒**：歷史 outbox（含死 handle 時代的失敗）一旦累計超過上限，
        舊邏輯會每趟 tick 把 `awaiting_human` 重標回去，讓人類的 grant_more 永遠
        蓋不住（實測 W-001 一直被卡住）。最新一棒已送達 → 通道是通的，
        不因舊失敗重標。
        """
        if not work_id:
            return
        with store.tx(self.conn):
            row = self.conn.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
            if row is None:
                return
            latest = self.conn.execute(
                "SELECT status, attempts FROM outbox WHERE target_id = ? AND kind = 'dispatch'"
                " ORDER BY id DESC LIMIT 1", (work_id,)).fetchone()
            if latest is None or latest["status"] == OUT_DELIVERED:
                return
            attempts = int(latest["attempts"] or 0)
            if attempts >= store.limit_of(self.conn, "max_dispatch", 5):
                self.conn.execute(
                    "UPDATE works SET awaiting_human = 1, awaiting_reason = ?, updated_at = ?"
                    " WHERE id = ?", ("dispatch_failed", store.now(), work_id))

    # ------------------------------------------------------------ 結案

    def _traceability_check(self) -> Dict[str, Any]:
        """專案結案的**雙向追溯檢查**（規格第 8 節）。

        - 每項已確認需求都有結案工單與通過證據
        - 每項結案工單都追得到已確認需求

        **抽成一個函式**：人類走 `project close`、引擎走專案結案檢視，
        兩邊必須**用同一套判定**——否則一邊說過、一邊說沒過。
        """
        missing_works: List[str] = []
        superseded_requirements: List[str] = []
        stale_works: List[str] = []
        requirements = self.conn.execute(
            "SELECT * FROM requirements WHERE confirmed_at IS NOT NULL").fetchall()
        for requirement in requirements:
            # **需求被新需求取代** → 不再要求它有結案工單（新需求自己負責）。
            if requirement["superseded_by"]:
                superseded_requirements.append(requirement["id"])
                for row in self.conn.execute(
                        "SELECT id FROM works WHERE requirement_id = ?"
                        " AND COALESCE(superseded_by, '') = ''",
                        (requirement["id"],)).fetchall():
                    # 需求換了，但底下的工單還沒被標成已取代 → 它們還會被派工做白工。
                    stale_works.append(row["id"])
                continue
            works = self.conn.execute(
                "SELECT * FROM works WHERE requirement_id = ? AND state = ?",
                (requirement["id"], workflow.CLOSED)).fetchall()
            if not works:
                missing_works.append(requirement["id"])
                continue
            for work in works:
                passed = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM results r JOIN checks c"
                    " ON c.work_id = r.work_id AND c.id = r.check_id"
                    " WHERE r.work_id = ? AND c.required = 1 AND r.status = ?",
                    (work["id"], workflow.PASS)).fetchone()["n"]
                required = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM checks WHERE work_id = ? AND required = 1",
                    (work["id"],)).fetchone()["n"]
                if required and passed < required:
                    missing_works.append("%s(%s)" % (requirement["id"], work["id"]))
                    break

        orphan_works = [r["id"] for r in self.conn.execute(
            "SELECT w.id FROM works w WHERE w.state = ?"
            " AND NOT EXISTS (SELECT 1 FROM requirements r"
            "   WHERE r.id = w.requirement_id AND r.confirmed_at IS NOT NULL)",
            (workflow.CLOSED,)).fetchall()]
        ok = not missing_works and not orphan_works and not stale_works
        return {"ok": ok,
                "details": {"requirements_without_closed_work": missing_works,
                            "works_without_confirmed_requirement": orphan_works,
                            "superseded_requirements": superseded_requirements,
                            "works_of_superseded_requirement": stale_works}}

    def project_close(self, confirmed_by: str) -> Dict[str, Any]:
        """專案結案前的**雙向追溯檢查**（規格第 8 節）。

        - 每項已確認需求都有結案工單與通過證據
        - 每項結案工單都追得到已確認需求
        """
        trace = self._traceability_check()
        if not trace["ok"]:
            return {"ok": False, "code": "traceability.incomplete",
                    "message": "雙向追溯未通過",
                    "details": trace["details"]}
        store.set_setting(self.conn, "closed_at", store.now())
        store.set_setting(self.conn, "closed_by", confirmed_by)
        return {"ok": True, "data": {"closed_at": store.now(), "closed_by": confirmed_by}}

    # ------------------------------------------------------------ 待人處理

    def awaiting_list(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, title, state, awaiting_reason, rework_count, revision_count"
            " FROM works WHERE awaiting_human = 1 ORDER BY id").fetchall()
        out = [dict(r) for r in rows]
        issues = self.conn.execute(
            "SELECT id, state, return_count, awaiting_reason FROM issues"
            " WHERE awaiting_human = 1 ORDER BY id").fetchall()
        for row in issues:
            out.append({"id": row["id"], "title": "ISSUE", "state": row["state"],
                        "awaiting_reason": row["awaiting_reason"] or "issue_return",
                        "return_count": row["return_count"]})
        return out

    def awaiting_resolve(self, target_id: str, action: str, reason: str) -> Dict[str, Any]:
        """人類裁決：grant_more／revise／abort（規格第 8 節）。"""
        if action not in ("grant_more", "revise", "abort"):
            return {"ok": False, "code": "action.invalid",
                    "message": "處置只可選：grant_more / revise / abort"}
        with store.tx(self.conn):
            work = workflow.load_work(self.conn, target_id)
            if work is not None:
                reason_code = work.get("awaiting_reason") or ""
                if action == "abort":
                    self.conn.execute(
                        "UPDATE works SET state = ?, awaiting_human = 0,"
                        " awaiting_reason = NULL, version = version + 1, updated_at = ?"
                        " WHERE id = ?", (workflow.CLOSED, store.now(), target_id))
                elif reason_code == "receipt_timeout":
                    # **回執超時的停等人**：工單本身沒有錯、狀態也沒問題——
                    # 只是回執一直核不過（多半是平台證據視窗截斷，不是角色沒做）。
                    #
                    # `grant_more` → 清停等、重置到期計數、**不動 state**：
                    #   繼續等證據；證據晚到就自動套用，**角色不用重交**。
                    # `revise` → 真的要重做：退回 `rework_design`，
                    #   換新 task 讓角色重交一次（**人的決定**，不是引擎自動做的）。
                    if action == "revise":
                        self.conn.execute(
                            "UPDATE works SET state = ?, rework_count = 0,"
                            " awaiting_human = 0, awaiting_reason = NULL,"
                            " version = version + 1, updated_at = ? WHERE id = ?",
                            (workflow.REWORK_DESIGN, store.now(), target_id))
                    else:
                        self.conn.execute(
                            "UPDATE works SET awaiting_human = 0, awaiting_reason = NULL,"
                            " version = version + 1, updated_at = ? WHERE id = ?",
                            (store.now(), target_id))
                    self.conn.execute(
                        "UPDATE counters SET value = 0 WHERE name = ?",
                        ("receipt_expires:%s" % target_id,))
                else:
                    self.conn.execute(
                        "UPDATE works SET awaiting_human = 0, awaiting_reason = NULL,"
                        " rework_count = 0, version = version + 1, updated_at = ? WHERE id = ?",
                        (store.now(), target_id))
                    if action == "grant_more":
                        self.conn.execute(
                            "UPDATE works SET state = ?, updated_at = ? WHERE id = ?",
                            (workflow.REWORK_DESIGN, store.now(), target_id))
                store.record_decision(self.conn, uuid.uuid4().hex[:12],
                                      "awaiting.%s" % action, target_id=target_id,
                                      detail={"reason": reason,
                                              "awaiting_reason": reason_code})
                return {"ok": True, "data": {"target_id": target_id, "action": action}}
            issue = workflow.load_issue(self.conn, target_id)
            if issue is not None:
                issue_reason = issue.get("awaiting_reason") or ""
                self.conn.execute(
                    "UPDATE issues SET state = ?, return_count = 0, awaiting_human = 0,"
                    " awaiting_reason = NULL, version = version + 1, updated_at = ?"
                    " WHERE id = ?",
                    (workflow.ISSUE_CLOSED if action == "abort" else workflow.ISSUE_HANDLING,
                     store.now(), target_id))
                if issue_reason == "receipt_timeout":
                    # 同上：回執超時不是「退回達上限」，重置到期計數即可
                    # （ISSUE 狀態已回 handling；下一趟 `_dispatch_issues` 重派）。
                    self.conn.execute(
                        "UPDATE counters SET value = 0 WHERE name = ?",
                        ("receipt_expires:%s" % target_id,))
                store.record_decision(self.conn, uuid.uuid4().hex[:12],
                                      "awaiting.%s" % action, target_id=target_id,
                                      detail={"reason": reason,
                                              "awaiting_reason": issue_reason})
                return {"ok": True, "data": {"target_id": target_id, "action": action}}
        return {"ok": False, "code": "target.missing", "message": "找不到 %s" % target_id}
