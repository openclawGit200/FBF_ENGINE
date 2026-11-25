"""FBF ENGINE 的測試套件。

**測試輸出本身也固定 UTF-8**：Windows 預設主控台編碼是 cp950，
用工具讀測試結果（agent、CI log）時中文測試名會變成亂碼。
輸出格式不該由字碼頁決定。
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is None:
        continue
    try:
        _reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass
