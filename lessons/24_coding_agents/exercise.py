"""第 24 课练习：亲手打造编码 Agent 的 ACI（Agent-Computer Interface）地基。

四道题，每道都对应 aci_tools.py 里的一个工具：
  (a) view_window           open_file 的核心：带行号的窗口化查看（处理文件开头 / 结尾 / 空文件）
  (b) safe_path             所有工具的第一道关：绝对路径、".."、符号链接都不能逃出工作区
  (c) apply_edit_with_lint  edit 的核心：按行号替换 + ast.parse 语法检查，失败时原样返回
  (d) is_test_file          测试保护规则（可选）：哪些文件 Agent 不许改

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=24
    # 或者：.venv/bin/python -m pytest lessons/24_coding_agents -v
    # 跳过可选题 (d)：.venv/bin/python -m pytest lessons/24_coding_agents -k "not is_test_file"

做完之后重跑 demo（python lessons/24_coding_agents/demo.py --offline），开头会显示哪些工具用上了你的实现。
卡住了？先重读 README 第 2 节，再看 solution.py。
"""

from __future__ import annotations

import ast  # noqa: F401  (c) 会用到
from pathlib import Path, PurePosixPath  # noqa: F401  (b)(d) 会用到

# =====================================================================
# (a) view_window：带行号的窗口
# =====================================================================


def view_window(lines: list[str], center: int, window: int = 50) -> str:
    """返回文件的一个"窗口"：以 center 为中心的 window 行，每行带行号。

    为什么不一次把整个文件给模型？SWE-agent 的消融实验里，"显示整个文件"比"每次 100 行"的成功率
    低了 5.3 个百分点（18.0% → 12.7%）：文件太长会淹没重点、浪费上下文；窗口太小（30 行）又得反复翻页。

    参数：
        lines   文件内容按行拆开（不含换行符），即 text.splitlines() 的结果
        center  希望居中显示的行号（从 1 开始）
        window  窗口大小（行数）

    规则（测试会逐条检查）：
      1. 每行的格式是 f"{行号}: {内容}"，行号从 1 开始，例如 "12:     return total"
      2. 尽量以 center 居中：start = center - window // 2，end = start + window - 1
      3. 贴边处理：start 不能小于 1；end 不能超过总行数；
         如果碰到文件末尾导致不足 window 行，窗口整体上移：start = max(1, end - window + 1)
      4. center 超出 [1, 总行数] 时先夹回这个范围（比如 center=999 当作最后一行）
      5. 窗口上方还有行时，第一行输出 f"（上面还有 {N} 行）"；下方还有行时，最后一行输出 f"（下面还有 {N} 行）"
         （注意是中文全角括号）
      6. 空文件（lines 为空列表）返回 "（空文件）"
      7. window < 1 抛 ValueError
      8. 各行之间用 "\\n" 连接，结尾不加换行

    例：lines 有 120 行，center=60，window=10 → 显示第 55–64 行，
        上面是 "（上面还有 54 行）"，下面是 "（下面还有 56 行）"。
    """
    raise NotImplementedError("TODO: (a) 实现 view_window")


# =====================================================================
# (b) safe_path：把路径限制在工作区根目录之内
# =====================================================================


def safe_path(root: str | Path, user_path: str) -> Path:
    """把模型给的路径解析成工作区里的真实路径；任何逃出工作区的企图都要拒绝。

    模型给的路径是**不可信输入**：它可能写错，也可能被仓库里的恶意文件（提示词注入）诱导去读
    ~/.ssh/id_rsa 或 ../../.env。所以所有文件类工具的第一步都是 safe_path。

    参数：
        root       工作区根目录
        user_path  模型传来的路径（可能是相对路径、绝对路径、带 ".." 的路径，或指向外部的符号链接）

    返回：
        解析后的绝对路径（Path，已经 resolve 过）。路径指向的文件**可以不存在**（比如要新建的文件）。

    规则：
      1. 相对路径相对 root 解析；"pkg/../a.py" 这种最终仍在 root 里的路径是允许的
      2. 最终路径必须是 root 本身或在 root 之下，否则抛 PermissionError：
         - "../secret.txt"、"pkg/../../x" 这类用 ".." 逃出去的
         - "/etc/passwd" 这类指向工作区外的绝对路径（指向工作区**内**的绝对路径是允许的）
         - 工作区里的符号链接指向外部（"link_to_outside/x.txt"）
      3. 路径里含空字符 "\\x00" 时抛 ValueError

    提示：
      - 先 (root / user_path) 再 .resolve()：resolve 会展开 ".." 并跟随符号链接，得到"真正会被打开的文件"
      - root 自己也要 resolve（macOS 的 /var 其实是 /private/var 的符号链接，不 resolve 的话比较会出错）
      - Path.is_relative_to(other) 可以判断"是否在某目录之下"
      - 只检查字符串里有没有 ".." 是不够的：符号链接根本不含 ".."
    """
    raise NotImplementedError("TODO: (b) 实现 safe_path")


