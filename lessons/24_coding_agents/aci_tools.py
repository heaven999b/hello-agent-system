"""第 24 课：SWE-agent 风格的 ACI（Agent-Computer Interface，智能体-计算机接口）工具集。

五个工具，全部被限制在工作区根目录（workspace root）之内：

    open_file(path, line)                 带行号的窗口化查看，每次 50 行，告诉模型"上面/下面还有几行、怎么继续翻"
    search(pattern, path)                 全仓纯文本搜索：结果截断 + 按文件汇总，太多时提示换更具体的关键词
    edit(path, start, end, replacement)   按行号替换；改完做语法检查，不通过就撤销，并把"改成了什么样 / 原来什么样"都告诉模型
    run_tests()                           在子进程里跑 pytest：有超时、环境变量白名单（不带密钥），输出压缩成摘要
    submit()                              交卷：先跑测试（必须全绿），再生成 diff 交给审查

再加三道护栏：

    测试保护      edit 拒绝写测试文件 / 测试配置；run_tests 前校验测试文件哈希，被改过就从基线恢复
    SubmitReview  submit 之前审查 diff：新增代码里出现测试里的"魔法数字"、跳过测试、提前退出进程……一律打回
    LoopGuard     同一个动作（工具名 + 参数完全相同）连续重复 N 次就拒绝；拒绝太多次直接中止运行

四个基础函数（view_window / safe_path / apply_edit_with_lint / is_test_file）是本课的练习，
Workspace 通过 helpers 参数使用它们：默认用 solution.py 的参考实现，demo 会优先换成你在 exercise.py 里的实现。
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Annotated, Callable

from pydantic import Field

from agentkit import Hook, StopRun, Tool, ToolError

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "toy_repo"

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".harness_snapshots", ".venv", "venv"}
# run_tests 子进程能看到的环境变量白名单。父进程里的 LLM_API_KEY、云厂商密钥等一律不传：
# Agent 能改代码，代码能读环境变量，测试输出又会回到模型的上下文里 —— 这就是一条现成的密钥泄露通道。
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "HOME")
SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（课程目录名以数字开头，不能写普通 import）。用"目录名__模块名"注册，全进程只有一份。"""
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def default_helpers() -> ModuleType:
    """四个基础函数的参考实现（solution.py）。"""
    return _load_sibling("solution")


# =====================================================================
# 小工具
# =====================================================================


