"""FBF ENGINE 的**常駐協調器**：持有 timer、持有租約、持有狀態機的唯一寫入權。

規格第 6 節：
> 一個常駐進程擁有內建 timer，定期執行 `tick`；也提供人類 CLI `engine tick --once` 作診斷。
> **MCP server 是該進程的介面，不是排程器本身。**

所以這裡的職責只有三件：
1. 取得並續租（寫入前驗租約世代——舊進程不得寫進新世代）；
2. 定期跑 `tick`；
3. 收到停止訊號時**放掉租約**，讓下一個進程立刻接手。

**這個模組不知道 MCP 怎麼連。** 它只是個有 timer 的協調器。
"""
from __future__ import annotations

import os
import signal
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import engine, store

#: 預設 tick 間隔（秒）
DEFAULT_INTERVAL_SECONDS = 30


class Resident:
    """常駐協調器。

    **跟著既有 `Coordinator` 跑**——不自己再開一條連線。
    理由：同一個專案同一時間只能有一個寫入者（租約），
    MCP 的工具呼叫與背景 timer 必須共用同一個協調器物件，
    否則兩個進程／兩條連線會互相搶租約。
    """

    def __init__(self, coordinator: Any, interval_seconds: int = DEFAULT_INTERVAL_SECONDS):
        self.interval = max(5, int(interval_seconds))
        self.coordinator = coordinator
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_report: Dict[str, Any] = {}

    @classmethod
    def for_project(cls, project_root: str, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                    owner: str = "", endpoint: str = "",
                    binary: str = "opencode") -> "Resident":
        """自己開一個協調器（給獨立行程用）。"""
        return cls(engine.Coordinator(project_root, owner=owner, endpoint=endpoint,
                                      binary=binary),
                   interval_seconds=interval_seconds)

    # ------------------------------------------------------------ 生命週期

    def start(self) -> bool:
        """啟動背景 tick 迴圈。

        **不要因為一開始拿不到租約就不啟動。**
        同一個專案可能同時有多個 MCP 行程（TUI 一個、CLI 每個 session 一個），
        先啟動的那個先拿到租約，其餘的拿不到。
        如果拿不到就放棄，那麼**持有者一掛掉（行程被關、機器重開），
        整個專案就沒有人推進了**——所有工單靜靜卡住、沒有任何錯誤。

        租約是 30 秒 TTL → 持有者死掉後，最多 30 秒就有人接手 ✓。
        """
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="fbf-tick", daemon=True)
        self._thread.start()
        return True

    def stop(self, close: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if close:
            self.coordinator.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # 每個執行緒用**自己的**連線（見 `Coordinator.conn`）——
                # 這把鎖只管同一條連線自己的一致性，不拿來做跨執行緒序列化。
                with self.coordinator.conn._fbf_lock:
                    if self._stop.is_set():
                        break
                    if not self.coordinator.generation:
                        # 還沒拿到租約 → **每輪都再試一次**：
                        # 持有者死掉後，這裡就接手（否則整個專案沒有人推進）。
                        if not self.coordinator.acquire():
                            self._stop.wait(self.interval)
                            continue
                    elif not self.coordinator.renew():
                        # 租約被別人拿走（世代換了）→ 放掉這一輪，下輪再搶
                        self.coordinator.generation = 0
                        self._stop.wait(self.interval)
                        continue
                    self.last_report = self.coordinator.tick()
            except sqlite3.DatabaseError as exc:
                if store.is_corrupted(exc):
                    # **正本損毀** → 立刻停下，不要繼續往壞掉的正本寫（DEC-003）。
                    self.last_report = {"ok": False, "error": "正本損毀：%s" % exc}
                    break
                # 暫時性資料庫錯誤（`database is locked` 等）不是損毀——
                # 記下來、下一輪再試，**不要讓整個協調器熄火**（DEC-003：
                # 損毀才停，暫時性錯誤要繼續）。
                self.last_report = {"ok": False,
                                    "error": "暫時性資料庫錯誤（下一輪重試）：%s" % exc}
            except Exception as exc:
                self.last_report = {"ok": False, "error": str(exc)}
            self._stop.wait(self.interval)

    def run_forever(self, on_tick: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        """前景執行（人類用 `fbf coordinator run`）。"""
        if not self.coordinator.acquire():
            raise RuntimeError("拿不到協調器租約：可能已有其他進程在跑")
        try:
            while True:
                report = self.coordinator.tick()
                self.last_report = report
                if on_tick:
                    on_tick(report)
                time.sleep(self.interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.coordinator.close()


def install_signal_handlers(resident: Resident) -> None:
    """讓 Ctrl-C / SIGTERM 會放掉租約，下一個進程可以立刻接手。"""

    def handler(signum: int, frame: Any) -> None:
        resident.stop()
        raise SystemExit(0)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
