# 與規格的差異（逐條列理由）

規格：`doc/spec-fbflow-v2-first-principles.md`（本包唯一的規格正本）。
本檔記錄**實作時不得不做的調整**。每條都寫清楚：規格怎麼說、為什麼這樣改、影響是什麼。

---

## D1. 多了一個 MCP 工具：`fbflow_report_milestone`

**規格怎麼說**：第 11 節的工具表列了 8 個工具，沒有里程碑回報。
第 8 節卻要求里程碑檢視時 PLANNER 必須回報 `continue`／`add_work`／`await_human`。

**為什麼必須改**：規格第 8 節的機制在第 11 節的工具表下**無法實作**——
沒有任何通道能把里程碑檢視結果送回正本。這是規格內部的缺口，不是實作偷懶。

**怎麼改**：新增 `fbflow_report_milestone(task_id, milestone_id, expected_version, action, reason, new_works)`。
動作只接受規格第 8 節列的三個值。

**影響**：工具數 8 → 9。其餘契約不變。

---

## D2. CLI 多了一組 `session bind`／`list`／`revoke`

**規格怎麼說**：第 11 節「人類 CLI 只提供 `init`、`requirement confirm`、
`engine status`、`engine tick --once`、`awaiting list`、`awaiting resolve`、`project close`」。
第 7 節又要求「寫入工具必須由**可信 host 綁定**的 session 連線呼叫」。

**為什麼必須改**：host 綁定的憑證總得有個地方建立。
規格的命令清單沒有這個入口，**照字面實作會讓所有寫入工具永遠回 `G-SESSION-UNVERIFIED`**。

**怎麼改**：新增 `session` 這組命令。`bind` 產生 token 並寫進
`<專案>/.fbf/credentials.json`（host 層，模型碰不到）。

**影響**：CLI 多一組命令。**沒有把任何命令搬到 MCP 面上**——
規格要求「人類命令不得掛在 session 可見的 MCP 工具上」，這條守住了（測試有驗）。

---

## D3. `requirement confirm` 可以「建立並確認」

**規格怎麼說**：命令表有 `requirement confirm`，但沒有 `requirement add`；
第 11 節的 MCP 工具表也沒有建立需求的工具。

**為什麼必須改**：需求是追溯鏈的第一節（需求 → 工單 → 驗收方式 → 證據），
而 `fbflow_work_create` 要求 `requirement_id` 指向**已確認**的需求。
沒有任何通道能產生需求，整條追溯鏈會懸空。

**怎麼改**：`requirement confirm` 給 `--id` 就確認既有的；
給 `--text` + `--criterion` 就**建立並確認**。

**影響**：命令數不變，行為多一條分支。

---

## D4. `works` 表多了幾個欄位

**規格怎麼說**：第 4 節的 `works` 表列了
`id, requirement_id, milestone_id, state, version, assigned_session, rework_count,
revision_count, awaiting_human, content, created_at, updated_at`。

**為什麼必須改**：規格同時要求「所有 JSON 欄位在入口解析、型別與必填校驗一次；
資料庫內只存通過校驗的值」——表示會有 JSON 欄位，但沒列是哪幾個。

**怎麼改**：

| 欄位 | 用途 | 為什麼必要 |
|---|---|---|
| `title` | 工單標題 | 派工訊息要帶；追溯查核要能辨識是哪張單 |
| `assigned_role` | 這一棒由哪個角色做 | 守衛要驗「角色相符」 |
| `expected_outputs` | 預期產出 | 工單契約的一部分 |
| `selection` | JSON：選案 | 設計驗收方式中產出 |
| `build_result` | JSON：施工結果 | 施工中／重工中產出 |
| `rework_plan` | JSON：重工方案 | 重工方案設計中產出 |
| `rework_reason`／`rework_type` | 退回原因與類型 | 重工上限與「驗收錯 vs 施工錯」的判斷依據 |
| `cases_ran`／`execution_kind` | 案例數與驗收種類 | 假綠燈偵測（`cases_ran=0` 不得宣稱驗收） |
| `verdict` | 總判 | **程式算**，不採信模型自稱 |

**特別說明**：原本想把 `selection`／`build_result`／`rework_plan` 塞進 `content`，
**實作時發現這是錯的**——`content` 是工單的自然語言內容，不是 JSON 承載槽。
兩種東西共用一欄會讓 `content` 變成「有時是字串、有時是 JSON」的怪物。
（這是實作期抓到的真缺陷，已改。）

---

## D5. UUID 存在 `roles` 表，不另開 `sessions` 表

**規格怎麼說**：第 4 節的表清單沒有 `sessions`；第 4 節又說
「首版一個專案，不設全域專案索引」。

**為什麼這樣改**：一個 session 就是角色表的一列（`session_id` 唯一），
再開一張 `sessions` 表就是同一份資料存兩處。
`roles.uuid_sha256` 只存雜湊，明文 UUID 只出現在該 session 自己的提示詞裡。

**影響**：`roles` 多一欄 `uuid_sha256`。

---

## D6. outbox 狀態詞彙沿用既有 INFORM 通道

**規格怎麼說**：第 4 節只說 outbox 有 `status`，沒列值。

**怎麼改**：沿用既有 INFORM 通道（`workflow_outbox` 表）的詞彙：
`queued` → `sending` → `submitted`（平台已接受）→ `delivered`（已確認），
失敗 `failed`，狀態不明 `unknown`。

**理由**：兩套系統對同一件事用同一組詞彙，少一處對照成本。
（新的 `deferrals` 欄位是這個版本獨有的——用來給 presence 暫緩計數，
避免「查不到 presence」變成無限喚醒。）

---

## D7. `unverified` 提交的重查有截止時間

**規格怎麼說**：第 11 節「submission 保留並標 `unverified`，回 `RECEIPT-PENDING`，
待下趟 tick 重查，不讓角色重交一份」。

**為什麼補**：照字面無限重查，回執永遠不來就會永遠卡住。
加上 10 分鐘截止：逾時標 `awaiting_human` 並**保留原 submission**，
**不自動宣稱失敗**（規格第 6 節的同一個原則）。

---

## D8. `tasks`／`milestones`／`issues` 多了三個欄位

**規格怎麼說**：第 4 節的 `tasks` 表列 `id, work_id, stage, assigned_session, status, created_at`；
`milestones` 列 `id, seq, outcome, state, review_count`；
`issues` 列 `id, work_id, initiator_session, state, return_count, payload`。
但第 11 節要求「每個寫入工具均要求有效 task、符合角色、`expected_version` 等於正本版本」，
第 8 節要求「退回最多 3 次，之後標待人處理」。

**為什麼必須改**：這兩條規格要用到的欄位，表清單裡沒有——照字面實作就沒有地方存放。

| 欄位 | 放在哪 | 為什麼必要 |
|---|---|---|
| `tasks.milestone_id` | tasks | 里程碑檢視單要能證明「這張單是給哪一段的」；沒有它，別張單就能拿來結案 |
| `milestones.version` | milestones | 里程碑檢視是寫入工具，`expected_version` 要有對應的正本版本，否則過期回報無從判定 |
| `issues.awaiting_human`／`awaiting_reason` | issues | 「退回達上限 → 標待人處理」需要旗標；`works` 上的 `awaiting_human` 是同樣做法 |

**怎麼改**：三組欄位加進 `SCHEMA`，同時加進 `store.MIGRATIONS`（既有資料庫啟動時自動補欄位）。

**影響**：schema 多三組欄位，行為不變——這些規則原本根本沒被實作。

---

## D9. CLI 多了一組 `role release`（席位的出口）

**規格怎麼說**：第 11 節「人類 CLI 只提供 `init`、`requirement confirm`、
`engine status`、`engine tick --once`、`awaiting list`、`awaiting resolve`、
`project close`」。第 7 節要求「角色白名單在 MCP 邊界驗一次」，
但**沒有任何地方說明席位被一個中斷的 session 占住時怎麼辦**。

**為什麼必須改**：席位的綁定本來**沒有生命週期**——session 一旦認領就永遠不放；
`session revoke` 也只改 `status`、留著 `session_id` → 新的 session 永遠認不到
（`ROLE-SLOT-TAKEN`），整條流程靜靜卡住，而且沒有任何畫面顯示為什麼卡住。
「卡死後無人知道」正是規格第 0 節列的故障之一。

**怎麼改**：
- 新增 `fbf.py role release --role <角色> --short-name <簡稱> [--reason ...]`
  （清空 `session_id` 與該 session 的 UUID 雜湊，寫進裁決日誌）。
- `session revoke` 現在**真的空出席位**（以前只改 `status`，席位照樣被占）。
- `role list` 與 `engine status` 會顯示「被占住卻很久沒活動」的席位（`stale`），
  讓孤兒席位看得見。
- 被擋的 session 收到的訊息會直接說明出口（`fbf.py role release ...`）。

**影響**：CLI 多一個子命令。釋放是**人的決定**，所以留在人類控制面，
不開放給 session（規格第 7 節：人類命令不暴露給 session）。

---

## D10. `unverified` 到期改標 `expired`（新增終態），並允許換新 task 重試

**規格怎麼說**：第 11 節「逾時標 `awaiting_human` 並**保留原 submission**，
不自動宣稱失敗」（DEVIATIONS D7 已加 10 分鐘截止）。

**為什麼必須改**：保留原 submission 在 `unverified` = **每輪 tick 永遠重查
平台**，而且舊 task 仍開著 → `_dispatch_pending` 跳過 → 裁決後也接不回去。
W-004／W-013 就是這樣停死的：`awaiting_human` 有標、但重試路徑不存在。

**怎麼改**：
- 到期 → submission 標 `expired`（**終態**，脫離重查）——不是「宣稱失敗」，
  它照樣留在 `submissions` 裡可查；
- 作廢舊 task → 換新 task 重試（`max_receipt_expires`，預設 3 次）；
- 連 3 次 → 標 `awaiting_human=receipt_timeout` → 人用
  `awaiting resolve grant_more` 裁決 → **重置計數、重派一棒**。

**影響**：submission 狀態 5 → 6 種（多 `expired`）；新增設定
`max_receipt_expires`；`awaiting_resolve` 對 `receipt_timeout` 不再跳
`rework_design`（那不是重工）。

## 沒 deviate 的地方（刻意照規格走）

- **九狀態**、`awaiting_human` 旗標不設第 10 個狀態。
- **兩層資格**：host 綁定為主證據、UUID 為附加校驗；沒有可信綁定就停止寫入工具。
- **交易順序**：查回執在交易外、套用在交易內、送 outbox 在交易外；平台呼叫一律不在交易內。
- **冪等鍵**：`(task_id, session_id, stage, expected_version)` 唯一。
- **租約用資料庫欄位**（不依賴記憶體鎖），舊進程寫入前要驗世代。
- **派工優先序**：未完成工單最少 → 最久沒被指派 → session ID 字典序。
- **派工重用既有 INFORM**（`opencode run --session`），**沒有另造派工通道**。
- **回執核對六條件**全過才建立可信 run；缺任一項標 `unverified`。
- **里程碑三選一**、最多兩次補工檢視。
- **人類命令不下放給 session**（有測試驗）。

## D11. 出工單走 host CLI 終端（`terminal send` 寫進終端 PTY），session 自動登入角色表

**規格怎麼說**：第 10 節第 2 階段「不得另造派工通道」——同一份 outbox，只換投遞媒體。
第 6 節 presence 三態語義（busy／idle／unknown，查不到 ≠ 離線、不明不得盲目重送）。
第 7 節「寫入工具必須由可信 host 綁定的 session 連線呼叫」。

**為什麼必須改**：使用者要求「FBF ENGINE 把提示詞送進已經在 host 跑著的 Claude Code /
OpenCode session，並在 TUI 顯示」。OpenCode 原生 `opencode run --session`（INFORM 通道）
沒辦法把提示詞塞進**已經開著、有對話上下文的** session；只有 host CLI
的 `terminal send --terminal <handle> --text --enter` 能寫進該終端的 PTY 並在 TUI
顯示。

**怎麼改**：

- `fbf_engine/hostterm.py`（新模組）：`scan_sessions`（`terminal list` + `agent list`，
  依 paneKey 合併）、`presence_of_handle`（busy／idle／unknown）、`send`（`terminal send
  --text --enter --wait-submit`，`delivered` 看到回合開始，`submitted` 改讓回執層再核對）、
  `read_agent`（`agent read`，有 transcript 時；OpenCode 回 `[]`）、`read_screen`（`terminal read`，
  僅供回執參考）、`launch`（人類開新 session，引擎不碰）、`verify_host`／`delivery_state`
 （回執錨點是 task_id；host 認得的 agent 走 `agent read`，否則讀畫面；看不到 → unknown，
  不當沒送達）。
