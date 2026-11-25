#!/usr/bin/env python3
"""FBF ENGINE 的**人類控制面 CLI**。

規格第 11 節：
> 人類 CLI 只提供 `init`、`requirement confirm`、`engine status`、`engine tick --once`、
> `awaiting list`、`awaiting resolve`、`project close`。
> **不得把這些命令掛在 session 可見的 MCP 工具上。**

所以這個檔案是**給人用的**，不是給 session 用的。session 的唯一通道是 MCP 工具。

兩處為了讓系統真的能跑而做的調整（已在 `DEVIATIONS.md` 記錄）：
1. `requirement confirm` 也能「建立並確認」——否則需求無從產生，
   追溯鏈（需求 → 工單 → 驗收方式 → 證據）的第一節會懸空，而
   `fbflow_work_create` 要求 `requirement_id` 指向已確認的需求。
2. 多一組 `session bind`／`session list`／`session revoke`——
   規格第 7 節要求「host 綁定的 session 身分」是主證據，
   但 §11 的命令清單沒有任何建立綁定的入口。沒有它，MCP 寫入工具永遠是關閉的。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fbf_engine import compact, dashboard, engine, ocserver, paths, project, prompts, store, workflow  # noqa: E402
from fbf_engine.mcp_server import CREDENTIALS_DIR, CREDENTIALS_FILE  # noqa: E402

#: 專案代碼格式：PRJ- + 6 碼大寫英數


def _out(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _err(code: str, message: str, **details: Any) -> int:
    print(json.dumps({"ok": False, "code": code, "message": message,
                      "details": details}, ensure_ascii=False, indent=2),
          file=sys.stderr)
    return 1


def _force_utf8_output() -> None:
    """CLI 的輸出**固定 UTF-8**。

    這份輸出不只是給人看的——工具與 agent 也會讀它。Windows 的預設主控台編碼
    是 cp950（不是 UTF-8），不強制就會吐出**UTF-8 讀取端解不掉的位元組**，
    中文欄位變成亂碼。輸出格式不該由字碼頁或環境變數決定。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