# =====================================================================
# (c) apply_edit_with_lint：替换 + 语法检查，失败原样返回
# =====================================================================


def apply_edit_with_lint(source: str, start: int, end: int, replacement: str) -> tuple[str, str | None]:
    """把 source 的第 start 到第 end 行（从 1 开始，**包含** end）替换成 replacement，并用 ast.parse 检查语法。

    为什么要在编辑时就检查语法？SWE-agent 论文发现，编辑引入语法错误后，模型经常在同一段代码上
    反复修改、越改越乱（论文称之为"连锁的失败编辑"）；去掉 edit 的语法检查，成功率从 18.0% 降到 15.0%。
    把错误挡在写入之前，文件永远保持"可解析"，模型只需要重试这一次编辑。

    返回：
        成功：(新文本, None)
        语法错误：(原文 source, 错误信息)。错误信息格式：f"第 {lineno} 行：{msg}"
                  lineno、msg 取自 SyntaxError 的 .lineno 和 .msg

    规则：
      1. 行号从 1 开始；替换范围包含 start 和 end 两行
      2. 纯插入：end == start - 1，表示把 replacement 插到第 start 行之前（start 最大可以是 行数 + 1，即追加到末尾）
      3. 删除：replacement 传空字符串 ""，表示删掉第 start–end 行
      4. replacement 非空且不以 "\\n" 结尾时，自动补一个 "\\n"
      5. source 最后一行没有换行符时，先补上 "\\n"（否则在末尾追加会和最后一行粘在一起）
      6. 行号不合法（start < 1、start > 行数 + 1、end < start - 1、end > 行数）→ 抛 ValueError
         （这是"调用方用错了"，和"改出了语法错误"是两类问题）
      7. 语法错误时返回的必须是**原文**，一个字符都不能变

    例：
        apply_edit_with_lint("a = 1\\nb = 2\\nc = 3\\n", 2, 2, "b = 20")
        → ("a = 1\\nb = 20\\nc = 3\\n", None)
        apply_edit_with_lint("a = 1\\n", 1, 1, "a = (1")
        → ("a = 1\\n", "第 1 行：'(' was never closed")
    """
    raise NotImplementedError("TODO: (c) 实现 apply_edit_with_lint")


# =====================================================================
# (d) is_test_file：测试保护规则（可选）
# =====================================================================


def is_test_file(path: str) -> bool:
    """判断一个（相对工作区的）路径是不是"测试文件或测试配置"，这些文件 Agent 不许改。

    为什么要保护？编码 Agent 的奖励信号是"测试通过"。改测试、删测试、在配置里把失败的用例过滤掉，
    都能让测试"变绿"，但 bug 还在 —— 这就是奖励投机（reward hacking）。

    规则（满足任意一条即为 True）：
      1. 路径中任何一级**目录**名是 tests / test / testing（例："tests/helpers.py"、"pkg/test/data.py"）
      2. 文件名形如 test_*.py、*_test.py，或者正好是 test.py / tests.py
      3. 文件名是测试配置：conftest.py、pytest.ini、tox.ini、setup.cfg、pyproject.toml、noxfile.py
         （这些文件能跳过或过滤用例，改它们等于改测试）
      4. 大小写不敏感；Windows 风格的反斜杠 "tests\\\\test_a.py" 也要识别

    反例（都应返回 False）："pricing.py"、"src/contest.py"、"latest.py"、"testing_utils.py"、"docs/testing.md"
    """
    raise NotImplementedError("TODO: (d) 实现 is_test_file（可选）")
