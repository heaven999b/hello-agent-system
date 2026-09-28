"""第 19 课：进程级 Python 代码执行沙箱（零依赖）。

    result = await run_python("print(sum(range(10)))", SandboxLimits(timeout_s=2))
    result.stdout, result.exit_code, result.timed_out

它能做到的（每一条都对应一种威胁）：
  - 墙钟超时 + 杀掉整个进程组        → 死循环、sleep、子进程赖着不走
  - 调用方取消时同样整组杀掉          → Agent 的运行被取消 / 工具层超时后，代码不会在后台接着跑
  - RLIMIT_CPU                       → 纯计算把 CPU 烧满
  - 内存上限                          → 内存炸弹（Linux 用 RLIMIT_AS；macOS 设不上，改为轮询 phys_footprint 兜底）
  - RLIMIT_FSIZE / RLIMIT_NOFILE     → 写满磁盘、耗尽文件描述符
  - 一次性临时工作目录 + 最小环境变量  → 不在你的仓库里乱写；拿不到环境变量里的 API key
  - 输出截断                          → 一行 print 撑爆模型上下文

它做不到的（这正是需要容器 / microVM 的原因，demo 第 3 部分会实测给你看）：
  - 读不该读的文件：代码以你的用户身份运行，~/.ssh、仓库根目录的 .env 都能按绝对路径读到；
  - 禁止联网：rlimit 管不了网络。macOS 上只能借助已标记为废弃的 sandbox-exec（Seatbelt），
    Linux 上要用 network namespace（容器、bubblewrap）；
  - 逃逸：和宿主共享同一个内核，内核漏洞 = 沙箱失效。

os_sandbox=True 时，在 macOS 上额外套一层 Seatbelt：拒绝网络、拒绝读家目录、只允许写临时目录。
这是 Claude Code 等工具在 macOS 上使用的同一类机制，但 sandbox-exec 本身已被 Apple 标记为废弃。
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import functools
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from agentkit import Tool, tool

try:  # resource 只在 POSIX 上有
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]


@dataclass
class SandboxLimits:
    timeout_s: float = 5.0  # 墙钟超时：sleep、等 I/O 也算时间
    cpu_s: int = 5  # CPU 时间上限（RLIMIT_CPU），到点内核发 SIGXCPU
    memory_mb: int = 256  # 内存上限
    max_output_chars: int = 8000  # stdout / stderr 各自最多保留多少字符
    max_file_mb: int = 16  # 单个文件最大多大（RLIMIT_FSIZE），防止写满磁盘
    max_open_files: int = 64  # 最多同时打开多少个文件描述符
    os_sandbox: bool = False  # macOS 上额外用 Seatbelt 禁网络、禁读家目录


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int | None  # 被信号杀死时是负数（-9 = SIGKILL），沿用 subprocess 的约定
    timed_out: bool
    duration_s: float = 0.0
    truncated: bool = False
    killed_reason: str | None = None  # timeout / memory / cpu / None
    notes: list[str] = field(default_factory=list)  # 哪些限制真正生效了、哪些没有

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        """给模型看的结果：状态在前、输出在后；错误信息原样给模型，它才能自己改代码。"""
        if self.killed_reason == "timeout":
            head = "执行超时，进程已被终止（可能是死循环，或者计算量太大）。"
        elif self.killed_reason == "memory":
            head = "内存超出上限，进程已被终止。请减少内存占用（比如分块处理）。"
        elif self.killed_reason == "cpu":
            head = "CPU 时间超出上限，进程已被终止。"
        else:
            head = f"exit_code: {self.exit_code}"
        parts = [head]
        if self.stdout:
            parts.append("stdout:\n" + self.stdout)
        if self.stderr:
            parts.append("stderr:\n" + self.stderr)
        if not self.stdout and not self.stderr:
            parts.append("（没有任何输出。记得用 print() 输出结果。）")
        return "\n".join(parts)


# ───────────────────────────── 平台能力探测（实测，不靠猜） ─────────────────────────────


@functools.lru_cache(maxsize=None)
def rlimit_as_works(memory_mb: int = 256) -> bool:
    """实测：当前平台能不能把 RLIMIT_AS 设到 memory_mb。

    Linux 上可以。macOS（实测 Apple Silicon + macOS 14）上不行：一个空的 Python 进程虚拟地址空间
    就有约 390 GB（系统共享缓存等映射），setrlimit 设成比当前用量小的值直接报 EINVAL，
    Python 里表现为 ValueError('current limit exceeds maximum limit')。
    """
    if resource is None or not hasattr(resource, "RLIMIT_AS"):
        return False
    probe = f"import resource; b = {memory_mb} * 1024 * 1024; resource.setrlimit(resource.RLIMIT_AS, (b, b))"
    return subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True).returncode == 0


def rlimit_cpu_reliable() -> bool:
    """RLIMIT_CPU 在本平台是否可靠。

    实测（Apple M1 + macOS 14.4.1）：把 RLIMIT_CPU 设成 5 秒，纯计算的子进程在 0.08～0.23 CPU 秒
    就被 SIGXCPU 杀掉，每次都不一样 —— 这会误杀正常代码，所以在 macOS 上干脆不设，只靠墙钟超时。
    Linux 上它按预期工作。
    """
    return resource is not None and sys.platform != "darwin"


def seatbelt_available() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _real_home() -> str:
    """真正的家目录：从用户数据库查，不看 HOME 环境变量（沙箱里把 HOME 改了，也改变不了它）。"""
    return pwd.getpwuid(os.getuid()).pw_dir


def _seatbelt_profile(workdir: str) -> str:
    """Seatbelt 配置（SBPL 语言，后面的规则覆盖前面的）：默认放行，再收紧网络、家目录读、所有写。"""

    def q(path: str) -> str:
        return '"' + os.path.realpath(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

    readable = [workdir, sys.prefix, sys.base_prefix, os.path.dirname(os.path.realpath(sys.executable))]
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            f"(deny file-read* (subpath {q(_real_home())}))",
            "(allow file-read* " + " ".join(f"(subpath {q(p)})" for p in readable) + ")",
            "(deny file-write*)",
            f'(allow file-write* (subpath {q(workdir)}) (literal "/dev/null"))',
        ]
    )


# ───────────────────────────── 执行 ─────────────────────────────


# 启动器：一个极小的 Python 程序，先给自己设好 rlimit，再用 execv 把自己"替换"成真正要跑的程序。
# 为什么不用 subprocess 的 preexec_fn？官方文档明确警告：父进程里有其他线程时（事件循环的默认线程池、
# asyncio 在 3.11 上等待子进程退出用的监视线程、同步工具的线程池），preexec_fn 可能在 fork 出来的子进程里死锁。
# 启动器是全新的单线程进程，没有这个问题；
# execv 不换 pid，所以进程组、内存监控都照常工作。rlimit 会跨 exec 继承。
_LAUNCHER = """
import json, os, resource, sys
for name, soft, hard in json.loads(sys.argv[1]):
    try:
        resource.setrlimit(getattr(resource, name), (soft, hard))
    except (ValueError, OSError):
        pass  # 设不上就算了；父进程已经在 notes 里如实记录
