"""FBF ENGINE 的**流程層**：狀態機、交件契約、守衛、總判。

這一層是**規則的唯一一份宣告**（規格第 3 節、第 5 節）。
提示詞從這裡渲染（`prompts.py`），程式從這裡執行守衛，
**任何「提示詞說一套、程式驗另一套」的地方都是 bug**。

依據：`doc/spec-fbflow-v2-first-principles.md`
- 第 5 節：工單流程與交件契約
- 第 6 節：協調器、派工與重啟（計數器上限）
- 第 8 節：ISSUE、里程碑與人類出口

**這一層不知道 agent 怎麼連、也不知道怎麼派工。** 那是 `engine.py` 的事。
"""
from __future__ import annotations

SESSION_IDLE_RULE = "交件收到確認（含 RECEIPT-PENDING）或目前沒有任務時，立即結束本輪回覆。禁止用 sleep、定時工具、背景迴圈或反覆呼叫 fbflow_status／fbflow_read 等待派工。由 MCP 定期檢查，透過 CLI 向本 session 傳送下一張工單或 ISSUE 的提示詞。"

import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from . import store

# ---------------------------------------------------------------- 狀態

#: 工單九狀態。順序即流程順序；`rework_design`／`rework_build` 是退回支線。
BACKLOG = "backlog"
DESIGN = "design"
ACCEPTANCE_DESIGN = "acceptance_design"
BUILD = "build"
VERIFY = "verify"
ADJUDICATE = "adjudicate"
REWORK_DESIGN = "rework_design"
REWORK_ACCEPTANCE_DESIGN = "rework_acceptance_design"
REWORK_BUILD = "rework_build"
CLOSED = "closed"

#: 專案啟動前的需求釐清任務階段（**不是工單九狀態之一**——
#: 與 `milestone_review` 同型：有 task、無 work）。人確認需求後就關掉。
STAGE_GRILL = "grill"

#: 三段啟動的第 3 段：人類同意後、生成工單前，提示 PLANNER 分解並建工單。
#: 也是 task-only（不在九狀態）；第一張工單一建出來就收掉。
STAGE_DECOMPOSE = "decompose"

#: 里程碑狀態
MS_OPEN = "open"
MS_REVIEWING = "reviewing"
MS_DONE = "done"

#: ISSUE 五狀態
ISSUE_RAISED = "raised"
ISSUE_DESIGNING = "designing"
ISSUE_HANDLING = "handling"
ISSUE_BUILDING = "building"
ISSUE_ADJUDICATING = "adjudicating"
ISSUE_CLOSED = "closed"

#: 驗收結果狀態
PASS = "PASS"
FAIL = "FAIL"
PARTIAL = "PARTIAL"
NOT_RUN = "NOT_RUN"
RESULT_STATUSES = (PASS, FAIL, PARTIAL, NOT_RUN)

#: 驗收方式的執行種類：`test` 必須真的跑案例；`review` 是文件／人工檢視。
EXEC_TEST = "test"
EXEC_REVIEW = "review"
EXEC_KINDS = (EXEC_TEST, EXEC_REVIEW)

#: 退回類型（adjudicate 階段 PLANNER 填）。
#: **不可與階段常數同名**：`REWORK_BUILD` 已是階段 `rework_build`，
#: 這裡若再用同名常數，`WORK_STATES` 的鍵會被蓋成中文（實測 bug）。
REWORK_TYPE_BUILD = "施工錯"
REWORK_TYPE_ACCEPTANCE = "驗收錯"
REWORK_TYPE_SOLUTION = "解法錯"
REWORK_TYPES = (REWORK_TYPE_BUILD, REWORK_TYPE_ACCEPTANCE, REWORK_TYPE_SOLUTION)

#: 英文別名 → 正本中文值。**派工提示與守衛都要能接受**——
#: 實測：提示的「標準」欄位寫英文（build／acceptance／solution）、
#: 「範例」也寫 build，角色照著填就會被守衛擋（7 次退件）。
#: 兩邊都要收，英文填法正規化成中文再存。
REWORK_TYPE_ALIASES = {
    "build": REWORK_TYPE_BUILD,
    "acceptance": REWORK_TYPE_ACCEPTANCE,
    "solution": REWORK_TYPE_SOLUTION,
}

#: 重新檢討的診斷（重工達上限後必填）
DIAGNOSES = ("work", "acceptance", "solution", "scope", "resource")

#: 重新檢討的處置
REMEDY_HALT = "halt"
REMEDY_REVISE = "revise"

#: 里程碑檢視的三個選項（規格第 8 節：PLANNER **只可**選這些）
MS_CONTINUE = "continue"
MS_ADD_WORK = "add_work"
MS_AWAIT_HUMAN = "await_human"
MS_REVIEW_ACTIONS = (MS_CONTINUE, MS_ADD_WORK, MS_AWAIT_HUMAN)

#: **專案結案檢視**的階段名（task-only：有 task、沒有 work）。
#:
#: **與里程碑無關**（使用者澄清 2026-11-24）：
#: - `milestone`＝防止長施工流程在過程中偏移方向的**分段**機制；
#: - 專案結案＝**專案層級**「任務目標到底有沒有達成」的判定。
#:
#: 所以專案結案**不得依賴 `milestones` 表有資料**——專案可能一個里程碑都沒有。
STAGE_PROJECT_REVIEW = "project_review"

#: 專案結案檢視的三個動作（PLANNER 只可選這些）。
#:
#: - `complete`   —— 達成 → 引擎跑雙向追溯，通過就標專案結案
#: - `add_work`   —— 未達成 → 針對未完成的部分開新工單
#: - `await_human`—— 停止派工，等人裁決
PR_COMPLETE = "complete"
PR_ADD_WORK = "add_work"
PR_AWAIT_HUMAN = "await_human"
PR_REVIEW_ACTIONS = (PR_COMPLETE, PR_ADD_WORK, PR_AWAIT_HUMAN)

#: 專案結案檢視的狀態。
PR_REVIEWING = "reviewing"
PR_CLOSED = "closed"
PR_AWAITING = "awaiting_human"
#: 補了工單 → 這一輪結束，等新工單完成後**再觸發一次**檢視。
PR_REOPENED = "reopened"


#: 缺口代碼。全部是小寫、點分隔，與規格第 5 節的守衛一一對應。
GAP_REQUIREMENT_UNCONFIRMED = "requirement.unconfirmed"
GAP_REQUIREMENT_UNKNOWN = "requirement.unknown"
GAP_CHECK_REQUIREMENT_UNKNOWN = "check.requirement_unknown"
GAP_SOLUTIONS_COUNT = "solutions.count"
GAP_SOLUTION_TOO_SIMILAR = "solution.too_similar"
GAP_SOLUTION_FIELD_MISSING = "solution.field_missing"
GAP_SELECTION_MISSING = "selection.missing"
GAP_SELECTION_UNKNOWN_SOLUTION = "selection.unknown_solution"
GAP_CHECKS_MISSING = "checks.missing"
#: 驗收棒開工前，答案卷（checks 表）是空的。
#: **這不是驗收者該欄位沒填**——是「定驗收方式」那一關的欄位沒人填過
#: （重工路 `rework_build → verify` 會跳過 acceptance_design，答案卷從來沒建出來）。
#: 引擎把工單退回 acceptance_design 去補欄，不讓它停在 verify 遞一張永遠填不對的單。
GAP_ACCEPTANCE_NOT_DESIGNED = "acceptance.not_designed"
GAP_CHECK_FIELD_MISSING = "check.field_missing"
GAP_CHECK_THRESHOLD_LOWERED = "check.threshold_lowered"
GAP_CHECK_REQUIRED_DOWNGRADED = "check.required_downgraded"
GAP_CHECK_RESULT_DELETED = "check.result_deleted"
GAP_EVIDENCE_MISSING = "evidence.missing"
GAP_EVIDENCE_HASH_MISMATCH = "evidence.hash_mismatch"
GAP_CASES_ZERO = "cases_ran.zero"
GAP_RESULT_MISSING = "result.missing"
GAP_RESULT_STATUS_INVALID = "result.status_invalid"
GAP_RESULT_EVIDENCE_MISSING = "result.evidence_missing"
GAP_SELF_VERIFY = "verify.self_conflict"
GAP_HANDOFF_MISSING = "handoff.missing"
GAP_DESIGN_NOTE_MISSING = "design_note.missing"
GAP_SELFCHECK_MISSING = "selfcheck.missing"
GAP_UNFINISHED_INVALID = "unfinished.invalid"
GAP_OUTPUTS_MISSING = "outputs.missing"
GAP_REWORK_LIMIT = "rework.limit_reached"
GAP_REVISION_LIMIT = "revision.limit_reached"
GAP_REVIEW_REQUIRED = "review.required"
GAP_REVIEW_DIAGNOSIS_INVALID = "review.diagnosis_invalid"
GAP_REVIEW_REMEDY_INVALID = "review.remedy_invalid"
GAP_REVIEW_REVISE_NOT_ALLOWED = "review.revise_not_allowed"
GAP_ACTION_MISSING = "action.missing"
GAP_ACTION_INVALID = "action.invalid"
#: `action` 合法但**它要求的附屬欄位**不合法（例：rework_type 填了不存在的值）。
#: 與 `action.invalid` 分開——混用會讓角色以為自己的 action 選錯了（實測退件 7 次）。
GAP_REWORK_TYPE_INVALID = "rework_type.invalid"
GAP_ISSUE_RETURN_LIMIT = "issue.return_limit"
GAP_ISSUE_FIELD_MISSING = "issue.field_missing"
GAP_APPLY_REJECTED = "apply.rejected"

#: 回執／context 核對缺口代碼。與 `receipts` 模組的理由代碼**同字串**——
#: 退回時 `GAP_HINTS` 靠這些代碼把「缺哪一項證據、怎麼補齊」講給角色聽。
GAP_RECEIPT_UNVERIFIED = "receipt.unverified"
GAP_RECEIPT_QUERY_FAILED = "receipt.query_failed"
GAP_RECEIPT_MESSAGE_FALLBACK = "receipt.message_fallback"
GAP_RECEIPT_MESSAGE_REUSED = "receipt.message_reused"
GAP_RECEIPT_MESSAGE_ID_MISSING = "receipt.message_id_missing"
GAP_CONTEXT_QUERY_FAILED = "context.query_failed"
GAP_WORK_SUPERSEDED = "work.superseded"


