"""FBF ENGINE 的 **MCP 介面層**：session 與正本之間唯一的門。

規格第 11 節（契約）：
- 所有工具回覆同一外殼：
  成功 `{"ok": true, "data": {...}}`
  失敗 `{"ok": false, "code": "穩定代碼", "message": "人可讀說明", "details": {...}}`
- **失敗不得偷偷改正本。**
- 工具的 `project` 由 host 綁定，**不讓 session 自報路徑**。
- 每個寫入工具都要求有效 task、符合角色、`expected_version` 等於正本版本。
- MCP 在入口檢查型別、欄目白名單與大小上限，**再交 workflow**。

---

## 兩個範圍（這是本檔最重要的設計）

| 範圍 | 誰的東西 | 放哪 |
|---|---|---|
| **MCP 安裝範圍** | 引擎程式碼。**沒有任何專案資料，也沒有專案註冊表** | MCP 根目錄 |
| **專案範圍** | 工單、角色、需求、憑證、回執 | **加入這個 MCP 的那個專案自己的目錄** |

**安裝 MCP 不需要任何專案、任何角色。** 安裝只是把引擎的入口放進 OpenCode 設定。

某個專案要用這個 MCP 推動工作流時，它才**自己加入**：
1. 在自己的 `opencode.json` 裡加這個 MCP，並帶 `FBF_PROJECT_ROOT=<該專案>`；
2. 建立角色席位（`fbf.py role add`），session 才認領（`fbflow_register`）。

**「交付目錄」永遠是那個加入的專案的目錄**——不是 MCP 根目錄，
也不是另外開一個專用目錄。

---

## 身分：兩道

1. **host 綁定（主證據）**：OpenCode 在 `CallToolRequest.params._meta["ai.opencode/sessionID"]`
   帶入呼叫者的 session id。這是 host 塞的，**不是 session 自報的**。
   （備援：環境變數 `FBF_SESSION_TOKEN` 對到專案憑證表。）
2. **session UUID（附加校驗）**：註冊時由 MCP 發給該 session，
   **只出現在它自己的私有提示詞裡**；資料庫只存雜湊。

**兩道都過才放行寫入。** 缺任一 → `G-SESSION-UNVERIFIED`。

**人類控制命令（init／requirement confirm／engine status／engine tick --once／
awaiting list／awaiting resolve／project close）不在這裡**——它們走 CLI，
不得掛在 session 可見的 MCP 工具上。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import compact, engine, paths, project, prompts, store, workflow

#: MCP 協定版本
PROTOCOL_VERSION = "2024-11-05"

#: 伺服器識別
SERVER_NAME = "fbf-engine-mcp"
SERVER_VERSION = "1.1.0"

#: 環境變數：host 綁定的專案根（**專案層** opencode.json 設定它）
ENV_PROJECT_ROOT = "FBF_PROJECT_ROOT"

#: 環境變數：host 綁定的 session token（備援的身分來源）
ENV_SESSION_TOKEN = "FBF_SESSION_TOKEN"

#: 環境變數：宿主終端 handle（host CLI 設定給終端裡的程式，模型改不了）
#: → 與 OpenCode `_meta` 同級的**主證據**：host 綁定。
ENV_TERMINAL_HANDLE = "ORCA_TERMINAL_HANDLE"

#: 憑證檔（人類寫的，模型碰不到）
CREDENTIALS_DIR = ".fbf"
CREDENTIALS_FILE = "credentials.json"

#: OpenCode 在 tools/call 裡帶 session id 的位置
META_SESSION_KEY = "ai.opencode/sessionID"

#: payload 大小上限（1 MiB）
MAX_PAYLOAD_BYTES = 1024 * 1024

#: 穩定錯誤代碼
CODE_UNKNOWN_FIELD = "INPUT-UNKNOWN-FIELD"
CODE_CONTENT_MISSING = "CONTENT-MISSING"
CODE_TASK_STALE = "TASK-STALE"
CODE_ROLE_DENIED = "ROLE-DENIED"
CODE_SESSION_UNVERIFIED = "G-SESSION-UNVERIFIED"
CODE_PROJECT_UNRESOLVED = "G-PROJECT-UNRESOLVED"
CODE_PROJECT_NOT_INITIALIZED = "G-PROJECT-NOT-INITIALIZED"
CODE_PROJECT_CORRUPTED = "G-PROJECT-CORRUPTED"
CODE_RECEIPT_PENDING = "RECEIPT-PENDING"
CODE_INPUT_INVALID = "INPUT-INVALID"
CODE_WORK_REJECTED = "WORK-REJECTED"
CODE_WORK_SUPERSEDED = "WORK-SUPERSEDED"
CODE_WORK_SUPERSEDE_DENIED = "WORK-SUPERSEDE-DENIED"
CODE_NOT_FOUND = "NOT-FOUND"
CODE_INTERNAL = "INTERNAL"


def ok(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _force_utf8(stream: Any, errors: str = "strict") -> None:
    """把一個文字流設成 UTF-8（stdio 契約要求）。

    已經是 UTF-8 就什麼都不做；不支援 `reconfigure` 的流（例如測試注入的
    `io.StringIO`）直接略過。**這裡不能看環境變數**——stdio 契約要求的就是
    UTF-8，讓 `PYTHONIOENCODING` 或主控台字碼頁決定會讓握手在某些機器上壞掉。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors=errors)
    except (ValueError, OSError, LookupError):
        pass


def fail(code: str, message: str, **details: Any) -> Dict[str, Any]:
    return {"ok": False, "code": code, "message": message, "details": details}


# ---------------------------------------------------------------- 工具定義

def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_OBJ = {"type": "object"}
_ARR = {"type": "array"}