os.execv(sys.executable, [sys.executable] + sys.argv[2:])
"""


def _rlimits(limits: SandboxLimits, use_rlimit_as: bool, use_rlimit_cpu: bool) -> list[tuple[str, int, int]]:
    mb = 1024 * 1024
    wanted = [
        ("RLIMIT_FSIZE", limits.max_file_mb * mb, limits.max_file_mb * mb),
        ("RLIMIT_NOFILE", limits.max_open_files, limits.max_open_files),
        ("RLIMIT_CORE", 0, 0),  # 被 SIGXCPU 杀掉时不要生成 core 文件
    ]
    if use_rlimit_cpu:  # 软限制到点发 SIGXCPU，硬限制再多 1 秒直接 SIGKILL
        wanted.append(("RLIMIT_CPU", limits.cpu_s, limits.cpu_s + 1))
    if use_rlimit_as:
        wanted.append(("RLIMIT_AS", limits.memory_mb * mb, limits.memory_mb * mb))
    return wanted


class _RUsageInfoV2(ctypes.Structure):
    """macOS <sys/resource.h> 里的 struct rusage_info_v2（只为读 ri_phys_footprint）。"""

    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size",
            "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime", "child_user_time",
            "child_system_time", "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins",
            "child_elapsed_abstime", "diskio_bytesread", "diskio_byteswritten",
        )
    ]


@functools.lru_cache(maxsize=None)
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c"))


def _memory_mb(pid: int) -> float | None:
    """读子进程当前占用的内存（MB）。

    Linux：/proc/<pid>/status 里的 VmRSS。
    macOS：不能用 RSS！实测一个每次分配 64 MB 可压缩数据（b"x" * n）的内存炸弹，分配到 518 MB 时
    ps 看到的 RSS 只有 161 MB —— 系统的内存压缩器把页面压缩了，压缩后的部分不算 RSS。
    要看"活动监视器"里那个"内存"数字，也就是 phys_footprint（包含被压缩的部分），用 proc_pid_rusage 读。
    """
    if sys.platform == "darwin":
        try:
            libc = _libc()
            info = _RUsageInfoV2()
            if libc.proc_pid_rusage(pid, 2, ctypes.byref(info)) == 0:  # 2 = RUSAGE_INFO_V2
                return info.phys_footprint / (1024 * 1024)
        except (OSError, AttributeError):
            pass
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
        return int(out) / 1024 if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """杀掉整个进程组：代码里启动的子进程、孙进程一起杀（前提是启动时 start_new_session=True）。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class _Capped:
    """读一个管道，只保留前 cap 字节，后面的读出来直接丢掉（不读的话子进程写满管道会卡住）。"""

    def __init__(self, cap: int):
        self.cap = cap
        self.data = bytearray()
        self.total = 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            self.total += len(chunk)
            room = self.cap - len(self.data)
            if room > 0:
                self.data += chunk[:room]


