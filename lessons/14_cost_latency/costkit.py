"""第 14 课的教学实现：成本与延迟优化工具箱（async）。

    ResponseCache                进程内的 LRU + TTL 缓存：第一步，零依赖、最快，但每个进程各有一份
    SQLiteResponseCache          跨进程共享的缓存：同一台机器上的所有 worker 进程读写同一个 SQLite 文件
    CachingLLM                   精确缓存装饰器：同一个租户、一模一样的请求，不再付第二次钱（两种存储都能用）
    CascadeLLM                   级联：先让小模型答，validator 判不合格再升级到大模型
    MeteredLLM                   计量：每次调用是完成了（产生 usage、要付钱）、失败了，还是被取消了；在途数量
    hedged_call                  对冲请求：等太久就再发一个，谁先回来用谁，**输的那个立刻取消**
    context_cost                 上下文膨胀的成本曲线（问题 5）
    cost_report / budget_alerts  按租户、功能归因成本，预算告警（问题 6）

CachingLLM、CascadeLLM、MeteredLLM 与第 08 课的 ResilientLLM 一样是"LLM 装饰器"：对外仍然是
`.model + async chat()`，Agent 完全无感，而且可以任意叠加：

    llm = CachingLLM(CascadeLLM(small, ResilientLLM(large), validator), cache, scope={...})
    agent = Agent(llm, tools)
    result = await agent.run(text, metadata=identity)

一个进程里，这些装饰器被很多并发的会话（asyncio 任务）同时使用；多个 worker 进程之间，
共享的只有 SQLiteResponseCache 背后的那个数据库文件（Demo 场景 1b 用真实的 worker 进程演示）。
每个组件升级到生产要换成什么（Redis、模型网关……），见 README 里对应问题卡片的"本课实现"。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from agentkit.distributed import SQLiteDB
from agentkit.llm import LLM, LLMError
from agentkit.reliability import is_retryable
from agentkit.tools import maybe_await
from agentkit.types import LLMResponse, Message, ToolCall, Usage, new_call_id

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
    用 hashlib.sha256 而不是内置 hash()：多个进程共享一个缓存时，同一个请求在每个进程里必须算出同一个键。
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


def response_to_json(r: LLMResponse) -> str:
    """LLMResponse → JSON 文本（跨进程的缓存只能存字节，不能存 Python 对象）。"""
    return json.dumps(asdict(r), ensure_ascii=False)


def response_from_json(text: str) -> LLMResponse:
    d = json.loads(text)
    return LLMResponse(
        content=d.get("content"),
        tool_calls=[ToolCall(**c) for c in d.get("tool_calls") or []],
        usage=Usage(**(d.get("usage") or {})),
        model=d.get("model") or "",
        finish_reason=d.get("finish_reason") or "stop",
    )


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
    - 不需要锁：get / put 里没有 await，在一个事件循环里执行时不会被别的协程打断（整段就是原子的）。

    局限：它活在**一个进程的内存里**。3 个 worker 进程就是 3 份互不相通的缓存 ——
    同一个问题要在每个进程里各未命中一次；进程重启，缓存清零。跨进程共享用 SQLiteResponseCache（单机），
    多机用 Redis（`SET key value EX ttl`，maxmemory-policy 设为 allkeys-lru）。常见的组合是两级：
    进程内 LRU 挡住最热的那一小撮（零网络开销），未命中再查共享缓存。
    """

    def __init__(self, ttl_s: float = 600.0, max_entries: int = 10_000, clock: Callable[[], float] = time.monotonic):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.clock = clock
        self.stats = CacheStats()
        self._data: OrderedDict[str, tuple[float, LLMResponse]] = OrderedDict()

    def get(self, key: str) -> LLMResponse | None:
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
        self._data[key] = (self.clock() + self.ttl_s, copy.deepcopy(response))
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)
            self.stats.evicted += 1

    def skip(self) -> None:
        self.stats.not_cacheable += 1

    def __len__(self) -> int:
        return len(self._data)