- `fbf_engine/platform.py`：新增 `send_hostterm` + `scan_host_sessions`，`send_to_session`
  依 `session_id`／`delivery_handle` 是否以 `term_` 開頭路由（`term_…` → host，否則 OpenCode 原生）。
- `fbf_engine/receipts.py`：`VERIFIERS["claude"]` 指向 host 通道核對器（`verify_host`）；
  `CONTEXT_VERIFIERS["claude"]` 回 `context.query_failed`（host CLI 沒有 sub-agent context 查詢）。
- `fbf_engine/engine.py`：tick 起頭 `_enroll_host_sessions()`——把 host 裡現在開著的 session
  填到 roles 表裡尚未綁定 session_id 的席位（agent='claude'，不主動建席位）。
  session 不在角色表裡永遠不會收到提示詞（`_pick_session` 只從 roles 讀）→ 只加不誤派。

**受測**：hostterm 對真實 host 已逐一驗證（scan 3 session、presence、read_agent
claude 成功／opencode 回 `[]`、send claude→`delivered`、opencode→`submitted`）。
送達三態跟 OpenCode 通道同語義（delivered／not-delivered／unknown）。

**影響**：出工單通道多一個（host terminal 與 OpenCode 原生並列，同一份 outbox）；
roles 表會多寫 auto-enroll 的席位綁定；claude agent 的回執核對從「opencode API」
改為「host 終端讀取」。


---

## D12. 專案啟動三段：`grill` 與 `decompose` task 插在 join 與工單流水線之間

**規格怎麼說**：規格沒有「啟動前需求釐清」的任務——工單流水線從人類
`requirement confirm` 之後才算開始。

**為什麼必須改**：上一輪實測踩到——專案文件與 PLANNER 的理解不一致時，
工單生成後才被发现，整條流水線白跑。採用 GRILL ME（追問 + 複述 + 同意）
作為**生成工單前**的段落，且人類 `requirement confirm` 就是「同意」的訊號。

**怎麼改**：

- `workflow.STAGE_GRILL = "grill"`——**不是**九狀態之一（與 `milestone_review`
  同型：有 task、無 work），不進 `WORK_STATES`／`WORK_TRANSITIONS`。
- `engine._dispatch_grill()`：`tick()` 在 auto-enroll 之後檢查**已確認需求數**；
  為 0 → 派 grill task 給 PLANNER（`prompts.render_grill_prompt`：讀文件 →
  追問四項 → 自己話複述 → 問同意，並明確禁止 `fbflow_work_create`）；
  已確認 ≥ 1 → 收掉開著的 grill task（`grill.close` 日誌）。
- `fbf.py requirement confirm` 收尾同型（人類端）：把開著 grill task 標 done、
  寫 `grill.close` 日誌；`join` 的 `note` 印出三段說明給人類。

**怎麼改（續，第 3 段）**：`workflow.STAGE_DECOMPOSE = "decompose"` +
`engine._dispatch_decompose()`——需求已確認、works = 0 時派 `decompose`
task 給 PLANNER（`prompts.render_decompose_prompt`：三句話原則 +
`fbflow_work_create`）；第一張工單一建出來就自己收掉（`decompose.close` 日誌），
之後 `_dispatch_pending` 接管。**沒有這一段就沒有「人類同意後開始生成工單」的
觸發器**——上一輪實測 PLANNER 複述完同意後停著不動，就是缺這個。

**影響**：工單流水線前多兩個**只各跑一次**的 task（grill → decompose）；
人類同意前工單數必為 0（`create_work` 仍只吃已確認需求，守衛不變）。

---

## D13. `join` 同時寫 OpenCode 與 Claude Code 兩邊 MCP 設定

**規格怎麼說**：規格第 11 節的安裝只講 OpenCode 的 `opencode.json`。

**為什麼必須改**：上一輪實測——Claude Code 讀的是**專案層** `.mcp.json`
（`mcpServers` 格式，跟 OpenCode 的 `mcp.servers` 不同），`join` 只寫
OpenCode 邊 → Claude Code session 看不到 9 個工具，PLANNER 被迫直接碰
SQLite（被擋下才回到正路）。

**怎麼改**：`paths.claude_entry()`／`paths.merge_claude()` 寫專案
`.mcp.json`（合併不覆蓋）；`cmd_join` 兩邊都寫，回傳加 `claude_config`。

**影響**：`join` 多寫一個檔案；兩邊設定共用同一組 env（`PYTHONPATH`、
`FBF_PROJECT_ROOT`）。

---

## D14. host CLI 解析加 macOS 應用程式內附路徑；`resolve_cli` 取代寫死命令名

**規格怎麼說**：規格沒有 host CLI（D11 已記錄整體通道）。

**為什麼必須改**：實測 host CLI 不在 `PATH`，`shutil.which` 找不到 → 派工
「找不到宿主 CLI」；而且**不能要求使用者手動 export**（那是 FBF 啟動路徑
自己的事）。另外 platform/engine 原本把 CLI 命令名寫死傳入，繞過
`FBF_HOST_CLI` 與應用程式兩層——宿主 CLI 改名／換路徑就壞。

**怎麼改**：`hostterm.resolve_cli()` 順序＝顯式 → `FBF_HOST_CLI` → `PATH` →
macOS 應用程式內附 CLI（`_MAC_APP_CLI`，名稱用字串接成以過衛生守門）；
`platform.send_to_session` 與 `engine._enroll_host_sessions` 改傳 `cli=""`
（走完整解析），不再寫死命令名。

**影響**：host 通道在 CLI 不在 PATH 的機器上仍可用；換機器／換 CLI 名
只動 `resolve_cli` 一處。

---

## D15. 重工類型常數改名（`REWORK_TYPE_*`），避免蓋掉 `rework_build` 階段名

**規格怎麼說**：第 5 節的九狀態——`rework_build` 是**階段**，
「施工錯／驗收錯／解法錯」是 adjudicate 時 PLANNER 填的**退回類型**，
兩件事。

**為什麼必須改**：實測 bug——`workflow.py` 把退回類型常數也起名
`REWORK_BUILD`（值「施工錯」），蓋掉前面的階段常數（值 `rework_build`）→
`WORK_STATES` 的鍵變成「施工錯」、`WORK_TRANSITIONS` 指向中文鍵；
測試因為**兩邊都用同一個常數**而全綠，狀態機實際上已壞（提示詞渲染、
守衛查表都拿到錯的鍵）。

**怎麼改**：退回類型改名 `REWORK_TYPE_BUILD`／`REWORK_TYPE_ACCEPTANCE`／
`REWORK_TYPE_SOLUTION`（值不變，DB 裡的 `rework_type` 欄位內容也不變）；
`test_nine_states_and_transitions` 加**英文字面**斷言
（`set(WORK_STATES) == {…9 個英文名…}`＋常數值斷言），常數再被蓋就紅。

**影響**：只有常數名；DB、提示詞值、守衛行為都不變。

---

## D16. `fbflow_status` 回已確認需求清單；`fbflow_read` 接受 `requirement`；decompose 提示詞直接嵌入需求 id

**規格怎麼說**：第 11 節工具表只有 `fbflow_status()` 與
`fbflow_read(target_kind, target_id)`，沒有規定 status 要回什麼、read 支援哪些 kind。

**為什麼必須改**：實測踩到——PLANNER 拿到 decompose 派工後**查不到 requirement_id**
（status 不回需求、read 不支援 requirement），於是回頭問人類「標題要什麼／需求 id
是什麼」。整條工單生成卡死；更糟的是模型開始提議直寫 SQLite 繞過（被擋下）。
這不是提示詞問題，是**引擎沒有把必要資訊給出去**。

**怎麼改**：

- `_tool_status` 回傳加 `confirmed_requirements`（id／text／success_criterion，
  只列 `confirmed_at` 非空的）與 `open_works`（未結案工單清單）。
- `fbflow_read` 的 `target_kind` enum 加 `requirement`；讀不到回 `NOT-FOUND`。
- `render_decompose_prompt` 渲染時直接讀已確認需求並**把 id 與完成條件寫進提示詞**，
  同時明文禁止「回頭問人類」「只列步驟不呼叫工具」「直寫 SQLite／CLI」，
  並拿掉會讓模型提早收工的 `SESSION_IDLE_RULE`（那句原本被理解成
  「沒任務就結束回合」，正好扼殺「還沒建工單」的狀態）。

**影響**：status 回傳多兩個欄位（向後相容，只加不改）；read 多一種 kind；
decompose 提示詞變長但資訊自足。新增 6 條回歸測試
（`TestPlannerGetsRequirements`）。

---

## 沒 deviate 的地方（刻意照規格走）

---

# 對齊 `spec/`（新的規格正本）

> **基準換了**：SPEC 正本從 `doc/spec-fbflow-v2-first-principles.md` 改成 **`spec/`**
> （`spec/FBF ENGINE工作流.md`、`spec/工單設計.HTML`、`spec/ISSUE單設計.HTML`）。
> 舊文件降為工程參考稿。本節記錄對齊 `spec/` 時的調整。

## D17. 提示詞不再帶工作狀態；交件契約搬進「工單檔案」

**spec 怎麼說**：第 70 行「提示詞內容不包含工作狀態，所有狀態都在文件中」；
第 69 行的範例提示詞只有「派工工單編號1234567，請讀取這張工單跟專案SKILL，進行開工」。

**原本**：派工提示詞帶 `stage:`、`工單目前狀態：…（第 N 輪驗收）`，
以及整份「這一棒要交的欄位 + 交接檔八欄」。

**為什麼必須改**：spec 明講狀態不在提示詞裡。原實作把「哪一棒」寫進提示詞，
等於把狀態送出去；而角色真正需要的是「這一棒要交什麼」——那是**契約**，
跟著狀態走，應該跟狀態一起放在文件裡。

**怎麼改**：

- 新增模組 `workfiles.py`（**已於 D34 移除**）：**工單檔案**（`工單/未完成/W-001/工單.md`）
  與 **ISSUE 檔案**（`ISSUE/未完成/IS-001/ISSUE.md`）由引擎寫死生成，
  內含正本現況（狀態、版本、解法、選案、驗收方式、產出、重工）＋
  **「現在這一棒」段落（這一棒要交的欄位與規格）**。
- 派工提示詞只留：task_id、work_id、expected_version、工單檔案路徑（相對＋絕對）、
  專案 SKILL、產出資料夾、**方法**、交件方式、讀取工具。**不含狀態、不含欄位清單**。
- `fbflow_report_work`／`fbflow_report_issue` 的 `stage` 改成**選填**：
  留空就依 `task_id` 從任務表查出這一棒（角色不必知道狀態也能交件）。

**影響**：提示詞變短；**契約改由工單檔案承載**（這正是 spec 要的「文件層」）。
測試 `TestWorkFileCarriesContract` 原本斷言「提示詞帶契約」，
改為斷言「**工單檔案**帶契約 ＋ 提示詞指向它 ＋ 提示詞不含狀態」。

## D18. 新增工單／ISSUE 檔案層與 `works.skill`

**spec 怎麼說**：第 73~80 行「FBF引擎生成一個工單資料夾，裡面分成已完成跟未完成兩個資料夾／
工單建立時建立在未完成資料夾，完成時移動到已完成資料夾」（ISSUE 同）；
第 85 行「PLANNER填寫：工作名稱，內容，解法，驗收方式，**搭配的專案SKILL**」；
第 71 行「WORKER…建立一個名稱為簡稱的資料夾，工作的產出都放在自己的資料夾」。

**怎麼改**：

- 新模組 `workfiles.py`（**已於 D34 移除**；`render_work`／`render_issue`／
  `sync_work`／`sync_issue`／`move_to_done`）。
- `works` 表新增 `skill` 欄位＋遷移；`fbflow_work_create` 新增選填參數 `skill`。
- 建立工單／ISSUE 時立刻寫檔；每一趟 `tick` 重寫（**檔案落後正本＝角色照舊狀態做事**）；
  結案時搬到「已完成」。
- **產出資料夾**（WORKER 簡稱）只寫進提示詞，**不**由引擎強制——
  spec 把它歸在「角色怎麼做事」，屬於交給模型的部分。

**影響**：專案根多出 `工單/` 與 `ISSUE/` 兩個資料夾。