def _truncate(text: str, limit: int, total_bytes: int, kept_bytes: int) -> tuple[str, bool]:
    if len(text) <= limit and total_bytes <= kept_bytes:
        return text, False
    return text[:limit] + f"\n...[输出已截断：原始输出约 {total_bytes} 字节]", True


async def _reap(proc: asyncio.subprocess.Process, exited: asyncio.Task, readers: list[asyncio.Task], notes: list[str]) -> None:
    """收尾：确认进程已经退出（不留僵尸），读管道的任务读到 EOF。"""
    if not exited.done():
        _kill_group(proc)
    await asyncio.wait({exited})
    _, pending = await asyncio.wait(readers, timeout=2)  # 有孙进程用 setsid 逃出了进程组、还攥着管道时，别无限等
    if pending:
        notes.append("有进程逃出了进程组，仍占着输出管道（进程级沙箱管不住它，容器的 PID namespace 可以）")
        for t in pending:
            t.cancel()
        await asyncio.wait(pending)
    for t in [exited, *readers]:  # 取走异常：没人取的任务异常会在垃圾回收时报"never retrieved"
        if not t.cancelled() and t.exception() is not None:
            notes.append(f"收尾时读到异常：{t.exception()!r}")


async def run_python(code: str, limits: SandboxLimits | None = None) -> SandboxResult:
    """在一次性子进程里执行 code，返回结构化结果。

    async：等子进程的这段时间让出事件循环。调用方被取消（Agent 的运行被取消、工具层超时、用户断开）时，
    立刻杀掉整个进程组再把取消传出去 —— 不用等墙钟超时，也不会留下没人管的子进程。
    """
    limits = limits or SandboxLimits()
    notes: list[str] = []
    workdir = tempfile.mkdtemp(prefix="agent-sandbox-")
    try:
        with open(os.path.join(workdir, "main.py"), "w", encoding="utf-8") as f:
            f.write(code)

        use_as = await asyncio.to_thread(rlimit_as_works, limits.memory_mb)  # 第一次要起一个子进程探测：放进线程，不卡事件循环
        watch_memory = not use_as
        notes.append("内存：RLIMIT_AS（内核强制）" if use_as else "内存：RLIMIT_AS 在本平台设不上，改为轮询内存占用兜底（有竞态窗口）")
        use_cpu = rlimit_cpu_reliable()
        if not use_cpu:
            notes.append("CPU：RLIMIT_CPU 在本平台会提前误杀，已禁用，只靠墙钟超时")

        # 启动器设好 rlimit 后 exec 成：python -I -B -X utf8 main.py
        # -I 隔离模式：忽略 PYTHON* 环境变量、不把脚本目录和用户 site-packages 加进 sys.path
        # -B 不写 .pyc；-X utf8 统一用 UTF-8 输出
        rl = json.dumps(_rlimits(limits, use_as, use_cpu))
        cmd = [sys.executable, "-I", "-c", _LAUNCHER, rl, "-I", "-B", "-X", "utf8", "main.py"]
        if limits.os_sandbox:
            if seatbelt_available():
                cmd = ["sandbox-exec", "-p", _seatbelt_profile(workdir)] + cmd
                notes.append("Seatbelt：已禁止网络、禁止读家目录、只允许写临时目录")
            else:
                notes.append("os_sandbox 不可用（只实现了 macOS Seatbelt）；Linux 上请用 bubblewrap、容器或 microVM")

        # 最小环境变量：不继承父进程的 LLM_API_KEY、云凭证等；HOME 指向临时目录
        env = {"PATH": "/usr/bin:/bin", "HOME": workdir, "TMPDIR": workdir}
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # 新会话 = 新进程组，超时或被取消时可以整组杀掉
        )
        cap = limits.max_output_chars * 4 + 16  # UTF-8 一个字符最多 4 字节
        out_c, err_c = _Capped(cap), _Capped(cap)
        readers = [asyncio.create_task(out_c.drain(proc.stdout)), asyncio.create_task(err_c.drain(proc.stderr))]
        exited = asyncio.create_task(proc.wait())

        killed_reason = None
        try:
            while not exited.done():
                if time.monotonic() - start > limits.timeout_s:
                    killed_reason = "timeout"
                    _kill_group(proc)
                    break
                if watch_memory:
                    used = _memory_mb(proc.pid)
                    if used is not None and used > limits.memory_mb:
                        killed_reason = "memory"
                        notes.append(f"被杀时内存占用 ≈ {used:.0f} MB（上限 {limits.memory_mb} MB）")
                        _kill_group(proc)
                        break
                # 轮询间隔：越短越及时；两次轮询之间分配的内存就是"竞态窗口"。asyncio.wait 超时不会取消 exited
                await asyncio.wait({exited}, timeout=0.02)
        except BaseException:
            _kill_group(proc)  # 被取消（或出了别的错）：先杀整个进程组，再往外传
            raise
        finally:
            # shield：收尾本身不能被再次取消打断，否则可能留下僵尸进程和悬空的读管道任务
            await asyncio.shield(asyncio.ensure_future(_reap(proc, exited, readers, notes)))
        exit_code = exited.result()
        duration = time.monotonic() - start
        if killed_reason is None and exit_code == -getattr(signal, "SIGXCPU", 0):
            killed_reason = "cpu"

        stdout, t1 = _truncate(out_c.data.decode("utf-8", "replace"), limits.max_output_chars, out_c.total, len(out_c.data))
        stderr, t2 = _truncate(err_c.data.decode("utf-8", "replace"), limits.max_output_chars, err_c.total, len(err_c.data))
        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=killed_reason == "timeout",
            duration_s=round(duration, 3),
            truncated=t1 or t2,
            killed_reason=killed_reason,
            notes=notes,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # 每次都是全新目录，用完即焚


