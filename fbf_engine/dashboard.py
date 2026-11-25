"""**後台網頁（唯讀儀表板）**：即時看每張工單現在是誰在寫、哪個欄位改成什麼。

**這是純讀的旁路**：不寫正本、不拿租約、不打擾任何 session。
它只是**讀** SQLite 正本，把「正在進行中的樣子」畫出來。

**為什麼要有**（live 實測 2026-11-24）：引擎在跑、單子卻看起來停住，
人只能去猜——是沒派工？角色沒回？還是退件了？
「誰卡在哪一棒、剛剛哪個欄位被改成什麼」本來只存在引擎的記憶體裡，
人看不到 → 只好靠讀終端畫面猜。這一頁把那件事變成一眼可見。

**「誰在寫」怎麼判定**（不猜、不問宿主）：
- 有**開著的 task** → 這一棒派給誰，誰就是正在寫的人（`tasks.assigned_role`）。
- 席位是空的（`roles.session_id` 為空）→ 顯示「無人認領」，不是假裝有人在寫。

**「哪個欄位改成什麼」怎麼來**（純讀衍生，**不改引擎**）：
- `submissions.payload` 是角色交件的**原始內容**；
- `decisions.detail.gap_details` 是退件時引擎算出的**逐欄位差異**；
- 兩者合併 → 一條時間軸：「W-003／selection.reason：舊值 → 新值」。
- 欄位層級的標準與範例**沿用 `workflow.FIELD_STANDARDS`**（同一份規則正本，
  不另寫一份解釋）。

**HTTP 部分**：純標準函式庫（`http.server`），不引入第三方相依。
- `GET /` → 頁面（HTML/CSS/JS 內嵌，單一檔案、無外部資源）。
- `GET /api/state` → JSON 快照（給前端輪詢）。
- `GET /api/stream` → Server-Sent Events（即時推播，瀏覽器端不用輪詢）。

**預設只綁 127.0.0.1**——這一頁含工單內容與席位資訊，
不是公開頁面。要讓區網看得到，必須明確用 `--host 0.0.0.0` 指定。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from . import store, workflow

#: 預設監聽位址——**只給本機**（這一頁有工單內容與席位資訊）。
DEFAULT_HOST = "127.0.0.1"

#: 預設埠。
DEFAULT_PORT = 8788

#: SSE 心跳間隔（秒）——沒有資料也要送一次，否則代理會斷線。
SSE_HEARTBEAT_SECONDS = 15.0

#: 一次推播的快照間隔（秒）。
SNAPSHOT_INTERVAL_SECONDS = 2.0

#: 快照最多帶幾筆欄位變更（時間軸不用無限長）。
MAX_CHANGES = 60


# ---------------------------------------------------------------- 衍生

def _short(value: Any, limit: int = 160) -> str:
    """把一個值壓成適合顯示的一行。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def _numeric_suffix(text: Any) -> int:
    """從 `S-000031` 這種識別碼取出數字尾碼（取不到就是 0）。"""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return int(digits) if digits else 0


def _payload_fields(payload: Any) -> Dict[str, str]:
    """把交件 payload 摊平成「欄位 → 值」。

    **只取頂層欄位**（以及物件型欄位的第一層子鍵）：
    巢狀再深就沒有人看得懂了，而且會讓時間軸炸成雜訊。
    """
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in payload.items():
        if key in ("handoff", "session_uuid"):
            continue
        if isinstance(value, dict):
            for sub, sub_value in value.items():
                out["%s.%s" % (key, sub)] = _short(sub_value)
        else:
            out[key] = _short(value)
    return out


def _stage_title(stage: str) -> str:
    spec = workflow.WORK_STATES.get(stage) or workflow.ISSUE_STATES.get(stage) or {}
    return spec.get("title") or stage or ""


