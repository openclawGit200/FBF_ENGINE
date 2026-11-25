# Decision Log

> This document records important project decisions and the reasoning behind them.
>
> 本機交接紀錄由 Git 排除。
> This document describes why important decisions were made.
>
> Do not use this file as a task list or research dump.

---

# Decision Status

Allowed statuses:

- `ACTIVE` — Current decision remains valid.
- `SUPERSEDED` — Replaced by a newer decision.
- `REJECTED` — Explicitly considered and rejected.
- `TENTATIVE` — Temporary decision pending validation.

---

## DEC-001 — 用既有連線鎖序列化 MCP 與 resident tick


**狀態** SUPERSEDED（由 DEC-002 取代；2026-11-19）

**日期** 2026-11-19

MCP 工具與背景續租／tick 在完整操作期間持有同一把連線鎖。現有交易沿用可重入鎖。關閉時先等待 tick 結束。

原先的交易鎖只涵蓋 store.tx。兩邊都可能在交易外查詢、取回資料或寫 journal，鎖住完整操作能涵蓋這些路徑。逐一鎖 SQL 需追蹤所有游標和間接呼叫，分開連線則需調整協調器的連線與租約生命週期。

工具呼叫可能需要等待一整趟 tick，包含平台 I/O。依據是 macOS 的 resident A/B 回報、程式的共用連線路徑，以及角色查詢例外的 stdio 回歸測試。

**為什麼被取代**：共用同一條 sqlite3 連線本身就是未定義行為——CPython 用
`SQLITE_OPEN_NOMUTEX` 開連線，底層不再替這條連線序列化。鎖只能保證「不交錯」，
不能保證「安全共用」。macOS 上的實測（開 resident 3/3 次 SIGSEGV、關 resident
一百多萬次呼叫零崩潰）證明這條路會炸。DEC-002 走的是這裡當時評估過、
但因為「要調整連線與租約生命週期」而沒選的那條路——那個調整成本現在已經付出去了。

---

## DEC-002 — 每個執行緒一條 sqlite3 連線


**狀態** ACTIVE

**日期** 2026-11-19

`Coordinator.conn` 改成 thread-local 屬性：**每個執行緒各開一條連線**指向同一個正本檔案。
背景 tick 執行緒與 MCP 請求執行緒不再共用連線。跨連線的並行交給 SQLite 自己的
檔案鎖（WAL ＋ `busy_timeout=30s`）；邏輯層的保護照舊是版本號與租約。
連線生命週期一併調整：協調器記住所有開過的連線，`close()` 逐一關閉並放掉租約。

取代 DEC-001 的理由是實測（見 `research/2026-11-19-resident-crash.md`）：共用一條
`NOMUTEX` 連線是未定義行為，鎖擋不住。另一個好處是工具呼叫不再要等一整趟 tick
（含平台 I/O）——那原本是 DEC-001 明知要付的代價。

影響：連線數隨執行緒數增加（每個專案最多數條）；`store.tx` 的可重入鎖退化成
同連線內的保護，不再是跨執行緒序列化。

---

## DEC-003 — 正本損毀要回穩定碼並停下來


**狀態** ACTIVE

**日期** 2026-11-19

正本損毀（`file is not a database`、`database disk image is malformed` 等）與
暫時性資料庫錯誤（`database is locked`）**分開判定**：

- 損毀 → MCP 回 `G-PROJECT-CORRUPTED`（附「先從備份還原」的動作），CLI 回
  `project.corrupted`；**常駐 tick 立刻停下**，不再往壞掉的正本寫。
- 暫時性錯誤 → 仍走 `INTERNAL`／`database.error`，不誤導人去還原備份。

理由：損毀與競爭的處置完全不同；把競爭說成「壞了、去找備份」會誤導人，
把損毀說成「INTERNAL」則看不出要動作。損毀是**原生層**的問題，沒有 Python
traceback，所以必須由介面層給出穩定代碼與可行動作。

---

## DEC-004 — 安裝器寫入實際執行安裝器的 Python


**狀態** ACTIVE

**日期** 2026-11-19

`fbf.py install`／`join` 寫進 OpenCode 設定的啟動命令是 `sys.executable`
（**正在跑安裝器的那個 Python**），不再是寫死的 `python3`——Windows 沒有
`python3`，舊設定直接載入失敗。替另一台主機產生設定時（帶 `--host-root`），
必須用 `--python` 指定**目標主機**的命令；沒指定就**拒絕執行**，寧可不要寫出
一份跑不起來的設定。

理由：設定要能在產生它的機器上直接跑；命令名稱寫死就是把環境假設塞給使用者。
跨主機時本機的直譯器路徑在那台機器上不存在，所以不能猜。

驗收：用安裝器產生的原始設定原樣啟動 → `initialize` 握手 → 9 個工具
（測試 `TestInstallerConfig`，刻意在非 UTF-8 環境啟動）。

---

## DEC-005 — stdio 與 CLI 輸出固定 UTF-8


**狀態** ACTIVE

**日期** 2026-11-19

MCP 的 stdio 傳輸與 CLI 的輸出**固定 UTF-8**，不讓 `PYTHONIOENCODING` 或主控台
字碼頁決定。Windows 預設是 cp950，不強制就會送出**主機端解不掉的位元組**：
握手回應裡的中文變成亂碼 → 握手失敗；CLI 的 JSON 同樣變成亂碼。

測試框架的輸出（`tests/__init__.py`）也同樣固定 UTF-8——測試結果要被工具讀，
不能是亂碼。

理由：stdio 是協定（契約要求 UTF-8），CLI 的輸出不只給人看（工具與 agent 也讀）。
輸出格式不該由環境決定。

---

## DEC-006 — 席位要有生命週期：人可以釋放，孤兒席位要看得見


**狀態** ACTIVE

**日期** 2026-11-19

席位的綁定（`roles.session_id`）本來**沒有生命週期**：session 一旦認領就永遠不放，
`session revoke` 也只改 `status`、留著 `session_id` → 沒有任何 session 能認領它
（`ROLE-SLOT-TAKEN`），整條流程靜靜卡住，而且沒有任何畫面顯示為什麼卡住。
「卡死後無人知道」是規格第 0 節列的故障之一，這裡就是它的入口。

決定：

1. **釋放是人的動作**：新增 `fbf.py role release --role <角色> --short-name <簡稱>`
   （清空 `session_id` 與該 session 的 UUID 雜湊，寫進裁決日誌）。
   留在人類控制面——**不開放給 session**（規格第 7 節：人類命令不暴露給 session）。
   理由：釋放等於宣告「那個 session 不行了」，這是人的判斷，不是機器的。
2. **`session revoke` 要真的空出席位**（以前只改 `status` → 席位照樣被占，
   那個指令等於沒有出口）。順手修掉 `session bind` 對既有席位撞 UNIQUE 約束的問題
   （它查席位是按 session_id 查的）。
3. **孤兒席位要看得見**：`role list` 與 `engine status` 顯示「被占住卻很久沒活動」
   （`stale`，預設 12 小時，設定 `seat_stale_hours`）。
   **只標示、不自動釋放**——自動釋放會讓一個只是安靜工作的 session 突然失去席位。
4. **被擋的 session 要知道出口**：`ROLE-SLOT-TAKEN` 的訊息直接說明
   `fbf.py role release ...`，否則它只會一直重試、而人也不知道要處理什麼。

影響：CLI 多一個子命令（DEVIATIONS D9）。席位佔用不再是一個不可逆的狀態。