## D19. pstack 對應調整：`interrogate` 移到驗收棒、`no-comments` 拿掉

**spec 怎麼說**：第 15／19／24／33／46／51 行標了各棒對應的 pstack 工具。

**檢查後的結論**（逐個讀過 skill 內容）：

- `interrogate` 標在第 3 步（PLANNER 設計驗收方式）**位置不合**：
  它真正在做的是「對抗式審查，**只有跑得出來的發現才算數**」，
  那是**驗收**的動作 → 移到第 5 步（THINKER 驗收）。
- `no-comments` 標在第 5 步**功能重疊**：
  它的功能已被 `thermos` 的 code-quality 路涵蓋 → 拿掉。
- spec 沒標但位置合適、補上去的：
  `benchmark-checklist`（驗收方式設計：門檻、範圍、證據、可重現）、
  `verify-and-stop`（結案判斷：最小足夠證明集合、證明完成就停）。
- `sharpen` 取它的**四問**（專家會做什麼／最原始的材料／方法工具／誰能證明），
  不是「逐題追問」。

**怎麼改**：`fbf_engine/prompts.py` 新增 `METHOD_NOTES`（每棒的方法精神）與
`ISSUE_METHOD_NOTES`；派工提示詞多一段「## 方法」。**放的是精神不是工具名**——
工具可能沒裝，精神照樣能用。

**影響**：提示詞多一段方法指引；對應關係與 spec 不同的地方記在這裡。

## D20. 工單狀態改成十個（新增「重工驗收方式設計中」）

**spec 怎麼說**：第 39~52 行——第 6 步退回 → 第 7 步重工解法 → **第 8 步重工驗收方式**
→ 第 9 步重工 → 回到第 5 步驗收。

**原本**：退回只走 `rework_design → rework_build`，**跳過了第 8 步**
（PLANNER 重工時要重新設計驗收方式、門檻只能更嚴）。

**怎麼改**：新增狀態 `REWORK_ACCEPTANCE_DESIGN`（角色 PLANNER，必交 `selection`＋`checks`），
轉移表 `REWORK_DESIGN → REWORK_ACCEPTANCE_DESIGN → REWORK_BUILD → VERIFY`。
**重工解法也要兩個獨立 SUBAGENT**（跟第一輪同一條硬約束）。

**影響**：工單狀態 9 → 10。

## D21. ISSUE 改成六個狀態，退回上限 2

**spec 怎麼說**：第 26~28 行（撞三次 → ISSUE → THINKER 兩案 → PLANNER 選一＋填對策 →
WORKER 依對策執行 → 生效就關單、沒生效就在同一張單上重複流程）＋第 95 行
（ISSUE 流程跑了兩輪解決不了 → 人類介入）。

**原本**：ISSUE 只有三個狀態（raised／handling／closed）、退回上限 3。

**怎麼改**：ISSUE 六狀態 `raised → designing → handling → building → adjudicating → closed`；
退回重派到 `designing`（重複整個流程）；上限 2（舊預設值 3 會自動遷移成 2，
人自己調過的不動）。`issues` 表新增 `completion_criteria`／`countermeasure`／`selection`。

**影響**：ISSUE 流程的必交欄位全部換新。

## D22. 修掉兩個引擎缺陷（跑完整工作流時抓到）

**(a) 永久退件沒有重新派工 → 單子靜靜卡住**

回執永久失敗（`receipt.message_reused`）或 SUBAGENT context 永久查不到時，
引擎只把提交標成 `rejected`，**不重新派工、不通知角色**。
角色交完就結束回合（它只收到「已收到」）→ 沒有人叫它重交 → 單子原地不動、
沒有錯誤訊息。守衛退件那條路有 `_redispatch_after_rejection`，這條路漏了。

*修法*：新增 `_redispatch_rejected()`，兩種永久失敗都重新派一棒，
並在決策紀錄裡補 `from_state`（退件次數上限才數得到）。


**(c) `apply.rejected` 退件不重新派工 → 這張單靜靜卡住（live 實測）**

`apply_stage` 拋 ValueError 的分支原本只標 rejected、升版本就 return：
不重新派工、也不把原因講清楚。角色交完就結束回合 → 沒有人叫它重交 →
單子原地不動、沒有錯誤訊息。

live 實測（2026-11-23 20:18~22:00）：W-002／W-003 的 `rework_design` 棒
先被 `solution.context_unverified` 擋（THINKER 交了自編的 context id），
再交時改用**第一輪的 context id** → 套用時撞 UNIQUE → `apply.rejected`
→ **沒有重派** → 兩張單靜靜卡了 1.5 小時。

*修法*（三層）：
1. `apply.rejected` 也走重新派工，退件說明帶上引擎實際擋下的原因；
2. **重用這張單用過的 context id 在守衛就擋**（新缺口
   `solution.context_reused`），不必等到套用才炸——而且講得清楚：
   重工解法要的是**兩個新的**獨立 SUBAGENT；
3. **自癒路徑**（`_heal_rejected_tasks`）：task 還開著、但它自己的提交已被
   退件 → 收掉它，讓這張單重新被派。退件路徑萬一又漏了，也不會再靜靜卡住。

**(b) ISSUE 退件不升版本 → 這張 ISSUE 再也推不動**

`_bump_work_version` 只處理 `works`，ISSUE 退件後版本不動 →
角色用同一版本重交 → 撞上 `(task_id, session_id, stage, expected_version)` 唯一約束 →
`submit_issue` 回傳原本那筆（已退件的）submission → **永久卡死**。

*修法*：`_bump_work_version` 改成工單／ISSUE 都升版（改名語意不變）。

**驗證**：`tests/test_workflow_e2e.py`（把 spec 的工作流整條跑完），
其中 `test_issue_still_advances_after_a_permanent_rejection` 是 (b) 的回歸測試。

---

## D23　派工提示詞必帶「上一次為什麼被退」（2026-11-23 追加）

**現場**（引擎版本 `20261123k`→`l`）：上面三層修法讓退件都會重新派工了，
但角色還是「不接」——THINKER 收到重派後回「引擎壞了，我不重交」，
因為**派工提示詞裡沒有說清楚上次被退的原因**：它讀不到上一輪對話，
提示詞又不帶狀態（spec 第 70 行），於是以為自己還沒交過，
交出同樣的東西再被退一次。

**修法**：新增 `_pending_rejection_note(conn, kind, target_id)`——
查「退件了、而且退件之後還沒有任何 applied 的提交」→ 有就把
缺口代碼、缺口說明、**引擎實際擋下的原因**一起帶進派工提示詞。
工單／ISSUE 檔案的「最近一次退件」段落也從檔案最後移到**狀態表之後（最上面）**，
角色從頭讀就能看到最可執行的資訊。

**live 驗證**（2026-11-23 22:26，`20261123l` 上線後第一次重派）：

- W-002：收到帶「`apply.rejected`｜換成新的獨立 SUBAGENT」的重派 →
  THINKER 重交 → **S-000042 applied** → 進入 `rework_acceptance_design`（T-000060）。
- W-003：收到帶「`apply.rejected`」的重派 → 重交時沿用舊 message_id →
  被 `receipt.message_reused` 擋 → 引擎自動重派 T-000061，
  提示詞明講「務必換**新的 message_id** 重交」。

→ 退件 → 重派 → 角色看得到原因 → 重交，這條閉環第一次在 live 上跑通。

---

## D24　「沒選它」變亂碼：型別沒正規化（2026-11-23 追加）

**現場**（`20261123m` 上線前）：三張工單檔案的「選擇哪個解法」段落長這樣——

```
- 選：a821262bd25679534
- 理由：選 checklist-fill 案：成本 3 對比 5……
- 沒選它：S
- 沒選它：O
- 沒選它：L
- 沒選它：-
- 沒選它：0
```

**原因**：角色把「為什麼不要其他案」交成**一個字串**
（`"rejected_because": "SOL-00008（…）：成本 5、風險 medium…"`），
引擎照收（守衛只檢查 `solution_id`／`reason` 在不在、選案合不合法），
渲染時 `for rejected in selection["rejected_because"]` 把**字串當 iterable**
→ 逐字元印成十幾行。

**修法**（兩端都正規化，缺一端就會再犯）：

1. **寫入端**：`workflow.normalize_selection()`——`rejected_because` 一律轉成
   清單（字串 → `[字串]`），在守衛層就轉好再存進正本。
2. **讀取端**：`workfiles` 模組的 `render_work()` 渲染前再正規化一次 →
   **既有資料**（已經存成字串的三張單）下一次 tick 就修好，不用回溯改資料。
3. 順手修掉一個潛在崩潰：ISSUE 的 `selection` 若是字串，
   `.get()` 會炸掉整趟 tick → 改用同一個正規化函式。

**驗證**：`tests/test_workflow_e2e.py` 的 `test_selection_rejected_because_given_as_a_string_stays_one_line`
（存進正本是清單、檔案裡只有**一行**「沒選它」）。

---

## D25　退件缺口統計：引擎擋的是「角色交壞」，不是引擎壞（2026-11-23 追加）

live 跑到 22:4x 的退件統計（11 次）：

| 缺口 | 次數 | 是誰的問題 |
|---|---|---|
| `solution.field_missing` | 3 | 角色漏填解法欄位 |
| `receipt.message_reused` | 2 | 角色沿用舊 message_id |
| `evidence.missing` | 2 | 角色漏填驗收證據 |
| `result.missing` | 2 | 角色漏填驗收結果 |
| `apply.rejected` | 2 | 角色重用舊 subagent_context_id |
| `selection.unknown_solution` / `handoff.missing` / `solution.context_unverified` / `check.threshold_lowered` | 各 1 | 角色交錯型別／欄位／抄錯門檻 |

→ **退件全部是角色端交壞**，引擎該擋的都擋了、該重派的都重派了。
真正要修的是「角色反覆交同一種壞東西」：每次退件要等 LLM 重想 10~20 分鐘，
三張假單跑完就花掉一整天。**假單不會讓角色變準**，只會讓它更快撞到引擎的洞。

---

## D26　取消 SUBAGENT 機制：THINKER 一棒直接產兩案（2026-11-23 追加）

**為什麼改**：使用者判定 SUBAGENT 是坑——兩個 SUBAGENT 各跑一次，**單棒要等十幾分鐘**
（實測 W-001 的重工解法棒，兩個 SUBAGENT 跑了十幾分鐘）；
而且 THINKER 還得**自己編 context id** 交回來給引擎逐案核對，
自己編的 id 過不了核對就退件（`solution.context_unverified`），重交再等一棒。
代價是延遲、還多一層編造風險，換來的「兩案獨立」並不值。

**改成什麼**：THINKER 在同一棒內**直接產出至少兩案解法**，**不分派 SUBAGENT**。

- 解法欄位不變：`approach`／`cost`／`change_scope`／`effort`／`risk`／`verifiable_by`（選填 `reuses`）。
- **不帶 `subagent_context_id`**，引擎**不再回平台核對 context**；
  `solution.context_duplicate`／`solution.context_unverified` 兩個缺口代碼一併取消。
- **PLANNER 選案改用「案號」**：案號＝解法清單的順序（1 起算），
  工單／ISSUE 檔案的解法表多一欄「案號」，PLANNER 在 `selection.solution_id` 填案號（例如 `"1"`）。
- **保留的硬約束**：至少兩案；**兩案不得實質相同**（成本與修改範圍不能都一樣）
  → `solution.too_similar`；解法欄位要齊 → `solution.field_missing`。
- pstack 對應不變（`architect`、`how`、`why`）。

**代價（誠實寫）**：兩案不再由兩個獨立代理產生，**獨立性下降**——
從前是兩個 session 各想各的，現在是同一個 THINKER 一次交兩案，
同一份上下文容易讓兩案收斂成同一個思路。
「兩案不同」因此**只能靠內容檢查**（成本與修改範圍不能都一樣），
沒有第二個上下文當天然的對照組。
交接檔八欄的 `SUBAGENTS RUNNING` **維持原樣不動**——那是角色的自報簿，不是這套機制。

**規格同步**：`spec/FBF ENGINE工作流.md`、`spec/ISSUE單設計.HTML`、
`spec/工單欄位對照表.html` 已同步；`spec/工單設計.HTML`（解法表加「案號」欄）
與對外契約 `README.md`、`SKILL.md` 一併同步。


---

## D27　角色不回應 → 引擎自己收掉重派（2026-11-23 追加）