def _field_standard(field: str) -> str:
    """欄位的標準——**沿用 `workflow.FIELD_STANDARDS`**（規則正本一份）。"""
    name = field.split(".")[-1]
    known = workflow.FIELD_STANDARDS.get(name) or {}
    parts = [p for p in (known.get("type"), known.get("rule")) if p]
    return "；".join(parts)


def collect_changes(conn: sqlite3.Connection, limit: int = MAX_CHANGES) -> List[Dict[str, Any]]:
    """**欄位變更時間軸**（純讀衍生）。

    兩種來源合併成同一條時間軸：

    1. **退件的逐欄位差異**（`decisions.detail.gap_details`）：
       引擎算出「哪個欄位不標準、標準是什麼、範例長什麼樣」→ 直接帶上。
    2. **成功套用的交件**（`submissions.payload`）：
       角色交了什麼 → 欄位值變成什麼。

    **這不是權威的變更紀錄**（那是引擎的職責）；它是「人看得懂的樣子」，
    來源都是正本裡既有的事實，沒有臆測。
    """
    events: List[Dict[str, Any]] = []

    # 1) 退件：引擎算出的逐欄位差異
    rows = conn.execute(
        "SELECT d.id, d.target_id, d.submission_id, d.from_state, d.gaps, d.detail,"
        "       d.created_at, s.session_id, s.stage, s.work_id, s.issue_id"
        " FROM decisions d LEFT JOIN submissions s ON s.id = d.submission_id"
        " WHERE d.action = 'rejected' ORDER BY d.id DESC LIMIT ?", (limit,)).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        for item in detail.get("gap_details") or []:
            if not isinstance(item, dict):
                continue
            field = item.get("field") or ""
            events.append({
                "at": row["created_at"],
                "target_id": row["target_id"] or "",
                "kind": "rejected",
                "field": field,
                "problem": item.get("problem") or "",
                "standard": item.get("standard") or _field_standard(field),
                "example": item.get("example") or "",
                "stage": row["stage"] or "",
                "session_id": row["session_id"] or "",
                "seq": row["id"],
                # **排序鍵一律是整數**：`decisions.id` 是整數、`submissions.id`
                # 是 `S-000031` 這種字串，混著排序會在 `int()` 上炸掉。
                "order": int(row["id"]),
            })

    # 2) 套用成功的交件：欄位值變成什麼
    # **取比輸出多一些的行**：要算「舊值 → 新值」得看見前一筆，
    # 只取最後 N 筆的話，最早那筆的前值會不見。
    rows = conn.execute(
        "SELECT s.id, s.work_id, s.issue_id, s.stage, s.payload, s.session_id,"
        "       s.created_at FROM submissions s"
        " WHERE s.status = 'applied' ORDER BY s.id DESC LIMIT ?", (limit * 3,)).fetchall()
    # **同一個欄位前後兩次的值** → 「舊值 → 新值」。
    # 純讀衍生：把同一張單、同一個欄位的歷次值排出來，後一次與前一次不同就是一次變更。
    seen_value: Dict[str, str] = {}
    for row in reversed(rows):        # 由舊到新
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        fields = _payload_fields(payload)
        if not fields:
            continue
        target = row["work_id"] or row["issue_id"] or ""
        diff: Dict[str, str] = {}
        for key, value in fields.items():
            marker = "%s::%s" % (target, key)
            old = seen_value.get(marker)
            if old is not None and old != value:
                diff[key] = {"old": old, "new": value}
            seen_value[marker] = value
        events.append({
            "at": row["created_at"],
            "target_id": target,
            "kind": "applied",
            "field": "、".join(sorted(fields.keys())),
            "problem": "交件已套用",
            "standard": "",
            "example": "",
            "values": fields,
            "diffs": diff,
            "stage": row["stage"] or "",
            "session_id": row["session_id"] or "",
            "seq": row["id"],
            # 與退件事件同樣的排序鍵（見上）——`S-000031` 的數字尾碼。
            "order": 1000000 + _numeric_suffix(row["id"]),
        })

    events.sort(key=lambda e: (str(e.get("at") or ""), int(e.get("order") or 0)))
    return events[-limit:]


