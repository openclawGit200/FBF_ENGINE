---
name: FBF_ENGINE_MCP
description: 以可恢復交付為核心的多 session 工作流引擎。透過 MCP 工具讓 PLANNER／THINKER／WORKER 三個角色接力完成一張工單，狀態只由協調器推進、可信 run 只由核對過的平台回執建立，重啟不丟提交也不重複推進。適用於需要多角色接力、每一步都可由人核對的工程工作；不適用於單一 session 就能完成的小任務，也不承載一般聊天。
version: r1
---

# FBF ENGINE（MCP 介面）

這份 SKILL 是**引擎的對外契約**。Session 透過 MCP 工具參與流程，
**不碰 CLI、不碰資料庫**。規則正本在 `spec/FBF ENGINE工作流.md`
（`doc/spec-fbflow-v2-first-principles.md` 是舊的工程參考稿）。

## 新專案怎麼加入 {#stage-planner-join}

**你在一個新專案裡，第一個動作是 `fbflow_project_join`**（還沒加入時）。

| 情况 | 你该做什么 |
|---|---|
| 工具回「這個 MCP 連線沒有綁任何專案」 | 在**專案的目錄**下呼叫 `fbflow_project_join`（不給路徑就用目前工作目錄） |
| 已經加入過了 | 直接 `fbflow_register`；`fbflow_project_join` 再呼叫一次也**只是補缺的部分**，不會洗掉既有資料 |

`fbflow_project_join` 做四件事：建正本（`fbf_engine.db`）、建三个角色席位
（PLANNER／P1、THINKER／T1、WORKER／W1）、把 MCP 寫進該專案的設定檔
（`opencode.json` 與專案層 `.mcp.json`，帶 `FBF_PROJECT_ROOT`）、
並把**這個 session** 綁到該專案。

**呼叫之後不用重開連線**——其他工具立刻就能用。

**護欄**：只能加入**目前工作目錄或它的子目錄**；引擎的安裝目錄不存放專案資料，
在那裡呼叫會被明確拒絕。

**規則在哪**：動工前先讀引擎的 `SKILL.md`（本檔）與
`spec/FBF ENGINE工作流.md`。派工提示詞裡會帶上這兩條路徑——
**不用自己去翻程式碼，也不用問人**。

## 需求變了怎麼辦 {#stage-planner-change}

**已發出的工單不要改**（改已發出的工單太麻煩：版本、退件、重工、交接全部要重來）。
需求或做法變了，正確做法是**照新需求開新工單**。

| 步驟 | 誰 | 動作 |
|---|---|---|
| 1 | **人** | 需求本身變了 → **新增**一條需求（`fbf.py requirement confirm --text ... --criterion ... --supersedes <舊需求 id>`）。已確認的需求不可修改——新舊都留著 |
| 2 | PLANNER | 照新需求開新工單（`fbflow_work_create`），新工單掛新需求 |
| 3 | PLANNER | 把舊工單標成已取代（`fbflow_work_supersede`）：**舊工單的內容與狀態不動**，只是標記、停工、不再派工 |

**舊工單標成已取代之後**：它開著的 task 會被取消（角色不會再做一張作廢的單）、
之後不再被派工、交件也會被擋（回 `WORK-SUPERSEDED`）。
`fbflow_status` 的 `superseded_works` 會列出它們——**看得到，才不會以為它還在跑**。

**護欄**：不能取代已結案的工單（結案是歷史）、取代者必須是剛開的新工單、
一定要說明原因、只有 PLANNER 能做。

**舊需求的工單也要一起標**：需求被新需求取代後，結案追溯會檢查
「被取代需求底下的工單是否也標成已取代」——沒標的話，舊單還會被派工做白工，
專案也結不了案。

## 範圍 {#stage-planner-scope}

本 skill 處理：**把一件人類確認過的任務，交給多個 session 接力做完，交回可由人核對的成果。**

- 本 skill 處理：需求確認 → 分解 → 派工 → 施工 → 獨立驗收 → 結案；卡住時的裁決出口。
- 明確不做：**領域知識**（那是專案自帶的 domain skill）、**通用聊天**、**人類的專案初始化**。
- 不適用：單一 session 就能做完、也不需要交接與驗收的小事。

