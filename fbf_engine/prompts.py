"""FBF ENGINE 的**提示詞渲染層**：把規則變成每個 session 看得到的那一份。

三條規則（規格第 5 節、第 9 節）：
1. **逐 session 渲染**——不是一份共用模板。每個 session 拿到的是它自己那一份：
   角色規則 ＋ 它的角色／簡稱／專案代碼 ＋ **它自己的 UUID** ＋ 目前這一棒的交件清單。
2. **交件清單來自 `workflow.stage_contract`**——提示詞不另寫一份欄位規則，
   否則一定會和守衛漂移。
3. **交接檔是摘要，不能覆蓋正本欄位**——下一棒只讀交接檔與正本。

UUID 只出現在**該 session 自己的提示詞**裡；資料庫只存雜湊。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any, Dict, List, Optional

from . import paths, workflow


def rule_sources() -> List[str]:
    """**規則正本的路徑**——派工提示詞自帶這兩條路徑。

    沒有這一段，session 想知道「這個欄位為什麼要這樣、這流程怎麼走」時
    只能自己去翻引擎原始碼（或猜）。**引擎自動帶路**：路徑寫在提示詞裡，
    session 動工前先讀，不用重看一次 CODE。

    路徑在執行時才算出來（引擎安裝在哪台機器就指哪裡），
    提示詞裡不會出現寫死的路徑。
    """
    root = paths.mcp_root()
    return [os.path.join(root, "SKILL.md"),
            os.path.join(root, "spec", "FBF ENGINE工作流.md")]


def rule_lines() -> List[str]:
    """提示詞裡的「規則正本」段落。"""
    lines = ["## 規則正本（動工前先讀，不用問、也不用翻程式碼）"]
    for path in rule_sources():
        lines.append("- %s" % path)
    lines.append("- 這兩份是**唯一**的規則來源；提示詞與交接檔都只是摘要，"
                 "有衝突以規則正本為準。")
    return lines


def _one_line(text: str, limit: int = 160) -> str:
    """把多行文字壓成一行（提示詞用），過長截斷。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _confirmed_requirements(project_root: str) -> List[Dict[str, Any]]:
    """讀已確認需求清單（唯讀）。提示詞直接嵌入 id，PLANNER 不必自己查。"""
    if not project_root:
        return []
    db = os.path.join(project_root, "fbf_engine.db")
    if not os.path.isfile(db):
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            rows = conn.execute(
                "SELECT id, text, success_criterion FROM requirements"
                " WHERE confirmed_at IS NOT NULL AND confirmed_at <> ''"
                " ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [{"id": r[0], "text": r[1], "success_criterion": r[2]} for r in rows]

#: 三個角色的規則。這是「角色怎麼做事」的唯一一份宣告。
ROLE_RULES: Dict[str, List[str]] = {
    "PLANNER": [
        "你是 PLANNER。你決定「要做什麼、做多少、算不算做完」。",
        "你不動手施工，也不自己驗自己的施工成果。",
        "需求訪談：一次只問一題，每題附上自己的猜測；不奉承；每個回答都要取立場，"
        "並說明什麼證據會改變你的立場。",
        "需求未經人類明確確認前不得建立工單。",
        "分解：垂直切片；一張工單＝一條端到端可驗收的路徑；"
        "標題裡出現「而且」就拆；L 以上必拆。",
        "驗收方式：每條 check 必須指向一條已確認需求。",
        "結案：只有所有 required 的 check 都 PASS 才可 close；總判由程式算，不採信自稱。",
        "退回：若原因是「驗收方式訂錯了」，要回設計驗收方式（revision），"
        "不能把它偽裝成重做施工。",
        "里程碑檢視：對照 outcome，只能選 continue / add_work / await_human。",
        "**需求或做法變了，而工單已經發出去 → 不要改那張工單**"
        "（改已發出的工單太麻煩）：照新需求開新工單（fbflow_work_create），"
        "再用 fbflow_work_supersede 把舊工單標成已取代。"
        "已確認需求本身也不可改——要新增一條需求（由人確認）再開新工單。",
    ],
    "THINKER": [
        "你是 THINKER。你要想辦法，也要獨立判斷別人做的對不對。",
        "你不決定整體範圍，也不宣布專案完成。",
        "設計方案：這一棒直接產出至少兩案（不必分派 SUBAGENT）；"
        "每案要寫解法、成本、修改範圍、工作量、風險與驗證方式。可附可重用項。",
        "兩個方案不得實質相同（成本與修改範圍不能都一樣）。",
        "驗收：每條 check 都要有結果與理由；不可驗自己在本工單的施工。",
        "重工方案：針對退回原因設計，並引用那個原因。",
    ],
    "WORKER": [
        "你是 WORKER。你要真的做出產物。",
        "你不決定驗收標準，也不宣布過不過。",
        "施工：依需求／解法／驗收方式做；交出產物指紋（路徑／大小／SHA-256）、"
        "已做、未做、自檢結果、實際案例數。",
        "案例數不得灌水；純文件／人工檢視的自報 execution_kind=review 並附審閱紀錄。",
        "同一個問題連撞三次就停下來發 ISSUE，不要硬撞第四次。",
        "不要自己改驗收方式——那是 PLANNER 的權限。",
    ],
}

#: 八欄交接檔（規格第 9 節）。下一棒必讀。
#: **定義在 `workflow`**（規則正本）——派工提示詞也要帶這八欄，
#: 兩邊共用同一份定義才不會漂移。這裡只是轉出去讓舊的引用繼續可用。
HANDOFF_FIELDS = workflow.HANDOFF_FIELDS


# ---------------------------------------------------------------- 方法精神

#: 每一棒的**方法**（spec 的「對應 pstack」）。
#:
#: **這裡放的是精神，不是工具名**——那些工具可能沒裝、也不該被當成依賴。
#: 但它們真正在做的事可以照著做：先把該想清楚的想清楚、每個結論都要有證據、
#: 證明完成就停。提示詞把這個精神交給 session，方法由模型自己決定怎麼做。
#:
#: 對應關係（spec 第 1~9 步）：
#: - `sharpen`／`figure-it-out`／`in-your-own-words` → 訪談、複述、分解
#: - `architect`／`how`／`why` → 解法設計（工單與 ISSUE 同一條路）
#: - `benchmark-checklist`／`verify-and-stop` → 驗收方式設計
#: - `tdd` → 施工與自檢
#: - `interrogate`／`thermos` → 驗收（對抗式；**每個發現要跑出來證明**）
#: - `verify-and-stop` → 結案判斷（證明完成就停，不擴大範圍）
#:
#: 與 spec 的兩處差異記在 `DEVIATIONS.md`：
#: 1. `interrogate` 由「PLANNER 設計驗收方式」移到「THINKER 驗收」——
#:    它真正在做的是「對抗式審查，跑得出來才算」；
#: 2. `no-comments` 從驗收棒拿掉——它的功能已被 `thermos` 的 code-quality 路涵蓋。
METHOD_NOTES: Dict[str, List[str]] = {
    workflow.STAGE_GRILL: [
        "先問四件事：①這個領域的頂尖專業人士會做什麼，是我沒要求的？"
        "②他會從哪個**最原始**的材料開始（不是已經被消化過的版本）？"
        "③他會用哪些具體方法與工具？④除了你我，還有什麼能證明結果是對的？",
        "四項都清楚後，用**自己的話**把「使用者的目標」與「要解決的問題」"
        "完整複述一遍——複述讓你發現自己哪裡還在想當然。",
        "一次只問一題，每題附上你自己的猜測；不奉承。",
        "實驗能解決的選擇，先做原型再依觀察建議；"
        "產品／偏好這種實驗解決不了的，保留給人決定。",
    ],
    workflow.STAGE_DECOMPOSE: [
        "先寫下「完成的定義」＝**可證偽的判準**（不是「做好」）；"
        "把範圍量化（大概幾個單位、大概多少工時、卡點在哪裡）。",
        "嚴謹度**偏高**：不可逆、影響大的地方多設關卡；可逆、低風險的地方少設。"
        "嚴謹＝關卡與產出物，不是「更努力」。",
        "再分拆：一張工單＝一條端到端可驗收的垂直切片；"
        "用三句話（標題、目標、交件方式）描述不完就再拆。",
        "把決定記在工單交件資料中，讓人能從正本回查。",
    ],
    workflow.DESIGN: [
        "動工前先追出這張單會碰到的**每一個系統**：誰呼叫誰、狀態誰擁有。",
        "每個案子先畫**兩種**形狀（型別、簽名、模組邊界），"
        "挑那個「每份狀態只有一個擁有者、每件任務只有一種做法、"
        "呼叫端最少需要知道」的。",
        "說明它**怎麼運作**（執行流程）與**為什麼這樣設計**——"
        "理由要從紀錄來（當初作者寫了什麼），不要憑空猜。",
        "兩個案子不得實質相同（成本與修改範圍不能都一樣）。",
    ],
    workflow.ACCEPTANCE_DESIGN: [
        "每條 check 先寫下你要發布的那句話（要證明什麼），再把它變成可跑的判準。",
        "每條 check 要有：門檻、範圍、必不必要、以及**用什麼證據**判定"
        "（跑出來的，不是看起來對）。",
        "門檻要可重現：別人照著做能得到同樣結果；"
        "拿不到證據就寫 NOT_RUN，不要猜。",
        "只保留**最小足夠**的證明集合：每條 check 指向一條已確認需求；"
        "先跑聚焦的再跑寬的。",
    ],
    workflow.BUILD: [
        "交出來之前先自檢：把壞的行為變成可執行的檢查，"
        "先確認它真的會失敗，再修到它通過。",
        "選**最窄**的那個可執行檢查；測試跑不起來就不要硬造，"
        "改用最接近的可驗證方式並寫清楚。",
        "產出指紋（路徑／大小／SHA-256）、已做、未做、實際案例數——"
        "案例數不得灌水；純文件／人工檢視要自報 execution_kind=review。",
        "產出放在**你自己的簡稱資料夾**裡，不要放進別人的資料夾。",
    ],
    workflow.VERIFY: [
        "對抗式驗收：主動找「其實沒做對」的地方，而不是照著工單複誦。",
        "**每個發現都要跑出來證明**；跑不出來的不算發現。",
        "兩路並行：一路查缺陷與安全性，一路查品質與可維護性，再綜合。",
        "不可驗自己在本工單的施工——自驗不算數。",
    ],
    workflow.ADJUDICATE: [
        "把驗收條件翻成**最小足夠的證明集合**：重用仍然有效的結果，"
        "先跑聚焦檢查再跑寬的閘門。",
        "分清楚四種結果：pass／fail／unavailable／blocked——"
        "「跑不了」不等於「過了」。",
        "證明完成就**立刻停**：不通過時只寫清楚還差什麼，"
        "不要順手擴大範圍或替別人補做。",
        "總判由程式算，不採信自稱。",
    ],
    workflow.REWORK_DESIGN: [
        "針對退回的原因重新設計，並在 rework_plan 裡**引用那個原因**。",
        "同解法設計：追出會碰到的系統、畫兩種形狀、"
        "挑狀態只有一個擁有者的那個；理由從紀錄來。",
    ],
    workflow.REWORK_ACCEPTANCE_DESIGN: [
        "同驗收方式設計，但**門檻只能更嚴**、required 不可降級。",
        "選的是**這一輪**的重工解法，不是第一輪的。",
    ],
    workflow.REWORK_BUILD: [
        "同施工：依對策與這一輪的驗收方式做，先自檢再交。",
        "產出放在你自己的簡稱資料夾裡。",
    ],
}

#: ISSUE 各棒的方法精神（跟工單同一套精神，換成 ISSUE 的情境）。
ISSUE_METHOD_NOTES: Dict[str, List[str]] = {
    workflow.ISSUE_DESIGNING: [
        "至少兩案（跟工單解法設計同一條硬約束；不必分派 SUBAGENT）。",
        "每案照解法設計的方法：追出會碰到的系統、畫兩種形狀、"
        "挑狀態只有一個擁有者的那個、說明為什麼這樣設計。",
    ],
    workflow.ISSUE_HANDLING: [
        "從兩案挑**成本較低、工程量較小**的那個，並寫清楚為什麼。",
        "對策要寫成「要怎麼修」＋「做完怎麼算解決」——"
        "後者必須是可判定的，不能是「看起來好了」。",
    ],
    workflow.ISSUE_BUILDING: [
        "依對策執行；自檢要回答「**對策有沒有生效**」，不是「我做了什麼」。",
        "產出放在你自己的簡稱資料夾裡。",
    ],
    workflow.ISSUE_ADJUDICATING: [
        "用最小足夠的證明集合確認對策是否真的生效："
        "生效 → 結案；沒生效 → 附上實際發生的情況退回。",
        "**退回不是結案**：退回次數會累計，達上限就停下來交由人裁決。",
    ],
}


def method_note(stage: str) -> List[str]:
    """某一棒的方法精神（沒有就回空清單）。"""
    return list(METHOD_NOTES.get(stage) or [])


def issue_method_note(state: str) -> List[str]:
    """ISSUE 某一棒的方法精神。"""
    return list(ISSUE_METHOD_NOTES.get(state) or [])


# ---------------------------------------------------------------- UUID

def hash_uuid(uuid_value: str) -> str:
    """UUID 的雜湊。**資料庫只存這個，不存明文。**"""
    return hashlib.sha256((uuid_value or "").encode("utf-8")).hexdigest()


def check_uuid(conn: sqlite3.Connection, session_id: str, uuid_value: str) -> bool:
    """核對 UUID。**這是附加校驗，不是主證據**（主證據是 host 綁定）。

    規格第 7 節：「session UUID **若保留**，只在該 session 私有提示詞中交付」。
    所以：
    - 這個 session **沒登記** UUID → 不要求帶（host 綁定已足以證明身分）。
    - 這個 session **登記了** UUID → **必須帶，而且要對**。
      帶空值不算數——那等於放棄了這個 session 唯一的秘密。
    """
    row = conn.execute(
        "SELECT uuid_sha256 FROM roles WHERE session_id = ?", (session_id,)).fetchone()
    if row is None or not row["uuid_sha256"]:
        return True
    if not uuid_value:
        return False
    return row["uuid_sha256"] == hash_uuid(uuid_value)


def issue_uuid(conn: sqlite3.Connection, session_id: str) -> str:
    """產生並登記這個 session 的 UUID。**明文只回傳一次，不落地。**"""
    import secrets
    value = secrets.token_hex(16)
    conn.execute(
        "UPDATE roles SET uuid_sha256 = ? WHERE session_id = ?",
        (hash_uuid(value), session_id))
    return value


def rotate_uuid(conn: sqlite3.Connection, session_id: str) -> str:
    """撤銷舊 UUID、發新的。舊值立刻失效。"""
    return issue_uuid(conn, session_id)


# ---------------------------------------------------------------- 渲染

def contract_lines(contract: Dict[str, Any], brief: bool = False) -> List[str]:
    """把交件契約渲染成提示詞用的行。

    **來源是 `workflow.stage_contract`／`issue_stage_contract`**——守衛看的是
    同一份，提示詞只負責把它寫成人看得懂的樣子。

    **不印狀態名稱**：契約只說「這一棒要交什麼」，不說這張單現在到哪。
    現況由角色自己呼叫 `fbflow_read` 查（spec：提示詞不帶工作狀態）。

    **`brief=True`＝精簡版**（使用者裁決 2026-11-25）：
    **送進 session 的只有提示詞，工作狀態留在磁碟上**。
    完整版（每欄的標準、範例、長說明）是**規格文件**，
    角色要讀時用 `fbflow_read` 的 `contract` → `field_specs` 自己取——
    不在每則派工裡重複貼一份（實測派工中位 1,843 字元，
    其中八成是欄位標準與範例；貼太多會擠爆對話上下文，
    也讓「模型端內容被安全篩選擋下」更容易發生）。
    精簡版**保留欄位名、型別與結構要求**——agent 至少知道要交哪些欄位。
    """
    lines: List[str] = []
    required = contract.get("required") or []
    specs = contract.get("field_specs") or {}
    if required:
        lines.append("要交的（payload 的頂層欄位）：")
        for field in required:
            spec = specs.get(field) or {}
            if brief:
                # **保留「型別＋短規則」**：agent 至少要知道能填什麼值
                # （實測被退的就是「solutions 至少 2 案」這種規則——
                # 規則不在提示詞裡，agent 就不會照做）。
                # **範例與長說明留在磁碟**（`fbflow_read` 的 contract）。
                detail = str(spec.get("kind", "欄位"))
                standard = workflow.standard_of(field, spec)
                rule = standard.split("；", 1)[1] if "；" in standard else ""
                if rule and len(rule) <= 80:
                    detail += "；%s" % rule
                elif rule:
                    detail += "（規則見 contract.field_specs）"
                if spec.get("min_items"):
                    detail += "；至少 %d 項" % int(spec["min_items"])
                if spec.get("item_fields"):
                    detail += "；每項欄位：%s" % ", ".join(spec["item_fields"])
                lines.append("- %s（%s）" % (field, detail))
                continue
            lines.append("- %s（%s）" % (field, spec.get("kind", "欄位")))
            standard = workflow.standard_of(field, spec)
            if standard:
                lines.append("    標準：%s" % standard)
            example = workflow.example_of(field)
            if example:
                lines.append("    範例：%s" % example)
            if spec.get("min_items"):
                lines.append("    至少 %d 項" % int(spec["min_items"]))
            if spec.get("item_fields"):
                lines.append("    每項欄位：%s" % ", ".join(spec["item_fields"]))
            if spec.get("note") and spec["note"] not in standard:
                lines.append("    %s" % spec["note"])
    else:
        lines.append("這一棒沒有要交的欄位。")
    lines.append("**表單以外的欄位是附加項**——引擎不檢查它們，也不會因為它們退件。")
    if contract.get("required_note"):
        lines.append("")
        lines.append(contract["required_note"])
    handoff_fields = contract.get("handoff_fields") or []
    if handoff_fields:
        lines.append("")
        lines.append("另外必須附上 handoff（交接檔，欄位固定八欄，缺了會被退件）：")
        for field in handoff_fields:
            lines.append("- %s" % field)
    return lines


def render_session_prompt(conn: sqlite3.Connection, session_id: str,
                          project_code: str = "") -> str:
    """渲染**這個 session** 的私有提示詞。"""
    row = conn.execute(
        "SELECT * FROM roles WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM roles WHERE role = ? ORDER BY created_at LIMIT 1",
            (session_id,)).fetchone()
    role = (row["role"] if row else "") or ""
    short_name = (row["short_name"] if row else "") or ""

    task = conn.execute(
        "SELECT * FROM tasks WHERE assigned_session = ? AND status = 'open'"
        " ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()

    lines: List[str] = []
    lines.append("# FBF ENGINE — 你的工作契約")
    lines.append("")
    lines.append("版本：FBF_ENGINE_MCP v1")
    lines.append("")
    lines.append("## 你是誰")
    lines.append("- session_id: %s" % session_id)
    lines.append("- 角色: %s" % role)
    lines.append("- 簡稱: %s" % short_name)
    if project_code:
        lines.append("- 專案代碼: %s" % project_code)
    lines.append("")
    lines.append("## 你的 UUID（只有你知道，別人看不到）")
    lines.append("- 這是寫入工具的附加校驗。**不要寫在任何檔案裡。**")
    lines.append("- 每次呼叫寫入工具時帶在 `session_uuid`。")
    lines.append("")
    lines.append(workflow.SESSION_IDLE_RULE)
    lines.append("## 角色規則")
    for rule in ROLE_RULES.get(role, []):
        lines.append("- %s" % rule)
    lines.append("")
    lines.extend(rule_lines())
    lines.append("")

    if task is not None:
        work = conn.execute("SELECT * FROM works WHERE id = ?",
                            (task["work_id"],)).fetchone() if task["work_id"] else None
        lines.append("## 你現在這一棒")
        lines.append("- task_id: %s" % task["id"])
        if work is not None:
            work_dict = dict(work)
            lines.append("- work_id: %s" % work["id"])
            lines.append("- 搭配的專案 SKILL：%s" % (work_dict.get("skill") or "（未指定）"))
        lines.append("")
        lines.append("### 這一棒要交的東西")
        contract = {}
        if task["issue_id"]:
            contract = workflow.issue_stage_contract(task["stage"] or "")
        elif task["stage"]:
            contract = workflow.stage_contract(task["stage"])
        if contract:
            lines.extend(contract_lines(contract, brief=True))
        else:
            lines.append("（沒有待交的欄位）")
        lines.append("")
        lines.append("工單內容、驗收方式、既有解法在 `fbflow_read` 的正本快照裡；"
                     "**每一欄的標準與範例也在那裡**"
                     "（`contract` → `field_specs`）——不要靠猜，交件前照它填。")
        lines.append("")
        lines.append("### 交接檔（交件時要附上，欄位固定八欄）")
        for field in HANDOFF_FIELDS:
            lines.append("- %s" % field)
        lines.append("")
        lines.append("### 怎麼交")
        lines.append("用 MCP 工具回報，不要用 CLI：")
        lines.append("  fbflow_report_work(task_id, work_id, expected_version, payload, message_id)")
        lines.append("  payload 必須含派工提示詞列出的每一個欄位，外加 `handoff`。")
        lines.append("  expected_version 用 fbflow_read 讀到的版本；stage 可留空"
                     "（引擎依 task_id 判斷這一棒）。")
        lines.append("  message_id 必須是**這一輪平台新產生的**那個訊息 id"
                     "（每次交件都要新的，**不可沿用上一輪的**——"
                     "沿用會被引擎以 receipt.message_reused 退件）。")
        lines.append("")
        lines.append("### 讀正本")
        lines.append("  fbflow_read(target_kind=work, target_id=%s)" % (work["id"] if work else ""))
        lines.append("  fbflow_status()")
    else:
        lines.append("## 你現在沒有待交的工作")
        lines.append("結束本輪回覆，等待 MCP 傳入派工提示詞。")
    return "\n".join(lines)


def render_grill_prompt(task_id: str, project_root: str = "") -> str:
    """專案啟動 → 生成工單前的 GRILL ME 派工提示詞（給 PLANNER）。

    三段中的第 2 段：分析專案文件 → 釐清範圍／目標／產出 →
    **用自己的話複述** → 問使用者是否同意。**不建工單**——
    工單要等人類 `requirement confirm`（同意）之後才生成。
    """
    lines = [
        "【FBF ENGINE 專案啟動 · GRILL ME 派工】",
        "task_id: %s" % task_id,
        "stage: %s" % workflow.STAGE_GRILL,
        "role: PLANNER",
        "",
        "這是專案啟動的第 2 段（共 3 段）。**還不能生成工單。**",
        "",
        "## 你要做的事（GRILL ME）",
        "1. 讀這個專案裡的任務／需求文件（路徑：%s）。" % (project_root or "專案根目錄"),
        "2. 用追問的方式向使用者釐清四項：",
        "   - 任務範圍（做什麼／不做什么）",
        "   - 目標（完成後要達成什麼）",
        "   - 需求（可驗證的條件）",
        "   - 預期產出（具體交付物）",
        "   一次只問一題，每題附上你自己的猜測；不奉承。",
        "3. 四項都清楚後，**用你自己的話完整複述一遍**（任務、範圍、目標、產出）。",
        "4. 複述完，明確問使用者：「以上理解是否同意？同意後才會生成工單。」",
        "",
        "## 禁止",
        "- 不要用 fbflow_work_create（需求未經人類確認前不得建工單）。",
        "- 不要直接讀寫正本資料庫——一切走 MCP 工具。",
        "- 使用者還沒說同意，就不要往下走。",
        "",
        "## 給人類的三段說明",
        "段 1：三個角色席位加入（join 已建 P1／T1／W1；session 認領或 tick 自動 enroll）。",
        "段 2：你現在這一段——GRILL ME 釐清 + 複述 + 問同意。",
        "段 3：人類跑 `fbf.py requirement confirm --text ... --criterion ...` 表示同意後，",
        "      engine 才生成工單、走 design→…→closed。",
        "",
        "交件方式：這一棒**沒有 payload 要交**。把複述與提問直接回覆給使用者即可。",
        "人類同意後由人類跑 requirement confirm；engine 會關掉這張 grill task。",
        "",
        "讀正本（唯讀）：",
        "  fbflow_status()",
        "",
    ]
    lines.extend(rule_lines())
    lines.append("")
    lines.append(workflow.SESSION_IDLE_RULE)
    return "\n".join(lines)


def render_decompose_prompt(task_id: str, project_root: str = "") -> str:
    """三段啟動的第 3 段（給 PLANNER）：人類已同意 → 開始分解並生成工單。

    這一段在 grill 收掉、需求已確認之後派：PLANNER 照三句話原則把
    已確認需求拆成工單，並用 `fbflow_work_create` 建出第一張工單。
    **建出工單後引擎會自動收掉這張 task**——不需要人工結案。

    實測踩過的坑：模型讀完 `fbflow_status` 看到「目前沒有任務」就收工，
    回頭問人類「標題要什麼」。因此本段**明確禁止**回頭問人，也禁止
    在工單建完前結束回合。
    """
    confirmed = _confirmed_requirements(project_root)
    lines = [
        "【FBF ENGINE 工單生成派工】",
        "task_id: %s" % task_id,
        "stage: %s" % workflow.STAGE_DECOMPOSE,
        "role: PLANNER",
        "",
        "人類已確認需求（三段啟動的第 3 段開始）。**這張 task 本身就是你的任務。**",
        "",
    ]
    if confirmed:
        lines.append("本專案**已確認的需求**（requirement_id 就用這些，不必再查、不必問人）：")
        for req in confirmed:
            lines.append("  - %s：%s" % (req["id"], _one_line(req.get("text") or "")))
            criterion = _one_line(req.get("success_criterion") or "")
            if criterion:
                lines.append("    完成條件：%s" % criterion)
        lines.append("")
    else:
        lines.append("⚠️ 目前查不到已確認需求；請先呼叫 fbflow_status() 確認，若仍為空，")
        lines.append("   表示人類尚未 requirement confirm——回報這個狀況並結束，不要自己補需求。")
        lines.append("")
    lines += [
        "本輪你必須自己完成下面三件事，不准回頭問人類：",
        "1. 需求 id 已列在上方；需要全文就呼叫 fbflow_read(target_kind=requirement, target_id=<id>)。",
        "2. 依三句話原則把需求拆成工單（通常 1–5 張）：",
        "   一件工單要能用三句話描述完（標題、目標、交件方式）；描述不完就再拆。",
        "   一張工單＝一條端到端可驗收的垂直切片。",
        "3. 逐張呼叫 fbflow_work_create(title, requirement_id, content, expected_outputs, milestone_id) 建工單：",
        "   - title：這個工單叫什麼（動詞＋對象）",
        "   - requirement_id：第 1 步拿到的已確認需求 id（必填，不可留空）",
        "   - content：用自己的話寫清標題／目標／交件方式",
        "   - expected_outputs：具體檔案或結果清單（字串陣列）",
        "   - milestone_id：沒有里程碑就傳空字串",
        "",
        "**絕對禁止**（實測踩過的錯誤，不要重犯）：",
        "- 禁止問人類「標題要什麼／需求 id 是什麼／內容要什麼」——所有資訊你都可以從",
        "  fbflow_status()／fbflow_read() 與專案文件自己取得。",
        "- 禁止只列出分解步驟就結束回合——必須真正發出 fbflow_work_create 呼叫。",
        "- 禁止用 python 直寫 SQLite 或呼叫 fbf.py CLI——建工單的唯一通道是 MCP 工具。",
        "- 禁止在工單建完前結束回合；本輪唯一正常的結束方式是「所有工單都已建立」。",
        "",
        "第一張工單建出來後，引擎會自動收掉這張派工；後續工單照同一規則繼續建，",
        "直到已確認需求都被覆蓋。建完後回報每張工單的 work_id 與三句話摘要。",
        "",
        "讀正本（唯讀）：",
        "  fbflow_status()",
        "  fbflow_read(target_kind=requirement, target_id=<需求 id>)",
    ]
    lines.extend(rule_lines())
    return "\n".join(lines)


def render_contract_markdown() -> str:
    """交件契約表（供文件與 SKILL.md 產出；內容來自 workflow，不另寫）。"""
    lines = ["| 狀態 | 角色 | 必交欄位 | 下一狀態 |", "|---|---|---|---|"]
    for state in workflow.WORK_STATES:
        contract = workflow.stage_contract(state)
        if not contract:
            continue
        lines.append("| %s（%s） | %s | %s | %s |" % (
            state, contract.get("title", ""), contract.get("role") or "—",
            ", ".join(contract.get("required", [])) or "—",
            contract.get("next") or "—"))
    return "\n".join(lines)