---

## DEC-007 — 派工送不出去時要講清楚是哪裡壞了

**狀態** ACTIVE

**日期** 2026-11-20

實測（2026-11-20）：工單建好了、角色表也有人認領了，outbox 卻全部 `queued`、
`last_error=presence:unknown`——**看起來像「平台在忙」，實際是根本沒有設定
OpenCode server endpoint**：`presence_of()` 在 endpoint 為空時直接回 UNKNOWN
（那不是平台狀態，是設定缺失）。於是每個派工都被暫緩，
要等 `max_deferrals`（240 輪 ≈ 2 小時）才會標待人處理。

決定：

1. **沒有 endpoint 是設定錯誤，不是「狀態不明」** → outbox 的 `last_error`
   寫 `endpoint.unset: ...` 並附上設定指令，不再混在 `presence:*` 裡。
2. **`engine status` 是卡住時第一個該看的畫面** → 它現在列出
   `opencode_endpoint` 與 `warnings`（含「N 筆在等」）。
3. **文件說清楚 `--endpoint` 是必要的**（README 的安裝段與 FAQ）。
   沒有它，presence 查不到、訊息送不到、回執也核不過——整條流程只會靜靜卡住。

---

## DEC-008 — 全程只用 OpenCode 指令，不設定 server 位址

**狀態** ACTIVE

**日期** 2026-11-20

DEC-007 只讓「錯看得見」；這裡讓「錯不必發生、而且改了立刻生效」。

**先前的判斷錯了**：我們以為要設定 `opencode_endpoint`。實測 OpenCode 的日誌：

```
cli starting args="[\"--server\",\"http://127.0.0.1:49374\",\"api\",\"GET\",...]"
cli process failed  cause="Server at http://127.0.0.1:49374 requires a password; set OPENCODE_PASSWORD"
cli starting args="[\"api\",\"get\",\"/api/session/active\"]"     ← 這行沒有錯誤
```

兩件事：**server 跑在隨機埠**（49374，每次重開就換）而且**要密碼** →
寫死位址一定會壞，填對了也撐不過下一次重開。
而**不帶 `--server`** 的 `opencode api …` 會**自己找到本機正在跑的 server**
（連認證都一起處理 ✓）——那正是我們要的 presence 查詢。

決定：

1. **預設不帶 `--server`**（`platform._api`、`receipts._api`）。
   呼叫的是 **OpenCode 指令**（`opencode api …`），不是 HTTP server ✓
   ——這也正是規格第 10 節說的「重用既有 INFORM 的 OpenCode 指定 Session 通知」，
   不得另造派工通道。
2. **`--endpoint` 改回選用**：只有「要連到別台機器的 server」才需要明確指定。
   `join` 不再因為缺它而拒絕。
3. **endpoint 每輪重讀設定**（`Coordinator.endpoint` 改成屬性）。
   以前是啟動時讀一次：改了設定，正在跑的伺服器還是空的那份 →
   還得重啟整個 OpenCode 才知道。**改設定應該立刻生效。**
4. **查不到時要帶回平台那句話**（`presence:unknown（Could not reach server…）`）——
   「查不到」跟「平台在忙」外觀一樣，沒原因就查不下去。

### 附則一：查不到 presence 也要送（2026-11-20 追加）

使用者裁決：**不要用 server，讀工單狀態，依照工單狀態發提示詞給對應的 session**。

決定：**presence 只用來避免「打擾正在忙碌的 session」，查不到就照送。**

- 派工的依據是**工單狀態**（哪一棒該誰做）→ 角色 → 指派的 session，
  然後用 `opencode run --session <id> <提示詞>` 發過去——**全程只有 OpenCode 指令**。
- `presence == busy` → 暫緩（不打擾它）；上限到了標待人處理。
- `presence == unknown` → **照送**。查不到只代表「我們問不到」，
  不代表那個 session 不該收到（規格第 6 節：不當成離線）；
  而且那條路不通時，卡住不送等於**整條流程癱瘓**——實測就是這樣卡了兩小時，
  14 張單全部送不出去。
- 送不出去會走失敗路徑（有上限的指數退避 → 標待人處理）✓ 不會靜靜卡住。

---

### 附則二：租約帶版本，「跑的是不是新版」不再靠猜（2026-11-20 追加）

實測踩到的坑：**改了程式碼，但持有租約的行程還是舊的**——
MCP 伺服器是 OpenCode 一起起的，改檔案不會讓它重載，於是「修正沒生效」
看起來像「修正沒用」，來回查了好幾輪。判斷依據只有 outbox 的錯誤字串。

決定：**租約的 owner 帶上引擎版本**（`fbf-<pid>-<uuid>-<版本>`）。
`fbf.py engine status` 的 `lease.owner` 一看就知道正在跑的是哪一版 ✓；
換版時版本字串也一起換 ✓。

---

### 附則三：租約要能接手（2026-11-20 追加）

實測又踩到一個洞：**同一個專案會同時有多個 MCP 行程**（TUI 一個、
每個 CLI session 一個）。它們只啟動一次 `Resident`，而 `Resident.start()`
**拿不到租約就 return False 放棄** → 那些行程**永遠不再試**。
於是：持有者一被關掉，**沒有任何行程會接手** →
整個專案沒有人推進 → 工單靜靜卡住、沒有任何錯誤訊息。

決定：**`Resident` 永遠啟動，每輪都再試一次拿租約。**

- 拿不到 → 這一輪不做事，等下一輪再試（不搶、不靜默搶 ✓）。
- 持有者死掉 → 它的租約 30 秒 TTL 到期 → **有人接手** ✓。
- 自己的租約被搶（世代換了）→ 放掉那一輪，下輪再搶 ✓。

租約的「單一寫入者」不變——活著的持有者不會被搶 ✓（有測試守住）。

---

### 附則四：新版可以直接從舊版手上接管租約（2026-11-20 追加）

實況：改了程式碼，但**持有租約的舊行程還活著**（MCP 伺服器是 OpenCode 一起起的，
關掉 session 視窗不會停它 → 舊行程繼續用舊程式碼跑、繼續把派工暫緩）。
這時要新程式碼生效，舊的就得讓位。

決定：**租約 owner 帶引擎版本；新版遇到舊版持有者就強制接手**
（世代 +1 → 舊行程的續約立刻失敗 → 舊行程自動收手 ✓）。

- 判定：owner 最後一段是版本；**沒有版本字樣 → 算舊的**（版本標記之前的行程 ✓）。
- **同版本不搶** → 兩個新版行程仍然只有一個寫入者 ✓（有測試守住）。
- 強制接手會寫 `coordinator.takeover` 的日誌 ✓（可追）。

好處：**不必殺任何行程**——開一個新的 OpenCode session 就會帶起新的 MCP，
它會從舊行程手裡接管租約，30 秒內把停滯的派工送出去。

---

### 附則五：回執核對的關聯鍵是 task_id，不是 agent 自報的 message_id（2026-11-20 追加）

實測：session 有確實收到派工、有確實回報（`inbox/.opencode/*.log` 有完整回應 ✓），
但提交一直卡在 `unverified`，理由是「缺少 OpenCode server endpoint」✓。
兩個問題疊在一起：

1. **回執核對的前置檢查還在要求 endpoint** → 明明 CLI 自己找得到 server，
   卻連查都不查就放棄 ✓ → 拿掉（`_api` 已經會用 CLI 自動發現 ✓）。
