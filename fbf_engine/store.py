"""FBF ENGINE 的**正本層**：SQLite schema、交易邊界、資料存取、租約。

這一層只回答三個問題：資料長什麼樣、誰能改它、改動怎麼保持原子性。
**不知道流程、不知道角色、不知道 agent 怎麼連。**

依據：`doc/spec-fbflow-v2-first-principles.md` 第 4 節（資料契約）與第 6 節（協調器）。

設計要點（都是規格要求，不是偏好）：
- **一個專案一份 `fbf_engine.db`**，啟動時建立；外鍵與 WAL 開啟。
- **所有 JSON 欄位在入口解析、型別與必填校驗一次**；資料庫內只存通過校驗的值。
- 狀態更新用 `WHERE id=? AND version=?`（樂觀鎖）；
  更新筆數為 0 代表競爭或過期提交 → 標 `stale`，**不重試套用**。
- **租約（lease）用資料庫欄位實作**，不依賴記憶體鎖：
  重啟後新進程可取得租約，舊進程每次寫入前須驗租約世代。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import paths

#: 專案根目錄下的正本資料庫檔名。
#:
#: **檔名刻意避開舊版的名稱**：同一個檔名放兩種 schema 會互相毀掉，
#: 所以本包的正本叫 `fbf_engine.db`。
DB_FILENAME = "fbf_engine.db"

#: 租約用途名（首版只有協調器一種寫入者）
LEASE_COORDINATOR = "coordinator"

#: 租約預設存活秒數。超過就可能被新進程接手。
LEASE_TTL_SECONDS = 30


# ---------------------------------------------------------------- 時間

def now(epoch: Optional[float] = None) -> str:
    """本機時間的 ISO 字串（無時區，與 SQLite TEXT 比較一致）。

    帶 `epoch` 可以算出「N 秒前」的時間戳（退避與過期判定用）。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.localtime(time.time() if epoch is None else epoch))


def now_epoch() -> float:
    return time.time()


# ---------------------------------------------------------------- 路徑

def db_path(project_root: str) -> str:
    """專案正本資料庫的絕對路徑。"""
    return os.path.join(os.path.abspath(os.path.expanduser(project_root)), DB_FILENAME)


#: SQLite 報「正本壞了」時的錯誤訊息特徵。
#:
#: 這些不是暫時性錯誤（不像 `database is locked`），**要人從備份還原**。
_CORRUPTED_MARKERS = (
    "file is not a database",
    "database disk image is malformed",
    "file is encrypted",
    "malformed database schema",
)


def is_corrupted(exc: BaseException) -> bool:
    """這個資料庫錯誤是「正本損毀」嗎？

    區分得開很重要：`database is locked` 只是暫時性競爭，
    「file is not a database」代表**檔案本身壞了**——兩者的處置完全不同。
    """
    message = str(exc).lower()
    return any(marker in message for marker in _CORRUPTED_MARKERS)


class PathEscape(Exception):
    """證據路徑逃出專案根目錄。"""


class McpRootProject(Exception):
    """這個路徑在 **MCP 安裝目錄之內**，不能拿來當專案。

    **引擎的安裝目錄整棵樹都不放專案資料**（安裝狀態必須與專案無關）。
    在這裡建立正本，等於讓「引擎的安裝目錄」變成一個專案——
    這個檔案之後還會讓 MCP 伺服器把安裝目錄認錯成專案根。
    """


def resolve_inside(project_root: str, relative: str) -> str:
    """把證據路徑解析成專案根內的絕對路徑。

    規則（規格第 4 節）：
    - 必須是相對路徑；絕對路徑一律拒絕。
    - canonicalize 之後必須仍在專案根內（防 `..` 與符號連結逃逸）。
    - 不存在也可以（驗收時才會發現），但**逃逸一律拒絕**。
    """
    if not isinstance(relative, str) or not relative.strip():
        raise PathEscape("證據路徑是空的")
    raw = relative.strip().replace("\\", "/")
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        raise PathEscape("證據路徑必須是相對路徑：%s" % relative)
    root = os.path.realpath(os.path.abspath(os.path.expanduser(project_root)))
    target = os.path.realpath(os.path.join(root, raw))
    if target != root and not target.startswith(root + os.sep):
        raise PathEscape("證據路徑逃出專案根目錄：%s" % relative)
    return target


