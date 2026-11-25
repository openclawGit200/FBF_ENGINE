"""**發行前衛生守門**：這個套件要能獨立散布，所以包裡不能殘留任何
「這個機器上／那個專案裡」的東西。

守的東西：
1. **絕對路徑與個人帳號**——散布到別台機器就會失效，也會洩漏環境。
2. **舊架構的名稱**（舊正本檔名、舊全域索引、舊表格名、包外模組路徑）。
3. **失效的套件內路徑**（例如搬過家的目錄名）。
4. **包裡只能有現行規格**——設計歷史不隨包走（見 `doc/README.md`）。
5. **文件引用的套件檔案必須真的存在**——避免指向空氣的路徑。
6. `__pycache__/` 不得進包（`.gitignore` 必須排除它）。

**這個檔案本身跳過掃描**：它必須寫出那些禁用字樣才能守它們。
"""
from __future__ import annotations

import io
import os
import re
import unittest

#: 套件根目錄（本檔的上上一層）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 掃描時跳過的目錄。
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
             ".codegraph"}
SKIP_FILES = {os.path.basename(__file__), "HANDOFF.md", "README.original.local.md"}

#: 不得出現的字樣。**大小寫不敏感**，且同時擋 `/` 與 `\` 兩種分隔符——
#: 只擋正斜線的話，Windows 寫法的 `impl\\engine.py` 會漏掉。
FORBIDDEN = (
    # 磁碟代號前面必須不是英數字元，才不會把 `https://` 的 `s:/` 當成磁碟路徑
    ("絕對路徑（Windows 磁碟）", r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    ("絕對路徑（POSIX 家目錄）", r"/(?:Users|home)/[A-Za-z0-9._-]+"),
    ("舊正本檔名", r"fbflow\.db"),
    ("舊全域索引檔名", r"registry\.db"),
    ("舊表格名", r"escalations"),
    ("包外模組路徑", r"\bimpl[\\/]"),
    ("失效套件路徑", r"docs[\\/]V2[\\/]"),
    ("其他專案的工具名", r"\bORCA\b"),
)

#: 文件裡反引號包住的字串——可能是套件內的路徑。
_QUOTED = re.compile(r"`([^`\n]{2,160})`")


def _read(path: str) -> str:
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _rel(path: str) -> str:
    """相對套件根的路徑，**分隔符一律正斜線**（Windows 的反斜線會讓比對失敗）。"""
    return os.path.relpath(path, ROOT).replace("\\", "/")


def _is_binary(path: str) -> bool:
    """二進位檔不掃（`.pyc` 與資料庫裡的字串不算文件內容）。"""
    with open(path, "rb") as fh:
        return b"\x00" in fh.read(4096)


def package_files():
    """套件裡全部的文字檔（跳過快取、二進位與本檔）。

    **不設副檔名白名單**：新增一種副檔名不該讓它逃過掃描。
    """
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name in SKIP_FILES:
                continue
            path = os.path.join(base, name)
            if _is_binary(path):
                continue
            out.append((path, _read(path)))
    return out


def top_level_names():
    """套件根目錄下的檔案與目錄名（判斷「包內路徑」用）。"""
    return set(os.listdir(ROOT))


def referenced_package_paths(text: str):
    """文件裡提到、而且看起來是**套件內**路徑的字串。"""
    found = []
    for raw in _QUOTED.findall(text):
        if any(ch in raw for ch in " \t*?<>|"):
            continue
        if raw.startswith(("~", "$", "http", "mcp://", "/")):
            continue
        head = raw.replace("\\", "/").split("/")[0]
        if head in top_level_names():
            found.append(raw)
    return sorted(set(found))


class TestPackageHygiene(unittest.TestCase):

    def test_no_absolute_paths_or_personal_names(self):
        """散布到別台機器要能用——不能綁死某台機器的路徑或帳號。"""
        offenders = []
        for path, text in package_files():
            rel = os.path.relpath(path, ROOT)
            for label, pattern in FORBIDDEN:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    offenders.append("%s → %s（%r）" % (rel, label, match.group(0)))
            # 檔名本身也要乾淨（有人可能直接拿舊名字當檔名）
            for label, pattern in FORBIDDEN:
                match = re.search(pattern, os.path.basename(path), re.IGNORECASE)
                if match:
                    offenders.append("%s（檔名）→ %s（%r）" % (rel, label, match.group(0)))
        self.assertEqual(offenders, [], "包內有不得出現的字樣：\n" + "\n".join(offenders))

    def test_referenced_package_files_exist(self):
        """文件提到的套件檔案必須真的存在——不要指向空氣。"""
        missing = []
        for path, text in package_files():
            if not path.endswith(".md"):
                continue
            for ref in referenced_package_paths(text):
                if not os.path.exists(os.path.join(ROOT, ref)):
                    missing.append("%s → %s" % (os.path.relpath(path, ROOT), ref))
        self.assertEqual(missing, [], "文件指向不存在的檔案：\n" + "\n".join(missing))

    def test_doc_folder_only_holds_the_current_spec(self):
        """包裡只留現行規格——設計歷史不隨包提供。

        **遞迴檢查**：只列一層會漏掉子目錄裡偷渡進來的舊文件。
        """
        doc = os.path.join(ROOT, "doc")
        found = []
        for base, dirs, files in os.walk(doc):
            for name in dirs:
                found.append(_rel(os.path.join(base, name)) + "/")
            for name in files:
                found.append(_rel(os.path.join(base, name)))
        self.assertEqual(sorted(found),
                         ["doc/README.md", "doc/spec-fbflow-v2-first-principles.md"],
                         "doc/ 只能有現行規格與索引（連子目錄都不行）")

    def test_document_folders_have_known_contents(self):
        """決策紀錄與研究資料夾的內容是明定的——不要偷偷長出新目錄。"""
        decisions = os.path.join(ROOT, "docs")
        found = []
        for base, dirs, files in os.walk(decisions):
            for name in dirs:
                found.append(_rel(os.path.join(base, name)) + "/")
            for name in files:
                found.append(_rel(os.path.join(base, name)))
        self.assertEqual(sorted(found), ["docs/decisions.md"],
                         "docs/ 只能有決策紀錄")
        research = os.path.join(ROOT, "research")
        self.assertTrue(os.path.isdir(research), "缺少 research/")
        self.assertIn("README.md", os.listdir(research),
                      "research/ 必須有說明它放什麼的 README")

    def test_spec_references_only_in_package_modules(self):
        """規格提到的模組都必須真的在包裡。"""
        spec = _read(os.path.join(ROOT, "doc", "spec-fbflow-v2-first-principles.md"))
        refs = sorted(set(re.findall(r"fbf_engine/[a-zA-Z_]+\.py", spec)))
        self.assertTrue(refs, "規格應該指名包內的模組")
        for ref in refs:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, ref)),
                            "規格提到 %s，但包裡沒有這個檔案" % ref)

    def test_gitignore_excludes_runtime_artifacts(self):
        """`__pycache__/` 與專案正本不得進包。

        **只認生效的規則**：被註解掉的行不算數——比對字串會讓
        「`# __pycache__/`」這種註解通過檢查。

        **這條不能用測試驗**：跑測試本身就會產生 `__pycache__/`。
        所以靠 `.gitignore` ＋ 散布前的目錄檢查（見 README）。
        """
        ignore = os.path.join(ROOT, ".gitignore")
        self.assertTrue(os.path.isfile(ignore), "缺少 .gitignore")
        rules = [line.strip() for line in _read(ignore).splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        for rule in ("__pycache__/", "*.py[cod]", "*.db"):
            self.assertIn(rule, rules, ".gitignore 缺少生效的規則：%s" % rule)

    def test_codegraph_index_is_not_shipped(self):
        """本機程式索引不隨包散布。

        **索引是每台機器各建各的**：裡面的資料庫檔很大，且會隨著
        原始碼變動而過期。散布出去只會讓接手的人拿到一份錯的索引。
        """
        ignore = os.path.join(ROOT, ".gitignore")
        rules = [line.strip() for line in _read(ignore).splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        self.assertIn(".codegraph/", rules,
                      ".gitignore 缺少生效的規則：.codegraph/")

    def test_skipped_dirs_are_actually_skipped(self):
        """跳過清單要真的擋掉掃描——不然索引內容會被當文件內容掃。

        **用 `__pycache__/` 當代理**：它一定會被建立（跑測試就有），
        而且裡面的 `.pyc` 是二進位，本來就會被 `_is_binary` 擋掉——
        真正會漏的是同目錄下的**文字檔**。所以放一個文字檔進去，
        確認掃描時看不到它。
        """
        probe_dir = os.path.join(ROOT, "__pycache__")
        probe = os.path.join(probe_dir, "_hygiene_probe.txt")
        try:
            os.makedirs(probe_dir, exist_ok=True)
            with io.open(probe, "w", encoding="utf-8") as fh:
                fh.write("__pycache__/ 應該被跳過，不該被掃到\n")
            scanned = [rel for rel, _ in package_files()]
            self.assertFalse(any(rel.endswith("_hygiene_probe.txt") for rel in scanned),
                             "__pycache__/ 沒被跳過——探針檔被掃進來了")
        finally:
            if os.path.isfile(probe):
                os.remove(probe)


if __name__ == "__main__":
    unittest.main()
