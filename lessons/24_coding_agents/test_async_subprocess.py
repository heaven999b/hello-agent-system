"""第 24 课：run_tests 背后的 asyncio 子进程（不是练习）。这组测试会真的启动 pytest 子进程，每个 1～5 秒。

证明三件事：
1. 跑 pytest 时事件循环没有被卡住：子进程里的测试要等父进程的一个协程"应答"才能结束 ——
   如果 run_pytest 阻塞了事件循环，应答协程根本没机会运行，测试会等满 10 秒后失败；
2. 超时：整个进程组被杀掉（pytest 进程 + 被测代码自己起的孙进程），不留孤儿；
3. 被取消（Agent 的工具超时、run_timeout、用户断开）：同样杀掉整个进程组，CancelledError 照常传出去。

运行：.venv/bin/python -m pytest lessons/24_coding_agents/test_async_subprocess.py -v
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from agentkit import ToolCall, ToolRegistry
from agentkit.testing import load_sibling

aci = load_sibling(__file__, "aci_tools")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="进程组（killpg）只在 POSIX 上有")

HANDSHAKE_TEST = '''
import time
from pathlib import Path


def test_waits_for_parent():
    Path("started").write_text("1")
    deadline = time.time() + 10
    while not Path("go").exists():  # 父进程事件循环里的一个协程看到 started 后才会写 go
        assert time.time() < deadline, "父进程的事件循环没有响应：go 文件一直没出现"
        time.sleep(0.01)
'''

HANG_TEST = '''
import os
import subprocess
import sys
import time
from pathlib import Path


def test_hang():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])  # 被测代码自己起的孙进程
    Path("pids.tmp").write_text(f"{os.getpid()} {child.pid}")
    os.replace("pids.tmp", "pids.txt")  # 原子地出现：读的一方不会读到半个文件
    time.sleep(120)
'''


async def _gone(pid: int, within: float = 10.0) -> bool:
    """pid 对应的进程是否已经不存在（被杀且被回收）。孙进程被杀后由 init/launchd 回收，要稍等一下。"""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def _pids(root: Path, within: float = 20.0) -> list[int]:
    deadline = time.monotonic() + within
    while not (root / "pids.txt").exists():
        assert time.monotonic() < deadline, "被测代码没有按时写出 pids.txt"
        await asyncio.sleep(0.05)
    return [int(x) for x in (root / "pids.txt").read_text().split()]


async def test_run_pytest_keeps_the_event_loop_running(tmp_path):
    (tmp_path / "test_handshake.py").write_text(HANDSHAKE_TEST)

    async def respond():
        while not (tmp_path / "started").exists():
            await asyncio.sleep(0.01)
        (tmp_path / "go").write_text("1")

    helper = asyncio.create_task(respond())
    try:
        run = await aci.run_pytest(tmp_path, ["test_handshake.py"], timeout=30)
    finally:
        if not helper.done():
            helper.cancel()
        await asyncio.gather(helper, return_exceptions=True)  # 取回结果，不留没人管的 task
    assert run.ok, run.failures or run.raw_tail
    assert (tmp_path / "go").exists()  # 应答协程是在 pytest 子进程运行期间执行的


async def test_run_pytest_timeout_kills_the_whole_process_group(tmp_path):
    (tmp_path / "test_hang.py").write_text(HANG_TEST)
    t0 = time.perf_counter()
    run = await aci.run_pytest(tmp_path, ["test_hang.py"], timeout=5)
    assert run.timed_out and not run.ok
    assert time.perf_counter() - t0 < 60  # 宽松上限：没有等被测代码的 120 秒
    pytest_pid, grandchild_pid = await _pids(tmp_path)
    assert await _gone(pytest_pid), "pytest 进程还活着"
    assert await _gone(grandchild_pid), "被测代码起的孙进程还活着（没有杀整个进程组）"


async def test_cancelling_run_pytest_kills_the_subprocess(tmp_path):
    (tmp_path / "test_hang.py").write_text(HANG_TEST)
    task = asyncio.create_task(aci.run_pytest(tmp_path, ["test_hang.py"], timeout=300))
    pytest_pid, grandchild_pid = await _pids(tmp_path)
    assert not await _gone(pytest_pid, within=0.2)  # 取消之前它确实在跑
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(pytest_pid)
    assert await _gone(grandchild_pid)


async def test_agent_tool_timeout_cancels_run_tests_and_kills_pytest(tmp_path):
    """经由 agentkit 的工具执行器：run_tests 是 async 工具，工具超时 → 取消 → 子进程被杀，模型拿到"执行超时"的观察。"""
    (tmp_path / "test_hang.py").write_text(HANG_TEST)
    ws = aci.Workspace(tmp_path, test_targets=["test_hang.py"], test_timeout=300)
    tools = {t.name: t for t in ws.tools()}
    assert [tools[n].is_async for n in ("open_file", "search", "edit", "run_tests", "submit")] == [False, False, False, True, True]
    run_tests = tools["run_tests"]
    run_tests.timeout_s = 8  # 比 pytest 自己的超时（300 秒）短：由 agentkit 的工具超时来取消它
    result = await ToolRegistry([run_tests]).execute(ToolCall(id="c1", name="run_tests", arguments="{}"))
    assert result.error_type == "timeout"
    pytest_pid, grandchild_pid = await _pids(tmp_path)
    assert await _gone(pytest_pid)
    assert await _gone(grandchild_pid)