#: 工具表。每個工具都有：誰能呼叫、參數白名單、必填欄位。
#: **這份表同時是 MCP `tools/list` 的來源**——不另寫一份 schema 文件。
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "fbflow_project_join",
        "title": "把這個專案加入 FBF ENGINE",
        "description": (
            "讓目前工作目錄成為一個 FBF 專案：建立正本（fbf_engine.db）、"
            "建立 PLANNER／THINKER／WORKER 三個席位、把這個 MCP 寫進該專案的"
            "設定檔（帶 FBF_PROJECT_ROOT），並把這個 session 綁到該專案。"
            "**新專案的第一個動作就是這個**——呼叫後其他工具才會開始工作。"
            "只能加入目前工作目錄或它的子目錄；不給路徑就用目前工作目錄。"
            "已經是專案時不會洗掉既有資料，只會補上缺的部分。"),
        "for_roles": None,
        "allowed": {"project_root", "name"},
        "required": [],
        "inputSchema": _schema({
            "project_root": _STR,
            "name": _STR,
        }, []),
    },
    {
        "name": "fbflow_register",
        "title": "註冊角色",
        "description": "把這個 session 綁到本專案的一個角色席位，取回專屬提示詞（含專屬 UUID）。"
                       "**每個 session 只能註冊一次。**",
        "for_roles": None,
        "allowed": {"role", "short_name", "agent"},
        "required": ["role", "short_name"],
        "inputSchema": _schema({
            "role": {"type": "string", "enum": ["PLANNER", "THINKER", "WORKER"]},
            "short_name": _STR,
            "agent": _STR,
        }, ["role", "short_name"]),
    },
    {
        "name": "fbflow_read",
        "title": "讀取正本",
        "description": "讀工單／ISSUE／里程碑／需求的角色可見正本快照（含 version 與交接）。",
        "for_roles": None,
        "allowed": {"target_kind", "target_id"},
        "required": ["target_kind", "target_id"],
        "inputSchema": _schema({
            "target_kind": {"type": "string", "enum": ["work", "issue", "milestone", "requirement"]},
            "target_id": _STR,
        }, ["target_kind", "target_id"]),
    },
    {
        "name": "fbflow_report_work",
        "title": "回報工單這一棒",
        "description": "交出這一棒的成果。**不轉狀態**——狀態只由協調器推進。"
                       "同一組參數重送會回傳原本的 submission ID（冪等）。",
        "for_roles": None,
        "allowed": {"task_id", "work_id", "expected_version", "stage", "payload",
                    "message_id", "session_uuid"},
        "required": ["task_id", "work_id", "expected_version", "payload",
                     "message_id"],
        "inputSchema": _schema({
            "task_id": _STR,
            "work_id": _STR,
            "expected_version": _INT,
            "stage": {"type": "string",
                      "description": "可留空：引擎依 task_id 判斷這一棒"
                                     "（提示詞不帶工作狀態，spec 第 70 行）"},
            "payload": _OBJ,
            "message_id": _STR,
            "session_uuid": {"type": "string",
                             "description": "註冊時發給你的專屬 UUID（只有你知道）"},
        }, ["task_id", "work_id", "expected_version", "payload", "message_id",
            "session_uuid"]),
    },
    {
        "name": "fbflow_report_issue",
        "title": "回報 ISSUE 這一棒",
        "description": "交出 ISSUE 這一棒的處理結果。**不轉狀態**。",
        "for_roles": None,
        "allowed": {"task_id", "issue_id", "expected_version", "stage", "payload",
                    "message_id", "session_uuid"},
        "required": ["task_id", "issue_id", "expected_version", "payload",
                     "message_id"],
        "inputSchema": _schema({
            "task_id": _STR,
            "issue_id": _STR,
            "expected_version": _INT,
            "stage": {"type": "string",
                      "description": "可留空：引擎依 task_id 判斷這一棒"},
            "payload": _OBJ,
            "message_id": _STR,
            "session_uuid": _STR,
        }, ["task_id", "issue_id", "expected_version", "payload", "message_id",
            "session_uuid"]),
    },
    {
        "name": "fbflow_report_milestone",
        "title": "回報里程碑檢視",
        "description": "段末對照 outcome 的結果。動作只可選 continue／add_work／await_human。",
        "for_roles": None,
        "allowed": {"task_id", "milestone_id", "expected_version", "action", "reason",
                    "new_works", "session_uuid"},
        "required": ["task_id", "milestone_id", "expected_version", "action"],
        "inputSchema": _schema({
            "task_id": _STR,
            "milestone_id": _STR,
            "expected_version": _INT,
            "action": {"type": "string", "enum": list(workflow.MS_REVIEW_ACTIONS)},
            "reason": _STR,
            "new_works": _ARR,
            "session_uuid": _STR,
        }, ["task_id", "milestone_id", "expected_version", "action", "session_uuid"]),
    },
    {
        "name": "fbflow_report_project_review",
        "title": "回報專案結案檢視",
        "description": "**專案層級**的結案判定：把產出跟任務目標比一比。"
                       "動作只可選 complete（達成→標專案結案）／add_work（未達成→補工單）／"
                       "await_human（停下等人）。"
                       "**同一個回報也要交引擎改善建議**（improvements）——"
                       "這是結案後的引擎復盤，依據是退件紀錄。",
        "for_roles": ["PLANNER"],
        "allowed": {"task_id", "expected_version", "action", "reason",
                    "new_works", "improvements", "session_uuid"},
        "required": ["task_id", "expected_version", "action", "session_uuid"],
        "inputSchema": _schema({
            "task_id": _STR,
            "expected_version": _INT,
            "action": {"type": "string", "enum": list(workflow.PR_REVIEW_ACTIONS)},
            "reason": _STR,
            "new_works": {"type": "array",
                          "description": "action=add_work 時要補的工單："
                                         "[{title, requirement_id, content, expected_outputs}]"},
            "improvements": {"type": "array",
                             "description": "引擎改善建議（復盤產出）："
                                            "[{target, suggestion, reason}]；沒有就交 []"},
            "session_uuid": _STR,
        }, ["task_id", "expected_version", "action", "session_uuid"]),
    },
    {
        "name": "fbflow_work_create",
        "title": "建立工單",
        "description": "建立一張工單（狀態 backlog）。需求必須已由人類確認。"
                       "`skill`＝搭配的專案 SKILL（spec：PLANNER 填寫）。",
        "for_roles": ["PLANNER"],
        "allowed": {"title", "requirement_id", "content", "expected_outputs",
                    "milestone_id", "skill"},
        "required": ["title", "requirement_id", "content"],
        "inputSchema": _schema({
            "title": _STR,
            "requirement_id": _STR,
            "content": _STR,
            "expected_outputs": _ARR,
            "milestone_id": _STR,
            "skill": {"type": "string",
                      "description": "搭配的專案 SKILL（這張單用哪一套方法做）"},
        }, ["title", "requirement_id", "content"]),
    },
    {
        "name": "fbflow_work_supersede",
        "title": "把已發出的工單標成已取代（需求變了 → 開新工單）",
        "description": (
            "**需求或做法變了，而那張工單已經發出去** → **不要改那張工單**，"
            "照新需求開新工單（fbflow_work_create），再把舊工單標成已取代。"
            "這裡只做標記與停工：舊工單的**內容與狀態不動**、"
            "開著的 task 會被取消、之後不再被派工、交件也會被擋。"
            "取代者必須是剛開的新工單（不能已結案、不能自己已被取代）。"),
        "for_roles": ["PLANNER"],
        "allowed": {"work_id", "replacement_work_ids", "reason", "session_uuid"},
        "required": ["work_id", "replacement_work_ids", "reason", "session_uuid"],
        "inputSchema": _schema({
            "work_id": _STR,
            "replacement_work_ids": _ARR,
            "reason": _STR,
            "session_uuid": _STR,
        }, ["work_id", "replacement_work_ids", "reason", "session_uuid"]),
    },
    {
        "name": "fbflow_issue_create",
        "title": "發起 ISSUE",
        "description": "回報一個阻礙或需要裁決的問題。只處理「目前工單無法往前」"
                       "或「需要 PLANNER 裁決」，不承載一般聊天。",
        "for_roles": ["WORKER", "THINKER"],
        "allowed": {"work_id", "trigger_kind", "problem", "location", "tried", "impact",
                    "question", "completion_criteria"},
        "required": ["work_id", "trigger_kind", "problem", "location", "tried", "impact",
                     "question", "completion_criteria"],
        "inputSchema": _schema({
            "work_id": _STR,
            "trigger_kind": {"type": "string",
                             "enum": ["work_blocked", "needs_decision"]},
            "problem": _STR,
            "location": _STR,
            "tried": _STR,
            "impact": _STR,
            "question": _STR,
            "completion_criteria": _STR,
        }, ["work_id", "trigger_kind", "problem", "location", "tried", "impact",
            "question", "completion_criteria"]),
    },
    {
        "name": "fbflow_milestone_create",
        "title": "建立里程碑",
        "description": "建立一段任務。outcome 必須在派第一張單前寫定。",
        "for_roles": ["PLANNER"],
        "allowed": {"seq", "name", "outcome"},
        "required": ["seq", "name", "outcome"],
        "inputSchema": _schema({
            "seq": _INT,
            "name": _STR,
            "outcome": _STR,
        }, ["seq", "name", "outcome"]),
    },
    {
        "name": "fbflow_status",
        "title": "看自己的狀態",
        "description": "自己的 tasks、待處理缺口、目前里程碑。",
        "for_roles": None,
        "allowed": set(),
        "required": [],
        "inputSchema": _schema({}, []),
    },
]