2. **拿 agent 自報的 message_id 去平台比對** → agent 收到的是「注入給它的那段提示詞」，
   **看不到平台給訊息編的 id** → 它報的必然是編的（如 `msg_W001_THA_T1_001`）
   → 永遠比對不到 ✓。

決定：**關聯鍵改用 `task_id`**（派工提示詞裡一定帶 ✓——那本來就是設計給
回執核對用的關聯鍵）。agent 自報的 message_id 退為「唯一性檢查」用途
（規格第 7 節要求的「message_id 未被其他 submission 使用」✓），
若它剛好在平台上則用來核對 session 歸屬 ✓。

不變的：規格第 7 節那五項仍然全要過——task_id 對得上、message 由被指派 session
產生、時間在派工後、平台標示完成（`finish=stop`）、message_id 未被其他提交使用 ✓；
**查不到就當成沒完成** ✓。

---

### 附則六：回執要照「平台真正的樣子」認，不照想像的樣子（2026-11-20 追加）

規格第 7 節早就寫著：**整合時以實際 OpenCode 回應固定 fixture，
不根據猜測的 JSON 格式宣稱核對成功** ✓。這次照著做——直接從 Mac 上
`opencode.db` 挖真實訊息，餵進核對器驗證。

**OpenCode 2.2.5 的真實樣子**（實測；與舊假設差很多）：

| 項目 | 舊假設 | 真實（2.2.5） |
|---|---|---|
| 訊息外殼 | `{info:{...}, parts:[...]}` | **扁平** `{id, type, time, ...}` |
| 角色欄位 | `role` | **`type`**（`user`／`assistant`） |
| 使用者文字 | `parts[].text` | **`text`** |
| assistant 內容 | `parts[]` | **`content[]`** |
| 模型 | `providerID` ＋ `modelID` | **`model:{id, providerID}`** |
| 完成狀態 | `finish` 或用時間戳推論 | **只看平台明講的 `finish`** |
| 端點回應 | 直接是陣列 | **`{"data": [...]}` ＋ 游標分頁**（預設只回 50 筆） |
| active 清單 | 陣列 | **以 session id 為鍵的物件** |

四個實測校正（都是照真實資料改的，不是猜的）：

1. **回執錨點是「交件工具的呼叫」**，不是「最後一則 assistant 訊息」。
   OpenCode 一輪對話會拆成多則 assistant 訊息（產生回覆 → 跑工具 → 再產生下一則），
   最後那則 `finish=stop` 反而**晚於**提交。
   真正的證據是 agent 用 `execute` 工具跑 JavaScript 呼叫
   `tools["fbf-engine"].fbflow_report_work({task_id: "T-000001", ...})`
   → **同一個工具呼叫裡**同時出現交件工具名與 task_id 才算數 ✓。
2. **重試要挑「真的交成」那一則**：實測同一工單交三次，前兩次是語法錯誤
   （工具根本沒跑），第三次才產生 submission。
   工具回應裡有 `"submission_id"` → 那才是交件落地的證據 ✓。
3. **`completed` 比提交時間晚不到一秒**（平台在工具收尾之後才標），
   所以「回執必須早於提交」會誤殺正確回執 → 改成 5 秒容差 ✓。
4. **`time.completed` 不能拿來推論完成**：中間步驟訊息也帶它 →
   完成只看平台明講的 `finish` ✓
   （附則五寫的「平台標示完成（`finish=stop`）」由本附則校正）。

另一個卡住的地方（跟回執無關，但一樣是「靜靜卡住」）：
工單被標 `awaiting_human=receipt_timeout` 之後，
`_create_task_for_state` 看到 `awaiting_human` 就回頭 → **下一棒（PLANNER）永遠建不出來**，
而且沒有任何錯誤訊息。決定：**這一棒套用成功就解除停等人標記**——
那一棒既然通了，就沒有東西需要人處理 ✓。

驗收：拿 Mac 上真實的 14 筆提交 ＋ 真實訊息跑核對 → **14/14 通過**，
且每一筆對到**自己**那一則訊息（不是 14 筆共用同一則）✓。

---

### 附則七：派工提示詞要帶「完整交件契約」，退件要有人接手重交（2026-11-20 追加）

用真實資料跑守衛才發現的兩件事（都不是猜的）：

**一、agent 交不來，不是它偷懶，是它**根本不知道**要交什麼。**

實測：THINKER 註冊時提示詞寫的是「**你現在沒有待交的工作**」
（那時還沒有任何 task），而派工提示詞只列了欄位**名稱**：

```
這一棒要交的東西：
  - solutions
  - design_note
```

於是它交了兩欄就交件——沒有 `handoff`、每案沒有 `subagent_context_id`、
也沒有 `approach`／`cost`／`change_scope`／`effort`／`risk`／`verifiable_by`
→ **整批 14 筆全被守衛退回**，工單原地不動、沒有錯誤訊息。

決定：**派工提示詞要帶完整交件契約**（規格第 6 節的「本棒交件清單」）。
欄位規格定義在 `workflow.STAGE_FIELD_SPECS`（規則正本），
`stage_contract()` 把它交給提示詞渲染 → **提示詞與守衛共用同一份定義**，
不會漂移（規格第 3 節：「新規則只准有一份定義」）。

**二、退件不等於卡住——要重新派一棒，並把缺什麼講清楚。**

守衛是在 tick 裡跑的，角色交件當下只會收到「已收到，狀態由協調器推進」。
退件結果是**事後**才有的；沒有重新派工，沒有人知道要重交。
決定：**退件 → 取消舊 task → 建新 task → 重新派工**，提示詞帶著
「缺哪個欄位 ＋ 可行下一步」（`workflow.GAP_HINTS`）。
退件次數有上限（`MAX_STAGE_REJECTIONS`，預設 3）→ 超過標待人處理，
不允許無限繞圈（規格第 5 節的流程上限精神）。

順帶修正兩個**缺口代碼不精確**的地方（規格第 5 節要求「缺哪個欄位」看得見）：
`design_note` 缺項、`selfcheck` 缺項、`unfinished` 格式錯
原本**全部**記成 `handoff.missing` → 拆成 `design_note.missing`、
`selfcheck.missing`、`unfinished.invalid`；`build` 階段補上 `outputs.missing`。

**還有一個靜靜卡住的角落**：工單被標 `awaiting_human=receipt_timeout` 之後，
`_create_task_for_state` 看到 `awaiting_human` 就回頭 → 下一棒建不出來、
退件後的重交也派不出去。決定：**回執核對通過就解除停等人標記**
（那個理由是「等回執等到放棄」，回執既然到了就沒東西需要人處理）✓。

**同類的第三個角落**：`outbox` 的 `submitted`（CLI 已啟動、未確認送達）
原本**永遠不會再被檢查** → 派工若靜默失敗，沒有任何錯誤訊號，
工單就停在「沒有人接手」的狀態。決定：`submitted` 逾時 180 秒後
**併入送達查核**（跟 `unknown`、逾時的 `sending` 同一路徑）→
查得到就標 `delivered`，查不到就轉 `unknown` 等下一輪，
證明沒送達才重送 → 仍然不盲目重送、仍然有上限 ✓。

---

### 附則八：引擎**永遠不啟動** OpenCode server（2026-11-20 追加）

使用者指示：「拿掉 opencode server，也不要再啟動，把程式碼裡面所有會啟動
OpenCode SERVER 的程式碼全部改掉。」