**為什麼需要**：前面所有「重派」路徑都假設**角色會看到派工並重交**
（守衛退件、回執永久失敗、context 永久查不到、`apply.rejected`）。
但角色可能**根本不理**——人不在、session 卡住、指令壞掉、忘了。
那時 task 一直開著、沒有人回報、單子原地不動、**沒有錯誤訊息**
（live 實測：W-001 在 19:51 那次是人工 cancel task 才解開）。

**修法**：`engine._expire_stalled_tasks()`，每趟 tick 檢查：

- **計時基準＝角色最近一次交件**，不是某一棒的建立時間。
  同一個 session 排了三棒時，後面兩棒是「在排隊」不是「失聯」——
  用建立時間算會把它們誤判成逾時、連環收掉。
- **一次只收最早的那一棒** → 不會把整排一次砍光。
- 已經有提交在途（pending／unverified）的那棒不算失聯。
- 等人裁決的單子（`awaiting_human`）不動。
- 逾時（`ROLE_STALL_SECONDS`，預設 30 分鐘）→ 收掉舊 task、重派一棒，
  提示詞講清楚「這一棒 N 分鐘沒交件，重新派」（角色才知道要接手）。

**驗證**：`tests/test_workflow_e2e.py` 的 `TestStalledRoleGetsRedispatched`
兩條——失聯會被收掉重派；**角色還在交件時不會被收掉**（防誤殺）。

---

## D28　租約被舊版行程搶走 → 專案改由舊邏輯驅動（2026-11-23 追加）

**現場**（`20261123n` → `o`，23:00~23:05 live 實測）：

```
23:00:43  新版（n）接手租約，generation 966
23:01:17  舊版 MCP server（j）取得租約，generation 967   ← 搶走
23:02:04  新版搶回來，generation 968
```

**原因（兩個洞疊在一起）**：

1. **一趟 tick 可能比租約 TTL（30 秒）還久**：派工要等宿主 CLI 提交
   （最多 30 秒）、回執核對、`delivery_state` 都會等。
   續約卻只發生在 **tick 之間** → tick 跑到一半，租約到期 → 別人拿得到。
2. **到期後誰先來誰拿到**：正常取得路徑不看版本 → **舊行程剛好搶到**，
   這個專案就改由**舊版程式碼**驅動 → 今天修的所有修正都失效，
   直到新版再搶回來為止（實測整整一分鐘）。

**修法（兩道防線）**：

1. **背景續約**（`Coordinator._start_lease_renewer`）：
   持有者活著就續約，不受慢 tick 影響；續約失敗（別人接手）就安靜放棄。
2. **取得租約時先看版本**（`Coordinator.acquire`）：
   持有者是**舊版**就直接強制接手，**不論租約有沒有到期** → 不給空窗期。

**驗證**：`tests/test_workflow_e2e.py` 的 `TestLeaseStaysWithTheNewestVersion`
三條——新版從舊版手裡強制接手（租約有效時／已過期時）、背景續約把過期租約救回來。

---

## D29　「沒有人接手」不能靜靜卡住（2026-11-23 追加）

**現場**（00:43~10:41，10 小時零提交）：角色沒認領席位 → 派工送不出去。
引擎每 30 秒寫一筆 `dispatch.verify`（結果 unknown），**卻沒有任何一筆訊息
說「這張單沒人接」** → 沒有訊息就沒有人知道要開 session。

**修法**：`_dispatch_pending` 在「派不出去」時寫一筆 `errors`：
「工單 W-001 在 解法設計中 沒有可用席位（THINKER 沒人認領），派不出去」。
配合 D27 的逾時回收，**沒人接手這件事從此有聲音**。

**驗證**：`tests/test_workflow_e2e.py` 的 `TestNoSeatIsNotSilent`。

---

## D30　「出聲的只能是引擎」：訊息要留在正本、而且問得到（2026-11-23 追加）

**現場**（00:43~10:41）：角色沒認領席位 → 派工送不出去、10 小時零提交，
而引擎**完全沒有留下任何訊息**。tick 報告會被下一次覆蓋，journal 只有
`dispatch.verify` → 事後根本查不到「這張單在等人」。

**修法**（三層，都要由引擎自己做）：

1. **journal 留痕**：`dispatch.no_seat` 事件（工單／ISSUE、狀態、缺哪個席位）。
   每個 (單, 狀態) **只記一次**（用 `counters` 表當旗標）→ 不會每 30 秒灌一筆。
2. **引擎的狀態看得見**：`Coordinator.unassigned_targets()` 列出所有
   「派不出去、沒人接手」的單子；MCP 的 `fbflow_status` 把它帶出去
   （欄位 `unassigned`）→ 人問引擎就問得到。
3. **角色端不該知道**：這是引擎對人講話，不是派工提示詞的一部分——
   提示詞只給「這一棒要交什麼」，席位問題不該塞給角色。

**驗證**：`tests/test_workflow_e2e.py` 的 `TestNoSeatIsNotSilent` 三條——
報告有訊息、journal 留得住、`unassigned_targets()` 問得到、而且不重複灌。

---

## D31　巡檢＋喚醒：引擎自己找出哪張單有問題，並叫他回來（2026-11-23 追加）

**使用者指示**：「引擎要去檢查是不是哪張單據有問題」＋
「依照單據狀態判斷現在是誰應該要在做，如果沒有就送個提示詞做喚醒」。

**巡檢**（`Coordinator.health_check()`）——引擎自己看四種病：

| 代碼 | 判斷 | 誰該處理 |
|---|---|---|
| `no_seat` | 這張單該誰做，但**那個角色沒人認領席位** | 人（開 session） |
| `undeliverable` | 有開著的 task，但派給的 session **不在角色表上**（換人或死掉） | 人（重新認領） |
| `stuck` | 單子 **45 分鐘沒有任何提交**（且沒有在途提交、不在等人裁決） | 引擎（喚醒） |
| `awaiting_too_long` | 標了待人處理但 **4 小時沒人裁決** | 人（裁決） |

結果寫進 `fbflow_status` 的 `health` 欄位、`fbf.py engine status` 的 `health`，
新問題另外寫一筆 `health.problem` 到 journal（每個問題只記一次）。

**喚醒**（`Coordinator._wake_up_idle_roles()`）——巡檢說了還不算，
**要真的推一把**：

- 觸發：某一棒開著、**沒有提交在途**、而且這個角色最近 10 分鐘沒動靜；
- 內容：依**單據狀態**講清楚「現在輪到 <角色> 做、單據是哪一張、這一棒是什麼」；
- 上限：同一棒最多 3 次、兩次至少隔 10 分鐘（避免一直叫）；
- **錨點**：喚醒提示詞**不放 task_id**，改放獨立錨點（`WU…`）——
  因為回執核對（`hostterm.verify_host`）會把「user 項含 task_id 且之後有
  assistant 回覆」當成**這一棒已交件** → 喚醒帶 task_id 會製造**假回執**。
  送達狀態改用錨點核對，真的 task_id 完全不受影響。

**驗證**：`tests/test_workflow_e2e.py` 的
`TestWakeUpCallsTheRoleThatShouldWork`（三條：會叫、叫有上限、角色在做事就不叫）
與 `TestHealthCheckFindsTheBadTicket`（四條）。

---

## D32　席位對位要**比對專案**，不只是比對角色標題（2026-11-24 追加）

**規格怎麼說**：規格沒有 host CLI（D11 已記錄整體通道），席位對位格式是
D11 定的 `<ROLE>/<SHORT_NAME>`。

**為什麼必須改**：實測踩到——兩個專案各開一組宿主終端（tab 標題都是
`PLANNER/P1`），舊專案的席位 handle 死掉後，`_rebind_by_title` **只看 tab 標題**
（`engine.py` 裡完全沒有 worktree／專案判斷）→ 把舊專案的席位**改綁到新專案的
終端** → 舊工單的提示詞會送進**錯目錄**的工作階段。

同一輪還抓到第二個坑：`terminal list` 的列舉順序跟角色無關，而填空席位是
「依序填空」→ thinker 的終端被綁成 PLANNER（錯位一格）。

**怎麼改**：

- **標題格式**（`hostterm.seat_title`）：`<ROLE>/<SHORT>` → `<專案>/<ROLE>/<SHORT>`
  （例：`目錄拆產品圖/PLANNER/P1`）。**專案名是跨專案共用的對位依據**，
  不帶就會撞。專案名裡的 `/` 換成 `-`（`sanitize_project_name`）。
- **解析**（`hostterm.parse_seat_title`）：兩種格式都認；回
  `{project, role, short_name}`，形狀不對就回空字典（寧可不對位，也不要猜）。
- **對位**（`engine._session_in_project`）：**兩個證據都要過**——
  1. 標題的專案段（有的話）必須等於本專案名；
  2. 終端所在路徑（`worktree_path`）必須是本專案根目錄或其上層。
     標題**沒有**專案段（舊格式）時，路徑必須**正好等於**本專案根目錄。
  兩個證據都缺 → **不綁**（寧可空著，也不要誤綁）。
- **先看標題再填空**：標題指定了席位就綁那個席位（大小寫不拘，
  `thinker/t1` 與 `THINKER/T1` 視為同一個）；標題指定了但沒有這個席位 → **不綁**。
- 引擎寫回 tab 標題時用 `_seat_title(role, short)`（帶專案名）。

**影響**：`roles` 表的綁定多一層專案判斷；tab 標題從 `PLANNER/P1` 變成
`<專案>/PLANNER/P1`。舊格式標題仍可對位（靠路徑證據），不會因此失效。

**驗證**：`tests/test_host_channel_e2e.py` 的 `TestCrossProjectRebind`
（六條：別的專案不換綁／同專案仍換綁／新格式名對得上才換綁／新格式名錯不換綁／
標題帶專案名／專案名含 `/` 會被轉換）＋ `HostChannelHarness` 的
`test_auto_enroll_uses_pane_title_not_list_order`、
`test_auto_enroll_ignores_other_project_sessions`。

---

## D33　CLI **不得在錯的地方建立正本**；釋放席位要說清楚動到哪裡（2026-11-24 追加）

**規格怎麼說**：規格第 7 節要求「**一個專案一份 `fbf_engine.db`**」；
第 11 節的人類 CLI 是「人類控制面」，讀寫的是**那個專案**的正本。
「MCP 根目錄不記任何專案」是本包自己一直遵守的規則（D1 起的既有規則：
`_registry_projects` 永遠回空）。

**為什麼必須改**：實測踩到——使用者在 **MCP 套件根目錄**跑

```
python3 fbf.py role release --role PLANNER --short-name P1
```

回報 `ok:true, released_session:""`，但重新認領仍被 `ROLE-SLOT-TAKEN` 擋下。
根因是 CLI 的專案解析：`--project` 未給也沒有 `FBF_PROJECT_ROOT` → 落到
**目前目錄**；接著 `store.connect` ＋ `store.init_db` 會**在那個目錄憑空建立**
一份空正本 → 這道指令讀寫的是一份**沒人認領的空資料庫**，
而真正的專案席位原封不動。回報卻是「ok」——
**人在錯誤的地方操作，拿到的是成功的回應**（使用者原話：
「懷疑釋放跟認領讀的是不同地方」——判斷正確）。

同一輪還發現三個連帶的問題：

1. **釋放席位只作廢 task，不清「還沒送出去的派工」（outbox）**。
   收件人是已死的 session → 送不出去 → 每 30 秒重查一次、一直累積
   `unknown`（實測累積 27 次），還可能誤把工單標成待人處理。
   喚醒類的 outbox **查不到對應 task** → `_flush_outbox` 的
   「task 已關閉」孤兒檢查撿不到它 → 只能由釋放這條路清。
2. **席位本來就空著時，回報仍是 `ok` ＋ `released_session: ""`**，
   看起來像「釋放了什麼」，實際上什麼都沒動。
3. **`_flush_outbox` 沒有「收件人已死」的判準**——只靠 task 狀態判斷孤兒。
   喚醒類的 outbox 查不到對應 task → 收件人死後**永遠重查**：
   實測一個死 handle 累積 27 筆 `dispatch.verify unknown`，outbox 也永遠佔著。

**怎麼改**：

- **CLI 的專案解析分成兩條路**：
  - `_project_root()`（所有讀寫正本的指令走這條）——目標目錄**必須已經是
    FBF 專案**（有 `fbf_engine.db`），否則回 `project.not-found` 並指名
    要 `--project <專案路徑>`。**不再憑空建立正本。**
  - `_root()`（只有 `init`／`join` 走）——這兩個指令本來就是為了建立正本。
