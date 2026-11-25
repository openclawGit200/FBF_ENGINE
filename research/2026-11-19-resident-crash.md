  # 2026-11-19 — resident 壓測崩潰（SIGSEGV）觀察

> 這是**證據與觀察**，不是決策。決策寫在 `docs/decisions.md`（DEC-002／DEC-003）。

## 現象

在 macOS 上用真正的 `python -m fbf_engine.mcp_server` 走 stdio，瘋狂送
`tools/call fbflow_status`，每次跑 75 秒：

| 組 | 崩潰？ | exit code | 工具呼叫數 |
|---|---|---|---|
| 關（`--no-resident`）×3 | 沒崩 | 0 | 1,052,676 / 947,339 / 1,020,369 |
| 開（跟 OpenCode 實際啟動一樣）×3 | **3/3 全崩** | **-11 = SIGSEGV** | 260,084 / 0 / 0 |

**兩次連第一次工具呼叫都沒回**。SIGSEGV 時 Python 完全沒有 traceback（原生層炸的）。
其中一次是較輕的變形：SQLite 回 `file is not a database`，被沒有防護的行往上拋。

## 唯一的差異

兩個組別唯一不同的是**背景 tick 執行緒在不在**。對照組跑一百多萬次呼叫零崩潰。

## 已排除的假設

- **不是 `-wal`／`-shm` 殘跡**：四種破壞狀態（wal 截到 0、只剩 8 bytes、
  頭 4 bytes 灌亂數、整段歸零）——長生命連線與新連線全部 OK。
  SQLite 會自己重建 wal-index。先前看到的 0-byte `-wal` + 32KB `-shm` 是症狀不是病因。
- **不是磁碟／檔案損毀**：全程 `PRAGMA integrity_check = ok`。
  正本完好（需求 R-001、三個角色席位都在）。

## Windows 端的觀察（本機重測）

在 Windows 上用等價的壓測（`tools/call fbflow_status` 連打）：

| 組 | 結果 |
|---|---|
| 關 resident，20 秒 | 554,965 次呼叫，零崩潰 |
| 開 resident，30 秒（修正前） | 1,102,374 次呼叫，零崩潰 |
| 開 resident，30 秒（修正後） | 995,002 次呼叫，零崩潰 |

**Windows 上重現不出來**——所以這個修正**不是**靠本機重現驗證的，
而是靠「共用一條 `NOMUTEX` 連線是未定義行為」這個結構性事實加上上面的 A/B 因果。

## 推論

CPython 用 `SQLITE_OPEN_NOMUTEX` 開連線：底層**不再替這一條連線序列化**。
原本的設計（DEC-001）讓背景 tick 與 MCP 請求**共用同一條連線**，
用一把可重入鎖保證「不交錯」——但鎖只能保證不交錯，保證不了共用安全。
兩個執行緒仍可能在底層層級交錯（游標、statement cache、page cache），
在 macOS 的 SQLite 上就是 SIGSEGV，輕一點是「正本被寫壞」。

## 修正

見 DEC-002（每個執行緒一條連線）與 DEC-003（正本損毀的防護）。

## 還沒做的事

- **在 macOS 上重跑 `repro_race.py` 確認修正有效**（開 resident 3 次 + 關 resident 對照）。
  這是唯一能確認「這個 SIGSEGV 消失」的地方——本機 Windows 重現不出來。
- 重現腳本位置（Mac）：`/private/var/folders/t2/…/T/opencode/repro_race.py`
  （`RESIDENT=0` 是對照組）與同目錄的 `repro_wal_variants.py`。
- **網路檔案系統的 WAL 風險**：SQLite 的 WAL 需要共享記憶體，在 SMB／NFS／DFS 上
  不受支援（可能直接寫壞正本）。Windows 端若把專案放在網路掛載磁碟上就有這個風險。
  這是**另一個**問題，尚未處理——目前只在 README 標示限制。