**根因**（上游原始碼，v2.2.5 `packages/cli/src/services/server-connection.ts`）：

- 帶 `--server <url>` → CLI **只連線**，連不上就報錯，**不會啟動任何東西** ✓
- 沒帶 `--server` → `Service.ensure()` → **自動啟動一個常駐背景 server** ✗

而舊的三條路徑（`platform._api`／`receipts._api`／`send_opencode`）都是
「endpoint 為空就不帶 `--server`」→ **每一次呼叫都可能在那台機器上開一個 OpenCode server**
（連帶它裡面的 MCP）。實測：Mac 上那個 server（pid 1568、埠 49374）就是這樣來的。

決定：

1. **每一個 `opencode` CLI 呼叫都必定帶 `--server`**（`ocserver.cli_argv`）✓
2. 端點由 `fbf_engine/ocserver.py` **唯讀**探索既有 server：OpenCode 自己把
   位址與密碼寫在 `~/.local/state/opencode/service.json`
   （實測欄位 `url`／`password`／`pid`／`version`）；也支援 `--endpoint` 與
   `FBF_OPENCODE_SERVER`。
3. **探索不到就明確報錯**（`ServerMissing`），**不讓 CLI 去開一個**：
   - presence → 當成「查不到」（`UNKNOWN`），照規格照送；
   - 回執 → 當成「未核對」（`unverified`），不推進；
   - 派工 → **明確失敗**（`DispatchError` state=`failed`）→ 退避重試 → 標待人處理 ✓。

**這是 DEC-008 的補充，不是推翻**：仍然「全程只用 OpenCode 指令」，
只是**不再讓 CLI 自己去發現**（那會開 server）；改成我們自己唯讀探索、並且必定帶 `--server`。

守門測試（`TestNeverStartsOpenCodeServer`）：三條路徑的 argv 都必須含 `--server`；
探索不到時**連 CLI 都不呼叫** ✓。

---

### 附則九：派工提示詞是**回執的錨點**——三種派工都要帶 `task_id`（2026-11-20 追加）

把「派工這段」逐條核對後才發現的缺陷。

**派工的責任歸屬**（先講清楚，避免再混淆）：

```
MCP 被 session 連上而醒來
  → 定期跑 tick：定期檢查工單跟 ISSUE 單
  → 依照工單狀態決定這一棒該誰做
  → **走 CLI 發指令給 OpenCode session**（派工者是 MCP，不是 OpenCode server）
  → 提示詞告訴 session 去讀哪一張工單／ISSUE 單
```

OpenCode server 只是「session 所在的地方」，不是被我們管理的服務 → 這就是
「引擎永遠不啟動 server」的落點（附則八）✓。

**缺陷**：回執核對的關聯鍵是 `task_id`（規格第 7 節）——它必須**出現在派工提示詞裡**，
核對器才能在平台訊息清單裡找到「這一次的派工訊息」。實測三條派工路徑：

| 派工路徑 | 原本有 `task_id`？ | 後果 |
|---|---|---|
| 工單派工 | ✅ 有 | 正常 |
| **ISSUE 派工** | ❌ 沒有 | **ISSUE 提交永遠 `unverified`** → 流程卡在第一筆 |
| **里程碑檢視派工** | ❌ 沒有 | **里程碑檢視永遠 `unverified`** → 段末永遠收不掉 |

而且這兩種提示詞也**沒有告訴 session 怎麼讀**（工單派工有 `fbflow_read(target_kind=work, …)`）。

決定：**三種派工提示詞都必須帶 `task_id` ＋ 讀取工具 ＋ 回報方式**，
格式與工單派工一致 ✓。

守門測試（`TestEveryDispatchCarriesTaskId`）：三種派工的提示詞都必須含
`task_id: T-…` 與對應的 `fbflow_read(target_kind=…)` ✓。

**同一段還有一個缺陷**：ISSUE `handling` 階段的契約寫著必交欄位 `answer_or_work`，
但守衛（`engine._check_issue_stage`）實際查的是 **`answer` 或 `linked_work_ids`**——
提示詞叫 agent 交一個守衛根本不看的欄位 → agent 照做 → 退件 → 流程卡住。
決定：**契約欄位名必須跟守衛一致**，並加上 `ISSUE_FIELD_SPECS` ＋
`required_note`（「兩者至少一個」／「二選一不可同時給」）→
提示詞與守衛共用同一份定義（同樣不留第二份）✓。
守門測試：`test_issue_prompt_names_the_fields_the_guard_checks` ✓。

---

## DEC-009 — 讓「卡住」的流程有出口：回執到期重試、釋放席位作廢 task、暫時性 DB 錯誤不熄火

**狀態** ACTIVE

**日期** 2026-11-20（引擎版本 `20261120k` → `20261120m`）

**起因**：使用者回報 W-004／W-013 卡在 `receipt.unverified`，流程停住；
加上前一輪靜態審查出的四個「整條流程會靜靜死掉」的問題。**七項修正，
每一項都有守門測試**（193 項全過）。

### 1. 回執到期不再無限重查：`expired` 終態 ＋ 換新 task 重試 ＋ 上限停等人

**原本**：`unverified` 提交等 10 分鐘到期 → 把 works 標 `awaiting_human`，
但 submission **留在 `unverified`** → tick 每輪照樣撈出來重查平台，
沒有重試、沒有次數上限、`awaiting_resolve` 裁決後也**接不回去**
（舊 task 還開著 → `_dispatch_pending` 跳過）。

**現在**（`engine._receipt_expired`）：
- 到期 → submission 標 **`expired`（終態，脫離重查）**；
- 作廢該筆的 task → **換新 task 重試**（提示詞帶「上一棒回執未核對」
  與「用新 message_id」）；
- 同一張單的到期次數用 `counters` 表計（`receipt_expires:<id>`），
  連 **3 次**（`max_receipt_expires`）才標 `awaiting_human=receipt_timeout`，
  進 `awaiting list` 給人裁決；
- `awaiting resolve grant_more` 對 `receipt_timeout` **重置計數、
  不動 state**（那不是重工，不該跳 rework_design），下一趟 tick 重派。

**同時放寬回執錨點**（`receipts.verify_opencode`）：agent 看不到平台編的
message id（附則五），自報值找不到時**不再直接判 unverified**——改用
「assistant 用交件工具交出這個 task_id」的錨點核對；但**派工訊息與工具
錨點都沒有 → 依然判 unverified**（查不到就當沒完成，規格第 7 節不動）。

### 2. 釋放席位要一併作廢該 session 的 open task

**原本**：`role release`／`session revoke` 只清 roles 表；session 死掉後
它名下的 open task 永遠開著 → `_dispatch_pending` 看到「已有開啟任務」
就跳過 → **新 session 認領了席位也接不到活**（README 承諾的「接管」是假的）。

**現在**：釋放／撤銷時把該 session 的 open task 全部標 `cancelled`
（寫進 journal），下一趟 tick 為受影響的工單重派給新主人。

### 3. 暫時性 DB 錯誤不得讓常駐協調器熄火

**原本**：`Resident._loop` 把**任何** `sqlite3.DatabaseError` 當正本損毀
`break` → 一次 `database is locked` 就讓 tick 執行緒永久死亡。

**現在**：用 `store.is_corrupted` 區分——損毀才停（DEC-003 不變）；
暫時性錯誤記進 `last_report`，下一輪重試。

### 4. tick 中途續租約、派工前驗租約