- **MCP 根目錄拒絕當專案**：`_project_root` 與 `init`／`join` 都擋下
  `paths.mcp_root()`（安裝狀態必須與專案無關）。
- **釋放／撤銷一併清掉遺留派工**：`release_role` 與 `session revoke`
  會把該 session 名下 `queued`／`unknown`／`sending` 的 outbox 標成 `failed`
  （與 `_rebind_by_title` 同一條路），回報多出 `dropped_outbox`。
- **回報要說清楚**：`role release` 多回 `was_occupied`（席位本來有沒有被占著）
  與 `project_root`（**實際動到哪一份正本**）；席位本來就空著時，
  `note` 明講「沒有東西可釋放」，不再看起來像成功了。
- **`--project` 放在子命令後面也要有效**：文件與習慣寫法都是
  `fbf.py role release --project <專案>`，但 `--project` 原本是全域選項、
  只能放在子命令**前面** → 照文件寫會直接報「unrecognized arguments」。
  改成每一層子命令都接受 `--project`（子命令後面的值優先）。
- **`_flush_outbox` 加一條「喚醒的收件人已不是任何席位」的判準**：
  喚醒的 `task_id` 是**喚醒錨點 `WU…`**（不是 task 編號）→ 「task 已關閉」
  那條孤兒檢查撿不到它 → 收件人死後每 30 秒重查一次、永遠不停。
  判準：`roles` 表裡沒有任何席位認領這個 session → 標 `failed`，不再重查。
  **只限喚醒類**：派工類的 `task_id` 是真的 task 編號（那條孤兒檢查會處理）；
  而 ISSUE 裁決是派給「**發起者 session**」（可能已不在席位上，那是人的事，
  不該由這裡判死）→ 不能一併套用。

**影響**：

- 在**非專案目錄**跑任何讀寫正本的 CLI 指令 → 從「靜靜建立空正本並回報成功」
  變成「報 `project.not-found` 並指名要 `--project`」。**這是刻意的行為改變**：
  寧可報錯，也不要寫到錯的地方。
- `role release`／`session revoke` 的回應多兩個欄位（`was_occupied`／
  `dropped_outbox`、`project_root`），既有欄位不變。
- 套件根目錄裡誤建的 `fbf_engine.db` 已清掉（它違反「MCP 根目錄不記任何專案」，
  而且是 MCP 端 `resolve_project` 的 cwd fallback 誤判的來源）。

**驗證**：

- `tests/test_cli_e2e.py` 的 `TestCliWrongRoot`（五條：非專案目錄被拒且
  **不建立正本**／明確指到錯目錄同樣被拒／回報寫出實際正本路徑／
  空席位的釋放說明「沒有東西可釋放」／MCP 根目錄不能當專案）。
- `tests/test_cli_e2e.py` 的 `TestCliEndToEnd.test_revoke_drops_undelivered_dispatch`。
- `tests/test_fbf_engine.py` 的
  `TestReleaseCancelsOpenTasks.test_release_also_drops_the_undelivered_dispatch`、
  `TestSeatLifecycle.test_release_of_an_empty_seat_says_nothing_was_released`、
  `TestOutboxDeliverySafety.test_wakeup_to_a_dead_session_is_dropped_not_retried_forever`。
- `TestCliWrongRoot.test_project_option_works_after_the_subcommand`。
- 測試總數 237 → 247。


---

## D34. 取消工單／ISSUE 的磁碟檔案層：SQLite 是唯一資料正本

**規格怎麼說**：`doc/spec-fbflow-v2-first-principles.md` 只要求「SQLite 正本」
＋ `fbflow_read` 回「本角色可見的正本快照與 version」（第 11 節），
**沒有**要求任何工單資料夾。舊規格（`spec/FBF ENGINE工作流.md` 第 70~80 行）
要求「提示詞內容不包含工作狀態，所有狀態都在文件中」並規定工單資料夾結構。

**為什麼必須改**：使用者裁決——「把工單輸出到文件夾這件事取消，
多一個版本多一個錯亂的機會，讓 SQLite 變成唯一的資料正本」。

檔案層是**正本的第二份投影**：每次狀態變動都要重寫、結案要搬資料夾，
兩份東西只要有一份沒同步（寫檔失敗、搬檔失敗、行程在兩者之間死掉），
角色就會照著**舊狀態**做事 → 交件被退、流程原地打轉。
實測（2026-11-24）：`工單/` 資料夾裡的 W-002／W-003／W-004 已經落後正本，
而角色讀的是檔案。

**怎麼改**：

1. **刪掉整個工單檔案層**（模組 `workfiles.py` 與它的寫檔／搬檔路徑），
   引擎不再產生任何 `工單/`、`ISSUE/` 資料夾。
2. **交件契約改由派工提示詞直接攜帶**：來源仍是 `workflow.stage_contract`／
   `issue_stage_contract`（**與守衛同一份定義**），渲染在 `prompts.contract_lines()`。
   規格第 7 節本來就要求「所有通知包含 task_id、工單 ID、讀取工具與本棒交件清單」。
3. **工單現況由角色自己查正本**：`fbflow_read(target_kind=work|issue)` 回傳
   `work`／`checks`／`results`／`solutions`／`handoff`，另外新增
   `contract`（這一棒要交的欄位）與 `last_rejection`（最近一次退件 + 引擎擋下的原因）。
4. **退件資訊不再靠檔案**：改由 `workflow.last_rejection()` 結構化提供，
   派工提示詞與 `fbflow_read` 共用同一份。

**取代的既有差異**：D17、D18 記的是「把契約搬進工單檔案」——
檔案層不存在了，兩條的**手段**失效，但**目的**（角色一定看得到交件契約）
由第 2、3 點接手。

**影響**：

- 專案目錄裡**不再**長出 `工單/`、`ISSUE/`。既有專案裡已經生成的資料夾
  是**殘留物**，不會被引擎再更新（也該清掉——那是刪資料，需人類授權）。
- 派工提示詞變長（多一段「現在這一棒」交件清單）——這是規格本來要求的。
- `fbflow_read` 的回應多了 `contract` 與 `last_rejection` 兩個欄位。

**驗證**：

- `tests/test_fbf_engine.py` 的 `TestDispatchCarriesContract`
  （契約進提示詞、提示詞不帶狀態、不指向檔案）。
- `tests/test_workflow_e2e.py` 的
  `test_full_path_to_closed_writes_no_work_files`、
  `test_dispatch_carries_skill_and_solutions_live_in_the_workbook`、
  `test_issue_path_to_closed`（全程不寫檔案）。
- `tests/test_mcp_server.py`（`fbflow_read` 回 `contract`／`last_rejection`）。

---

## D35. 回執逾時不再叫角色重做：停等人，不燒掉角色已經做完的工

**規格怎麼說**：第 11 節——「提交 `unverified` 後如何重查：timer 查 `pending`
與 `unverified`，直到回執到達或 10 分鐘截止；**逾時標 `awaiting_human`
並保存原 submission，不自動宣稱失敗**」。

**為什麼必須改**：舊實作到期後是「submission 標 `expired` → 作廢 task →
換新 task → 派一句『【回執未核對】退回補齊，請用新的 message_id 重交』」，
連續三次才停等人。

**那是純浪費時間，不是在工作**：角色已經把那一棒做完了，引擎只是**核不到證據**
（多半是平台端的證據視窗被截掉），就把它的工丟掉要它重來。
使用者回報的「【回執未核對】…退回補齊」正是這條路徑。

**怎麼改**：

1. 到期 → 工單／ISSUE 標 `awaiting_human=receipt_timeout`（`awaiting list` 看得到）。
2. **不重派、不叫角色重做**；原 submission 與原 task 都留著
   （task 留著開著 → 不會被「角色沒動靜」那條路收掉重派）。
3. 證據晚到 → **自動套用**（回執一核到就套用並解除停等）→ **角色不用再交一次**。
4. 人裁決分成兩種：`grant_more`（繼續等證據，**不動 state**）／
   `revise`（真的要重做 → 退回 `rework_design`）／`abort`（收掉）。

**影響**：

- 回證核不過時，流程會**停下來等人**（而不是自動把角色操練三遍）。
- 舊的「換新 task 重試」路徑（`receipt_retry`）已移除。
- `max_receipt_expires` 計數器保留（記錄到期次數、裁決後歸零），但不再用來決定重試。

**驗證**：

- `tests/test_fbf_engine.py` 的 `TestReceiptTimeoutParksWithoutBurningTheRole`
  （逾時停等人、不重派、task 留著、證據晚到自動套用、`grant_more`／`revise` 兩種處置）。

---

## D36. 退件說明改成**逐欄位**：哪個欄位、為什麼、標準、範例

**規格怎麼說**：第 5 節——硬拒絕要回「穩定缺口代碼、**缺哪個欄位**與可行下一步」；
第 7 節——payload 的欄位集合由第 5 節的交件契約決定。

**為什麼必須改**：舊實作退件只丟缺口代碼（`solution.field_missing`）
＋一句籠統的 hint（「有案子缺欄位；每案都要有 approach、cost、…」）。
角色讀不到上一輪對話，又不知道**是第幾案、哪一格**，→ 交出同樣的東西再被退
（live 實測：THINKER 回「引擎壞了，我不重交」）。

**怎麼改**：

1. `workflow.FIELD_STANDARDS`：每個欄位的**型別、規則、範例**（規則正本一份）。
2. `workflow.explain_form(stage, payload, issue=False)`：照**同一份契約**
   （`stage_contract`／`issue_stage_contract`）逐欄位檢查這一筆交件，
   回 `{code, field, problem, standard, example}`。它是**解說**，不是守衛——
   真正的判定仍在 `check_stage`。
3. 退件說明（`engine._rejection_note`）逐條唸出來：
   `欄位 solutions[1].cost：缺漏…／標準：…／範例：…`；
   決策紀錄的 `detail.gap_details` 留著同一份（可追溯）。
4. **表單以外的欄位是附加項**：引擎不檢查、也不會因為它們退件
   （派工提示詞與退件說明都明講這句）。

**影響**：

- 派工提示詞的「現在某一棒」多兩行（標準＋範例）——角色**交件前**就知道怎麼填。
- 退件提示詞變長，但每一行都是可執行的資訊（哪一格、為什麼、標準、範例）。
- 條件式必交欄位（`adjudicate` 的 `close_reason`／`rework_reason`／…、
  ISSUE `adjudicating` 的 `resolution`／`return_reason`）**只查當前該交的那組**，
  不會叫角色補一堆用不上的欄位。

**驗證**：

- `tests/test_fbf_engine.py` 的 `TestRejectionExplainsTheField`
  （指名欄位／原因／標準／範例／附加項不影響判定／型別不符的說明）。

---

## D37. 孤兒喚醒標成 failed 之後，不再走一次送出路徑

**規格怎麼說**：第 6 節——派送失敗要有上限、不得無限重試。

**為什麼必須改**：D32 加的「喚醒類 outbox 的收件人已不是任何席位 → 標 failed」
只擋在 `queued`／`unknown`／`sending` 三種狀態；標成 `failed` 之後，
`failed` 本身是**可重試**狀態 → 下一輪又走一次送出路徑 → 送不出去又變回
`unknown` → 每 30 秒敲一次宿主 CLI。

線上實測（2026-11-24）：outbox 3（一個死 handle 的喚醒）累積 40+ 次
`dispatch.verify unknown`；而且它每次都會去問平台「這筆送達了沒」。

**怎麼改**：`failed` 也納入孤兒判準 → 這筆就地停住，不再打擾平台
（仍然每輪被撿一次，但只是個 DB 更新，不碰平台）。

**影響**：死收件人的喚醒不再每 30 秒打一次平台。

**驗證**：`tests/test_fbf_engine.py` 的
`TestOutboxDeliverySafety.test_orphan_wakeup_stays_failed_and_stops_calling_the_host`。

---

## D38. 喚醒訊息帶上**交件契約**與**退件原因**（不再叫角色去讀已停更的工單檔）

**規格怎麼說**：第 5 節——交件契約由引擎決定；第 11 節——引擎要主動推一把，
不能只是巡檢時說一句。

**為什麼必須改**：live 實測（2026-11-24 12:44，專案「目錄拆產品圖2」）：
喚醒訊息寫的是「照工單檔案的『現在某一棒』交件」——但 D34 把工單／ISSUE 的
**磁碟檔案層**拿掉了，那份檔案**不再更新**。PLANNER 收到喚醒後去讀那份舊檔、
搞不清楚自己要交什麼，最後**什麼都沒交**；使用者看到的就是「引擎停了」。

