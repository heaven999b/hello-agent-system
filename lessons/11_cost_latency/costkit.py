"""第 11 课的教学实现：成本与延迟优化工具箱。

    ResponseCache + CachingLLM   精确缓存：同一个租户、一模一样的请求，不再付第二次钱
    CascadeLLM                   级联：先让小模型答，validator 判不合格再升级到大模型
    hedged_call                  对冲请求：等太久就再发一个，谁先回来用谁（用钱换长尾延迟）
    context_cost                 上下文膨胀的成本曲线（问题 5）
    cost_report / budget_alerts  按租户、功能归因成本，预算告警（问题 6）

CachingLLM 和 CascadeLLM 与第 05 课的 ResilientLLM 一样是"LLM 装饰器"：对外仍然是
`.model + .chat()`，Agent 完全无感，而且可以任意叠加：

    llm = CachingLLM(CascadeLLM(small, ResilientLLM(large), validator), cache, scope={...})
    agent = Agent(llm, tools)

⚠️ 这是为了读懂而写的实现：单进程、内存存储。每个组件升级到生产要换成什么，
见 README 里对应问题卡片的"本课实现"。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from agentkit.llm import LLM, LLMError
from agentkit.reliability import is_retryable
from agentkit.types import LLMResponse, Message, Usage, new_call_id

# =====================================================================
# 问题 2：精确缓存
# =====================================================================

KEY_VERSION = "v1"  # 改了键的算法就改版本号：旧缓存自然全部失效，不会误读格式不兼容的旧条目


def canonical_json(obj: Any) -> str:
    """规范化 JSON：key 排序、去掉空白、中文不转义。语义相同的 dict → 完全相同的字符串。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def normalize_call_ids(messages: list[Message]) -> list[Message]:
    """把 tool_call id 换成按出现顺序的编号（#0、#1……），返回新列表，不修改原消息。

    id 是模型随机生成的（如 call_x8Jf2…），不携带任何语义，只用来把 tool 消息和对应的调用配对。
    不做这一步，Agent 第 2 步之后的请求里全是随机 id：两次一模一样的对话，也永远算出不同的键。
    """
    mapping: dict[str, str] = {}

    def canon(call_id: str) -> str:
        return mapping.setdefault(call_id, f"#{len(mapping)}")

    out = []
    for m in messages:
        m = copy.deepcopy(m)
        for tc in m.get("tool_calls") or []:
            if "id" in tc:
                tc["id"] = canon(tc["id"])
        if m.get("tool_call_id") is not None:
            m["tool_call_id"] = canon(m["tool_call_id"])
        out.append(m)
    return out


def normalize_scope(scope: dict) -> dict:
    """作用域里的集合类值排序：roles=["b","a"] 和 ["a","b"] 是同一个权限上下文。"""
    return {k: sorted(map(str, v)) if isinstance(v, (list, tuple, set, frozenset)) else v for k, v in scope.items()}


