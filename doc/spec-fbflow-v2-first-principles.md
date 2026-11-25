# FB FLOW v2 開發規格：以可恢復交付為核心

版本：2.1，2026-11-17。本文是 v2 新專案的開發依據，也是**本包唯一的規格正本**；其他版本的規格與設計筆記不隨包提供，不得與本文混用。實機整合列為開發驗收，不是動工前置條件。

> **本包自足**：本文提到的程式碼都在本包內——派工與 presence 是
> `fbf_engine/platform.py`、回執核對是 `fbf_engine/receipts.py`、
> 規則正本是 `fbf_engine/workflow.py`、正本是 `fbf_engine/store.py`。
> 本文不引用任何包外的檔案。外部的公開來源（OpenCode、gstack 公開倉庫）
> 只作設計對照，連結見第 12 節。

## 0. 要解決的問題與判準

使用者要把一項人類確認過的任務交給多個 AI session，讓它們在無人盯守時接力，最後交回**可由人核對**的成果。真正的故障不是「缺 MCP」或「角色數不夠」，而是：交接遺失、重複派工、錯誤自稱完成、方向逐漸偏離、卡死後無人知道。MCP、UUID、九狀態是實作手段；每單兩個獨立 SUBAGENT 是使用者明定的工作約束。

成功判準：在一個專案、三個角色 session 的真實 OpenCode 環境中，完成「人確認需求 → 派工 → 施工 → 獨立驗收 → 人確認結案」；中途殺掉服務並重啟後不遺失提交、不重複推進；每個結論可追到需求、驗收、證據與平台回執；超限或資訊不明時停在可見的待人處理項。假平台測試不能代替這項驗收。

**明示假設**：第一版只服務一個專案、一個本機 OpenCode server；PLANNER、THINKER、WORKER 是責任角色，同一 session 可兼任不同工單的角色，但同一工單不得自做自驗。啟動 session 的工具只負責啟動 session，不是正本。上述假設若與實際部署不符，先改本文，勿靠實作補洞。

**這個系統只負責推動流程，不負責省錢。** 上面列的故障全部是**流程性**的
（交接遺失、重複派工、錯誤自稱完成、方向偏移、卡死無人知），
沒有一個是**資源性**的。所以本系統不做成本／資源量測，也**不做資源上限**——
若成本成了問題，那是**設計問題**：成本在「真的執行工作的部分」就去改工作分解，
成本在流程本身就去改流程。**量了卻沒有對應的設計決策，就不要建那個儀表板。**
（本系統保留的「上限」全是**流程計數器**——重工、修訂、退回、派工重試、補工檢視——
它們數的是「流程繞了幾圈」，用來決定何時停下來等人，不是資源配額。）

## 1. 反證與架構取捨

| 現案主張 | 最強反例／可推翻證據 | 決定 |
|---|---|---|
| MCP 是中心 | MCP client 斷線、重啟或不支援 server push 時，派工與計時不能靠工具呼叫存活 | **持久正本與協調器是中心**；MCP 只是 session 介面 |
| 每張工單固定走九狀態 | 文件修改也要為兩個解法、驗收方式與兩次裁決走完全套；人工任務不一定有可跑案例 | 保留九狀態作**標準路徑**；先跑端到端實測，若實測顯示低風險工單的流程負擔明顯不合理，再以有證據的決策縮減，不預設例外 |
| 每張單至少兩個 SUBAGENT 解法 | 解法可能只是同義改寫；小修正也有固定成本 | **保留使用者硬約束**：每張工單至少兩個獨立 SUBAGENT 各提一案；守衛檢查來源不同、內容取捨不同。**每案都要寫出成本估算**（供選案比較用），角色不得自行豁免 |
| UUID 放在提示詞即可防冒名 | 若兩個 session 可讀同一檔案或 transcript，或 MCP 連線沒有可信 session 綁定，自報 session ID 無法證明來源 | host 綁定的 session 身分是主證據；UUID 是附加校驗。沒有可信綁定時停止寫入工具，不宣稱已安全 |
| 規則越多越可靠 | 更多關卡會增加空轉、誤拒與繞過誘因 | 只保留能對應真實失敗模式的硬守衛；主觀品質交給獨立驗收與人類 |

