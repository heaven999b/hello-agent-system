[中文](README.md) | [English](README.en.md)

# 第 08 课：可靠性工程 —— 让 Agent 在失败中存活

> 🕐 建议用时：20 分钟 ｜ 🎯 学完你能：面对限流、宕机、死循环、崩溃、重复副作用、长时间审批这六类企业常见故障，说出 2-4 种方案的取舍并选对方案 ｜ 📦 对应源码：`agentkit/reliability.py`、`agentkit/budget.py`、`agentkit/state.py`、`agentkit/tools.py`（幂等）、`agentkit/agent.py`（恢复）、`agentkit/distributed/sqlite.py`（跨进程的熔断器与幂等存储）
>
> 📖 必读：[Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/)（Mike Ulrich, 2016）—— Google SRE Book 第 22 章，讲清故障如何沿调用链层层放大，本课问题 1 里"多层重试相乘"的例子就出自这里；重点读 "Retries" 和 "Latency and Deadlines" 两节，理解为什么重试要有预算、截止时间要沿调用链传递。

## 0. 一句话讲清楚

**可靠的系统不是"不出故障"，而是"出了故障也能把事办成，并且不办错"。**

想象一个靠谱的快递员送一天的件：

| 快递员遇到的事 | 他怎么做 | 对应的工程手段 |
|---|---|---|
| 敲门没人应 | 等一会儿再敲，每次等得更久一点 | **重试 + 指数退避** |
| 整栋楼的快递员约好 10 点同时再来 | 各自随便挑个时间来，别挤电梯 | **抖动（jitter）** |
| 一条路连续三次都堵死 | 这段时间别走这条路，过会儿派一个人去探路 | **熔断器（circuit breaker）** |
| 主干道封了 | 绕小路；再不行放驿站；再不行让客户自取 | **降级链（fallback）** |
| 今天工时到了 | 收工，而不是通宵跑 | **预算（budget）** |
| 送到一半车坏了 | 换辆车，从上一个签收点接着送 | **检查点（checkpoint）** |
| 不确定上一单签收了没有 | 先查签收记录，别送两次 | **幂等键（idempotency key）** |
| 贵重物品要本人签字 | 先送别的件，等人回来再说 | **暂停 / 恢复** |

**为什么 Agent 比普通 API 更需要这些？** 一次 Agent 运行要调用模型和工具 20 次，每次成功率 99%，整次运行全部成功的概率只有 $0.99^{20} \approx 81.8\%$ —— **五个用户里就有一个看到报错**。每次调用失败后重试 2 次（假设失败相互独立），整次成功率回到 99.998%。但真实故障里"相互独立"往往不成立：供应商宕机时所有请求**同时**失败，重试只会雪上加霜。所以还需要熔断、降级、预算、检查点……

本课属于课程第二部分：**先给出企业里的真实问题，再对比多种方案，最后看 agentkit 怎么实现。**

## 1. 核心概念

### 1.1 Agent 会怎样失败：四类故障

对错误分类，决定了应对是否正确：把 400 当成可重试，你会白白浪费配额；把 503 当成不可重试，你会放弃本来能成功的请求。

```mermaid
flowchart TB
    F["Agent 的失败"] --> I["基础设施故障"]
    F --> T["工具故障"]
    F --> M["模型行为故障"]
    F --> P["进程与流程故障"]
    I --> I1["429 限流 / 5xx / 超时 / 断连<br/>→ 重试、熔断、降级"]
    I --> I2["400 / 401 / 404 / 上下文超长<br/>→ 不重试，立刻失败并告警"]
    T --> T1["超时 / 抛异常 / 下游不可用 / 返回超大结果<br/>→ 超时、错误即观察、截断"]
    M --> M1["死循环 / 重复调用 / 幻觉参数 / 提前放弃<br/>→ 参数校验、循环检测、预算、评估"]
    P --> P1["崩溃 / OOM / 滚动发布 / 等人审批<br/>→ 检查点、幂等、暂停恢复"]
```

**模型行为故障是 Agent 独有的。** 普通服务的 bug 稳定复现，模型的"坏行为"是概率性的：同一个输入，100 次里可能有 3 次陷入循环。你没法"修好"它，只能**限制它的破坏半径**，并用评估集（第 11 课）监控它的发生率。

### 1.2 可靠性的"洋葱"：每一层兜住上一层漏下的

```mermaid
flowchart LR
    U["用户请求<br/>带截止时间"] --> B
    subgraph AG["Agent 运行"]
        B["预算 + 循环检测<br/>BudgetHook / LoopGuard"] --> L
        subgraph RL["ResilientLLM"]
            L["重试<br/>退避 + 抖动"] --> C["熔断器"] --> FB["降级链<br/>主模型 → 备用模型"]
        end
        B --> TL["工具调用<br/>超时 + 幂等"]
    end
    AG -.每一步都存盘.-> CP[("检查点<br/>Checkpointer")]
    FB --> P1["模型供应商 A"]
    FB --> P2["模型供应商 B"]
    TL --> DS["下游系统<br/>工单 / 支付 / CRM"]
```

- **重试**处理偶发、短暂的失败；**熔断**处理持续的失败（这时重试有害）；**降级**让熔断之后还有路可走；
- **预算和循环检测**处理"模型自己停不下来"；
- **检查点和幂等**处理"进程本身死掉了"。

### 1.3 本课的六张问题卡片

| # | 企业问题 | 关键技术 | agentkit | 练习 |
|---|---|---|---|---|
| 1 | 高峰期模型 API 频繁 429 | 退避 + 抖动、重试预算、截止时间 | `retry_call`、`backoff_delay` | (b) `retry_with_budget` |
| 2 | 主模型供应商宕机 | 熔断器、降级链、跨进程共享熔断状态 | `CircuitBreaker`、`ResilientLLM`、`SQLiteCircuitBreaker` | (c) 半开单试探 |
| 3 | Agent 死循环烧钱 | 多维预算、循环检测 | `BudgetHook` | (a) `LoopGuard` |
| 4 | 进程崩溃、发布重启 | 检查点、持久化执行 | `FileCheckpointer`、`Agent.resume` | — |
| 5 | 恢复后重复退款 / 重复建单 | 幂等键 | `SQLiteIdempotencyStore`、`ToolContext.idempotency_key` | — |
| 6 | 审批要等好几个小时 | 暂停落盘、异步恢复 | `PauseRun`、`Agent.approve` | — |

## 2. 企业问题卡片

### 问题 1：高峰期模型 API 频繁返回 429

**场景**：一家 2000 人公司的 IT 助手，工作日 9:00–9:30 高峰每秒启动 30 个 Agent 运行，每个运行平均调用模型 6 次 → 峰值约 180 次/秒。供应商给的限额是每分钟 6000 次（100 次/秒）。高峰期约 45% 的调用返回 `429 Too Many Requests`。

**为什么难**：

- **什么都重试是错的。** 400（参数错、上下文超长）、401（key 失效）重试一万次也是同样结果，还可能触发风控；只有 429、5xx、超时、断连这类**瞬时错误**才值得重试；
- **固定间隔重试会制造"整齐的冲锋"。** 一大批客户端同时收到 429、都等同样长的时间再重试，服务端在同一瞬间又收到一整批请求（**惊群效应**，thundering herd），于是又是大部分被拒、再整齐地等、再一起冲。Demo 场景 1 做了一个真实的实验（不是用随机数画直方图）：500 个并发客户端同时打一个**独立进程**里的网关，网关同时最多处理 50 个请求、每个 100ms，满了立刻返回 429；表里的数字全部来自网关自己的日志（MacBook 8 核 / 8GB，负载约 4，一次运行的结果）：

  ```text
  策略                总请求  被 429  重试最密的 10ms  全部成功  一半成功  网关利用率
  固定间隔 0.5s       2750    2250    324              4.67s     2.64s     21%
  指数退避，无抖动    2750    2250    340              6.66s     2.64s     15%
  指数退避 + 全抖动   2001    1501    76               1.64s     0.61s     61%
  ```

  固定间隔时，每 0.5 秒有一堵"墙"：几百个重试挤在同一个 10ms 里到达，网关只接得住 50 个，其余全部 429，两堵墙之间网关却闲着（利用率 21%）。无抖动的指数退避只是把墙与墙的间隔拉长了。全抖动把同样的重试摊开在时间轴上：429 少了三分之一，所有人都成功的时间缩短到约 1/3。

- **重试会放大流量。** Google SRE 书举过例子：数据库过载时，后端、前端、JavaScript 三层各自重试 3 次（每层 4 次尝试），一次用户操作最多在数据库上产生 $4^3=64$ 次请求。Agent 系统同样有前端 → 网关 → Agent 服务 → 模型网关多层；
- **持续超额时，重试只是把失败推迟。** 需求是限额的 1.8 倍，重试再多也塞不进去，只会增加延迟；
- **重试不能超过截止时间。** 用户请求 90 秒超时，单次模型调用超时 30 秒、最多 3 次 → 最坏 90 秒加退避，最后一次注定做到一半被取消，白白消耗配额。