## 交付物 {#stage-planner-deliverable}

一張工單走完十個狀態，最後產出：

| 產出 | 內容 | 落在哪 |
|---|---|---|
| 產物指紋 | 路徑＋大小＋SHA-256 | `works.build_result` |
| 驗收結果 | 每條 check 的 PASS／FAIL／PARTIAL／NOT_RUN＋理由 | `results` |
| 證據 | 路徑＋大小＋SHA-256 | `evidence` |
| 總判 | `pass`／`fail`／`partial`（**程式算**） | `works.verdict` |
| 交接 | 每棒八欄 | `handoffs` |
| 軌跡 | 每一次裁決與狀態轉移 | `decisions`／`journal` |

## 分解維度 {#stage-planner-decompose}

PLANNER 只能從這幾個面向挑，**不得自創**：

- **F-01 需求**：人類確認過的要求，含可觀察的成功標準。
- **F-02 解法**：至少兩案解法，由 THINKER 在同一棒**直接產出**（不分派 SUBAGENT），兩案不得實質相同。
- **F-03 驗收**：每條 check 必須指向一條已確認需求。
- **F-04 施工**：依需求／解法／驗收方式做出產物，並留下指紋與案例數。
- **F-05 獨立驗收**：由沒做過本工單施工的 session 逐條驗。
- **F-06 裁決**：結案或退回；退回要寫原因與類型。
- **F-07 里程碑**：一段任務的成果與對應工單，段末對照。
- **F-08 卡住**：ISSUE——目前工單無法往前，或需要 PLANNER 裁決。

**拆解信號**（任一成立就拆）：超過一個專注時段、驗收條件寫不出三條以內、
跨到兩個以上獨立子系統、標題裡出現「而且」。

## 驗收方式 {#stage-planner-acceptance}

| 規則 id | 判什麼 | 精確預期 | 執行方式 | 證據 |
|---|---|---|---|---|
| AC-EVIDENCE | 產物存在且未被竄改 | 大小與 SHA-256 與現檔相符 | 程式重算 | `evidence` 表 |
| AC-CASES | 真的跑過案例 | `cases_ran ≥ 1` | 角色自報並附紀錄 | `works.cases_ran` |
| AC-REVIEW | 純文件／人工檢視 | `execution_kind=review` 且附審閱紀錄 | 角色附上審閱內容 | `results.reason` |
| AC-REQUIRED | 必要條件全過 | 所有 `required=true` 的 check 都 PASS | 程式算總判 | `works.verdict` |
| AC-TRACE | 需求被涵蓋 | 每條已確認需求都有結案工單與通過證據 | 結案時雙向查核 | `decisions` |
| AC-SELF | 不是自己驗自己 | 驗收 session ≠ 本工單施工 session | 程式比對 task 指派 | 守衛 |

**門檻只能更嚴**：修訂驗收方式時門檻不得放寬、`required` 不可由 true 改 false、
已有驗收結果的 check 不可刪、需求的 `success_criterion` 不可由 PLANNER 改。

## 規則全集 {#stage-thinker-rules}

### 收錄條件
- 需求已由人類確認。
- 至少**兩案**解法，由 THINKER 在同一棒**直接產出**（不分派 SUBAGENT），且兩案不得實質相同。
- 每條 check 指向一條已確認需求。
- 總判由程式算，不採信模型自稱。

### 一律排除
- 不得自己改驗收方式。
- 不得自做自驗。
- 不得在沒有平台回執時宣稱「跑過了」。
- 不得修改需求的成功標準。

## 已知失敗模式 {#stage-thinker-failures}