def cache_key(messages: list[Message], tools: list[dict] | None, model: str, *, scope: dict, params: dict | None = None) -> str:
    """缓存键 = 请求本身（messages + tools + model + 调用参数）+ 所有"影响答案、却不在请求里"的上下文（scope）。

    scope 至少要有 tenant_id；按需加入 roles、user_id（个性化回答）、locale、数据权限版本……
    经验法则：**任何会让两个人得到不同答案的东西，都必须进键**。漏掉一个，就是一次数据泄露。
    """
    payload = {
        "v": KEY_VERSION,
        "scope": normalize_scope(scope),
        "model": model,
        "params": params or {},  # temperature / max_tokens / response_format 不同，答案也可能不同
        "tools": tools or [],  # None 和 [] 都表示"没有工具"
        "messages": normalize_call_ids(messages),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    expired: int = 0  # 查到了但已过期（也算一次 miss）
    not_cacheable: int = 0  # 按策略不缓存的响应：带工具调用、被截断、空回复……
    evicted: int = 0  # 超过容量被 LRU 淘汰
    saved: Usage = field(default_factory=Usage)  # 命中缓存省下的 token

    @property
    def hit_rate(self) -> float:
        n = self.hits + self.misses
        return self.hits / n if n else 0.0


class ResponseCache:
    """进程内的 LRU + TTL 缓存存储，可以被多个 CachingLLM（多个租户的请求）共享。

    - TTL：答案会过时（知识库更新了、政策变了），过期就当不存在；
    - LRU 容量上限：内存不是无限的，最久没被用过的先淘汰；
    - 存取都做深拷贝：调用方改了拿到的响应，不能污染缓存里的原件；
    - 一把锁：CachingLLM 可能被多个线程同时调用（对冲、并行请求）。

    生产替换：Redis（`SET key value EX ttl`，天然跨进程共享，maxmemory-policy 设为 allkeys-lru）。
    """

    def __init__(self, ttl_s: float = 600.0, max_entries: int = 10_000, clock: Callable[[], float] = time.monotonic):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.clock = clock
        self.stats = CacheStats()
        self._data: OrderedDict[str, tuple[float, LLMResponse]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> LLMResponse | None:
        with self._lock:
            item = self._data.get(key)
            if item is not None and self.clock() >= item[0]:
                del self._data[key]
                self.stats.expired += 1
                item = None
            if item is None:
                self.stats.misses += 1
                return None
            self._data.move_to_end(key)  # LRU：刚被用过的挪到队尾
            self.stats.hits += 1
            self.stats.saved = self.stats.saved + item[1].usage
            return copy.deepcopy(item[1])

    def put(self, key: str, response: LLMResponse) -> None:
        with self._lock:
            self._data[key] = (self.clock() + self.ttl_s, copy.deepcopy(response))
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
                self.stats.evicted += 1

    def skip(self) -> None:
        with self._lock:
            self.stats.not_cacheable += 1

    def __len__(self) -> int:
        return len(self._data)


class CachingLLM:
    """精确缓存装饰器：同一作用域（租户 + 权限）下，完全相同的请求直接返回上次的响应。

    典型用法 —— 缓存存储全局共享，装饰器按请求创建（它很轻），作用域来自认证系统：

        CACHE = ResponseCache(ttl_s=600)                      # 进程级单例（生产中是 Redis）

        def handle(request, identity):
            llm = CachingLLM(base_llm, CACHE, scope={"tenant_id": identity["tenant_id"],
                                                      "roles": identity["roles"]})
            return Agent(llm, tools).run(request.text, metadata=identity)

    默认只缓存"最终文本回答"，不缓存"工具调用决定"（cache_tool_calls=False），取舍见 README 问题 2。
    命中时返回的响应 usage 为 0 —— 这次调用确实没花钱，Agent 的 cost_usd 和 trace 会如实反映。
    """

    def __init__(
        self,
        inner: LLM,
        cache: ResponseCache,
        *,
        scope: dict,
        cache_tool_calls: bool = False,
        never_cache_tools: Iterable[str] = (),
    ):
        if not scope or not scope.get("tenant_id"):
            # fail-closed：宁可不用缓存，也不能用一个"不分租户"的缓存
            raise ValueError("scope 必须包含 tenant_id：不分租户的缓存迟早会把 A 公司的答案发给 B 公司")
        self.inner = inner
        self.cache = cache
        self.scope = normalize_scope(scope)
        self.cache_tool_calls = cache_tool_calls
        self.never_cache_tools = set(never_cache_tools)
        self.model = inner.model

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        key = cache_key(messages, tools, self.inner.model, scope=self.scope, params=kwargs)
        hit = self.cache.get(key)
        if hit is not None:
            return self._replay(hit)
        response = self.inner.chat(messages, tools, **kwargs)  # 异常直接抛出：错误绝不进缓存
        if self.why_not_cacheable(response) is None:
            self.cache.put(key, response)
        else:
            self.cache.skip()
        return response

    def why_not_cacheable(self, r: LLMResponse) -> str | None:
        """返回"不该缓存"的原因；None 表示可以缓存。"""
        if r.finish_reason not in ("stop", "tool_calls"):
            return f"finish_reason={r.finish_reason}"  # length（被截断）/ content_filter：别把残缺答案固化下来
        if not r.content and not r.tool_calls:
            return "empty"
        if r.tool_calls:
            if not self.cache_tool_calls:
                return "tool_calls"
            if any(c.name in self.never_cache_tools for c in r.tool_calls):
                return "side_effect_tool"  # 写操作的"决定"不缓存：它应该每次都由模型结合最新上下文重新做
        return None

    @staticmethod
    def _replay(hit: LLMResponse) -> LLMResponse:
        hit.usage = Usage()  # 这次没有调用模型，成本为 0
        for c in hit.tool_calls:
            # 重新生成 tool_call id：同一个缓存响应可能在同一次运行里被重放两次，id 重复会让
            # 工具结果配错对，也会让基于 run_id:call_id 的幂等键误判"已经执行过"
            c.id = new_call_id()
        return hit


# =====================================================================
# 问题 1：级联（先小后大）
# =====================================================================

Validator = Callable[[list[Message], LLMResponse], bool]


class CascadeLLM:
    """级联装饰器：先问小模型，validator 说"合格"就直接用；不合格（或小模型报错）再升级到大模型。

    统计：
      calls / escalations / escalation_rate   升级率 —— 级联省不省钱，全看它
      reasons                                   升级原因分布：rejected / error / validator_error
      small_usage / large_usage                 两个模型各自消耗的 token（真实账单）
      wasted                                    升级时被丢弃的小模型输出 —— 这部分钱白花了，但照样要付

    ⚠️ Agent 的 cost_usd 只按"最终返回的那个响应"计价，看不到被丢弃的小模型调用。
    所以级联要自己记账（small_usage / large_usage），否则你会系统性地低估成本。
    """

    def __init__(self, small: LLM, large: LLM, validator: Validator, *, escalate_on_error: bool = True):
        self.small = small
        self.large = large
        self.validator = validator
        self.escalate_on_error = escalate_on_error
        self.model = f"cascade({small.model}→{large.model})"
        self.calls = 0
        self.escalations = 0
        self.reasons: Counter[str] = Counter()
        self.small_usage = Usage()
        self.large_usage = Usage()
        self.wasted = Usage()
        self._lock = threading.Lock()

    @property
    def escalation_rate(self) -> float:
        return self.escalations / self.calls if self.calls else 0.0

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        with self._lock:
            self.calls += 1
        try:
            draft = self.small.chat(messages, tools, **kwargs)
        except LLMError:
            if not self.escalate_on_error:
                raise
            return self._escalate("error", messages, tools, kwargs)

        with self._lock:
            self.small_usage = self.small_usage + draft.usage
        try:
            ok = bool(self.validator(messages, draft))
            reason = "rejected"
        except Exception:  # noqa: BLE001 —— 校验器自己崩了，不能让请求失败：按"不合格"处理
            ok, reason = False, "validator_error"
        if ok:
            return draft
        with self._lock:
            self.wasted = self.wasted + draft.usage
        return self._escalate(reason, messages, tools, kwargs)

    def _escalate(self, reason: str, messages, tools, kwargs) -> LLMResponse:
        with self._lock:
            self.escalations += 1
            self.reasons[reason] += 1
        response = self.large.chat(messages, tools, **kwargs)  # 大模型也失败就直接抛出，交给外层 ResilientLLM
        with self._lock:
            self.large_usage = self.large_usage + response.usage
        return response


# =====================================================================
# 问题 3：对冲请求
# =====================================================================


@dataclass
class HedgeOutcome:
    value: Any
    winner: int  # 0 = 原始请求胜出，1 = 第一个对冲请求胜出……
    launched: int  # 一共发出了几个请求 —— 这就是你付的钱
    elapsed_s: float


_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _default_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="hedge")
        return _pool


def hedged_call(
    fn: Callable[[], Any],
    *,
    hedge_after_s: float,
    max_requests: int = 2,
    executor: ThreadPoolExecutor | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> HedgeOutcome:
    """对冲请求：先发一个；hedge_after_s 秒后还没回来，再发一个一模一样的；谁先成功用谁。

    - hedge_after_s 通常设成这个调用的 p90~p95 延迟：只有最慢的 5%~10% 请求会触发对冲，
      额外成本 ≈ 5%~10%，却能把 p99 拉回到接近 p95（原理见 README 问题 3）；
    - 某个请求失败了：还有别的在跑就等别的；都失败了，如果错误可重试且名额没用完，立刻再发一个（不再干等）；
    - 输了的请求**无法真正取消**（Python 线程杀不掉，HTTP 请求可能已经在服务端生成）：它的 token 通常照样计费；
    - ⚠️ 只能用于没有副作用的调用。对冲一个"退款"请求 = 退两次款。
    """
    pool = executor or _default_pool()
    start = clock()
    futures: list[Future] = [pool.submit(fn)]
    pending: set[Future] = set(futures)
    last_error: BaseException | None = None

    while True:
        can_hedge = len(futures) < max_requests
        done, pending = wait(pending, timeout=hedge_after_s if can_hedge else None, return_when=FIRST_COMPLETED)
        if not done:  # 等够了还没有任何一个回来 → 发对冲请求
            f = pool.submit(fn)
            futures.append(f)
            pending.add(f)
            continue
        for f in sorted(done, key=futures.index):
            if f.exception() is None:
                return HedgeOutcome(f.result(), futures.index(f), len(futures), clock() - start)
            last_error = f.exception()
        if pending:
            continue
        if len(futures) < max_requests and isinstance(last_error, Exception) and is_retryable(last_error):
            f = pool.submit(fn)  # 全失败了但名额还在：别再等 hedge_after_s，立刻补发
            futures.append(f)
            pending.add(f)
            continue
        assert last_error is not None
        raise last_error


# =====================================================================
# 问题 5：上下文膨胀的成本曲线
# =====================================================================


def context_cost(
    steps: int,
    *,
    base: int,
    growth: int,
    window: int | None = None,
    cache_ratio: float | None = None,
) -> dict:
    """估算一次 N 步 Agent 运行的输入 token 与"折算后计费 token"。

    模型：第 k 步（k 从 1 开始）的输入 = base（system + 工具定义）+ (k-1) × growth（每步新增的调用和结果）。
      - 只追加（window=None）：总输入 = N·base + growth·N(N-1)/2 —— 随步数**平方**增长；
      - 滑动窗口（window=W）：每步输入封顶 W，总量变成线性，但窗口一旦开始滑动，
        前缀里只剩 base（system + 工具定义）没变，后面的提示词缓存全部失效；
      - 提示词缓存（cache_ratio=r）：和上一步相同的前缀按 r 倍单价计费（r 是缓存命中价 / 正常价）。

    返回 {"inputs": 每步输入 token, "total_input": 总输入 token, "billed": 折算成全价后的 token 数}。
    这是忽略了缓存最小长度、过期时间等细节的理想化模型，用来看趋势，不用来做精确报价。
    """
    inputs: list[int] = []
    billed = 0.0
    prev: int | None = None
    for k in range(1, steps + 1):
        full = base + (k - 1) * growth
        n = full if window is None else min(full, window)
        if cache_ratio is None or prev is None:
            cached = 0
        elif window is not None and full > window:
            cached = min(base, n)  # 窗口在滑：只有最前面的 system + 工具定义和上一步一样
        else:
            cached = prev  # 只追加：上一步的整个输入都是这一步的前缀
        billed += (n - cached) + cached * (cache_ratio or 0.0)
        inputs.append(n)
        prev = n
    return {"inputs": inputs, "total_input": sum(inputs), "billed": round(billed)}


# =====================================================================
# 问题 6：成本归因与预算
# =====================================================================


def cost_report(spans: list[dict], dims: tuple[str, ...] = ("tenant.id",)) -> dict[tuple, dict]:
    """把 jsonl_exporter 导出的扁平 span 按根 span 上的维度（租户、功能……）汇总成本。

    约定：服务层给每个请求开一个根 span，带上可信的维度标签，agent.run 是它的子 span：

        with tracer.span("request", **{"tenant.id": "acme", "app.feature": "faq"}):
            agent.run(...)

    返回 {(维度值...): {"runs", "completed", "cost_usd", "cost_per_success"}}。
    cost_per_success = 总成本 / 成功次数 —— 失败的运行也花了钱，这才是真实的单位经济。
    同一个 run_id 出现多次（run 之后又 resume）时取最大的 agent.cost_usd（它是累计值，相加会重复计算）。
    """
    roots = {s["trace_id"]: s for s in spans if s.get("parent_id") is None}
    best: dict[str, dict] = {}
    for s in spans:
        a = s.get("attrs", {})
        if "agent.cost_usd" not in a:
            continue
        rid = a.get("run_id") or s["span_id"]
        if rid not in best or a["agent.cost_usd"] > best[rid]["attrs"]["agent.cost_usd"]:
            best[rid] = s

    report: dict[tuple, dict] = defaultdict(lambda: {"runs": 0, "completed": 0, "cost_usd": 0.0})
    for s in best.values():
        root_attrs = roots.get(s["trace_id"], {}).get("attrs", {})
        key = tuple(root_attrs.get(d, "unknown") for d in dims)
        row = report[key]
        row["runs"] += 1
        row["completed"] += s["attrs"].get("agent.status") == "completed"
        row["cost_usd"] += s["attrs"]["agent.cost_usd"]
    for row in report.values():
        row["cost_usd"] = round(row["cost_usd"], 6)
        row["cost_per_success"] = round(row["cost_usd"] / row["completed"], 6) if row["completed"] else None
    return dict(report)


def budget_alerts(costs: dict[str, float], budgets: dict[str, float], warn_ratio: float = 0.8) -> list[tuple[str, str, float]]:
    """按预算检查每个租户：返回 [(租户, "warn" | "exceeded", 使用比例)]。

    分两级是为了给人留反应时间：到 80% 先通知客户成功经理和租户管理员，到 100% 再执行策略
    （降级到小模型 / 限流 / 只读 / 停服，取决于合同）。没有配置预算的租户不检查，但应该在报表里单独列出。
    """
    alerts = []
    for tenant, cost in sorted(costs.items()):
        budget = budgets.get(tenant)
        if not budget:
            continue
        ratio = cost / budget
        if ratio >= 1:
            alerts.append((tenant, "exceeded", ratio))
        elif ratio >= warn_ratio:
            alerts.append((tenant, "warn", ratio))
    return alerts