class ProjectRootError(Exception):
    """要操作的根目錄**不合法**（例如 MCP 根目錄）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ProjectNotFound(Exception):
    """要操作的目錄**不是** FBF 專案。

    為什麼要有這個錯：CLI 若在錯的目錄執行，`store.connect` ＋ `init_db`
    會**憑空建立**一份空正本 → 指令讀寫的是一份沒人認領的空資料庫，
    卻回報成功（實測：`role release` 回 `released_session: ""`，而真正的
    專案席位原封不動 → 新 session 重新認領照樣被擋）。
    **寧可直接報錯，也不要在錯的地方寫出正本。**
    """

    def __init__(self, root: str):
        super().__init__(root)
        self.root = root


#: 這個套件自己的根目錄。**引擎的安裝目錄不存放專案資料**（含其子目錄）。
#:
#: 判斷「某個路徑是不是在它底下」請用 `paths.is_under_mcp_root()`——
#: 那邊會做 symlink 解析，這裡的字串比對不會。
MCP_ROOT = paths.mcp_root()


def _resolve_root(args: argparse.Namespace) -> str:
    """把「要看哪個專案」解析成絕對路徑。

    順序：`--project` → 環境變數 `FBF_PROJECT_ROOT` → 目前目錄。
    **只在這裡解析路徑**，不碰檔案——有沒有這個專案由 `_project_root` 判斷。
    """
    explicit = (getattr(args, "project", None) or "").strip()
    if not explicit:
        explicit = os.environ.get("FBF_PROJECT_ROOT", "").strip()
    if not explicit:
        explicit = os.getcwd()
    return os.path.abspath(os.path.expanduser(explicit))


def _root(args: argparse.Namespace) -> str:
    """要**建立**專案的根目錄（`init`／`join` 才用）。

    其餘指令一律走 `_project_root`——那裡會擋掉「這裡不是專案」。
    """
    return _resolve_root(args)


def _project_root(args: argparse.Namespace) -> str:
    """要操作的專案根目錄。**必須已經是 FBF 專案**，否則報錯。

    這是「釋放跟認領讀不同地方」的根因修法：
    - 沒有 `--project` 也沒有 `FBF_PROJECT_ROOT` → 落在目前目錄；
      那裡若不是專案 → 明確報錯，**不再憑空建立空正本**。
    - 明確指定的目錄若沒有正本 → 同樣報錯（不猜、不建立）。
    """
    root = _resolve_root(args)
    if paths.is_under_mcp_root(root):
        raise ProjectRootError(
            "project.root-is-mcp",
            "引擎的安裝目錄不存放專案資料（含其子目錄），"
            "不能把它當專案來操作：%s" % root)
    if not os.path.isfile(store.db_path(root)):
        raise ProjectNotFound(root)
    return root


def _conn(args: argparse.Namespace):
    root = _project_root(args)
    conn = store.connect(root)
    store.init_db(conn)
    return conn


# ---------------------------------------------------------------- init

def cmd_init(args: argparse.Namespace) -> int:
    """建立專案正本。**不覆蓋既有專案。**"""
    root = _root(args)
    if paths.is_under_mcp_root(root):
        return _err("project.root-is-mcp",
                    "引擎的安裝目錄不存放專案資料（含其子目錄），"
                    "不能在這裡建立正本：%s" % root)
    path = store.db_path(root)
    if os.path.exists(path) and not args.force:
        return _err("project.exists", "這個專案已經初始化了：%s" % path)
    os.makedirs(root, exist_ok=True)
    conn = store.connect(root)
    store.init_db(conn)
    try:
        code = (args.code or project.generate_code(conn)).upper()
    except project.JoinError as exc:
        conn.close()
        return _err(exc.code, exc.message)
    with store.tx(conn):
        store.set_setting(conn, "project_code", code)
        store.set_setting(conn, "name", args.name or os.path.basename(root))
        if args.endpoint:
            store.set_setting(conn, "opencode_endpoint", args.endpoint)
    store.journal(conn, "project.init", code, detail={"root": root})
    conn.close()
    return _out({"ok": True, "project": root, "project_code": code,
                 "db": path})


# ---------------------------------------------------------------- requirement

def cmd_requirement_confirm(args: argparse.Namespace) -> int:
    """確認需求。給 `--id` 就確認既有的；給 `--text` 就**建立並確認**。

    `--supersedes R-001`（可重複）：**這條需求取代那些舊需求**——
    需求變了要新增一條（新舊都留著），不是改舊的。
    引擎據此在結案追溯時不再要求舊需求有結案工單。
    """
    conn = _conn(args)
    try:
        with store.tx(conn):
            if args.id:
                row = conn.execute("SELECT * FROM requirements WHERE id = ?",
                                   (args.id,)).fetchone()
                if row is None:
                    return _err("requirement.missing", "找不到需求 %s" % args.id)
                conn.execute("UPDATE requirements SET confirmed_at = ? WHERE id = ?",
                             (store.now(), args.id))
                requirement_id = args.id
            else:
                if not args.text or not args.criterion:
                    return _err("CONTENT-MISSING",
                                "要嘛給 --id，否則 --text 與 --criterion 都要給")
                requirement_id = store.next_id(conn, "requirements", "R", 3)
                conn.execute(
                    "INSERT INTO requirements(id, text, success_criterion, confirmed_at,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (requirement_id, args.text, args.criterion, store.now(), store.now()))
            # **需求層的取代關係**：這條需求取代哪些舊需求（人類宣告）。
            for old_id in (getattr(args, "supersedes", None) or []):
                old_id = str(old_id).strip()
                if not old_id or old_id == requirement_id:
                    continue
                row = conn.execute(
                    "SELECT * FROM requirements WHERE id = ?", (old_id,)).fetchone()
                if row is None:
                    return _err("requirement.missing",
                                "找不到要取代的需求 %s" % old_id)
                conn.execute(
                    "UPDATE requirements SET superseded_by = ? WHERE id = ?",
                    (requirement_id, old_id))
                store.journal(conn, "requirement.supersede", old_id,
                              actor=args.confirmed_by or "human",
                              detail={"superseded_by": requirement_id})
            store.journal(conn, "requirement.confirm", requirement_id,
                          actor=args.confirmed_by or "human")
            # 人類同意 = GRILL 段的完成訊號 → 收掉還開著的 grill task，
            # 下一輪 tick 才會開始生成工單（三段中的第 3 段）。
            grill_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE stage = ? AND status = 'open'",
                (workflow.STAGE_GRILL,)).fetchall()]
            for task_id in grill_ids:
                conn.execute(
                    "UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?",
                    (store.now(), task_id))
                store.journal(conn, "grill.close", task_id,
                              actor=args.confirmed_by or "human",
                              detail={"reason": "requirement.confirmed",
                                     "requirement_id": requirement_id})
        row = conn.execute("SELECT * FROM requirements WHERE id = ?",
                           (requirement_id,)).fetchone()
        return _out({"ok": True, "requirement": dict(row),
                     "grill_tasks_closed": grill_ids})
    finally:
        conn.close()


# ---------------------------------------------------------------- engine

def cmd_engine_status(args: argparse.Namespace) -> int:
    """協調器現況：待處理提交、待人處理項、目前里程碑。"""
    conn = _conn(args)
    try:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE status IN (?,?)",
            (engine.SUB_PENDING, engine.SUB_UNVERIFIED)).fetchone()["n"]
        outbox = conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status").fetchall()
        milestones = conn.execute(
            "SELECT * FROM milestones ORDER BY seq").fetchall()
        works = conn.execute(
            "SELECT state, COUNT(*) AS n FROM works GROUP BY state").fetchall()
        lease = store.read_lease(conn)
        awaiting = conn.execute(
            "SELECT id, title, state, awaiting_reason FROM works"
            " WHERE awaiting_human = 1 ORDER BY id").fetchall()
        # **被取代的工單也要列出來**——它們停在原狀態，不看這裡會以為還在跑。
        superseded = conn.execute(
            "SELECT id, title, state, superseded_by, supersede_reason FROM works"
            " WHERE COALESCE(superseded_by, '') <> '' ORDER BY id").fetchall()
        endpoint = store.get_setting(conn, "opencode_endpoint", "")
        # **看得出來卡在哪**：派不出去時，outbox 的 `last_error` 會寫清楚原因
        # （平台回的那句話，例如「Could not reach server」）✓
        warnings = []
        if not endpoint:
            # **引擎不會自己開 OpenCode server**（上游 CLI 沒帶 `--server` 會自動開），
            # 所以端點一律由 `ocserver` 唯讀探索既有 server；探索不到就是真的沒有。
            found_url, _ = ocserver.discover("")
            if found_url:
                warnings.append("OpenCode endpoint：沿用既有 server %s"
                                "（唯讀探索；引擎不會自己開一個）。" % found_url)
            else:
                warnings.append(
                    "**找不到可連線的 OpenCode server，而且引擎不會自己開一個** → "
                    "先讓 OpenCode 跑起來，或用 --endpoint／FBF_OPENCODE_SERVER 指定位址。"
                    "（沒有 server 時：presence 當查不到、回執當未核對、派工會明確失敗。）")
        deferred = conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE status IN ('queued','unknown')"
        ).fetchone()["n"]
        if deferred:
            warnings.append("%d 筆派工還在 outbox 等著（看 last_error 才知道卡在哪）。" % deferred)
        # 席位也放進來：**流程卡住不動時，通常是席位被一個沒活動的 session 占著**，
        # 不把席位列出來就看不出來卡在哪。
        coordinator = engine.Coordinator(_project_root(args))
        try:
            seats = coordinator.seat_status()
            # **巡檢**：哪張單有問題（停滯、派不到、等人太久…）。
            health = coordinator.health_check()
        finally:
            coordinator.close()
        return _out({
            "ok": True,
            "project_code": store.get_setting(conn, "project_code", ""),
            "opencode_endpoint": endpoint,
            "warnings": warnings,
            "pending_submissions": pending,
            "outbox": {r["status"]: r["n"] for r in outbox},
            "works_by_state": {r["state"]: r["n"] for r in works},
            "superseded_works": [dict(r) for r in superseded],
            # **上下文收合紀錄**：哪個 session 什麼時候 compact 過、
            # 引擎又叫它收合過幾次（source：detected／requested）。
            "compacts": compact.ledger_snapshot(conn),
            # **目前用哪一種收合方式**（compact＝壓縮／reset＝清空對話）。
            "compact_mode": compact.mode_of(conn),
            "milestones": [dict(m) for m in milestones],
            "awaiting": [dict(a) for a in awaiting],
            "seats": seats,
            "stale_seats": [s for s in seats if s.get("stale")],
            "lease": lease,
            "health": health,
        })
    finally:
        conn.close()


def cmd_engine_tick(args: argparse.Namespace) -> int:
    """跑一趟 tick（診斷用）。常駐排程由 MCP 進程持有。"""
    try:
        coordinator = engine.Coordinator(_project_root(args))
    except sqlite3.DatabaseError as exc:
        return _db_error(exc)
    try:
        report = coordinator.tick()
        return _out({"ok": True, "report": report})
    except sqlite3.DatabaseError as exc:
        return _db_error(exc)
    finally:
        coordinator.close()


def _db_error(exc: sqlite3.DatabaseError) -> int:
    """資料庫錯誤：損毀與暫時性錯誤要分開講。"""
    if store.is_corrupted(exc):
        return _err("project.corrupted",
                    "專案正本損毀或無法讀取（%s）：請先從備份還原 %s，不要繼續寫入。"
                    % (exc, store.DB_FILENAME))
    return _err("database.error", "資料庫錯誤（暫時性）：%s" % exc)


# ---------------------------------------------------------------- awaiting

def cmd_awaiting_list(args: argparse.Namespace) -> int:
    conn = _conn(args)
    try:
        coordinator = engine.Coordinator(_project_root(args))
        try:
            return _out({"ok": True, "awaiting": coordinator.awaiting_list()})
        finally:
            coordinator.close()
    finally:
        conn.close()


def cmd_awaiting_resolve(args: argparse.Namespace) -> int:
    """人類裁決：grant_more（再給一次機會）／revise（修訂工單）／abort（中止）。"""
    coordinator = engine.Coordinator(_project_root(args))
    try:
        result = coordinator.awaiting_resolve(args.id, args.action, args.reason or "")
        return _out(result) if result.get("ok") else _err(
            result.get("code", "UNKNOWN"), result.get("message", ""),
            **result.get("details", {}))
    finally:
        coordinator.close()


# ---------------------------------------------------------------- dashboard

def cmd_dashboard(args: argparse.Namespace) -> int:
    """**後台網頁（唯讀儀表板）**：即時看每張工單現在是誰在寫、欄位改成什麼。

    **唯讀**：不寫正本、不拿租約、不打擾任何 session——只是把正本畫出來。

    預設只綁 `127.0.0.1`（這一頁含工單內容與席位資訊）。
    要讓區網看得到必須明確指定 `--host 0.0.0.0`。
    """
    root = _project_root(args)
    server = dashboard.DashboardServer(root, host=args.host, port=args.port)
    try:
        port = server.start()
    except (ValueError, OSError) as exc:
        return _err("dashboard.failed", str(exc))
    print("FBF ENGINE 儀表板：%s" % server.url)
    print("（唯讀；Ctrl-C 停止）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
        print("已停止。")
    return 0


# ---------------------------------------------------------------- project

def cmd_project_close(args: argparse.Namespace) -> int:
    """專案結案。**必須通過雙向追溯檢查**，並記錄確認者與時間。"""
    coordinator = engine.Coordinator(_project_root(args))
    try:
        result = coordinator.project_close(args.confirmed_by or "human")
        return _out(result) if result.get("ok") else _err(
            result.get("code", "UNKNOWN"), result.get("message", ""),
            **result.get("details", {}))
    finally:
        coordinator.close()


# ---------------------------------------------------------------- review

def cmd_review_improvements(args: argparse.Namespace) -> int:
    """引擎改善建議（**結案復盤的產出**）。

    **唯讀**：只讀 `engine_improvements` 表，不寫正本。
    """
    conn = _conn(args)
    try:
        rows = conn.execute(
            "SELECT id, review_id, target, suggestion, reason, status, created_at"
            " FROM engine_improvements ORDER BY id DESC").fetchall()
        review = conn.execute(
            "SELECT * FROM project_reviews WHERE id = ?",
            (engine.PROJECT_REVIEW_ID,)).fetchone()
        return _out({"ok": True,
                     "improvements": [dict(r) for r in rows],
                     "project_review": dict(review) if review else None})
    finally:
        conn.close()


def cmd_review_improvement_status(args: argparse.Namespace) -> int:
    """把某一筆引擎改善建議標成已處理／不採用。

    **為什麼要能寫**：復盤產出的建議沒人標記狀態 → 下次復盤不知道
    上一輪的建議到底修了沒（DB 裡四筆都長一樣，看不出進度）。
    """
    conn = _conn(args)
    try:
        row = conn.execute(
            "SELECT id, target, suggestion FROM engine_improvements WHERE id = ?",
            (int(args.improvement_id),)).fetchone()
        if row is None:
            return _err("improvement.missing",
                        "找不到 id=%s 的引擎改善建議" % args.improvement_id)
        with store.tx(conn):
            conn.execute(
                "UPDATE engine_improvements SET status = ? WHERE id = ?",
                (args.status, int(args.improvement_id)))
        return _out({"ok": True, "id": int(args.improvement_id),
                     "status": args.status,
                     "target": row["target"], "suggestion": row["suggestion"]})
    finally:
        conn.close()


def cmd_review_rejections(args: argparse.Namespace) -> int:
    """退件 LOG（**每一次退件都留的那一筆**）。

    每筆含：退件單據的完整副本、退件原因、為了哪個欄位退件、退的是誰的件。
    """
    conn = _conn(args)
    try:
        limit = int(args.limit or 50)
        rows = conn.execute(
            "SELECT id, target_kind, target_id, submission_id, stage, session_id,"
            " role, short_name, gaps, fields, reason, created_at"
            " FROM rejection_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM rejection_log").fetchone()["n"]
        return _out({"ok": True, "total": count,
                     "rejections": [dict(r) for r in rows]})
    finally:
        conn.close()


# ---------------------------------------------------------------- session

def _credentials_path(root: str) -> str:
    return os.path.join(root, CREDENTIALS_DIR, CREDENTIALS_FILE)


def cmd_session_bind(args: argparse.Namespace) -> int:
    """把一個 session 綁到角色，並產生 **host 端 token**。

    token 寫在 `.fbf/credentials.json`（host 層，模型讀不到）。
    MCP client 設定把 token 帶進來（`FBF_SESSION_TOKEN`），伺服器據此認出 session。
    """
    root = _project_root(args)
    conn = store.connect(root)
    store.init_db(conn)
    try:
        with store.tx(conn):
            seat = conn.execute(
                "SELECT * FROM roles WHERE role = ? AND short_name = ?",
                (args.role.upper(), args.short_name)).fetchone()
            holder = conn.execute(
                "SELECT role, short_name FROM roles WHERE session_id = ?",
                (args.session_id,)).fetchone()
            if holder is not None and (holder["role"] != args.role.upper()
                                       or holder["short_name"] != args.short_name):
                return _err("ROLE-DENIED",
                            "這個 session 已經占著 %s／%s（一個 session 只能占一個席位）"
                            % (holder["role"], holder["short_name"]))
            if seat is None:
                # 沒有這個席位 → 建一個（席位本來就該由人先建，這是相容用法）
                conn.execute(
                    "INSERT INTO roles(role, short_name, session_id, agent, status,"
                    " project_code, workdir, created_at) VALUES (?,?,?,?, 'active', ?, ?, ?)",
                    (args.role.upper(), args.short_name, args.session_id,
                     args.agent or "opencode",
                     store.get_setting(conn, "project_code", ""), args.workdir or "",
                     store.now()))
            elif seat["session_id"] and seat["session_id"] != args.session_id:
                return _err("ROLE-SLOT-TAKEN",
                            "這個席位已經被 session %s 認領，無法搶走。"
                            "若那個 session 已經中斷，請讓人跑 "
                            "`fbf.py role release --role %s --short-name %s` 釋放席位。"
                            % (seat["session_id"], args.role.upper(), args.short_name))
            else:
                conn.execute(
                    "UPDATE roles SET session_id = ?, agent = ?, status = 'active',"
                    " project_code = ?, updated_at = ? WHERE role = ? AND short_name = ?",
                    (args.session_id, args.agent or "opencode",
                     store.get_setting(conn, "project_code", ""), store.now(),
                     args.role.upper(), args.short_name))
            store.journal(conn, "session.bind", args.session_id,
                          actor=args.role, session_id=args.session_id,
                          detail={"short_name": args.short_name})

        path = _credentials_path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data: Dict[str, Any] = {"sessions": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict) and isinstance(loaded.get("sessions"), dict):
                    data = loaded
            except (OSError, ValueError):
                pass
        token = secrets.token_hex(24)
        data["sessions"][token] = {
            "session_id": args.session_id,
            "role": args.role.upper(),
            "short_name": args.short_name,
            "agent": args.agent or "opencode",
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return _out({
            "ok": True,
            "session_id": args.session_id,
            "role": args.role.upper(),
            "short_name": args.short_name,
            "session_token": token,
            "note": "把 session_token 放進 MCP client 設定的 FBF_SESSION_TOKEN 環境變數。"
                    "這是 host 層的憑證，不要寫進任何模型看得到的檔案。",
        })
    finally:
        conn.close()


def cmd_session_list(args: argparse.Namespace) -> int:
    conn = _conn(args)
    try:
        rows = conn.execute("SELECT role, short_name, session_id, agent, status,"
                            " uuid_sha256 FROM roles ORDER BY role, short_name").fetchall()
        return _out({"ok": True, "roles": [
            {"role": r["role"], "short_name": r["short_name"],
             "session_id": r["session_id"], "agent": r["agent"], "status": r["status"],
             "has_uuid": bool(r["uuid_sha256"])} for r in rows]})
    finally:
        conn.close()


def cmd_session_revoke(args: argparse.Namespace) -> int:
    """撤銷綁定。**撤銷後這個 token 立刻失效，席位也會空出來。**

    以前這裡只把 `status` 設成 revoked、`session_id` 留著 → 席位**永遠被占住**，
    新的 session 認領不到（`ROLE-SLOT-TAKEN`），等於沒有出口。

    現在：`session_id`／UUID 清掉，該 session 名下的 open task 一併作廢
    （與 `role release` 同一條路）——否則 `_dispatch_pending` 看到「已有開啟任務」
    會跳過那些工單，流程接不回去。
    """
    root = _project_root(args)
    conn = store.connect(root)
    store.init_db(conn)
    try:
        with store.tx(conn):
            cur = conn.execute(
                "UPDATE roles SET status = 'revoked', session_id = NULL,"
                " uuid_sha256 = NULL WHERE session_id = ?", (args.session_id,))
            cancelled = conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ?"
                " WHERE assigned_session = ? AND status = 'open'",
                (store.now(), args.session_id)).rowcount
            # **沒送出去的派工一併作廢**（與 `role release` 同一條路）：
            # 收件人已經被撤銷 → 送不出去，只會一直重查累積 `unknown`。
            dropped = conn.execute(
                "UPDATE outbox SET status = ?, last_error = ?, updated_at = ?"
                " WHERE recipient_session = ? AND status IN (?,?,?)",
                (engine.OUT_FAILED, "session 已撤銷，遺留派工", store.now(),
                 args.session_id, engine.OUT_QUEUED, engine.OUT_UNKNOWN,
                 engine.OUT_SENDING)).rowcount
            store.journal(conn, "session.revoke", args.session_id, actor="human",
                          detail={"cancelled_open_tasks": cancelled,
                                  "dropped_outbox": dropped})
        path = _credentials_path(root)
        removed = 0
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                sessions = data.get("sessions", {})
                for token in [t for t, v in sessions.items()
                              if isinstance(v, dict) and v.get("session_id") == args.session_id]:
                    del sessions[token]
                    removed += 1
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
            except (OSError, ValueError):
                pass
        return _out({"ok": True, "revoked_tokens": removed,
                     "cancelled_tasks": cancelled, "dropped_outbox": dropped,
                     "project_root": root})
    finally:
        conn.close()


def cmd_role_add(args: argparse.Namespace) -> int:
    """建立一個**角色席位**（空的，等 session 來認領）。

    這是「安裝 MCP 不要求建立角色，專案要推動工作流時才建角色」的落地點：
    人類先確認簡稱、把席位建好，session 才認領。
    """
    conn = _conn(args)
    try:
        with store.tx(conn):
            row = conn.execute(
                "SELECT * FROM roles WHERE role = ? AND short_name = ?",
                (args.role.upper(), args.short_name)).fetchone()
            if row is not None:
                if row["session_id"]:
                    return _err("ROLE-SLOT-TAKEN",
                                "這個席位已經被 session %s 認領：%s／%s。"
                                "若那個 session 已經中斷，請讓人跑 "
                                "`fbf.py role release --role %s --short-name %s` 釋放席位。"
                                % (row["session_id"], row["role"], row["short_name"],
                                   row["role"], row["short_name"]))
                return _out({"ok": True, "role": row["role"],
                             "short_name": row["short_name"],
                             "note": "席位已存在（尚未有人認領）"})
            conn.execute(
                "INSERT INTO roles(role, short_name, session_id, agent, status,"
                " project_code, workdir, created_at) VALUES (?,?,NULL,?, 'active', ?, '', ?)",
                (args.role.upper(), args.short_name, args.agent or "opencode",
                 store.get_setting(conn, "project_code", ""), store.now()))
            store.journal(conn, "role.slot", "%s/%s" % (args.role.upper(), args.short_name),
                          actor="human")
        return _out({"ok": True, "role": args.role.upper(), "short_name": args.short_name,
                     "note": "席位已建好。等 session 呼叫 fbflow_register 來認領。"})
    finally:
        conn.close()


def cmd_role_release(args: argparse.Namespace) -> int:
    """**釋放席位**——讓另一個 session 可以接管。

    情境：原本認領席位的 session 意外中斷（機器重開、程式崩了、session 被關掉），
    席位卻還被占著 → 沒有任何 session 能認領它 → 整條流程卡住，而且沒有任何
    地方會顯示為什麼卡住。這裡把席位退回成「空的」，新 session 呼叫
    `fbflow_register` 就能認領。

    **這是人的決定**：確認那個 session 真的不行了才釋放。
    """
    coordinator = engine.Coordinator(_project_root(args))
    try:
        result = coordinator.release_role(args.role, args.short_name,
                                          reason=args.reason or "")
        if not result.get("ok"):
            return _err(result.get("code", "release.failed"), result.get("message", ""))
        out = {"ok": True, "role": result["role"], "short_name": result["short_name"],
               "released_session": result["released_session"],
               "was_occupied": result.get("was_occupied", bool(result["released_session"])),
               # **是哪一份正本**——回報「釋放成功」卻讀寫到別的資料庫，
               # 是這條指令最容易被誤判成「已生效」的地方。
               "project_root": coordinator.root,
               "note": result["note"]}
        if result["released_session"]:
            out["also"] = ("若要連那個 session 的 host 憑證一起撤銷："
                           "`fbf.py session revoke --session-id %s`"
                           % result["released_session"])
        return _out(out)
    finally:
        coordinator.close()


def cmd_role_list(args: argparse.Namespace) -> int:
    """列出角色席位：哪些還空著、哪些被認領、以及**被認領卻很久沒活動**的（孤兒）。"""
    coordinator = engine.Coordinator(_project_root(args))
    try:
        seats = coordinator.seat_status()
        return _out({"ok": True, "roles": seats,
                     "stale_seats": [s for s in seats if s.get("stale")],
                     "note": "釋放席位：`fbf.py role release --role <角色> "
                             "--short-name <簡稱>`"})
    finally:
        coordinator.close()


def cmd_install(args: argparse.Namespace) -> int:
    """**安裝這個 MCP**（全域範圍）：只放引擎入口，**不跟任何專案綁定**。

    安裝完不會有任何專案、角色、資料。
    某個專案要用它推動工作流時，那個專案自己 `fbf.py join`。
    """
    config = args.config or paths.config_path()
    try:
        command = paths.resolve_python_command(args.python, args.host_root)
    except ValueError as exc:
        return _err("usage", str(exc))
    written = paths.merge_server(config, project_root="", host_root=args.host_root,
                                 python_command=command)
    return _out({"ok": True, "scope": "global", "config": written,
                 "mcp_root": paths.mcp_root(),
                 "python": command,
                 "note": "這是全域安裝，不綁任何專案。要讓某個專案用它，"
                         "在那個專案裡跑 `fbf.py join --project <該專案>`。"})


def cmd_join(args: argparse.Namespace) -> int:
    """**某個專案加入這個 MCP**。

    做三件事：
    1. 在該專案建立 v2 正本（`fbf_engine.db`）——**交付物在這個專案裡**。
    2. 在該專案的 `opencode.json` 加入這個 MCP，帶 `FBF_PROJECT_ROOT`。
    3. （可選）建立角色席位。

    **`--endpoint` 是選用的**：OpenCode server 跑在**隨機埠**而且要密碼
    （實測是 49374，每次重開就換）→ 寫死位址一定會壞。
    不指定時由引擎**唯讀**探索 OpenCode 自己的註冊檔
    （`~/.local/state/opencode/service.json`）取得既有 server 的位址。
    **引擎永遠不會自己啟動 OpenCode server**——上游 CLI 沒帶 `--server`
    會自動開一個常駐背景 server，那不是這個套件該做的事。

    實作走 `fbf_engine.project.join_project`——**與 MCP 工具
    `fbflow_project_join` 同一份邏輯**，不會有兩套「加入」的定義。
    """
    root = os.path.abspath(os.path.expanduser(args.root or _root(args)))
    try:
        command = paths.resolve_python_command(args.python, args.host_root)
    except ValueError as exc:
        return _err("usage", str(exc))
    try:
        info = project.join_project(
            root, name=args.name or "", endpoint=args.endpoint or "",
            host_root=args.host_root, python_command=command)
    except project.JoinError as exc:
        return _err(exc.code, exc.message)

    return _out({"ok": True, "scope": "project", "project_root": info["project_root"],
                 "project_code": info["project_code"], "config": info["config"],
                 "claude_config": info["claude_config"],
                 "db": info["db"], "roles_created": info["roles_created"],
                 "python": command, "opencode_endpoint": info["opencode_endpoint"],
                 "note": "這個專案已經加入 FBF ENGINE。啟動三段（給人看）：\n"
                         "1) 三個角色席位加入（本命令已建 P1／T1／W1；"
                         "session 啟動後各自 fbflow_register 認領，或 engine tick 自動 enroll）；\n"
                         "2) engine tick 會派 GRILL 給 PLANNER：讀專案文件、"
                         "釐清範圍／目標／產出、用自己的話複述後問你同不同意；\n"
                         "3) 你跑 requirement confirm 同意後，才生成工單、"
                         "開始 design→…→closed 流水線。"})


def cmd_contract(args: argparse.Namespace) -> int:
    """印出交件契約（給文件／SKILL 用；內容來自 workflow，不另寫）。"""
    return _out({"ok": True, "contract": prompts.render_contract_markdown()})


# ---------------------------------------------------------------- compact

def cmd_compact_mode(args: argparse.Namespace) -> int:
    """設定這個專案的**上下文收合方式**。

    - `reset`（預設）：清空對話、**session 不變**——模型、權限、工具設定全部留著。
    - `compact`：壓縮成摘要（保留脈絡）。

    **沒有「換一個 session」這個選項**：新 session 會用預設值重新開始，
    使用者配給那個 session 的模型與設定會不見（使用者裁決 2026-11-25）。
    """
    root = _project_root(args)
    conn = store.connect(root)
    store.init_db(conn)
    try:
        with store.tx(conn):
            mode = compact.set_mode(conn, args.mode)
        store.journal(conn, "compact.mode", mode, actor="human")
        return _out({"ok": True, "mode": mode, "modes": list(compact.MODES),
                     "note": "從下一次收合提醒開始生效（引擎每趟 tick 看一次）。"})
    except ValueError as exc:
        return _err("usage", str(exc))
    finally:
        conn.close()


def cmd_compact_status(args: argparse.Namespace) -> int:
    """收合方式、收合紀錄、各宿主的收合指令對照。"""
    root = _project_root(args)
    conn = store.connect(root)
    store.init_db(conn)
    try:
        return _out({"ok": True, "mode": compact.mode_of(conn),
                     "modes": list(compact.MODES),
                     "compacts": compact.ledger_snapshot(conn),
                     "commands": {agent: compact.AGENT_COMMANDS.get(agent, {})
                                  for agent in sorted(compact.AGENT_COMMANDS)}})
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI

def _add_project_option(parser: argparse.ArgumentParser) -> None:
    """讓 `--project` **放在子命令後面也有效**。

    為什麼要這樣：文件與習慣寫法都是 `fbf.py role release --project <專案>`，
    但 `--project` 原本是**全域選項**，只能放在子命令**前面**
    （`fbf.py --project <專案> role release`）→ 照文件寫會直接報
    「unrecognized arguments」。

    用 `SUPPRESS` 當預設值：**沒有指定時不會覆蓋**全域選項的值；
    指定了就覆蓋（子命令後面的值優先）。
    """
    parser.add_argument("--project", default=argparse.SUPPRESS,
                        help="專案根目錄（同全域選項；放子命令後面也有效）")


def _add_project_to_subparsers(parser: argparse.ArgumentParser) -> None:
    """把 `--project` 加到每一層子命令（含群組層）。"""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                _add_project_option(sub)
                _add_project_to_subparsers(sub)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fbf", description="FBF ENGINE 人類控制面（session 請用 MCP 工具）")
    parser.add_argument("--project", default="",
                        help="專案根目錄（預設：環境變數 FBF_PROJECT_ROOT 或目前目錄；"
                             "**該目錄必須已經是 FBF 專案**，否則指令會報錯，"
                             "不會憑空建立正本）")
    sub = parser.add_subparsers(dest="group")

    p_init = sub.add_parser("init", help="建立專案正本")
    p_init.add_argument("--name", default="")
    p_init.add_argument("--code", default="")
    p_init.add_argument("--endpoint", default="", help="OpenCode server endpoint")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_req = sub.add_parser("requirement", help="需求")
    p_req_sub = p_req.add_subparsers(dest="action")
    p_conf = p_req_sub.add_parser("confirm", help="確認需求（或建立並確認）")
    p_conf.add_argument("--id", default="")
    p_conf.add_argument("--text", default="")
    p_conf.add_argument("--criterion", default="")
    p_conf.add_argument("--confirmed-by", default="")
    p_conf.add_argument("--supersedes", action="append", default=[],
                        help="這條需求取代哪些舊需求（可重複；需求變了要新增而不是改舊的）")
    p_conf.set_defaults(func=cmd_requirement_confirm)
    p_req.set_defaults(func=lambda a: _err("usage", "requirement 要指定子命令"))

    p_engine = sub.add_parser("engine", help="協調器")
    p_engine_sub = p_engine.add_subparsers(dest="action")
    p_status = p_engine_sub.add_parser("status", help="協調器現況")
    p_status.set_defaults(func=cmd_engine_status)
    p_tick = p_engine_sub.add_parser("tick", help="跑一趟 tick")
    p_tick.add_argument("--once", action="store_true", default=True)
    p_tick.set_defaults(func=cmd_engine_tick)
    p_engine.set_defaults(func=lambda a: _err("usage", "engine 要指定子命令"))

    p_await = sub.add_parser("awaiting", help="待人工裁決")
    p_await_sub = p_await.add_subparsers(dest="action")
    p_await_list = p_await_sub.add_parser("list", help="列出待人處理項")
    p_await_list.set_defaults(func=cmd_awaiting_list)
    p_await_resolve = p_await_sub.add_parser("resolve", help="裁決")
    p_await_resolve.add_argument("--id", required=True)
    p_await_resolve.add_argument("--action", required=True,
                                 choices=["grant_more", "revise", "abort"])
    p_await_resolve.add_argument("--reason", default="")
    p_await_resolve.set_defaults(func=cmd_awaiting_resolve)
    p_await.set_defaults(func=lambda a: _err("usage", "awaiting 要指定子命令"))

    p_dash = sub.add_parser("dashboard", help="後台網頁（唯讀儀表板）")
    p_dash.add_argument("--host", default=dashboard.DEFAULT_HOST,
                         help="監聽位址（預設 %s；只有本機看得到）"
                              % dashboard.DEFAULT_HOST)
    p_dash.add_argument("--port", type=int, default=dashboard.DEFAULT_PORT,
                         help="監聽埠（預設 %d；被占用時自動換一個）"
                              % dashboard.DEFAULT_PORT)
    p_dash.set_defaults(func=cmd_dashboard)

    p_project = sub.add_parser("project", help="專案")
    p_project_sub = p_project.add_subparsers(dest="action")
    p_close = p_project_sub.add_parser("close", help="結案（需通過雙向追溯）")
    p_close.add_argument("--confirmed-by", default="")
    p_close.set_defaults(func=cmd_project_close)
    p_project.set_defaults(func=lambda a: _err("usage", "project 要指定子命令"))

    p_install = sub.add_parser("install", help="安裝這個 MCP（全域，不綁任何專案）")
    p_install.add_argument("--config", default="",
                           help="OpenCode 設定檔（預設 ~/.config/opencode/opencode.json）")
    p_install.add_argument("--host-root", default="",
                           help="本機哪個前綴對應到設定檔那台機器的 /。"
                                "設定檔裡的路徑會換成對方的形式")
    p_install.add_argument("--python", default="",
                           help="寫進設定的 Python 命令（預設：就是現在跑這個安裝器的"
                                "那個 Python）。替別台主機產生設定時必須指定對方的命令")
    p_install.set_defaults(func=cmd_install)

    p_join = sub.add_parser("join", help="某個專案加入這個 MCP")
    p_join.add_argument("--root", default="", help="專案根目錄（預設：--project）")
    p_join.add_argument("--name", default="")
    p_join.add_argument("--endpoint", default="", help="OpenCode server endpoint")
    p_join.add_argument("--host-root", default="",
                        help="本機哪個前綴對應到設定檔那台機器的 /")
    p_join.add_argument("--python", default="",
                        help="寫進設定的 Python 命令（預設：就是現在跑這個安裝器的"
                             "那個 Python）")
    p_join.set_defaults(func=cmd_join)

    p_compact = sub.add_parser("compact", help="上下文收合方式")
    p_compact_sub = p_compact.add_subparsers(dest="action")
    p_cmode = p_compact_sub.add_parser("mode", help="設定收合方式")
    p_cmode.add_argument("--mode", required=True, choices=list(compact.MODES),
                         help="reset＝清空對話（session 不變，設定都留著）；"
                              "compact＝壓縮成摘要（保留脈絡）")
    p_cmode.set_defaults(func=cmd_compact_mode)
    p_cstatus = p_compact_sub.add_parser("status", help="收合方式與紀錄")
    p_cstatus.set_defaults(func=cmd_compact_status)

    p_role = sub.add_parser("role", help="角色席位（人類建，session 認領）")
    p_role_sub = p_role.add_subparsers(dest="action")
    p_radd = p_role_sub.add_parser("add", help="建立角色席位")
    p_radd.add_argument("--role", required=True,
                        choices=["PLANNER", "THINKER", "WORKER"])
    p_radd.add_argument("--short-name", required=True)
    p_radd.add_argument("--agent", default="opencode")
    p_radd.set_defaults(func=cmd_role_add)
    p_rlist = p_role_sub.add_parser("list", help="列出角色席位與綁定狀態")
    p_rlist.set_defaults(func=cmd_role_list)
    p_rrelease = p_role_sub.add_parser(
        "release", help="釋放席位（原本的 session 中斷時，讓新的 session 接管）")
    p_rrelease.add_argument("--role", required=True,
                            choices=["PLANNER", "THINKER", "WORKER"])
    p_rrelease.add_argument("--short-name", required=True)
    p_rrelease.add_argument("--reason", default="",
                            help="為什麼釋放（會寫進裁決日誌）")
    p_rrelease.set_defaults(func=cmd_role_release)
    p_role.set_defaults(func=lambda a: _err("usage", "role 要指定子命令"))

    p_review = sub.add_parser("review", help="結案復盤與退件紀錄")
    p_review_sub = p_review.add_subparsers(dest="action")
    p_impr = p_review_sub.add_parser("improvements", help="引擎改善建議")
    p_impr.set_defaults(func=cmd_review_improvements)
    p_impr_st = p_review_sub.add_parser(
        "improvement-status", help="標記某筆引擎改善建議的處理狀態")
    p_impr_st.add_argument("improvement_id", type=int)
    p_impr_st.add_argument("--status", required=True,
                           choices=["open", "done", "rejected"])
    p_impr_st.set_defaults(func=cmd_review_improvement_status)
    p_rej = p_review_sub.add_parser("rejections", help="退件 LOG")
    p_rej.add_argument("--limit", type=int, default=50)
    p_rej.set_defaults(func=cmd_review_rejections)
    p_review.set_defaults(func=lambda a: _err("usage", "review 要指定子命令"))

    p_session = sub.add_parser("session", help="session 綁定（host 憑證）")
    p_session_sub = p_session.add_subparsers(dest="action")
    p_bind = p_session_sub.add_parser("bind", help="綁定 session 到角色")
    p_bind.add_argument("--session-id", required=True)
    p_bind.add_argument("--role", required=True,
                        choices=["PLANNER", "THINKER", "WORKER"])
    p_bind.add_argument("--short-name", required=True)
    p_bind.add_argument("--agent", default="opencode")
    p_bind.add_argument("--workdir", default="")
    p_bind.set_defaults(func=cmd_session_bind)
    p_slist = p_session_sub.add_parser("list", help="列出角色綁定")
    p_slist.set_defaults(func=cmd_session_list)
    p_srevoke = p_session_sub.add_parser("revoke", help="撤銷綁定")
    p_srevoke.add_argument("--session-id", required=True)
    p_srevoke.set_defaults(func=cmd_session_revoke)
    p_session.set_defaults(func=lambda a: _err("usage", "session 要指定子命令"))

    p_contract = sub.add_parser("contract", help="印出交件契約")
    p_contract.set_defaults(func=cmd_contract)

    _add_project_to_subparsers(parser)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except ProjectNotFound as exc:
        return _err(
            "project.not-found",
            "這個位置不是 FBF 專案（沒有 %s）：%s\n"
            "請用 `--project <專案根目錄>` 指向真正的專案，"
            "或先在該專案執行 `fbf.py join --root <專案根目錄>`。\n"
            "（**不會**在這個目錄憑空建立正本——那會讓指令讀寫到一份沒人認領的"
            "空資料庫，回報成功但實際上沒動到任何東西。）"
            % (store.DB_FILENAME, exc.root))
    except ProjectRootError as exc:
        return _err(exc.code, exc.message)
    except Exception as exc:
        return _err("INTERNAL", str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
