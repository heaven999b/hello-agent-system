#!/usr/bin/env python3
"""学习进度看板：逐课运行你的练习测试，告诉你学到哪了、下一步该做什么。

    .venv/bin/python scripts/progress.py             # 彩色输出
    .venv/bin/python scripts/progress.py --no-color  # 纯文本（也支持 NO_COLOR 环境变量）

判定规则（针对你写的 exercise.py，不会使用参考答案）：
    ✅ 完成     该课所有测试通过
    🚧 进行中   有测试通过，或者失败原因不是"还没实现"
    ⬜ 未开始   所有测试都失败，且失败原因都是 NotImplementedError（TODO 还没动）
    📖 阅读课   该课没有练习测试
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 180


# ---------------------------------------------------------------------------- 运行与解析


@dataclass
class Lesson:
    num: str
    slug: str
    title: str
    path: Path
    test_file: Path | None
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    not_implemented: int = 0  # 因 NotImplementedError 失败的测试数
    failing: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed

    @property
    def status(self) -> str:
        if self.test_file is None:
            return "reading"
        if self.total == 0:
            return "wip" if self.note else "reading"
        if self.passed == self.total:
            return "done"
        if self.passed == 0 and self.not_implemented == self.failed:
            return "todo"
        return "wip"


def discover(root: Path) -> list[Lesson]:
    lessons_dir = root / "lessons"
    if not lessons_dir.is_dir():
        return []
    out = []
    for d in sorted(p for p in lessons_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        m = re.match(r"(\d+)[_-]?(.*)", d.name)
        num, slug = (m.group(1), m.group(2)) if m else (d.name, d.name)
        test_file = d / "test_exercise.py"
        out.append(Lesson(num, slug, lesson_title(d, slug), d, test_file if test_file.is_file() else None))
    return out


def lesson_title(lesson_dir: Path, slug: str) -> str:
    """从 README 第一行标题里取课程名：'# 第 02 课：Agent 主循环' → 'Agent 主循环'。"""
    readme = lesson_dir / "README.md"
    if readme.is_file():
        try:
            for line in readme.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    title = re.sub(r"^第\s*\d+\s*课\s*[：:]\s*", "", title)
                    return title or slug
        except (OSError, UnicodeDecodeError):
            pass
    return slug.replace("_", " ")


def run_tests(lesson: Lesson, root: Path, timeout: float) -> None:
    """在子进程里跑该课的 test_exercise.py，用 JUnit XML 拿到结构化结果。"""
    env = dict(os.environ)
    env.pop("AGENTKIT_SOLUTION", None)  # 永远检查学员自己的 exercise.py
    env.setdefault("PYTHONIOENCODING", "utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        cmd = [sys.executable, "-m", "pytest", str(lesson.test_file), "-q", "-p", "no:cacheprovider",
               f"--junitxml={report}"]
        try:
            proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            lesson.failed, lesson.note = 1, f"测试运行超过 {timeout:.0f} 秒（死循环？）"
            return
        if report.is_file():
            try:
                parse_junit(lesson, report)
                return
            except ET.ParseError:
                pass
        parse_summary(lesson, proc.stdout + proc.stderr)


def parse_junit(lesson: Lesson, report: Path) -> None:
    for case in ET.parse(report).getroot().iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            lesson.failed += 1
            text = (problem.get("message") or "") + "\n" + (problem.text or "")
            if "NotImplementedError" in text:
                lesson.not_implemented += 1
            name = case.get("name") or "?"
            if problem.get("message", "").startswith("collection failure") or not case.get("classname"):
                name = "（测试文件加载失败）"
                if "NotImplementedError" not in text:
                    lesson.note = "测试文件无法加载（exercise.py 有语法错误或导入失败？）"
            lesson.failing.append(name)
        elif case.find("skipped") is not None:
            lesson.skipped += 1
        else:
            lesson.passed += 1


def parse_summary(lesson: Lesson, output: str) -> None:
    """兜底：没有 JUnit 报告时解析 pytest 的摘要行，如 '2 failed, 3 passed in 0.1s'。"""
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", output)}
    lesson.passed = counts.get("passed", 0)
    lesson.failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    lesson.skipped = counts.get("skipped", 0)
    if lesson.total == 0:
        lesson.note = "pytest 没有产出结果，请手动运行查看报错"
        lesson.failed = 1


# ---------------------------------------------------------------------------- 输出


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        table = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
                 "magenta": "35", "cyan": "36", "gray": "90"}
        return "\033[" + ";".join(table[c] for c in codes) + "m" + text + "\033[0m"


def width(text: str) -> int:
    """终端显示宽度：中文和 emoji 占 2 列，组合字符 / 变体选择符占 0 列。"""
    w = 0
    for ch in text:
        if unicodedata.combining(ch) or ch in "‍︎️":
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def fit(text: str, cols: int) -> str:
    """截断并用空格补齐到指定显示宽度。"""
    if width(text) > cols:
        while text and width(text + "…") > cols:
            text = text[:-1]
        text += "…"
    return text + " " * (cols - width(text))


STATUS = {
    "done": ("✅ 完成", "green"),
    "wip": ("🚧 进行中", "yellow"),
    "todo": ("⬜ 未开始", "gray"),
    "reading": ("📖 阅读课", "blue"),
}


def progress_bar(ratio: float, cols: int, s: Style) -> str:
    filled = round(ratio * cols)
    return s("█" * filled, "green") + s("░" * (cols - filled), "gray")


def render(lessons: list[Lesson], root: Path, s: Style) -> str:
    name_cols, count_cols = 30, 9
    rule = s("─" * (4 + name_cols + count_cols + 14), "gray")
    lines = ["", "  " + s("Hello Agent System · 学习进度", "bold", "cyan"), rule]
    lines.append("  " + s(fit("课程", name_cols + 4) + "通过/总数".rjust(count_cols - 4) + "   状态", "bold"))
    lines.append(rule)
    for les in lessons:
        label, color = STATUS[les.status]
        count = "—" if les.status == "reading" else f"{les.passed}/{les.total}"
        count_cell = " " * (count_cols - width(count)) + count
        row = "  " + s(fit(les.num, 4), "bold") + fit(les.title, name_cols) + count_cell + "   " + s(label, color)
        if les.note:
            row += s("  " + les.note, "red")
        lines.append(row)
    lines.append(rule)

    graded = [les for les in lessons if les.status != "reading"]
    passed = sum(les.passed for les in graded)
    total = sum(les.total for les in graded)
    done = sum(les.status == "done" for les in graded)
    ratio = passed / total if total else 0.0
    lines.append(
        "  总进度 " + progress_bar(ratio, 24, s) + s(f" {ratio:4.0%}", "bold")
        + s(f"   {passed}/{total} 个测试通过 · {done}/{len(graded)} 课完成", "gray")
    )
    lines.append("")

    nxt = next((les for les in graded if les.status != "done"), None)
    if nxt is None and graded:
        capstone = root / "capstone" / "README.md"
        lines.append("  🎉 " + s("所有练习都通过了！", "bold", "green"))
        if capstone.is_file():
            lines.append(f"     下一步：综合项目  {capstone.relative_to(root).as_posix()}")
    elif nxt is not None:
        head = f"第 {nxt.num} 课 {nxt.title}"
        extra = "（还没开始）" if nxt.status == "todo" else f"（{nxt.passed}/{nxt.total} 通过）"
        lines.append("  👉 " + s("下一步：", "bold") + s(head, "bold", "cyan") + s(extra, "gray"))
        readme = nxt.path / "README.md"
        lines.append("     讲义  " + (nxt.path.relative_to(root) / "README.md").as_posix()
                     + ("" if readme.is_file() else s("（尚未编写）", "gray")))
        lines.append("     练习  " + (nxt.path.relative_to(root) / "exercise.py").as_posix())
        lines.append("     运行  " + s(f"make lesson N={nxt.num}", "green"))
        if nxt.status == "wip" and nxt.failing:
            shown = "、".join(nxt.failing[:3]) + (f" 等 {len(nxt.failing)} 个" if len(nxt.failing) > 3 else "")
            lines.append("     未通过  " + s(shown, "yellow"))
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="学习进度看板：逐课运行练习测试并给出下一步建议。")
    parser.add_argument("--no-color", action="store_true", help="不输出颜色（也可以设置 NO_COLOR 环境变量）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="仓库根目录（默认：本脚本所在仓库）")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S, help=f"每课测试的超时秒数（默认 {TIMEOUT_S}）")
    args = parser.parse_args(argv)

    try:  # Windows 控制台可能不是 UTF-8：宁可显示 ? 也不要崩溃
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    use_color = not args.no_color and "NO_COLOR" not in os.environ and (
        sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))
    )
    if use_color and os.name == "nt":
        os.system("")  # 让 Windows 终端启用 ANSI 转义序列
    s = Style(use_color)

    root = args.root.resolve()
    lessons = discover(root)
    if not lessons:
        print(f"没有在 {root / 'lessons'} 下找到课程目录。", file=sys.stderr)
        return 1

    live = sys.stderr.isatty()
    for les in lessons:
        if les.test_file is None:
            continue
        if live:
            sys.stderr.write(f"\r  正在检查 第 {les.num} 课 {les.title} …\033[K")
            sys.stderr.flush()
        run_tests(les, root, args.timeout)
    if live:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    print(render(lessons, root, s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