def collect_improvements(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """**引擎改善建議**（結案復盤的產出）。

    **唯讀**：只讀 `engine_improvements` 表。
    """
    try:
        rows = conn.execute(
            "SELECT id, review_id, target, suggestion, reason, status, created_at"
            " FROM engine_improvements ORDER BY id DESC LIMIT 50").fetchall()
    except sqlite3.DatabaseError:
        return []
    return [dict(r) for r in rows]


def collect_rejections(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """**退件 LOG**（每一次退件都留的那一筆）。

    含：退的是誰的件、退在哪一棒、為了哪個欄位退、退件原因。
    """
    try:
        rows = conn.execute(
            "SELECT id, target_kind, target_id, submission_id, stage, session_id,"
            " role, short_name, gaps, fields, reason, created_at"
            " FROM rejection_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [dict(r) for r in rows]


def collect_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """整頁快照：工單 × 誰在寫 × 欄位變更。"""
    roles = conn.execute(
        "SELECT role, short_name, session_id, status FROM roles ORDER BY role, short_name"
    ).fetchall()
    seat_by_session = {}
    for row in roles:
        if row["session_id"]:
            seat_by_session[row["session_id"]] = {
                "role": row["role"], "short_name": row["short_name"]}

    # 開著的 task = 這一棒正在誰手上
    open_tasks = conn.execute(
        "SELECT id, work_id, issue_id, stage, assigned_role, assigned_session, created_at"
        " FROM tasks WHERE status = 'open' ORDER BY id").fetchall()
    active_by_target: Dict[str, Any] = {}
    for row in open_tasks:
        target = row["work_id"] or row["issue_id"] or ""
        if not target:
            continue
        seat = seat_by_session.get(row["assigned_session"] or "")
        active_by_target[target] = {
            "task_id": row["id"],
            "stage": row["stage"] or "",
            "stage_title": _stage_title(row["stage"] or ""),
            "role": row["assigned_role"] or "",
            "session_id": row["assigned_session"] or "",
            "seat": (seat or {}).get("short_name") or "",
            "claimed": bool(seat),
            "since": row["created_at"] or "",
        }

    # 有提交在途 = 角色正在回報中（還沒核完）
    inflight = conn.execute(
        "SELECT work_id, issue_id, task_id, session_id, stage, created_at"
        " FROM submissions WHERE status IN ('pending','unverified')").fetchall()
    inflight_by_target: Dict[str, Any] = {}
    for row in inflight:
        target = row["work_id"] or row["issue_id"] or ""
        if not target:
            continue
        seat = seat_by_session.get(row["session_id"] or "")
        inflight_by_target[target] = {
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "seat": (seat or {}).get("short_name") or "",
            "role": (seat or {}).get("role") or "",
            "stage": row["stage"] or "",
            "since": row["created_at"] or "",
        }

    works = conn.execute(
        "SELECT id, title, state, version, assigned_role, assigned_session,"
        "       awaiting_human, awaiting_reason, rework_count, verdict, updated_at"
        " FROM works ORDER BY id").fetchall()
    cards = []
    for row in works:
        target = row["id"]
        active = active_by_target.get(target)
        pending = inflight_by_target.get(target)
        seat = seat_by_session.get(row["assigned_session"] or "")
        cards.append({
            "id": target,
            "title": row["title"] or "",
            "state": row["state"] or "",
            "state_title": _stage_title(row["state"] or ""),
            "version": int(row["version"] or 1),
            "assigned_role": row["assigned_role"] or "",
            "seat": (seat or {}).get("short_name") or "",
            "claimed": bool(seat),
            "awaiting_human": bool(row["awaiting_human"]),
            "awaiting_reason": row["awaiting_reason"] or "",
            "rework_count": int(row["rework_count"] or 0),
            "verdict": row["verdict"] or "",
            "updated_at": row["updated_at"] or "",
            "active": active,
            "inflight": pending,
            "who": ("%s／%s" % (active["role"], active["seat"])) if active and active["claimed"]
                   else (active["role"] if active else ""),
        })

    issues = conn.execute(
        "SELECT id, state, problem, awaiting_human, awaiting_reason, updated_at"
        " FROM issues ORDER BY id").fetchall()
    issue_cards = []
    for row in issues:
        target = row["id"]
        active = active_by_target.get(target)
        pending = inflight_by_target.get(target)
        issue_cards.append({
            "id": target,
            "title": _short(row["problem"] or "", 80),
            "state": row["state"] or "",
            "state_title": _stage_title(row["state"] or ""),
            "awaiting_human": bool(row["awaiting_human"]),
            "awaiting_reason": row["awaiting_reason"] or "",
            "updated_at": row["updated_at"] or "",
            "active": active,
            "inflight": pending,
            "who": ("%s／%s" % (active["role"], active["seat"])) if active and active["claimed"]
                   else (active["role"] if active else ""),
        })

    lease = store.read_lease(conn)
    health = []
    try:
        rows = conn.execute(
            "SELECT id, awaiting_reason FROM works WHERE awaiting_human = 1 ORDER BY id"
        ).fetchall()
        health = [{"id": r["id"], "reason": r["awaiting_reason"] or ""} for r in rows]
    except sqlite3.DatabaseError:
        health = []

    try:
        review_row = conn.execute(
            "SELECT * FROM project_reviews WHERE id = ?",
            ("PRJ-001",)).fetchone()
        project_review = dict(review_row) if review_row else None
    except sqlite3.DatabaseError:
        project_review = None

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_code": store.get_setting(conn, "project_code", ""),
        "works": cards,
        "issues": issue_cards,
        "changes": collect_changes(conn),
        "awaiting": health,
        "lease": lease or {},
        "seats": [dict(r) for r in roles],
        "project_review": project_review,
        "improvements": collect_improvements(conn),
        "rejections": collect_rejections(conn),
    }


# ---------------------------------------------------------------- 頁面

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FBF ENGINE 儀表板</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --panel-2: #1c2330;
    --line: #2a3441;
    --text: #e6edf3;
    --muted: #8b98a9;
    --accent: #58a6ff;
    --ok: #3fb950;
    --warn: #d29922;
    --bad: #f85149;
    --live: #a371f7;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "PingFang TC", "Helvetica Neue", sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 14px 22px; background: rgba(13,17,23,.92);
    backdrop-filter: blur(8px); border-bottom: 1px solid var(--line);
  }
  h1 { font-size: 16px; margin: 0; letter-spacing: .5px; }
  .meta { color: var(--muted); font-size: 12px; }
  .dot {
    width: 9px; height: 9px; border-radius: 50%; background: var(--muted);
    display: inline-block; margin-right: 6px;
  }
  .dot.live { background: var(--live); animation: pulse 1.6s ease-in-out infinite; }
  .dot.off  { background: var(--bad); }
  @keyframes pulse {
    0%,100% { box-shadow: 0 0 0 0 rgba(163,113,247,.55); transform: scale(1); }
    50%     { box-shadow: 0 0 0 7px rgba(163,113,247,0); transform: scale(1.25); }
  }
  main { padding: 20px 22px 60px; max-width: 1500px; margin: 0 auto; }
  section { margin-bottom: 30px; }
  h2 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 1.4px;
    color: var(--muted); margin: 0 0 12px; font-weight: 600;
  }
  .grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(330px,1fr)); }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 15px 17px; position: relative; overflow: hidden;
    transition: transform .25s cubic-bezier(.2,.8,.2,1), border-color .25s, box-shadow .25s;
    animation: rise .45s cubic-bezier(.2,.8,.2,1) both;
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: none; }
  }
  .card:hover { transform: translateY(-3px); border-color: var(--accent); box-shadow: 0 10px 26px rgba(0,0,0,.45); }
  .card.working { border-color: var(--live); }
  .card.working::before {
    content: ""; position: absolute; inset: 0 auto 0 0; width: 4px;
    background: linear-gradient(180deg, var(--live), transparent);
    animation: slide 1.9s ease-in-out infinite;
  }
  @keyframes slide { 0%,100% { opacity: .35; } 50% { opacity: 1; } }
  .card-top { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
  .wid { font-weight: 700; letter-spacing: .6px; color: var(--accent); }
  .state {
    font-size: 11px; padding: 3px 9px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--line); color: var(--muted);
    white-space: nowrap;
  }
  .state.s-working { border-color: var(--live); color: var(--live); }
  .state.s-wait   { border-color: var(--warn);  color: var(--warn); }
  .state.s-done   { border-color: var(--ok);    color: var(--ok); }
  .title { margin: 9px 0 12px; font-size: 14px; color: var(--text); }
  .who { display: flex; align-items: center; gap: 9px; font-size: 13px; }
  .avatar {
    width: 30px; height: 30px; border-radius: 9px; flex: none;
    display: grid; place-items: center; font-size: 12px; font-weight: 700;
    background: var(--panel-2); border: 1px solid var(--line); color: var(--muted);
  }
  .avatar.on { background: rgba(163,113,247,.16); border-color: var(--live); color: var(--live);
               animation: glow 2s ease-in-out infinite; }
  @keyframes glow {
    0%,100% { box-shadow: 0 0 0 0 rgba(163,113,247,.45); }
    50%     { box-shadow: 0 0 12px 2px rgba(163,113,247,.22); }
  }
  .who-name { font-weight: 600; }
  .who-sub { color: var(--muted); font-size: 12px; }
  .feed { border-left: 2px solid var(--line); margin: 0; padding: 0 0 0 16px; list-style: none; }
  .feed li {
    position: relative; padding: 7px 0 7px 4px; font-size: 13px;
    animation: slidein .4s ease both;
  }
  @keyframes slidein {
    from { opacity: 0; transform: translateX(-10px); }
    to   { opacity: 1; transform: none; }
  }
  .feed li::before {
    content: ""; position: absolute; left: -21px; top: 14px;
    width: 8px; height: 8px; border-radius: 50%; background: var(--accent);
  }
  .feed li.kind-rejected::before { background: var(--bad); }
  .feed li.kind-applied::before  { background: var(--ok); }
  .f-time { color: var(--muted); font-size: 11px; margin-right: 8px;
            font-variant-numeric: tabular-nums; }
  .f-target { color: var(--accent); font-weight: 600; margin-right: 8px; }
  .f-field { font-weight: 600; }
  .f-detail { color: var(--muted); font-size: 12px; display: block; margin-top: 3px; }
  .f-std { color: var(--warn); font-size: 12px; display: block; margin-top: 2px; }
  .f-val { color: var(--ok); font-size: 12px; display: block; margin-top: 2px;
           word-break: break-all; }
  .f-diff { color: var(--accent); font-size: 12px; display: block; margin-top: 3px;
            word-break: break-all; }
  .f-diff .old { color: var(--bad); text-decoration: line-through; }
  .f-diff .new { color: var(--ok); }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
  .tag { font-size: 11px; color: var(--muted); border: 1px solid var(--line);
         border-radius: 5px; padding: 1px 6px; margin-left: 6px; }
  .tag.t-rej { border-color: var(--bad); color: var(--bad); }
  .tag.t-imp { border-color: var(--ok); color: var(--ok); }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 14px 17px; margin-bottom: 14px;
  }
  .panel h3 { margin: 0 0 8px; font-size: 13px; color: var(--accent); }
  .kv { color: var(--muted); font-size: 12px; }
  .kv b { color: var(--text); font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>FBF ENGINE 儀表板</h1>
  <span><span class="dot" id="dot"></span><span id="conn">連線中…</span></span>
  <span class="meta" id="meta"></span>
</header>
<main>
  <section>
    <h2>工單 — 現在是誰在寫</h2>
    <div class="grid" id="works"></div>
  </section>
  <section>
    <h2>ISSUE</h2>
    <div class="grid" id="issues"></div>
  </section>
  <section>
    <h2>欄位變更時間軸</h2>
    <ul class="feed" id="feed"></ul>
  </section>
  <section>
    <h2>專案結案檢視 ＋ 引擎改善建議</h2>
    <div id="review"></div>
    <ul class="feed" id="improvements"></ul>
  </section>
  <section>
    <h2>退件紀錄（每一次退件都留 LOG）</h2>
    <ul class="feed" id="rejections"></ul>
  </section>
</main>
<script>
(function () {
  var seen = {};
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function card(item) {
    var working = !!(item.active && item.active.claimed);
    var c = el('div', 'card' + (working ? ' working' : ''));
    var top = el('div', 'card-top');
    top.appendChild(el('span', 'wid', item.id));
    var st = el('span', 'state');
    if (item.awaiting_human) { st.className += ' s-wait'; st.textContent = '等人處理'; }
    else if (item.state === 'closed') { st.className += ' s-done'; st.textContent = '已結案'; }
    else if (working) { st.className += ' s-working'; st.textContent = item.state_title || '進行中'; }
    else { st.textContent = item.state_title || item.state; }
    top.appendChild(st);
    c.appendChild(top);
    c.appendChild(el('div', 'title', item.title || '（無標題）'));

    var who = el('div', 'who');
    var seat = (item.active && item.active.seat) || item.seat || '';
    var role = (item.active && item.active.role) || item.assigned_role || '';
    var av = el('div', 'avatar' + (working ? ' on' : ''), (role || '?').slice(0, 2));
    who.appendChild(av);
    var box = el('div');
    var name = el('div', 'who-name');
    if (!item.active) { name.textContent = '沒有進行中的一棒'; }
    else if (working) { name.textContent = role + '／' + seat + ' 正在寫'; }
    else { name.textContent = role + '（席位無人認領）'; name.style.color = 'var(--warn)'; }
    box.appendChild(name);
    var sub = el('div', 'who-sub');
    if (item.active) {
      sub.textContent = '這一棒：' + (item.active.stage_title || item.active.stage || '');
      if (item.active.task_id) { sub.textContent += '　task ' + item.active.task_id; }
    } else if (item.awaiting_human) {
      sub.textContent = '等人處理：' + (item.awaiting_reason || '');
    } else {
      sub.textContent = '最後更新：' + (item.updated_at || '').replace('T', ' ');
    }
    box.appendChild(sub);
    who.appendChild(box);
    c.appendChild(who);

    if (item.inflight) {
      var inf = el('div', 'f-detail');
      inf.textContent = '回報在途：task ' + item.inflight.task_id +
        (item.inflight.seat ? '（' + item.inflight.seat + '）' : '');
      c.appendChild(inf);
    }
    return c;
  }
  function render(state) {
    var dot = document.getElementById('dot');
    dot.className = 'dot live';
    document.getElementById('conn').textContent = '即時連線';
    var lease = state.lease || {};
    document.getElementById('meta').textContent =
      '專案 ' + (state.project_code || '—') +
      '　快照 ' + (state.generated_at || '').replace('T', ' ') +
      '　協調器 ' + (lease.owner ? lease.owner : '無');

    var w = document.getElementById('works');
    w.innerHTML = '';
    if (!state.works.length) { w.appendChild(el('div', 'empty', '（沒有工單）')); }
    state.works.forEach(function (i) { w.appendChild(card(i)); });

    var is = document.getElementById('issues');
    is.innerHTML = '';
    if (!state.issues.length) { is.appendChild(el('div', 'empty', '（沒有 ISSUE）')); }
    state.issues.forEach(function (i) { is.appendChild(card(i)); });

    var f = document.getElementById('feed');
    f.innerHTML = '';
    if (!state.changes.length) { f.appendChild(el('li', 'empty', '（還沒有變更）')); }
    state.changes.slice().reverse().forEach(function (ch) {
      var li = el('li', 'kind-' + ch.kind);
      li.appendChild(el('span', 'f-time', (ch.at || '').slice(11, 19)));
      li.appendChild(el('span', 'f-target', ch.target_id || ''));
      li.appendChild(el('span', 'f-field', ch.field || ''));
      var tag = el('span', 'tag', ch.kind === 'rejected' ? '退件' : '已套用');
      li.appendChild(tag);
      if (ch.problem) li.appendChild(el('span', 'f-detail', ch.problem));
      if (ch.standard) li.appendChild(el('span', 'f-std', '標準：' + ch.standard));
      if (ch.example) li.appendChild(el('span', 'f-detail', '範例：' + ch.example));
      if (ch.diffs) {
        Object.keys(ch.diffs).forEach(function (k) {
          var line = el('span', 'f-diff');
          line.appendChild(document.createTextNode(k + '：'));
          line.appendChild(el('span', 'old', ch.diffs[k].old));
          line.appendChild(document.createTextNode(' → '));
          line.appendChild(el('span', 'new', ch.diffs[k].new));
          li.appendChild(line);
        });
      } else if (ch.values) {
        // **沒有前後值就是「首次交這個欄位」**——報欄位名與值（截短），
        // 不要把整包 payload 倒出來（那會把時間軸變成雜訊，看不出變更）。
        var keys = Object.keys(ch.values);
        li.appendChild(el('span', 'f-detail', '設定：' + keys.join('、')));
        keys.slice(0, 3).forEach(function (k) {
          var v = String(ch.values[k]);
          if (v.length > 90) { v = v.slice(0, 89) + '…'; }
          li.appendChild(el('span', 'f-val', k + ' = ' + v));
        });
      }
      f.appendChild(li);
    });

    // 專案結案檢視
    var rv = document.getElementById('review');
    rv.innerHTML = '';
    var pr = state.project_review;
    var panel = el('div', 'panel');
    panel.appendChild(el('h3', '', '專案結案檢視'));
    if (!pr) {
      panel.appendChild(el('div', 'kv', '尚未觸發（所有單據都結案後才會派一棒給 PLANNER）'));
    } else {
      var kv = el('div', 'kv');
      kv.appendChild(document.createTextNode('狀態：'));
      kv.appendChild(el('b', '', pr.state || '-'));
      kv.appendChild(document.createTextNode('　第 ' + (pr.review_count || 0) + ' 輪'));
      if (pr.reason) {
        kv.appendChild(document.createTextNode('　理由：'));
        kv.appendChild(el('b', '', pr.reason));
      }
      panel.appendChild(kv);
    }
    rv.appendChild(panel);

    // 引擎改善建議（結案復盤產出）
    var im = document.getElementById('improvements');
    im.innerHTML = '';
    if (!state.improvements.length) {
      im.appendChild(el('li', 'empty', '（還沒有改善建議）'));
    }
    state.improvements.forEach(function (i) {
      var li = el('li');
      li.appendChild(el('span', 'f-time', (i.created_at || '').slice(5, 16)));
      li.appendChild(el('span', 'f-target', i.target || '（引擎）'));
      li.appendChild(el('span', 'tag t-imp', i.status === 'done' ? '已處理' : '待處理'));
      li.appendChild(el('span', 'f-detail', i.suggestion || ''));
      if (i.reason) li.appendChild(el('span', 'f-std', '依據：' + i.reason));
      im.appendChild(li);
    });

    // 退件紀錄
    var rj = document.getElementById('rejections');
    rj.innerHTML = '';
    if (!state.rejections.length) {
      rj.appendChild(el('li', 'empty', '（還沒有退件紀錄）'));
    }
    state.rejections.forEach(function (r) {
      var li = el('li');
      li.appendChild(el('span', 'f-time', (r.created_at || '').slice(5, 16)));
      li.appendChild(el('span', 'f-target', r.target_id || ''));
      li.appendChild(el('span', 'f-field', r.stage || ''));
      li.appendChild(el('span', 'tag t-rej', '退件'));
      var who = r.short_name || r.role || r.session_id || '?';
      li.appendChild(el('span', 'f-detail', '退的是 ' + who + ' 的件'));
      if (r.fields && r.fields.length) {
        li.appendChild(el('span', 'f-std', '欄位：' + r.fields.join('、')));
      }
      if (r.reason) li.appendChild(el('span', 'f-detail', r.reason));
      rj.appendChild(li);
    });
  }
  function connect() {
    var es = new EventSource('/api/stream');
    es.onmessage = function (ev) {
      try { render(JSON.parse(ev.data)); } catch (e) { /* 壞的一筆就跳過 */ }
    };
    es.onerror = function () {
      document.getElementById('dot').className = 'dot off';
      document.getElementById('conn').textContent = '連線中斷，重試…';
    };
  }
  connect();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 伺服器

class DashboardHandler(BaseHTTPRequestHandler):
    """**唯讀**的 HTTP 處理器。

    `server_version` 隱掉版本號（這是內頁，不該對外報細節）。
    """

    server_version = "FBFDashboard"

    def log_message(self, fmt: str, *args: Any) -> None:
        """不要把每個請求都寫到 stderr（會淹掉真正重要的訊息）。"""
        return

    # -- helpers -----------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    def _snapshot(self) -> Dict[str, Any]:
        root = self.server.project_root  # type: ignore[attr-defined]
        # **每個執行緒一條連線**（與引擎同一條規則）：sqlite3 連線不可跨執行緒共用。
        conn = store.connect(root)
        try:
            return collect_state(conn)
        finally:
            conn.close()
    # -- routes ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802（名稱是 BaseHTTPRequestHandler 定的）
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if path == "/api/state":
            try:
                self._json(self._snapshot())
            except Exception as exc:  # noqa: BLE001——頁面要看到錯誤而不是空白
                self._json({"ok": False, "error": str(exc)})
            return
        if path == "/api/stream":
            self._stream()
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _stream(self) -> None:
        """Server-Sent Events：資料沒變就不送，變了才推。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        try:
            while True:
                try:
                    state = self._snapshot()
                    fingerprint = json.dumps(state, ensure_ascii=False, sort_keys=True)
                except Exception as exc:  # noqa: BLE001
                    fingerprint = json.dumps({"error": str(exc)}, ensure_ascii=False)
                if fingerprint != last:
                    last = fingerprint
                    chunk = "data: %s\n\n" % fingerprint
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(SNAPSHOT_INTERVAL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class DashboardServer:
    """儀表板伺服器（背景執行緒）。"""

    def __init__(self, project_root: str, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT):
        self.project_root = os.path.abspath(os.path.expanduser(project_root))
        self.host = host
        self.port = int(port)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        """啟動並回報實際綁定的埠（埠被占用時會換一個）。"""
        if not os.path.isfile(store.db_path(self.project_root)):
            raise ValueError("這個目錄不是 FBF 專案（沒有正本檔）：%s" % self.project_root)
        httpd = ThreadingHTTPServer((self.host, self.port), DashboardHandler)
        httpd.daemon_threads = True
        httpd.project_root = self.project_root  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="fbf-dashboard", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def url(self) -> str:
        return "http://%s:%d/" % (self.host, self.port)


def serve(project_root: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          block: bool = True) -> DashboardServer:
    """起儀表板。`block=True` 時留在前景（Ctrl-C 停）。"""
    server = DashboardServer(project_root, host, port)
    server.start()
    if not block:
        return server
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return server