def relative_inside(project_root: str, absolute: str) -> str:
    """把絕對路徑轉回專案根內的相對路徑（用 `/` 分隔）。"""
    root = os.path.realpath(os.path.abspath(os.path.expanduser(project_root)))
    target = os.path.realpath(os.path.abspath(absolute))
    if target != root and not target.startswith(root + os.sep):
        raise PathEscape("路徑不在專案根目錄內：%s" % absolute)
    return os.path.relpath(target, root).replace("\\", "/")


def file_fingerprint(absolute: str) -> Tuple[int, str]:
    """回傳 (大小, SHA-256)。檔案不存在或讀不到就丟 OSError。"""
    digest = hashlib.sha256()
    size = 0
    with open(absolute, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


# ---------------------------------------------------------------- 連線

class _Connection(sqlite3.Connection):
    """帶一把可重入鎖的連線。

    `sqlite3.Connection` 不准掛自訂屬性（沒有 `__dict__`），
    所以用标准的 `factory=` 子類把鎖掛上去。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fbf_lock = threading.RLock()


def connect(project_root: str) -> sqlite3.Connection:
    """開啟（必要時建立）專案正本資料庫。

    `check_same_thread=False`：呼叫端可能從不同執行緒用這條連線，
    所以**每個執行緒要開自己的一條**（見 `engine.Coordinator.conn`）——
    CPython 用 `SQLITE_OPEN_NOMUTEX` 開連線，底層不再替這條連線序列化，
    兩個執行緒共用一條就是未定義行為（實測在 macOS 上是 SIGSEGV）。

    這裡的 `timeout` 與 `busy_timeout` 處理**跨連線**的並行：
    多個執行緒各開一條連線寫同一個檔案時，由 SQLite 的檔案鎖序列化。
    """
    root = os.path.abspath(os.path.expanduser(project_root))
    if paths.is_under_mcp_root(root):
        # **最後一道閘門**：正本絕不建立在引擎的安裝目錄（含其子目錄）。
        # 沒有這裡，一次在引擎目錄裡跑的指令就會留下 `fbf_engine.db`，
        # 之後 MCP 伺服器會把安裝目錄當成專案根（規格第 4、11 節）。
        raise McpRootProject(
            "引擎的安裝目錄不存放專案資料（含其子目錄），"
            "不能在這裡建立正本：%s" % root)
    if not os.path.isdir(root):
        os.makedirs(root, exist_ok=True)
    path = db_path(root)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None,
                           check_same_thread=False, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


#: 全部 DDL。順序無關（外鍵在 SQLite 內是宣告時檢查）。
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requirements (
    id                TEXT PRIMARY KEY,          -- R-001
    text              TEXT NOT NULL,
    success_criterion TEXT NOT NULL,
    confirmed_at      TEXT,                     -- NULL = 尚未經人類確認
    superseded_by     TEXT,                     -- 需求變了 → 被哪條新需求取代
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_compacts (
    session_id   TEXT PRIMARY KEY,              -- 席位上的 session id
    compacted_at TEXT NOT NULL,                 -- 上一次 compact 的時間
    source       TEXT NOT NULL,                 -- detected（宿主紀錄）／requested（引擎要求過）
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    role         TEXT NOT NULL,
    short_name   TEXT NOT NULL,
    session_id   TEXT,                          -- 同一個 session 不得重複佔兩列
    agent        TEXT NOT NULL DEFAULT 'opencode',
    status       TEXT NOT NULL DEFAULT 'active',-- active / revoked
    project_code TEXT NOT NULL DEFAULT '',
    workdir      TEXT,
    uuid_sha256  TEXT,                          -- 只存雜湊，不存明文
    created_at   TEXT NOT NULL,
    updated_at   TEXT,
    PRIMARY KEY (role, short_name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_session ON roles(session_id)
    WHERE session_id IS NOT NULL AND session_id <> '';

CREATE TABLE IF NOT EXISTS milestones (
    id           TEXT PRIMARY KEY,               -- MS-001
    seq          INTEGER NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    outcome      TEXT NOT NULL,                 -- 派第一張單前就要寫定
    state        TEXT NOT NULL DEFAULT 'open',  -- open / reviewing / done
    review_count INTEGER NOT NULL DEFAULT 0,
    version      INTEGER NOT NULL DEFAULT 1,    -- 樂觀鎖：檢視回報要帶這個版本
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_reviews (
    id           TEXT PRIMARY KEY,              -- PRJ-001
    state        TEXT NOT NULL,                 -- reviewing/closed/awaiting_human
    version      INTEGER NOT NULL DEFAULT 1,    -- 樂觀鎖：回報要帶這個版本
    review_count INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_improvements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id  TEXT,                           -- 哪一輪專案結案檢視提出的
    target     TEXT,                           -- 針對哪裡（欄位／單據／引擎）
    suggestion TEXT NOT NULL,                  -- 引擎改善建議
    reason     TEXT,                           -- 依據（退件紀錄摘要）
    status     TEXT NOT NULL DEFAULT 'open',   -- open／done／rejected（是否已處理）
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engine_improvements_review ON engine_improvements(review_id);

CREATE TABLE IF NOT EXISTS works (
    id                TEXT PRIMARY KEY,          -- W-001
    title             TEXT NOT NULL,
    requirement_id    TEXT NOT NULL REFERENCES requirements(id),
    milestone_id      TEXT REFERENCES milestones(id),
    state             TEXT NOT NULL,             -- 見 workflow.WORK_STATES
    version           INTEGER NOT NULL DEFAULT 1,
    assigned_role     TEXT,
    assigned_session  TEXT,
    content           TEXT NOT NULL DEFAULT '',   -- 工單內容（自然語言）
    expected_outputs  TEXT NOT NULL DEFAULT '[]',
    skill             TEXT,                       -- 搭配的專案 SKILL（spec：PLANNER 填寫）
    selection         TEXT,                       -- JSON：選案（設計驗收方式中）
    build_result      TEXT,                       -- JSON：施工結果（施工中／重工中）
    rework_plan       TEXT,                       -- JSON：重工方案（重工方案設計中）
    rework_count      INTEGER NOT NULL DEFAULT 0,
    revision_count    INTEGER NOT NULL DEFAULT 0,
    awaiting_human    INTEGER NOT NULL DEFAULT 0,
    awaiting_reason   TEXT,
    cases_ran         INTEGER,
    execution_kind    TEXT,                     -- test / review（本單主要驗收方式）
    verdict           TEXT,                     -- pass / fail / partial（程式算）
    rework_reason     TEXT,
    rework_type       TEXT,                     -- 施工錯 / 驗收錯 / 解法錯
    superseded_by     TEXT,                     -- 被取代：取代它的新工單（JSON 陣列）
    supersede_reason  TEXT,                     -- 為什麼被取代（需求／做法變了）
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_works_state ON works(state);
CREATE INDEX IF NOT EXISTS idx_works_milestone ON works(milestone_id);

CREATE TABLE IF NOT EXISTS solutions (
    id                 TEXT PRIMARY KEY,
    work_id            TEXT NOT NULL REFERENCES works(id),
    subagent_context_id TEXT,             -- 舊欄位（SUBAGENT 取消後可空；保留供追溯）
    approach           TEXT NOT NULL,
    cost               TEXT,
    change_scope       TEXT,
    effort             TEXT,
    risk               TEXT,
    verifiable_by      TEXT,
    reuses             TEXT,
    design_note        TEXT,
    round              INTEGER NOT NULL DEFAULT 1,  -- 第幾輪的解法（重工解法另算一輪）
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    work_id        TEXT NOT NULL REFERENCES works(id),
    id             TEXT NOT NULL,
    requirement_id TEXT NOT NULL REFERENCES requirements(id),
    method         TEXT NOT NULL,
    scope          TEXT,
    threshold      TEXT,
    evidence       TEXT,
    required       INTEGER NOT NULL DEFAULT 1,
    execution_kind TEXT NOT NULL DEFAULT 'test', -- test / review
    created_at     TEXT NOT NULL,
    PRIMARY KEY (work_id, id)
);

CREATE TABLE IF NOT EXISTS results (
    work_id     TEXT NOT NULL REFERENCES works(id),
    check_id    TEXT NOT NULL,
    round       INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL,                  -- PASS / FAIL / PARTIAL / NOT_RUN
    reason      TEXT,
    evidence_id TEXT REFERENCES evidence(id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (work_id, check_id, round)
);

CREATE TABLE IF NOT EXISTS evidence (
    id                  TEXT PRIMARY KEY,
    project_relative_path TEXT NOT NULL,
    size                INTEGER NOT NULL,
    sha256              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id                TEXT PRIMARY KEY,          -- IS-001
    work_id           TEXT REFERENCES works(id),
    initiator_session TEXT,
    initiator_role    TEXT,
    state             TEXT NOT NULL,             -- 見 workflow.ISSUE_STATES
    trigger_kind      TEXT NOT NULL,             -- work_blocked / needs_decision
    problem           TEXT,
    location          TEXT,
    tried             TEXT,
    impact            TEXT,
    question          TEXT,
    answer            TEXT,
    completion_criteria TEXT,                    -- ISSUE 完成要求（達成什麼效果才算解決）
    countermeasure   TEXT,                       -- 對策（PLANNER 選的那一案＋要怎麼修）
    selection        TEXT,                       -- 選案（JSON：選哪一案、為什麼）
    return_reason     TEXT,
    return_count      INTEGER NOT NULL DEFAULT 0,
    linked_work_ids   TEXT NOT NULL DEFAULT '[]',
    payload           TEXT NOT NULL DEFAULT '{}',
    version           INTEGER NOT NULL DEFAULT 1,
    awaiting_human    INTEGER NOT NULL DEFAULT 0, -- 退回達上限 → 標待人處理
    awaiting_reason   TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id               TEXT PRIMARY KEY,            -- S-000001
    task_id          TEXT NOT NULL,
    work_id          TEXT REFERENCES works(id),
    issue_id         TEXT REFERENCES issues(id),
    expected_version INTEGER NOT NULL,
    session_id       TEXT NOT NULL,
    stage            TEXT NOT NULL,
    payload          TEXT NOT NULL,
    message_id       TEXT,
    status           TEXT NOT NULL,              -- pending/applied/stale/unverified/rejected
    gaps             TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (task_id, session_id, stage, expected_version)
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);

CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    work_id          TEXT REFERENCES works(id),
    issue_id         TEXT REFERENCES issues(id),
    milestone_id     TEXT REFERENCES milestones(id),
    project_review_id TEXT REFERENCES project_reviews(id),
    stage            TEXT NOT NULL,
    assigned_session TEXT,
    assigned_role    TEXT,
    status           TEXT NOT NULL DEFAULT 'open', -- open / done / cancelled
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_work ON tasks(work_id, status);

CREATE TABLE IF NOT EXISTS outbox (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT,
    kind              TEXT NOT NULL,             -- dispatch / gate / reminder / issue
    recipient_session TEXT,
    recipient_role    TEXT,
    target_id         TEXT,
    payload           TEXT NOT NULL,
    status            TEXT NOT NULL,             -- queued/sending/submitted/delivered/failed/unknown
    attempts          INTEGER NOT NULL DEFAULT 0,
    deferrals         INTEGER NOT NULL DEFAULT 0, -- presence 查不到／忙碌而暫緩的次數
    next_attempt_at   TEXT,
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS receipts (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    model_id     TEXT,
    context_id   TEXT,
    completed_at TEXT,
    verified_at  TEXT,
    UNIQUE (session_id, message_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id       TEXT NOT NULL,
    target_id     TEXT,
    from_state    TEXT,
    to_state      TEXT,
    submission_id TEXT,
    receipt_id    TEXT,
    gaps          TEXT NOT NULL DEFAULT '[]',
    action        TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id    TEXT NOT NULL REFERENCES works(id),
    from_state TEXT,
    to_state   TEXT,
    from_role  TEXT,
    to_role    TEXT,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejection_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind     TEXT NOT NULL,             -- work / issue
    target_id       TEXT NOT NULL,
    submission_id   TEXT,
    stage           TEXT,                      -- 退件發生在哪一棒
    session_id      TEXT,                      -- 退的是誰的件
    role            TEXT,
    short_name      TEXT,
    gaps            TEXT NOT NULL DEFAULT '[]',-- 缺口代碼
    fields          TEXT NOT NULL DEFAULT '[]',-- 為了哪個欄位退件
    reason          TEXT NOT NULL DEFAULT '',  -- 退件原因（人可讀）
    payload         TEXT NOT NULL DEFAULT '{}',-- 交件完整副本
    target_snapshot TEXT NOT NULL DEFAULT '{}',-- 被退單據當下的完整副本
    details         TEXT NOT NULL DEFAULT '[]',-- 逐欄位解說
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejection_log_target ON rejection_log(target_kind, target_id);

CREATE TABLE IF NOT EXISTS leases (
    name       TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    owner      TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    op         TEXT NOT NULL,
    actor      TEXT,
    session_id TEXT,
    target_id  TEXT,
    detail     TEXT
);
"""


#: 加法式遷移：(表, 欄位, 定義)。只加欄位，不刪不改。
#: 既有專案的資料庫是舊版 schema 建的，`CREATE TABLE IF NOT EXISTS` 不會補欄位——
#: 沒有這裡就會在第一次用到新欄位時炸掉。
MIGRATIONS = [
    ("roles", "uuid_sha256", "TEXT"),
    ("roles", "updated_at", "TEXT"),
    ("works", "title", "TEXT"),
    ("works", "expected_outputs", "TEXT NOT NULL DEFAULT '[]'"),
    ("works", "selection", "TEXT"),
    ("works", "build_result", "TEXT"),
    ("works", "rework_plan", "TEXT"),
    ("works", "assigned_role", "TEXT"),
    ("works", "rework_reason", "TEXT"),
    ("works", "rework_type", "TEXT"),
    ("works", "cases_ran", "INTEGER"),
    ("works", "execution_kind", "TEXT"),
    ("works", "superseded_by", "TEXT"),
    ("works", "supersede_reason", "TEXT"),
    ("requirements", "superseded_by", "TEXT"),
    ("works", "verdict", "TEXT"),
    ("outbox", "deferrals", "INTEGER NOT NULL DEFAULT 0"),
    ("issues", "version", "INTEGER NOT NULL DEFAULT 1"),
    ("issues", "payload", "TEXT NOT NULL DEFAULT '{}'"),
    ("issues", "initiator_role", "TEXT"),
    ("issues", "initiator_session", "TEXT"),
    ("issues", "trigger_kind", "TEXT"),
    ("issues", "awaiting_human", "INTEGER NOT NULL DEFAULT 0"),
    ("issues", "awaiting_reason", "TEXT"),
    ("tasks", "milestone_id", "TEXT"),
    ("milestones", "version", "INTEGER NOT NULL DEFAULT 1"),
    ("solutions", "round", "INTEGER NOT NULL DEFAULT 1"),
    ("issues", "completion_criteria", "TEXT"),
    ("issues", "countermeasure", "TEXT"),
    ("issues", "selection", "TEXT"),
    ("works", "skill", "TEXT"),
    ("tasks", "project_review_id", "TEXT"),
    ("engine_improvements", "status", "TEXT NOT NULL DEFAULT 'open'"),
]

#: 舊預設 → 新預設（spec 改了上限時用）。
#: **只換「等於舊預設」的值**——人自己調過的數字不動。
SETTING_DEFAULTS_CHANGED = [
    ("max_return", "3", "2"),   # spec 第 4(b) 步：連續兩次未結案就停等人類
]


def migrate_settings(conn: sqlite3.Connection) -> List[str]:
    """把還是舊預設值的設定換成新的（人調過的不動）。"""
    changed: List[str] = []
    for name, old, new in SETTING_DEFAULTS_CHANGED:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (name,)).fetchone()
        if row is None or row[0] != old:
            continue
        conn.execute("UPDATE settings SET value = ? WHERE key = ?", (new, name))
        changed.append("%s: %s → %s" % (name, old, new))
    return changed