#: 缺口代碼 → **人／角色看得懂的說明**（含缺哪個欄位與可行下一步）。
#:
#: 規格第 5 節：「所有硬拒絕回傳**穩定缺口代碼、缺哪個欄位與可行下一步**」——
#: 光有代碼不夠，角色看不懂就改不對。
GAP_HINTS: Dict[str, str] = {
    GAP_WORK_SUPERSEDED: (
        "這張工單已被新工單取代（需求或做法變了）——不要在它上面繼續施工或交件；"
        "照新需求開新工單就好。"),
    GAP_REQUIREMENT_UNCONFIRMED: "需求還沒有人確認——要先由人確認需求。",
    GAP_REQUIREMENT_UNKNOWN: "指向的需求不存在。",
    GAP_CHECK_REQUIREMENT_UNKNOWN: "這條 check 指到的需求不存在；每條 check 必須指向一條已確認需求。",
    GAP_SOLUTIONS_COUNT: "solutions 至少要 2 案。",
    GAP_SOLUTION_TOO_SIMILAR: "兩案實質相同（成本與修改範圍都一樣）；至少要有一處不同。",
    GAP_SOLUTION_FIELD_MISSING: "有案子缺欄位；每案都要有 approach、cost、change_scope、effort、risk、verifiable_by。",
    GAP_SELECTION_MISSING: "缺 selection（選了哪一案、為什麼、為什麼不要其他案）。",
    GAP_SELECTION_UNKNOWN_SOLUTION: "selection.solution_id 指向不存在的方案。",
    GAP_CHECKS_MISSING: "缺 checks（至少一條）。",
    GAP_ACCEPTANCE_NOT_DESIGNED: (
        "這張工單的答案卷（checks 表）是空的：「定驗收方式」那一關的欄位還沒人填過"
        "（重工路 rework_build→verify 會跳過 acceptance_design，答案卷就從來沒建出來）。"
        "引擎已把工單退回 acceptance_design——請以 PLANNER 身分補齊："
        "「選哪一案、為什麼」＋每條 check 的 id/requirement_id/method/scope/threshold"
        "/evidence/required/execution_kind。補完才進 verify；verify 不收自由格式的結果，"
        "結果的 id 必須就是 checks 表的題號（整張單不該有空白欄位）。"
    ),
    GAP_CHECK_FIELD_MISSING: "有 check 缺欄位；每條要有 id、requirement_id、method、scope、threshold、evidence、required、execution_kind。",
    GAP_CHECK_THRESHOLD_LOWERED: "門檻被降低了——不得降低門檻。",
    GAP_CHECK_REQUIRED_DOWNGRADED: "required 被改成 false——不得把必驗項目拿掉。",
    GAP_CHECK_RESULT_DELETED: "已存在的驗收結果被刪了。",
    GAP_EVIDENCE_MISSING: (
        "某一筆 outputs 的證據核不到。最常見：path 寫成**逃出專案根的絕對路徑**，"
        "或檔案在盤上確實不存在。path 請填**相對專案根**的路徑（例：fbf-e2e-checklist.md）；"
        "若填的是落在專案根內的絕對路徑，引擎會自動轉成相對路徑、不必重寫檔案。"
        "重交前請先確認檔案確實在專案目錄裡。"
    ),
    GAP_EVIDENCE_HASH_MISMATCH: "證據的大小或 SHA-256 與現檔不符。",
    GAP_CASES_ZERO: "cases_ran=0 卻宣稱已執行驗收；純文件／人工檢視請自報 execution_kind=review 並附審閱紀錄。",
    GAP_RESULT_MISSING: "某條 check 沒有結果。",
    GAP_RESULT_STATUS_INVALID: "結果狀態不合法（只能是 PASS／FAIL／PARTIAL／NOT_RUN）。",
    GAP_RESULT_EVIDENCE_MISSING: "結果沒有附證據。",
    GAP_SELF_VERIFY: "不可驗自己在本工單的施工。",
    GAP_HANDOFF_MISSING: "缺 handoff（交接檔八欄）。",
    GAP_DESIGN_NOTE_MISSING: "缺 design_note（設計說明）。",
    GAP_SELFCHECK_MISSING: "缺 selfcheck（自檢結果）。",
    GAP_UNFINISHED_INVALID: "unfinished 必須是清單（沒做完的事；空清單也要交）。",
    GAP_OUTPUTS_MISSING: "缺 outputs（產物指紋：路徑／大小／SHA-256）。",
    GAP_REWORK_LIMIT: "重工次數已達上限，要人裁決。",
    GAP_REVISION_LIMIT: "修訂次數已達上限，要人裁決。",
    GAP_REVIEW_REQUIRED: "要先做里程碑檢視。",
    GAP_REVIEW_DIAGNOSIS_INVALID: (
        "diagnosis 不合法——只可選 work／acceptance／solution／scope／resource"
        "（work＝施工錯、acceptance＝驗收錯、solution＝解法錯、scope＝範圍錯、"
        "resource＝資源錯）。"),
    GAP_REVIEW_REMEDY_INVALID: "remedy 不合法——只可選 halt（停等人裁決）／revise（重訂）。",
    GAP_REVIEW_REVISE_NOT_ALLOWED: (
        "diagnosis=work 或 resource 時**不允許 remedy=revise**——"
        "revise 只能用在 acceptance／solution／scope（要重訂的是驗收方式、"
        "解法或範圍）。施工或資源的問題請填 remedy=halt 停等人裁決。"),
    GAP_ACTION_MISSING: "缺 action。",
    GAP_ACTION_INVALID: ("action 不合法——只可選 close（結案）／rework（退回）／"
                         "review（重新檢討）。"),
    GAP_REWORK_TYPE_INVALID: ("rework_type 不合法——只可選 施工錯／驗收錯／解法錯"
                               "（英文 build／acceptance／solution 也接受）。"),
    GAP_ISSUE_RETURN_LIMIT: "退回次數已達上限，標待人處理。",
    GAP_ISSUE_FIELD_MISSING: "ISSUE 的欄位不齊。",
    GAP_APPLY_REJECTED: "守衛過了，但套用時被引擎擋下——照著退件說明改完再交一次。",
    "verdict.fail": (
        "你交 action=close，但**程式算出的總判是 fail**——有 required 的 check 是 FAIL。"
        "總判只由程式算，不能自稱（規格第 5 節）。"
    ),
    "verdict.partial": (
        "你交 action=close，但**程式算出的總判是 partial**——還有 required 的 check 是 "
        "PARTIAL／NOT_RUN，或這張單根本沒有 required 的 check（沒有判準就沒有通過）。"
        "總判只由程式算，不能自稱（規格第 5 節）。"
    ),
    "verdict.pass": "總判是 pass（這一項不該出現在缺口清單裡——引擎的問題，請回報）。",
    GAP_RECEIPT_UNVERIFIED: (
        "回執在平台上對不上——task_id 錨點、產生這則訊息的 session、完成標記三項"
        "至少要核到一項，目前都核不到。補齊方式：用上面**新的 task**重交一次，"
        "交件時帶**新的 message_id**（別重用舊的）。若這棒是重試、舊 task_id 已不在"
        "平台可查視窗內，照做就好——重交會產生新的可核對錨點。"
    ),
    GAP_RECEIPT_QUERY_FAILED: (
        "平台查不到回執（暫時性）：查詢端點此刻連不到，不是交件內容的問題。"
        "不用改交件，等下一輪再查；持續對不上會自動換新 task 重試。"
    ),
    GAP_RECEIPT_MESSAGE_FALLBACK: (
        "agent 看不見平台編的 message_id，它自報的值只當備援錨點（正式關聯鍵是 task_id）。"
        "重交時帶**新的** message_id，別重用舊的。"
    ),
    GAP_RECEIPT_MESSAGE_REUSED: (
        "這個 message_id 已被別的提交／任務用過——回執無從獨佔，核對不了。"
        "務必換**新的 message_id** 重交。"
    ),
    GAP_RECEIPT_MESSAGE_ID_MISSING: (
        "交件沒帶 message_id，回執無從核對。重交時一定要帶**新的** message_id。"
    ),
}


#: 工單九狀態的完整契約。
#:
#: `role`      這一棒由哪個角色做（`None` 表示不需人送交，協調器自己推進）
#: `next`      成功時的下一個狀態
#: `required`  這一棒必交的欄位（提示詞與守衛共用這份清單）
WORK_STATES: Dict[str, Dict[str, Any]] = {
    BACKLOG: {
        "role": None,
        "next": DESIGN,
        "title": "待派工",
        "required": [],
        "handoff_required": False,
    },
    DESIGN: {
        "role": "THINKER",
        "next": ACCEPTANCE_DESIGN,
        "title": "解法設計中",
        "required": ["solutions", "design_note"],
        "handoff_required": True,
        # **規則要寫在派工提示詞裡**（live 實測 2026-11-25：
        # THINKER 第一次只交 1 案 → 被 `solutions.count` 退件。
        # 退件訊息有說「至少要 2 案」，但**事前沒講** → 浪費一輪。
        # 重工設計那一關本來就有這句，這裡補上第一輪的。）
        "required_note": "**solutions 至少兩案**（THINKER 一棒直接產出，兩案要有實質差異——"
                         "成本與修改範圍不能都一樣）；design_note 用三句話講清楚"
                         "這件事要做什麼、為什麼這樣做、交件方式；"
                         "如果這件工作沒辦法用三句話描述完（標題、目標、交件方式），"
                         "就要嘗試分拆成多份工單。",
    },
    ACCEPTANCE_DESIGN: {
        "role": "PLANNER",
        "next": BUILD,
        "title": "驗收方式設計中",
        "required": ["selection", "checks"],
        "handoff_required": True,
    },
    BUILD: {
        "role": "WORKER",
        "next": VERIFY,
        "title": "施工中",
        "required": ["outputs", "done", "unfinished", "selfcheck", "cases_ran"],
        "handoff_required": True,
        "required_note": "WORKER 交付前必須先以 PLANNER 設計好的驗收方式自檢一遍，"
                         "把自檢結果寫入 selfcheck 欄位，並在 cases_ran 中標注實際跑的案例數。",
    },
    VERIFY: {
        "role": "THINKER",
        "next": ADJUDICATE,
        "title": "驗收中",
        "required": ["results", "cases_ran"],
        "handoff_required": True,
    },
    ADJUDICATE: {
        "role": "PLANNER",
        "next": CLOSED,
        "title": "結案中",
        "required": ["action", "close_reason", "rework_reason", "rework_type",
                     "diagnosis", "remedy"],
        # **提示詞要列全守衛會看的欄位**（與 `_guard_adjudicate` 同一份定義）。
        # 只列 `action` 時 agent 照提示詞交 → 守衛要 close_reason／rework_* 卻沒有
        # → 退件三次 → 標待人處理（與 ISSUE `answer_or_work` 同型的漂移）。
        "required_note": "**依 action 只交需要的那組**（其餘欄位交空字串即可）："
                         "close → close_reason；rework → rework_reason + rework_type"
                         "（施工錯／驗收錯／解法錯；英文 build／acceptance／solution 也接受）；"
                         "rework 達上限 → review → diagnosis + remedy"
                         "（halt／revise，與 close/rework 二選一）"
                         "——revise 只適用於 diagnosis=acceptance／solution／scope，"
                         "diagnosis=work／resource 只能 halt",
        # **偵測是否應該重新分解工單**：
        # 若 solutions 超過 2 件、或者成本/修改範圍都一樣（兩案其實同一案），
        # 就標記為重新分解。
        # 這邏輯會在 `_guard_adjudicate` 中實作（見 engine.py）。
        "handoff_required": True,
    },
    REWORK_DESIGN: {
        "role": "THINKER",
        "next": REWORK_ACCEPTANCE_DESIGN,
        "title": "重工解法設計中",
        # **重工解法也要兩案**（spec 第 7 步）——THINKER 一棒直接產出，
        # 不用再分派兩個 SUBAGENT（2026-11-23 取消：那太慢、還要自己編 id）。
        "required": ["solutions", "rework_plan"],
        "handoff_required": True,
        "required_note": "solutions 至少兩案（THINKER 一棒直接產出，兩案要有實質差異）；"
                         "rework_plan 要引用退回原因。",
    },
    REWORK_ACCEPTANCE_DESIGN: {
        "role": "PLANNER",
        "next": REWORK_BUILD,
        "title": "重工驗收方式設計中",
        # spec 第 8 步：PLANNER 挑重工解法 → **設計驗收方式** → 發工單給 WORKER。
        "required": ["selection", "checks"],
        "handoff_required": True,
        "required_note": "selection 要指**這一輪**的解法（不是第一輪的）；"
                         "checks 每條仍要指向已確認需求，門檻只能更嚴。",
    },
    REWORK_BUILD: {
        "role": "WORKER",
        "next": VERIFY,
        "title": "重工中",
        "required": ["outputs", "done", "unfinished", "selfcheck", "cases_ran"],
        "handoff_required": True,
    },
    CLOSED: {
        "role": None,
        "next": None,
        "title": "已結案",
        "required": [],
        "handoff_required": False,
    },
}

#: 合法轉移。退回支線：adjudicate → rework_design → rework_acceptance_design
#: → rework_build → verify（spec 第 6~9 步：退回 → 重工解法 → **重工驗收方式**
#: → 重工 → 再驗收）。
WORK_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    BACKLOG: (DESIGN,),
    DESIGN: (ACCEPTANCE_DESIGN,),
    ACCEPTANCE_DESIGN: (BUILD,),
    BUILD: (VERIFY,),
    VERIFY: (ADJUDICATE,),
    ADJUDICATE: (CLOSED, REWORK_DESIGN),
    REWORK_DESIGN: (REWORK_ACCEPTANCE_DESIGN,),
    REWORK_ACCEPTANCE_DESIGN: (REWORK_BUILD,),
    REWORK_BUILD: (VERIFY,),
    CLOSED: (),
}