class SQLiteResponseCache:
    """跨进程共享的响应缓存：同一台机器上的所有 worker 进程读写同一个 SQLite 文件（agentkit.distributed.SQLiteDB）。

    和 ResponseCache 同样的语义（TTL、LRU 容量上限、命中统计），区别在于存储：
    - 进程 A 算出来的答案，进程 B 直接命中；进程重启，缓存还在；
    - 每次读写都是一次数据库事务（在 SQLiteDB 的专用线程里执行，不阻塞事件循环），比进程内字典慢几十倍
      （本机实测：进程内命中约 13 微秒，SQLite 命中约 0.6 毫秒，见 README 问题 2），但比一次模型调用（秒级）快三个数量级以上；
    - 时钟用 time.time()（墙上时钟）：过期时间要在多个进程之间比较，进程各自的 monotonic 时钟不能混用；
    - writer 列记下是哪个进程写入的（worker id / pid），用来证明"命中的是别的进程算出来的答案"。
    stats 是**本进程**看到的命中情况；所有进程加起来的数字，要从各进程的统计汇总。

    局限（如实说明）：只能在一台机器上（SQLite 文件不能放在网络文件系统上）；同一时刻只有一个写者，
    写入吞吐有上限（第 13 课实测过）。多机部署换成 Redis，接口不变（get / put / skip）。
    """

    def __init__(
        self,
        path_or_db: str | Path | SQLiteDB,
        table: str = "llm_cache",
        *,
        ttl_s: float = 600.0,
        max_entries: int = 10_000,
        writer: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if not table.replace("_", "").isalnum() or not table[0].isalpha():
            raise ValueError(f"非法的表名：{table!r}")
        if isinstance(path_or_db, SQLiteDB):
            self.db, self._owns_db = path_or_db, False
        else:
            self.db, self._owns_db = SQLiteDB(path_or_db), True
        self.table = table
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.writer = writer
        self.clock = clock
        self.stats = CacheStats()
        self._ready = False

    async def setup(self) -> None:
        t = self.table

        def ddl(conn):
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {t} (key TEXT PRIMARY KEY, response TEXT NOT NULL, "
                f"expires_at REAL NOT NULL, last_used REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0, writer TEXT)"
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_lru_idx ON {t} (last_used)")

        await self.db.write(ddl)
        self._ready = True

    async def _ensure(self) -> None:
        if not self._ready:
            await self.setup()

    async def get(self, key: str) -> LLMResponse | None:
        await self._ensure()
        t = self.table

        def op(conn):
            now = self.clock()
            row = conn.execute(f"SELECT response, expires_at FROM {t} WHERE key = ?", (key,)).fetchone()
            if row is None:
                return "miss", None
            if now >= row["expires_at"]:
                conn.execute(f"DELETE FROM {t} WHERE key = ?", (key,))
                return "expired", None
            conn.execute(f"UPDATE {t} SET last_used = ?, hits = hits + 1 WHERE key = ?", (now, key))  # LRU：记下最近使用时间
            return "hit", row["response"]

        status, raw = await self.db.write(op)  # 读 + 更新 last_used 在一个写事务里，别的进程插不进来
        if status != "hit":
            self.stats.expired += status == "expired"
            self.stats.misses += 1
            return None
        response = response_from_json(raw)
        self.stats.hits += 1
        self.stats.saved = self.stats.saved + response.usage
        return response

    async def put(self, key: str, response: LLMResponse) -> None:
        await self._ensure()
        t = self.table
        data = response_to_json(response)

        def op(conn):
            now = self.clock()
            conn.execute(
                f"INSERT OR REPLACE INTO {t} (key, response, expires_at, last_used, hits, writer) VALUES (?, ?, ?, ?, 0, ?)",
                (key, data, now + self.ttl_s, now, self.writer),
            )
            n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            if n <= self.max_entries:
                return 0
            return conn.execute(
                f"DELETE FROM {t} WHERE key IN (SELECT key FROM {t} ORDER BY last_used LIMIT ?)", (n - self.max_entries,)
            ).rowcount

        self.stats.evicted += await self.db.write(op)

    def skip(self) -> None:
        self.stats.not_cacheable += 1

    async def entries(self) -> list[dict]:
        """所有条目的摘要（给 Demo / 运维看：谁写入的、被命中了几次）。"""
        await self._ensure()
        rows = await self.db.run(lambda conn: conn.execute(
            f"SELECT key, writer, hits, expires_at, last_used FROM {self.table} ORDER BY last_used"
        ).fetchall())
        return [dict(r) for r in rows]

    async def close(self) -> None:
        if self._owns_db:
            await self.db.close()


class CachingLLM:
    """精确缓存装饰器：同一作用域（租户 + 权限）下，完全相同的请求直接返回上次的响应。

    典型用法 —— 缓存存储全局共享，装饰器按请求创建（它很轻），作用域来自认证系统：

        CACHE = SQLiteResponseCache("runs/cache.db")            # 所有 worker 进程共用（多机：Redis）

        async def handle(request, identity):
            llm = CachingLLM(base_llm, CACHE, scope={"tenant_id": identity["tenant_id"],
                                                      "roles": identity["roles"]})
            return await Agent(llm, tools).run(request.text, metadata=identity)

    cache 可以是 ResponseCache（get / put 是普通方法）或 SQLiteResponseCache（async），两种都支持。
    按请求创建时，hits / misses 就是这个请求自己的命中情况（存储上的 stats 是整个进程的）。

    默认只缓存"最终文本回答"，不缓存"工具调用决定"（cache_tool_calls=False），取舍见 README 问题 2。
    命中时返回的响应 usage 为 0 —— 这次调用确实没花钱，Agent 的 cost_usd 和 trace 会如实反映。
    没有做 single-flight：同一个键的几个请求**同时**未命中，会各自调用一次模型（缓存击穿，见 README 5.2）。
    """

    def __init__(
        self,
        inner: LLM,
        cache: Any,
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
        self.hits = 0
        self.misses = 0

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        key = cache_key(messages, tools, self.inner.model, scope=self.scope, params=kwargs)
        hit = await maybe_await(self.cache.get(key))
        if hit is not None:
            self.hits += 1
            return self._replay(hit)
        self.misses += 1
        response = await self.inner.chat(messages, tools, **kwargs)  # 异常直接抛出：错误绝不进缓存
        if self.why_not_cacheable(response) is None:
            await maybe_await(self.cache.put(key, response))
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

    同一个 CascadeLLM 会被很多并发的会话同时使用，为什么计数不加锁？asyncio 里协程只在 await 处让出控制权，
    `self.calls += 1`、`self.wasted = self.wasted + draft.usage` 这种"读-改-写"中间没有 await，
    执行时不会被别的协程插队，所以是原子的。只有"读 → await 别的东西 → 再写回"才需要 asyncio.Lock。
    （多线程里就不一样了：线程可以在任意两条字节码之间被切走，那时才需要 threading.Lock。）
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

    @property
    def escalation_rate(self) -> float:
        return self.escalations / self.calls if self.calls else 0.0

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        self.calls += 1
        try:
            draft = await self.small.chat(messages, tools, **kwargs)
        except LLMError:
            if not self.escalate_on_error:
                raise
            return await self._escalate("error", messages, tools, kwargs)

        self.small_usage = self.small_usage + draft.usage
        try:
            ok = bool(self.validator(messages, draft))
            reason = "rejected"
        except Exception:  # noqa: BLE001 —— 校验器自己崩了，不能让请求失败：按"不合格"处理
            ok, reason = False, "validator_error"
        if ok:
            return draft
        self.wasted = self.wasted + draft.usage
        return await self._escalate(reason, messages, tools, kwargs)

    async def _escalate(self, reason: str, messages, tools, kwargs) -> LLMResponse:
        self.escalations += 1
        self.reasons[reason] += 1
        response = await self.large.chat(messages, tools, **kwargs)  # 大模型也失败就直接抛出，交给外层 ResilientLLM
        self.large_usage = self.large_usage + response.usage
        return response


# =====================================================================
# 问题 3：计量与对冲请求
# =====================================================================


class MeteredLLM:
    """计量装饰器：每次调用的结局是什么 —— 完成（拿到响应和 usage，要付钱）、失败，还是被取消（没拿到响应）。

      started / completed / failed / cancelled   调用次数（cancelled = 等待中被取消，CancelledError 从这里穿过）
      in_flight / max_in_flight                  此刻 / 历史上同时在等模型的调用数 —— "并发真的发生了"的证据
      billed                                     完成的调用累计的 usage

    "取消了就不付钱"只在**本进程能看到的范围内**成立：被取消的调用没有拿到响应，也就没有 usage。
    服务端是否已经生成、是否照样计费，取决于厂商和是否流式（见 README 问题 3）。
    """

    def __init__(self, inner: LLM):
        self.inner = inner
        self.model = inner.model
        self.started = self.completed = self.failed = self.cancelled = 0
        self.in_flight = self.max_in_flight = 0
        self.billed = Usage()

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        self.started += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            response = await self.inner.chat(messages, tools, **kwargs)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        except Exception:
            self.failed += 1
            raise
        finally:
            self.in_flight -= 1
        self.completed += 1
        self.billed = self.billed + response.usage
        return response

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()


@dataclass
class HedgeOutcome:
    value: Any
    winner: int  # 0 = 原始请求胜出，1 = 第一个对冲请求胜出……
    launched: int  # 一共发出了几个请求
    cancelled: int  # 胜者出现时还没完成、被取消掉的请求数（已经等到它们真正结束）
    elapsed_s: float


async def _cancel_and_wait(tasks: Iterable[asyncio.Future]) -> int:
    """取消这些任务并等它们真正结束（finally 跑完、连接释放），返回确实以"被取消"结束的个数。"""
    tasks = [t for t in tasks if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks)  # 用 wait 而不是 gather：只等待，不把子任务的异常抛给我们
    for t in tasks:  # 取走异常，避免 "Task exception was never retrieved"
        if not t.cancelled():
            t.exception()
    return sum(t.cancelled() for t in tasks)


async def hedged_call(
    fn: Callable[[], Awaitable[Any]],
    *,
    hedge_after_s: float,
    max_requests: int = 2,
    clock: Callable[[], float] = time.monotonic,
) -> HedgeOutcome:
    """对冲请求：先发一个；hedge_after_s 秒后还没回来，再发一个一模一样的；谁先成功用谁，**其余的立刻取消**。

    fn：无参函数，每次调用返回一个新的协程，例如 `lambda: llm.chat(messages)`。

    - hedge_after_s 通常设成这个调用的 p90~p95 延迟：只有最慢的 5%~10% 请求会触发对冲，
      额外成本 ≈ 5%~10%，却能把 p99 拉回到接近 p95（原理见 README 问题 3）；
    - 胜者出现后，还在跑的请求被 cancel()：CancelledError 送进它正在 await 的地方（ScriptedLLM 的 asyncio.sleep、
      OpenAICompatLLM 里 httpx 的读响应），这次 HTTP 请求被中止、在途数归零。我们等它们真正结束才返回，不留悬空任务；
    - 某个请求失败了：还有别的在跑就等别的；都失败了，如果错误可重试且名额没用完，立刻再发一个（不再干等）；
    - 调用方取消了 hedged_call（例如用户断开）：所有在途请求一起取消；
    - ⚠️ 只能用于没有副作用的调用。对冲一个"退款"请求 = 退两次款：取消只能保证"不再等它"，
      不能保证服务端没有执行。
    """
    if max_requests < 1:
        raise ValueError("max_requests 至少为 1")
    start = clock()
    tasks: list[asyncio.Future] = []

    def launch() -> asyncio.Future:
        t = asyncio.ensure_future(fn())
        tasks.append(t)
        return t

    pending: set[asyncio.Future] = {launch()}
    last_error: BaseException | None = None
    try:
        while True:
            can_hedge = len(tasks) < max_requests
            done, pending = await asyncio.wait(pending, timeout=hedge_after_s if can_hedge else None,
                                               return_when=asyncio.FIRST_COMPLETED)
            if not done:  # 等够了还没有任何一个回来 → 发对冲请求
                pending.add(launch())
                continue
            winner = None
            for t in sorted(done, key=tasks.index):
                if t.cancelled():
                    last_error = asyncio.CancelledError("对冲中的请求被外部取消")
                elif t.exception() is not None:
                    last_error = t.exception()
                elif winner is None:
                    winner = t
            if winner is not None:
                cancelled = await _cancel_and_wait(pending)  # 输家立刻取消，等它们收尾
                return HedgeOutcome(winner.result(), tasks.index(winner), len(tasks), cancelled, clock() - start)
            if pending:
                continue
            if len(tasks) < max_requests and isinstance(last_error, Exception) and is_retryable(last_error):
                pending.add(launch())  # 全失败了但名额还在：别再等 hedge_after_s，立刻补发
                continue
            assert last_error is not None
            raise last_error
    finally:
        # 正常返回时这里已经没有在途任务；调用方取消 / 抛异常时，把还在跑的请求全部取消，不让它们在后台继续花钱
        await _cancel_and_wait(tasks)


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
            await agent.run(...)

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