```mermaid
flowchart LR
    U["前端<br/>尝试 3 次"] --> G["API 网关<br/>尝试 3 次"] --> A["Agent 服务<br/>尝试 3 次"] --> MG["模型网关<br/>尝试 3 次"] --> P["模型供应商<br/>正在过载"]
    P -.- N["最坏情况：3 × 3 × 3 × 3 = 81 倍流量"]
```

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 客户端重试 | 只重试瞬时错误；指数退避 + 全抖动；单请求上限 + **全局重试预算** + 截止时间 | 实现简单，对偶发限流非常有效 | 持续超额时无效；多层重试会放大 | 偶发 429、需求低于限额 |
| B. 队列削峰 | 请求先进队列，按供应商限额匀速出队（令牌桶限速），可按优先级调度 | 平滑突发，不浪费配额，不触发 429 | 增加排队延迟；需要队列和分布式限流器 | 批处理、异步任务、能接受等待 |
| C. 多供应商 / 多账号负载均衡 | 模型网关按各自配额把流量分到多个供应商、区域或账号 | 提高总容量，顺便获得容灾能力 | 不同模型行为有差异，需要评估；合同与成本更复杂 | 峰值持续超过单一限额 |
| D. 减少调用 | 缓存相同问题的回答、简单问题分流给小模型、合并调用 | 从源头降低需求和成本 | 需要额外工程，命中率不确定 | 重复问题多、成本敏感 |

**怎么选**：先算账 —— 峰值需求 ÷ 限额。**A 是所有系统的必备底座**（任何规模都会遇到偶发 429）；比值长期大于约 0.8 时，A 已经不够：可延迟的任务上 B，交互式任务上 C；D 永远值得做（见[第 14 课：成本与延迟](../14_cost_latency/README.md)）。多实例部署时，限流器和重试预算要做成全局共享的（见[第 13 课：高并发与分布式](../13_distributed_concurrency/README.md)）。

**本课实现**：方案 A。

1. **错误分类在 LLM 适配层完成**（[agentkit/llm.py](../../agentkit/llm.py) 的 `map_openai_error`），上层只看 `retryable` 一个布尔值，换厂商时重试逻辑不用改：

   ```python
   code = e.status_code
   # 429 限流、408 超时、5xx 服务端错误：重试可能成功；400/401/403/404：重试没用。
   # 但 429 有两种：限流（等一等就好）和额度用完（insufficient_quota，重试一万次也没用）。
   retryable = code in (408, 409, 429) or code >= 500
   if code == 429 and "insufficient_quota" in str(e):
       retryable = False
   retry_after = float(e.response.headers.get("retry-after"))   # 服务端建议的等待秒数（没有这个头时为 None）
   return LLMError(str(e), status_code=code, retryable=retryable, retry_after=retry_after)
   ```

   这个判定与 OpenAI 官方 Python SDK 的默认重试判定一致。注意 `OpenAICompatLLM` 创建客户端时写了 `max_retries=0`：**故意关掉 SDK 自带的重试**，否则 SDK 重试 2 次、你的代码再重试 3 次，一次调用最多变成 9 次请求，而且 SDK 内部的重试你看不见。**重试只在一层做，并且做在看得见的地方。**

2. **全抖动指数退避**（[agentkit/reliability.py](../../agentkit/reliability.py)）：

   ```python
   def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0, rng: random.Random | None = None) -> float:
       upper = min(cap, base * (2 ** (attempt - 1)))   # 指数增长：0.5s、1s、2s、4s……封顶 8s
       return (rng or random).uniform(0, upper)         # 全抖动：在 [0, 上限] 内均匀随机
   ```

   AWS 的 Marc Brooker 用模拟比较过"不抖动 / 全抖动 / 等值抖动 / 去相关抖动"：不抖动的方案做的工作最多、耗时也最长；全抖动的总调用量最少，而且实现最简单。上面 Demo 的真实实验得出了同样的结论（全抖动那一行用的就是 `backoff_delay(n, base=0.1, cap=1.0)`）。

   重试循环本身是 async 的（`retry_call`，`ResilientLLM` 内部用的就是它）：

   ```python
   async def retry_call(fn, *, max_attempts=3, base_delay=0.5, max_delay=8.0, retry_if=is_retryable, sleep=asyncio.sleep, on_retry=None):
       for attempt in range(1, max_attempts + 1):
           try:
               return await fn()            # fn 每次返回一个新协程：协程只能 await 一次，重试要重新创建
           except Exception as e:           # CancelledError 是 BaseException：调用方取消时直接穿透，不会被重试
               if attempt == max_attempts or not retry_if(e):
                   raise
               delay = backoff_delay(attempt, base_delay, max_delay)
               server_hint = getattr(e, "retry_after", None)   # 服务端说了"N 秒后再试"就听它的
               if server_hint is not None:
                   delay = max(delay, min(server_hint, max_delay * 4))
               await sleep(delay)           # 等待期间让出事件循环：同一个进程里的其他会话照常推进
   ```

   用法：`await retry_call(lambda: llm.chat(messages))`。`rng`、`sleep`、`clock` 都可注入，所以本课的练习测试都是确定性的，不会真的睡眠。

3. **练习 (b)** 补上 agentkit 没有的两道闸门：**全局重试预算**（令牌桶：每个新请求存 0.1 个令牌、每次重试花 1 个，思路同 Google SRE 的"重试不超过请求的 10%"和 gRPC 的 `retryThrottling`）和**截止时间**（剩余时间不够再等一次退避，就不再重试）。测试用 `asyncio.gather` 真的并发发起 100 个请求、共享一个预算：下游彻底宕机时，"每个请求最多 3 次尝试"本来要打 300 次，有了预算只打 110 次。为什么是 110 而不是逐个发送时的 113？并发时 100 个首次请求几乎同时存入令牌，桶在失败回来之前就被存到了上限 `max_tokens=10` —— 上限的作用正是限制"一出故障能有多少重试同时冲出去"。

   预算不用加锁：100 个请求是同一个事件循环里的协程，而协程只在 `await` 处切换，`try_acquire` 里没有 `await`，"读令牌 → 判断 → 扣减"不会被打断。多个**进程**共享预算时，内存里的桶就不够了，要放进所有进程都看得到的地方（单机：`agentkit.distributed.SQLiteTokenBucket`；多机：Redis 或模型网关）。

> 生产级升级：`Retry-After` 已经接上了（`OpenAICompatLLM` 把响应头解析进 `LLMError.retry_after`，`retry_call` 取它和自己算的退避中较大的那个）；全局限流和重试预算放到模型网关或 Redis 里共享；给不同层约定"已重试过，别再重试"的错误码。

### 问题 2：主模型供应商宕机了 25 分钟

**场景**：周二 14:00，主模型供应商大面积返回 503，持续 25 分钟。Agent 的模型调用超时 60 秒、重试 3 次，每个用户请求要等 3 分钟才看到报错；Web 服务的 200 个工作线程在两分钟内全部被"等超时"的请求占满，连不需要调用模型的页面也打不开了。

**为什么难**：

- 重试的前提是"失败是暂时的"。持续故障时，每个请求都老老实实地"尝试 3 次、每次等 60 秒"，只会拖垮自己；供应商恢复的那一刻，积压的请求一拥而上，又把它打趴下；
- 切到备用模型听起来简单，但备用模型的能力、上下文长度、工具调用支持、提示词适配都可能不同：你可能只是把"报错"换成了"胡说八道"；
- 哪些错误算"供应商不健康"？一个用户的超长对话触发 400 `context_length_exceeded`，如果也计入熔断，几个这样的请求就能让所有用户都被切到备用模型。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 只重试，最后报错 | 重试用尽后返回错误 | 最简单 | 用户久等；线程被耗尽；恢复时惊群 | 内部工具，能接受短时不可用 |
| B. 熔断 + 快速失败 | 连续失败达到阈值就"跳闸"，之后请求毫秒级失败，冷却后放试探请求 | 保护自己、给下游喘息 | 故障期间功能完全不可用 | 没有备选方案的依赖 |
| C. 熔断 + 降级到备用模型 | 跳闸后切到另一家供应商或另一个模型 | 用户基本无感 | 质量差异、提示词兼容、成本；备用模型也要评估 | 面向客户的核心功能 |
| D. 再兜底：缓存 / 规则 / 人工 | 所有模型都不可用时，用 FAQ、历史答案、转人工 | 最后一道体验保障 | 覆盖面有限，需要业务设计 | 客服等有人工渠道的场景 |