def migrate_solutions_without_context_unique(conn: sqlite3.Connection) -> List[str]:
    """**SUBAGENT 取消**（2026-11-23）：`solutions` 不再要求每案不同的 context id。

    舊 schema：`subagent_context_id TEXT NOT NULL` ＋ `UNIQUE (work_id, subagent_context_id)`。
    新 schema：欄位可空、拿掉 UNIQUE（兩案不再由不同 SUBAGENT 產生，改由 THINKER 一棒產出）。

    SQLite 沒有 `ALTER COLUMN` → 只能重建表（資料照抄、id 不變）。
    已經是新版就跳過（看 DDL 有沒有那條 UNIQUE）。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'solutions'").fetchone()
    ddl = (row[0] if row else "") or ""
    if "UNIQUE (work_id, subagent_context_id)" not in ddl:
        return []
    conn.executescript(
        "PRAGMA foreign_keys=off;"
        "CREATE TABLE solutions_new ("
        " id TEXT PRIMARY KEY,"
        " work_id TEXT NOT NULL REFERENCES works(id),"
        " subagent_context_id TEXT,"
        " approach TEXT NOT NULL,"
        " cost TEXT, change_scope TEXT, effort TEXT, risk TEXT,"
        " verifiable_by TEXT, reuses TEXT, design_note TEXT,"
        " round INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT NOT NULL);"
        "INSERT INTO solutions_new SELECT id, work_id, subagent_context_id, approach, cost,"
        " change_scope, effort, risk, verifiable_by, reuses, design_note, round, created_at"
        " FROM solutions;"
        "DROP TABLE solutions;"
        "ALTER TABLE solutions_new RENAME TO solutions;")
    return ["solutions: 取消 subagent_context_id 的 NOT NULL 與 UNIQUE"]


def migrate_schema(conn: sqlite3.Connection) -> List[str]:
    """把既有資料庫補到當前 schema（只加欄位）。回傳實際動了什麼。"""
    applied: List[str] = []
    applied.extend(migrate_solutions_without_context_unique(conn))
    tables = set(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall())
    for table, column, definition in MIGRATIONS:
        if table not in tables:
            continue
        columns = set(r[1] for r in conn.execute(
            "PRAGMA table_info(%s)" % table).fetchall())
        if column in columns:
            continue
        try:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))
            applied.append("%s.%s" % (table, column))
        except sqlite3.Error:
            continue
    return applied


def init_db(conn: sqlite3.Connection) -> None:
    """建立 schema（可重複呼叫）並補齊既有資料庫缺的欄位。"""
    conn.executescript(SCHEMA)
    migrate_schema(conn)
    migrate_settings(conn)
    ensure_setting(conn, "project_code", "")
    ensure_setting(conn, "name", "")
    ensure_setting(conn, "opencode_endpoint", "")
    ensure_setting(conn, "max_rework", "2")
    ensure_setting(conn, "max_revision", "2")
    ensure_setting(conn, "max_return", "2")
    ensure_setting(conn, "max_dispatch", "5")
    ensure_setting(conn, "max_review", "2")
    ensure_setting(conn, "max_deferrals", "240")
    ensure_setting(conn, "seat_stale_hours", "12")
    ensure_setting(conn, "max_receipt_expires", "3")


# ---------------------------------------------------------------- 交易

@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """交易邊界。**可巢狀**（內層用 SAVEPOINT）。

    外層開 BEGIN IMMEDIATE（先拿寫鎖，避免升級鎖時死鎖）；
    內層用 SAVEPOINT，例外時只回捲內層。

    同一條連線可能被多個執行緒用（工具呼叫 ＋ 背景 timer），
    所以整段交易由連線上的 `_fbf_lock` 序列化——**不讓兩個執行緒交錯進同一個交易**。
    """
    lock = getattr(conn, "_fbf_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        if conn.in_transaction:
            name = "sp_%d" % int(time.time() * 1000000 % 1000000000)
            conn.execute("SAVEPOINT %s" % name)
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK TO %s" % name)
                conn.execute("RELEASE %s" % name)
                raise
            conn.execute("RELEASE %s" % name)
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        if lock is not None:
            lock.release()


# ---------------------------------------------------------------- 設定

def ensure_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def limit_of(conn: sqlite3.Connection, key: str, default: int) -> int:
    """讀上限設定；壞值退回預設（不要因為設定壞掉就停機）。"""
    raw = get_setting(conn, key, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# ---------------------------------------------------------------- ID

def next_id(conn: sqlite3.Connection, counter: str, prefix: str, width: int = 3) -> str:
    """產生流水號（R-001 / W-001 / MS-001 / IS-001 / S-000001）。"""
    conn.execute("INSERT INTO counters(name, value) VALUES (?, 0) "
                 "ON CONFLICT(name) DO NOTHING", (counter,))
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (counter,))
    row = conn.execute("SELECT value FROM counters WHERE name = ?", (counter,)).fetchone()
    return "%s-%0*d" % (prefix, width, int(row["value"]))


# ---------------------------------------------------------------- 租約

def read_lease(conn: sqlite3.Connection, name: str = LEASE_COORDINATOR) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT name, generation, owner, expires_at FROM leases WHERE name = ?",
                       (name,)).fetchone()
    if row is None:
        return None
    return {"name": row["name"], "generation": int(row["generation"]),
            "owner": row["owner"], "expires_at": float(row["expires_at"])}


def acquire_lease(conn: sqlite3.Connection, owner: str,
                  ttl: float = LEASE_TTL_SECONDS,
                  name: str = LEASE_COORDINATOR,
                  force: bool = False) -> Optional[int]:
    """取得租約。**拿不到就回 None**（不要靜默搶）。

    規則（規格第 6 節）：
    - 租約還在且持有者不是自己 → 拿不到（除非 `force`，見下）。
    - 租約已過期 → 新持有者接手，**世代 +1**。
    - 自己已持有 → 續約，世代不變。

    `force=True`：**強制接手**（世代 +1）。只有在一種情況下用——
    持有者跑的是**舊版**引擎，而新版必須接管（否則舊行程會一直霸著租約，
    新版的修正永遠載入不了）。世代 +1 會讓舊行程的續約立刻失敗 →
    舊行程自然收手，不會兩個行程同時寫 ✓。
    """
    current = read_lease(conn, name)
    stamp = now_epoch()
    if current is None:
        conn.execute("INSERT INTO leases(name, generation, owner, expires_at) VALUES (?, 1, ?, ?)",
                     (name, owner, stamp + ttl))
        return 1
    if current["owner"] == owner:
        conn.execute("UPDATE leases SET expires_at = ? WHERE name = ?", (stamp + ttl, name))
        return current["generation"]
    if current["expires_at"] > stamp and not force:
        return None
    generation = current["generation"] + 1
    conn.execute("UPDATE leases SET generation = ?, owner = ?, expires_at = ? WHERE name = ?",
                 (generation, owner, stamp + ttl, name))
    return generation


def renew_lease(conn: sqlite3.Connection, owner: str, generation: int,
                ttl: float = LEASE_TTL_SECONDS,
                name: str = LEASE_COORDINATOR) -> bool:
    """續約。**世代不符就失敗**——舊進程不得續到新世代的租約。"""
    current = read_lease(conn, name)
    if current is None:
        return False
    if current["owner"] != owner or current["generation"] != generation:
        return False
    conn.execute("UPDATE leases SET expires_at = ? WHERE name = ?", (now_epoch() + ttl, name))
    return True


def lease_valid(conn: sqlite3.Connection, owner: str, generation: int,
                name: str = LEASE_COORDINATOR) -> bool:
    """寫入前驗租約世代。"""
    current = read_lease(conn, name)
    if current is None:
        return False
    return current["owner"] == owner and current["generation"] == generation


def release_lease(conn: sqlite3.Connection, owner: str, generation: int,
                  name: str = LEASE_COORDINATOR) -> None:
    current = read_lease(conn, name)
    if current and current["owner"] == owner and current["generation"] == generation:
        conn.execute("UPDATE leases SET expires_at = 0 WHERE name = ?", (name,))


# ---------------------------------------------------------------- 寫入

def bump_version(conn: sqlite3.Connection, table: str, row_id: str,
                 expected_version: int) -> bool:
    """樂觀鎖更新主體。**回 False 代表被別人改過或提交已過期。**

    呼叫端拿到 False 必須標 `stale`，不可重試套用。
    """
    cur = conn.execute(
        "UPDATE %s SET version = version + 1, updated_at = ? WHERE id = ? AND version = ?"
        % table, (now(), row_id, expected_version))
    return cur.rowcount == 1


def journal(conn: sqlite3.Connection, op: str, target_id: str = "",
            actor: str = "", session_id: str = "", detail: Any = None) -> None:
    import json
    conn.execute(
        "INSERT INTO journal(ts, op, actor, session_id, target_id, detail) VALUES (?,?,?,?,?,?)",
        (now(), op, actor, session_id, target_id,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def record_decision(conn: sqlite3.Connection, scan_id: str, action: str,
                    target_id: str = "", from_state: str = "", to_state: str = "",
                    submission_id: str = "", receipt_id: str = "",
                    gaps: Optional[Iterable[str]] = None,
                    detail: Optional[Dict[str, Any]] = None) -> int:
    """追加一筆裁決紀錄。**只追加，不更新。**"""
    import json
    cur = conn.execute(
        "INSERT INTO decisions(scan_id, target_id, from_state, to_state, submission_id,"
        " receipt_id, gaps, action, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (scan_id, target_id, from_state, to_state, submission_id, receipt_id,
         json.dumps(list(gaps or []), ensure_ascii=False), action,
         json.dumps(detail or {}, ensure_ascii=False), now()))
    return int(cur.lastrowid)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]