def make_run_python_tool(limits: SandboxLimits | None = None) -> Tool:
    """把沙箱包装成 agentkit 工具。risk="dangerous"：配合 PermissionPolicy，每次执行都要审批。"""
    limits = limits or SandboxLimits()
    net = "没有网络，" if limits.os_sandbox and seatbelt_available() else ""
    description = (
        f"在一次性的隔离子进程里执行一段完整的 Python 3 程序，返回 exit_code、stdout 和 stderr。"
        f"{net}限时 {limits.timeout_s:g} 秒、内存 {limits.memory_mb} MB；每次都是全新环境，变量不会保留；"
        f"只有标准库可用。结果必须用 print() 输出。"
    )

    async def run_python_code(code: Annotated[str, Field(description="要执行的完整 Python 3 程序")]) -> str:
        return (await run_python(code, limits)).summary()

    # async 工具：Agent 的运行被取消、或工具层超时，run_python 会被取消，进程组立刻被杀。
    # 工具层超时要比沙箱超时长：让沙箱自己先杀进程、返回结构化结果，而不是被工具层"放弃等待"
    return Tool(
        run_python_code,
        name="run_python",
        description=description,
        risk="dangerous",
        timeout_s=limits.timeout_s + 10,
        max_output_chars=2 * limits.max_output_chars + 500,
    )


run_python_tool = make_run_python_tool()