C 和 D 串起来就是一条降级链：

```mermaid
flowchart LR
    A["主模型<br/>能力最强"] -->|"失败或熔断"| B["备用模型<br/>另一家供应商或更小的模型"]
    B -->|"也失败"| C["缓存或规则<br/>历史答案 / FAQ / 固定话术"]
    C -->|"也没有"| D["人工兜底<br/>转人工 / 生成工单 / 稍后回复"]
```

**怎么选**：面向客户的核心功能用 **C + D**，内部工具用 **B** 即可。无论选哪个都必须：备用模型跑一遍评估集（第 11 课）；降级事件打点并告警（"悄悄降级"最危险，质量下降一周都没人发现）；熔断器只统计能反映"下游健康"的错误（5xx、超时、429，以及 401、模型不存在这类"整个依赖都不可用"的错误），不统计单个请求自身的问题。

**本课实现**：方案 B + C。熔断器三态：

```mermaid
stateDiagram-v2
    [*] --> closed
    closed --> closed : 调用成功，失败计数清零
    closed --> open : 连续失败次数达到 failure_threshold
    open --> open : 请求直接快速失败，不调用下游
    open --> half_open : 等待 reset_timeout 秒
    half_open --> closed : 试探请求成功
    half_open --> open : 试探请求失败，重新计时
```

`CircuitBreaker` 的状态不存储，而是**根据时间算出来**，不需要后台定时器把 open 改成 half_open，少了一类并发 bug（[agentkit/reliability.py](../../agentkit/reliability.py)）：

```python
@property
def state(self) -> str:
    if self.opened_at is None:
        return "closed"
    if self.clock() - self.opened_at >= self.reset_timeout:
        return "half_open"
    return "open"
```

`ResilientLLM` 把"重试 → 熔断 → 降级"叠成一个装饰器，**对外依然是一个普通 LLM**，Agent 完全无感：

```python
from agentkit import Agent, ResilientLLM, default_llm

llm = ResilientLLM(
    default_llm("gpt-5.5"),                 # 主模型
    [default_llm("gpt-5.6-luna")],          # 备用模型（可以有多个，按顺序尝试）
    max_attempts=3, base_delay=0.5,         # 每个模型最多尝试 3 次，全抖动指数退避
    failure_threshold=5, reset_timeout=30,  # 连续 5 次失败就熔断，30 秒后半开试探
)
agent = Agent(llm, tools)
```

设计要点：每个模型有**自己的**熔断器；熔断器包在重试**外面**（一次"用尽了所有重试的调用"才算一次失败）；所有模型都失败时抛 `LLMError`，Agent 把它变成 `status="failed"` 和一句"服务暂时不可用"，而不是 500 —— 方案 D 应该在调用 Agent 的业务层根据这个状态来做。

熔断器还有两个细节，生产级实现（如 Java 的 Resilience4j）都有，agentkit 也都实现了：

- **哪些异常计入熔断**：`CircuitBreaker(record_if=...)`（`ResilientLLM` 同名参数）。默认全部计入 —— Demo 场景 2 正是用"模型不存在"的 400 来触发熔断；生产中可以只统计反映"下游不健康"的错误，别让一个用户自己的超长上下文把所有人切到备用模型；
- **半开时只放行 1 个试探请求**，其余并发请求继续快速失败。否则下游刚恢复一点，500 个并发请求一拥而上，又把它压垮：

  ```python
  async def call(self, fn):
      state = self.state
      if state == "open":
          raise CircuitOpenError(self.name)
      probe = state == "half_open"
      if probe:
          if self._probing:              # 已经有一个试探请求在路上
              raise CircuitOpenError(self.name)
          self._probing = True           # 检查和置位之间没有 await：同一个事件循环里的其他协程插不进来
      try:
          result = await fn()            # 唯一的切换点：试探请求在这里等，别的协程被上面的检查挡住
      except Exception as e:
          ...                            # 计入失败；试探失败则重新打开、重新计时
      finally:
          if probe:
              self._probing = False      # 成功、失败、被取消都要复位
      ...
  ```

  为什么一个布尔标志就够、不需要锁？协程只会在 `await` 处切换。练习 (c)（加分题）让你自己实现一遍，测试用 `asyncio.gather` 在半开时同时发 50 个请求，证明只有 1 个打到下游。

**多个 worker 进程共享一个熔断器。** 上面的熔断器在进程内存里。服务通常跑着很多个 worker 进程：进程 A 已经连续失败、熔断了，进程 B、C 还各自要再失败 `failure_threshold` 次才会熔断 —— 下游已经挂了，你还要再往它身上打一批请求；每个新扩容出来的进程也都从 closed 开始重新试错。`agentkit.distributed.SQLiteCircuitBreaker` 是同一个状态机，只是失败计数、打开时间、"谁在试探"都存在一个所有进程共用的 SQLite 文件里：

```python
from agentkit.distributed import SQLiteCircuitBreaker

llm = ResilientLLM(primary, [backup], max_attempts=2,
                   breaker_factory=lambda model: SQLiteCircuitBreaker("runs/breakers.db", model,
                                                                      failure_threshold=3, reset_timeout=30))
```

Demo 场景 2 的 B 部分用真实进程验证了它：坏掉的主模型是另一个进程（`services.py`，每次返回 503，自己给调用计数），每个 worker 是 `demo.py` 再起的一个进程（`OpenAICompatLLM` → 主模型，每个请求最多尝试 2 次）。主模型服务统计到的调用次数：

| 进程 | 熔断器 | 打了坏掉的主模型几次 |
|---|---|---|
| A（第一个发现故障的） | 共享 | 6（3 个请求 × 2 次尝试后熔断，第 4 个请求快速失败） |
| B（A 退出后才启动） | 共享 | **0**：第一次调用就快速失败，直接走备用模型 |
| B′（对照） | 各自内存里的 `CircuitBreaker` | 6：自己再撞 3 个请求才熔断 |
| C（主模型修好、过了 `reset_timeout` 之后） | 共享 | 4：第 1 个请求就是半开试探，成功后熔断器关闭 |

半开时的"只放一个试探"跨进程也要成立：内存里的布尔标志别的进程看不见，所以 `SQLiteCircuitBreaker` 用一个带过期时间的**试探租约**（`probe_until`）—— 在一个写事务里检查并占住它的进程去试探，其余进程继续快速失败；试探者如果中途崩溃，租约过期后别的进程可以接着试，不会永远卡在半开。`tests/test_distributed.py` 里有两个对应的测试：另一个真实子进程把熔断器打开后，本进程第一次调用就快速失败、主模型调用次数为 0；两个各自持有连接的实例（和两个进程一样只通过文件共享状态）半开时只放行一个试探。局限：SQLite 只能在一台机器上共享；多台机器时把熔断放到模型网关层（第 29 课）或 Redis。

### 问题 3：Agent 死循环，一晚上烧掉一大笔钱

**场景**：一个报表 Agent 的工具永远返回"生成中（进度 99%），请稍后再次查询"，系统提示又要求"完成前不许放弃"。它对同一个工具、用同样的参数调用了 400 次。每一步都要把越来越长的历史重新发给模型：Demo 场景 4 用真实模型测得每步输入 token 增长约 57 个（409 → 466 → 523 → 580）。照此推算，400 步累计约 470 万输入 token，按 agentkit 默认的示例单价（每百万输入 token $1.25）约 6 美元 —— 如果每天有 1000 个这样的运行呢？

**为什么难**：

- 普通程序花多少钱是确定的；Agent 花多少钱**由模型在运行时决定**。OWASP 把这类风险列为 LLM 应用十大风险之一：LLM10:2025 Unbounded Consumption；
- 成本不是线性的：每一步都比上一步贵，循环 N 步的总成本近似按 $N^2$ 增长；
- 只限步数不够：一步就可能用掉 10 万 token；只限金额又太晚：钱花完了才知道；
- 不能简单地"检测到重复就杀掉"：轮询类任务本来就会合理地重复几次。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 步数上限 | `max_steps=15` | 零成本，最后一道保险 | 不管每步多贵；可能误杀正常的长任务 | 所有 Agent 都必须有 |
| B. 多维预算 | token、金额、工具次数、时长分别设上限，超限优雅停止 | 直接控制钱和时间 | 只知道"花完了"，不知道"为什么"；阈值不好定 | 所有 Agent 都必须有 |
| C. 循环检测 | 同工具 + 相同参数在滑动窗口内重复 → 先提醒模型换思路，再中止 | 精准、便宜，还给模型一次改正的机会 | 只能识别"完全相同"的重复，参数稍有变化就漏掉 | 工具调用多的 Agent |
| D. 进度看门狗 | 定期让规则或另一个模型判断"最近 N 步是否有新进展" | 能识别更隐蔽的原地打转 | 额外成本，可能误判 | 小时级长任务 |