**原本**：租約只在 tick 開頭續約，一趟 tick 可能含大量平台 I/O（每頁 CLI
30 秒逾時）超過 30 秒 TTL → 另一行程接手 → **兩邊同時派工**（`_flush_outbox`
不驗租約）。

**現在**：每處理完一筆 submission 續一次約（續不到 → 收手本輪）；
`_flush_outbox` 之前再驗一次租約（`lease-lost-before-flush`）。

### 5. 工單主更新補樂觀鎖 rowcount 檢查

**原本**：`_apply_work_submission` 的 `UPDATE works ... WHERE version=?`
不看 rowcount → 競爭時照樣 mark applied、建下一棒、寫回執 → 狀態機推歪
（ISSUE 路徑本來有檢查）。

**現在**：rowcount ≠ 1 → 丟 `_StaleSubmission` 讓交易回滾 → 標 `stale`、
**不重試套用**（規格第 4 節）。

### 6. adjudicate 的交件契約與守衛對齊

**原本**：`required=["action"]`，守衛卻要 `close_reason`／`rework_reason`／
`rework_type`／`diagnosis`／`remedy` → agent 照提示詞只交 action →
退件三次 → 標待人處理（與 ISSUE `answer_or_work` 同型的「兩份定義漂移」）。

**現在**：契約列全六欄 ＋ `STAGE_FIELD_SPECS` 補欄位說明 ＋
`required_note`（「依 action 只交需要的那組」）——提示詞與守衛共用
`workflow.py` 同一份定義（規格第 3 節）。`stage_contract` 與
`build_dispatch_text` 現在會帶出 `required_note`。

### 7. 重工原因真的落庫

**原本**：`apply_stage` 的 adjudicate 分支把 `work` 裡**還沒被寫入的**
`rework_reason` 讀回來用（永遠是 None）→ PLANNER 填的退回原因從來沒進
正本 → `rework_design` 的 THINKER 收不到「為什麼被退回」。

**現在**：`_guard_adjudicate` 把 payload 欄位隨 `data` 帶回去
（`close_reason`／`rework_reason`／`rework_type`／`diagnosis`／`remedy`），
`apply_stage` 寫 `data` 的值。

### 不變的

規格第 7 節五項回執條件、查不到就當沒完成、引擎永不啟動 OpenCode server、
MCP 唯一前門、九狀態、流程計數器不是資源配額——全部不動。

### 驗證

`python -m unittest discover -s tests -t .` → **193 項全過**
（184 既有 + 9 新增守門測試）。
Windows／cp950 環境實測；**macOS 與真實 OpenCode server 端到端仍 NOT_RUN**。

---

## DEC-010 — 對齊 `spec/`：工單／ISSUE 檔案層、提示詞不帶狀態、十狀態、ISSUE 六狀態（2026-11-23）

**背景**：規格正本改為 `spec/`（`FBF ENGINE工作流.md` ＋ 兩份 HTML 模板）。
本輪把引擎對齊到那份規格，並修掉兩個跑完整工作流時抓到的引擎缺陷。
逐條理由見 `DEVIATIONS.md` D17~D22。

### 1. 工單／ISSUE 檔案層（spec 第 73~80 行）

新增模組 `workfiles.py`（**已於 DEVIATIONS D34 移除**）：引擎生成 `工單/未完成/<id>/工單.md` 與
`ISSUE/未完成/<id>/ISSUE.md`，結案時搬到「已完成」。建立時寫檔、每趟 tick 重寫。
`works` 表新增 `skill`（搭配的專案 SKILL），`fbflow_work_create` 對應新增參數。

### 2. 提示詞不再帶工作狀態（spec 第 70 行）

派工提示詞只留 task_id／工單檔案路徑／專案 SKILL／產出資料夾／**方法**／
交件方式；**狀態與交件契約搬進工單檔案**（`stage_contract` 同一份定義）。
`fbflow_report_work`／`fbflow_report_issue` 的 `stage` 改成選填——
留空就依 `task_id` 判定這一棒。

### 3. 方法精神進提示詞（spec 各步的「對應 pstack」）

`prompts.METHOD_NOTES`／`ISSUE_METHOD_NOTES`：每棒一段「該怎麼做」的方法指引
（精神，不是工具名）。對應調整見 `DEVIATIONS.md` D19。

### 4. 工單十狀態、ISSUE 六狀態

新增「重工驗收方式設計中」；重工解法也要兩個獨立 SUBAGENT。
ISSUE 改 `raised → designing → handling → building → adjudicating → closed`，
退回上限 2（舊預設 3 自動遷移，人自訂不動）。

### 5. 兩個引擎缺陷（跑完整工作流時抓到）

- **永久退件沒有重新派工** → 角色收不到「要重交」的通知 → 單子靜靜卡住。
  新增 `_redispatch_rejected()`，回執永久失敗與 context 永久查不到都會重新派一棒。
- **ISSUE 退件不升版本** → 角色用同版本重交會撞唯一約束 → 這張 ISSUE 再也推不動。
  `_bump_work_version` 改成工單／ISSUE 都升版。

### 驗證

`python3 -m unittest tests.test_fbf_engine tests.test_mcp_server tests.test_hostterm_context tests.test_workflow_e2e`
→ **197 項全過**（含 `tests/test_workflow_e2e.py`：把 spec 的工單／重工／ISSUE
三條流程整條跑完，順便驗工單檔案會跟著狀態走、結案會搬檔）。

### 不變的

回執五項條件、查不到就當沒完成、引擎永不啟動任何 session server、
MCP 唯一前門、租約與版本接手機制——全部不動。

### 6. live 實測又抓到第三條：退件不重新派工 → 單子靜靜卡住

**現場**（2026-11-23 20:18~22:00，引擎版本 `20261123j`）：W-002／W-003 連續兩次
被退件（`solution.context_unverified` → `apply.rejected`），**引擎沒有重新派工** →
角色交完就結束回合、沒有人通知 → 兩張單靜靜卡了 1.5 小時，沒有錯誤訊息。

**為什麼會 `apply.rejected`**：THINKER 第二次交件時把**第一輪的
`subagent_context_id`** 拿來用 → 套用階段撞 UNIQUE 約束 → ValueError。
（守衛那時只檢查「這一筆裡兩案是否同一個」，沒檢查「是否用過這張單用過的 id」。）

**三層修法**：

1. `apply.rejected` 也重新派工，退件說明帶上引擎實際擋下的原因。
2. 新缺口 `solution.context_reused`：**重用這張單用過的 context id 在守衛就擋**，
   並明講「重工解法要的是兩個**新的**獨立 SUBAGENT」。
3. **自癒路徑** `_heal_rejected_tasks`：task 還開著、但它自己的提交已被退件
   → 收掉它、讓這張單重新被派。退件路徑萬一又漏了，也不會再靜靜卡住。
   （已標待人處理的不動——那是在等人裁決，不是忘了重派。）

**驗證**：`tests/test_workflow_e2e.py` 兩條回歸測試
（`test_rework_design_cannot_reuse_the_first_round_contexts`、
`test_engine_heals_a_task_whose_submission_was_rejected`）。

### 7. 工單檔案加「最近一次退件」

角色讀不到上一輪的對話，退件也不會進它的畫面——只看工單檔案的話，
它會以為自己還沒交過，然後交出同樣的東西。檔案裡現在寫著最近一次退件的
缺口代碼、說明、與引擎實際擋下的原因。

