"""FBF ENGINE — 以可恢復交付為核心的多 session 工作流引擎。

模組職責（規格第 3 節；**依賴只能往下**）：

    mcp_server → coordinator → engine → workflow → store
                                ↘ platform（派工／presence）
                                ↘ receipts（回執核對）
                                ↘ prompts（提示詞渲染）

| 模組 | 只負責 | 不知道 |
|---|---|---|
| `store` | 正本：schema、交易、租約、資料存取 | 流程、agent 怎麼連 |
| `workflow` | 狀態機、交件契約、守衛、總判 | agent 怎麼連、怎麼派工 |
| `engine` | 對照正本、套用提交、佇列派工、重試與計數 | 規則為什麼是這樣 |
| `platform` | 派工（INFORM）與 presence | 工單、狀態機 |
| `receipts` | 唯讀核對平台回執 | 流程 |
| `prompts` | 從 `workflow` 的契約渲染角色提示詞 | 規則 |
| `coordinator` | 常駐 timer 與租約 | MCP 怎麼連 |
| `mcp_server` | 對 session 提供工具、身分與角色檢查 | 直接更新狀態 |
"""
from __future__ import annotations

__version__ = "1.0.0"
__skill_name__ = "FBF_ENGINE_MCP"

__all__ = [
    "store", "workflow", "engine", "platform", "receipts",
    "prompts", "coordinator", "mcp_server",
]