跨領域對照：資料庫交易提供「提交只套用一次」；工作佇列採「至少送達一次、消費端冪等」；控制器定期對照正本而非相信單次事件；安全領域以可信 host 身分作邊界，不把模型能讀的文字當祕密。這些是設計類比，不是已驗證本專案具備的能力。

gstack 可借的是「先挑戰問題、交接文件、獨立 review、失控指標」，不是複製其角色數或整套生命週期。見[原始倉庫](https://github.com/garrytan/gstack)與[流程說明](https://github.com/garrytan/gstack/blob/main/README.md)。

## 2. 冷啟動工程師只需記住的模型

```
人確認需求 ──> 工單 ──> 角色提交 ──> 協調器核對並推進 ──> 下一棒
                 │                         │
                 └── 驗收條件 ──> 結果 ──> 證據／回執
                              里程碑段末對照 ──> 人確認結案
```

**唯一術語**：`work`＝工單；`issue`＝阻礙／待裁決問題；`submission`＝角色寫完後交給協調器的資料；`receipt`＝平台證明該 session 執行過指定任務的紀錄；`decision`＝協調器的可追溯處置；`dispatch`＝通知，不是工作完成；`handoff`＝下一棒必讀的現況摘要。

**五個不變量**：
1. 正本狀態只有協調器能改；session 只能提交資料。
2. 同一個 submission 只能被套用一次；通知可重送，但不得因此重做狀態轉移。
3. 沒有有效需求、完整驗收結果與可核對證據，不得結案。
4. 不能確認來源的提交、回執或 session 身分，不能推進；必須留下可見原因。
5. 人類確認需求與最終結案；任何自動迴圈都有上限與人工出口。

## 3. 範圍與檔案責任

本版只支援**新專案**。既有 v1 專案凍結，不做資料遷移；這避免在尚未驗證 v2 時同時改舊資料。若必須延續 v1 歷史，先另立遷移需求與回滾驗收。此取捨須由專案擁有人確認後才可對既有資料執行。

| 檔案 | 責任 | 禁止 |
|---|---|---|
| `store.py` | SQLite schema、交易、資料存取 | 判斷流程與角色 |
| `workflow.py` | 單一狀態轉移表、交件契約、守衛、總判 | 網路、派工 |
| `engine.py` | 對照正本、套用提交、佇列派工、重試與計數 | 複製守衛規則 |
| `receipts.py` | 唯讀核對 OpenCode 回執 | 改正本 |
| `mcp_server.py` | 對 session 提供工具、身分與角色檢查 | 直接更新狀態 |
| `prompts.py` | 從 `workflow.py` 的交件契約渲染角色指示 | 另寫一份欄位規則 |
| `fbflow.py` | 人類管理 CLI、診斷、人工裁決 | 供 session 寫入 |

依賴順序：介面 → engine → workflow → store；receipts 由 engine 呼叫。`workflow.py` 不得 import `engine.py`。`gates.py` 的現有守衛逐條搬入或由 `workflow.py` 呼叫；新規則只准有一份定義。最小可跑路徑先做單工單閉環，再加入 ISSUE 和里程碑。

## 4. 資料契約

每個新專案一份 `fbf_engine.db`，啟動時建立；SQLite 啟用外鍵與 WAL。首版一個專案，不設全域專案索引。`project_code` 可寫在專案正本；未來確定需要跨專案搜尋時再加全域索引。所有 JSON 欄位在入口解析、型別與必填校驗一次；資料庫內只存通過校驗的值。

必要實體與鍵：

| 表 | 必要欄位／唯一性 | 寫入者 |
|---|---|---|
| `requirements` | `id`, `text`, `success_criterion`, `confirmed_at` | PLANNER 提案；人類確認 |
| `roles` | `(role, short_name)`, `session_id`, `agent`, `status`；`session_id` 唯一 | 人類指定、host 綁定 |
| `works` | `id`, `requirement_id`, `milestone_id`, `state`, `version`, `assigned_session`, `rework_count`, `revision_count`, `awaiting_human`, `content`, `created_at`, `updated_at` | 協調器；內容由提交帶入 |
| `solutions` | `id`, `work_id`, `subagent_context_id`, `approach`, `cost`, `change_scope`, `effort`, `risk`, `verifiable_by`, `reuses`；同工單 `subagent_context_id` 唯一 | THINKER 彙整提交，協調器驗證 |
| `checks` | `(work_id,id)`, `requirement_id`, `method`, `threshold`, `required` | PLANNER 提交，協調器驗證 |
| `results` | `(work_id,check_id,round)`, `status`, `reason`, `evidence_id` | THINKER 提交，協調器驗證 |
| `evidence` | `id`, `project_relative_path`, `size`, `sha256` | 提交時建立，協調器重算 |
| `submissions` | `id`, `task_id`, `work_id`, `expected_version`, `session_id`, `stage`, `payload`, `status`; `task_id,session_id,stage,expected_version` 唯一 | MCP 入口 |
| `tasks` | `id`, `work_id`, `stage`, `assigned_session`, `status`, `created_at` | 協調器 |
| `outbox` | `id`, `task_id`, `recipient_session`, `payload`, `status`, `attempts`, `next_attempt_at` | 協調器 |
| `receipts` | `id`, `task_id`, `session_id`, `message_id`, `model_id`, `completed_at`, `verified_at`; `(session_id,message_id)` 唯一 | 核對器 |
| `decisions` | `id`, `scan_id`, `target_id`, `from_state`, `to_state`, `submission_id`, `gaps`, `action`, `created_at` | 協調器，追加寫入 |
| `milestones` | `id`, `seq` 唯一, `outcome`, `state`, `review_count` | PLANNER 提案，協調器推進 |
| `issues` | `id`, `work_id`, `initiator_session`, `state`, `return_count`, `payload` | 發起者提交、協調器推進 |

外鍵指向現存需求／工單／check；證據路徑先 canonicalize，必須留在專案根內，拒絕絕對路徑與符號連結逃逸。狀態更新用 `WHERE id=? AND version=?`，同交易內寫 `decisions`、新 `tasks` 與 `outbox`；更新筆數為 0 代表競爭或過期提交，標 `stale`，不重試套用。

## 5. 工單流程與交件契約

狀態名固定：`backlog → design → acceptance_design → build → verify → adjudicate → closed`；退回時 `adjudicate → rework_design → rework_build → verify`。這是**九個狀態**，`awaiting_human` 是旗標，不另設狀態。資料庫只存英文代碼，UI 可顯示繁中。`closed` 終態不可自動重開。

| 當前狀態 | 責任角色 | 提交必備 | 成功時下一狀態 |
|---|---|---|---|
| `backlog` | 協調器 | 需求已確認、指派存在 | `design` |
| `design` | THINKER | 至少兩個獨立 SUBAGENT 各提一案；每案含來源 context、解法、成本、修改範圍、工作量、風險、驗證方式、可重用項；THINKER 彙整設計說明 | `acceptance_design` |
| `acceptance_design` | PLANNER | 選案理由、每條 `check` 的方法／門檻／需求 ID | `build` |
| `build` | WORKER | 產物索引、已做／未做、自檢結果、實際案例數 | `verify` |
| `verify` | THINKER | 每條 check 的 PASS／FAIL／PARTIAL／NOT_RUN、理由與證據；不可驗自己在本工單的施工 | `adjudicate` |
| `adjudicate` | PLANNER | `close` 加結案理由，或 `rework` 加原因與類型（施工錯／驗收錯／解法錯） | `closed` 或 `rework_design` |
| `rework_design` | THINKER | 對應退回原因的重工方案 | `rework_build` |
| `rework_build` | WORKER | 同 `build` | `verify` |

守衛：需求必須由人確認；每條 check 指向已確認需求；所有 required check 都 PASS 才能 `close`；檔案證據的大小與 SHA-256 與現檔相符；`cases_ran=0` 不得宣稱已執行驗收，但純文件／人工檢視 check 可填 `execution_kind=review`，提交可核對的審閱紀錄，不造假為測試案例。`verdict` 只由程式計算，不接受模型提交。若驗收方式錯，回 `acceptance_design` 並增加 `revision_count`，不能把「改標準」偽裝成重做施工。

重工上限 2 次。第 2 次後 PLANNER 必須診斷 `work|acceptance|solution|scope|resource`，選擇有界修訂或停等人；修改驗收方式時不得降低門檻、把 required 改 false、刪已有結果的 check。`revision_count` 上限 2；達上限標 `awaiting_human`。所有硬拒絕回傳穩定缺口代碼、缺哪個欄位與可行下一步。

## 6. 協調器、派工與重啟

一個常駐進程擁有內建 timer，定期執行 `tick`；也提供人類 CLI `engine tick --once` 作診斷。**MCP server 是該進程的介面，不是排程器本身**。同一專案同時只准一個協調器寫入；用 SQLite 租約欄位與到期時間防重啟後雙主，不依賴記憶體鎖。租約過期後新進程取得；舊進程每次寫入前須驗租約世代。

每趟 tick：讀取待處理 submission → 交易外查回執 → 單筆交易內重讀版本、核對角色與守衛、套用或標原因、建立下一 task/outbox、追加 decision → 交易外送 outbox → 回寫送達狀態。資料庫更新失敗時不送通知。派送沿用現有 INFORM 的 outbox 與 `native_notify.send_opencode`；`fbflow inform` CLI 對新版工單仍禁用，Session 不得自選收件人。通知可能已送達而回寫失敗：狀態不明時先查 task／session，不得盲目重送；重複收到同一 `task_id` 也只能提交一次。進程重啟後掃描待提交與待送 outbox，繼續處理。

派工只選目前里程碑的工單；同角色多 session 時依「未完成工單最少 → 最久未被指派 → session ID 字典序」決定。presence 只影響送出時機，不影響提交裁決；查不到 presence 記 `UNKNOWN` 並暫緩派送，不當成離線。連續派送失敗 5 次標待人處理，不無限喚醒。所有通知包含 `task_id`、工單 ID、讀取工具與本棒交件清單；不得包含其他 session 的憑證。

## 7. 身分與平台回執

寫入工具必須由**可信 host 綁定**的 session 連線呼叫，MCP adapter 取得連線身分；請求中的 `session_id` 僅用於交叉檢查。session UUID 若保留，只在該 session 私有提示詞中交付，DB 只存雜湊；它不能替代 host 綁定。無法證明連線所屬 session 時，寫入工具關閉並回 `G-SESSION-UNVERIFIED`。角色白名單在 MCP 邊界驗一次；PLANNER 不可假冒 WORKER 提交施工。人類控制命令走獨立入口，不能暴露給 session。

回執核對只讀平台資料：`task_id` 對得上、message 由被指派 session 產生、時間在派工後、平台標示完成、`message_id` 未被其他 submission 使用。缺任一項標 `unverified`，不建立可信 run，不推進。核對路徑是 `fbf_engine/receipts.py`（唯讀，只查平台資料）；整合時以實際 OpenCode 回應固定 fixture，不根據猜測的 JSON 格式宣稱核對成功。兩個 SUBAGENT 的 context 身分也須能在平台回執中分別核對。

## 8. ISSUE、里程碑與人類出口

ISSUE 只處理「目前工單無法往前」或「需要 PLANNER 裁決」，不承載一般聊天。發起者提交問題、位置、已試方法、影響、需要的決策；PLANNER 回答或建立關聯工單；關聯工單全結案後，原發起者驗證結果並結案或附原因退回。退回最多 3 次，之後標待人處理。ISSUE 不得暗中改母工單的驗收條件。

里程碑的 `outcome` 在派第一張單前寫定。當本段工單全結案，協調器產出「工單 × check × 結果」清單；PLANNER 對照 outcome，**只可選**：`continue`（達成，下一段開工）、`add_work`（未達成，補工單後留在本段）、`await_human`（停止派工，等人裁決）。最多兩次補工檢視，之後只能 `await_human`。PLANNER 不需要每段請人確認，但可自行停下來等人。MILESTONE 用來擋整段方向偏移，不能替代逐單驗收。專案結案須雙向查核：每項已確認需求有結案工單與通過證據；每項結案工單能追到已確認需求；最後由人類確認。人可選 `grant_more|revise|abort`，每次附理由並記日誌。

## 9. 交接與認知成本

每棒只寫一份結構化 handoff：角色、未完成項、仍在跑的子工作、當前門檻、可重用做法、決策理由、踩過的坑、已耗資源。下一棒只讀 handoff 與正本，無須重讀 transcript；handoff 是摘要，不能覆蓋正本欄位。狀態變更時未提供 handoff，守衛退件。

舊版的閱讀路徑長達數千行，且「MCP 中心／協調器中心」「九狀態／暫停狀態」「內建排程／外部 tick」混用；那些文件**不隨本包提供**。冷啟動工程師若逐份通讀會先花大量時間解衝突，且仍可能選錯權威來源。本文把入口縮成：本節模型 → 第 3 節責任 → 第 4–8 節契約 → 第 10–11 節驗收與工具。若需要超過這條路徑才能回答「誰改狀態、何時派工、何時停、何時算完成」，本規格仍不合格。

## 10. 施工順序與驗收

1. **單工單閉環**：新專案 schema、九狀態、單一提交入口、協調器與既有 INFORM 派工路徑。驗收：一張工單從 backlog 到 closed，重啟一次仍只套用一次。
2. **INFORM 整合**：重用既有 INFORM 通道（本包為 `fbf_engine/platform.py` 的 outbox 與 OpenCode 指定 Session 通知）；保留過期任務重查與忙碌／UNKNOWN 分支。驗收：送出、狀態不明、過期與重啟情境均不重複推進。使用者已驗證 INFORM 可用，開發不得另造派工通道。
3. **信任與證據**：角色白名單、host 身分、平台回執、證據雜湊。驗收：錯 session、重複 message、缺回執、變造證據都拒絕且留原因。
4. **ISSUE 與里程碑**：加入上限、人工出口、段末對照與專案結案。驗收：卡死會停、補工後可繼續、需求雙向追溯缺一即不得結案。
5. **失控量測**：只記錄**失控指標**——空轉率（最近回合中空喚醒的比例）、驗收輪數、退回次數。
   這三項是**設計訊號**，不是成本儀表板：空轉率高表示流程或角色提示詞有問題，
   驗收輪數高表示驗收方式訂得不對。**不做成本／資源量測**（呼叫數、token、牆鐘、退回成本）：
   成本若成為問題是設計問題，去改設計，不要建沒有對應決策的 framework。

測試用暫存專案：狀態合法／非法轉移、每個必填缺項、同 submission 重放、兩個協調器競爭、送達後回寫失敗、進程在交易前後被殺、憑證冒用、回執錯 session、文件型驗收、重工與 ISSUE 上限、里程碑順序、需求雙向追溯。Windows 與 macOS 各跑一次；真實 TUI／回執／多 session 接力必須標 `REAL_PASS` 或 `NOT_RUN`，不可用 mock 冒充。

## 11. MCP 與人類控制面的具體契約

所有工具回覆同一外殼：成功 `{ok:true,data:{...}}`；失敗 `{ok:false,code:"穩定代碼",message:"人可讀說明",details:{...}}`。失敗不得偷偷改正本。工具的 `project` 由 host 綁定，不讓 session 自報路徑。每個寫入工具均要求有效 task、符合角色、`expected_version` 等於正本版本；MCP 在入口檢查型別、欄位白名單與大小上限，再交 workflow。

| 工具 | 呼叫者 | 輸入 | 回傳 |
|---|---|---|---|
| `fbflow_register` | host 指定的新 session | `project_code, role, short_name`；連線身分由 host 帶入 | 自己的角色、session ID、可用工具、私有提示詞 |
| `fbflow_read` | 已註冊 session | `target_kind=work|issue|milestone, target_id` | 本角色可見的正本快照與 `version`；不含別人的憑證 |
| `fbflow_report_work` | task 指派者 | `task_id, work_id, expected_version, stage, payload, message_id` | `submission_id, status=pending`；不回「已轉態」 |
| `fbflow_report_issue` | 發起者或目前處理者 | `task_id, issue_id, expected_version, stage, payload, message_id` | 同上 |
| `fbflow_work_create` | PLANNER | `requirement_id, milestone_id, title, content, expected_outputs` | 工單 ID、`state=backlog`, `version=1` |
| `fbflow_issue_create` | WORKER／THINKER | `work_id, trigger_kind, problem, location, tried, impact, question` | ISSUE ID；仍需協調器派工 |
| `fbflow_milestone_create` | PLANNER | `seq, outcome, name` | 里程碑 ID |
| `fbflow_status` | 已註冊 session | 無 | 自己的 tasks、待處理缺口、目前里程碑 |

`fbflow_report_work.stage` 必須等於 task 的 stage；payload 的欄位集合由第 5 節交件契約決定。未知欄位回 `INPUT-UNKNOWN-FIELD`，缺欄位回 `CONTENT-MISSING`；過期 version 回 `TASK-STALE`；角色錯回 `ROLE-DENIED`；不可信連線回 `G-SESSION-UNVERIFIED`；回執欠缺時 submission 保留並標 `unverified`，回 `RECEIPT-PENDING`，待下趟 tick 重查，不讓角色重交一份。對同一 idempotency 鍵的重送，回傳原 submission ID。

人類 CLI 只提供 `init`、`requirement confirm`、`engine status`、`engine tick --once`、`awaiting list`、`awaiting resolve`、`project close`。`awaiting resolve` 需目標 ID、動作與理由；`project close` 需通過第 8 節雙向追溯並記人類確認者與時間。不得把這些命令掛在 session 可見的 MCP 工具上。

### 提交與推進的交易順序

```
MCP report:
  驗 host 身分與角色 → 驗 task/version/payload → INSERT submission(pending)
  UNIQUE 衝突 → 回原 submission；不得更新 works.state

engine tick:
  取候選 submission → 在交易外查平台回執
  BEGIN IMMEDIATE
    重讀 task、work.version、指派、submission.status、租約世代
    不符 → submission=stale，記 decision；COMMIT
    回執不足 → submission=unverified，記原因；COMMIT
    守衛不過 → submission=rejected，記 gaps 與 decision；COMMIT
    全通過 → 寫內容／結果、version+1、下一 task、outbox、decision、submission=applied
  COMMIT
  交易外送 outbox；送後回寫結果
```

提交 `unverified` 後如何重查：timer 查 `pending` 與 `unverified`，直到回執到達或 10 分鐘截止；逾時標 `awaiting_human` 並保存原 submission，不自動宣稱失敗。`rejected` 提交不重套用；角色依缺口建新提交。發送 outbox 失敗採指數退避，最多 5 次，且不得在 SQLite 交易內呼叫平台。

## 12. 整合驗收與已裁決事項

使用者已確認既有的 INFORM 派工機制可用；本規格將它列為既有能力並直接重用。這項確認**只涵蓋派工**，不等於 session 身分綁定、可信執行回執或服務重啟已驗收。後三項在第 10 節施工過程中驗證，留下 OpenCode 版本、去識別回應、預期與實際結果；若平台不提供必要身分／回執證據，該守衛不得降級成相信模型自報，應停工回報設計阻礙。

使用者已裁決：v2 只用於新專案；每張工單都由至少兩個獨立 SUBAGENT 提案；里程碑由 PLANNER 決定繼續、補工或停等人。這三項是硬契約，不留給工程師自行取捨。

來源（外部公開來源，只作設計對照）：[gstack README](https://github.com/garrytan/gstack/blob/main/README.md)、[gstack 技能說明](https://github.com/garrytan/gstack/blob/main/docs/skills.md)。外部做法只作設計對照，本文中的架構選擇是本專案推論。