| 失效 | 怎麼認出來 | 怎麼處理 |
|---|---|---|
| 假綠燈 | 報告「通過」但 `cases_ran = 0` | 守衛擋下（`cases_ran.zero`） |
| 證據對不上 | 提交的大小／雜湊與現檔不同 | 程式重算（`evidence.hash_mismatch`） |
| 兩案其實同一案 | approach／cost／change_scope 全一樣 | 擋下（`solution.too_similar`） |
| 解法欄位漏填 | 少 approach／cost／change_scope／effort／risk／verifiable_by | 擋下（`solution.field_missing`） |
| **同一個 message_id 交兩次** | 第二筆用同一個平台訊息 | 擋下（`receipt.message_reused`）；重交要用**新的** message_id |
| **回執查不到就當完成** | 只報「做完了」沒有平台訊息 | `RECEIPT-PENDING`，**不要重交一份** |
| 自我放水 | 修訂時門檻變鬆 | 擋下（`check.threshold_lowered`） |
| 拿舊數字做決定 | 交接檔裡的門檻已過期 | 交接必含「目前有效的門檻」 |
| 交付重複套用 | 同一 submission 送兩次 | 冪等鍵擋下，只套用一次 |
| 送達不明就重送 | 送出結果不明 | 不重送，先查 task／session |

## 可調邊界 {#stage-thinker-boundary}

### 紅線（AI 不得調整）
- 需求的 `success_criterion`。
- **已確認需求的內容**——需求變了要**新增**一條（由人確認），不是改舊的。
- **已發出的工單**——需求或做法變了要**開新工單**，不是改舊單。
- 任何 `required=true` 的 check。
- 已有驗收結果的 check。
- 門檻（只可更嚴）。
- 重工上限（2）、修訂上限（2）、退回上限（3）。

### 可調參數（可提建議，人核定後生效）
- 工單大小與拆法。
- 里程碑分段點。
- 派工優先序（預設：未完成工單最少 → 最久沒被指派 → session ID 字典序）。

## 執行步驟 {#stage-worker-steps}

1. **讀正本**：`fbflow_read(target_kind="work", target_id=...)` 與 `fbflow_status()`。
2. **讀交接**：交接檔八欄，特別是「RULES AND NUMBERS IN FORCE」——**門檻可能已變，不要用舊值**。
3. **照交件清單做**：這一棒要交的欄位由狀態決定（見下）。
4. **自檢**：照驗收方式跑過，記錄案例數與產物指紋。
5. **交件**：`fbflow_report_work(task_id, work_id, expected_version, stage, payload, message_id)`。
6. **別自己改狀態**：交完就停，狀態由協調器推進。

**設計階段（`design`）的額外要求**：
每棒至少交**兩案**解法，由你**在同一棒直接產出**——**不分派 SUBAGENT**、
**不帶 `subagent_context_id`**、引擎也**不回平台核對 context**。
兩案不得實質相同：**成本與修改範圍不能都一樣**（否則退回 `solution.too_similar`）；
解法欄位缺一項就退回（`solution.field_missing`）。
**案號＝你交件清單裡的順序（1 起算）**；下一棒 PLANNER 在 `selection.solution_id` 填案號。

**每次交件的 `message_id` 用你自己知道的唯一值即可**——你**看不到**平台
給訊息編的 id，回執核對的關聯鍵是 `task_id`（派工提示詞裡帶給你那個）。
同一個 message_id 被另一筆提交用過，會被判 `receipt.message_reused` 退回。
被退回時版本會 +1，**重新讀正本、拿新的版本與新的 message_id 再交**。
回執等不到平台核對時不會無限重查：到期會**作廢本棒 task、換新 task 重派**；
重試達上限才進待人裁決（`awaiting list`）。

**各狀態的必交欄位**（這份清單來自程式，提示詞不要另寫一份）：

| 狀態 | 角色 | 必交 |
|---|---|---|
| `design` | THINKER | `solutions[]`（≥2 案，**THINKER 直接產出**）＋`design_note` |
| `acceptance_design` | PLANNER | `selection`（選案理由＋**案號**）＋`checks[]`（每條指向需求） |
| `build` / `rework_build` | WORKER | `outputs[]`（指紋）＋`done`＋`unfinished`＋`selfcheck`＋`cases_ran` |
| `verify` | THINKER | `results[]`（每條 check 一列）＋`cases_ran` |
| `adjudicate` | PLANNER | `action`（close／rework／review）＋依 action 選填：`close_reason`／`rework_reason`＋`rework_type`／`diagnosis`＋`remedy`（**至少各要有一組；其餘交空字串即可**） |
| `rework_design` | THINKER | `solutions[]`（≥2 案、**THINKER 一棒直接產出**）＋`rework_plan`（要引用退回原因） |
| `rework_acceptance_design` | PLANNER | `selection`（**這一輪**的解案，填**案號**）＋`checks[]`（門檻只能更嚴） |
| `milestone_review` | PLANNER | `action` ∈ `continue`／`add_work`／`await_human` |

