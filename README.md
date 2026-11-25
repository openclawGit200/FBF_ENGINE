# FBF ENGINE

> **多棒協作的軟體開發協調器**：一個需求進來，一連串工單流出去，
> PLANNER／THINKER／WORKER 三個 AI 代理接力做完，每一棒都有契約、有證據、有守衛。
>
> PLANNER 的需求訪談受 Matt Pocock 的 **GRILL ME** 啟發。三個角色的規劃、施工與驗收方式
> 則參考 Lauren Tan（poteto）的 **PSTACK**。詳見[致謝與各角色的做法](#11-致謝與授權)。**採用 [The Unlicense](LICENSE)**。

---

## 開始使用

建議使用 Python 3.10 以上。將引擎和目標專案放在不同目錄，並將 SQLite 正本放在本機磁碟。OpenCode 需要相容的 server API。Claude Code 需要可用的宿主終端通道。

```sh
python fbf.py join --project "PROJECT_DIR"
python fbf.py role list --project "PROJECT_DIR"
```

將 `PROJECT_DIR` 換成目標專案路徑。接著開啟三個工作階段，依[快速開始](#2-快速開始啟動三段)認領席位並確認需求。工單階段與欄位摘要可用 `python fbf.py contract` 查看，流程參見[工作流規格](spec/FBF%20ENGINE工作流.md)。

請在 GitHub Issues 回報問題與建議，並先移除專案資料、憑證與對話內容。

## 目錄

1. [它是什麼](#1-它是什麼)
2. [快速開始（啟動三段）](#2-快速開始啟動三段)
3. [完整技能清單](#3-完整技能清單)
4. [每個角色每一關做什麼](#4-每個角色每一關做什麼)
5. [防止工作流漂移的機制](#5-防止工作流漂移的機制)
6. [自動 COMPACT 機制](#6-自動-compact-機制)
7. [需求變了怎麼辦](#7-需求變了怎麼辦)
8. [宿主支援矩陣](#8-宿主支援矩陣)
9. [檔案地圖](#9-檔案地圖)
10. [測試](#10-測試)
11. [致謝與授權](#11-致謝與授權)

---

## 1. 它是什麼

FBF ENGINE 是一個**跑在你專案裡的協調器**。它不自己寫程式，它**管**那些會寫程式的
AI 代理怎麼排隊、怎麼交件、怎麼驗收、怎麼在有人跑錯時把它拉回來。

### 1.1 核心設計原則

| 原則 | 白話說明 |
|---|---|
| **十狀態工單** | 一張單從 `backlog` 走到 `closed`，中間每一關都有指定角色、必交欄位、合法轉移。 |
| **狀態只由協調器推進** | 代理**不能**自己宣告「我做好了」。它只能交件，狀態由引擎判。 |
| **SQLite 是唯一資料正本** | 不產生任何工單／ISSUE 檔案——多一份版本就多一份錯亂的機會。 |
| **交件契約由派工提示詞攜帶** | 來源與守衛是**同一份** `workflow.stage_contract`，不會兩邊說法不同。 |
| **回執必須可查** | 每一筆提交都要在宿主平台上查得到，才算可信；查不到≠完成。 |
| **人類命令不下放給 session** | 寫入操作必須來自可信的 host 綁定 session；session 不能自己升權。 |
| **送進 session 的只有提示詞** | 工作狀態（工單內容、驗收題目、規格）**留在磁碟上**，角色自己去讀。 |

### 1.2 三個角色一句話

- **PLANNER** — 決定「要做什麼、做多少、算不算做完」。
- **THINKER** — 想辦法（至少兩案），並且**獨立判斷別人做的對不對**。
- **WORKER** — 真的做出產物，附上指紋（路徑／大小／SHA-256）。

三者的邊界是硬性的：**WORKER 不定驗收標準，THINKER 不宣布專案完成，PLANNER 不動手施工。**

### 1.3 它長什麼樣

```
需求（人確認）
   │
   ├─ GRILL ME ──→ PLANNER 追問四項、自己的話複述、你點頭
   │
   ├─ DECOMPOSE ─→ PLANNER 三句話原則拆成一張張工單
   │
   └─ 流水線（每張單獨立走完）
        backlog → design → acceptance_design → build → verify → adjudicate → closed
                            ↑                                │
                            └──── rework_*（退回支線）←──────┘
```

---

## 2. 快速開始（啟動三段）

### 段 1：某個專案加入引擎

```bash
python fbf.py join --project <專案路徑>
```

會做三件事：建正本（`fbf_engine.db`）＋ 寫專案層 MCP 設定（`opencode.json`、`.mcp.json`）
＋ 建三個角色席位（P1／T1／W1）。

**也可以讓 session 自己加入**（在專案目錄下開一個 session，呼叫一次就行）：

```
fbflow_project_join
```

它會建正本、建席位、寫設定，並**把這個 session 綁到該專案**——
之後同一個 session 的其他工具立刻能用，不用重開連線。

### 段 2：GRILL ME（需求釐清）

在目標專案開啟三個已載入 MCP 設定的工作階段，各自呼叫 `fbflow_register`。角色與簡稱分別填 PLANNER／P1、THINKER／T1、WORKER／W1。

```bash
python fbf.py engine tick --once --project <專案路徑>
```

引擎會派 `grill` 棒給 PLANNER，它讀專案文件、追問四項（任務／範圍／目標／預期產出）、
**用自己的話複述**，然後問你「是否同意」。此時**禁止建工單**。

### 段 3：生成工單、開始流水線

```bash
python fbf.py requirement confirm --project <專案路徑> \
    --text "<需求>" --criterion "<可驗證的完成條件>" --confirmed-by <你的名>
```

你按下「同意」＝確認需求 → 引擎派 `decompose` 棒給 PLANNER → 三句話原則拆單 →
之後每趟 tick 自動推進流水線。

### 日常查看

```bash
python fbf.py engine status  --project <專案路徑>   # 協調器現況
python fbf.py dashboard     --project <專案路徑>   # 後台網頁（唯讀，預設 127.0.0.1:8788）
python fbf.py awaiting list  --project <專案路徑>   # 待人裁決項
python fbf.py role list     --project <專案路徑>   # 席位與綁定
```

**後台網頁是唯讀旁路**：只讀正本，不寫正本、不拿租約、不打擾任何 session。
**預設只綁 `127.0.0.1`**；要讓區網看得到必須明確指定 `--host 0.0.0.0`。

---

## 3. 完整技能清單

### 3.1 MCP 工具（12 個）

> **session 用 MCP 工具，不要用 CLI。** CLI 是人類控制面。

| 工具 | 誰能呼叫 | 必填 | 用途 |
|---|---|---|---|
| `fbflow_project_join` | 任何人 | — | 把目前專案加入引擎（建正本＋席位＋設定檔），並把自己綁到該專案 |
| `fbflow_register` | 任何人 | `role`, `short_name` | 認領一個角色席位，取得**專屬提示詞**（含專屬 UUID） |
| `fbflow_read` | 任何人 | `target_kind`, `target_id` | 讀正本快照：`work`／`issue`／`requirement`／`milestone` |
| `fbflow_status` | 任何人 | — | 整體現況：待辦工單、席位、待人裁決、健康巡檢、收合紀錄與收合方式、已取代工單 |
| `fbflow_work_create` | PLANNER | `title`, `requirement_id`, `content` | 建立一張工單（`backlog` 狀態） |
| `fbflow_work_supersede` | PLANNER | `work_id`, `replacement_work_ids`, `reason`, `session_uuid` | 把已發出的工單標成**已取代**（需求變了 → 開新單） |
| `fbflow_issue_create` | WORKER / THINKER | `work_id`, `trigger_kind`, `problem`, `location`, `tried`, `impact`, `question`, `completion_criteria` | 發起 ISSUE：目前工單無法往前，或需要 PLANNER 裁決 |
| `fbflow_milestone_create` | PLANNER | `seq`, `name`, `outcome` | 建立里程碑（分段目標，防長流程偏移） |
| `fbflow_report_work` | 任何人 | `task_id`, `work_id`, `expected_version`, `payload`, `message_id`, `session_uuid` | **交件**（不改狀態；狀態由引擎判） |
| `fbflow_report_issue` | 任何人 | `task_id`, `issue_id`, `expected_version`, `payload`, `message_id`, `session_uuid` | 交 ISSUE 的處理結果 |
| `fbflow_report_milestone` | PLANNER | `task_id`, `milestone_id`, `expected_version`, `action`, `session_uuid` | 里程碑檢視：`continue`／`add_work`／`await_human` |
| `fbflow_report_project_review` | PLANNER | `task_id`, `expected_version`, `action`, `session_uuid` | 專案結案檢視（判定＋引擎復盤） |

#### 交件工具的共同規則

- `expected_version` 用 `fbflow_read` 讀到的版本；**版本不對就是退件**（樂觀鎖）。
- `stage` 可以留空——引擎依 `task_id` 判定這一棒。
- `message_id` **照抄派工訊息裡那一個**（引擎為這一輪發的）。
  不要自己編、**不可沿用上一輪的**——沿用會被引擎以 `receipt.message_reused` 退件。
- `session_uuid` 是註冊時發給你的專屬 UUID（只有你知道）。
- **payload 要含派工提示詞「現在這一棒」列出的每一個欄位，外加 `handoff`（八欄）**。

### 3.2 CLI 指令（人類控制面）

```
fbf.py --project <專案路徑> <子命令>

init                  建立專案正本
join                  某個專案加入這個 MCP（段 1）
install                安裝這個 MCP（全域，不綁任何專案）

requirement confirm    確認需求（或建立並確認）——＝你按下「同意」
engine tick            跑一趟 tick
engine status          協調器現況
awaiting list          列出待人裁決項
awaiting resolve       裁決待人處理項
project close          專案結案（需通過雙向追溯）
role add               建立角色席位
role list              列出角色席位與綁定狀態
role release           釋放席位（原本的 session 中斷時，讓新的 session 接管）
session bind           綁定 session 到角色
session list           列出角色綁定
session revoke         撤銷綁定
compact mode           設定上下文收合方式（compact／reset）
compact status         收合方式、各宿主指令對照、收合紀錄
review improvements    引擎改善建議（結案復盤產出）
review improvement-status  標記某筆建議的處理狀態
review rejections      每一次退件的 LOG
dashboard              後台網頁（唯讀儀表板）
contract               印出交件契約
```

除了用來建立正本的 `init` 和 `join`，操作正本的 CLI 指令都要求目標目錄**已經是 FBF 專案**
（有 `fbf_engine.db`）；不是就報 `project.not-found` 並指名要 `--project <專案路徑>`。

**引擎的安裝目錄整棵樹（含所有子目錄）都不存放專案資料**——想加入的專案必須在自己的
資料夾裡，正本、`opencode.json`、`.mcp.json`、憑證與派工日誌全部寫在**那個專案自己的資料夾**。

### 3.3 規則正本與專案 SKILL

| 檔案 | 誰讀 | 內容 |
|---|---|---|
| `SKILL.md` | 每個 session（經 `fbflow_register` 的私有提示詞） | 角色規則、每一棒的方法精神、資料格式 |
| `spec/FBF ENGINE工作流.md` | 人類＋引擎 | 規格正本：十狀態、ISSUE 六狀態、里程碑、結案檢視 |
| 專案層 `SKILL.md` | 該專案的 WORKER | 這個專案**自己的**技能（例如 `pdf-product-crops`） |
| `docs/decisions.md` | 人類 | 決策紀錄（含每次裁決的理由與替代方案） |
| `DEVIATIONS.md` | 人類 | **偏離規格的地方與為什麼**（含實測證據） |
| `HANDOFF.md` | 人類 | 本機交接紀錄，由 Git 排除 |

**安裝成 Skill**（讓 Claude Code／OpenCode 直接認得這套方法）：

```bash
# Claude Code
ln -s <引擎路徑>/SKILL.md ~/.claude/skills/FBF_ENGINE_MCP/SKILL.md
# OpenCode
ln -s <引擎路徑>/SKILL.md ~/.config/opencode/skills/FBF_ENGINE_MCP/SKILL.md
```

---

## 4. 每個角色每一關做什麼

### 4.1 工單十狀態總表

```
backlog → design → acceptance_design → build → verify → adjudicate
                                                          ↓
                                                          ├─ closed
                                                          └─ rework_design → rework_acceptance_design
                                                                       → rework_build → verify（循環）
```

| 狀態 | 中文 | 由誰做 | 必交欄位 |
|---|---|---|---|
| `backlog` | 待派工 | 引擎 | — |
| `design` | 解法設計中 | THINKER | `solutions`, `design_note` |
| `acceptance_design` | 驗收方式設計中 | PLANNER | `selection`, `checks` |
| `build` | 施工中 | WORKER | `outputs`, `done`, `unfinished`, `selfcheck`, `cases_ran` |
| `verify` | 驗收中 | THINKER | `results`, `cases_ran` |
| `adjudicate` | 結案中 | PLANNER | `action`, `close_reason`, `rework_reason`, `rework_type`, `diagnosis`, `remedy` |
| `rework_design` | 重工解法設計中 | THINKER | `solutions`, `rework_plan` |
| `rework_acceptance_design` | 重工驗收方式設計中 | PLANNER | `selection`, `checks` |
| `rework_build` | 重工中 | WORKER | `outputs`, `done`, `unfinished`, `selfcheck`, `cases_ran` |
| `closed` | 已結案 | 引擎 | — |

**ISSUE 六狀態**：`raised`（發起中）→ `designing`（THINKER 出兩解法）→ `handling`
（PLANNER 挑一案寫對策）→ `building`（WORKER 依對策施工）→ `adjudicating`
（原發起者結案）→ `closed`。對策沒生效就在**同一張單上重跑整個流程**；
**連續兩輪未結案就停下來等人類裁決**。

---

### 4.2 PLANNER — 決定「做什麼、做多少、算不算做完」

| 關卡 | 做什麼 | 交什麼 | 會被什麼擋 |
|---|---|---|---|
| **GRILL** | 讀專案文件；一次只問一題、每題附上自己的猜測；**用自己的話**複述任務／範圍／目標／預期產出；問你同不同意 | 複述 + 問題（**不建工單**） | 未經人確認需求就建工單 → `requirement.unconfirmed` |
| **DECOMPOSE** | 三句話原則拆單；垂直切片；**一張工單＝一條端到端可驗收的路徑**；標題出現「而且」就拆 | 新工單（`fbflow_work_create`） | 需求未確認 → `requirement.unconfirmed` |
| **acceptance_design** | 為這張單設計**可執行的驗收方式**：每條 check 指向一條已確認需求，寫明 `method`／`scope`／`threshold`／`evidence`／`required`／`execution_kind`；挑選 THINKER 的哪一案（`selection`） | `selection`, `checks` | 沒選案 → `selection.missing`；案號不存在 → `selection.unknown_solution`；沒檢查 → `checks.missing`；check 指向未確認需求 → `check.requirement_unknown`；門檻被放寬 → `check.threshold_lowered`；required 被降級 → `check.required_downgraded` |
| **milestone_review** | 對照里程碑的 `outcome` 判斷要不要繼續 | `action`＝`continue`／`add_work`／`await_human` | 動作不合法 → `review.diagnosis_invalid`；動作與處置不匹配 → `review.remedy_invalid` |
| **adjudicate** | 看驗收結果裁決：`close`（附 `close_reason`）／`rework`（附 `rework_reason` + `rework_type`：施工錯／驗收錯／解法錯）／`review`（附 `diagnosis` + `remedy`） | 依 `action` 只交需要的那組欄位 | 動作不合法 → `action.invalid`；`rework_type` 不合法 → `rework_type.invalid`；重工次數到上限 → `rework.limit_reached` |
| **project_review** | **專案層級**判定「任務目標到底有沒有達成」＋順便做引擎復盤 | `action`＋`improvements` | 動作不合法 → `action.invalid` |
| **—（授權）** | `fbflow_work_create`／`fbflow_work_supersede`／`fbflow_milestone_create` | — | 席位角色不符 → `ROLE-DENIED` |

**PLANNER 的紅線**：需求未經人類明確確認前不得建立工單；所有 required 的 check 都 PASS
才可以 close；**總判由程式算，不採信自稱**；若原因是「驗收方式訂錯了」要回設計驗收方式
（revision），**不能把它偽裝成重做施工**。

---

### 4.3 THINKER — 想辦法，並獨立判斷別人做的對不對

| 關卡 | 做什麼 | 交什麼 | 會被什麼擋 |
|---|---|---|---|
| **design** | **一棒產出至少兩案解法**（不分派 SUBAGENT）：`approach`／`cost`／`change_scope`／`effort`／`risk`／`verifiable_by`（選填 `reuses`）；兩案要**實質不同**；寫 `design_note`（三句話） | `solutions`, `design_note` | 少於兩案 → `solutions.count`；兩案實質相同 → `solution.too_similar`；欄位缺 → `solution.field_missing`；缺設計說明 → `design_note.missing` |
| **verify** | **按 `checks` 逐條執行驗收**，每條給結果與理由；證據要能重跑 | `results`（每條 check 一筆結果）, `cases_ran` | 沒結果 → `result.missing`；狀態不合法 → `result.status_invalid`；結果沒附證據 → `result.evidence_missing`；`cases_ran=0` 卻宣稱已驗 → `cases_ran.zero`；**驗自己在本工單的施工** → `verify.self_conflict`；缺交接檔 → `handoff.missing` |
| **rework_design** | 針對退回原因重新設計解法，並**引用那個原因** | `solutions`, `rework_plan` | 同 design |
| **ISSUE 發起** | 同一個問題撞三次就停下來發 ISSUE，不要硬撞第四次 | ISSUE 單 | 欄位缺 → `issue.field_missing`；連續未結案 → `issue.return_limit` |

**THINKER 的紅線**：不決定整體範圍；不宣布專案完成；**不可驗自己在本工單的施工**。

---

### 4.4 WORKER — 真的做出產物

| 關卡 | 做什麼 | 交什麼 | 會被什麼擋 |
|---|---|---|---|
| **build** | 依需求／解法／驗收方式做；**交付前先自己照 PLANNER 設計的驗收方式自檢一遍** | `outputs`（產物指紋：路徑／大小／SHA-256）、`done`、`unfinished`、`selfcheck`、`cases_ran` | 產物核不到 → `evidence.missing`；大小或雜湊對不上 → `evidence.hash_mismatch`；沒交指紋 → `outputs.missing`；`unfinished` 不是清單 → `unfinished.invalid`；缺自檢 → `selfcheck.missing`；缺交接檔 → `handoff.missing` |
| **rework_build** | 依退回原因重做 | 同上 | 同上 |
| **ISSUE 對策施工** | 依 PLANNER 的對策執行 | `outputs`, `selfcheck` | 同上 |
| **ISSUE 結案** | 對策生效 → `resolution`；沒生效 → `return_reason`（**二選一，不可都給**） | 依動作只交一項 | 二選一都給 → `issue.field_missing` |

**WORKER 的紅線**：不決定驗收標準，也不宣布過不過；**案例數不得灌水**（純文件／人工檢視要
自報 `execution_kind=review` 並附審閱紀錄）；**不要自己改驗收方式**——那是 PLANNER 的權限。

---

### 4.5 交接檔（handoff）— 每一棒都要交

固定八欄，缺了整份退件：

`ROLE`｜`OPEN WORK`｜`SUBAGENTS RUNNING`｜`RULES AND NUMBERS IN FORCE`｜`RECIPES`｜
`DECISIONS AND WHY`｜`LESSONS`｜`SPEND`

交接檔是**跨棒的唯一線索**：下一棒靠它知道上一棒做了什麼、為什麼這麼做、還欠什麼。

### 4.6 里程碑與專案結案檢視

- **milestones**＝防止長施工流程在過程中偏移方向的**分段**機制。
- **結案**＝**專案層級**「任務目標到底有沒有達成」的判定，**不依賴 `milestones` 表有資料**。

所有單據都標示結案後，引擎自動通知 PLANNER 做一棒**專案結案檢視**：

| 動作 | 意思 | 結果 |
|---|---|---|
| `complete` | 產出對得上任務目標 → 達成 | 引擎自動跑**雙向追溯**，**通過才標專案結案**；不通過就不得結案 |
| `add_work` | 目標還沒達成 | 針對未完成的部分開新工單；新工單完成後再觸發一輪 |
| `await_human` | 停下來 | 等人裁決 |

同一個回報還要交**引擎改善建議**（結案復盤）。復盤依據是**退件紀錄**，
引擎會把每一筆退件編號、標出階段與缺口代碼給它——**照抄階段，不要憑印象改寫**。

```bash
python fbf.py review improvements --project <專案路徑>   # 引擎改善建議
python fbf.py review rejections  --project <專案路徑>    # 每一次退件的 LOG
```

---

## 5. 防止工作流漂移的機制

漂移＝工作不再照契約走。FBF ENGINE 用**五層**機制把它拉回來。

### 5.1 第一層：狀態機契約

- **十個狀態**＋**合法轉移表**：引擎只認表上的轉移；非法轉移直接拒（`WORK-REJECTED`）。
- **每個狀態指定角色**：該做的角色以外的 session 呼叫會被 `ROLE-DENIED` 擋。
- **每個狀態指定必交欄位**：缺一個就退件，不會「先收下再說」。
- **同一份定義兩處用**：`workflow.stage_contract` 同時供守衛檢查與提示詞渲染——
  **不可能「提示詞說要交 A、守衛卻要求 B」**。

### 5.2 第二層：守衛與缺口代碼（45 個）

每個硬拒絕都回傳**穩定缺口代碼 ＋ 缺哪個欄位 ＋ 可行下一步**——
只丟代碼，角色看不懂就改不對。分類如下：

| 類別 | 例子 | 擋什麼 |
|---|---|---|
| 需求與解法 | `requirement.unconfirmed`、`solutions.count`、`solution.too_similar`、`selection.unknown_solution` | 沒人確認就開工；只交一個案子；兩案其實同一案；選了不存在的案 |
| 驗收設計 | `checks.missing`、`check.requirement_unknown`、`check.threshold_lowered`、`check.required_downgraded`、`acceptance.not_designed` | 驗收題目不完整／指向沒確認的需求／**偷偷放寬門檻**／把必檢降級 |
| 交件內容 | `outputs.missing`、`evidence.missing`、`evidence.hash_mismatch`、`cases_ran.zero`、`result.missing`、`handoff.missing` | 沒交產物／產物對不上／**宣稱驗過但一件案例都沒跑** |
| 驗收誠信 | `verify.self_conflict` | **驗自己在本工單做的施工** |
| 結案與重工 | `action.invalid`、`rework_type.invalid`、`rework.limit_reached`、`revision.limit_reached` | 動作不合法；**無限重工**；用無限修訂拖時間 |
| 回執 | `receipt.unverified`、`receipt.message_reused`、`receipt.message_id_missing` | 回執查不到；**同一個回執被兩次提交共用**；沒帶 message id |
| 流程一致性 | `work.superseded`、`apply.rejected`、`issue.return_limit` | 已被取代的工單還在交件；套用被拒；ISSUE 連續未結案 |

### 5.3 第三層：證據鏈

- **產物指紋**：引擎**自己重算**大小與 SHA-256，不採信模型填的數字。
- **回執核對**：提交必須在宿主平台上查得到（`task_id` 是關聯鍵——
  agent 看不到平台訊息 id，自報的必然是編的）。**查不到 ≠ 完成**。
- **樂觀鎖**：`expected_version` 不符就是退件——兩個 session 不能同時改一張單。
- **租約**：同一時間只有一個協調器行程寫正本；舊版引擎會被強制接手。
- **journal**：每一次狀態推進、每一次退件、每一次派工都留紀錄，可完整回放。

### 5.4 第四層：節奏控制

| 機制 | 行為 |
|---|---|
| **喚醒** | 派了沒人回 → 引擎送一段提示詞叫它回來（不帶 task_id，避免假回執）。同一棒最多喚醒 3 次、間隔 10 分鐘。 |
| **逾時分型** | 只有「回合已結束、什麼都沒在動」才收掉重派；**在等模型／在跑工具／剛被壓縮 → 不催不收**（見 §6.3）。 |
| **模型端停滯** | 模型端卡超過 3 小時 → 記健康事件，**但不收掉 task**（不是 agent 的錯）。 |
| **退件上限** | 重工次數／修訂次數到上限 → 標待人處理，不准無限循環。 |
| **ISSUE 上限** | 同一個問題撞三次 → 發 ISSUE；ISSUE 連續兩輪未結案 → 停下來等人。 |
| **里程碑** | 長流程分段，每段對照 `outcome` 決定要不要繼續。 |
| **雙向追溯** | 結案前引擎自己跑追溯：需求→工單→驗收→產物→需求，**不通過就不准結案**。 |
| **退件紀錄** | 每一次退件都留 LOG（單據副本、原因、欄位、退件者）→ 復盤有依據，也看得到流程在退什麼。 |

### 5.5 第五層：一次性語意

有些東西**只能發生一次**，一旦允許重來，流程就會失真：

- **已確認的需求不可修改**——需求變了要**新增**一條需求（由人確認）。
- **已發出的工單不可修改**——內容、狀態、版本號都不動；要改就開新單 + 標取代（見 §7）。
- **回執不得被兩次提交共用**——同一個 `message_id` 用在別的提交上 → 永久性退件。
- **取代鏈只留一層**——被取代的單不能再被取代（避免幽靈鏈）。

---

## 6. 自動 COMPACT 機制

閒置的 session 對話會一直脹，等真的接上新工作時上下文已經塞滿。
引擎會**主動叫它收合**。

### 6.1 什麼叫「沒有工作可以做」

**看單據狀態**，不是看任務數：

1. 這個 session 有開著的 task → 有工作。
2. 有單據正處於「等這個角色做」、而且**還沒有人做** → 它等一下就有事做。
3. 都沒有 → 它現在真的沒事做。

### 6.2 四道條件（缺一不可）

1. **沒有工作可以做**（見上）。
2. **閒置落定期 10 分鐘**——剛交完件、剛被派過單的 session 不算閒。
   （否則會出現「MCP 一派單，session 就去 compact」這種壞事。）
3. **距上一次 compact 超過 30 分鐘**——以**宿主紀錄**為準：
   Claude Code 的 transcript（`isCompactSummary`）、OpenCode 的訊息（`type=compaction`）；
   偵測不到就用引擎自己記的「上次要求」時間當保守值。**偵測不到就回 `None`，不猜**。
4. **有對話紀錄**——剛開的空 session 沒有東西可收合。

### 6.3 收合方式：能 reset 就 reset，不行就 compact

| 模式 | 指令 | 效果 | 代價 |
|---|---|---|---|
| `reset`（預設） | `/clear`（Claude Code） | **清空對話、session 本身不變** | 對話脈絡沒了（這就是重點） |
| `compact` | `/compact` | 壓縮成摘要，**保留脈絡** | 摘要仍留有舊痕跡 |

- **優先序**：能 reset 就 reset；某個 agent 沒有清空指令（OpenCode 官方常用指令裡**沒有**
  `/clear`）→ **自動退回 `/compact`**，提示詞會寫出這個退化，**不假裝有 `/clear`**。
- **沒有「開新 session（`/new`）」這個選項**：開新 session 會用預設值重新開始——
  那個 session 專屬的**模型、權限、工具設定全部不見**。
  `/clear` 清空對話但**保留 session 本身**，設定不會掉。
- **`/clear` 不會讓引擎認得的 session id 變**：席位綁在**宿主終端 handle**，
  專屬 UUID 也依它存 → 席位不會變孤兒，也不用重新認領。
  引擎也**不會**在 reset 時釋放席位（釋放＝換 session，那是不要的那條路）。

```bash
python fbf.py compact mode --mode compact --project <專案路徑>   # 改成壓縮成摘要
python fbf.py compact status --project <專案路徑>                 # 方式、指令對照、收合紀錄
```

### 6.4 送達與節流

提醒走既有的 outbox 通道，用**獨立錨點**（`CP…`）——
**不帶真 task_id**（帶了會被回執核對當成「這一棒已交件」→ 假回執）。
提示詞**第一行就是收合指令**（宿主終端那條通道是 bracketed paste，
有些宿主把開頭斜線當指令執行 → 直接收合；當成訊息送，agent 也讀得懂）。
收合紀錄在 `fbflow_status` 與 `fbf.py engine status` 都看得到
（`detected`＝宿主紀錄、`requested`＝引擎要求過）。

### 6.5 逾時分型（同一個道理）

「逾時」≠「失聯」。引擎先看**宿主側狀態**（讀 transcript 尾端）：

| 狀態 | 意義 | 引擎怎麼做 |
|---|---|---|
| `waiting_model` | 請求已送出，模型還沒回 | **不催、不收** |
| `running_tool` | 工具執行中／等工具回來 | **不催、不收** |
| `compacting` | context 被壓縮，正在重建 | **不催、不收** |
| `idle` | 回合已結束，什麼都沒在動 | **這才催**；逾時才收掉重派 |
| `unknown` | 查不到宿主紀錄 | 門檻加倍（保守） |

計時基準取「正本交件時間」與「宿主最後活動時間」的**較晚者**。
（實測：曾有 session 在跑 長時間批次工作時被誤判逾時 15 次——
它每一分鐘都還在跑，最後一筆全是模型請求，其中幾次是模型端直接回錯。）

---

## 7. 需求變了怎麼辦

需求在工單發出之後又變了，**不要改那張工單**（版本、退件、重工、交接都要重來）。
正確做法：

```bash
# 1) 需求本身變了 → 由人新增一條需求（已確認的需求不可修改）
python fbf.py requirement confirm --project <專案路徑> \
    --text "<新需求>" --criterion "<新判準>" --supersedes R-001

# 2) PLANNER 照新需求開新工單（fbflow_work_create），新工單掛新需求
# 3) PLANNER 把舊工單標成已取代（fbflow_work_supersede）
```

`fbflow_work_supersede` **不改舊工單的內容、狀態與版本號**，只做三件事：
標記（被哪些新單取代、為什麼）、停工（取消開著的 task）、退場（不再派工、交件被擋）。

被取代的工單不再出現在 `fbflow_status` 的 `open_works`，
但看得到（在 `superseded_works` 裡，附被誰取代與原因）——
**看得到，才不會以為它還在跑**。

---

## 8. 宿主支援矩陣

| 能力 | **Claude Code** | OpenCode | 其他宿主 |
|---|---|---|---|
| 派工送達 | 透過相容宿主 CLI 的終端通道 | ✅ 原生訊息 | ⚠️ 未實測 |
| 回執核對 | ✅ transcript 讀取 | ✅ API | ⚠️ 未實測 |
| compact 偵測 | ✅ `isCompactSummary` | ✅ `type=compaction` | ❌ 格式未支援（→ 保守加倍門檻） |
| `/compact` | ✅ | ✅ | — |
| `/clear`（reset） | ✅ | ❌ **沒有這個指令** → 自動退回 `/compact` | — |
| 逾時判型 | ✅ transcript 尾端 | ✅ 訊息時間 | ⚠️ 回 unknown |
| 席位綁定 | ✅ 終端 handle | ✅ session id | ✅ 通用 |

Claude Code 派工需要支援 `terminal` 子命令的宿主 CLI，可用 `FBF_HOST_CLI` 指定。 OpenCode 的核心流程（派工、交件、回執、compact）可用，
但**沒有 `/clear`** → reset 模式在 OpenCode 上**明確退化為 `/compact`**，
並在提示詞裡寫出這個退化。其他宿主的收合偵測與逾時判型會回 `unknown`，
引擎採**保守策略**（門檻加倍，寧可晚點催也不誤殺）。

---

## 9. 檔案地圖

```
fbf.py                      人類控制面 CLI
fbf_engine/
  mcp_server.py             MCP 介面層（12 個工具）
  compact.py                上下文收合：偵測「上一次 compact」＋提醒提示詞＋收合方式（compact／reset）
  hostact.py                宿主活動判型：在等模型／在跑工具／剛壓縮／真的沒動靜
  project.py                專案加入：建正本／席位／設定檔 ＋ session→專案綁定
  coordinator.py            常駐協調器（timer ＋ 租約）
  engine.py                 協調器：tick、套用提交、派工、計數、租約、里程碑、ISSUE、結案檢視
  workflow.py               規則正本：十狀態契約、交件契約、守衛、45 個缺口代碼與說明
  store.py                  SQLite schema、交易、租約、資料存取
  platform.py               派工（host CLI / OpenCode）與 presence、派工提示詞、message_id nonce
  hostterm.py               宿主終端層（host CLI terminal send、transcript 定位）
  ocserver.py               既有 OpenCode server 的唯讀探索
  receipts.py               平台回執核對（唯讀）
  prompts.py                逐 session 渲染的提示詞＋每一棒的方法精神
  dashboard.py              後台網頁（唯讀儀表板）：誰在寫、欄位改成什麼
  paths.py                  跨主機路徑／Python 命令解析
spec/                       規格正本（工作流＋工單／ISSUE 模板）
doc/                        舊工程參考稿（已降級）
docs/decisions.md           決策紀錄（附則：每個裁決的理由與替代方案）
DEVIATIONS.md               偏離規格的地方與為什麼（含實測證據）
HANDOFF.md                  本機交接紀錄，由 Git 排除
research/                   實測證據與觀察
tests/                      自動化測試
```

> `.codegraph/` 是本機程式索引（每台機器各建各的），**不隨包散布**。
> 開發時如需程式索引，可安裝 CodeGraph 後執行 `codegraph init`。

---

## 10. 測試

```bash
# 全部測試
python -m unittest discover -s tests

# Hygiene 守門（禁寫宿主名稱、絕對路徑、舊正本檔名）
python -m unittest tests.test_package_hygiene -v
```

> 建議使用 Python 3.10 以上。引擎與測試使用標準函式庫。Windows PowerShell 的預設編碼若不是 UTF-8，請先執行 `$env:PYTHONUTF8 = '1'`，讓測試啟動的子程序也使用 UTF-8。

---

## 11. 致謝與授權

### 授權

**本專案採用 [The Unlicense](LICENSE)。**

程式碼、規格、文件、測試與設計紀錄，一律不受限制地使用、複製、修改、再散布、
商用、衍生新作品，**無需署名、不設任何條件、不要求任何授權聲明**。
不提供任何形式的擔保；使用者須自行評估適用性。

### 特別致謝

感謝 **Matt Pocock** 公開 GRILL ME。它讓規劃從追問與確認開始，讓人和代理先釐清同一個問題，再動手。 [GRILL ME](https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md) · [grilling](https://github.com/mattpocock/skills/blob/main/skills/productivity/grilling/SKILL.md)

感謝 **Lauren Tan（poteto）** 公開 PSTACK。它對可驗收的拆單、動工前的解法比較，以及用可重跑的證據判定完成，直接影響了 FBF ENGINE 的角色設計。 [PSTACK 原版](https://github.com/cursor/plugins/tree/main/pstack)

| 角色與環節 | 借用的觀念 | FBF ENGINE 怎麼實作 |
|---|---|---|
| **PLANNER · 需求訪談** `grill` | GRILL ME 的追問、附上建議與等人確認，加上 PSTACK 的 `sharpen` 與 `in-your-own-words` | 訪談提示詞一次問一題、附上自己的判斷，最後用自己的話重述需求。要等人用 `requirement confirm` 確認才能建工單。 |
| **PLANNER · 拆單、設計驗收與裁決** | PSTACK 的 `figure-it-out`、`benchmark-checklist` 與 `verify-and-stop` | 一張工單對應一條端到端可驗收的路徑。每條檢查掛回已確認需求，訂出門檻與可重跑的證據。引擎依必做項結果判定能否結案。 |
| **THINKER · 解法與重工設計** | PSTACK 的 `architect`、`how`、`why` | 追蹤呼叫與狀態歸屬，提出至少兩案，比較成本與修改範圍。引擎檢查方案數和實質差異。 |
| **THINKER · 獨立驗收** | PSTACK 的 `interrogate`、`thermos` | 分別檢查缺陷與可維護性，逐條執行驗收並提交證據。引擎檢查結果是否覆蓋應驗項目，以及驗收者是否和施工者衝突。 |
| **WORKER · 施工、自檢與重工** | PSTACK 的 `tdd` | 能執行時先重現失敗，再修到通過。交件包含自檢結果、實跑案例數和產物指紋，引擎核對產物大小與 SHA-256。 |

FBF ENGINE 把上述觀念寫入自己的角色提示詞與交件守衛。角色方法集中在 `METHOD_NOTES`，狀態契約與驗收判定集中在 `workflow.py`。執行時只需本專案，兩套上游 Skill 均為方法來源。 [角色提示詞](fbf_engine/prompts.py) · [工作流契約與守衛](fbf_engine/workflow.py)

---

### 參考

- 規格正本：`spec/`（`FBF ENGINE工作流.md`、`工單設計.HTML`、`工單欄位對照表.html`、`ISSUE單設計.HTML`）
- 決策紀錄：`docs/decisions.md`
- 偏離紀錄：`DEVIATIONS.md`
- 本機交接紀錄由 Git 排除