**怎么选**：**A + B 是底线**，每个 Agent 都要有；C 成本极低，工具多的 Agent 都应该加；D 留给长时间运行的任务。单次运行的预算之外，还要有用户级、租户级的日 / 月配额，放在网关层做（[第 14 课](../14_cost_latency/README.md)）。

**本课实现**：A 是 `Agent(max_steps=...)`，B 是 `BudgetHook`（[agentkit/budget.py](../../agentkit/budget.py)）：

```python
agent = Agent(llm, tools, max_steps=15,
              hooks=[BudgetHook(max_tokens=50_000, max_cost_usd=0.50, max_tool_calls=20, max_seconds=120)])
```

超限时抛 `StopRun`，主循环把它收敛成 `status="stopped"`、正常保存检查点，用户看到"已超出预算，任务中止"，而不是 500。几个值得注意的细节：

- 金额和 token 在模型返回之后才知道，所以检查放在 `after_llm`，预算可能被"超出一次调用的量"，阈值要留余量；
- 工具次数的检查在 `before_tool`，这时模型已经发出了工具调用。中止时，Agent 会给最后一条 assistant 消息里**还没执行的工具调用**补一条结果"未执行：运行已中止（budget_exceeded）"。这是 OpenAI 消息协议的要求：**每个 `tool_call` 都必须有一条对应的 `tool` 消息**，否则用户接着这段历史继续对话时，下一次模型调用会直接返回 400；
- `max_seconds` 只统计**实际执行**的时间（`state.active_seconds`），暂停等待人工审批的时间不算（见问题 6）。

C 是**练习 (a) `LoopGuard`**。本题最重要的知识点是：**计数必须存在 `state.metadata` 上，而不是 hook 实例上。** 一个 Hook 实例被同一个 Agent 的所有运行共享 —— 在 Web 服务里就是所有用户、所有租户的并发请求。计数放在 `self` 上，A 用户的调用历史会让 B 用户被误判为死循环；而 `state` 是每次运行独有的，还会随检查点落盘，崩溃或审批暂停后恢复，计数也不会丢。

### 问题 4：发布一次，正在跑的任务全部从头再来

**场景**：一个合同审查 Agent 平均运行 8 分钟、调用模型 25 次。服务每天发布 3 次，每次发布时同一时刻约有 20 个任务正在运行，它们被强制中断，于是每天约 60 个任务要从头再跑：重复花钱、用户重复等待，更糟的是，已经执行过的工具调用会再执行一次。

**为什么难**：

- 进程会崩溃、机器会宕机、发布会重启，这些都无法避免；
- 从头重跑不仅浪费，而且**不确定**：模型重新做决定，可能得出和上次不同的结论；
- 保存进度本身也可能失败：写检查点写到一半崩溃，得到一个损坏的文件，连旧进度都没了。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 不做检查点，失败重跑 | 任务失败就整个重新提交 | 最简单 | 浪费钱和时间；有副作用的工具会重复执行 | 几秒钟的只读任务 |
| B. 检查点 + 恢复 | 每一步把完整状态原子地写入存储；恢复时从断点继续，只补执行"没有结果的工具调用" | 自己可控，不需要新的基础设施 | 恢复的触发、并发恢复（两个实例抢同一个任务）、检查点清理都要自己做 | 分钟级、有副作用的任务 |
| C. 持久化工作流引擎 | 用 Temporal、LangGraph 等框架记录每一步，自动重放 | 定时器、重试、信号、可视化一应俱全 | 学习曲线；工作流代码有确定性约束；多一个要运维的组件 | 小时到天级、多步骤、多人参与的流程 |

**怎么选**：看任务时长和副作用。几秒钟的只读任务用 A；分钟级、会改外部系统的用 B；跨小时甚至跨天、需要定时唤醒或多方协作的用 C。

**本课实现**：方案 B。主循环在每个状态变化点存盘：初始状态、每次模型回复之后、**每个工具结果之后**（[agentkit/agent.py](../../agentkit/agent.py)）。`Agent.resume(run_id)` 加载检查点后先调用 `_run_pending_tools`：找到最后一条 assistant 消息里还没有结果的工具调用，补执行完再继续主循环，**不会让模型重新做一个已经做过的决定**。

`FileCheckpointer` 用"先写临时文件、再原子替换"保证检查点不会写坏（[agentkit/state.py](../../agentkit/state.py)）：

```python
def save(self, state: RunState) -> None:
    path = self._path(state.run_id)
    # 临时文件名必须唯一：两个进程同时保存同一个 run 时，固定的 .tmp 会互相踩
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(state.to_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)   # 原子替换：崩溃时文件要么是旧的完整版本，要么是新的完整版本
```

原子替换只保证"文件不会写坏"，不保证"不会被覆盖"：两个进程都以为自己在处理同一个 run 时，后写的那个悄悄覆盖先写的。所以 `FileCheckpointer` 适合"同一时刻只有一个进程处理一个 run"（本课 Demo 场景 3：进程 1 死了，进程 2 才接手）；多个 worker 可能抢同一个 run 时，要用带版本号和 fencing token 的检查点（见下面的"生产级升级"）。

这就是**持久化执行（durable execution）** 的核心思想。和业界方案的对照：

| | agentkit | LangGraph checkpointer | Temporal |
|---|---|---|---|
| 存什么 | 完整的 RunState | 每个 super-step 结束时的图状态 | 事件历史：每个 Activity 的调用和结果 |
| 恢复方式 | 加载快照，补执行没有结果的工具调用 | 从最后一个检查点继续；同一步里已成功节点的写入不重跑 | 重放工作流代码，已完成的 Activity 直接用历史结果 |
| 对代码的要求 | 工具要幂等 | 节点确定性、幂等；副作用放进 task | 工作流代码确定性；Activity 幂等 |

注意最后一行：**所有方案都要求有副作用的操作是幂等的。** 为什么？见下一张卡片。

**谁来触发恢复？** Demo 里是我们手动启动进程 2。生产中要自动化："有个进程死了，它手上的运行由别人接着跑"，而且同一时刻只能有一个进程在恢复它。这套机制在 `agentkit.distributed` 里是真实实现的（[第 13 课](../13_distributed_concurrency/README.md)详讲），核心是**租约**：

- 运行以任务的形式放进持久化队列，worker 领取任务不是"拿走"而是"借走一段时间"（租约），`run_worker` 给每个任务起一个心跳协程定期续租；
- 持有者崩溃、卡死或被暂停，心跳就停了。任何 worker 下一次领取任务时，`SQLiteJobQueue.claim` 在**同一个写事务**里先回收过期的租约：

  ```python
  rows = conn.execute("SELECT id, attempts, max_attempts, worker_id FROM agent_jobs "
                      "WHERE status = 'leased' AND lease_until < ?", (now,)).fetchall()
  # 还有尝试次数 → 退避后放回队列；次数用尽 → 死信（一个每次都让 worker 崩溃的"毒消息"最多害死 max_attempts 个 worker）
  ```

- 接手的 worker 用 `AgentJobHandler` 执行任务：发现这个 run 已经有检查点，就调用 `agent.resume(run_id)` 从断点继续，而不是从头再来；
- 被暂停后又醒过来的旧持有者（"僵尸"）还以为自己持有任务。每次领取都会发一个全局递增的 fencing token，检查点和任务状态的写入都带着它，对不上就拒绝 —— 所以检查点要换成带版本号 CAS 和 fence 的 `SQLiteCheckpointer`。

  ```python
  # 每个 worker 进程里（python -m agentkit.distributed.worker ...，或用 WorkerPool 一次拉起 N 个）：
  db = SQLiteDB("runs/jobs.db")                   # 同一个进程里的几个对象共用一个连接
  queue, ckpt, idem = SQLiteJobQueue(db), SQLiteCheckpointer(db), SQLiteIdempotencyStore(db)
  for x in (queue, ckpt, idem):
      await x.setup()                             # 建表
  agent = Agent(llm, tools, checkpointer=ckpt, idempotency_store=idem)
  await run_worker(queue, AgentJobHandler(agent, ckpt), worker_id="w1", stop_event=stop, lease_seconds=30)
  ```

`tests/test_distributed.py` 用真实的进程和信号验证了这几件事：`kill -9` 持有任务的 worker 后，别的进程在租约过期后接手、从检查点继续、工具不会再执行一次；`SIGSTOP` 冻结的僵尸醒来后，它的续租、检查点写入和提交全部被拒绝。发布时先"排空"再停机见[第 16 课：发布与运维](../16_release_ops/README.md)。