def copy_template(dest: str | Path | None = None, template: Path = TEMPLATE) -> Path:
    """把玩具仓库模板复制到一个全新的临时目录。**Agent 永远只在副本上工作，模板本身绝不修改。**"""
    import shutil

    dest = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="lesson24_"))
    shutil.copytree(template, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dest.resolve()


def sandbox_env() -> dict[str, str]:
    """给子进程的最小环境变量：只放白名单里的，再关掉 .pyc 写入（保持工作区干净，git status 不出现噪音）。"""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def secret_like_env_names() -> list[str]:
    """父进程里"看起来像密钥"的环境变量名（只返回名字，绝不返回值）。demo 用它说明 sandbox_env 挡住了什么。"""
    return sorted(k for k in os.environ if SECRET_NAME_RE.search(k))


def iter_files(root: Path, start: Path | None = None):
    """遍历工作区里的普通文件，跳过隐藏目录、缓存目录。"""
    start = start or root
    if start.is_file():
        yield start
        return
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith(".") and not name.endswith((".pyc", ".pyo")):
                yield Path(dirpath) / name


def read_tree(root: Path) -> dict[str, str]:
    """{相对路径: 文本内容}，跳过二进制和大文件。用作 diff 的基线。"""
    out: dict[str, str] = {}
    for p in iter_files(root):
        if p.stat().st_size > 1_000_000:
            continue
        try:
            out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return out


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def splice(source: str, start: int, end: int, replacement: str) -> str:
    """不做语法检查的行替换（给非 .py 文件用）。规则与 apply_edit_with_lint 相同。"""
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    if start < 1 or start > len(lines) + 1 or end < start - 1 or end > len(lines):
        raise ValueError(f"行号范围不合法：start={start}, end={end}（文件共 {len(lines)} 行；插入请用 end = start - 1）")
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    return "".join(lines[: start - 1]) + replacement + "".join(lines[end:])


def window_bounds(n: int, center: int, window: int) -> tuple[int, int]:
    """与 view_window 相同的窗口计算，用来生成"下一页"的提示。"""
    if n == 0:
        return 1, 0
    center = min(max(center, 1), n)
    start = max(1, center - window // 2)
    end = min(n, start + window - 1)
    return max(1, end - window + 1), end


# =====================================================================
# 运行测试：子进程 + 超时 + 干净环境 + 结构化摘要
# =====================================================================


@dataclass
class TestRun:
    __test__ = False  # 告诉 pytest：这不是测试类

    passed: int = 0
    failed: int = 0
    errors: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (测试名, 断言信息)
    seconds: float = 0.0
    timed_out: bool = False
    raw_tail: str = ""  # 解析失败时保留的原始输出末尾

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.failed == 0 and self.errors == 0 and self.passed > 0

    def headline(self) -> str:
        if self.timed_out:
            return "超时"
        parts = [f"{self.passed} passed"]
        if self.failed:
            parts.insert(0, f"{self.failed} failed")
        if self.errors:
            parts.append(f"{self.errors} errors")
        return ", ".join(parts)


def run_pytest(root: Path, targets: list[str], timeout: float = 30.0) -> TestRun:
    """在独立子进程里运行 pytest，用 JUnit XML 拿结构化结果（比解析终端输出可靠得多）。

    为什么是子进程而不是在当前进程里 import 测试？
      1. 超时后能真正杀掉（subprocess.run 超时会 kill 子进程）；线程做不到（见 agentkit/tools.py 的注释）；
      2. 被测代码的死循环、sys.exit、猴子补丁都影响不到 Agent 进程本身；
      3. 环境变量走白名单，密钥不会进入被测代码的视野。
    """
    fd, xml_path = tempfile.mkstemp(prefix="lesson24_junit_", suffix=".xml")  # 报告写在工作区外，不污染仓库
    os.close(fd)
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", f"--junitxml={xml_path}", *targets]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=root, env=sandbox_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.unlink(xml_path)
        return TestRun(timed_out=True, seconds=time.time() - t0)
    run = TestRun(seconds=time.time() - t0)
    try:
        tree = ET.parse(xml_path)
        for case in tree.iter("testcase"):
            outcome = next((c for c in case if c.tag in ("failure", "error", "skipped")), None)
            name = case.get("name", "?")
            if outcome is None:
                run.passed += 1
            elif outcome.tag == "skipped":
                continue
            else:
                msg = (outcome.get("message") or outcome.text or "").strip().splitlines()
                brief = " ｜ ".join(line.strip()[:160] for line in msg[:2])
                run.failures.append((name, brief))
                if outcome.tag == "failure":
                    run.failed += 1
                else:
                    run.errors += 1
    except (ET.ParseError, FileNotFoundError):
        run.errors += 1
        run.raw_tail = (proc.stdout + proc.stderr)[-1500:]
    finally:
        if os.path.exists(xml_path):
            os.unlink(xml_path)
    if run.passed + run.failed + run.errors == 0:  # 一个测试都没收集到（导入错误、路径写错……）
        run.errors += 1
        run.raw_tail = run.raw_tail or (proc.stdout + proc.stderr)[-1500:]
    return run


# =====================================================================
# diff 审查：找"为了让测试变绿而作弊"的痕迹
# =====================================================================

CHEAT_PATTERNS = [
    (re.compile(r"pytest\.skip|pytest\.mark\.skip|unittest\.skip|pytest\.xfail"), "跳过或标记测试为预期失败"),
    (re.compile(r"sys\.exit\(|os\._exit\("), "提前退出进程（测试框架可能把它当成通过）"),
    (re.compile(r"PYTEST_CURRENT_TEST|inspect\.stack|sys\._getframe|sys\.modules\[.pytest"), "探测自己是否在测试里运行"),
    (re.compile(r"__eq__\s*=|def __eq__"), "重载相等比较（让 assert == 永远成立）"),
]
NUMBER_RE = re.compile(r"(?<![\w.])\d{4,}(?![\w.])")  # 4 位以上的整数字面量


def review_diff(baseline: dict[str, str], current: dict[str, str], is_test_file: Callable[[str], bool]) -> list[str]:
    """启发式审查：返回可疑点列表（空列表 = 没发现问题）。

    这只是给人类审查员的"提示灯"，不是证明：它会误报（比如新增的常量恰好和测试里的数字相同），
    也会漏报（换个写法的特判它就看不出来）。真正的防线是：测试不可写 + 评估用 Agent 看不到的测试 + 人工审查。
    """
    findings: list[str] = []
    test_numbers = {m for rel, text in current.items() if is_test_file(rel) for m in NUMBER_RE.findall(text)}
    for rel in sorted(set(baseline) | set(current)):
        old, new = baseline.get(rel, ""), current.get(rel, "")
        if old == new:
            continue
        if is_test_file(rel):
            findings.append(f"{rel}：测试文件或测试配置被修改了")
            continue
        added = [line[1:] for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
                 if line.startswith("+") and not line.startswith("+++")]
        old_numbers = set(NUMBER_RE.findall(old))
        for line in added:
            code = line.split("#", 1)[0]
            for pattern, why in CHEAT_PATTERNS:
                if pattern.search(code):
                    findings.append(f"{rel}：{why}：{line.strip()[:100]}")
            magic = sorted({n for n in NUMBER_RE.findall(code) if n in test_numbers and n not in old_numbers})
            if magic:
                findings.append(f"{rel}：新增代码里出现了测试用例里的具体数值 {', '.join(magic)}，疑似针对测试输入写特判：{line.strip()[:100]}")
    return findings


def unified_diff(baseline: dict[str, str], current: dict[str, str]) -> str:
    chunks = []
    for rel in sorted(set(baseline) | set(current)):
        old, new = baseline.get(rel), current.get(rel)
        if old == new:
            continue
        chunks.extend(difflib.unified_diff(
            (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
            fromfile=f"a/{rel}" if old is not None else "/dev/null", tofile=f"b/{rel}" if new is not None else "/dev/null",
        ))
    return "".join(chunks)


# =====================================================================
# 工作区 + 五个工具
# =====================================================================


class Workspace:
    """一个编码 Agent 的"工位"：根目录、基线快照、受保护的文件、事件记录，以及绑定在它上面的五个工具。"""

    def __init__(
        self,
        root: str | Path,
        *,
        helpers: ModuleType | object | None = None,
        window: int = 50,
        max_search_results: int = 20,
        test_targets: list[str] | None = None,
        test_timeout: float = 30.0,
        protect_tests: bool = True,
        extra_protected: tuple[str, ...] = (),
        require_green_to_submit: bool = True,
    ):
        self.root = Path(root).resolve()
        self.h = helpers or default_helpers()
        self.window = window
        self.max_search_results = max_search_results
        self.test_targets = list(test_targets or [])
        self.test_timeout = test_timeout
        self.protect_tests = protect_tests
        self.extra_protected = set(extra_protected)
        self.require_green_to_submit = require_green_to_submit
        self.baseline = read_tree(self.root)  # 开工时的样子：diff 审查、测试恢复都以它为准
        self.protected_hashes = {rel: sha256(text) for rel, text in self.baseline.items() if self.is_protected(rel)}
        self.events: list[dict] = []  # 被拒绝的越界访问、改测试企图、测试文件被篡改……审查和审计都要看
        self.edits = 0
        self.last_run: TestRun | None = None
        self.submitted = False

    # ------------------------------------------------------------ 规则

    def is_protected(self, rel: str) -> bool:
        return rel in self.extra_protected or (self.protect_tests and self.h.is_test_file(rel))

    def _event(self, kind: str, **detail) -> None:
        self.events.append({"kind": kind, "at": time.time(), **detail})

    def _resolve(self, user_path: str) -> tuple[Path, str]:
        try:
            p = self.h.safe_path(self.root, user_path)
        except (PermissionError, ValueError) as e:
            self._event("path_refused", path=user_path)
            raise ToolError(f"拒绝：{e}。所有路径都必须在仓库根目录之内，请使用相对路径（如 pricing.py）。") from None
        rel = p.relative_to(self.root).as_posix() if p != self.root else "."
        return p, rel

    def current_tree(self) -> dict[str, str]:
        return read_tree(self.root)

    def diff(self) -> str:
        return unified_diff(self.baseline, self.current_tree())

    def review(self) -> list[str]:
        return review_diff(self.baseline, self.current_tree(), self.is_protected)

    # ------------------------------------------------------------ 工具 1：open_file

    def open_file(
        self,
        path: Annotated[str, Field(description="相对仓库根目录的文件路径，如 pricing.py 或 tests/test_pricing.py")],
        line: Annotated[int, Field(ge=1, description="窗口中心的行号（从 1 开始）。翻页时照抄上一次输出末尾提示的行号")] = 1,
    ) -> str:
        """打开一个文本文件，显示以 line 为中心的 50 行，每行前面带行号（edit 用的就是这些行号）。
        输出会告诉你上面/下面还有多少行，以及继续翻页该传的 line。"""
        p, rel = self._resolve(path)
        if p.is_dir():
            entries = sorted(q.relative_to(self.root).as_posix() for q in iter_files(self.root, p))[:30]
            raise ToolError(f"{rel} 是目录，不是文件。目录下的文件有：{', '.join(entries) or '（空）'}")
        if not p.exists():
            raise ToolError(f"文件 {rel} 不存在。可以用 search 按文件名或关键词查找。")
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            raise ToolError(f"{rel} 不是 UTF-8 文本文件（可能是二进制文件），无法显示。") from None
        start, end = window_bounds(len(lines), line, self.window)
        out = [f"[文件：{rel}（共 {len(lines)} 行，显示第 {start}-{end} 行）]", self.h.view_window(lines, line, self.window)]
        if end < len(lines):
            out.append(f'（继续往下看：open_file("{rel}", line={min(len(lines), end + 1 + self.window // 2)})）')
        return "\n".join(out)

    # ------------------------------------------------------------ 工具 2：search

    def search(
        self,
        pattern: Annotated[str, Field(min_length=1, description="要查找的文本（区分大小写的纯文本匹配，不是正则），如 def subtotal")],
        path: Annotated[str, Field(description="在哪个目录或文件里搜，默认整个仓库")] = ".",
    ) -> str:
        """在仓库里搜索一段文本，返回"文件:行号: 内容"。结果太多时只显示前 20 条，并按文件汇总匹配数。"""
        base, rel_base = self._resolve(path)
        if not base.exists():
            raise ToolError(f"{rel_base} 不存在。")
        hits: list[tuple[str, int, str]] = []
        for f in iter_files(self.root, base):
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = f.relative_to(self.root).as_posix()
            hits.extend((rel, i, line.strip()) for i, line in enumerate(text.splitlines(), 1) if pattern in line)
        if not hits:
            return f"没有找到 {pattern!r}（在 {rel_base} 中）。提示：这是区分大小写的纯文本搜索，试试更短或不同的关键词。"
        per_file: dict[str, int] = {}
        for rel, _, _ in hits:
            per_file[rel] = per_file.get(rel, 0) + 1
        head = f"找到 {len(hits)} 处匹配，分布在 {len(per_file)} 个文件中"
        shown = hits[: self.max_search_results]
        lines = [f"{rel}:{i}: {text[:150]}" for rel, i, text in shown]
        if len(hits) <= self.max_search_results:
            return head + "：\n" + "\n".join(lines)
        summary = "，".join(f"{rel}（{n}）" for rel, n in sorted(per_file.items(), key=lambda kv: -kv[1])[:10])
        return (f"{head}（{summary}）。只显示前 {self.max_search_results} 条：\n" + "\n".join(lines)
                + "\n（结果太多：请用更具体的关键词，或者用 path 参数限定到某个文件/目录。）")

    # ------------------------------------------------------------ 工具 3：edit

    def edit(
        self,
        path: Annotated[str, Field(description="要修改的文件（相对路径）。文件不存在时可用 start=1、end=0 新建")],
        start: Annotated[int, Field(ge=1, description="起始行号（从 1 开始，包含）")],
        end: Annotated[int, Field(ge=0, description="结束行号（包含）。end = start - 1 表示在第 start 行之前插入")],
        replacement: Annotated[str, Field(description="替换后的完整代码行（带正确缩进，可以多行）。传空字符串表示删除这些行")],
    ) -> str:
        """把文件第 start 到第 end 行（从 1 开始，包含 end）替换成 replacement。
        行号以你最近一次 open_file 看到的为准；Python 文件改完会做语法检查，不通过则自动撤销、文件保持原样。
        每次成功编辑后行号可能变化，继续编辑前请看返回的新代码。测试文件和测试配置受保护，不能修改。"""
        p, rel = self._resolve(path)
        if self.is_protected(rel):
            self._event("protected_edit_refused", path=rel)
            raise ToolError(
                f"拒绝：{rel} 是测试文件、测试配置或 harness 的状态文件，受保护，不能修改。"
                "测试是验收标准 —— 如果你认为测试本身有错，请不要绕过它，而是停止修改，在最终回复里说明理由，交给人类决定。"
            )
        if p.is_dir():
            raise ToolError(f"{rel} 是目录，不能编辑。")
        source = p.read_text(encoding="utf-8") if p.exists() else ""
        if not p.exists() and not (start == 1 and end == 0):
            raise ToolError(f"文件 {rel} 不存在。要新建文件，请用 start=1、end=0。")
        try:
            if rel.endswith(".py"):
                new_source, error = self.h.apply_edit_with_lint(source, start, end, replacement)
            else:
                new_source, error = splice(source, start, end, replacement), None
        except ValueError as e:
            raise ToolError(f"{e}。请先用 open_file 确认行号。") from None
        if error is not None:
            self._event("lint_rejected", path=rel, error=error)
            raise ToolError(self._lint_feedback(rel, source, start, end, replacement, error))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_source, encoding="utf-8")
        self.edits += 1
        new_lines = new_source.splitlines()
        added = replacement.count("\n") + (1 if replacement and not replacement.endswith("\n") else 0)
        center = start + max(added - 1, 0) // 2
        view = self.h.view_window(new_lines, center, max(added, 1) + 6)
        if end == start - 1:
            what = f"在第 {start} 行之前插入了 {added} 行"
        elif not replacement:
            what = f"删除了第 {start}-{end} 行"
        else:
            what = f"第 {start}-{end} 行（{end - start + 1} 行）替换成了 {added} 行"
        return (f"已修改 {rel}：{what}，文件现在共 {len(new_lines)} 行。修改后的代码：\n{view}\n"
                "请检查缩进和逻辑；准备好了就 run_tests。")

    def _lint_feedback(self, rel: str, source: str, start: int, end: int, replacement: str, error: str) -> str:
        """SWE-agent 的经验：语法错误的反馈要同时给出"改成了什么样"和"原来是什么样"，模型才知道怎么改。"""
        proposed = splice(source, start, end, replacement).splitlines()
        try:
            ast.parse("\n".join(proposed))
            bad_line = start
        except SyntaxError as e:
            bad_line = e.lineno or start
        original = source.splitlines()
        return (f"这次编辑会引入语法错误（{error}），已撤销，文件保持原样。\n"
                f"如果应用了你的编辑，代码会是这样：\n{self.h.view_window(proposed, bad_line, 7)}\n"
                f"原来的代码是这样：\n{self.h.view_window(original, start, max(end - start + 1, 1) + 4)}\n"
                "请修正 replacement（注意缩进和括号）后重新调用 edit，行号仍以原文件为准。")

    # ------------------------------------------------------------ 工具 4：run_tests

    def verify_protected_files(self) -> list[str]:
        """测试文件的哈希和基线不一致 → 说明有人绕过了 edit（比如 Agent 有 bash），从基线恢复。"""
        restored = []
        for rel, digest in self.protected_hashes.items():
            p = self.root / rel
            if not p.exists() or sha256(p.read_text(encoding="utf-8")) != digest:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(self.baseline[rel], encoding="utf-8")
                restored.append(rel)
        if restored:
            self._event("protected_files_restored", paths=restored)
        return restored

    def run_tests(self) -> str:
        """运行本仓库的测试（pytest，独立子进程，限时 30 秒），返回摘要：通过/失败数，以及每个失败测试的断言信息。"""
        restored = self.verify_protected_files()
        run = run_pytest(self.root, self.test_targets, self.test_timeout)
        self.last_run = run
        out = []
        if restored:
            out.append(f"⚠️ 检测到受保护文件被改动：{', '.join(restored)}。已从基线恢复，本次按原始测试运行。")
        if run.timed_out:
            out.append(f"错误：测试运行超过 {self.test_timeout:.0f} 秒，进程已被终止。可能有死循环或阻塞调用，请检查最近的修改。")
            return "\n".join(out)
        if run.ok:
            out.append(f"✅ 全部通过：{run.headline()}（{run.seconds:.1f}s）。确认修改完整后可以 submit。")
            return "\n".join(out)
        out.append(f"测试结果：{run.headline()}（{run.seconds:.1f}s）")
        for name, brief in run.failures[:8]:
            out.append(f"  ✗ {name}：{brief}")
        if len(run.failures) > 8:
            out.append(f"  ……还有 {len(run.failures) - 8} 个失败没有列出")
        if run.raw_tail:
            out.append("pytest 原始输出（末尾）：\n" + run.raw_tail[-800:])
        return "\n".join(out)

    # ------------------------------------------------------------ 工具 5：submit

    def submit(self) -> str:
        """完成修改后调用：先重新跑一遍测试（必须全部通过），再生成 diff 作为最终提交。提交前还会经过 diff 审查。"""
        self.verify_protected_files()
        run = run_pytest(self.root, self.test_targets, self.test_timeout)
        self.last_run = run
        if self.require_green_to_submit and not run.ok:
            raise ToolError(f"提交被拒绝：测试没有全部通过（{run.headline()}）。先 run_tests 看失败原因。")
        diff = self.diff()
        if not diff:
            raise ToolError("没有任何修改，无需提交。")
        self.submitted = True
        self._event("submitted", files=[line[6:] for line in diff.splitlines() if line.startswith("+++ b/")])
        return f"已提交（测试 {run.headline()}）。diff 如下：\n{diff}\n请用两三句话总结你改了什么、为什么，然后结束。"

    # ------------------------------------------------------------ 打包成 agentkit 工具

    def tools(self) -> list[Tool]:
        return [
            Tool(self.open_file, max_output_chars=6000),
            Tool(self.search, max_output_chars=4000),
            Tool(self.edit, risk="write", max_output_chars=4000),
            Tool(self.run_tests, timeout_s=self.test_timeout + 15, max_output_chars=4000),
            Tool(self.submit, risk="dangerous", timeout_s=self.test_timeout + 15, max_output_chars=6000),
        ]


# =====================================================================
# 两个 Hook：提交前审查 diff、防死循环
# =====================================================================


class SubmitReview(Hook):
    """submit 之前审查 diff。有可疑点 → 拒绝并把原因反馈给模型；没有 → 交给 approver（人工审批，可选）。

    为什么不直接用 agentkit 的 PermissionPolicy？它拒绝时只会说"审批人没有批准"，
    而这里我们想把**具体的审查意见**反馈给模型，让它知道错在哪 —— 错误即观察（第 03 课）。
    """

    def __init__(self, workspace: Workspace, approver: Callable[[str, list[str]], bool] | None = None):
        self.ws = workspace
        self.approver = approver
        self.rejections: list[dict] = []  # 每次打回：{"findings": [...], "diff": 当时的 diff}

    def before_tool(self, state, call, tool) -> str | None:
        if call.name != "submit":
            return None
        findings = self.ws.review()
        if findings:
            self.rejections.append({"findings": findings, "diff": self.ws.diff()})
            self.ws._event("review_rejected", findings=findings)
            return ("拒绝提交：diff 审查发现可疑改动：\n- " + "\n- ".join(findings)
                    + "\n请撤销这些改动，改为通用的修复。如果你认为测试或需求之间存在矛盾、无法用通用修复同时满足，"
                      "请不要再尝试绕过，直接在最终回复里说明矛盾在哪里，交给人类决定。")
        if self.approver is not None and not self.approver(self.ws.diff(), findings):
            self.ws._event("human_rejected")
            return "拒绝提交：人工审查没有通过这次修改。请在最终回复里说明你的修改和理由。"
        return None


class LoopGuard(Hook):
    """同一个工具调用（名字 + 参数完全相同）连续出现 max_repeats 次 → 拒绝并提示换思路；累计拒绝 max_denials 次 → 中止运行。

    步数上限（max_steps）是最后一道保险，但它发现得太晚：Agent 可能已经在同一个地方原地打转了几十步。
    """

    def __init__(self, max_repeats: int = 3, max_denials: int = 3):
        self.max_repeats = max_repeats
        self.max_denials = max_denials
        self.last: tuple[str, str] | None = None
        self.streak = 0
        self.denials = 0

    def before_tool(self, state, call, tool) -> str | None:
        try:
            args = json.dumps(json.loads(call.arguments or "{}"), sort_keys=True, ensure_ascii=False)
        except json.JSONDecodeError:
            args = call.arguments
        sig = (call.name, args)
        self.streak = self.streak + 1 if sig == self.last else 1
        self.last = sig
        if self.streak < self.max_repeats:
            return None
        self.denials += 1
        if self.denials >= self.max_denials:
            raise StopRun("loop_detected", f"检测到 Agent 反复执行相同操作（{call.name}），已中止，请人工介入。")
        return (f"拒绝：你已经连续 {self.streak} 次执行完全相同的 {call.name} 调用，结果不会改变。"
                "请换一种做法（重新阅读代码、检查假设），或者在最终回复里说明你卡在哪里。")