**每個狀態都要附 `handoff`（八欄）**，沒有就會被守衛退件。

派工提示詞列出這一棒的交件欄位與短規則。工作狀態與詳細契約從 `fbflow_read` 讀取。
`fbflow_report_work` 的 `stage` 可留空，引擎依 `task_id` 判定。

## 沒有工作可以做時的收合提醒 {#stage-worker-compact}

引擎會在**你沒有待交的工作**、而且**離上一次 compact 超過半小時**時，
送一段以 `/compact` 開頭的訊息給你——那是引擎叫你收合上下文。

**怎麼判定「沒有工作可以做」**：看**單據狀態**，不是看任務數。

1. 你有開著的 task → 有工作，引擎不會叫你。
2. 有單據正處於「等你這個角色做」的狀態、而且**還沒有人做** → 你等一下就有事做，引擎不會叫你。
3. 都沒有 → 你現在真的沒事做（單據都在別人手上、或都結束了）。

**還有一道「閒置落定期」**：剛交完件、剛被派過單的 session **不會**被叫——
你只是暫時沒有開著的 task，不代表你閒。引擎要等閒置滿 10 分鐘才會提醒。

**收到之後**：依提示詞第一行的指令收合（預設 `/compact`），
然後**結束本輪回覆**——不要 sleep、不要輪詢、不要自己開新工作。之後有派工會再叫你。

**兩種收合方式**（由專案設定決定，引擎會寫在提示詞裡）：

- **`/clear`（reset，預設）**：**清空目前對話、session 本身不變**。
  清空**不會**改掉你的 session 設定（模型、權限、工具都留著），
  席位也還在——引擎認的是這個 session，不是剛才那段對話。
  若清空後你發現拿不到席位或 UUID 不認了，再重新呼叫
  `fbflow_register(role=<你的角色>, short_name=<你的簡稱>)`。
- **`/compact`**：把目前對話壓縮成摘要，保留任務脈絡。
  某個 agent 沒有清空指令時（OpenCode 沒有 `/clear`）會退回這種做法。

  ⚠️ 引擎**不會**叫你開新 session（`/new`）：那會讓你這個 session
  專屬的模型與設定全部不見。

## 資料格式 {#stage-worker-schema}

`payload` 是一個物件，欄位依狀態而定。共用形狀：

```jsonc
{
  "handoff": {
    "ROLE": "...", "OPEN WORK": "...", "SUBAGENTS RUNNING": "...",
    "RULES AND NUMBERS IN FORCE": "...", "RECIPES": "...",
    "DECISIONS AND WHY": "...", "LESSONS": "...", "SPEND": "..."
  }
}
```

產物與證據：

```jsonc
{ "path": "專案根內的相對路徑", "size": 1234, "sha256": "64碼十六進位" }
```

解法（每案六欄，**不帶 `subagent_context_id`**；案號＝清單順序，1 起算）：

```jsonc
{
  "approach": "...", "cost": "...",
  "change_scope": "...", "effort": "...", "risk": "...",
  "verifiable_by": "...", "reuses": "..."
}
```

驗收方式：

```jsonc
{
  "id": "AC-01", "requirement_id": "R-001", "method": "...",
  "scope": "...", "threshold": "...", "evidence": "...",
  "required": true, "execution_kind": "test"
}
```

## 自檢方式 {#stage-worker-selfcheck}

交件前自己確認這五件事：

1. 這一招的必交欄位**每一個**都填了。
2. 產物的路徑在專案根內，指紋是現算的（不是你記得的）。
3. 案例數是**真的跑過的數量**。跑不了就把 `execution_kind` 設成 `review` 並附審閱紀錄，
   **不要**把 0 說成 3。