### 8. 派工提示詞必帶「上次為什麼被退」——閉環最後一塊

**問題**：退件會重派了，但角色仍不接。THINKER 的原話是「引擎壞了，我不重交」——
它讀不到上一輪對話，提示詞又不帶狀態，所以以為自己還沒交過，
交出同樣的東西再被退一次。

**決定**：`_pending_rejection_note()` 把「退件了、且退件後沒有 applied 提交」
的缺口代碼＋缺口說明＋**引擎實際擋下的原因**附在派工提示詞裡；
工單／ISSUE 檔案的「最近一次退件」段落移到狀態表之後（最上面）。

**live 驗證**（`20261123l`，2026-11-23 22:26）：
W-002 收到帶原因的重派 → 重交 → **applied** → 進入 `rework_acceptance_design`；
W-003 重交時沿用舊 message_id → 被 `receipt.message_reused` 擋 →
引擎自動重派並明講「換新的 message_id」。

**沒做的**：派工後角色逾時未回應的自動回收。spec 沒定義，
加上會有「還在做事但被收掉」的風險，先留著。

### 9. 型別也要正規化——「沒選它」變成逐字元亂碼

**現場**：`20261123m` 上線前，三張工單檔案的「選擇哪個解法」段落是
「沒選它：S」「沒選它：O」「沒選它：L」……十幾行。

**原因**：角色把「為什麼不要其他案」交成**一個字串**；守衛只驗
`solution_id`／`reason`，沒管 `rejected_because` 的型別；
渲染時直接迭代 → 逐字元印出來。

**決定**：兩端都正規化（`workflow.normalize_selection`）——
寫入端在守衛層轉、讀取端渲染前再轉一次。
讀取端那一次是為了**把既有資料救回來**（不用回溯改 DB）。
ISSUE 的 `selection` 同樣處理（字串會讓 `.get()` 炸掉整趟 tick）。

**回歸測試**：`test_selection_rejected_because_given_as_a_string_stays_one_line`。

### 10. 一天花在哪：BUG 產出，不是假單跑完

**11 次退件全是角色端交壞**（欄位漏填 8 次、沿用舊 id／舊 message_id 5 次…），
引擎該擋的擋了、該重派的重派了。**假單不會讓角色變準**，
但只有真的角色才會拒絕重交、才會交錯型別、才會拿舊 id 交——
所以 BUG 只會在 live 才浮現。今天的產出：

- 4 條退件路徑的重派閉環 + 派工帶退件原因；
- `selection` 型別正規化 + ISSUE `selection` 崩潰防護；
- 每個 live BUG 都釘成回歸測試（229 條，26 秒跑完）——
  **同一類 BUG 不用再花一天**。


### 11. 取消 SUBAGENT：THINKER 一棒直接產兩案（2026-11-23）

**現場**：W-001 的重工解法棒，THINKER 分兩個 SUBAGENT 各跑一案，
**跑了十幾分鐘還沒回來**；而且它得自己編 `subagent_context_id` 交回來給引擎核對，
核不過就退件 → 單子繼續坐。

**決定**（spec 三個檔案同步改）：

1. THINKER 在同一棒**直接產至少兩案**，不分派 SUBAGENT。
2. **不再要求 `subagent_context_id`**，引擎**不再回平台核對 context**
   （`receipts.verify_context`／`hostterm.verify_context` 與整條 transcript 證據鏈一併刪除）。
3. PLANNER 選案改用**案號**（解法表第幾案，1 起算）；引擎的 `SOL-xxxxx` 與舊的
   context id 也接受 → 舊的交件不會因為換了選法就卡住。
4. **保留**「至少兩案」與「兩案不得實質相同（成本與修改範圍不能都一樣）」。

**代價（誠實記）**：兩案不再由兩個獨立代理產生，獨立性下降；
「兩案不同」只剩內容檢查。這是取捨，不是免費的。

**資料庫**：`solutions.subagent_context_id` 改為可空、拿掉
`UNIQUE(work_id, subagent_context_id)` → 需要重建表（SQLite 不能 ALTER COLUMN）。
`store.migrate_solutions_without_context_unique()` 會在啟動時做，資料照抄、id 不變；
已用 live 正本的副本驗過（10 筆 solutions 全數保留、可插多筆 NULL、第二次執行不動手）。

### 12. 角色不回應也要有人接手（2026-11-23）

**決定**：加 `engine._expire_stalled_tasks()`——角色完全沒動靜 30 分鐘，
引擎收掉那一棒並重派，提示詞講清楚是逾時。

**為什麼這樣算**：計時基準是**角色最近一次交件**（不是單棒建立時間），
而且一次只收最早的一棒 → 多棒排隊時不會誤殺。

**不做**：不設「這張單已等太久就標待人處理」——那是另一種語意（等人裁決），
跟「換人接手」不同，混在一起會把自動復原變成人工瓶頸。

### 13. 租約要「活著就續約」，而且舊版不准搶（2026-11-23）

**現場**：23:01 舊版 MCP server 把租約搶走，專案整整一分鐘跑舊邏輯。

**原因**：一趟 tick 可能比 30 秒 TTL 還久（派工要等宿主 CLI 提交），
續約只在 tick 之間做 → 中途過期；而到期後的正常取得路徑不看版本 → 舊版搶得到。

**決定**：

1. **背景續約執行緒**：持有者活著就續約（這正是租約的意思）。
2. **取得租約時先比版本**：持有者是舊版就強制接手，不論有沒有到期。

**不做**：不把 TTL 拉長。TTL 拉長會讓「持有者死掉 → 別人接手」變慢，
而接手延遲正是「單子靜靜卡住」的主要來源之一。

### 14. 「沒有人接手」要有聲音（2026-11-23）

**現場**：10 小時零提交、引擎只有 dispatch.verify、**沒有任何訊息**指出
「沒人認領席位」。

**決定**：派不出去時寫進 tick 報告的 `errors`（一句話：工單、狀態、缺哪個席位）。
沒有訊息 = 沒有人知道要開 session = 單子靜靜卡到天亮。

### 15. 「出聲的只能是 FBF ENGINE」（2026-11-23）

**決定**：「沒有人接手」這件事由**引擎自己**負責表達，三個層次：

- **正本留痕**（journal `dispatch.no_seat`，每個 (單, 狀態) 一次）；
- **引擎的狀態問得到**（`fbflow_status` 的 `unassigned` 欄位）；
- **不塞進派工提示詞**——席位沒人認領是**人的問題**，不是那一棒要交的內容；
  塞給角色只會讓角色以為自己該處理。

**不做的**：不因為「沒席位」就把單子標成待人處理（`awaiting_human`）——
那是「流程停下來等人裁決」的語意，跟「還沒有人開 session」不同，
混在一起會讓引擎自動復原的路徑少一條。

### 16. 巡檢＋喚醒：引擎要會看、也要會叫（2026-11-23）

**決定**：

1. **巡檢**：引擎每趟 tick 看四種病（沒席位／派不到／停滯／等人太久），
   結果進 `fbflow_status` 與 `fbf.py engine status`，新問題寫 journal。
2. **喚醒**：單據狀態決定現在該誰做；**沒有在做的時候送提示詞叫他回來**
   （10 分鐘沒動靜就推一把，同一棒最多 3 次）。

**為什麼喚醒提示詞不放 task_id**：回執核對靠「user 項含 task_id 且之後有
assistant 回覆」判定交件成立 → 喚醒若帶 task_id，角色回一句「收到」就會被
當成**已交件**（假回執）→ 引擎誤以為有人做了。改用獨立錨點（`WU…`），
送達核對不受影響。

