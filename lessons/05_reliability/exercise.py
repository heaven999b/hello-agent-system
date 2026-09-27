"""第 05 课练习：可靠性工程。

一共三题（第三题是加分题）：
  (a) LoopGuard            —— 检测 Agent 死循环（同一工具 + 相同参数反复调用）
  (b) retry_with_budget    —— 带全局重试预算和截止时间的重试，防止重试风暴
  (c) SingleProbeCircuitBreaker（加分）—— half-open 状态只放行 1 个试探请求

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=05
    # 或者：.venv/bin/python -m pytest lessons/05_reliability -v

加分题没做时，它的测试会被自动跳过（skipped），不影响前两题通过。
卡住了？先重读 README 对应小节，再看 solution.py。
"""

from __future__ import annotations

import json  # noqa: F401  （实现 normalize_arguments 时会用到）
import random
import threading
import time
from typing import Callable, TypeVar

from agentkit.hooks import Hook, StopRun  # noqa: F401
from agentkit.reliability import CircuitBreaker, CircuitOpenError, backoff_delay, is_retryable  # noqa: F401

T = TypeVar("T")


# =====================================================================
# 练习 (a)：LoopGuard —— 检测 Agent 死循环
# =====================================================================


def normalize_arguments(arguments: str) -> str:
    """把工具参数（JSON 字符串）归一化，使"语义相同"的参数得到完全相同的字符串。

    为什么需要：模型两次输出的 '{"a": 1, "b": 2}' 和 '{"b":2,"a":1}' 是同一个调用，
    直接比较字符串会漏判死循环。

    规则：
      - 合法 JSON：用 json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        重新序列化（key 排序、去掉空白、中文保持原样）；
      - 空字符串或 None：按 "{}" 处理；
      - 不合法 JSON：返回去掉首尾空白的原始字符串（护栏自己绝不能因为坏参数而崩溃）。

    例：
      normalize_arguments('{"b": 2, "a": 1}')  == '{"a":1,"b":2}'
      normalize_arguments('{"城市": "北京"}')   == '{"城市":"北京"}'
      normalize_arguments('  {bad json ')      == '{bad json'
    """
    raise NotImplementedError("TODO: 练习 (a) —— 实现参数归一化")