4. 交接檔八欄都填了，「RULES AND NUMBERS IN FORCE」寫的是**目前**有效的值。
5. `expected_version` 是你**剛剛讀到**的版本（不是記憶中的）。

## 停止條件 {#stage-worker-stop}

- **同一個問題連撞三次** → 停下來發 ISSUE（`fbflow_issue_create`），不要撞第四次。
- **需要 PLANNER 裁決** → 發 ISSUE（`trigger_kind="needs_decision"`）。
- **提交被退件** → 照 `gaps` 補件後**重新讀正本**再交一次。
  退件會讓工單版本 +1，用舊版本重交一定會被判 `TASK-STALE`。
- **回執未到**（`RECEIPT-PENDING`）→ **不要重交一份**，等下一輪 tick 重查。
  到期沒核過會**作廢本棒 task、換新 task 重派**（提示詞會帶說明）；
  重試達上限工單才會進待人裁決。
- **收到 `G-SESSION-UNVERIFIED`** → 這不是你的問題，是 host 綁定沒設好；回報給人。

## 驗收判準 {#stage-acceptance-criteria}

驗收（`verify` 狀態）由 **THINKER** 做，而且**不可驗自己在本工單的施工**。

- 判定標準：每條 check 給 PASS／FAIL／PARTIAL／NOT_RUN 之一，**不給「大概可以」**。
- 證據要求：每個結果都要能追到檔案（路徑＋大小＋SHA-256）。
- 每輪結果格式：`{"id": "AC-01", "status": "PASS", "reason": "...", "evidence": {...}}`。
- 規則版本：依 `checks` 表當下的門檻判定；門檻變了就重新讀。

**總判是程式算的**——THINKER 說 PASS 不等於能結案。

---

## 附錄：工具清單

所有工具回覆同一外殼：成功 `{"ok":true,"data":{...}}`，
失敗 `{"ok":false,"code":"...","message":"...","details":{...}}`。

| 工具 | 誰能呼叫 | 用途 |
|---|---|---|
| `fbflow_project_join` | 任何 session | **新專案的第一個動作**：把目前專案加入引擎（建正本、席位、設定檔，並把自己綁上去） |
| `fbflow_register` | 任何 session | **接著呼叫的工具**：綁角色席位、取回私有提示詞（含專屬 UUID） |
| `fbflow_read` | 已註冊 session | 讀工單／ISSUE／里程碑正本 |
| `fbflow_report_work` | task 指派者 | 交工單這一棒（**不轉狀態**） |
| `fbflow_report_issue` | task 指派者 | 交 ISSUE 這一棒 |
| `fbflow_report_milestone` | 里程碑檢視的受派者 | 段末對照結果 |
| `fbflow_work_create` | PLANNER | 建立工單 |
| `fbflow_work_supersede` | PLANNER | **需求變了 → 開新工單後，把舊工單標成已取代**（不改舊單的內容與狀態） |
| `fbflow_issue_create` | WORKER／THINKER | 發起 ISSUE |
| `fbflow_milestone_create` | PLANNER | 建立里程碑 |
| `fbflow_status` | 已註冊 session | 自己的任務、待處理缺口、目前里程碑 |

**第一步：`fbflow_project_join`（還沒加入這個專案的話）→ 接著 `fbflow_register`**：

```json
{"role": "PLANNER", "short_name": "P1"}
```

它會綁定你的角色席位，並回傳**專屬提示詞**（含只屬於你的 UUID）。
你**不需要**（也不能）自報專案代碼或路徑——專案是 host 綁定的。

**寫入工具一定要帶 `session_uuid`**（註冊時發給你的那組，只有你知道）。
身分有兩道：**host 綁定**（OpenCode 帶進來的 session id）＋ **你的 UUID**。
兩道都過才放行。**UUID 不可寫在任何檔案裡。**

**人類命令不在這裡**：`init`／`requirement confirm`／`engine status`／
`engine tick --once`／`awaiting list`／`awaiting resolve`／`project close`
走 `fbf.py` CLI，session 看不到也不該看到。

**唯一的例外是 `fbflow_project_join`**：它就是「讓新專案加入」的入口，
冪等、只能作用於目前工作目錄或它的子目錄，而且不改任何既有工單。