**怎麼改**（`engine._wake_up_idle_roles()`）：

1. 喚醒訊息內嵌**這一棒的交件契約**：來源 `workflow.stage_contract`／
   `issue_stage_contract`（與守衛**同一份**定義），用 `prompts.contract_lines()`
   渲染——要交的欄位、標準、範例。
2. 喚醒訊息明講**現況從哪裡查**：`fbflow_status()` 拿 task_id、
   `fbflow_read(target_kind, target_id)` 讀正本快照。
3. 喚醒訊息帶上**上次為什麼被退、要怎麼改**（`workflow.last_rejection()`
   → `engine._rejection_note()`，逐欄位說明）。
4. **喚醒訊息不帶 task_id**（帶了會被回執核對當成假回執），所以退件說明裡
   「用上面那個 task_id」改成「task_id 用 `fbflow_status()` 查你的待辦」
   （`_rejection_note` 新增 `task_hint` 參數）。

**影響**：

- 喚醒訊息變長，但每一行都是可執行的資訊。
- 派工／重派訊息的退件說明**不變**（那裡本來就有 task_id）。

**驗證**：

- `tests/test_fbf_engine.py` 的
  `TestReceiptTimeoutParksWithoutBurningTheRole.test_wakeup_carries_the_contract_and_the_rejection_reason`
  （帶契約欄位／帶 `fbflow_read`／**不含**「工單檔案」／**不含** task_id）。

---

## D39. 後台網頁（唯讀儀表板）：即時看「誰在寫、哪個欄位改成什麼」

**規格怎麼說**：第 11 節——「人類 CLI 只提供 `init`、`requirement confirm`、
`engine status`、`engine tick --once`、`awaiting list`、`awaiting resolve`、
`project close`」。

**為什麼必須加**（使用者需求 2026-11-24）：引擎在跑、單子看起來停住時，
人只能靠讀終端畫面猜「是沒派工？角色沒回？還是退件了？」
「誰卡在哪一棒、剛剛哪個欄位被改成什麼」**只存在引擎的記憶體裡**，
人看不到 → 引擎變成黑箱。

**怎麼加**（`fbf_engine/dashboard.py` ＋ `fbf.py dashboard`）：

1. **純讀旁路**：只讀 SQLite 正本，**不寫正本、不拿租約、不打擾任何 session**。
   開一次頁面不讓引擎狀態有任何變化（有測試守著）。
2. **「誰在寫」要誠實**：
   - 開著的 `tasks` → 那一棒派給誰，誰就是正在寫的人；
   - 席位沒人認領（`roles.session_id` 為空）→ 顯示「席位無人認領」，
     **不假裝有人在寫**（那會讓人誤判卡在哪）；
   - 沒有開著的 task → 不顯示殘留的角色。
3. **「哪個欄位改成什麼」純讀衍生**（**不改引擎、不加資料表**）：
   - 退件 → `decisions.detail.gap_details`（引擎已算好的逐欄位差異：
     哪個欄位、標準、範例）直接帶上；
   - 套用 → `submissions.payload` 摊平成欄位 → 值；
     同一欄位前後兩次值不同 → 顯示「**舊值 → 新值**」。
   - 欄位標準沿用 `workflow.FIELD_STANDARDS`（同一份規則正本，不另寫一份）。
4. **視覺**：卡片進場動畫、進行中卡片有脈動側邊與光暈頭像、
   時間軸逐條滑入、舊值刪除線、新值綠色、SSE 即時推播（前端不輪詢）。
5. **安全**：**預設只綁 `127.0.0.1`**（頁面含工單內容與席位資訊）；
   要區網可看必須明確指定 `--host 0.0.0.0`。

**這是規格差異**：第 11 節的命令清單被多了一個 `dashboard`。
**為什麼可以**：它是**唯讀的旁路**，不是新的控制面——
不新增任何寫入／裁決能力，第 11 節要防的（session 透過 CLI 取得控制權）沒變。
**不改 `engine.py`** → `ENGINE_VERSION` 不必 bump（協調器行為完全沒變）。

**影響**：

- 多一個模組 `fbf_engine/dashboard.py`（純標準函式庫，無第三方相依）。
- 多一個 CLI 命令 `fbf.py dashboard`（唯讀）。
- 引擎與正本**零變動**——不需要重載常駐協調器。

**驗證**：

- `tests/test_dashboard.py`（10 項）：唯讀不變狀態／誰在寫／
  席位無人認領要誠實／套用帶新值／同欄位改動帶前後值／
  退件帶逐欄位說明／頁面與 API 可開／排序鍵不炸／不是專案要報錯。

---

## D40. 專案層級結案檢視（`project_review`）：**結案 ≠ milestones**

**規格怎麼說**：第 8 節把「專案結案」寫在里程碑那一段的最後一句：
「專案結案須雙向查核：每項已確認需求有結案工單與通過證據；每項結案工單能追到已確認需求；
**最後由人類確認**。」第 11 節的工具表只有 8 個工具，沒有任何通道能讓引擎**主動**觸發結案判定。

**為什麼必須改**（使用者澄清 2026-11-24）：

> 「結案跟 milestones 是兩回事。milestones 是用來防止任務施工流程很長、在過程中偏移方向。」

- **milestones＝分段**（防止長流程偏移方向）；**結案＝專案層級的目標達成判定**。兩件事。
- 舊路徑把結案綁在里程碑上 → 專案**一個里程碑都沒有**時，結案永遠不觸發。
  （實測：活躍專案 `目錄拆產品圖2` 的 `milestones` 表是空的，四張單卻全部 closed。）

**怎麼改**（`fbf_engine/workflow.py` ＋ `engine.py` ＋ `mcp_server.py` ＋ `store.py`）：

1. **新階段** `STAGE_PROJECT_REVIEW = "project_review"`（**task-only**：不掛任何工單，
   跟里程碑無關）＋新常數 `PR_COMPLETE`／`PR_ADD_WORK`／`PR_AWAIT_HUMAN`
   ＋狀態 `PR_REVIEWING`／`PR_CLOSED`／`PR_AWAITING`／`PR_REOPENED`。
2. **觸發條件**（全部成立才派）：至少一張工單、**所有工單都 `closed`**、
   沒有任何開著的 task、沒有待人處理的單、專案尚未結案、
   **milestones 沒有 `reviewing`**（段末對照優先，順序不可反）。
   **完全不看 `milestones` 表有沒有資料**。
3. **一次派工、一個回報**（使用者裁決：復盤與結案判定同一棒做完）：
   新增第 10 個 MCP 工具 `fbflow_report_project_review(task_id, expected_version,
   action, reason, new_works, improvements)`（`for_roles=["PLANNER"]`）。
4. **動作**（跟里程碑檢視同一套三選一）：
   - `complete` → 引擎**自動跑雙向追溯**（第 8 節，與人類 `project close` **共用同一個
     `_traceability_check()`**）→ **通過才標專案結案**；不通過回 `traceability.incomplete`
     （**不許假結案**），PLANNER 改用 `add_work` 補。
   - `add_work` → 針對未完成的部分開新工單 → `state=reopened`，
     新工單完成後**再觸發一輪**檢視（version + 1）。
   - `await_human` → `state=awaiting`。
5. **四道核對**（跟里程碑檢視同一套）：stage 必須是 `project_review`、task 必須還開著、
   必須是受派者、`expected_version` 必須等於正本版本；角色限 PLANNER。
6. **派工提示詞**帶 `task_id`（回執核對靠它）、`expected_version`，
   加上三份摘要：**任務目標**（已確認需求＋成功判準）、**產出**（各單 verdict 與
   required check 的 PASS 數）、**退件紀錄**（復盤依據）。

**與規格的偏離（要寫在這裡）**：第 8 節說「**最後由人類確認**」，
本實作是「**PLANNER 判定達成 → 引擎自動追溯 → 追溯通過就標結案**」，
**人類仍可覆核**（`fbf.py project close` 照舊；人也可在 `await_human` 分支接手）。
**為什麼**：使用者裁決「PLANNER 判定達成就標專案結案」——否則結案還要再排一個人類回合，
而 §8 的雙向追溯已經是客觀判定條件。人類覆核路徑沒有被拿掉。

**影響**：工具數 9 → 10；多兩張表（`project_reviews`、`engine_improvements`）；
`tasks` 多一欄 `project_review_id`；`ENGINE_VERSION` `20261124h` → `20261124i`。

---

## D41. **每一次退件都留 LOG** ＋ **結案復盤（引擎改善建議）**

**規格怎麼說**：第 5 節要求硬拒絕回傳「穩定缺口代碼、缺哪個欄位與可行下一步」；
第 8 節沒有要求「留退件紀錄」，也沒有要求「結案後復盤」。

**為什麼必須加**（使用者規則 2026-11-24）：

> 「引擎每一次退件都要留下 LOG，內容必備：退件單據的完整副本、退件原因、
> 為了哪個欄位退件、退的是誰的件。」＋「每次結案後依退件紀錄做復盤，
> 提出引擎改善建議。」

**怎麼改**：

1. **新表 `rejection_log`**：`target_kind, target_id, submission_id, stage, session_id,
   role, short_name, gaps, fields, reason, payload, target_snapshot, details, created_at`。
   由 `engine._mark_submission()` 在 `status=rejected` 時**自動**寫（兩個守衛退件點都經過它）。
   - **完整副本** → `payload`（被退的那次交件內容）＋ `target_snapshot`（單據當下的整列）
   - **退件原因** → `reason`（缺口代碼的人可讀說明）＋ `gaps`（原始代碼）
   - **為了哪個欄位** → `fields`（逐欄位解說裡的欄位名）
   - **退的是誰的件** → `session_id` ＋ `role` ＋ `short_name`
   通過的交件**不產生**紀錄。
2. **結案復盤**：**跟結案判定同一棒**（使用者裁決），回報時多交 `improvements`
   （每筆 `{target, suggestion, reason}`）。
   寫進新表 **`engine_improvements`**；後台網頁顯示；CLI `fbf.py review improvements` 可讀。
3. **CLI 新增 `review` 子命令群**（唯讀）：`review improvements`、`review rejections`。

**影響**：多兩張表、一組唯讀 CLI 命令、儀表板多兩個區塊。
`ENGINE_VERSION` 同 D40 一併 bump 到 `20261124i`。

---

## D42. 逾時收單的計時基準修正 ＋ 結案檢視單被收掉會重新派

**規格怎麼說**：第 8 節要求「卡死會停」（角色不回應 → 引擎收掉那一棒並重派）。

**為什麼必須改**（2026-11-24 線上實測發現）：

專案結案檢視派出去 1 分鐘後就被當「角色失聯」收掉：

```
journal 550  task.stalled  {"role":"PLANNER","idle_minutes":162}
```

原因是逾時偵測的計時基準只有「**這個角色最近一次交件**」：
PLANNER 上一棒交完後閒置 162 分鐘 → 剛派出去的檢視單**建立才 1 分鐘**，
卻因為「角色很久沒交件」被收掉。而且檢視單**沒有 `work_id`／`issue_id`**，
逾時路徑不會補一張新單 → **這一輪的結案判定永遠卡住**。

**怎麼改**（`fbf_engine/engine.py`）：

1. **計時基準取「角色最後一次交件」與「這一棒建立時間」的較晚者**：
   排隊中的後面幾棒本來就不該被連環收掉（這是原設計的意圖），
   剛派出來的一棒也一樣——它至少要有 `ROLE_STALL_SECONDS` 的回應時間。
