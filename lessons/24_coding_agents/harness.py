"""第 24 课：长时运行 Agent 的 harness —— 让一个"会失忆"的 Agent 跨多个会话接力干完一个大任务。

思路来自 Anthropic《Effective harnesses for long-running agents》（2025-11）：
每个新会话（新的上下文窗口）都从零记忆开始，所以"记忆"必须落在文件和 git 里，而不是聊天记录里。

    初始化（只做一次）   写功能清单 features.json（全部 passes=false）+ 进度文件 PROGRESS.md + 第一次提交
    每个会话            接班：丢弃上个会话没验证完的改动 → 跑已完成功能的测试确认环境健康 → 生成接班简报
                        → 一次只做一个功能 → harness 亲自验证（新功能 + 回归）→ 通过才标记 passes=true
                        → git commit → 追加进度记录
    会话预算用完        （模拟上下文窗口耗尽）就停；下一个会话只靠文件恢复

和原文的一处不同：原文让 Agent 自己改 features.json 的 passes 字段（并用严厉的提示词禁止它改别的）；
这里改成由 harness 在验证通过后改，Agent 的工具根本写不了这两个文件 —— 用代码保证，而不是靠提示词。

系统没有 git 时自动降级为"快照"：每次提交把工作区复制到 .harness_snapshots/NNNN/。

harness 是 async 的：编码 Agent 要 await；跑 pytest、跑 git 都用 aci_tools.run_command（asyncio 子进程），
等子进程的时候事件循环不被卡住，超时 / 取消时整个进程组被杀掉。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Awaitable, Callable

HERE = Path(__file__).resolve().parent


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（课程目录名以数字开头，不能写普通 import）。与 aci_tools.py 里的同名函数一致。"""
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


_aci = _load_sibling("aci_tools")
read_tree, run_pytest, run_command = _aci.read_tree, _aci.run_pytest, _aci.run_command  # 复用：读工作区、跑 pytest、跑命令

FEATURES_FILE = "features.json"
PROGRESS_FILE = "PROGRESS.md"
MARKER_FILE = ".harness.json"
STATE_FILES = (FEATURES_FILE, PROGRESS_FILE, MARKER_FILE)  # 只允许 harness 写，Agent 的 edit 工具必须拒绝


# =====================================================================
# 版本控制：git 优先，没有 git 就用快照
# =====================================================================


class GitVCS:
    """每完成一个功能提交一次：既是"存档点"（坏了可以回退），也是写给下一个会话看的工作日志（git log）。
    所有方法都是 async 的（git 在子进程里跑，不阻塞事件循环）；SnapshotVCS 提供同样的接口。"""

    name = "git"

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    async def _git(self, *args: str) -> str:
        # 隔离用户的全局 git 配置：别人的 commit 签名、钩子、默认分支名都不应该影响 demo 的行为
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT") if k in os.environ}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        cmd = ["git", "-c", "user.name=lesson24-harness", "-c", "user.email=harness@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args]
        res = await run_command(cmd, cwd=self.root, env=env, timeout=30)
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败（{'超时' if res.timed_out else f'退出码 {res.returncode}'}）：{res.stderr.strip()[:300]}")
        return res.stdout

    async def init(self) -> None:
        await self._git("init", "-q")
        (self.root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")

    async def commit(self, message: str) -> str:
        await self._git("add", "-A")
        await self._git("commit", "-q", "--allow-empty", "-m", message)
        return (await self._git("rev-parse", "--short", "HEAD")).strip()

    async def log(self, n: int = 10) -> list[str]:
        return (await self._git("log", f"-n{n}", "--format=%h %s")).splitlines()

    async def dirty_files(self) -> list[str]:
        return [line[3:] for line in (await self._git("status", "--porcelain", "--untracked-files=all")).splitlines()]

    async def set_aside(self, message: str) -> list[str]:
        """把未提交的改动挪到一边（git stash，含未跟踪文件）：工作区回到最后一次提交，但改动没丢，需要时还能找回。"""
        files = await self.dirty_files()
        if files:
            await self._git("stash", "push", "--include-untracked", "-q", "-m", message)
        return files


class SnapshotVCS:
    """没有 git 时的降级方案：每次"提交"把工作区完整复制一份。朴素，但接班所需的三件事它都能做：
    记录历史（log）、发现未提交的改动（dirty_files）、回到上一个存档点（set_aside）。
    接口和 GitVCS 一样是 async 的；里面只是读写小仓库的几个文件（毫秒级），所以直接做同步文件 IO。"""

    name = "snapshot"
    DIR = ".harness_snapshots"

    def __init__(self, root: Path):
        self.root = root
        self.dir = root / self.DIR

    async def init(self) -> None:
        self.dir.mkdir(exist_ok=True)

    def _entries(self) -> list[dict]:
        log = self.dir / "log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _digest(tree: dict[str, str]) -> dict[str, str]:
        return {rel: hashlib.sha256(text.encode("utf-8")).hexdigest() for rel, text in tree.items()}

    async def commit(self, message: str) -> str:
        n = len(self._entries()) + 1
        snap_id = f"snap-{n:04d}"
        tree = read_tree(self.root)
        for rel, text in tree.items():
            dest = self.dir / snap_id / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        entry = {"id": snap_id, "message": message, "at": time.time(), "files": self._digest(tree)}
        with (self.dir / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return snap_id

    async def log(self, n: int = 10) -> list[str]:
        return [f"{e['id']} {e['message']}" for e in reversed(self._entries())][:n]

    async def dirty_files(self) -> list[str]:
        entries = self._entries()
        last = entries[-1]["files"] if entries else {}
        now = self._digest(read_tree(self.root))
        return sorted(rel for rel in set(last) | set(now) if last.get(rel) != now.get(rel))

    async def set_aside(self, message: str) -> list[str]:
        files = await self.dirty_files()
        entries = self._entries()
        if not files or not entries:
            return files
        last_id = entries[-1]["id"]
        stash = self.dir / f"stash-{int(time.time() * 1000)}"
        for rel in files:
            cur = self.root / rel
            if cur.exists():  # 先把改动挪进 stash 目录，保留现场
                (stash / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cur), stash / rel)
            saved = self.dir / last_id / rel
            if saved.exists():  # 再从最后一个快照恢复
                cur.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, cur)
        (stash / "MESSAGE.txt").parent.mkdir(parents=True, exist_ok=True)
        (stash / "MESSAGE.txt").write_text(message, encoding="utf-8")
        return files