> 检查点解决的是"同一个任务在几分钟到几小时内被打断"。如果任务长到一个上下文窗口都装不下（几小时到几天），就需要另一种持久化：用功能清单、进度文件和 git 让一个全新的会话读交接文档后接班，见[第 24 课的长时运行 harness](../24_coding_agents/README.md#13-长时运行agent-每次醒来都失忆)。

> 🏭 **生产版**：单机多进程时，本课 Demo 用的 `SQLiteIdempotencyStore`、`SQLiteCircuitBreaker`，以及第 13 课的 `SQLiteCheckpointer` / `SQLiteJobQueue` 已经是真实的跨进程实现；多台机器时，检查点换成带版本号 CAS 和 fencing 的 Postgres、幂等存储换成 Redis，接口相同，见[第 26 课](../26_state_and_queues/README.md)；跨天等待审批、需要定时唤醒的流程交给 Temporal 这类持久化执行引擎，见[第 27 课](../27_durable_workflows/README.md)。关于超时：`async def` 工具超时会被真正取消；普通同步函数在线程池里执行，超时后调用方不再等它，但线程杀不掉；`@tool(isolation="process")` 在子进程里执行，超时直接 kill（[第 30 课](../30_async_runtime/README.md)详讲取消语义）。

### 问题 5：恢复之后，客户收到了两笔退款

**场景**：退款 Agent 调用支付接口成功，就在写检查点之前，进程被 OOM kill。新进程从检查点恢复，看到"模型决定调用 refund，但没有结果"，于是又调用了一次。如果每天 5 万笔退款中有 0.01% 恰好落在这个窗口里，就是每天 5 笔重复退款。

**为什么难**：检查点再频繁，"执行工具"和"记录结果"之间总有一个缝隙：

```mermaid
sequenceDiagram
    participant A as Agent 进程
    participant T as 工单系统
    participant C as 检查点
    A->>C: 保存 模型决定调用 create_ticket
    A->>T: create_ticket 3 楼打印机卡纸
    T-->>A: 已创建 T-1001
    Note over A: 💥 进程在这里崩溃
    Note over C: 检查点里没有工具结果
    A->>C: 新进程 resume 读取检查点
    Note over A: 看起来工具还没执行
    A->>T: 再次 create_ticket
    T-->>A: 已创建 T-1002 重复了
```

分布式系统里做不到真正的"恰好执行一次"，能做到的是 **"至少执行一次 + 幂等" = 效果上恰好一次**。检查点保证"至少一次"（不丢），幂等保证"多次等于一次"（不重）。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 调用方幂等存储 | 用稳定的幂等键（`run_id:call_id`），执行前查、成功后记 | 通用，对下游没有要求 | 副作用完成后、记录写入前崩溃，仍会重复；存储必须持久化 | 框架层默认开启 |
| B. 幂等键传给下游 | 下游在**同一个事务**里"执行操作 + 记录 key"，同一个 key 第二次到来直接返回上次结果 | 真正做到效果上恰好一次 | 需要下游支持 | 支付、发消息、对外 API |
| C. 业务唯一约束 | 用业务键建唯一索引，例如"一个订单只能有一笔退款" | 最强的保证，和技术实现无关 | 不是所有操作都有天然的唯一键；可能误拒合法的第二次操作 | 有明确业务键的写操作 |
| D. 先查后做 | 执行前查询"这件事做过没有" | 不用改下游 | 查和做之间有竞态窗口，并发时不可靠 | 只能作为辅助 |

**怎么选**：A 作为框架默认；涉及钱、对外通知、不可逆操作的，必须再加 B 或 C；D 不能单独依赖。

**本课实现**：方案 A。关键在于幂等键怎么来（[agentkit/tools.py](../../agentkit/tools.py)）：

```python
@property
def idempotency_key(self) -> str:
    """同一个 run 里的同一次工具调用 → 同一个 key。重放时据此去重。"""
    return f"{self.run_id}:{self.call_id}"
```

`tool_call_id` 由模型生成、存在检查点里，恢复后不会变，所以重放时能命中；模型在另一步里再决定建一张工单，是一个新的 `call_id`，不会被误去重。Temporal 官方文档给出的建议如出一辙：用 Workflow Run ID + Activity ID 组合成幂等键。**千万不要在工具内部用 `uuid4()` 现生成幂等键**：每次执行都生成新的，重放时 key 变了，等于没有。

工具执行器只对 `write` / `dangerous` 工具启用幂等（读操作天然幂等，缓存它反而会让恢复后读到过期数据）：执行前 `get(key)`，命中就直接返回上次的结果；成功后 `put(key, result)`。

```python
from agentkit.distributed import SQLiteIdempotencyStore

store = SQLiteIdempotencyStore("runs/idempotency.db")
await store.setup()   # 建表
agent = Agent(llm, tools, checkpointer=FileCheckpointer("runs/checkpoints"), idempotency_store=store)
```

**幂等存储必须在"接手的那个进程"也看得到的地方。** agentkit 自带的 `IdempotencyStore` 是内存里的一个 dict，进程一崩它就跟着没了，恰恰在最需要它的时候失效。自己写一个"读 JSON 文件 → 改 → 写回"的版本也不行：两个进程同时读到旧内容、各自加一条再写回，后写的覆盖先写的（丢更新）；固定的 `.tmp` 临时文件名还会让两个进程互相踩。`SQLiteIdempotencyStore` 把记录放在 SQLite 表里，`put` 是一条事务里的 `INSERT OR IGNORE`（两个进程几乎同时成功时，先写入的结果为准）。

Demo 场景 3 用真实的进程复现这个窗口：外部的工单系统是另一个进程（`services.py`）；进程 1 里的 Agent 调用 `create_ticket`（真的发 HTTP 请求建单），然后**真的死掉**（`os._exit(137)`，不执行任何清理）；进程 2 是一个全新的进程，用同一个 `run_id` 从检查点恢复。工单数来自工单系统自己的记录：

| 用例 | 进程 1 死在哪里 | 保护 | 工单数 |
|---|---|---|---|
| 1 | 工具执行完、检查点写入前 | 无 | 2 ❌ |
| 2 | 同上（幂等记录已写） | `SQLiteIdempotencyStore` | 1 ✅ 进程 2 命中幂等记录，工具没有再执行 |
| 3 | 工具内部：下游已建单、幂等记录还没写 | `SQLiteIdempotencyStore` | 2 ❌ 调用方的幂等存储无能为力 |
| 4 | 同上 | 再把 `ctx.idempotency_key` 作为 `Idempotency-Key` 头传给下游 | 1 ✅ 工单系统认出同一个 key，返回原来的工单 |

用例 3 就是方案 A 表格里说的缺点："副作用完成后、记录写入前崩溃，仍会重复"。用例 4 是方案 B 的写法 —— 下游在自己的存储里"执行操作 + 记录 key"，同一个 key 第二次到来直接返回上次的结果：

```python
@tool(risk="write")
async def create_ticket(title: str, ctx: ToolContext) -> str:
    """创建工单"""
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{TICKETS_URL}/tickets", json={"title": title},
                            headers={"Idempotency-Key": ctx.idempotency_key})   # 同一次调用重放时 key 不变
    return f"工单已创建：{r.json()['id']}"
```

这正是 Stripe 等支付 API 支持 `Idempotency-Key` 请求头的原因。

> 生产级升级：单机多进程用 `SQLiteIdempotencyStore`（本课 Demo）；多台机器用 Redis（`SET key value NX` 加过期时间）或数据库唯一索引（第 26 课）。注意它只记"成功之后"的结果：两个进程**同时**执行同一个调用（僵尸 worker 和接手者撞在一起）时两边都查不到记录、都会执行，所以真正有副作用的下游还是要自己认幂等键；并发场景下的去重、租约、fencing 见[第 13 课](../13_distributed_concurrency/README.md)。

### 问题 6：审批要等好几个小时

**场景**：报销 Agent 处理金额超过 5000 元的单据需要主管审批。主管平均 3 小时后才处理，高峰每小时新增 300 单待审批。如果每一单都在内存里挂着等（一个线程或一个协程），稳态下同时挂着约 900 个；协程很便宜，挂 900 个本身不是问题，真正的问题是：这 3 小时里服务只要发布一次，所有等待中的审批就全部丢失。

**为什么难**：等待时间不可预测（5 分钟到第二天）；等待期间服务会重启、会扩缩容；审批通过后，执行的必须**恰好是**当初提交审批的那个操作，参数不能被改。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 同步阻塞等待 | 线程里等回调或轮询审批结果 | 实现最简单 | 占用资源；发布或崩溃就丢；无法扩容 | 命令行工具、秒级确认 |
| B. 暂停落盘 + 异步恢复 | 状态保存为 paused，通知审批人，审批后任意实例加载检查点继续 | 等待期间零资源占用，跨进程、跨机器 | 通知、提醒、超时处理要自己做 | 大多数企业场景 |
| C. 工作流引擎的信号 / 中断 | Temporal 的 signal、LangGraph 的 interrupt 等 | 内置定时器（超时自动拒绝）、提醒、可视化 | 需要引入引擎 | 多级审批、复杂流程 |

**怎么选**：秒级、有人坐在屏幕前的确认用 A；其余默认用 B；审批链条复杂（多级、会签、超时升级）时用 C。无论哪种，**审批超时都应按拒绝处理**（fail closed）。

**本课实现**：方案 B。

```mermaid
sequenceDiagram
    participant U as 用户
    participant A as Agent 进程 1
    participant C as 检查点
    participant H as 审批人
    participant B as Agent 进程 2
    U->>A: 订单 A1 退款 99 元
    A->>A: PermissionPolicy 发现 risk=dangerous，抛 PauseRun
    A->>C: 保存 status=paused，pending=refund 调用
    A-->>U: 已提交审批，请稍候
    Note over A: 进程可以退出、重启、被替换
    H->>B: 几小时后，批准
    B->>C: 加载检查点
    B->>B: 执行 refund，继续主循环
    B-->>U: 已为您退款 99 元
```

代码上只需要两步：

```python
agent = Agent(llm, [refund], hooks=[PermissionPolicy()], checkpointer=FileCheckpointer("runs/approvals"))
res = await agent.run("订单 A1 退款 99 元")
res.status            # 'paused'
res.pending_approval  # ToolCall(id='call_1', name='refund', arguments='{"order_id": "A1", "amount": 99.0}')

# ……审批系统通知主管。几小时后，可能是另一个进程、另一台机器：
agent2 = Agent(llm, [refund], hooks=[PermissionPolicy()], checkpointer=FileCheckpointer("runs/approvals"))
res2 = await agent2.approve(res.run_id, approved=True,   # 执行退款并继续；approved=False 则把"未获批准"告诉模型
                            by="zhang.manager", comment="核对订单无误")   # 谁批的、为什么批，写入审批记录
```

审批按 `tool_call_id` 记录，调用参数随检查点保存，所以批准的就是当初那次调用，模型没有机会在批准后改参数。"审批什么、谁来批、如何避免审批疲劳"是第 09 课的内容。

`by` 和 `comment` 会写进 `state.approval_log`，`AuditLog` 的记录里也会带上 `approved_by`：审计要回答的不只是"批没批"，还有"谁批的"。

> **暂停的时间算不算预算？** 不算。设想一个运行因审批暂停了 2 小时，如果时长预算按"从开始运行到现在"计算，批准后恢复：被批准的退款先执行，紧接着下一次模型调用前时长预算就会触发 `budget_exceeded` —— **钱退了，用户却只收到"已超过时长预算"**。所以 agentkit 的 `BudgetHook` 用 `state.active_seconds` 只累计每一段 `run` / `resume` 实际执行的时间。设计预算时要想清楚每个维度统计的是什么：金额和 token 跨暂停累计，墙钟时长只算活跃时间，而"审批最多等多久"是另一个独立的超时（到期按拒绝处理）。

## 3. 动手：运行 Demo

```bash
python lessons/08_reliability/demo.py --offline            # 离线剧本，无需 API key（约 20 秒）
python lessons/08_reliability/demo.py                      # 真实模型（约 1 分钟，二十次左右模型调用）
python lessons/08_reliability/demo.py --offline --only 1   # 只跑场景 1；--keep 把检查点、SQLite 文件留在 runs/08_reliability/
```

Demo 一开始把 [services.py](services.py) 拉起为**一个独立的进程**，扮演三个下游：容量有限的模型网关（场景 1）、一个坏掉的主模型（场景 2B）、一个工单系统（场景 3）；演示结束时关掉它。场景 2B、3 还会再启动若干个 `demo.py` 子进程扮演 worker —— 进程之间不共享内存，只通过 HTTP 和 SQLite 文件打交道。惊群实验、跨进程熔断、崩溃恢复这三部分的数字都来自"另一个进程"自己的记录，两种模式下都一样。

真实模式下，场景 1 在真实模型外面套了一层故障注入（`FlakyLLM`：前两次调用抛 429）；场景 2A 用一个故意不存在的模型名模拟主模型不可用，备用模型读取 `.env` 里的 `LLM_FALLBACK_MODEL`。以下是一次真实模型运行的输出节选（MacBook 8 核 / 8GB，负载约 4）：

**场景 1：重试 + 惊群实验（对应问题 1）**

```text
▶ Agent → ResilientLLM → FlakyLLM（前两次抛 429）→ 模型
   📝 retry gpt-5.5 #1 after 0.29s: Error code: 429 - Rate limit reached, please retry later
   📝 retry gpt-5.5 #2 after 0.49s: Error code: 429 - Rate limit reached, please retry later
   ✅ 状态=completed，底层共调用 3 次，总耗时 4.4s
▶ 对照：如果错误是 400（参数错误），重试有用吗？
   底层调用次数：1（没有重试），状态=failed，给用户的回复：抱歉，服务暂时不可用，请稍后再试。
▶ 为什么一定要加抖动？真实实验：500 个客户端在同一时刻打同一个网关进程
   每 100ms 到达网关的请求数（一个字符 = 100ms，█ = 500 个，· = 0 个；数据来自网关日志）：
     固定间隔 0.5s      █····█····▇····▆····▅····▄····▄····▃····▂····▁
                        前 1 秒：500 0 0 0 0 450 0 0 0 0
     指数退避，无抖动   ██·▇···▆·······▅·········▄·········▄·········▃·········▂·········▁
                        前 1 秒：500 450 0 400 0 0 0 350 0 0
     指数退避 + 全抖动  █▆▄▂▂▂▁▁▁▁▁▁▁▁▁▁
                        前 1 秒：948 343 197 112 115 75 58 23 36 34

   策略                总请求  被 429  重试最密的 10ms  全部成功  一半成功  网关利用率
   固定间隔 0.5s       2750    2250    324              4.67s     2.64s     21%
   指数退避，无抖动    2750    2250    340              6.66s     2.64s     15%
   指数退避 + 全抖动   2001    1501    76               1.64s     0.61s     61%
```

👀 观察：Agent 对两次 429 毫无感知；400 一次都没有重试。惊群实验里，固定间隔那一行是一排整齐的"墙"：每一堵都是几百个重试挤在同一个 10ms 里，网关只接得住 50 个；全抖动那一行是一条连续下降的曲线。全抖动第一个 100ms 的请求反而最多（948 个）：`base=0.1s` 时第一次重试的等待在 [0, 0.1s] 里随机，大部分也落在这 100ms 内 —— 但它们是摊开到达的，最密的 10ms 只有 76 个。

实验的两个设计细节：① 客户端没有用 httpx，而是每个客户端一条 keep-alive 连接、手工拼请求字节。实测 httpx 在一个进程里每秒只能发出一两百个请求，500 个"同时"发出的请求被它自己的 CPU 开销摊开到好几秒，惊群还没到网关就被客户端抹平了；② 网关用标准库 asyncio 手写，而不是 FastAPI，理由相同：服务端每个请求多花 1ms，500 个同时到达的请求就会被它自己摊开到半秒。**测量工具本身不能成为被测现象的一部分。**

**场景 2：熔断 + 降级（对应问题 2）**

```text
▶ A. 进程内的熔断器（CircuitBreaker）：真实时钟 —— 真的等 reset_timeout 秒，而不是拨快一个假时钟
▶ 请求 1：用一句话解释：什么是熔断器？
   熔断器：closed → closed    主模型被调用 1 次    实际回答的模型：gpt-5.6-luna    耗时 3.8s
▶ 请求 2：用一句话解释：什么是降级？
   熔断器：closed → open    主模型被调用 1 次    实际回答的模型：gpt-5.6-luna    耗时 3.5s
▶ 请求 3：用一句话解释：什么是重试预算？
   熔断器：open → open    主模型被调用 0 次    实际回答的模型：gpt-5.6-luna    耗时 3.1s
   📝 fallback from gpt-5.5-does-not-exist: 熔断器 [gpt-5.5-does-not-exist] 处于打开状态，快速失败
▶ ⏳ 真的等熔断器进入半开（reset_timeout=8.0s，从它打开的那一刻算起）……
   又等了 1.4s，主模型已经修好。熔断器现在是 half_open，放一个试探请求过去……
▶ 请求 4：用一句话解释：什么是幂等？
   熔断器：half_open → closed    主模型被调用 1 次    实际回答的模型：gpt-5.5    耗时 1.6s

▶ B. 跨进程共享的熔断器（SQLiteCircuitBreaker）：每个 worker 都是一个独立的操作系统进程
▶ 进程 A：第一个发现故障的 worker
      [进程 A pid=43836] 请求 3：熔断器 closed → open    回答来自 backup-model
      [进程 A pid=43836] 请求 4：熔断器 open → open    回答来自 backup-model
▶ 进程 B：另一个 worker（A 已经退出，B 只看得到那个 SQLite 文件）
      [进程 B pid=43839] 请求 1：熔断器 open → open    回答来自 backup-model
      ...
▶ 主模型服务自己统计的调用次数（每个请求最多尝试 2 次）
   进程 A（共享熔断器）         打了主模型 6 次：连续 3 个请求失败（每个 2 次尝试）后熔断器打开，之后的请求不再打它
   进程 B（共享熔断器）         打了主模型 0 次 ✅ 第一次调用就快速失败，直接走备用模型
   进程 B′（各自的内存熔断器）   打了主模型 6 次：它得自己再失败 3 个请求才会熔断
   进程 C（恢复后）             打了主模型 4 次：每个请求 1 次，第 1 个是试探，成功后熔断器关闭（最终状态：closed）
```

👀 观察：请求 3 根本没有调用主模型；请求 4 的试探成功后流量回到主模型。A 部分用的是真实时钟：离线模式 `reset_timeout=1.5` 秒，真实模型 8 秒 —— 必须比备用模型回答一次的耗时长。我们第一次用 1.5 秒跑真实模型时，备用模型一次回答要 2-3 秒，熔断器在请求 2 还没结束时就已经进入半开，请求 3 变成了又一次（失败的）试探，"打开期间快速失败"根本看不到。**只用假时钟的测试发现不了这个问题。**

**场景 3：崩溃恢复（对应问题 4、5）**

```text
▶ 用例 1：没有幂等保护
      [进程 1 pid=43846] 工具已执行：工单已创建：T-1001（3 楼打印机卡纸，优先级 high）
      [进程 1] 💥 就在此刻进程死亡（os._exit(137)）—— 工具结果还没来得及写进检查点
   进程 1 退出码：137（被杀死）
   工单系统（另一个进程）里已有 1 张工单；检查点里最后一条消息是 role=assistant，带 1 个工具调用、没有工具结果 → 看起来'还没执行'
      [进程 2 pid=43860] 恢复后状态：completed    🤖 已为你提交 IT 工单：T-1002。
   进程 2 退出码：0    ❌ 工单系统里现在一共有 2 张工单：T-1001, T-1002
▶ 用例 2：SQLiteIdempotencyStore（所有进程共享的幂等记录）
      ...
      [进程 2] ♻️  幂等存储命中 ticket-demo:call_1eC2O6CQMKzwWpZBgOAlgCZv → 直接返回上次的结果，工具没有再执行
   进程 2 退出码：0    ✅ 工单系统里现在一共有 1 张工单：T-1001
▶ 用例 3：同上，但崩在更窄的窗口：下游已经建单、幂等记录还没写
      ...
   进程 2 退出码：0    ❌ 工单系统里现在一共有 2 张工单：T-1001, T-1002
▶ 用例 4：再把幂等键传给下游（Idempotency-Key 请求头）
      ...
      [进程 2] 工单系统认出了同一个 Idempotency-Key（ticket-demo:call_PZrV9sxB7ErMCVyiQwnredPU）→ 返回原来的 T-1001，没有新建
   进程 2 退出码：0    ✅ 工单系统里现在一共有 1 张工单：T-1001
```

👀 观察：加上 `--keep` 运行，打开 `runs/08_reliability/crash_1/checkpoint_at_crash.json`（进程 1 死亡那一刻的检查点副本；`checkpoints/` 里的那份已经被进程 2 恢复完成后的状态覆盖了），看最后一条消息；再用 `sqlite3 runs/08_reliability/crash_2/idempotency.db "select key from idempotency"` 看幂等键长什么样（`run_id:call_id`）。

**场景 4：预算（对应问题 3）**

```text
   状态=stopped  stop_reason=budget_exceeded  模型调用 4 次  工具执行 3 次
▶ 链路追踪（第 10 课详讲）—— 一眼看出它在原地打转：
   agent.run  6762ms  tokens=1978→88  status=stopped steps=4 cost=$0.00335
   ├─ llm.chat  1724ms  tokens=409→22  → tool_calls: check_report_status
   ├─ tool.check_report_status  0ms  ok
   ...
   每一步的输入 token：409 → 466 → 523 → 580
```

👀 观察：每一步的输入 token 都在增长。把 `BudgetHook` 去掉，它会一直跑到 `max_steps=30`。

## 4. 练习

打开 [exercise.py](exercise.py)，完成三道题：

**(a) `LoopGuard`：检测 Agent 死循环（问题 3）**

- 任务：实现 `normalize_arguments`（参数 JSON 归一化）和 `LoopGuard.before_tool`：同一工具 + 相同参数在最近 `window` 次调用中出现 `>= max_repeats` 次时，第一次返回拒绝理由提醒模型换思路，之后再超限抛 `StopRun("loop_detected")`。
- 提示：计数存在 `state.metadata` 上；被拒绝的那次调用也要计入窗口；`'{"a":1,"b":2}'` 和 `'{"b": 2, "a": 1}'` 是同一个调用。两个函数都是普通函数（钩子方法可以是同步的，纯计算不需要 `async`）。

**(b) `retry_with_budget`：带全局预算和截止时间的重试（问题 1）**

- 任务：实现 `RetryBudget.on_request` / `try_acquire`（普通函数）和 `async def retry_with_budget`（单请求上限 + 全局预算 + 截止时间三道闸门）。
- 提示：docstring 里列出了判断顺序。每次尝试都 `await fn()`（`fn` 每次返回一个新协程），退避用 `await sleep(delay)`；注定因截止时间而放弃的重试**不应消耗令牌**；放弃时抛出**原始异常**，调用方原有的错误处理（比如降级）不受影响；只捕获 `Exception`，让 `asyncio.CancelledError` 穿透（测试会取消一个进行中的请求，检查它没有被重试）。

**(c) 加分题 `SingleProbeCircuitBreaker`：半开时只放行 1 个试探请求（问题 2）**

- 任务：实现 `async def call(fn)`。测试在半开时用 `asyncio.gather` 同时发 50 个请求，试探请求挂在 `await` 上，要求只有 1 个打到下游、49 个快速失败；还会取消进行中的试探，检查下一个请求能接着试探。
- 提示：不需要锁 —— "检查 `_probing`"和"置位"之间不能有 `await`；无论试探成功、失败还是被取消，都在 `finally` 里复位 `_probing`。没做时它的测试会自动跳过。

验证：

```bash
make lesson N=08                                                           # 跑你的实现
AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/08_reliability -v   # 对照参考答案
```

测试全部离线，注入了假时钟、假的 async sleep 和固定随机种子；并发用 `asyncio.gather` 真实发起，断言的是确定性的量（调用次数、谁被拒绝），不靠墙钟。结果确定，不到 1 秒跑完。

## 5. 深入（给有余力的你）

- **对冲请求（hedged request）**：请求在 p95 延迟内还没返回，就向另一个副本再发一份，谁先回来用谁。Jeff Dean 和 Luiz André Barroso 在 *The Tail at Scale*（2013）中讨论过这一技术，gRPC 也支持。对 LLM 来说代价很高（两份 token 费），而且**只能用于没有副作用的请求**。
- **客户端自适应限流**：Google SRE 书 Handling Overload 一章描述了一种做法：客户端统计最近两分钟的请求数和被后端接受的请求数，请求数达到接受数的 K 倍（通常 K=2）后，开始在本地按概率拒绝新请求。在 LLM 场景下，可以用它在供应商限流时把一部分流量提前导向备用模型。
- **LLM 特有的"软失败"**：HTTP 200 不代表成功。`finish_reason == "length"`（输出被截断）、JSON 不符合 schema、空回复、工具参数校验失败，都要"带着错误信息重发"，而不是原样重试（第 03 课的错误即观察、第 06 课的 `complete_json`）。
- **流式输出中途断开**：用户已经看到半段回答时，不能简单地重试整个请求。常见做法是分开设置"首 token 超时"（这时重试是安全的）和"总时长超时"，中断后在 UI 上标记并提供"重新生成"。
- **故障注入**：没有演练过的恢复流程等于没有恢复流程。Demo 里的 `FlakyLLM`、`CrashAfterTool`、会返回 429 / 503 的 `services.py` 就是最小的故障注入；`agentkit.distributed.WorkerPool` 还能对真实的 worker 进程发 `kill -9` / `SIGSTOP` / `SIGTERM`（第 13 课）。生产中要在预发环境定期注入 429、超时、进程崩溃，并盯住重试率、熔断次数、降级比例、`budget_exceeded` 比例这些指标（第 10 课）。
- **持久化执行框架**：当流程复杂到需要定时唤醒、多 Agent 协作、跨天等待时，可以考虑 Temporal、LangGraph，或 DBOS、Restate、Inngest 等框架。它们的核心思想和本课一样：**记录每一步、重放而不是重做、副作用必须幂等**。

## 6. 常见坑与反模式

| 反模式 | 后果 | 正确做法 |
|---|---|---|
| 所有异常都重试 | 400/401 重试浪费配额，还可能触发风控 | 按 `retryable` 分类，只重试瞬时错误 |
| 固定间隔重试、没有抖动 | 惊群效应 | 指数退避 + 全抖动 |
| SDK 重试 + 自己重试 + 网关重试 | 重试次数相乘，故障时流量翻几十倍 | 只在一层重试（`max_retries=0`） |
| 只有单请求重试上限 | 下游宕机时流量直接翻 3 倍 | 加全局重试预算 |
| 内层超时 × 重试次数 > 外层超时 | 最后几次重试注定被取消 | 截止时间传播，按剩余时间决定是否重试 |
| 单个请求的 400 也计入熔断 | 个别用户的问题让所有人被降级 | 只统计反映下游健康的错误（`record_if`） |
| 每个 worker 进程各自一个熔断器 | 下游挂了，N 个进程各自再撞 N × 阈值次；新扩容的进程从头再撞 | 共享熔断状态（单机 `SQLiteCircuitBreaker`，多机放到网关层） |
| 悄悄降级 | 质量下降一周没人发现 | 降级事件打点、告警；备用模型跑评估集 |
| 只设 `max_steps` | 一步 10 万 token 照样烧钱 | 多维预算 |
| 在 hook 实例上存"本次运行"的状态 | 不同用户的数据串在一起；恢复后状态丢失 | 存在 `state.metadata` 上（JSON 可序列化） |
| 直接覆盖写检查点文件 | 写到一半崩溃，连旧进度都丢了 | 先写临时文件，再原子替换 |
| 幂等键在工具内部用 `uuid4()` 生成 | 重放时 key 变了，幂等形同虚设 | 用稳定的 `run_id:call_id` |
| 幂等存储放在内存里 | 进程崩溃时它也一起丢了 | 所有进程共享的存储（`SQLiteIdempotencyStore` / Redis / 数据库），并把 key 传给下游 |
| 用"读 JSON 文件 → 改 → 写回"做跨进程的幂等存储 | 两个进程同时写，后写的覆盖先写的（丢更新） | 数据库事务 + 唯一键（`INSERT OR IGNORE`） |
| 用假时钟测完熔断器就上线 | 真实延迟比 `reset_timeout` 还长时，"打开期间快速失败"根本不会发生（Demo 场景 2 实测） | 至少有一个用真实时间、真实下游延迟的端到端演练 |
| 有检查点，但从没测过恢复 | 真出事才发现恢复路径有 bug | 真的杀掉进程再从检查点恢复（Demo 场景 3、`tests/test_distributed.py`） |
| 时长预算把"等审批"的时间也算进去 | 批准的操作执行了，用户却收到"超时中止" | 只统计活跃运行时间（`state.active_seconds`） |
| 中止运行时留下没有结果的 `tool_call` | 下一轮带着这段历史调用模型直接 400 | 给每个未执行的调用补一条"未执行"结果 |

## 7. 面试 & 设计评审问题

<details>
<summary><b>Q1：为什么重试要加抖动？"全抖动"是什么？</b></summary>

- 没有抖动时，同时失败的大量客户端会在同一时刻重试，形成周期性的流量尖峰（惊群效应）；
- 全抖动：等待时间在 `[0, min(cap, base × 2^n)]` 内均匀随机；
- 加分：封顶 cap、优先遵守 `Retry-After`、测试时注入随机数生成器。
</details>

<details>
<summary><b>Q2：请求链路是 前端 → 网关 → Agent 服务 → 模型网关 → 模型，每层都重试 3 次，会怎样？怎么改？</b></summary>

- 重试次数相乘：四层各 3 次尝试，最坏 81 倍流量打到已经过载的模型上；
- 只在一层重试（通常是最懂错误语义的 LLM 适配层），其他层关闭；下层用明确的错误告诉上层"别再重试"；
- 再加全局重试预算和截止时间传播。
</details>

<details>
<summary><b>Q3：模型 API 限额是每秒 100 次，而高峰需求是每秒 180 次，你怎么设计？</b></summary>

- 先认识到：需求持续超过限额时，客户端重试只会推迟失败；
- 交互式请求：多供应商 / 多账号负载均衡扩容量；异步请求：进队列匀速消费；
- 同时从源头减少调用：缓存、小模型分流、合并调用；
- 客户端重试 + 重试预算作为底座，限流器和预算全局共享。
</details>

<details>
<summary><b>Q4：熔断器有哪三种状态？哪些错误应该计入熔断？半开时要注意什么？</b></summary>

- closed → open → half_open，试探成功回到 closed，失败回到 open 并重新计时；
- 计入：5xx、超时、429、依赖级别的错误（key 失效、模型不存在）；不计入：单个请求自身的问题（如上下文超长）；
- 半开时只放行 1 个或少量试探请求。
</details>

<details>
<summary><b>Q5：Agent 运行到一半进程崩溃了，如何保证既不丢进度、也不重复扣款？</b></summary>

- 每一步原子地写入持久化检查点；恢复时只补执行没有结果的工具调用，不让模型重新做决定；
- 写操作使用稳定的幂等键（`run_id + tool_call_id`），幂等存储要持久化；
- 涉及钱的操作把幂等键传给下游，由下游在同一事务里去重，或用业务唯一约束；
- "至少一次执行 + 幂等" = 效果上恰好一次。
</details>

<details>
<summary><b>Q6：审批可能要等几个小时，架构上怎么支持？</b></summary>

- 不能在内存里干等（无论线程还是协程，发布一次就全丢了）：暂停、状态落盘、异步通知、审批后任意实例恢复；
- 审批与具体调用和参数绑定；审批超时按拒绝处理；
- 预算计时要扣除等待时间；复杂流程可以用工作流引擎的信号机制。
</details>

<details>
<summary><b>Q7：怎样防止 Agent 死循环烧钱？</b></summary>

- 步数 + 多维预算（token、金额、工具次数、时长），超限用 StopRun 优雅结束；
- 循环检测：同工具同参数重复，先提醒再中止；状态存在每次运行的 state 上；
- 租户 / 用户级配额、成本告警，评估集里加入容易陷入循环的用例。
</details>

## 8. 自测清单

- [ ] 我能把 Agent 的失败分成四类，并为每一类说出对策
- [ ] 我能说出哪些错误应该重试、哪些不应该，以及为什么要关掉 SDK 自带的重试
- [ ] 我能解释惊群效应、重试放大，以及重试预算和截止时间如何应对
- [ ] 面对"需求持续超过限额"，我能说出重试之外的三种方案
- [ ] 我能画出熔断器三态图，并说出降级的三个隐藏代价
- [ ] 我能说出预算的多个维度，以及循环检测的状态为什么要存在 `state.metadata` 上
- [ ] 我能画出"工具执行了但没记录"的崩溃窗口，并对比幂等存储、下游幂等键、业务唯一约束
- [ ] 我能说出长时间审批的三种实现方式及其取舍
- [ ] 我完成了练习 (a)(b)，并且 `make lesson N=08` 全部通过

## 延伸阅读

- Marc Brooker，[Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)，AWS Architecture Blog（2015）
- Amazon Builders' Library，[Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
- Malcolm Featonby，[Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)，Amazon Builders' Library
- Google SRE Book，[Handling Overload](https://sre.google/sre-book/handling-overload/)（重试预算、客户端自适应限流）与 [Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/)（重试放大、截止时间传播）
- [gRPC 提案 A6：Client Retries](https://github.com/grpc/proposal/blob/master/A6-client-retries.md) —— `retryThrottling` 令牌桶的定义
- [Envoy 熔断器配置（含 retry_budget）](https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/cluster/v3/circuit_breaker.proto)
- Martin Fowler，[CircuitBreaker](https://martinfowler.com/bliki/CircuitBreaker.html)
- Brandur Leach，[Designing robust and predictable APIs with idempotency](https://stripe.com/blog/idempotency)，Stripe Blog（2017）
- Temporal 文档，[Activity Definition](https://docs.temporal.io/activity-definition) —— Activity 至少执行一次，以及如何构造幂等键
- LangGraph 文档，[Durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution) 与 [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- OWASP，[Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/) —— LLM10 Unbounded Consumption 与预算直接相关