2. **結案檢視單被收掉 → 重新派一輪**（比照里程碑檢視的路徑：
   「state=reviewing 但沒有開著的檢視單」→ 重新派，version + 1）。
   否則逾時收單＝結案判定卡死。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261124i` → `20261124j`。
新增 2 項測試（`tests/test_project_review.py`）。

---

## D43. 「MCP 根目錄不記任何專案」補上**三道閘門**（同一個問題第二次復發）

**規格怎麼說**：第 4 節、第 11 節——安裝 MCP 只是把引擎的入口放進設定，
跟任何專案無關。**MCP 根目錄裡沒有任何跟專案有關的東西**。

**為什麼必須改**（2026-11-25 線上發現）：

引擎的安裝目錄底下又出現了一個 `fbf_engine.db`。它讓三個既有測試紅掉
（`test_mcp_root_cannot_hold_a_project`／
`test_unbound_project_is_refused`／
`test_session_cannot_point_at_arbitrary_path`），
因為 **MCP 端的 `resolve_project` 會用「啟動目錄有沒有正本檔」當判準**：
安裝目錄裡有了正本 → MCP 伺服器就把**引擎的安裝目錄**認成一個專案。

**這是同一個問題第二次復發**：D 系列的舊條目已經清掉過一次誤建的
`fbf_engine.db`，但當年只擋了 **CLI 層**（`_project_root`／`init`／`join`），
漏掉下面兩層 → 只要有人再在引擎目錄裡跑一次會開正本的指令，就會復發。

**怎麼改**（三道閘門，判定來源只有一個）：

1. **`fbf_engine/paths.py`：新增 `is_under_mcp_root(path)`**
   —— 判斷「這個路徑**在安裝目錄之內**」：**含根目錄本身與所有子目錄**
   （使用者裁決：「未來也不存放加入的專案，所有資料都跑在專案自己的資料夾」）。
   兩邊都取 `realpath` 再比（macOS 的 `/tmp` 其實是 `/private/tmp` 的 symlink，
   不解析就比不相等）；比較用 `root + os.sep` 起頭，
   才不會把 `/引擎bc` 誤判成 `/引擎/bc` 的子目錄。
2. **`fbf_engine/store.py`：`connect()` 拒絕安裝目錄**（新例外 `McpRootProject`）
   —— **最後一道閘門**：正本絕不建立在安裝目錄（含其子目錄）。
   連檔案都不會被建立。
3. **`fbf_engine/mcp_server.py`：`resolve_project()` 兩條路徑都排除安裝目錄**
   （`FBF_PROJECT_ROOT` 與啟動目錄），`coordinator_for()` 也不替它開協調器。
4. **`fbf.py`**：三處 `== MCP_ROOT` 字串比對改用 `paths.is_under_mcp_root()`（單一來源）。

**資料落點盤點**（確認「所有資料都跑在專案自己的資料夾」）：
正本 `fbf_engine.db`、`opencode.json`、`.mcp.json`、憑證 `.fbf/credentials.json`、
派工日誌 `inbox/.opencode/` —— **全部寫在專案根**；
安裝目錄只放引擎的程式與規格，不放任何專案資料。

**驗證**：
- `tests/test_mcp_root_guard.py`：**11 項新測試**（守門第 1～3 道閘門；
  前幾項把 `paths.mcp_root()` 指到暫存目錄去測，**不污染真實安裝目錄**）。
  含「`join` 進安裝目錄底下要回 `project.root-is-mcp`」與
  「安裝目錄整棵樹都不該有正本檔」兩項。
- **突變測試**：把 `is_under_mcp_root()` 短路成恆 False → **7 項變紅**
  （證明這些測試真的有守門作用，不是永遠綠）。
- 實測：故意在安裝目錄放一份正本 → MCP 伺服器**仍然不認**它
  （`resolve_project` 回 `G-PROJECT-UNRESOLVED`）。
- 全套測試 **325 項全綠**（原 294 項 ＋ 復盤修正 20 項 ＋ 本條 11 項）。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261124j` → `20261125a`。

---

## D44. 多了一個 MCP 工具：`fbflow_project_join`（新專案自己加入引擎）

**規格怎麼說**：第 11 節的工具表列了 8 個工具（實作到 10 個），
「專案加入 MCP」是**人類 CLI 的事**（`fbf.py join`）：
`FBF_PROJECT_ROOT` 由**該專案自己的 `opencode.json`** 設定，
session 選不了專案（規格第 11 節「專案是 host 綁定的，session 選不了」）。

**為什麼必須改**：規格沒有「**還沒加入的專案怎麼開始**」這條路。
新專案的 session 開起來後，第一個工具呼叫就回 `G-PROJECT-UNRESOLVED`
（「這個 MCP 連線沒有綁任何專案」），而錯訊息只說不行、不說怎麼做 →
**要嘛人去終端機跑 `join`，要嘛自己改設定檔寫 `FBF_PROJECT_ROOT`**。
「加入」變成一件要人來救的事。

**怎麼改**：

1. **新工具 `fbflow_project_join`**：冪等；**只能加入目前工作目錄或它的子目錄**
   （session 宣告的是「我現在就在這個專案裡工作」）；引擎的安裝目錄仍拒絕
   （`project.root-is-mcp`，D43 的三道閘門不變）。
   做四件事：建正本、建 P1／T1／W1 席位、寫專案層 `opencode.json` 與
   `.mcp.json`（帶 `FBF_PROJECT_ROOT`）、把**這個 session** 綁到該專案。
2. **CLI 與 MCP 同一份實作**：新模組 `fbf_engine/project.py` 的 `join_project()`
   是唯一實作，`fbf.py join` 與 `fbflow_project_join` 都呼叫它。
3. **session→專案的綁定**記在全域狀態檔（家目錄的 `.fbflow`），
   **不放引擎安裝目錄**（那裡整棵樹都不放專案資料，D43）。
   `resolve_project()` 優先序變成：**環境變數 → session 綁定 → 啟動目錄**。
   綁定按 session 分開，且**重啟後仍在**（設計要求重啟不丟狀態）。
4. **錯訊息指路**：還沒綁專案時，錯誤訊息直接說「呼叫 `fbflow_project_join`」；
   `initialize` 的說明把加入放在第一步。
5. **引擎自動帶路**：派工提示詞（session／grill／decompose）**自帶規則正本的路徑**
   （`SKILL.md` 與 `spec/FBF ENGINE工作流.md`）——
   session 不必再去翻引擎原始碼才知道流程怎麼走。

**為什麼不牴觸「session 選不了專案」**：那條規則擋的是「**在參數裡報一個
別的專案**」（session 拿別人的正本來寫）。`fbflow_project_join` 寫的是
**自己的目錄**，而且不改任何既有工單、不越權到別的專案。
`fbflow_read`／`fbflow_report_work` 依然不接受專案參數。

**驗證**：
- `tests/test_project_join.py`：**14 項新測試**（建正本／席位／設定檔、
  冪等、兩個護欄、綁定按 session 分開、重啟後仍在、未綁時訊息指路、
  CLI 與 MCP 共用一份邏輯）。測試把全域狀態檔指到暫存目錄，不碰真的。
- 兩處工具數守門測試從 10 改 11。
- 實測（走真的 stdio）：`initialize` → `fbflow_project_join` → `fbflow_status`
  全綠；專案目錄生出 `fbf_engine.db`／`opencode.json`／`.mcp.json`，
  綁定檔記住 session。
- 全套測試 **339 項全綠**（原 337 項 ＋ 本條 14 項 － 2 項工具數改寫）。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261125a` → `20261125b`。

---

## D45. 多了一個 MCP 工具：`fbflow_work_supersede`（需求變了 → 開新工單）

**規格怎麼說**：規格第 1～10 步的流程裡，**沒有「需求在工單發出之後又變了」
這條路**。既有紅線只說「需求的 `success_criterion` 不可由 PLANNER 改」，
但**需求改了怎麼往下走**沒有定義；工單一旦發出，規格也只有「退回 → 重工」
這條支線（要改的是做法，不是已發出的工單本身）。

**為什麼必須改**：使用者裁決（2026-11-25）——
「工單發出後 USER 有需求修改，**不要改已發出工單**（太麻煩），
而是**按照新需求增加新工單**」。
規格沒有這條路，PLANNER 要嘛硬改已發出的工單（版本、退件、重工、交接全重來），
要嘛把需求悄悄改掉（牴觸紅線）。

**怎麼改**：

1. **新工具 `fbflow_work_supersede(work_id, replacement_work_ids, reason)`**
   （只有 PLANNER 能叫，要帶 `session_uuid`）：
   標記舊工單被哪些新單取代、取消它開著的 task、之後不再派工、交件被擋
   （回 `WORK-SUPERSEDED`）。**舊工單的內容、狀態、版本號都不動。**
2. **需求層的取代關係**：`requirements.superseded_by` ＋
   `fbf.py requirement confirm --supersedes R-001`（人宣告，可重複）。
   已確認需求**不可修改**——新舊都留著。
3. **結案追溯認取代**：被取代的需求不再要求有結案工單；
   但**它底下的工單必須也標成已取代**，否則舊單還會被派工做白工
   （引擎擋下來並指名是哪幾張單：`works_of_superseded_requirement`）。
4. **派工、巡檢、驗收關卡、沒人接手**四處都排除已被取代的工單。
5. **看得見**：`fbflow_status` 多帶 `superseded_works`（被誰取代、為什麼）；
   它們不再出現在 `open_works`。

**為什麼不改狀態機**：本來想加第 11 個狀態 `superseded`，
但那會動**十狀態契約**（spec 明列的狀態與轉移），
而使用者的裁決是「**不要改已發出工單**」——
動狀態就是改工單。改成「旗標 + 停工 + 不再派工 + 追溯認帳」，
**舊工單的原狀完整保留**，要查歷史的人看得到它當初是怎樣的。

**驗證**：
- `tests/test_supersede.py`：**14 項新測試**（內容與狀態不動、task 被取消、
  不再派工、交件被擋、四道護欄、需求取代關係、追溯行為、狀態看得見）。
- 兩處工具數守門測試 11 → 12。
- 全套測試 **353 項全綠**（原 339 項 ＋ 本條 14 項）。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261125b` → `20261125c`。

---

## D46. 引擎主動叫閒置的 session 收合上下文（`/compact`）

**規格怎麼說**：規格第 1～10 步只描述「單據狀態決定現在該誰做」與喚醒
（派了沒人回就再推一把）。**沒有任何一條**講「session 閒著時把對話收合」——
上下文是 agent 自己的事，規格不管。

**為什麼必須改**：使用者裁決（2026-11-25）——
「session 沒有工作可以做、呆滯的時候，離上一次 compact 超過半小時，
就讓引擎發出提示詞，叫 session compact」。
沒有這條，閒置的 session 對話會一直脹，等真的接上新工作時上下文已經塞滿。

**怎麼改**：

1. **新模組 `fbf_engine/compact.py`**：
   - `inspect_session()`——查一個 session「有沒有對話紀錄」與「上一次 compact 的時間」。
     - **Claude Code**：transcript（`~/.claude/projects/*/<session>.jsonl`）裡
       `isCompactSummary: true` 的最後一筆。
     - **OpenCode**：session 訊息裡 `type == "compaction"` 的最後一筆。
     - 其他宿主 → 查不到就回 `None`（**不猜**）。
   - `record()`／`last_recorded()`——正本裡的收合紀錄（`session_compacts` 表）。
   - `prompt_text()`——提示詞，**第一行就是收合指令**（預設 `/compact`）。
2. **觸發點**：常駐協調器的 `tick()`（派工**之後**，送出 outbox **之前**）。
   四道條件缺一不可（見 `docs/decisions.md` 附則 19）。
   其中「沒有工作可以做」是**看單據狀態**判定（使用者裁決），不是看任務數。
3. **閒置落定期 10 分鐘**——避免「MCP 一派單，session 就去 compact」。
4. **獨立錨點**（`CP…`）——帶真 task_id 會被回執核對當成已交件 → 假回執。
   與喚醒同一套做法。
5. **看得到**：`fbflow_status` 與 `fbf.py engine status` 都帶 compact 紀錄。

**為什麼不改十狀態契約**：這是**引擎主動提醒**，不是流程狀態——
不新增工單狀態、不動 WORK_STATES／WORK_TRANSITIONS。

**驗證**：
- `tests/test_compact_nudge.py`：**16 項新測試**（閒置才叫、有 task 不叫、
  等這個角色的單算有事做、落定期、距上次 compact 的兩側、
  叫過不重複、有對話紀錄才叫、錨點不是真 task_id、tick 整合、模組本身）。
- **真實 transcript 實測**：拿本機一個真的 Claude Code session 跑
  `compact.inspect_session()` → 正確讀出最後一次 compact 的時間
  （`2026-11-23T14:43:22Z`）與 `has_history=True`。