# =====================================================================
# harness
# =====================================================================


@dataclass
class SessionReport:
    session: int
    briefing: str
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    stopped_because: str = ""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Harness:
    def __init__(self, root: str | Path, *, prefer_git: bool = True, test_timeout: float = 30.0):
        self.root = Path(root).resolve()
        self.test_timeout = test_timeout
        marker = self.root / MARKER_FILE
        if marker.exists():  # 已初始化：沿用当初选定的 VCS，保证跨会话一致
            backend = json.loads(marker.read_text(encoding="utf-8"))["vcs"]
        else:
            backend = "git" if prefer_git and GitVCS.available() else "snapshot"
        self.vcs = GitVCS(self.root) if backend == "git" else SnapshotVCS(self.root)

    # ------------------------------------------------------------ 初始化（相当于原文的"初始化 Agent"）

    def is_initialized(self) -> bool:
        return (self.root / MARKER_FILE).exists()

    async def initialize(self, goal: str, features: list[dict]) -> str:
        """写功能清单、进度文件，做第一次提交。原文里这一步由一个专门的"初始化 Agent"根据需求生成；
        这里功能清单由调用方给出（demo 里是写死的 3 个功能），重点演示它的格式和用法。"""
        if self.is_initialized():
            raise RuntimeError(f"{self.root} 已经初始化过了")
        items = [{"id": f["id"], "title": f["title"], "description": f["description"], "verify": f["verify"], "passes": False}
                 for f in features]
        self._write_features(items)
        lines = [
            "# 进度记录（PROGRESS.md）",
            "",
            f"目标：{goal}",
            "",
            "规则：一次只做一个功能；harness 验证通过（新功能测试 + 已完成功能的回归测试）才标记完成并提交。",
            "",
            f"## 会话 0 · 初始化（{_now()}）",
            f"- 写入功能清单 {FEATURES_FILE}：{len(items)} 个功能，全部 passes=false",
            f"- 版本控制：{self.vcs.name}",
            f"- 下一步：从 {items[0]['id']} 开始" if items else "- 功能清单为空",
        ]
        (self.root / PROGRESS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.root / MARKER_FILE).write_text(json.dumps({"vcs": self.vcs.name, "created_at": _now()}), encoding="utf-8")
        await self.vcs.init()
        return await self.vcs.commit("chore: 初始化 harness（功能清单 + 进度文件）")

    # ------------------------------------------------------------ 状态读写

    def features(self) -> list[dict]:
        return json.loads((self.root / FEATURES_FILE).read_text(encoding="utf-8"))

    def _write_features(self, items: list[dict]) -> None:
        # 为什么用 JSON 而不是 Markdown 勾选框？原文的经验：模型不太会随手改写 JSON 的结构，
        # 而 Markdown 清单很容易被"顺手整理"掉。这里更进一步：Agent 的工具根本写不了它。
        (self.root / FEATURES_FILE).write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _set_passes(self, feature_id: str, passes: bool) -> None:
        items = self.features()
        for f in items:
            if f["id"] == feature_id:
                f["passes"] = passes
        self._write_features(items)

    def next_feature(self) -> dict | None:
        """清单顺序就是优先级：第一个还没通过的功能。"""
        return next((f for f in self.features() if not f["passes"]), None)

    def append_progress(self, *lines: str) -> None:
        with (self.root / PROGRESS_FILE).open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def progress_tail(self, n: int = 12) -> list[str]:
        return (self.root / PROGRESS_FILE).read_text(encoding="utf-8").splitlines()[-n:]

    # ------------------------------------------------------------ 接班

    async def orient(self, session: int) -> str:
        """新会话的第一件事。对应原文让 Agent 每次开工先做的几步：pwd → 读 git log 和进度文件 → 读功能清单
        → 先跑一遍基本测试确认环境没坏。这里由 harness 用代码做完，把结果写成一份"接班简报"交给 Agent。"""
        notes: list[str] = []
        dropped = await self.vcs.set_aside(f"会话 {session} 开始前发现的未提交改动")
        if dropped:
            notes.append(f"发现未提交的改动（{', '.join(dropped)}）：上个会话没验证完就中断了。"
                         "未验证的改动不可信，已挪到一边（"
                         + ("git stash" if self.vcs.name == "git" else f"{SnapshotVCS.DIR}/stash-*")
                         + "），工作区回到最后一次提交。")
        done = [f for f in self.features() if f["passes"]]
        if done:
            run = await run_pytest(self.root, [f["verify"] for f in done], self.test_timeout)
            if run.ok:
                notes.append(f"环境检查：已完成功能的测试全部通过（{run.headline()}）。")
            else:
                broken = {name for name, _ in run.failures}
                for f in done:
                    if f["verify"].split("::")[-1] in broken or run.timed_out:
                        self._set_passes(f["id"], False)
                notes.append(f"环境检查：已完成功能出现回归（{run.headline()}），相关功能已改回 passes=false，优先修复。")
        else:
            notes.append("环境检查：还没有已完成的功能，跳过回归测试。")
        items = self.features()
        todo = [f for f in items if not f["passes"]]
        nxt = todo[0] if todo else None
        self.append_progress("", f"## 会话 {session} · 开始（{_now()}）", *[f"- {n}" for n in notes])
        await self.vcs.commit(f"docs: 会话 {session} 接班记录")  # 先把接班记录提交：后面验证失败回退时不会连它一起丢掉
        briefing = [
            f"【接班简报 · 会话 {session}】你没有之前会话的任何记忆，以下信息来自仓库里的文件和 git 历史。",
            f"工作目录：{self.root}",
            "最近的提交：",
            *[f"  {line}" for line in await self.vcs.log(5)],
            f"进度文件 {PROGRESS_FILE}（最后几行）：",
            *[f"  {line}" for line in self.progress_tail(10)],
            f"功能清单：共 {len(items)} 个，已完成 {len(items) - len(todo)} 个"
            + (f"（{', '.join(f['id'] for f in items if f['passes'])}）" if len(items) > len(todo) else "")
            + f"；待完成：{', '.join(f['id'] + ' ' + f['title'] for f in todo) or '无'}",
            f"下一步：{nxt['id']} {nxt['title']}" if nxt else "下一步：全部完成",
        ]
        return "\n".join(briefing)

    # ------------------------------------------------------------ 验证 + 提交

    async def verify(self, feature: dict):
        """harness 亲自验证，不采信 Agent 的"我做完了"：新功能的测试 + 所有已完成功能的回归测试。"""
        done = [f["verify"] for f in self.features() if f["passes"] and f["id"] != feature["id"]]
        return await run_pytest(self.root, [feature["verify"], *done], self.test_timeout)

    async def run_session(
        self, session: int, worker: Callable[[dict, str], Awaitable[str]], max_features: int = 2
    ) -> SessionReport:
        """跑一个会话：接班 → 最多做 max_features 个功能（模拟上下文窗口的容量）→ 写小结并提交。

        worker(feature, briefing) -> 摘要：async 函数，真正干活的编码 Agent（demo 里是一个带 ACI 工具的 agentkit Agent，
        每个功能都新建一个，互不共享对话历史）。
        """
        report = SessionReport(session=session, briefing=await self.orient(session))
        report.stopped_because = "会话预算用完（模拟上下文窗口耗尽）"
        for _ in range(max_features):
            feature = self.next_feature()
            if feature is None:
                report.stopped_because = "功能清单全部完成"
                break
            summary = (await worker(feature, report.briefing) or "").strip().replace("\n", " ")[:200]
            run = await self.verify(feature)
            if run.ok:
                self._set_passes(feature["id"], True)
                self.append_progress(f"- ✅ {feature['id']} {feature['title']}：{summary}（harness 验证：{run.headline()}）")
                sha = await self.vcs.commit(f"feat({feature['id']}): {feature['title']}")
                report.completed.append(f"{feature['id']}@{sha}")
            else:
                # 验证不过：不提交半成品。改动挪到一边，记录原因，停止本会话 —— 别在坏掉的地基上继续盖楼。
                reason = "; ".join(f"{n}: {b}" for n, b in run.failures[:2]) or run.headline()
                await self.vcs.set_aside(f"{feature['id']} 验证失败的改动")
                self.append_progress(f"- ❌ {feature['id']} 验证失败，改动已挪到一边：{reason[:200]}")
                report.failed.append(feature["id"])
                report.stopped_because = f"{feature['id']} 验证失败"
                break
        nxt = self.next_feature()
        self.append_progress(f"- 会话 {session} 结束：{report.stopped_because}。"
                             + (f"下一步：{nxt['id']} {nxt['title']}" if nxt else "全部功能已完成。"))
        await self.vcs.commit(f"docs: 会话 {session} 小结")
        return report