class LoopGuard(Hook):
    """死循环护栏：同一工具 + 相同参数在最近 window 次调用里出现 >= max_repeats 次，就判定为"原地打转"。

    处理分两级（先给机会，再止损）：
      1. 本次运行第一次超限 → before_tool 返回一条拒绝理由字符串（工具不会执行，
         这段文字会作为观察反馈给模型），提醒它换思路；
      2. 之后再次超限（任意工具）→ 抛 StopRun("loop_detected", ...)，结束运行。

    ⚠️ 关键设计（也是本题最重要的知识点）：计数必须存在 state.metadata 上，而不是 self 上。
      - 一个 Hook 实例会被同一个 Agent 的所有运行共享 —— 在 Web 服务里就是所有用户、所有租户的并发请求。
        计数放在 self 上，A 用户的调用历史会让 B 用户被误判为死循环（还有并发竞争）；
      - state 会随检查点一起落盘：进程崩溃或人工审批暂停后 resume，计数不会丢；
      - 因此 state 里只能放 JSON 可序列化的东西（list / dict / str / bool / int）。
    经验法则：Hook 实例上只放"配置"（window、max_repeats），不放"某一次运行的状态"。

    建议的数据结构（放在 state.metadata[LoopGuard.STATE_KEY] 下）：
        {"recent": ["工具名:归一化参数", ...],   # 最近 window 次调用的指纹
         "warned": False}                        # 本次运行是否已经警告过

    边界情况：
      - 被拒绝的调用也要计入 recent（模型"又试了一次"本身就是循环的证据）；
      - 窗口是滑动的：只看最近 window 次调用，更早的不算；
      - 参数不同就是不同的调用（search(q="a") 和 search(q="b") 不算重复）；
      - 参数是坏 JSON 也不能崩溃（用 normalize_arguments 的退化规则）。

    使用提示：Agent 按 hooks 列表顺序调用 before_tool，前面的钩子一旦拒绝，后面的钩子就不会被调用。
    所以 LoopGuard 应放在 hooks 列表靠前的位置，才能看到每一次调用尝试。
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
        """提示：
        1. memo = state.metadata.setdefault(self.STATE_KEY, {...})
        2. 指纹 fp = f"{call.name}:{normalize_arguments(call.arguments)}"
        3. 追加到 recent 并截断到最近 window 个（注意要原地修改 state 里的那个列表）
        4. 统计 fp 在 recent 中出现的次数，未超限返回 None；超限按"先警告、再 StopRun"处理
        """
        raise NotImplementedError("TODO: 练习 (a) —— 实现 LoopGuard.before_tool")


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

    构造函数已经写好：内部用"千分之一令牌"为单位的整数（self._ratio / self._tokens / self._max）。
    你只需要实现 on_request 和 try_acquire，并维护三个统计字段 requests / retries / rejected。
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
        """记录一个新请求：requests += 1，并存入 ratio 个令牌（存完不能超过 max_tokens）。记得加锁。"""
        raise NotImplementedError("TODO: 练习 (b) —— 实现 RetryBudget.on_request")

    def try_acquire(self) -> bool:
        """为一次重试申请 1 个令牌（即 SCALE 个内部单位）。

        够：扣减、retries += 1、返回 True；不够：rejected += 1、返回 False。记得加锁。
        """
        raise NotImplementedError("TODO: 练习 (b) —— 实现 RetryBudget.try_acquire")


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
    """带"单请求上限 + 全局预算 + 截止时间"三道闸门的重试。

    任务：调用 fn()，失败时按下面的规则决定是否重试，最终返回 fn 的结果或抛出异常。

    规则（按顺序）：
      0. 如果 deadline 不为 None 且 clock() >= deadline：直接抛 DeadlineExceeded，**不调用 fn**，
         也不调用 budget.on_request()（截止时间传播：别让下游做注定白做的工作）。
      1. 调用 budget.on_request() —— 每次 retry_with_budget 调用只存一次，无论最终成功还是失败。
      2. 循环 attempt = 1..max_attempts，调用 fn()：
         - 成功 → 直接返回结果；
         - 失败（异常 e）：
           a. retry_if(e) 为 False（比如 400 参数错误）→ 立刻重新抛出 e（不重试、不消耗令牌）；
           b. 已经是最后一次尝试 → 重新抛出 e；
           c. 计算退避时间 delay = backoff_delay(attempt, base_delay, max_delay, rng)；
           d. 如果 deadline 不为 None 且 clock() + delay >= deadline →
              on_event("give_up_deadline", attempt, e, delay)，重新抛出 e（**不消耗令牌**）；
           e. 如果 budget.try_acquire() 为 False → on_event("give_up_budget", attempt, e, delay)，重新抛出 e；
           f. on_event("retry", attempt, e, delay)，然后 sleep(delay)，进入下一次尝试。
      on_event 为 None 时不要调用它。

    为什么放弃时抛出的是"原始异常 e"而不是新的异常类型？
      调用方原来怎么处理这个错误（比如 ResilientLLM 捕获 LLMError 去降级），加了预算之后还能照常处理。

    提示：
      - 用 `raise`（不带参数）在 except 块里重新抛出当前异常，保留原始 traceback；
      - clock / sleep / rng 都是注入的，测试会传入假时钟和不真正睡眠的 sleep。
    """
    raise NotImplementedError("TODO: 练习 (b) —— 实现 retry_with_budget")


# =====================================================================
# 练习 (c)（加分）：half-open 只放行 1 个试探请求的熔断器
# =====================================================================


class SingleProbeCircuitBreaker(CircuitBreaker):
    """agentkit 自带的 CircuitBreaker 在 half_open 时会放行"所有"请求。

    如果此时有 500 个并发请求涌进来，它们会同时打到刚刚恢复（或根本没恢复）的下游上 ——
    这正是熔断器想避免的事。改进：half_open 时只允许 1 个试探请求（probe），
    试探进行中其他请求一律快速失败（抛 CircuitOpenError），由调用方走降级。

    要求（call 方法）：
      - state == "open"：抛 CircuitOpenError(self.name)，不调用 fn；
      - state == "half_open"：
          * 如果已经有试探请求在进行（self._probing 为 True）→ 抛 CircuitOpenError；
          * 否则把自己标记为试探请求（self._probing = True），然后调用 fn；
          * 试探成功 → 关闭熔断器（failures = 0，opened_at = None）；
          * 试探失败 → 重新打开（failures += 1，opened_at = self.clock()），并重新抛出异常；
          * 无论成功失败，结束后都要把 self._probing 复位为 False（用 finally）；
      - state == "closed"：行为与父类相同（失败累计到 failure_threshold 次就打开，成功清零）。

    提示：读写 _probing / failures / opened_at 时持有 self._lock；但调用 fn() 时不要持有锁
    （否则慢请求会把所有请求串行化，甚至死锁）。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._probing = False

    def call(self, fn: Callable[[], T]) -> T:
        raise NotImplementedError("TODO: 练习 (c)（加分）—— 实现只放行一个试探请求的 call")