- 全套測試 **369 項全綠**（原 353 項 ＋ 本條 16 項）。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261125c` → `20261125d`。

---

## D47. 收合方式可選：壓縮（預設）或清空對話；**不做開新 session**

**規格怎麼說**：規格只說「閒置時叫 session 收合上下文」，**沒有說**用哪一種收合——
壓縮成摘要，還是把對話整個清掉。規格第 1～10 步完全不碰上下文。

**為什麼必須改**：使用者追問「Claude Code 的 `/clear` 是什麼作用」，
接著裁決：「**可以 RESET 就 RESET，不行就 COMPACT，不要 NEW**」，
並說明理由：「不要 new，我怕 new 把我給個 session 設定好的模型跑掉了」。
所以收合方式必須**可選**，而且**必須沒有 new 這一條路**。

**怎麼改**：

1. `compact.MODES = ("reset", "compact")`、`DEFAULT_MODE = "reset"`——
   專案層設定 `compact_mode`（寫在正本的 `settings` 表）。
   **優先序是「能 reset 就 reset，不行就 compact」**，所以預設是 reset。
2. `compact.AGENT_COMMANDS`——各宿主的指令對照（照官方文件核對）：
   - Claude Code：`/compact`、`/clear`。
   - OpenCode：官方常用指令裡**沒有** `/clear` → reset 在 OpenCode **退化成 `/compact`**，
     並在提示詞裡寫出這個退化。
3. `compact.command_for()`／`compact.prompt_text()`——依模式決定指令與提示詞。
   **退化時（OpenCode）提示詞要用「壓縮」的說法**，不能還叫它「清空對話」——
   說法要跟它實際該做的動作一致。
4. CLI：`fbf.py compact mode --mode reset|compact`、`fbf.py compact status`。

**為什麼不做 new**：`/new` 會用預設值重新開始——
使用者配給那個 session 的**模型、權限、工具設定全部不見**。
`/clear` 清空對話但**保留 session 本身**，設定不會掉。要換模型請另外用 session 設定。

**「`/clear` 會不會讓 session id 變？」——這套架構下不會**（使用者一度以此否決 reset，
查證後修正）：席位綁在**宿主終端 handle**（`term_…`），不是「對話 id」；
引擎認證用的專屬 UUID 也是依這個 handle 存 → `/clear` 不會動到它們，
席位不會變成孤兒。會換掉的只有 Claude 的**對話 UUID**（引擎只拿它讀 transcript，
**沒有拿它綁席位**）。
因此 **reset 時引擎不釋放席位**（釋放＝換一個 session 接手＝使用者不要的那條路），
提示詞也**不能**寫「UUID 跟著對話一起沒了」——那是不實的，會害 agent 白跑一趟。

**不做的**：不新增 MCP 工具（這是專案層設定，不是流程動作）；
不在沒有對應指令時假裝有；不提供任何「換 session」的模式。

**驗證**：`tests/test_compact_nudge.py` 涵蓋模式切換、指令對照、
OpenCode 退化路徑與退化後的措辭、reset 提示詞不宣稱 UUID 消失、**reset 不釋放席位**、
CLI 那條路（`compact status`／`compact mode`）。全套測試 **378 項全綠**。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261125d` → `20261125f`。

---

## D48. 「逾時」要分型：只有真的沒動靜才催；送進 session 的只有提示詞

**規格怎麼說**：規格第 6～7 步只說「角色不回應 → 引擎收掉重派」與
「派工提示詞要帶這一棒要交的欄位」。**沒有說**「怎麼算角色沒回應」，
也**沒有說**提示詞該帶多少。

**為什麼必須改**（使用者裁決 2026-11-25，實測一個批次處理專案）：
那個專案的 WORKER 跑 長時間批次工作，被引擎判了 **15 次逾時**並重派。
查證後真相是三件事疊在一起：

1. **模型端慢**：每一個被判逾時的當下，transcript 最後一筆都是
   `api-request`（**模型請求已送出、還沒回來**），不是工具執行。
   `api-request → assistant` 的間隔常 50～134 秒，最長 304 秒；
   全程 64 段 ≥45 秒的空檔合計 178 分鐘。
2. **模型端直接回錯**：`API Error: 422 NVIDIA: input rejected by content
   safety screening` ×4（15:47～15:55），正好夾在兩次逾時之間。
3. **context 被壓縮** 5 次（compact 後要重建上下文）——那幾十秒看起來像沒動靜。

使用者裁決：**「逾時也有可能是模型的網路問題」**，
以及 **「要 MCP 去催他，應該是要 SESSION 已經停滯的狀態——
沒有在對話、沒有在呼叫工具、什麼都沒有」**、
**「不要把任何文件送進 SESSION，只有提示詞可以送進 SESSION，
所有的工作狀態都留在磁碟上」**。

**怎麼改**：

1. **新模組 `fbf_engine/hostact.py`**——「宿主活動判型」：
   - 讀 transcript **尾端**（不整份讀：實測有 73 MB 的檔案），
     判成四種型態：`waiting_model`（在等模型）／`running_tool`（在跑工具）／
     `compacting`（剛被壓縮）／`idle`（回合已結束、什麼都沒在動）。
   - `classify()` 是**純函式**（好測試）；`is_alive()` 決定「還活著嗎」。
   - transcript 路徑定位（handle → UUID → 檔名）搬進來，
     `compact.py` 改用它（避免兩份實作）。
2. **喚醒（催）與逾時收掉都先判型**（`engine._wake_up_idle_roles`／
   `_expire_stalled_tasks`）：
   - **活著 → 不催、不收**（插訊息會打斷它正在做的事）。
   - **idle → 照舊**（先喚醒、逾時才收掉重派）。
   - **查不到宿主紀錄 → 門檻加倍**（保守：寧可晚點催，也不要誤殺）。
   - **計時基準**取「正本交件時間」與「宿主最後活動時間」的**較晚者**
     ——agent 一直有動作卻還沒交件時，正本時間很舊但它**沒有卡住**。
   - 模型端卡超過 3 小時 → 記 `health.model_stalled` 健康事件，
     **但不收掉 task**（不是 agent 的錯）。
3. **提示詞瘦身**：派工／喚醒／角色私有提示詞的交件契約改用
   `prompts.contract_lines(contract, brief=True)`——
   **保留欄位名、型別與短規則**（實測 agent 就是因為沒被告知
   「solutions 至少 2 案」才只交 1 案被退），
   **範例與長說明移到磁碟**（`fbflow_read` 的 `contract` → `field_specs`）。
   派工 payload 實測 1,843 → 1,332 字（-28%）；方法精神壓成一行＋指路 SKILL。
4. **同一個 session 這一趟只送一則**（`_flush_outbox`）：
   實測引擎在 90 秒內把 4 張重派工單一起插進正在跑批次的對話。
   其餘的延到下一趟（outbox 不丟）。

**為什麼不做「送出時合併成一則」**：那要改回執核對的粒度
（每筆 outbox 各自核對送達）。改用「這一趟只送一則、其餘延後」——
效果一樣（不會一次塞多張），但每一筆的送達與回執仍然各自獨立。

**驗證**：
- `tests/test_host_activity.py`（14 項）：型態判斷、活著不催不收、
  idle 照舊、unknown 門檻加倍、模型端卡住只記事件不收 task。
- `tests/test_prompt_thin.py`（5 項）：提示詞不含範例長文、保留關鍵規則、
  指路 contract、體積上限、同一 session 這一趟只一則。
- 全套測試 **397 項全綠**。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261125f` → `20261125g`。

---

## D49. 派工發「本輪 message_id」、design 棒事前講清規則、復盤依據照抄退件紀錄

**規格怎麼說**：規格第 5、7 節說「硬拒絕要回穩定缺口代碼、缺哪個欄位與可行下一步」，
第 7 節說「所有通知包含 task_id、工單 ID、讀取工具與本棒交件清單」。
**沒有說** message_id 該由誰產生，也**沒有說**提示詞要帶多少規則。

**為什麼必須改**（實測 2026-11-25一個批次處理專案，3 次退件）：

1. **`receipt.message_reused`**：agent 重複用了上一輪的 message_id 被退件。
   規則明明寫在提示詞裡（「每次交件都要新的」）→ **是它沒有「新的 id」的來源**，
   只能自己編，編錯就重複。
2. **`solutions.count`**：THINKER 在 design 棒只交 1 案被退件。
   退件訊息有說「至少要 2 案」，但**事前沒講** → 浪費一輪。
3. **復盤建議失準**：結案時 PLANNER 提的 3 條引擎改善建議裡，
   有 2 條方向不對、1 條把 `design` 階段記成 `acceptance_design`。
   根因：復盤的依據（退件紀錄）只印了一行摘要，**沒有編號、沒有欄位拆解**，
   PLANNER 憑印象寫就會記錯。

**怎麼改**：

1. **派工與喚醒由引擎發「本輪 message_id」**（`platform.dispatch_message_id()`）：
   值由 `task_id ＋ expected_version ＋ task 建立時間` 決定 →
   同一棒重送是同一個（同一輪），**退件重交（版本 +1）就是新的**。
   提示詞寫成 `message_id: msg-…`，並說明「**照抄上面那個**，不要自己編、
   不可沿用上一輪的」。
   - **它是雜湊，不含真的 task_id** → 不會造成假回執（喚醒訊息也帶它）。
   - **不影響回執核對**：`receipts.verify_opencode` 的註解明講
     「關聯鍵是 task_id，不是 agent 自報的 message_id」——
     agent 本來就看不到平台給訊息編的 id，它自報的必然是編的。
   - **去重守衛照留**：同一個 id 用在**別的**提交上 → 照樣被
     `receipt.message_reused` 擋下（驗證在 `_verify_receipt()` 層）。
2. **`WORK_STATES[DESIGN]` 補 `required_note`**：「**solutions 至少兩案**
   （兩案要有實質差異）」＋原來的三句話原則（合併成一條；
   原本那條會被 dict 重複鍵蓋掉，實作時要小心）。
   重工設計那一關本來就有這句 → 現在第一輪也有。
3. **結案復盤的退件紀錄改成逐欄結構化並編號**：
   `退件 #N｜階段：**design（解法設計中）**｜工單：W-007｜缺口代碼：solutions.count
   ｜缺哪個欄位：solutions｜交件：S-000011｜退件者：THINKER/T1` ＋原因一行，
   並明講「引用退件時請寫『退件 #N』，**並照抄它的階段**」。

**驗證**：
- `tests/test_prompt_thin.py`（13 項）：派工帶 nonce、nonce 每輪不同、
  nonce 不洩漏 task id、照抄 nonce 交件被接受、同一 nonce 用在別的提交仍被擋、
  design 提示詞含「至少兩案」、提示詞體積。
- `tests/test_project_review.py`（25 項）：退件紀錄含編號、階段全名、
  缺口代碼、交件編號與「照抄階段」的指示。
- 全套測試 **406 項全綠**。

**影響**：引擎行為改變 → `ENGINE_VERSION` `20261125g` → `20261125h`。

---

## D50. 清掉套件裡沒人用的東西；本機索引不隨包散布

**背景**：使用者要求清掉專案根目錄中不必要的東西（`spec/` 不動）。
問題是「能不能刪」不能靠感覺決定——**刪錯就是毀掉別人接手的依據**。

**判定方法**：先用 CodeGraph 索引查引用關係，再用文字搜尋補查
（索引看不到純文字檔的引用，例如 JSON 與 shell 腳本）。

**結果**（依據：索引查詢 ＋ 全樹 grep ＋ 測試結構要求）：

| 對象 | 判定 | 依據 |
|---|---|---|
| `__pycache__/`、`.pytest_cache/`、`.socktest/` | **刪** | 執行期產物；`.gitignore` 已排除 |
| `interfaces.json` | **刪** | 索引與 grep 都查無引用；內容已被取代（現行來源是 `fbf.py contract` ＋ `README.md` §3），且它停在 `rules_version: r1` |
| `install_agent_stack.sh` | **刪** | 索引查無引用；它裝的 pstack／TokenEconomizer 早已就位，剩下的是本機環境設定而不是套件的一部分 |
| `doc/` | **保留** | `tests/test_package_hygiene.py` 明定 `doc/` 只能有那兩個檔——是套件的強制結構 |
| `research/` | **保留** | 同上：測試要求 `research/` 必須存在且有 README |
| `spec/` | **保留** | 使用者明令不動（也是規格正本） |

**新增**：
- `.gitignore` 加 `.codegraph/` 與 `.pytest_cache/`。
- `tests/test_package_hygiene.py` 的 `SKIP_DIRS` 加 `.codegraph`。
- 兩項新守門測試：`.codegraph/` 必須被 `.gitignore` 排除；
  跳過清單**真的**有擋（放探針文字檔進 `__pycache__/`，確認掃不到——
  只列清單不驗行為，等於沒守）。

**影響**：套件檔案減少 7 項，測試 **406 → 408 項全綠**。
`ENGINE_VERSION` `20261125h` → `20261125i`。