**分工**：引擎能自己解決的（停滯、沒回應）自己解決；
需要人的（沒席位、等人裁決）**出聲**，而且出聲留在正本裡、問得到。

### 17. 新專案自己加入：session 可以呼叫 `fbflow_project_join`（2026-11-25）

**現場**：新專案的 session 在宿主裡開起來後，第一個 MCP 呼叫就被擋下來——
「這個 MCP 連線沒有綁任何專案」。要嘛人去終端機跑 `fbf.py join`，
要嘛懂技術的人自己去改專案設定檔寫 `FBF_PROJECT_ROOT`。
**「加入」變成一件要人來救的事**，而且錯訊息只說「不行」，不說怎麼做。

**決定**：

1. **加入引擎變成一個工具**：`fbflow_project_join`。
   冪等、可重複呼叫；**只能作用於目前工作目錄或它的子目錄**——
   session 宣告的是「我現在就在這個專案裡工作」，不是「我想指哪裡就指哪裡」。
   引擎的安裝目錄仍然拒絕（正本不放安裝目錄是硬規則）。
2. **CLI 與 MCP 走同一份邏輯**：`fbf_engine/project.py` 的 `join_project()`
   是唯一實作，`fbf.py join` 與 `fbflow_project_join` 都呼叫它——
   避免兩套「加入」的定義漂移。
3. **當下這條連線就能用**：session→專案的綁定記在全域狀態檔
   （家目錄的 `.fbflow`），`resolve_project` 的優先序變成
   **環境變數 → session 綁定 → 啟動目錄**。
   綁定**不放引擎安裝目錄**——那裡整棵樹都不放專案資料。
4. **錯訊息要指路**：還沒綁專案時，錯誤訊息直接說「呼叫 `fbflow_project_join`」，
   `initialize` 的說明也把加入放在第一步。
5. **引擎自動帶路**：派工提示詞（session／grill／decompose）**自帶規則正本的路徑**
   （`SKILL.md` 與 `spec/FBF ENGINE工作流.md`）。
   session 不必再去翻引擎原始碼才知道流程怎麼走。

**為什麼 session 可以自己加入**：加入是**宣告**（我在這個專案裡工作），
不是**決策**（要不要做這件事）。它不寫任何工單、不動既有資料、
不越權到別的專案——護欄是「只能目前工作目錄或子目錄」＋「安裝目錄拒絕」。

**不做的**：不讓 `fbflow_project_join` 建立里程碑、工單或確認需求——
那些仍然是人類的命令（`requirement confirm`），維持在 CLI。

### 18. 需求變了不要改已發出的工單——照新需求開新工單（2026-11-25）

**現場**：需求在工單發出之後又變了。改已發出的工單很麻煩：
版本要重對、已交的件要退件、交接要重寫、重工計數要重來。

**決定**：

1. **已發出的工單不可改**（內容與狀態都不動）。需求或做法變了 →
   **照新需求開新工單**（`fbflow_work_create`），
   再用 `fbflow_work_supersede` 把舊工單標成**已取代**。
2. **「已取代」只做三件事**：標記（寫下被哪些新單取代、為什麼）、
   停工（取消開著的 task）、退場（不再派工、交件被擋回 `WORK-SUPERSEDED`）。
   **版本號不 bump**——bump 會讓還在施工的角色收到看不懂的 `TASK-STALE`。
3. **需求層也要能取代**：需求變了要**新增**一條需求（人確認），
   舊需求記住被哪條取代（`requirement confirm --supersedes`）。
   已確認需求**不可修改**（既有紅線）。
4. **結案追溯要認取代**：被取代的需求不再要求有結案工單；
   但**它底下的工單必須也標成已取代**——沒標的話舊單還會被派工做白工，
   專案也結不了案（引擎會擋下來並指名是哪幾張單）。
5. **看得見**：`fbflow_status` 多帶 `superseded_works`——
   被取代的單不再出現在 `open_works`，但看得到它被誰取代、為什麼。

**護欄**：不能取代已結案的工單（結案是歷史）、取代者必須存在且不是已結案／
已被取代（取代鏈只留一層）、一定要說明原因、只有 PLANNER 能做。

**為什麼不在「需求變了」時自動取代**：取代是**範圍決定**（這件事還要不要做），
不是機械動作——引擎不該自己丟掉一張已發出的工單。必須有人（PLANNER）明講。

### 19. 閒置的 session 要有人叫它收合上下文（2026-11-25）

**現場**：session 做完一棒就結束回合，等下一棒時閒在那裡——
對話越來越長，等真的接上新工作時，前面的雜訊已經塞滿上下文。

**決定**：引擎在 session **沒有工作可以做**、而且**離上一次 compact 超過半小時**時，
送一段以 `/compact` 開頭的提示詞叫它收合上下文。

四道條件，缺一不可：

1. **沒有工作可以做**——**看單據狀態**（使用者裁決）：不是看任務數。
   - 有開著的 task → 有工作。
   - 有單據處於「等這個角色做」的狀態且**還沒有人做** → 它等一下就有事做。
   - 都沒有 → 真的沒事做。
2. **閒置落定期 10 分鐘**——使用者提醒：「MCP 一派單，session 就去 compact」是壞的。
   剛交完件、剛被派過單的 session 此刻沒有開著的 task，但它一點都不閒。
   落定期讓提醒只落在**真的閒置一陣子**的 session 上。
3. **距上一次 compact 超過半小時**——上半小時以**宿主紀錄**為準：
   Claude Code 的 transcript（`isCompactSummary`）、OpenCode 的訊息（`type=compaction`）；
   偵測不到就用引擎自己記的「上次要求」時間當保守值。
4. **有對話紀錄**——剛開的空 session 沒有東西可收合。

**提醒怎麼送**：走既有的 outbox 通道（與派工、喚醒同一套），
用**獨立錨點**（`CP…`）——帶真 task_id 會被回執核對當成「這一棒已交件」→ 假回執。
提示詞**第一行就是收合指令**（預設 `/compact`）：宿主終端那條通道是 bracketed paste，
有些宿主把開頭的斜線當指令執行 → 直接收合；當成訊息送，agent 也讀得懂。

**不做的**：不新增工單狀態、不動十狀態契約（這是**引擎主動提醒**，
不是流程狀態）；不因為「session 閒著」就假裝它做了什麼。

**收合方式可選（見附則 20）**：預設 `compact`，也可設成 `reset`（清空對話、session 不變）。

**看得見**：`fbflow_status` 與 `fbf.py engine status` 都帶 compact 紀錄
（`detected`＝宿主紀錄、`requested`＝引擎要求過）。

### 20. 收合方式：**能 reset 就 reset，不行就 compact，永不 new**（2026-11-25）

**現場**：`/compact` 保留摘要脈絡，session 的 UUID 不變；
但有時候使用者要的是**上下文真的歸零**（例如那棒工作談歪了、雜訊太多，
摘要也救不回來）。

**決定**：專案層設定 `compact_mode`，只有兩個值，**預設是 `reset`**：

| 模式 | 指令 | 效果 | 代價 |
|---|---|---|---|
| `reset`（預設） | `/clear`（Claude Code） | **清空對話、session 本身不變** | 對話脈絡沒了（這就是重點） |
| `compact` | `/compact` | 壓縮成摘要，**保留脈絡** | 摘要本身還留著舊痕跡 |