#: ISSUE 六狀態契約（spec 第 4(b) 步：撞三次 → ISSUE 單 → THINKER 出兩解法
#: → PLANNER 挑一個並填對策 → WORKER 依對策執行 → 生效則關單、未生效重複流程）。
ISSUE_STATES: Dict[str, Dict[str, Any]] = {
    ISSUE_RAISED: {
        "role": None, "next": ISSUE_DESIGNING, "title": "發起中",
        # **「ISSUE 完成要求」**（spec 的 ISSUE 單模板）：說明達成什麼效果才算完成。
        "required": ["trigger_kind", "problem", "location", "tried", "impact",
                     "question", "completion_criteria"],
    },
    ISSUE_DESIGNING: {
        "role": "THINKER", "next": ISSUE_HANDLING, "title": "解法設計中",
        # 兩案，跟工單的 design 棒同一條硬約束（THINKER 一棒直接產出）。
        "required": ["solutions"],
        "required_note": "至少兩案，THINKER 一棒直接產出，兩案要有實質差異"
                         "（成本與修改範圍不能都一樣）。",
    },
    ISSUE_HANDLING: {
        "role": "PLANNER", "next": ISSUE_BUILDING, "title": "對策中",
        # **對策**＝PLANNER 挑的那一案與「要怎麼修」——spec 明講要填在 ISSUE 單上。
        "required": ["selection", "countermeasure"],
        "required_note": "selection 指哪一案（案號，見 ISSUE 檔案的解法表）＋為什麼；"
                         "countermeasure 是**對策**：要怎麼修、做完怎麼算解決。",
    },
    ISSUE_BUILDING: {
        "role": "WORKER", "next": ISSUE_ADJUDICATING, "title": "依對策施工中",
        "required": ["outputs", "selfcheck"],
    },
    ISSUE_ADJUDICATING: {
        "role": "initiator", "next": ISSUE_CLOSED, "title": "結案中",
        # 結案（resolution）或退回（return_reason）二選一——退回不是結案。
        "required": ["resolution", "return_reason"],
        "required_note": "**二選一（不可同時給）**：對策生效 → resolution（結案）；"
                         "對策沒生效 → return_reason（寫發生的情況，次數累計）。",
    },
    ISSUE_CLOSED: {
        "role": None, "next": None, "title": "已結案", "required": [],
    },
}

ISSUE_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    ISSUE_RAISED: (ISSUE_DESIGNING,),
    ISSUE_DESIGNING: (ISSUE_HANDLING,),
    ISSUE_HANDLING: (ISSUE_BUILDING,),
    ISSUE_BUILDING: (ISSUE_ADJUDICATING,),
    # 退回 → **重複上一個流程**（spec：未生效就在同一張單上重跑解法→對策→施工）
    ISSUE_ADJUDICATING: (ISSUE_CLOSED, ISSUE_DESIGNING),
    ISSUE_CLOSED: (),
}

#: `solutions` 每一案**必備**的欄位（守衛與提示詞共用這一份定義）。
SOLUTION_REQUIRED_FIELDS: Tuple[str, ...] = (
    "approach", "cost", "change_scope", "effort", "risk", "verifiable_by",
)

#: `solutions` 每一案的**選填**欄位。
SOLUTION_OPTIONAL_FIELDS: Tuple[str, ...] = ("reuses",)

#: ISSUE 各狀態的**欄位規格**（提示詞直接渲染這份，不另寫一份欄位規則）。
#:
#: 與守衛（`engine._check_issue_stage`）看的欄位**同名**——
#: 提示詞叫 `answer_or_work`、守衛看 `answer`／`linked_work_ids` 的不一致，
#: 會讓 agent 照提示詞交、卻被守衛退件（實測會卡住）。
ISSUE_FIELD_SPECS: Dict[str, Dict[str, Any]] = {
    ISSUE_RAISED: {
        "trigger_kind": {"kind": "text", "note": "work_blocked（工單卡住）／needs_decision（要裁決）"},
        "problem": {"kind": "text", "note": "什麼問題"},
        "location": {"kind": "text", "note": "發生在哪裡（檔案／步驟／階段）"},
        "tried": {"kind": "text", "note": "已經試過什麼"},
        "impact": {"kind": "text", "note": "影響範圍"},
        "question": {"kind": "text", "note": "要誰決定什麼"},
        "completion_criteria": {"kind": "text",
                                "note": "ISSUE 完成要求：達成什麼效果才算解決"},
    },
    ISSUE_DESIGNING: {
        "solutions": {
            "kind": "list",
            "min_items": 2,
            "note": "至少兩案，THINKER 一棒直接產出；兩案不得實質相同"
                    "（成本與修改範圍不能都一樣）",
            "item_fields": (list(SOLUTION_REQUIRED_FIELDS)
                            + list(SOLUTION_OPTIONAL_FIELDS)),
        },
    },
    ISSUE_HANDLING: {
        "selection": {
            "kind": "object",
            "note": "選哪一案（案號，見 ISSUE 檔案的解法表）＋為什麼",
            "item_fields": ["solution_id", "reason"],
        },
        "countermeasure": {"kind": "text",
                           "note": "對策：要怎麼修、做完怎麼算解決"},
    },
    ISSUE_BUILDING: {
        "outputs": {"kind": "list", "min_items": 1,
                    "item_fields": ["path", "size", "sha256"],
                    "note": "依對策執行後的產物指紋（路徑要相對專案根）"},
        "selfcheck": {"kind": "text", "note": "自檢結果：對策有沒有生效"},
    },
    ISSUE_ADJUDICATING: {
        "resolution": {"kind": "text", "note": "對策生效 → 結案"},
        "return_reason": {"kind": "text",
                          "note": "對策沒生效 → 附發生的情況退回（次數累計）"},
    },
}

#: 退回支線：驗收方式錯 → 回 `acceptance_design`（不是回 rework_design）。
ACCEPTANCE_REWORK_TARGET = ACCEPTANCE_DESIGN
SOLUTION_REWORK_TARGET = DESIGN

#: 八欄交接檔（規格第 9 節）。下一棒必讀。
#:
#: **放在這裡**（規則正本）而不是 `prompts.py`：派工提示詞也要帶交接檔欄位，
#: 兩邊共用同一份定義才不會漂移。
HANDOFF_FIELDS: List[str] = [
    "ROLE",                          # 這一棒的角色
    "OPEN WORK",                     # 還沒做完的事
    "SUBAGENTS RUNNING",             # 還在跑的 subagent
    "RULES AND NUMBERS IN FORCE",    # 目前有效的門檻／範圍／資源上限（可能已變，不要用舊值）
    "RECIPES",                       # 可重用的做法
    "DECISIONS AND WHY",             # 已做的決定與理由
    "LESSONS",                       # 踩過的坑
    "SPEND",                         # 花掉的資源
]

#: 每個階段的**欄位規格**——提示詞直接渲染這份，不另寫一份欄位規則。
#:
#: 守衛（`check_stage`）檢的就是這些欄位。**提示詞與守衛共用同一份定義**，
#: 否則一定會漂移：agent 照提示詞交了，卻被守衛退回（實測就發生了這件事——
#: 派工提示詞只給欄位名稱，agent 交不出 `handoff`，整批 14 筆被退回）。
STAGE_FIELD_SPECS: Dict[str, Dict[str, Any]] = {
    DESIGN: {
        "solutions": {
            "kind": "list",
            "min_items": 2,
            "note": "至少兩案，THINKER 一棒直接產出；兩案不得實質相同"
                    "（成本與修改範圍不能都一樣）",
            "item_fields": (list(SOLUTION_REQUIRED_FIELDS)
                            + list(SOLUTION_OPTIONAL_FIELDS)),
        },
        "design_note": {"kind": "text", "note": "設計說明：做了什麼、為什麼這樣做"},
    },
    ACCEPTANCE_DESIGN: {
        "selection": {
            "kind": "object",
            "note": "選哪一案、為什麼、以及為什麼不要其他案",
            "item_fields": ["solution_id", "reason", "rejected_because"],
        },
        "checks": {
            "kind": "list",
            "min_items": 1,
            "note": "每條 check 必須指向一條已確認需求",
            "item_fields": ["id", "requirement_id", "method", "scope", "threshold",
                            "evidence", "required", "execution_kind"],
        },
    },
    BUILD: {
        "outputs": {"kind": "list", "min_items": 1,
                    "item_fields": ["path", "size", "sha256"],
                    "note": "path 要是**專案根內的路徑**（相對路徑最佳；落在專案根內的"
                            "絕對路徑也接受，引擎會自動轉）；size/sha256 要跟現檔一致"},
        "done": {"kind": "list", "note": "已做完成的事"},
        "unfinished": {"kind": "list", "note": "沒做完的事（空清單也要交）"},
        "selfcheck": {"kind": "text", "note": "自檢結果"},
        "cases_ran": {"kind": "number", "note": "實際跑的案例數，不得灌水；"
                                               "純文件／人工檢視自報 execution_kind=review"},
    },
    VERIFY: {
        "results": {"kind": "list", "min_items": 1,
                    "item_fields": ["id", "status", "reason", "evidence"]},
        "cases_ran": {"kind": "number"},
    },
    ADJUDICATE: {
        "action": {"kind": "text",
                   "note": "close（結案，附 close_reason）／rework（退回，附 "
                           "rework_reason + rework_type：施工錯／驗收錯／解法錯）／"
                           "review（重工達上限的重新檢討，附 diagnosis + remedy）"},
        "close_reason": {"kind": "text", "note": "action=close 必填：結案理由"},
        "rework_reason": {"kind": "text",
                          "note": "action=rework 必填：退回原因（下一棒會引用）"},
        "rework_type": {"kind": "text",
                        "note": "action=rework 必填：施工錯／驗收錯／解法錯"
                                "（英文 build／acceptance／solution 也接受）"},
        "diagnosis": {"kind": "text",
                      "note": "action=review 必填：work／acceptance／solution／scope／resource"},
        "remedy": {"kind": "text",
                   "note": "action=review 必填：halt／revise"
                           "（revise 只適用於 diagnosis=acceptance／solution／scope；"
                           "work／resource 只能 halt）"},
    },
    REWORK_DESIGN: {
        "solutions": {
            "kind": "list",
            "min_items": 2,
            "note": "至少兩案，THINKER 一棒直接產出；兩案不得實質相同",
            "item_fields": (list(SOLUTION_REQUIRED_FIELDS)
                            + list(SOLUTION_OPTIONAL_FIELDS)),
        },
        "rework_plan": {"kind": "text", "note": "針對退回原因設計，並引用那個原因"},
    },
    REWORK_ACCEPTANCE_DESIGN: {
        "selection": {
            "kind": "object",
            "note": "選哪一案（這一輪的解法）、為什麼、以及為什麼不要其他案",
            "item_fields": ["solution_id", "reason", "rejected_because"],
        },
        "checks": {
            "kind": "list",
            "min_items": 1,
            "note": "每條 check 必須指向一條已確認需求",
            "item_fields": ["id", "requirement_id", "method", "scope", "threshold",
                            "evidence", "required", "execution_kind"],
        },
    },
    REWORK_BUILD: {
        "outputs": {"kind": "list", "min_items": 1,
                    "item_fields": ["path", "size", "sha256"],
                    "note": "path 要是**專案根內的路徑**（相對路徑最佳；落在專案根內的"
                            "絕對路徑也接受，引擎會自動轉）；size/sha256 要跟現檔一致"},
        "done": {"kind": "list"},
        "unfinished": {"kind": "list"},
        "selfcheck": {"kind": "text"},
        "cases_ran": {"kind": "number"},
    },
}


# ---------------------------------------------------------------- 小工具

