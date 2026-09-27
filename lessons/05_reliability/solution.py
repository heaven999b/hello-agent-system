"""第 05 课练习 —— 参考答案。

先自己做 exercise.py，卡住超过 15 分钟再来看。对照时重点看注释里的"为什么"。
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Callable, TypeVar

from agentkit.hooks import Hook, StopRun
from agentkit.reliability import CircuitBreaker, CircuitOpenError, backoff_delay, is_retryable

T = TypeVar("T")


# =====================================================================
# 练习 (a)：LoopGuard —— 检测 Agent 死循环
# =====================================================================


def normalize_arguments(arguments: str) -> str:
    """把工具参数（JSON 字符串）归一化，使"语义相同"的参数得到完全相同的字符串。

    - 合法 JSON：按 key 排序、去掉多余空白、保留中文原样（ensure_ascii=False）；
    - 不合法 JSON：退化为去掉首尾空白的原始字符串（不能因为参数坏了就让护栏崩溃）。
    """
    try:
        value = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return (arguments or "").strip()
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class LoopGuard(Hook):
    """死循环护栏：同一工具 + 相同参数在最近 window 次调用里出现 >= max_repeats 次，就判定为"原地打转"。

    处理分两级（先给机会，再止损）：
      1. 本次运行第一次超限 → before_tool 返回一条拒绝理由（工具不执行），提醒模型换思路；
      2. 之后再次超限        → 抛 StopRun("loop_detected")，结束运行。

    ⚠️ 关键设计：计数必须存在 state.metadata 上，而不是 self 上。
      - 一个 Hook 实例会被同一个 Agent 的所有运行共享（Web 服务里就是所有用户、所有租户的并发请求）。
        计数放在 self 上 = A 用户的调用历史会让 B 用户被误判为死循环，还有并发竞争；
      - state 会随检查点一起落盘，进程崩溃 / 人工审批暂停后 resume，计数不会丢；
      - 所以 state 里只能放 JSON 可序列化的东西（list / dict / str / bool）。
    Hook 实例上只放"配置"（window、max_repeats），不放"每次运行的状态"。
    """

    STATE_KEY = "loop_guard"

    def __init__(self, window: int = 6, max_repeats: int = 3):
        if max_repeats < 2:
            raise ValueError("max_repeats 至少为 2：调用一次不可能算重复")
        if window < max_repeats:
            raise ValueError("window 必须 >= max_repeats，否则永远不可能触发")
        self.window = window
        self.max_repeats = max_repeats

    def before_tool(self, state, call, tool) -> str | None:
        memo = state.metadata.setdefault(self.STATE_KEY, {"recent": [], "warned": False})
        fp = f"{call.name}:{normalize_arguments(call.arguments)}"

        recent: list[str] = memo["recent"]
        recent.append(fp)  # 被拒绝的调用也要记下来：模型"又试了一次"本身就是循环的证据
        del recent[: -self.window]  # 只保留最近 window 次（原地修改，state 里的列表同步更新）

        count = recent.count(fp)
        if count < self.max_repeats:
            return None
        if not memo["warned"]:
            memo["warned"] = True
            return (
                f"拒绝：你在最近 {len(recent)} 次工具调用中已经 {count} 次用完全相同的参数调用 {call.name}，"
                "重复调用不会得到不同的结果。请换一种思路（换参数、换工具），或者直接根据已有信息回答用户。"
            )
        raise StopRun("loop_detected", f"检测到 Agent 陷入循环（反复调用 {call.name}），任务已中止。")


# =====================================================================
# 练习 (b)：带全局预算的重试 —— 防止重试风暴
# =====================================================================


class DeadlineExceeded(Exception):
    """截止时间已过。注意它故意不继承 TimeoutError：is_retryable 会把 TimeoutError 当成可重试，
    而"整体截止时间已过"重试多少次都不会成功，绝不能被外层再重试。"""


class RetryBudget:
    """令牌桶式重试预算（思路同 gRPC retryThrottling、Google SRE "重试比例不超过 10%"）。

    - 每发起一个"新请求"（首次尝试），往桶里存 ratio 个令牌，桶最多存 max_tokens 个；
    - 每次"重试"从桶里取 1 个令牌，不够 1 个就不许重试，直接把错误交给调用方；
    - 长期来看：重试次数 <= initial_tokens + ratio × 请求数。ratio=0.1 就是"重试不超过请求的 10%"。
    - initial_tokens 保证低流量时（比如刚启动）也有少量重试机会；
      max_tokens 防止长时间健康时攒下海量令牌，一出故障就全部用来重试（那又是一场风暴）。

    一个 RetryBudget 应该被"同一个下游"的所有请求共享（例如每个模型供应商一个），所以要加锁。
    """

    SCALE = 1000  # 内部以 1/1000 个令牌为单位做整数运算

    def __init__(self, ratio: float = 0.1, initial_tokens: float = 3.0, max_tokens: float = 10.0):
        if not 0 < ratio <= 1:
            raise ValueError("ratio 必须在 (0, 1] 之间")
        if max_tokens < 1 or not 0 <= initial_tokens <= max_tokens:
            raise ValueError("需要 max_tokens >= 1 且 0 <= initial_tokens <= max_tokens")
        # 为什么用整数？浮点数 0.1 累加 10 次是 0.9999999999999999 < 1，
        # "10 个请求攒 1 次重试"就会莫名其妙地差一点点。钱、令牌、配额这类计数都应该用整数。
        self._ratio = round(ratio * self.SCALE)
        self._tokens = round(initial_tokens * self.SCALE)
        self._max = round(max_tokens * self.SCALE)
        self._lock = threading.Lock()
        self.requests = 0  # 统计：新请求数
        self.retries = 0  # 统计：批准的重试数
        self.rejected = 0  # 统计：因预算不足被拒绝的重试数

    @property
    def tokens(self) -> float:
        return self._tokens / self.SCALE

    def on_request(self) -> None:
        """记录一个新请求：存入 ratio 个令牌（不超过 max_tokens）。"""
        with self._lock:
            self.requests += 1
            self._tokens = min(self._max, self._tokens + self._ratio)

    def try_acquire(self) -> bool:
        """为一次重试申请 1 个令牌。成功返回 True 并扣减；不足返回 False 并记一次 rejected。"""
        with self._lock:
            if self._tokens >= self.SCALE:
                self._tokens -= self.SCALE
                self.retries += 1
                return True
            self.rejected += 1
            return False


EventFn = Callable[[str, int, Exception, float], None]


def retry_with_budget(
    fn: Callable[[], T],
    budget: RetryBudget,
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_if: Callable[[Exception], bool] = is_retryable,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_event: EventFn | None = None,
) -> T:
    """带"单请求上限 + 全局预算 + 截止时间"的重试。三道闸门缺一不可：

    - max_attempts：单个请求最多尝试几次（每个请求自己的上限）；
    - budget：所有请求共享的重试预算（整体的上限，防止故障时流量翻倍）；
    - deadline：绝对截止时间（按 clock 计）。剩余时间不够再等一次退避，就别重试了。
    """
    if deadline is not None and clock() >= deadline:
        raise DeadlineExceeded("调用前截止时间就已经过了，放弃执行（别让下游做注定白做的工作）")

    budget.on_request()  # 每个新请求只存一次，无论成功失败
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            if not retry_if(e) or attempt == max_attempts:
                raise
            delay = backoff_delay(attempt, base_delay, max_delay, rng)
            # 先检查截止时间、再申请令牌：注定放弃的重试不应该白白消耗全局预算
            if deadline is not None and clock() + delay >= deadline:
                if on_event:
                    on_event("give_up_deadline", attempt, e, delay)
                raise
            if not budget.try_acquire():
                if on_event:
                    on_event("give_up_budget", attempt, e, delay)
                raise
            if on_event:
                on_event("retry", attempt, e, delay)
            sleep(delay)
    raise AssertionError("unreachable")


# =====================================================================
# 练习 (c)（加分）：half-open 只放行 1 个试探请求的熔断器
# =====================================================================


class SingleProbeCircuitBreaker(CircuitBreaker):
    """agentkit 自带的 CircuitBreaker 在 half_open 时会放行"所有"请求。

    如果此时有 500 个并发请求涌进来，它们会同时打到刚刚恢复（或根本没恢复）的下游上 ——
    这正是熔断器想避免的事。改进：half_open 时只允许 1 个试探请求（probe），
    试探进行中其他请求一律快速失败（CircuitOpenError），由调用方走降级。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._probing = False

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            state = self.state
            if state == "open":
                raise CircuitOpenError(self.name)
            is_probe = state == "half_open"
            if is_probe:
                if self._probing:  # 已经有一个试探请求在路上了
                    raise CircuitOpenError(self.name)
                self._probing = True
        try:
            result = fn()
        except Exception:
            with self._lock:
                self.failures += 1
                if is_probe or self.failures >= self.failure_threshold:
                    self.opened_at = self.clock()  # 试探失败：重新打开，重新计时
            raise
        else:
            with self._lock:
                self.failures = 0
                self.opened_at = None
            return result
        finally:
            if is_probe:
                with self._lock:
                    self._probing = False