# ---------------------------------------------------------------- 伺服器

class MCPServer:
    """stdio MCP 伺服器。**多專案**：不綁死任何一個專案。"""

    def __init__(self, project_root: str = "", endpoint: str = "",
                 interval_seconds: int = 30, binary: str = "opencode",
                 resident: bool = True):
        self.default_root = os.path.abspath(os.path.expanduser(project_root)) if project_root else ""
        self.endpoint = endpoint
        self.interval = interval_seconds
        self.binary = binary
        self.resident_enabled = resident
        self._coordinators: Dict[str, Any] = {}
        self._residents: Dict[str, Any] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 專案

    def coordinator_for(self, root: str) -> Optional[Any]:
        """取得（並記住）某個專案的協調器。

        **沒有初始化過的目錄不是專案**——不會自動建立資料庫。
        安裝 MCP 不應該在某個目錄留下正本。
        """
        root = os.path.abspath(os.path.expanduser(root))
        with self._lock:
            if root in self._coordinators:
                return self._coordinators[root]
            if paths.is_under_mcp_root(root):
                # **安裝目錄不存放專案資料**——不拿它當專案根。
                return None
            if not os.path.isfile(store.db_path(root)):
                return None
            coordinator = engine.Coordinator(root, endpoint=self.endpoint, binary=self.binary)
            self._coordinators[root] = coordinator
            if self.resident_enabled:
                from . import coordinator as coordinator_module
                resident = coordinator_module.Resident(
                    coordinator, interval_seconds=self.interval)
                if resident.start():
                    self._residents[root] = resident
            return coordinator

    def _registry_projects(self) -> List[Dict[str, Any]]:
        """**永遠是空的**——MCP 根目錄不記任何專案（安裝狀態必須與專案無關）。"""
        return []

    def _project_of_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """不記專案 → 這個 session 屬於哪個專案，由 **host 綁定**決定。"""
        return None

    def resolve_project(self, project_code: str = "",
                        session_id: str = "") -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """決定這次呼叫屬於哪個專案。回傳 (root, error)。

        **專案是 host 綁定的，session 選不了**（規格第 11 節）：
        1. `FBF_PROJECT_ROOT` 環境變數——**那個專案自己的 `opencode.json` 設定它**。
        2. **這個 session 自己呼叫過 `fbflow_project_join`**——它宣告
           「我現在就在這個專案裡工作」，所以當下這條連線立刻服務該專案。
        3. 啟動目錄（cwd），且它確實是個 FBF 專案。
        4. 都不行 → 明確報錯，**不猜、不自動建立**。
        """
        env_root = os.environ.get(ENV_PROJECT_ROOT, "").strip()
        if env_root:
            root = os.path.abspath(os.path.expanduser(env_root))
            if paths.is_under_mcp_root(root):
                # 綁到安裝目錄 = 沒綁專案。安裝目錄不存放專案資料。
                return None, fail(
                    CODE_PROJECT_UNRESOLVED,
                    "FBF_PROJECT_ROOT 指向引擎的安裝目錄（%s），"
                    "那裡不存放專案資料。請改成真正那個專案的目錄。" % root)
            return root, None

        if session_id:
            bound = project.session_root(session_id)
            if bound:
                if paths.is_under_mcp_root(bound):
                    return None, fail(
                        CODE_PROJECT_UNRESOLVED,
                        "這個 session 綁的專案指向引擎的安裝目錄（%s），"
                        "那裡不存放專案資料。請重新呼叫 fbflow_project_join。" % bound)
                if not os.path.isdir(bound):
                    return None, fail(
                        CODE_PROJECT_UNRESOLVED,
                        "這個 session 綁的專案目錄不見了：%s"
                        "（請重新呼叫 fbflow_project_join 綁到現存的專案）" % bound)
                return bound, None

        cwd = os.path.abspath(os.getcwd())
        if not paths.is_under_mcp_root(cwd) and os.path.isfile(store.db_path(cwd)):
            return cwd, None
        return None, fail(
            CODE_PROJECT_UNRESOLVED,
            "這個 MCP 連線沒有綁任何專案。**在這個專案的目錄下呼叫 "
            "`fbflow_project_join`** 就能把它加入 FBF ENGINE（會建立正本、"
            "三個角色席位，並把這個 MCP 寫進該專案的設定檔）。"
            "也可以讓該專案在自己的 opencode.json 裡加入這個 MCP 並設定 "
            "FBF_PROJECT_ROOT（用 `fbf.py join --project <該專案>` 可以自動寫好）。")

    # ------------------------------------------------------------ 身分

    def host_session_id(self, params: Dict[str, Any]) -> str:
        """OpenCode 帶進來的呼叫者 session id（host 綁定，不是自報的）。"""
        meta = params.get("_meta")
        if isinstance(meta, dict):
            value = meta.get(META_SESSION_KEY)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def credentials(self, root: str) -> Dict[str, Any]:
        path = os.path.join(root, CREDENTIALS_DIR, CREDENTIALS_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"sessions": {}}
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
            return {"sessions": {}}
        return data

    def _credential_roots(self, root: str = "") -> List[str]:
        """可能要查憑證表的專案根。

        **MCP 根目錄不記任何專案**，所以候選只有兩個：
        這個連線綁的專案（env），與啟動目錄。
        """
        roots: List[str] = []
        if root:
            roots.append(os.path.abspath(root))
        env_root = os.environ.get(ENV_PROJECT_ROOT, "").strip()
        if env_root:
            candidate = os.path.abspath(os.path.expanduser(env_root))
            if candidate not in roots:
                roots.append(candidate)
        cwd = os.path.abspath(os.getcwd())
        if cwd not in roots:
            roots.append(cwd)
        return roots

    def identity(self, params: Dict[str, Any], root: str = "") -> Dict[str, Any]:
        """這個連線是誰？

        1. OpenCode 的 `_meta` session id（**host 綁定，主證據**）。
        2. 宿主終端 handle（`ORCA_TERMINAL_HANDLE`，**host 綁定，主證據**）——
           host CLI 設給終端內程式的環境變數，模型改不了；值就是 roles 表裡的
           `session_id`（auto-enroll 綁的就是它）。
        3. 備援：`FBF_SESSION_TOKEN` 對到專案憑證表（也是 host 綁定）。
        4. 都沒有 → 寫入工具關閉。
        """
        session_id = self.host_session_id(params)
        if session_id:
            return {"ok": True, "session_id": session_id, "source": "host-meta"}
        handle = os.environ.get(ENV_TERMINAL_HANDLE, "").strip()
        if handle:
            return {"ok": True, "session_id": handle, "source": "host-terminal"}
        token = os.environ.get(ENV_SESSION_TOKEN, "").strip()
        if token:
            for candidate in self._credential_roots(root):
                record = self.credentials(candidate).get("sessions", {}).get(token)
                if isinstance(record, dict) and record.get("session_id"):
                    return {"ok": True, "session_id": str(record["session_id"]),
                            "source": "env-token", "project_root": candidate}
        return fail(CODE_SESSION_UNVERIFIED,
                    "無法證明這個連線所屬 session（沒有 host 綁定的 session id）")

    # ------------------------------------------------------------ 輸入檢查

    def _validate(self, tool: Dict[str, Any], args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """型別、欄位白名單、必填、大小。**不合格就不進 workflow。**"""
        if not isinstance(args, dict):
            return fail(CODE_INPUT_INVALID, "參數必須是物件")
        for key in args:
            if key not in tool["allowed"]:
                return fail(CODE_UNKNOWN_FIELD, "不認識的欄位：%s" % key,
                            allowed=sorted(tool["allowed"]))
        for key in tool["required"]:
            if key not in args or args[key] in (None, ""):
                return fail(CODE_CONTENT_MISSING, "缺欄位：%s" % key,
                            required=list(tool["required"]))
        try:
            size = len(json.dumps(args, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return fail(CODE_INPUT_INVALID, "參數無法序列化")
        if size > MAX_PAYLOAD_BYTES:
            return fail(CODE_INPUT_INVALID,
                        "參數太大（%d bytes，上限 %d）" % (size, MAX_PAYLOAD_BYTES))
        schema = tool["inputSchema"]
        for key, spec in schema.get("properties", {}).items():
            if key not in args:
                continue
            expected = spec.get("type")
            value = args[key]
            if expected == "string" and not isinstance(value, str):
                return fail(CODE_INPUT_INVALID, "%s 必須是字串" % key)
            if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                return fail(CODE_INPUT_INVALID, "%s 必須是整數" % key)
            if expected == "object" and not isinstance(value, dict):
                return fail(CODE_INPUT_INVALID, "%s 必須是物件" % key)
            if expected == "array" and not isinstance(value, list):
                return fail(CODE_INPUT_INVALID, "%s 必須是陣列" % key)
            if "enum" in spec and value not in spec["enum"]:
                return fail(CODE_INPUT_INVALID, "%s 只接受：%s" % (key, ", ".join(spec["enum"])))
        return None

    def _role_gate(self, tool: Dict[str, Any], role: str) -> Optional[Dict[str, Any]]:
        allowed = tool.get("for_roles")
        if not allowed:
            return None
        if role not in allowed:
            return fail(CODE_ROLE_DENIED, "這個工具只開放給：%s" % ", ".join(allowed),
                        your_role=role)
        return None

    # ------------------------------------------------------------ 工具實作

    def call_tool(self, name: str, args: Dict[str, Any],
                  params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return fail(CODE_NOT_FOUND, "沒有這個工具：%s" % name)

        problem = self._validate(tool, args)
        if problem:
            return problem

        identity = self.identity(params)
        if not identity.get("ok"):
            return fail(identity.get("code", CODE_SESSION_UNVERIFIED),
                        identity.get("message", "無法證明連線所屬 session"),
                        details=identity.get("details", {}))
        session_id = identity["session_id"]

        if name == "fbflow_project_join":
            # **加入專案要在解析專案之前跑**：這正是「還沒綁專案」時的入口。
            return self._tool_project_join(args, session_id)

        root, problem = self.resolve_project(str(args.get("project_code") or ""), session_id)
        if problem:
            return problem
        handler = getattr(self, "_tool_" + name[len("fbflow_"):], None)
        if handler is None:
            return fail(CODE_NOT_FOUND, "沒有這個工具：%s" % name)
        try:
            coordinator = self.coordinator_for(root)
            if coordinator is None:
                return fail(CODE_PROJECT_NOT_INITIALIZED,
                            "這個目錄還不是 FBF 專案（沒有 %s）：%s"
                            % (store.DB_FILENAME, root))

            # 與 resident tick 共用同一把鎖，涵蓋查詢、取回結果及交易。
            with coordinator.conn._fbf_lock:
                if name != "fbflow_register":
                    role = self._role_of(coordinator, session_id)
                    problem = self._role_gate(tool, role)
                    if problem:
                        return problem
                return handler(coordinator, session_id, args)
        except sqlite3.DatabaseError as exc:
            # **正本損毀** → 明確回報並給可行動作，不要變成 INTERNAL
            # （那是「程式壞了」，不是「你的正本壞了」）。
            # 暫時性的 DB 錯誤（例如 locked）仍走 INTERNAL。
            if store.is_corrupted(exc):
                return fail(CODE_PROJECT_CORRUPTED,
                            "專案正本損毀或無法讀取（%s）：%s。"
                            "請先從備份還原 %s，再繼續；引擎不會往壞掉的正本寫入。"
                            % (exc, root, store.DB_FILENAME))
            return fail(CODE_INTERNAL, "資料庫錯誤：%s" % exc)
        except Exception as exc:
            return fail(CODE_INTERNAL, "工具執行失敗：%s" % exc)

    def _role_of(self, coordinator: Any, session_id: str) -> str:
        row = coordinator.conn.execute(
            "SELECT role FROM roles WHERE session_id = ?", (session_id,)).fetchone()
        return row["role"] if row else ""

    # -- 加入專案 -------------------------------------------------------

    def _tool_project_join(self, args: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """**新專案加入這個引擎**——session 呼叫的第一個動作。

        這是唯一一個「還沒綁專案也能呼叫」的工具：它自己決定要加入哪個目錄，
        並把**這個 session** 綁到那個專案上（當下就能繼續用其他工具）。
        """
        cwd = os.path.abspath(os.getcwd())
        try:
            root = project.resolve_join_target(
                cwd, str(args.get("project_root") or ""))
            info = project.join_project(
                root, name=str(args.get("name") or ""),
                python_command=paths.resolve_python_command())
        except project.JoinError as exc:
            return fail(exc.code, exc.message)
        except Exception as exc:
            return fail(CODE_INTERNAL, "加入專案失敗：%s" % exc)

        project.bind_session(session_id, root)
        info["bound_session"] = session_id
        info["next"] = [
            "呼叫 fbflow_register(role=PLANNER|THINKER|WORKER, short_name=P1|T1|W1)"
            " 認領席位，取得你的私有提示詞（含專屬 UUID）。",
            "之後一律用 MCP 工具，不要用 CLI。",
            "這個專案的 %s 與 %s 已寫入帶 FBF_PROJECT_ROOT 的 MCP 設定；"
            "之後在這個目錄開的新 session 會自動綁到這個專案。"
            % (info.get("config") or "", info.get("claude_config") or ""),
        ]
        return ok(info)

    # -- 註冊 ---------------------------------------------------------

    def _tool_register(self, coordinator: Any, session_id: str,
                       args: Dict[str, Any]) -> Dict[str, Any]:
        """認領一個角色席位，換取自己的私有提示詞（含專屬 UUID）。"""
        project_code = store.get_setting(coordinator.conn, "project_code", "")

        bound = self.coordinator_bind(coordinator, str(args["role"]).upper(),
                                      str(args["short_name"]), session_id,
                                      str(args.get("agent") or "opencode"), project_code)
        if not bound.get("ok"):
            return fail(bound.get("code", CODE_ROLE_DENIED), bound.get("message", ""))

        uuid_value = prompts.issue_uuid(coordinator.conn, session_id)
        prompt = prompts.render_session_prompt(coordinator.conn, session_id, project_code)
        return ok({
            "role": bound["role"],
            "short_name": bound["short_name"],
            "session_id": session_id,
            "session_uuid": uuid_value,
            "project_code": project_code,
            "tools": [t["name"] for t in TOOLS],
            "prompt": prompt,
        })

    def coordinator_bind(self, coordinator: Any, role: str, short_name: str,
                         session_id: str, agent: str, project_code: str) -> Dict[str, Any]:
        """薄包裝，讓測試可以攔。"""
        return coordinator.bind_role(role, short_name, session_id,
                                      agent=agent, project_code=project_code)

    # -- 讀取 ---------------------------------------------------------

    def _tool_read(self, coordinator: Any, session_id: str,
                   args: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(args["target_kind"])
        target_id = str(args["target_id"])
        conn = coordinator.conn
        if kind == "work":
            work = workflow.load_work(conn, target_id)
            if work is None:
                return fail(CODE_NOT_FOUND, "找不到工單 %s" % target_id)
            checks = workflow.current_checks(conn, target_id)
            results = workflow.results_for(conn, target_id)
            solutions = workflow.current_solutions(conn, target_id)
            handoff = conn.execute(
                "SELECT * FROM handoffs WHERE work_id = ? ORDER BY id DESC LIMIT 1",
                (target_id,)).fetchone()
            return ok({
                "kind": "work",
                "work": work,
                "version": work["version"],
                "state": work["state"],
                # **這一棒要交的欄位**——取消工單檔案層後，這是角色的唯一來源。
                "contract": workflow.stage_contract(work["state"]),
                "last_rejection": workflow.last_rejection(conn, "work", target_id),
                "checks": checks,
                "results": results,
                "solutions": solutions,
                "handoff": json.loads(handoff["payload"]) if handoff else {},
                "handoff_from_state": handoff["from_state"] if handoff else "",
                "handoff_to_state": handoff["to_state"] if handoff else "",
            })
        if kind == "issue":
            issue = workflow.load_issue(conn, target_id)
            if issue is None:
                return fail(CODE_NOT_FOUND, "找不到 ISSUE %s" % target_id)
            return ok({"kind": "issue", "issue": issue, "version": issue["version"],
                       "state": issue["state"],
                       "contract": workflow.issue_stage_contract(issue["state"]),
                       "last_rejection": workflow.last_rejection(conn, "issue", target_id)})
        if kind == "requirement":
            requirement = conn.execute("SELECT * FROM requirements WHERE id = ?",
                                       (target_id,)).fetchone()
            if requirement is None:
                return fail(CODE_NOT_FOUND, "找不到需求 %s" % target_id)
            return ok({"kind": "requirement",
                       "requirement": dict(requirement),
                       "confirmed": bool(requirement["confirmed_at"])})
        milestone = conn.execute("SELECT * FROM milestones WHERE id = ?",
                                 (target_id,)).fetchone()
        if milestone is None:
            return fail(CODE_NOT_FOUND, "找不到里程碑 %s" % target_id)
        return ok({"kind": "milestone", "milestone": dict(milestone)})

    # -- 回報 ---------------------------------------------------------

    def _tool_report_work(self, coordinator: Any, session_id: str,
                          args: Dict[str, Any]) -> Dict[str, Any]:
        conn = coordinator.conn
        if not prompts.check_uuid(conn, session_id, str(args.get("session_uuid") or "")):
            return fail(CODE_SESSION_UNVERIFIED,
                        "UUID 不符或沒帶。這個 session 在註冊時拿到一組專屬 UUID，"
                        "每次寫入都要帶上它（只有你知道）。")
        task = conn.execute("SELECT * FROM tasks WHERE id = ?",
                            (str(args["task_id"]),)).fetchone()
        if task is None:
            return fail(CODE_TASK_STALE, "找不到這個 task")
        if task["assigned_session"] != session_id:
            return fail(CODE_ROLE_DENIED, "這個 task 不是派給你的")
        work = workflow.load_work(conn, str(args["work_id"]))
        if work is None:
            return fail(CODE_NOT_FOUND, "找不到工單 %s" % args["work_id"])
        if work.get("superseded_by"):
            # **這張單已經作廢**——需求或做法變了，PLANNER 照新需求開了新工單。
            # 收下這筆交件只會讓正本記錄一件沒人要做的事。
            return fail(CODE_WORK_SUPERSEDED,
                        "工單 %s 已被新工單取代（%s）：%s。"
                        "改需求要開新工單，不要在這張單上繼續交件。"
                        % (args["work_id"], work.get("superseded_by") or "",
                           work.get("supersede_reason") or ""))
        if int(work["version"]) != int(args["expected_version"]):
            return fail(CODE_TASK_STALE, "版本不符（正本是 %d，你送的是 %d）"
                        % (int(work["version"]), int(args["expected_version"])),
                        current_version=int(work["version"]))
        result = coordinator.submit_work(
            str(args["task_id"]), str(args["work_id"]), int(args["expected_version"]),
            session_id, str(args.get("stage") or ""), dict(args["payload"]),
            str(args["message_id"]))
        if result.get("status") == engine.SUB_UNVERIFIED:
            return fail(CODE_RECEIPT_PENDING, "這筆提交正在等平台回執。結束本輪回覆，由 MCP 核對並派工，禁止 sleep、輪詢或重交",
                        submission_id=result.get("submission_id"))
        return ok({"submission_id": result["submission_id"], "status": result["status"],
                   "idempotent": result.get("idempotent", False),
                   "note": "已收到。交件後結束本輪回覆，等待 MCP 傳入下一棒提示詞。禁止 sleep 或輪詢狀態。"})

    def _tool_report_issue(self, coordinator: Any, session_id: str,
                           args: Dict[str, Any]) -> Dict[str, Any]:
        conn = coordinator.conn
        if not prompts.check_uuid(conn, session_id, str(args.get("session_uuid") or "")):
            return fail(CODE_SESSION_UNVERIFIED, "UUID 不符")
        task = conn.execute("SELECT * FROM tasks WHERE id = ?",
                            (str(args["task_id"]),)).fetchone()
        if task is None:
            return fail(CODE_TASK_STALE, "找不到這個 task")
        if task["assigned_session"] != session_id:
            return fail(CODE_ROLE_DENIED, "這個 task 不是派給你的")
        issue = workflow.load_issue(conn, str(args["issue_id"]))
        if issue is None:
            return fail(CODE_NOT_FOUND, "找不到 ISSUE %s" % args["issue_id"])
        if int(issue["version"]) != int(args["expected_version"]):
            return fail(CODE_TASK_STALE, "版本不符（正本是 %d，你送的是 %d）"
                        % (int(issue["version"]), int(args["expected_version"])),
                        current_version=int(issue["version"]))
        result = coordinator.submit_issue(
            str(args["task_id"]), str(args["issue_id"]), int(args["expected_version"]),
            session_id, str(args.get("stage") or ""), dict(args["payload"]),
            str(args["message_id"]))
        return ok({"submission_id": result["submission_id"], "status": result["status"],
                   "idempotent": result.get("idempotent", False),
                   "note": "已收到。交件後結束本輪回覆，等待 MCP 傳入下一棒提示詞。禁止 sleep 或輪詢狀態。"})

    def _tool_report_milestone(self, coordinator: Any, session_id: str,
                               args: Dict[str, Any]) -> Dict[str, Any]:
        conn = coordinator.conn
        if not prompts.check_uuid(conn, session_id, str(args.get("session_uuid") or "")):
            return fail(CODE_SESSION_UNVERIFIED, "UUID 不符")
        result = coordinator.apply_milestone_review(
            str(args["task_id"]), str(args["milestone_id"]),
            int(args["expected_version"]), session_id, str(args["action"]),
            str(args.get("reason") or ""), list(args.get("new_works") or []))
        return result

    def _tool_report_project_review(self, coordinator: Any, session_id: str,
                                    args: Dict[str, Any]) -> Dict[str, Any]:
        conn = coordinator.conn
        if not prompts.check_uuid(conn, session_id, str(args.get("session_uuid") or "")):
            return fail(CODE_SESSION_UNVERIFIED, "UUID 不符")
        result = coordinator.apply_project_review(
            str(args["task_id"]), int(args["expected_version"]), session_id,
            str(args["action"]), str(args.get("reason") or ""),
            list(args.get("new_works") or []),
            list(args.get("improvements") or []))
        return result

    # -- 建立 ---------------------------------------------------------

    def _tool_work_create(self, coordinator: Any, session_id: str,
                          args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            work_id = coordinator.create_work(
                str(args["title"]), str(args["requirement_id"]), str(args["content"]),
                list(args.get("expected_outputs") or []),
                str(args.get("milestone_id") or ""),
                skill=str(args.get("skill") or ""))
        except ValueError as exc:
            # 需求未確認、里程碑必須指定這類**可判定**的拒絕 → 回穩定代碼，
            # 不要變成 INTERNAL（那是「程式壞了」，不是「你還不能做這件事」）。
            return fail(CODE_WORK_REJECTED, str(exc))
        work = workflow.load_work(coordinator.conn, work_id)
        return ok({"work_id": work_id, "state": work["state"] if work else "",
                   "version": work["version"] if work else 1})

    def _tool_work_supersede(self, coordinator: Any, session_id: str,
                             args: Dict[str, Any]) -> Dict[str, Any]:
        """**把已發出的工單標成已取代**（需求變了 → 開新工單）。"""
        conn = coordinator.conn
        if not prompts.check_uuid(conn, session_id, str(args.get("session_uuid") or "")):
            return fail(CODE_SESSION_UNVERIFIED,
                        "UUID 不符或沒帶。這個 session 在註冊時拿到一組專屬 UUID，"
                        "每次寫入都要帶上它（只有你知道）。")
        replacements = args.get("replacement_work_ids")
        if not isinstance(replacements, list) or not replacements:
            return fail(CODE_CONTENT_MISSING,
                        "replacement_work_ids 至少要一張新工單"
                        "（先 fbflow_work_create 開新單，再回來標記舊單）")
        try:
            result = coordinator.supersede_work(
                str(args["work_id"]), [str(x) for x in replacements],
                str(args.get("reason") or ""), session_id=session_id)
        except ValueError as exc:
            return fail(CODE_WORK_SUPERSEDE_DENIED, str(exc))
        result["note"] = ("舊工單的內容與狀態沒有被改動，只被標記為已取代並停止派工。"
                          "接下來把新工單走完即可。")
        return ok(result)

    def _tool_issue_create(self, coordinator: Any, session_id: str,
                           args: Dict[str, Any]) -> Dict[str, Any]:
        role = self._role_of(coordinator, session_id)
        issue_id = coordinator.create_issue(
            str(args["work_id"]), session_id, role,
            str(args["trigger_kind"]), str(args["problem"]), str(args["location"]),
            str(args["tried"]), str(args["impact"]), str(args["question"]),
            str(args.get("completion_criteria") or ""))
        issue = workflow.load_issue(coordinator.conn, issue_id)
        return ok({"issue_id": issue_id, "state": issue["state"] if issue else ""})

    def _tool_milestone_create(self, coordinator: Any, session_id: str,
                               args: Dict[str, Any]) -> Dict[str, Any]:
        milestone_id = coordinator.create_milestone(
            int(args["seq"]), str(args["name"]), str(args["outcome"]))
        return ok({"milestone_id": milestone_id})

    # -- 狀態 ---------------------------------------------------------

    def _tool_status(self, coordinator: Any, session_id: str,
                     args: Dict[str, Any]) -> Dict[str, Any]:
        conn = coordinator.conn
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE assigned_session = ? AND status = 'open'"
            " ORDER BY created_at", (session_id,)).fetchall()
        current = conn.execute(
            "SELECT * FROM milestones WHERE state <> 'done' ORDER BY seq LIMIT 1").fetchone()
        awaiting = coordinator.awaiting_list()
        # 已確認需求清單：PLANNER 的 decompose 需要它取得 requirement_id。
        # 沒有這個就得到「我無法確定哪些需求被確認」然後回頭問人（實測踩過）。
        requirements = conn.execute(
            "SELECT id, text, success_criterion FROM requirements"
            " WHERE confirmed_at IS NOT NULL AND confirmed_at <> ''"
            " ORDER BY id").fetchall()
        works = conn.execute(
            "SELECT id, title, state, version FROM works WHERE state <> 'closed'"
            " AND COALESCE(superseded_by, '') = '' ORDER BY id").fetchall()
        # **被取代的工單也要看得見**——不然人會以為它還在跑。
        superseded = conn.execute(
            "SELECT id, title, state, superseded_by, supersede_reason FROM works"
            " WHERE COALESCE(superseded_by, '') <> '' ORDER BY id").fetchall()
        return ok({
            "session_id": session_id,
            "role": self._role_of(coordinator, session_id),
            "tasks": [dict(t) for t in tasks],
            "confirmed_requirements": [dict(r) for r in requirements],
            "open_works": [dict(w) for w in works],
            "superseded_works": [dict(w) for w in superseded],
            "current_milestone": dict(current) if current else None,
            "awaiting": awaiting,
            # **引擎的聲音**：派不出去、沒有人接手的單子。
            # 沒有這一段的話，「沒人接手」只存在於引擎記憶體裡，
            # 人問引擎也問不到 → 單子靜靜卡著，沒有人知道要開 session
            # （live 實測：2026-11-23 00:43~10:41 零訊息、10 小時零提交）。
            "unassigned": coordinator.unassigned_targets(),
            # **巡檢結果**：哪張單有問題（停滯、派不到、等人太久…）。
            "health": coordinator.health_check(),
            # **上下文收合紀錄**：哪個 session 什麼時候 compact 過、
            # 引擎又叫它收合過幾次（來源：detected＝宿主紀錄、requested＝引擎要求）。
            "compacts": compact.ledger_snapshot(conn),
            # **目前用哪一種收合方式**（compact＝壓縮／reset＝清空對話）。
            "compact_mode": compact.mode_of(conn),
        })

    # ------------------------------------------------------------ JSON-RPC

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """處理一則 JSON-RPC 訊息。通知（沒有 id）回 None。"""
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            result = self._initialize(params)
        elif method == "notifications/initialized":
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [
                {"name": t["name"], "title": t.get("title", ""),
                 "description": t["description"],
                 "inputSchema": t["inputSchema"]} for t in TOOLS]}
        elif method == "tools/call":
            result = self._tools_call(params)
        else:
            if message_id is None:
                return None
            return {"jsonrpc": "2.0", "id": message_id,
                    "error": {"code": -32601, "message": "method not found: %s" % method}}

        if message_id is None:
            return None
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """`initialize` 沒有 session 上下文（OpenCode 只在 tools/call 帶 `_meta`），
        所以這裡回**通用**說明；屬於這個 session 的私有提示詞由
        `fbflow_register` 回傳（那才是「逐 session 渲染」的地方）。"""
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "FBF ENGINE（MCP）：多 session 接力完成一張工單，狀態只由協調器推進。\n"
                "第一步：確認這個專案已加入——呼叫 fbflow_project_join"
                "（建立正本與三個角色席位，並把這個 session 綁到這個專案；"
                "已經加入過就只補缺的部分，不會洗掉既有資料）。\n"
                "第二步：呼叫 fbflow_register（帶 role、short_name）"
                "綁定角色並取得你的私有提示詞（含專屬 UUID）。\n"
                "專案由 host 綁定，不要在參數裡指定專案。\n"
                "之後一律用 MCP 工具，不要用 CLI。\n"
                "寫入工具要帶 session_uuid（你註冊時拿到的，只有你知道）。\n"
                "規則正本在 %s/SKILL.md 與 %s/spec/FBF ENGINE工作流.md——動工前先讀。"
                % (paths.mcp_root(), paths.mcp_root())
            ),
        }

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = str(params.get("name") or "")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        envelope = self.call_tool(name, args, params)
        text = json.dumps(envelope, ensure_ascii=False, indent=2)
        return {"content": [{"type": "text", "text": text}],
                "isError": not envelope.get("ok", False)}

    # ------------------------------------------------------------ 迴圈

    def serve(self, stdin=None, stdout=None) -> None:
        """stdio 迴圈（newline-delimited JSON-RPC）。

        **stdio 傳輸必須是 UTF-8**（MCP 契約）。Windows 的預設主控台編碼
        不是 UTF-8（cp950／cp437…），不強制就會送出**主機端解不掉的位元組**——
        回應裡的中文直接變成亂碼，握手失敗。所以這裡主動把三個標準流設成
        UTF-8，不受環境變數（`PYTHONIOENCODING`）或主控台字碼頁影響。
        """
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        _force_utf8(stdin, errors="replace")
        _force_utf8(stdout, errors="strict")
        _force_utf8(sys.stderr, errors="replace")
        # MCP 程序開始服務就啟動排程，無須等待 session 呼叫工具。
        root = os.environ.get(ENV_PROJECT_ROOT, "").strip() or self.default_root
        if not root:
            root, _ = self.resolve_project()
        if root:
            self.coordinator_for(root)
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            response = self.handle(message)
            if response is None:
                continue
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()

    def close(self) -> None:
        for root in list(self._residents):
            try:
                self._residents[root].stop(close=False)
            except Exception:
                pass
        for root in list(self._coordinators):
            try:
                self._coordinators[root].close()
            except Exception:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    """進入點：`python -m fbf_engine.mcp_server`（不帶參數也可跑）。

    安裝器寫進設定的啟動命令用的是**當時跑安裝器的那個 Python**——
    不要假設它叫 `python3`。
    """
    import argparse
    parser = argparse.ArgumentParser(description="FBF ENGINE MCP server（stdio）")
    parser.add_argument("--project", default="",
                        help="綁定單一專案（不給就是多專案模式）")
    parser.add_argument("--endpoint", default="", help="OpenCode server endpoint")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--no-resident", action="store_true",
                        help="不啟動常駐協調器（只當 MCP 介面）")
    args = parser.parse_args(argv)

    server = MCPServer(args.project, endpoint=args.endpoint,
                       interval_seconds=args.interval,
                       resident=not args.no_resident)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