def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_selection(selection: Any) -> Dict[str, Any]:
    """選案資料正規化：`rejected_because` 一定是**清單**。

    live 實測（2026-11-23）：角色把「為什麼不要其他案」交成**一個字串**，
    引擎照收 → 工單檔案渲染時把字串當 iterable 逐字元印出來，
    「沒選它：S」「沒選它：O」「沒選它：L」……整段選擇變成亂碼。
    同一份資料因此在寫入端與讀取端都要正規化一次。
    """
    data = _as_dict(selection)
    data["rejected_because"] = _as_list(data.get("rejected_because"))
    return data


def _normalize_rework_type(value: Any) -> str:
    """把 rework_type 正規化成正本中文值；不合法回 ""。

    **提示與守衛不一致的實測後果**（2026-11-24 目錄拆產品圖3）：
    派工提示的「標準／範例」寫 build／acceptance／solution，
    守衛只認施工錯／驗收錯／解法錯 → 角色照提示填英文 → 7 次退件。
    這裡兩邊都收，英文填法正規化成中文再存。
    """
    text = _text(value).strip()
    if text in REWORK_TYPES:
        return text
    return REWORK_TYPE_ALIASES.get(text.lower(), "")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def load_work(conn: sqlite3.Connection, work_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
    return dict(row) if row else None


def load_issue(conn: sqlite3.Connection, issue_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return dict(row) if row else None


def current_checks(conn: sqlite3.Connection, work_id: str) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM checks WHERE work_id = ?", (work_id,)).fetchall()
    return {r["id"]: dict(r) for r in rows}


def current_solutions(conn: sqlite3.Connection, work_id: str,
                      round_no: Optional[int] = None) -> List[Dict[str, Any]]:
    """這張工單的解法清單。`round_no` 給定時只回**那一輪**的（重工解法另算一輪）。"""
    sql = "SELECT * FROM solutions WHERE work_id = ?"
    args: List[Any] = [work_id]
    if round_no is not None:
        sql += " AND round = ?"
        args.append(int(round_no))
    rows = conn.execute(sql + " ORDER BY id", tuple(args)).fetchall()
    return [dict(r) for r in rows]


def solution_selectors(solutions: List[Dict[str, Any]]) -> Dict[str, int]:
    """可選的解法代號 → 索引（0 起算）。

    **案號**（"1"、"2"…）是主選法：工單／ISSUE 檔案的解法表照順序編號，
    PLANNER 只要填案號就行（不必背 context id）。

    引擎自己的 id（`SOL-00013`）與舊的 context id 也接受——舊的交件不會
    因為換了選法就整個卡住。
    """
    selectors: Dict[str, int] = {}
    for index, sol in enumerate(solutions):
        selectors[str(index + 1)] = index
        if sol.get("id"):
            selectors[str(sol["id"])] = index
        context_id = _text(sol.get("subagent_context_id"))
        if context_id:
            selectors[context_id] = index
    return selectors


def results_for(conn: sqlite3.Connection, work_id: str,
                round_no: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    if round_no is None:
        rows = conn.execute(
            "SELECT * FROM results WHERE work_id = ? ORDER BY round, check_id",
            (work_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM results WHERE work_id = ? AND round = ? ORDER BY check_id",
            (work_id, round_no)).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        out[r["check_id"]] = dict(r)
    return out


def current_round(work: Dict[str, Any]) -> int:
    """第幾輪驗收。首次施工＝1；每次重工 +1。"""
    return int(work.get("rework_count") or 0) + 1


# ---------------------------------------------------------------- 總判

def compute_verdict(conn: sqlite3.Connection, work_id: str) -> str:
    """**總判只由程式算**（規格第 5 節），不接受模型提交。

    - 所有 `required=1` 的 check 都 PASS → `pass`
    - 任一 required check FAIL → `fail`
    - 其餘（有 required 但非全 PASS／有 NOT_RUN）→ `partial`
    - 沒有任何 required check → `partial`（沒有判準就沒有通過）
    """
    checks = current_checks(conn, work_id)
    required = [c for c in checks.values() if int(c.get("required") or 0) == 1]
    if not required:
        return "partial"
    results = results_for(conn, work_id)
    statuses = []
    for check in required:
        row = results.get(check["id"])
        statuses.append(row["status"] if row else NOT_RUN)
    if all(s == PASS for s in statuses):
        return "pass"
    if any(s == FAIL for s in statuses):
        return "fail"
    return "partial"


# ---------------------------------------------------------------- 證據

def _normalize_evidence_path(project_root: str, raw: str) -> Optional[str]:
    """把證據路徑正規化成「專案根內的相對路徑」。

    - **相對路徑** → 原樣回傳（`resolve_inside` 會驗它有沒有逃出根）。
    - **絕對路徑、落在專案根內** → 換成相對路徑回傳。
      這是 agent 最常踩的坑：它把產物寫在專案根、卻回報絕對路徑
      （`/Users/…/1111111/fbf-e2e-checklist.md`），`resolve_inside` 把絕對路徑
      當逃逸一律拒 → 歸成 `evidence.missing`，檔案明明存在也白交（實測 W-001/W-003
      每輪都卡在這）。檔案在根內就合法化，別讓格式擋掉實質交件。
    - **絕對路徑、逃出專案根** → 回 None（等同 `..` 逃逸，拒絕）。
    """
    raw = _text(raw).strip().replace("\\", "/")
    if not raw:
        return raw
    if not os.path.isabs(raw):
        return raw
    root = os.path.realpath(os.path.abspath(os.path.expanduser(project_root)))
    target = os.path.realpath(raw)
    if target == root or target.startswith(root + os.sep):
        return os.path.relpath(target, root).replace("\\", "/")
    return None


def register_evidence(conn: sqlite3.Connection, project_root: str,
                      items: List[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把提交的產物指紋建成證據。

    回傳 (證據列, 缺口代碼)。**任何一筆指紋對不上就是缺口**——
    程式自己重算，不採信模型填的大小與雜湊。
    """
    rows: List[Dict[str, Any]] = []
    gaps: List[str] = []
    for item in _as_list(items):
        data = _as_dict(item)
        rel = _text(data.get("path"))
        if not rel:
            gaps.append(GAP_EVIDENCE_MISSING)
            continue
        rel = _normalize_evidence_path(project_root, rel)
        if rel is None:
            # 絕對路徑逃出了專案根目錄 → 等同 `..` 逃逸，一律拒絕。
            gaps.append(GAP_EVIDENCE_MISSING)
            continue
        try:
            absolute = store.resolve_inside(project_root, rel)
        except store.PathEscape:
            gaps.append(GAP_EVIDENCE_MISSING)
            continue
        try:
            size, digest = store.file_fingerprint(absolute)
        except OSError:
            gaps.append(GAP_EVIDENCE_MISSING)
            continue
        evidence_id = store.next_id(conn, "evidence", "E", 6)
        conn.execute(
            "INSERT INTO evidence(id, project_relative_path, size, sha256, created_at)"
            " VALUES (?,?,?,?,?)", (evidence_id, rel, size, digest, store.now()))
        rows.append({"id": evidence_id, "path": rel, "size": size, "sha256": digest})
    return rows, gaps


# ---------------------------------------------------------------- 守衛

def _collect_solutions(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把 payload 的 `solutions` 收成清單，並回**機械缺口**。

    缺口：案數不足、兩案其實同一案、欄位沒填齊。

    **SUBAGENT 已取消（2026-11-23）**：不再要求 `subagent_context_id`、
    不再檢查兩案 context 是否不同、引擎也不再回平台核對 context——
    THINKER 一棒直接產兩案。
    """
    gaps: List[str] = []
    raw = _as_list(payload.get("solutions"))
    solutions: List[Dict[str, Any]] = []
    for item in raw:
        data = _as_dict(item)
        context_id = _text(data.get("subagent_context_id"))
        approach = _text(data.get("approach"))
        missing = [f for f in SOLUTION_REQUIRED_FIELDS if not _text(data.get(f))]
        if missing:
            gaps.append(GAP_SOLUTION_FIELD_MISSING)
        solutions.append({
            "subagent_context_id": context_id,
            "approach": approach,
            "cost": _text(data.get("cost")),
            "change_scope": _text(data.get("change_scope")),
            "effort": _text(data.get("effort")),
            "risk": _text(data.get("risk")),
            "verifiable_by": _text(data.get("verifiable_by")),
            "reuses": _text(data.get("reuses")),
        })

    if len(solutions) < 2:
        gaps.append(GAP_SOLUTIONS_COUNT)

    # 內容取捨不同：不得兩案在 approach／cost／change_scope 全部相同
    if len(solutions) >= 2:
        a, b = solutions[0], solutions[1]
        if (a["approach"] == b["approach"] and a["cost"] == b["cost"]
                and a["change_scope"] == b["change_scope"]):
            gaps.append(GAP_SOLUTION_TOO_SIMILAR)
    return solutions, gaps


def issue_solution_gaps(payload: Dict[str, Any]) -> List[str]:
    """ISSUE 的兩案機械檢查——跟工單 design 棒**同一條硬約束**。

    spec 第 4(b) 步：ISSUE 的解法也要兩案（THINKER 一棒直接產出）。
    """
    _, gaps = _collect_solutions(payload)
    return gaps


def _guard_design(conn: sqlite3.Connection, work: Dict[str, Any],
                  payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    """設計方案中：至少兩案，內容取捨不同。

    **這裡只擋機械事實**（有沒有填、兩案是不是其實同一案）。
    SUBAGENT 已取消（2026-11-23）→ 不再要求 context id、不再回平台核對。
    """
    solutions, gaps = _collect_solutions(payload)
    if not _text(payload.get("design_note")):
        gaps.append(GAP_DESIGN_NOTE_MISSING)
    return gaps, {"solutions": solutions}


def _guard_acceptance(conn: sqlite3.Connection, work: Dict[str, Any],
                      payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    """驗收方式設計中：選案理由 ＋ 每條 check 指向已確認需求。"""
    gaps: List[str] = []
    selection = normalize_selection(payload.get("selection"))
    checks_raw = _as_list(payload.get("checks"))

    selectors = solution_selectors(
        current_solutions(conn, work["id"], round_no=current_round(work)))
    selected = _text(selection.get("solution_id"))
    if not selected or not _text(selection.get("reason")):
        gaps.append(GAP_SELECTION_MISSING)
    if selected and selectors and selected not in selectors:
        gaps.append(GAP_SELECTION_UNKNOWN_SOLUTION)
    # 重工驗收方式設計：這一輪的解法必須已經在（重工解法設計中交上來了）。
    if work.get("state") == REWORK_ACCEPTANCE_DESIGN and not selectors:
        gaps.append(GAP_SOLUTIONS_COUNT)

    requirements = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM requirements").fetchall()}

    # 修改驗收方式的邊界（規格第 5 節）。**在守衛層判定**，
    # 這樣回報的是穩定缺口代碼，而不是一句例外訊息。
    old_checks = current_checks(conn, work["id"])
    old_results = results_for(conn, work["id"])

    prepared: List[Dict[str, Any]] = []
    seen_ids = set()
    for item in checks_raw:
        data = _as_dict(item)
        check_id = _text(data.get("id"))
        requirement_id = _text(data.get("requirement_id"))
        method = _text(data.get("method"))
        missing = [f for f in ("id", "requirement_id", "method", "scope", "threshold",
                               "evidence") if not _text(data.get(f))]
        if missing:
            gaps.append(GAP_CHECK_FIELD_MISSING)
        requirement = requirements.get(requirement_id)
        if requirement is None:
            gaps.append(GAP_CHECK_REQUIREMENT_UNKNOWN)
        elif not requirement.get("confirmed_at"):
            gaps.append(GAP_REQUIREMENT_UNCONFIRMED)
        kind = _text(data.get("execution_kind")) or EXEC_TEST
        if kind not in EXEC_KINDS:
            kind = EXEC_TEST
        required = 1 if data.get("required", True) else 0
        threshold = _text(data.get("threshold"))
        seen_ids.add(check_id)

        previous = old_checks.get(check_id)
        if previous is not None:
            if int(previous.get("required") or 0) == 1 and required == 0:
                gaps.append(GAP_CHECK_REQUIRED_DOWNGRADED)
            if not _threshold_not_lowered(previous.get("threshold"), threshold):
                gaps.append(GAP_CHECK_THRESHOLD_LOWERED)

        prepared.append({
            "id": check_id,
            "requirement_id": requirement_id,
            "method": method,
            "scope": _text(data.get("scope")),
            "threshold": threshold,
            "evidence": _text(data.get("evidence")),
            "required": required,
            "execution_kind": kind,
        })

    # 已有驗收結果的 check 不可刪
    for check_id in old_checks:
        if check_id not in seen_ids and check_id in old_results:
            gaps.append(GAP_CHECK_RESULT_DELETED)

    if not prepared:
        gaps.append(GAP_CHECKS_MISSING)
    return gaps, {"selection": selection, "checks": prepared}


def _guard_build(conn: sqlite3.Connection, work: Dict[str, Any],
                 payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    """施工中／重工中：產物指紋、已完成、未完成、自檢、案例數。

    **證據的實際雜湊比對在 `check_stage` 做**（那裡才知道專案根）。
    這裡只做結構檢查。
    """
    gaps: List[str] = []

    if not _text(payload.get("selfcheck")):
        gaps.append(GAP_SELFCHECK_MISSING)
    if "unfinished" not in payload:
        gaps.append(GAP_UNFINISHED_INVALID)
    elif not isinstance(payload.get("unfinished"), list):
        gaps.append(GAP_UNFINISHED_INVALID)
    if not _as_list(payload.get("outputs")):
        gaps.append(GAP_OUTPUTS_MISSING)

    cases = _num(payload.get("cases_ran"))
    if cases is None or cases < 1:
        # 純文件／人工檢視的單可以 0 案例，但必須自報 execution_kind=review
        if _text(payload.get("execution_kind")) != EXEC_REVIEW:
            gaps.append(GAP_CASES_ZERO)
    return gaps, {"outputs": _as_list(payload.get("outputs")),
                  "done": _as_list(payload.get("done")),
                  "unfinished": _as_list(payload.get("unfinished")),
                  "selfcheck": _text(payload.get("selfcheck")),
                  "cases_ran": int(cases) if cases is not None else 0}


def _guard_verify(conn: sqlite3.Connection, work: Dict[str, Any],
                  payload: Dict[str, Any],
                  session_id: str = "") -> Tuple[List[str], Dict[str, Any]]:
    """驗收中：每條 check 都要有結果；不可驗自己在本工單的施工。"""
    gaps: List[str] = []
    checks = current_checks(conn, work["id"])
    raw_results = _as_list(payload.get("results"))
    prepared: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_results:
        data = _as_dict(item)
        check_id = _text(data.get("id"))
        status = _text(data.get("status")).upper()
        if status not in RESULT_STATUSES:
            gaps.append(GAP_RESULT_STATUS_INVALID)
        if check_id in seen:
            gaps.append(GAP_RESULT_MISSING)
        seen.add(check_id)
        if check_id not in checks:
            gaps.append(GAP_RESULT_MISSING)
            continue
        prepared.append({
            "id": check_id,
            "status": status,
            "reason": _text(data.get("reason")),
            "evidence": _as_dict(data.get("evidence")),
        })

    for check_id in checks:
        if check_id not in seen:
            gaps.append(GAP_RESULT_MISSING)

    # 不可自做自驗：驗收者不得是本工單施工階段的執行 session
    builder = conn.execute(
        "SELECT assigned_session FROM tasks WHERE work_id = ? AND stage IN (?, ?)"
        " ORDER BY created_at DESC LIMIT 1",
        (work["id"], BUILD, REWORK_BUILD)).fetchone()
    if builder and builder["assigned_session"] and \
            builder["assigned_session"] == session_id:
        gaps.append(GAP_SELF_VERIFY)

    cases = _num(payload.get("cases_ran"))
    need_cases = any(c.get("execution_kind") == EXEC_TEST for c in checks.values())
    if need_cases and (cases is None or cases < 1):
        gaps.append(GAP_CASES_ZERO)
    return gaps, {"results": prepared,
                  "cases_ran": int(cases) if cases is not None else 0}


def _guard_adjudicate(conn: sqlite3.Connection, work: Dict[str, Any],
                      payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    """結案中：close 要程式算出 pass；rework 不到上限；達上限必走重新檢討。"""
    gaps: List[str] = []
    action = _text(payload.get("action")).lower()
    max_rework = store.limit_of(conn, "max_rework", 2)

    if action not in ("close", "rework", "review"):
        gaps.append(GAP_ACTION_INVALID if action else GAP_ACTION_MISSING)
        return gaps, {"action": action}

    if action == "close":
        verdict = compute_verdict(conn, work["id"])
        if verdict != "pass":
            gaps.append("verdict.%s" % verdict)
        if not _text(payload.get("close_reason")):
            gaps.append(GAP_ACTION_MISSING)

    elif action == "rework":
        if int(work.get("rework_count") or 0) >= max_rework:
            gaps.append(GAP_REWORK_LIMIT)
        if not _text(payload.get("rework_reason")):
            gaps.append(GAP_ACTION_MISSING)
        rework_type = _normalize_rework_type(payload.get("rework_type"))
        if not rework_type:
            gaps.append(GAP_REWORK_TYPE_INVALID)

    else:  # review —— 重工達上限後的重新檢討
        diagnosis = _text(payload.get("diagnosis")).lower()
        remedy = _text(payload.get("remedy")).lower()
        if diagnosis not in DIAGNOSES:
            gaps.append(GAP_REVIEW_DIAGNOSIS_INVALID)
        if remedy not in (REMEDY_HALT, REMEDY_REVISE):
            gaps.append(GAP_REVIEW_REMEDY_INVALID)
        max_revision = store.limit_of(conn, "max_revision", 2)
        if remedy == REMEDY_REVISE:
            if int(work.get("revision_count") or 0) >= max_revision:
                gaps.append(GAP_REVISION_LIMIT)
            if diagnosis in ("work", "resource"):
                gaps.append(GAP_REVIEW_REVISE_NOT_ALLOWED)
    # **payload 的字段要隨 data 帶回去**——`apply_stage`／`next_state_for` 寫正本
    # 用的就是這份。少了它們，PLANNER 填的退回原因**從來沒被存下來**
    # （下一棒 THINKER 收不到「為什麼被退回」、規格第 5 節的「退回要寫原因」落空）。
    return gaps, {
        "action": action,
        "close_reason": _text(payload.get("close_reason")),
        "rework_reason": _text(payload.get("rework_reason")),
        "rework_type": _normalize_rework_type(payload.get("rework_type")),
        "diagnosis": _text(payload.get("diagnosis")),
        "remedy": _text(payload.get("remedy")),
    }


def _guard_rework_design(conn: sqlite3.Connection, work: Dict[str, Any],
                         payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    """重工解法設計中：**兩案** ＋ 重工方案。

    spec 第 7 步：「THINKER 根據 PLANNER 填寫要重工的部分，根據搭配的專案 SKILL，
    分出兩個 SUB AGENT 各提供一個解法」——所以這一棒跟第一次設計一樣，
    要兩個**不同來源**的案子，不是一段文字。

    沿用 `_guard_design` 的機械檢查（案數、context 不重複、不得實質相同），
    只是**不要求 design_note**（那一欄是第一次設計的說明）。
    """
    gaps, data = _guard_design(conn, work, payload)
    gaps = [g for g in gaps if g != GAP_DESIGN_NOTE_MISSING]
    if not _text(payload.get("rework_plan")):
        gaps.append(GAP_ACTION_MISSING)
    return gaps, {"solutions": data.get("solutions", []),
                  "rework_plan": _text(payload.get("rework_plan"))}


#: 每個狀態的守衛函式
_GUARDS = {
    DESIGN: _guard_design,
    ACCEPTANCE_DESIGN: _guard_acceptance,
    BUILD: _guard_build,
    VERIFY: _guard_verify,
    ADJUDICATE: _guard_adjudicate,
    REWORK_DESIGN: _guard_rework_design,
    REWORK_ACCEPTANCE_DESIGN: _guard_acceptance,
    REWORK_BUILD: _guard_build,
}


def check_stage(conn: sqlite3.Connection, work: Dict[str, Any], stage: str,
                payload: Dict[str, Any],
                project_root: str = "",
                session_id: str = "") -> Tuple[List[str], Dict[str, Any]]:
    """跑某個狀態的守衛。回傳 (缺口代碼清單, 已整理的資料)。

    守衛不過 → **狀態不變**、寫 gate 紀錄、把缺口代碼通知目前角色。
    """
    guard = _GUARDS.get(stage)
    if guard is None:
        return [GAP_ACTION_INVALID], {}

    if guard is _guard_verify:
        gaps, data = guard(conn, work, payload, session_id)
    else:
        gaps, data = guard(conn, work, payload)

    # 產物指紋：程式自己重算，不採信模型填的大小與雜湊
    if stage in (BUILD, REWORK_BUILD) and project_root:
        outputs, evidence_gaps = register_evidence(conn, project_root, payload.get("outputs"))
        gaps = [g for g in gaps if g != GAP_EVIDENCE_MISSING] + evidence_gaps
        data["outputs"] = outputs

    # 交接檔必備（規格第 9 節：狀態變更時未提供 handoff，守衛退件）
    if WORK_STATES.get(stage, {}).get("handoff_required"):
        if not _as_dict(payload.get("handoff")):
            gaps.append(GAP_HANDOFF_MISSING)

    return sorted(set(gaps)), data


# ---------------------------------------------------------------- 表單標準

#: **每個欄位的標準**（型別、規則、範例）。
#:
#: 退件時引擎把「哪個欄位不標準、為什麼不標準、標準是什麼、範例長什麼樣」
#: 逐條唸出來——只丟一個缺口代碼，角色看不懂就改不對（規格第 5 節）。
#:
#: **這份是規則正本**：守衛檢的就是這些欄位，提示詞渲染的也是這份。
FIELD_STANDARDS: Dict[str, Dict[str, str]] = {
    "solutions": {"type": "清單",
                  "rule": "至少 2 案；兩案的 approach／cost／change_scope 不得都一樣",
                  "example": '[{"approach": "…", "cost": "低（2 小時）", "change_scope": "…",'
                             ' "effort": "小", "risk": "低", "verifiable_by": "跑測試"}]'},
    "approach": {"type": "字串", "rule": "這一案怎麼做（一句話講清楚做法）",
                 "example": "把工單狀態機抽成 workflow.py 的規則表"},
    "cost": {"type": "字串", "rule": "這一案的成本估算（時間／金錢／複雜度）",
             "example": "低（2 小時）"},
    "change_scope": {"type": "字串", "rule": "要動到哪些地方",
                     "example": "workflow.py、engine.py 兩個檔"},
    "effort": {"type": "字串", "rule": "工作量", "example": "小"},
    "risk": {"type": "字串", "rule": "風險與它的後果",
             "example": "低（要重跑全套測試）"},
    "verifiable_by": {"type": "字串", "rule": "怎麼驗證這一案有效",
                      "example": "python3 -m unittest discover -s tests 全過"},
    "reuses": {"type": "字串", "rule": "可重用的既有做法（**可以留空**）",
               "example": "沿用既有的 outbox 重送邏輯"},
    "design_note": {"type": "字串", "rule": "設計說明：做了什麼、為什麼這樣做",
                    "example": "把契約集中到 workflow.py，提示詞只渲染它"},
    "rework_plan": {"type": "字串", "rule": "針對退回原因設計，並引用那個原因",
                    "example": "補上 out2.txt（上次退回原因是少一項產出）"},
    "selection": {"type": "物件",
                  "rule": "選哪一案、為什麼、為什麼不要其他案",
                  "example": '{"solution_id": "1", "reason": "成本最小",'
                             ' "rejected_because": ["另一案要改到核心流程"]}'},
    "solution_id": {"type": "字串", "rule": "選中的**案號**（不是 context id）",
                    "example": "1"},
    "reason": {"type": "字串", "rule": "為什麼選它", "example": "成本最小"},
    "rejected_because": {"type": "清單（字串）",
                         "rule": "為什麼不要其他案；**要給清單**，給字串會被逐字元拆開",
                         "example": '["另一案要改到核心流程"]'},
    "countermeasure": {"type": "字串", "rule": "對策：要怎麼修、做完怎麼算解決",
                       "example": "改用方案甲重跑，並以 out.txt 的雜湊對上為準"},
    "checks": {"type": "清單（至少 1 條）",
               "rule": "每條 check 必須指向一條**已確認**需求",
               "example": '[{"id": "AC-01", "requirement_id": "R-001",'
                          ' "method": "跑測試", "scope": "全部", "threshold": "100%",'
                          ' "evidence": "out.txt", "required": true,'
                          ' "execution_kind": "test"}]'},
    "id": {"type": "字串", "rule": "這一條的編號（驗收結果要用同一個 id）",
           "example": "AC-01"},
    "requirement_id": {"type": "字串", "rule": "指向哪一條已確認需求",
                       "example": "R-001"},
    "method": {"type": "字串", "rule": "怎麼驗", "example": "跑測試"},
    "scope": {"type": "字串", "rule": "驗到哪裡（範圍）", "example": "全部"},
    "threshold": {"type": "字串", "rule": "通過門檻（**不得比上一輪低**）",
                  "example": "100%"},
    "evidence": {"type": "字串或物件", "rule": "證據指向哪個檔案／指令",
                 "example": "out.txt"},
    "required": {"type": "true／false", "rule": "是否必驗（**不得從 true 改成 false**）",
                 "example": "true"},
    "execution_kind": {"type": "test 或 review",
                       "rule": "執行的種類；純文件／人工檢視填 review",
                       "example": "test"},
    "outputs": {"type": "清單（至少 1 項）",
                "rule": "產物指紋：path／size／sha256 要跟現檔一致",
                "example": '[{"path": "out.txt", "size": 1024, "sha256": "3b1f…"}]'},
    "path": {"type": "字串",
             "rule": "**相對專案根**的路徑（不要用專案根外的絕對路徑）",
             "example": "reports/out.md"},
    "size": {"type": "數字", "rule": "檔案大小（bytes）", "example": "1024"},
    "sha256": {"type": "字串", "rule": "檔案的 SHA-256", "example": "3b1f…"},
    "done": {"type": "清單（字串）", "rule": "已做完成的事",
             "example": '["改完 workflow.py", "測試補到 254 項"]'},
    "unfinished": {"type": "清單（字串）",
                   "rule": "沒做完的事（**空清單也要交**，不能省略這個欄位）",
                   "example": "[]"},
    "selfcheck": {"type": "字串", "rule": "自檢結果（做了哪些驗證、結論是什麼）",
                  "example": "跑完 254 項測試全過"},
    "cases_ran": {"type": "數字",
                  "rule": "實際跑的案例數；純文件／人工檢視填 0 並自報 execution_kind=review",
                  "example": "12"},
    "results": {"type": "清單（至少 1 條）",
                "rule": "每條 check 都要有結果，id 要跟 checks 對得上",
                "example": '[{"id": "AC-01", "status": "PASS", "reason": "雜湊相符"}]'},
    "status": {"type": "PASS／FAIL／PARTIAL／NOT_RUN", "rule": "這一條的驗收結果",
               "example": "PASS"},
    "action": {"type": "close／rework／review",
               "rule": "這一棒要怎麼裁決", "example": "close"},
    "close_reason": {"type": "字串", "rule": "action=close 必填：結案理由",
                     "example": "R-001 的驗收已通過"},
    "rework_reason": {"type": "字串", "rule": "action=rework 必填：退回原因",
                      "example": "還差一項產出"},
    "rework_type": {"type": "施工錯／驗收錯／解法錯",
                    "rule": "action=rework 必填：錯在哪一環（英文 build／acceptance／solution 也接受）",
                    "example": "施工錯"},
    "diagnosis": {"type": "work／acceptance／solution／scope／resource",
                  "rule": "action=review 必填：問題的性質",
                  "example": "acceptance"},
    "remedy": {"type": "halt／revise",
               "rule": ("action=review 必填：怎麼處置——revise 只適用於 "
                        "diagnosis=acceptance／solution／scope；diagnosis=work／resource "
                        "只能填 halt（停等人裁決）"),
               "example": "revise"},
    "handoff": {"type": "物件（固定八欄）",
                "rule": "交接檔：八欄都要有（沒有的欄位寫「none」也算有）",
                "example": '{"ROLE": "WORKER", "OPEN WORK": "none", …}'},
    "trigger_kind": {"type": "work_blocked／needs_decision",
                     "rule": "ISSUE 的種類", "example": "work_blocked"},
    "problem": {"type": "字串", "rule": "什麼問題", "example": "同一個問題撞了三次"},
    "location": {"type": "字串", "rule": "發生在哪裡（檔案／步驟／階段）",
                 "example": "engine.py 的 _dispatch_pending"},
    "tried": {"type": "字串", "rule": "已經試過什麼", "example": "換過兩種做法"},
    "impact": {"type": "字串", "rule": "影響範圍", "example": "這張單卡住，後面三張都在等"},
    "question": {"type": "字串", "rule": "要誰決定什麼", "example": "要不要換目標"},
    "completion_criteria": {"type": "字串",
                            "rule": "達成什麼效果才算解決", "example": "重跑一次就過"},
    "resolution": {"type": "字串", "rule": "對策生效 → 結案說明",
                   "example": "改用方案甲後重跑通過"},
    "return_reason": {"type": "字串", "rule": "對策沒生效 → 附發生的情況退回",
                      "example": "還是沒解決，卡在同一個地方"},
}

#: 表單欄位 → 守衛的缺口代碼。讓「解說」跟守衛講的是**同一件事**。
FORM_GAP_CODES: Dict[str, str] = {
    "solutions": GAP_SOLUTIONS_COUNT,
    "design_note": GAP_DESIGN_NOTE_MISSING,
    "selection": GAP_SELECTION_MISSING,
    "checks": GAP_CHECKS_MISSING,
    "outputs": GAP_OUTPUTS_MISSING,
    "selfcheck": GAP_SELFCHECK_MISSING,
    "unfinished": GAP_UNFINISHED_INVALID,
    "results": GAP_RESULT_MISSING,
    "action": GAP_ACTION_MISSING,
    "handoff": GAP_HANDOFF_MISSING,
}

#: 清單項目裡**必填**的子欄位（其餘是附加項，缺了不算錯）。
ITEM_REQUIRED: Dict[str, List[str]] = {
    "solutions": list(SOLUTION_REQUIRED_FIELDS),
    "checks": ["id", "requirement_id", "method", "scope", "threshold", "evidence"],
    "outputs": ["path", "size", "sha256"],
    "results": ["id", "status"],
}

#: 清單項目裡缺欄位時的缺口代碼（跟守衛講同一個代碼）。
ITEM_GAP_CODES: Dict[str, str] = {
    "solutions": GAP_SOLUTION_FIELD_MISSING,
    "checks": GAP_CHECK_FIELD_MISSING,
    "outputs": GAP_OUTPUTS_MISSING,
    "results": GAP_RESULT_MISSING,
}

#: 物件項目裡**必填**的子欄位。
OBJECT_REQUIRED: Dict[str, List[str]] = {
    "selection": ["solution_id", "reason"],
}

#: 條件式必交欄位（依 action／結果而定，不該全部一起缺）。
#: `adjudicate`：action 決定要交哪一組；`adjudicating`（ISSUE）：二選一。
CONDITIONAL_FIELDS: Dict[str, Dict[str, List[str]]] = {
    "adjudicate": {"close": ["close_reason"],
                   "rework": ["rework_reason", "rework_type"],
                   "review": ["diagnosis", "remedy"]},
    "adjudicating": {"__any__": ["resolution", "return_reason"]},
}


def _typename(value: Any) -> str:
    """值的型別名（給角色看的）。"""
    if isinstance(value, bool):
        return "true／false"
    if isinstance(value, str):
        return "字串"
    if isinstance(value, (int, float)):
        return "數字"
    if isinstance(value, list):
        return "清單"
    if isinstance(value, dict):
        return "物件"
    return "空值"


def _is_empty(value: Any) -> bool:
    """沒有填（缺欄位、None、空字串、空清單、空物件）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def standard_of(field: str, spec: Optional[Dict[str, Any]] = None) -> str:
    """這個欄位的標準（型別＋規則）。"""
    known = FIELD_STANDARDS.get(field) or {}
    spec = spec or {}
    kind = known.get("type") or spec.get("kind") or ""
    rule = known.get("rule") or spec.get("note") or ""
    parts = [p for p in (kind, rule) if p]
    return "；".join(parts)


def example_of(field: str) -> str:
    """這個欄位的範例。"""
    return (FIELD_STANDARDS.get(field) or {}).get("example", "")


def _field_applies(stage: str, field: str, payload: Dict[str, Any]) -> bool:
    """這個必交欄位在這一筆 payload 裡**該不該出現**。

    條件式必交欄位（adjudicate 的 close_reason／rework_reason／…）
    只在對應的 action 下才檢查——不然會叫角色補一堆用不上的欄位。
    """
    rules = CONDITIONAL_FIELDS.get(stage)
    if not rules:
        return True
    if "__any__" in rules:
        # 二選一：兩個都沒給才算缺
        return True
    action = str(payload.get("action") or "").strip().lower()
    if action in rules:
        return field in rules[action]
    # action 還不合法 → 只檢查 action 本身，其他欄位等 action 對了再說
    return field == "action"


def _explain_detail(code: str, field: str, problem: str,
                    standard: str, example: str) -> Dict[str, str]:
    return {"code": code, "field": field, "problem": problem,
            "standard": standard, "example": example}


def explain_verdict(gap: str) -> Optional[Dict[str, str]]:
    """`verdict.*` 缺口的逐欄位解說（**不是表單欄位問題，是總判問題**）。

    角色交了 `action=close`，但程式算出的總判不是 pass → 說清楚：
    哪個欄位（action）、為什麼（總判由程式算）、標準是什麼（總判要 pass）。
    """
    if not gap.startswith("verdict."):
        return None
    verdict = gap.split(".", 1)[1]
    if verdict == "fail":
        problem = ("你交 action=close，但程式算出的總判是 fail"
                   "（有 required 的 check 是 FAIL）")
        standard = ("action=close 只有在**程式算出的總判是 pass** 時才成立；"
                    "總判只由程式算，不能自稱")
        example = "先讓所有 required check 真的 PASS，再交 action=close；或改交 action=rework 寫清楚還差什麼"
    elif verdict == "partial":
        problem = ("你交 action=close，但程式算出的總判是 partial"
                   "（還有 required 的 check 是 PARTIAL／NOT_RUN，或沒有 required 的 check）")
        standard = ("action=close 只有在**程式算出的總判是 pass** 時才成立；"
                    "總判只由程式算，不能自稱")
        example = "把沒跑或跑不成的 required check 補成 PASS（跑不成就自報 execution_kind=review）"
    else:
        return None
    return {"code": gap, "field": "action", "problem": problem,
            "standard": standard, "example": example}


def explain_form(stage: str, payload: Dict[str, Any],
                 issue: bool = False) -> List[Dict[str, str]]:
    """**逐欄位解說**：哪個欄位不標準、為什麼、標準是什麼、範例長什麼樣。

    **只檢查表單裡的欄位**——表單以外的欄位是**附加項**，
    引擎不看它們，也不會因為它們退件（使用者裁決：2026-11-24）。

    這是「解說」，不是守衛：真正的判定在 `check_stage`。
    兩者用**同一份**契約（`stage_contract`／`issue_stage_contract`）與
    同一份欄位標準（`FIELD_STANDARDS`），所以不會各講各的。
    """
    contract = issue_stage_contract(stage) if issue else stage_contract(stage)
    if not contract:
        return []
    specs = contract.get("field_specs") or {}
    details: List[Dict[str, str]] = []
    for field in list(contract.get("required") or []):
        if not _field_applies(stage, field, payload):
            continue
        spec = specs.get(field) or {}
        code = FORM_GAP_CODES.get(field, "%s.missing" % field)
        standard = standard_of(field, spec)
        example = example_of(field)
        value = payload.get(field)

        if _is_empty(value):
            problem = "缺漏（沒有這個欄位，或值是空的）"
            if stage == "adjudicating" and field in ("resolution", "return_reason"):
                problem = "缺漏（這一棒**二選一**：resolution 或 return_reason 給一個即可）"
            details.append(_explain_detail(code, field, problem, standard, example))
            continue

        kind = spec.get("kind")
        if kind == "list":
            if not isinstance(value, list):
                details.append(_explain_detail(
                    code, field, "格式不對：標準是清單，你給的是%s" % _typename(value),
                    standard, example))
                continue
            min_items = int(spec.get("min_items") or 0)
            if min_items and len(value) < min_items:
                details.append(_explain_detail(
                    code, field,
                    "項目數不足：標準至少 %d 項，你給了 %d 項" % (min_items, len(value)),
                    standard, example))
                continue
            item_code = ITEM_GAP_CODES.get(field, code)
            for index, item in enumerate(value):
                data = _as_dict(item)
                for sub in ITEM_REQUIRED.get(field, list(spec.get("item_fields") or [])):
                    if _is_empty(data.get(sub)):
                        details.append(_explain_detail(
                            item_code, "%s[%d].%s" % (field, index, sub),
                            "缺漏（這一項沒有這個欄位，或值是空的）",
                            standard_of(sub, {}), example_of(sub)))
        elif kind == "object":
            if not isinstance(value, dict):
                details.append(_explain_detail(
                    code, field,
                    "格式不對：標準是物件（key: value），你給的是%s" % _typename(value),
                    standard, example))
                continue
            for sub in OBJECT_REQUIRED.get(field, list(spec.get("item_fields") or [])):
                if _is_empty(value.get(sub)):
                    details.append(_explain_detail(
                        code, "%s.%s" % (field, sub), "缺漏（沒有這個欄位，或值是空的）",
                        standard_of(sub, {}), example_of(sub)))
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                details.append(_explain_detail(
                    code, field,
                    "格式不對：標準是數字，你給的是%s" % _typename(value),
                    standard, example))
        elif kind == "text":
            if not isinstance(value, str):
                details.append(_explain_detail(
                    code, field,
                    "格式不對：標準是字串，你給的是%s" % _typename(value),
                    standard, example))

    # **交接檔**：守衛查的是「有沒有」（規格第 9 節），所以只報缺漏本身。
    if contract.get("handoff_required") and not _as_dict(payload.get("handoff")):
        details.append(_explain_detail(
            GAP_HANDOFF_MISSING, "handoff", "缺漏（沒有交接檔，或交接檔不是物件）",
            standard_of("handoff", {}), example_of("handoff")))
    return details


# ---------------------------------------------------------------- 套用

def _store_solutions(conn: sqlite3.Connection, work_id: str,
                     solutions: List[Dict[str, Any]], design_note: Any,
                     round_no: int) -> None:
    """把一輪的解法寫進 `solutions` 表。

    **SUBAGENT 已取消（2026-11-23）**：不再有「每案必須來自不同 context」的
    約束，`subagent_context_id` 留著只是歷史欄位，沒帶就寫 NULL。
    """
    for sol in solutions:
        try:
            conn.execute(
                "INSERT INTO solutions(id, work_id, subagent_context_id, approach, cost,"
                " change_scope, effort, risk, verifiable_by, reuses, design_note, round,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (store.next_id(conn, "solutions", "SOL", 5), work_id,
                 sol["subagent_context_id"] or None, sol["approach"], sol["cost"],
                 sol["change_scope"], sol["effort"], sol["risk"], sol["verifiable_by"],
                 sol["reuses"], _text(design_note) or None, int(round_no), store.now()))
        except sqlite3.IntegrityError as exc:
            # **不要讓它炸掉整趟 tick**——退回要求重交就好。
            raise ValueError(
                "解法寫不進去（%s）：同一輪的解法必須是**不同的兩案**。"
                % sol.get("approach")) from exc


def apply_stage(conn: sqlite3.Connection, work: Dict[str, Any], stage: str,
                data: Dict[str, Any]) -> None:
    """把通過守衛的資料寫進正本。這裡只寫**正本欄位**，不決定下一狀態。"""
    work_id = work["id"]
    round_no = current_round(work)
    if stage == DESIGN:
        conn.execute("DELETE FROM solutions WHERE work_id = ?", (work_id,))
        _store_solutions(conn, work_id, data.get("solutions", []),
                         data.get("design_note"), round_no)

    elif stage == REWORK_DESIGN:
        # **重工解法另存一輪**——第一輪的案子要留著（那是「原本選了什麼」的證據），
        # 重工驗收方式設計時要選的是**這一輪**的案子。
        _store_solutions(conn, work_id, data.get("solutions", []), None, round_no)
        conn.execute(
            "UPDATE works SET rework_plan = ?, updated_at = ? WHERE id = ?",
            (json.dumps({"rework_plan": data.get("rework_plan")}, ensure_ascii=False),
             store.now(), work_id))

    elif stage in (ACCEPTANCE_DESIGN, REWORK_ACCEPTANCE_DESIGN):
        old = current_checks(conn, work_id)
        old_results = results_for(conn, work_id)
        new_checks = data.get("checks", [])
        new_ids = {c["id"] for c in new_checks}
        # 邊界：已有驗收結果的 check 不可刪
        for check_id in old:
            if check_id in old_results and check_id not in new_ids:
                raise ValueError("check %s 已有驗收結果，不可刪除" % check_id)
        # 邊界：門檻只能更嚴或等嚴；required 不可由 true 改 false
        for check in new_checks:
            previous = old.get(check["id"])
            if previous is None:
                continue
            if int(previous.get("required") or 0) == 1 and check["required"] == 0:
                raise ValueError("check %s 的 required 不可從 true 改成 false" % check["id"])
            if not _threshold_not_lowered(previous.get("threshold"), check["threshold"]):
                raise ValueError("check %s 的門檻不可放寬" % check["id"])
        conn.execute("DELETE FROM checks WHERE work_id = ?", (work_id,))
        for check in new_checks:
            conn.execute(
                "INSERT INTO checks(work_id, id, requirement_id, method, scope, threshold,"
                " evidence, required, execution_kind, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (work_id, check["id"], check["requirement_id"], check["method"],
                 check["scope"], check["threshold"], check["evidence"], check["required"],
                 check["execution_kind"], store.now()))
        selection = data.get("selection", {})
        conn.execute(
            "UPDATE works SET selection = ?, updated_at = ? WHERE id = ?",
            (json.dumps(selection, ensure_ascii=False), store.now(), work_id))

    elif stage in (BUILD, REWORK_BUILD):
        conn.execute(
            "UPDATE works SET build_result = ?, cases_ran = ?, execution_kind = ?,"
            " updated_at = ? WHERE id = ?",
            (json.dumps({"done": data.get("done"), "unfinished": data.get("unfinished"),
                         "selfcheck": data.get("selfcheck"),
                         "outputs": data.get("outputs")}, ensure_ascii=False),
             data.get("cases_ran"), _text(data.get("execution_kind")) or None,
             store.now(), work_id))

    elif stage == VERIFY:
        round_no = current_round(work)
        for item in data.get("results", []):
            evidence_id = None
            ev = item.get("evidence") or {}
            if ev.get("path"):
                evidence_id = store.next_id(conn, "evidence", "E", 6)
                conn.execute(
                    "INSERT INTO evidence(id, project_relative_path, size, sha256, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (evidence_id, ev.get("path"), int(ev.get("size") or 0),
                     ev.get("sha256") or "", store.now()))
            conn.execute(
                "INSERT OR REPLACE INTO results(work_id, check_id, round, status, reason,"
                " evidence_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (work_id, item["id"], round_no, item["status"], item["reason"],
                 evidence_id, store.now()))

    elif stage == ADJUDICATE:
        if data.get("action") == "close":
            conn.execute("UPDATE works SET verdict = ?, updated_at = ? WHERE id = ?",
                         (compute_verdict(conn, work_id), store.now(), work_id))
        else:
            # **寫 payload 的值**（不是讀 work 裡的舊值）。
            # 原本是 `(_text(work.get("rework_reason")) or None, work.get("rework_type"), ...)`
            # ——回讀自己舊的（永遠是 None）→ PLANNER 填的退回原因根本沒進正本，
            # THINKER 收到 rework_design 時看不到「為什麼被退回」。
            conn.execute(
                "UPDATE works SET rework_reason = ?, rework_type = ?, updated_at = ?"
                " WHERE id = ?",
                (_text(data.get("rework_reason")) or None,
                 _text(data.get("rework_type")) or None,
                 store.now(), work_id))


# ---------------------------------------------------------------- 門檻比較

#: 門檻比較用的**封閉文法**。
#:
#: 規格第 5 節：修改驗收方式時不得降低門檻。要判斷「有沒有被放寬」，
#: 門檻必須**可解析**——這裡把它拆成一組可比較的「要求條款」。
#: **拆不動就當作無法判定 → 擋**（寧可擋，不可放水）。
#:
#: 用意：不再靠一份會漏字的「放寬字樣清單」判斷。
#: `at least 95% or manual override`、`at least 95% is optional`、
#: `at least 95% best effort` 都不符合任何一條款 → 全部擋下；
#: 沒見過的寫法同樣擋（因為它一樣解析不出條款）。

#: 數字門檻的下限詞（決定鬆緊方向）。
_THRESHOLD_MIN_PREFIXES = ("at least", "no less than", "at minimum", "minimum",
                           "至少", "最少", "≥", ">=")
#: 數字門檻後面只准接這些收尾（都屬於**加嚴**或同義）。
_THRESHOLD_SUFFIXES = ("必須附證據", "需附證據", "附證據", "with evidence",
                       "附測試紀錄", "with test record", "以上", "覆蓋率", "通過率")
#: 通用要求：本身即強制（後面接什麼都不會讓它變鬆）。
_THRESHOLD_UNIVERSALS = ("must not ", "must ", "every ", "each ", "all ", "no ",
                         "不得", "不可", "禁止", "必須", "全數", "全部",
                         "每個", "每條", "所有", "一律", "均需")
#: 放行字樣：出現就代表「門檻可以不走原來那條路」。
#: 「例外」要避開「無例外」「不例外」——那是**收緊**不是放寬（實測門檻寫法
#: 「逐列 CSV 核對無例外」被誤判成放寬，整批卡在 rework_acceptance_design）。
_THRESHOLD_LOOSENERS = (r"\bor\b", "或", r"optional", "可選", r"不必", "豁免", "覆寫",
                        r"override", r"waiv", r"exempt", r"best effort", "盡力",
                        "酌情", "視情況", "非強制", "不要求", r"permit", "允許",
                        "可略過", r"\bexcept\b", r"\bunless\b", r"\bif\b",
                        "除外", r"(?<!不)(?<!無)例外", "除非")

#: 一條數字門檻的文法：〔下限詞〕數字〔單位〕〔收尾〕。**後面不准有多餘的字。**
_THRESHOLD_NUMBER_RE = re.compile(
    r"^(?:(?P<min>at least|no less than|at minimum|minimum|至少|最少|≥|>=)\s*)?"
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|percent|percentage|案例|個|次|項|cases?|tests?)?$",
    re.IGNORECASE)


def _threshold_has_loosener(text: str) -> bool:
    """這段文字裡有沒有「可以不走原路」的放行字樣。"""
    lowered = text.lower()
    for pattern in _THRESHOLD_LOOSENERS:
        if re.search(pattern, lowered):
            return True
    return False


def _strip_threshold_suffix(text: str) -> Tuple[str, bool]:
    """去掉允許的收尾（證據要求、「以上」之類）。回傳 (剩下的, 是否帶「以上」)。"""
    body = text
    at_least = False
    for _ in range(2):
        lowered = body.lower()
        for suffix in sorted(_THRESHOLD_SUFFIXES, key=len, reverse=True):
            if suffix and lowered.endswith(suffix):
                body = body[:-len(suffix)].strip()
                if suffix == "以上":
                    at_least = True
                break
        else:
            break
    return body, at_least


def _parse_threshold_clause(clause: str) -> Optional[Dict[str, Any]]:
    """把一條門檻條款解析成可比較的結構；解析不了回 None（= 無法判定）。"""
    text = " ".join(clause.split())
    if not text:
        return None
    if _threshold_has_loosener(text):
        return None
    body, at_least = _strip_threshold_suffix(text)
    if not body:
        return None
    lowered = body.lower()
    for marker in _THRESHOLD_UNIVERSALS:
        if lowered.startswith(marker):
            return {"kind": "universal", "text": text}
    match = _THRESHOLD_NUMBER_RE.match(body)
    if match is None:
        return None
    unit = (match.group("unit") or "").lower()
    # 裸百分比門檻語意上就是下限（`95%`＝至少 95%），要跟 `95% 以上` 同義。
    inherently_min = unit in ("%", "percent", "percentage")
    return {"kind": "number",
            "value": float(match.group("value")),
            "unit": unit,
            "min": bool(match.group("min")) or at_least or inherently_min,
            "text": text}


def _threshold_clauses(text: str) -> Optional[List[Dict[str, Any]]]:
    """把門檻拆成一組可判定的條款；拆不動回 None。

    **只按「而且」切，不按「或」切**——「二選一」不是單一要求，
    留在同一條裡就會因為解析不出條款而被擋下。

    **分隔符要涵蓋中文寫法**：`；`／`;`／`，`／`、`／`, 都切。
    （實測：角色用 `;` 或 `，` 串多條要求，引擎沒切 → 整條解析失敗 → 誤判門檻被降低。）
    """
    parts = re.split(r"(?<![A-Za-z])\band\b(?![A-Za-z])|且|並且|以及|[、,，；;]",
                     text, flags=re.IGNORECASE)
    clauses: List[Dict[str, Any]] = []
    for part in parts:
        parsed = _parse_threshold_clause(part)
        if parsed is None:
            return None
        clauses.append(parsed)
    return clauses or None


def _clause_covered(clause: Dict[str, Any],
                    new_clauses: List[Dict[str, Any]]) -> bool:
    """新門檻有沒有涵蓋這一條舊條款（而且沒有變鬆）。"""
    if clause["kind"] == "universal":
        return any(c["kind"] == "universal" for c in new_clauses)
    for candidate in new_clauses:
        if candidate["kind"] != "number":
            continue
        if candidate["unit"] != clause["unit"] or candidate["min"] != clause["min"]:
            continue
        if candidate["value"] >= clause["value"]:
            return True
    return False


def _threshold_split_clauses(text: str) -> List[str]:
    """把門檻依分隔符切成條款（**不判斷語意**）。"""
    parts = re.split(r"(?<![A-Za-z])\band\b(?![A-Za-z])|且|並且|以及|[、,，；;]",
                     text, flags=re.IGNORECASE)
    return [p for p in (" ".join(x.split()) for x in parts) if p]


#: 百分比數字（用來擋「100% → 100%…80%」這種藏在文字裡的放寬）。
_THRESHOLD_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _threshold_clause_text_covers(old_clause: str, new_clause: str) -> bool:
    """敘述型條款的涵蓋判定：**新條款保留舊條款原文**＝只加嚴。

    - 舊條款原文（去空白）必須出現在新條款裡 → 舊要求還在；
    - 舊條款有百分比時，新條款**不得出現更小的百分比**
      （擋「100% 卡片五欄齊全」→「100% 卡片五欄齊全率 80%」）。
    """
    old_core = re.sub(r"\s+", "", old_clause)
    new_core = re.sub(r"\s+", "", new_clause)
    if not old_core:
        return True
    if old_core not in new_core:
        return False
    old_percents = [float(m.group(1)) for m in _THRESHOLD_PERCENT_RE.finditer(old_clause)]
    if old_percents:
        ceiling = max(old_percents)
        new_percents = [float(m.group(1)) for m in _THRESHOLD_PERCENT_RE.finditer(new_clause)]
        if any(p < ceiling for p in new_percents):
            return False
    return True


def _threshold_text_contains(old_text: str, new_text: str) -> bool:
    """敘述型門檻（拆不成可判定條款）的判定。

    **為什麼需要這條**：敘述型中文門檻（「平衡等式成立」「矛盾=0 且 未確認=0」）
    拆不成可比較的數字條款 → 舊邏輯一律判「門檻被降低」→ 角色**加嚴也過不了**
    （實測 2026-11-24 目錄拆產品圖3：13 次退件，最後只能把門檻改回原樣，
    等於加嚴白做）。

    規則（保守）：
    - 舊門檻的**每一條**都必須被新門檻的某一條涵蓋（文字保留判定）；
    - 新門檻**不得出現放行字樣**（或／可選／豁免…）→ 那不是加嚴，是放寬；
    - 其餘情況（舊條款不見了）→ 擋。
    """
    if _threshold_has_loosener(new_text):
        return False
    old_clauses = _threshold_split_clauses(old_text)
    new_clauses = _threshold_split_clauses(new_text)
    for old_clause in old_clauses:
        if not any(_threshold_clause_text_covers(old_clause, c) for c in new_clauses):
            return False
    return True


def _threshold_not_lowered(old: Any, new: Any) -> bool:
    """門檻有沒有被放寬。

    規則（規格第 5 節：修改驗收方式時不得降低門檻）：
    - 舊門檻是數字、新門檻是數字 → 新值必須 ≥ 舊值。
    - 兩邊都拆得出**可判定的要求條款** → 新門檻必須涵蓋舊門檻的每一條
      （數字不得更小、通用要求不得消失）。
    - **拆不出條款**（敘述型中文門檻）→ 改用文字判定：新門檻保留舊門檻原文
      且沒有放行字樣＝只加嚴 → 放行；否則擋。
      `at least 95% or manual override`、`at least 95% is optional` 這類寫法
      含放行字樣 → 擋（寧可擋，不可放水）。
    - 舊門檻為空 → 視為沒有門檻，不擋。
    """
    old_text = _text(old)
    new_text = _text(new)
    if not old_text:
        return True
    if old_text == new_text:
        return True
    old_num = _num(old_text)
    new_num = _num(new_text)
    if old_num is not None and new_num is not None:
        return new_num >= old_num
    old_clauses = _threshold_clauses(old_text)
    new_clauses = _threshold_clauses(new_text)
    if old_clauses is not None and new_clauses is not None:
        return all(_clause_covered(clause, new_clauses) for clause in old_clauses)
    return _threshold_text_contains(old_text, new_text)


# ---------------------------------------------------------------- 轉移

def next_state_for(conn: sqlite3.Connection, work: Dict[str, Any],
                   stage: str, data: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """算出套用之後的下一個狀態。回傳 (state, 額外的工單欄位更新)。"""
    if stage == ADJUDICATE:
        action = data.get("action")
        if action == "close":
            return CLOSED, {}
        if action == "rework":
            return REWORK_DESIGN, {"rework_count": int(work.get("rework_count") or 0) + 1}
        # review
        remedy = _text(data.get("remedy")).lower()
        diagnosis = _text(data.get("diagnosis")).lower()
        if remedy == REMEDY_HALT:
            return None, {"awaiting_human": 1, "awaiting_reason": "rework_limit"}
        if diagnosis == "solution":
            return SOLUTION_REWORK_TARGET, {
                "revision_count": int(work.get("revision_count") or 0) + 1,
                "rework_count": 0,
            }
        # acceptance / scope → 回到驗收方式設計
        return ACCEPTANCE_REWORK_TARGET, {
            "revision_count": int(work.get("revision_count") or 0) + 1,
            "rework_count": 0,
        }
    target = WORK_STATES.get(stage, {}).get("next")
    return target, {}


def record_handoff(conn: sqlite3.Connection, work_id: str, from_state: str,
                   to_state: str, from_role: str, to_role: str,
                   handoff: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO handoffs(work_id, from_state, to_state, from_role, to_role, payload,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (work_id, from_state, to_state, from_role, to_role,
         json.dumps(handoff, ensure_ascii=False), store.now()))


# ---------------------------------------------------------------- 提示詞素材

def stage_contract(stage: str) -> Dict[str, Any]:
    """交件契約（提示詞的唯一來源）。

    **包含欄位規格與交接檔**——派工提示詞要靠它把「這一棒要交什麼、每個欄位長什麼樣」
    講清楚。實測：只給欄位名稱，agent 交不出來，整批提交被守衛退回。
    """
    state = WORK_STATES.get(stage)
    if state is None:
        return {}
    handoff_required = bool(state.get("handoff_required"))
    return {
        "stage": stage,
        "title": state["title"],
        "role": state["role"],
        "required": list(state["required"]),
        "next": state["next"],
        "field_specs": STAGE_FIELD_SPECS.get(stage, {}),
        "handoff_required": handoff_required,
        "handoff_fields": list(HANDOFF_FIELDS) if handoff_required else [],
        # 「這幾欄不是全部都要給」的說明（ISSUE 早就有，工單側補齊）——
        # 沒有它，agent 會以為六欄全都要填實值。
        "required_note": state.get("required_note", ""),
    }


def issue_stage_contract(state: str) -> Dict[str, Any]:
    """ISSUE 某一棒的交件契約（跟 `stage_contract` 同一個形狀）。

    **守衛與檔案層共用這一份**：`engine._check_issue_stage` 看的就是
    `ISSUE_STATES[state]["required"]` 與 `ISSUE_FIELD_SPECS`——
    提示詞／ISSUE 檔案若寫另一份，角色照著交就會被退件。
    """
    spec = ISSUE_STATES.get(state)
    if spec is None:
        return {}
    return {
        "stage": state,
        "title": spec["title"],
        "role": spec.get("role"),
        "required": list(spec.get("required") or []),
        "next": spec.get("next"),
        "field_specs": ISSUE_FIELD_SPECS.get(state, {}),
        "handoff_required": False,
        "handoff_fields": [],
        "required_note": spec.get("required_note", ""),
    }


def last_rejection(conn: sqlite3.Connection, kind: str,
                   target_id: str) -> Dict[str, Any]:
    """最近一次退件（退件之後還沒重交並通過的）——**結構化**。

    **為什麼要這一段**：角色讀不到上一輪的對話，退件又不會進它的畫面——
    沒有這段，它會以為自己還沒交過，然後交出同樣的東西。

    **提示詞與 `fbflow_read` 共用這一份**（同一件事不要兩套說法）。
    沒有退件就回空 dict。
    """
    column = "work_id" if kind == "work" else "issue_id"
    row = conn.execute(
        "SELECT id, stage, gaps FROM submissions"
        " WHERE %s = ? AND status = 'rejected' ORDER BY id DESC LIMIT 1" % column,
        (target_id,)).fetchone()
    if row is None:
        return {}
    later = conn.execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE %s = ? AND status = 'applied'"
        " AND id > ?" % column, (target_id, row["id"])).fetchone()["n"]
    if later:
        return {}   # 退件之後已經重交並通過 → 不用再提
    try:
        gaps = json.loads(row["gaps"] or "[]")
    except (TypeError, ValueError):
        gaps = []
    detail = ""
    gap_details: List[Dict[str, Any]] = []
    decision = conn.execute(
        "SELECT detail FROM decisions WHERE submission_id = ? AND action = 'rejected'"
        " ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    if decision is not None:
        try:
            payload = json.loads(decision["detail"] or "{}") or {}
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            detail = payload.get("error", "")
            raw_details = payload.get("gap_details") or []
            if isinstance(raw_details, list):
                gap_details = [d for d in raw_details if isinstance(d, dict)]
    return {"submission_id": row["id"], "stage": row["stage"],
            "gaps": gaps, "error": detail, "gap_details": gap_details}


def contract_table() -> List[Dict[str, Any]]:
    """全部狀態的交件契約（給提示詞渲染與文件產出用）。"""
    return [stage_contract(stage) for stage in WORK_STATES]
