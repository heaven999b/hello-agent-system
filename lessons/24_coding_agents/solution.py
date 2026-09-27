"""第 24 课练习参考答案。先自己做，再来对照。

四个函数都是 ACI（Agent-Computer Interface）的"地基"，aci_tools.py 里的工具就建立在它们之上：
  (a) view_window           open_file 的核心：带行号的窗口化查看
  (b) safe_path             所有工具的第一道关：路径必须留在工作区里
  (c) apply_edit_with_lint  edit 的核心：按行号替换 + 语法检查，失败原样返回
  (d) is_test_file          测试保护：哪些文件 Agent 不许改（可选）
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

# =====================================================================
# (a) view_window：带行号的窗口
# =====================================================================


def view_window(lines: list[str], center: int, window: int = 50) -> str:
    if window < 1:
        raise ValueError(f"window 必须 >= 1，收到 {window}")
    n = len(lines)
    if n == 0:
        return "（空文件）"
    center = min(max(center, 1), n)  # 越界的行号先夹回文件范围内
    start = max(1, center - window // 2)  # 尽量让 center 居中
    end = min(n, start + window - 1)
    start = max(1, end - window + 1)  # 碰到文件末尾：窗口整体上移，保证尽量显示满 window 行
    out: list[str] = []
    if start > 1:
        out.append(f"（上面还有 {start - 1} 行）")
    out.extend(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
    if end < n:
        out.append(f"（下面还有 {n - end} 行）")
    return "\n".join(out)


# =====================================================================
# (b) safe_path：把路径限制在工作区根目录之内
# =====================================================================


def safe_path(root: str | Path, user_path: str) -> Path:
    if not isinstance(user_path, str) or "\x00" in user_path:
        raise ValueError(f"非法路径：{user_path!r}")
    root_real = Path(root).resolve()
    # 关键一步：先拼接、再 resolve。
    #   - user_path 是绝对路径时，root / user_path 直接等于 user_path（pathlib 的规则），下面的检查会拦住它；
    #   - resolve() 会展开 ".." 并跟随符号链接，拿到"真正会被打开的那个文件"的路径。
    # 只做字符串检查（比如 "'..' in user_path"）是不够的：符号链接根本不含 ".."。
    candidate = (root_real / user_path).resolve()
    if candidate != root_real and not candidate.is_relative_to(root_real):
        raise PermissionError(f"路径 {user_path!r} 超出了工作区（{root_real}），拒绝访问")
    return candidate


# =====================================================================
# (c) apply_edit_with_lint：替换 + 语法检查，失败原样返回
# =====================================================================


def apply_edit_with_lint(source: str, start: int, end: int, replacement: str) -> tuple[str, str | None]:
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"  # 最后一行没有换行符时补上，否则在文件末尾追加会和最后一行粘在一起
    n = len(lines)
    if start < 1 or start > n + 1 or end < start - 1 or end > n:
        # 行号不合法是"调用方用错了"，和"改出了语法错误"是两类问题，所以用异常而不是返回错误字符串
        raise ValueError(f"行号范围不合法：start={start}, end={end}（文件共 {n} 行；插入请用 end = start - 1）")
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    new_source = "".join(lines[: start - 1]) + replacement + "".join(lines[end:])
    try:
        ast.parse(new_source)
    except (SyntaxError, ValueError) as e:  # IndentationError / TabError 都是 SyntaxError 的子类
        lineno = getattr(e, "lineno", None) or "?"
        msg = getattr(e, "msg", None) or str(e)
        return source, f"第 {lineno} 行：{msg}"
    return new_source, None


# =====================================================================
# (d) is_test_file：测试保护规则（可选）
# =====================================================================

TEST_DIR_NAMES = {"tests", "test", "testing"}
# 改这些文件同样能让测试"变绿"：conftest.py 里可以跳过用例，pytest.ini / pyproject.toml 里可以加 -k 过滤，
# 所以它们和测试文件一样要保护。宁可误拦，也别漏掉 —— 被误拦时交给人审批即可。
TEST_CONFIG_FILES = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py"}


def is_test_file(path: str) -> bool:
    parts = [p.lower() for p in PurePosixPath(path.replace("\\", "/")).parts if p not in ("", ".", "/")]
    if not parts:
        return False
    name = parts[-1]
    if any(d in TEST_DIR_NAMES for d in parts[:-1]):
        return True
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py") or name in ("test.py", "tests.py")):
        return True
    return name in TEST_CONFIG_FILES