**優先序（使用者裁決）**：**能 reset 就 reset，不行就 compact**——
某個 agent 沒有清空指令（OpenCode 沒有 `/clear`）→ **自動退回 compact**，
並在提示詞裡寫出這個退化，**不假裝有 `/clear`**。

**不做「開新 session」（`/new`）**：使用者裁決（2026-11-25）——
「不要 new，我怕 new 把我給個 session 設定好的模型跑掉了」。
開新 session 會用預設值重新開始，**那個 session 專屬的模型、權限、工具設定會全部不見**；
`reset`（`/clear`）清空對話但**保留 session 本身**，設定不會掉。
要換模型請另外用 session 設定／`/model`，不要靠開新 session。

**`/clear` 會不會讓 session id 變？——不會（就這套架構而言）**：
席位綁在**宿主終端 handle**（`term_…`），不是「對話 id」；
引擎認證用的專屬 UUID 也是依這個 handle 存的 → **`/clear` 不會動到它們**，
席位不會變成孤兒，也不必重新認領。
真正會換掉的是 Claude 的「對話 UUID」（引擎只拿它讀 transcript 找 compact 紀錄，
**沒有拿它綁席位**）。引擎因此**不會在 reset 時釋放席位**——
釋放席位就等於「換一個 session 接手」，那是使用者不要的那條路。

**各宿主的指令對照（照官方文件核對過）**：

- **Claude Code**：`/compact`（摘要）、`/clear`（清空對話、保留 session；
  別名 `/reset`、`/new`）。
- **OpenCode**：官方文件的常用指令裡**沒有** `/clear` → reset 模式在 OpenCode 上
  **明確退化為 `/compact`**，並在提示詞裡寫出這個退化，**不假裝有 `/clear`**。

**reset 的提示詞怎麼寫**：不能嚇唬人——**不要**寫「UUID 跟著對話一起沒了」
（那是事實錯誤，會害 agent 白跑一趟重新認領）。
正確說法是「清空不會改掉你的 session 設定，席位也還在；
若真的拿不到席位或 UUID 不認了，再重新 `fbflow_register`」。

**怎麼切換**：`fbf.py compact mode --mode reset|compact`（寫進專案正本的 settings 表）。
`fbf.py compact status` 看目前是哪一種、各宿主的指令對照、以及收合紀錄。

**不做的**：不新增 MCP 工具（這是專案層設定，不是流程動作）；
不把「換 session」做成任何一種模式（使用者明確否決）。

### 21. 「逾時」要分型：只有真的沒動靜才催；送進 session 的只有提示詞（2026-11-25）

**現場**（實測一個批次處理專案）：WORKER 跑 長時間批次工作，
被引擎判了 **15 次逾時**並重派；引擎還在 90 秒內把 4 張重派工單
一起插進它正跑的對話。

**查證後的真相**：那 15 次都不是「agent 不理人」，而是三件事疊在一起——
**模型端慢**（每一個被判逾時的當下，transcript 最後一筆都是 `api-request`，
模型請求已送出、還沒回來；`api-request → assistant` 常隔 50～134 秒）、
**模型端直接回錯**（`API Error: 422 … content safety screening` ×4）、
以及 **context 被壓縮** 5 次（compact 後要重建上下文）。

**決定**：

1. **逾時判定先看宿主側狀態**（新增 `fbf_engine/hostact.py`）：
   - `waiting_model`（在等模型）／`running_tool`（在跑工具）／
     `compacting`（剛被壓縮）→ **一律不催、不收掉 task**。
     插訊息會打斷它正在做的事；而且「在等模型」不是 agent 的錯。
   - `idle`（回合已結束、什麼都沒在動）→ **這才叫卡住**：先喚醒（催），
     逾時才收掉重派。
   - 查不到宿主紀錄 → **門檻加倍**（保守：寧可晚點催，也不要誤殺）。
   - 計時基準取「正本交件時間」與「宿主最後活動時間」的**較晚者**。
   - 模型端卡超過 3 小時 → 記 `health.model_stalled` 健康事件，
     **但不收掉 task**。
2. **送進 session 的只有提示詞，工作狀態留在磁碟上**：
   交件契約在提示詞裡只留**欄位名、型別與短規則**；
   範例、長說明與工單內容留在磁碟（`fbflow_read` 的 `contract` → `field_specs`）。
   **短規則要留**——實測 agent 就是因為沒被告知「solutions 至少 2 案」
   才只交 1 案被退。
3. **同一個 session 這一趟只送一則**：其餘的延到下一趟（outbox 不丟）。

**不做的**：不因為「等模型」就收掉 task 或改派別人（那是模型端的問題）；
不把工單正文、驗收題目、產物內容貼進 session。

**看得見**：`health.model_stalled` 進 journal（`fbf.py engine status` 看得到）。

### 附則二十二：套件裡只留「現在有人在用的東西」（2026-11-25 追加）

**問題**：一個會散布的套件，根目錄裡累積了很多「當初有用、現在沒人用」的東西。
每多一個檔，讀者就要多花一次判斷成本：**這份還權威嗎？**

**決定**：清理時**不能靠感覺**，要靠可重現的證據：

1. **CodeGraph 索引**回答「程式碼有沒有引用它」——
   符號、呼叫關係、匯入都是圖上的邊，比 grep 準。
2. **文字搜尋**補索引看不到的：JSON、shell 腳本、文件裡的反引號路徑。
3. **測試結構本身就是規格**：`tests/test_package_hygiene.py` 明定哪些目錄
   必須存在、內容必須是什麼——這比任何文件敘述都硬。
   （本例：`doc/` 與 `research/` 看起來像舊資料夾，其實是**套件強制結構**，
   刪了會讓散布出去的套件壞掉。）

**刪除的判準**：三項證據都指向「沒人用」才刪，而且**走可復原的垃圾桶**
（`mavis-trash`），不直接永久刪除——整理專案的判斷有可能錯。

**保留的判準**：只要有**任何一項**證據顯示它還承擔功能，就留。
本例 `doc/`、`research/`、`spec/` 三項全留。

**新增的守門**：`tests/test_package_hygiene.py`
- `test_codegraph_index_is_not_shipped`：本機索引（每台機器各建各的、
  含大資料庫、會過期）必須被 `.gitignore` 排除。
- `test_skipped_dirs_are_actually_skipped`：**驗行為，不只驗清單**——
  在被跳過的目錄放一個文字探針檔，確認掃描看不到它。
  （只列清單不驗行為，等於沒守：清單寫錯了也不會有人發現。）

**替代方案（不選）**：
- *只刪 `__pycache__` 這種明顯的* → 太保守，`interfaces.json` 這種
  看起來像規格、其實已過期的檔案才是真正拖累讀者的地方。
- *一次刪光所有看起來舊的* → 會誤刪 `doc/`、`research/`，
  套件散布出去就壞了——這正是「靠感覺」會踩到的坑。


### 附則二十三：公開發布保留可重現的資料（2026-11-25）

GitHub 首頁提供快速開始、宿主需求、規格入口與測試命令。發行採用 The Unlicense，授權全文見 `LICENSE`。

公開檔案保留程式、測試和去識別化的技術文件。本機交接、CodeGraph 索引、SQLite 正本、MCP 設定和憑證由 `.gitignore` 排除。以獨立暫存 Git 儲存庫的候選清單核對排除規則，避免只看忽略檔文字。
