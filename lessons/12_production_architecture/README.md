[中文](README.md) | [English](README.en.md)

# 第 12 课：生产架构 —— 从脚本到服务

> 🕐 建议用时：15 分钟 ｜ 🎯 学完你能：画出企业级 Agent 平台的参考架构，说清每个组件解决什么问题；在同步 / 异步、有状态 / 无状态、租户隔离、框架选型、模型网关这几个架构决策上做出合理选择 ｜ 📦 对应源码：本课练习（多租户限流、模型路由）；迷你部署 [`mini_api.py`](mini_api.py)（API 进程）、[`worker_app.py`](worker_app.py)（worker 进程）、[`deployment.py`](deployment.py)（把它们作为真实进程拉起）；[`agentkit/distributed/`](../../agentkit/distributed/__init__.py)、[`agentkit/limits.py`](../../agentkit/limits.py)、[`agentkit/state.py`](../../agentkit/state.py)
>
> 📖 必读：[12-Factor Agents - Principles for building reliable LLM applications](https://github.com/humanlayer/12-factor-agents)（Dex Horthy, 2025）—— HumanLayer 总结的"把 Agent 做成能交给客户的软件"的 12 条原则，和本课异步任务、无状态 worker、框架选型这几张问题卡片直接对应；重点读 Factor 5（统一执行状态与业务状态）、Factor 6（用简单的 API 启动 / 暂停 / 恢复）和 Factor 8（掌握自己的控制流）。

## 0. 一句话讲清楚

**在家做一桌菜，和开一家每天接待上万人的连锁餐厅，是两件完全不同的事。** 连锁餐厅需要前台接待和排队叫号，需要多个可以互相替班的厨师、写清楚每一桌点了什么的小票、统一的中央采购、食品安全抽检，还要算清每家分店的账。

Agent 也一样。`python agent.py` 在你的电脑上跑得很好，但周一早上 9 点，公司 5000 名员工同时打开 HR 助手：

| 现象 | 缺的是什么 | 在哪里讲 |
|---|---|---|
| 模型提供方返回 429，一半请求失败 | 限流、模型网关的重试与降级 | 本课练习、第 08 课、问题 5 |
| 某个部门的脚本死循环调用，把全公司的配额用光了 | 多租户限流 | 本课练习、本课 Demo 第 4、5 节 |
| "汇总部门上季度请假情况"要跑 2 分钟，网关 60 秒就超时了 | 异步任务 | 问题 1、本课 Demo 第 2 节 |
| 滚动发布时，正在跑的 200 个任务全丢了 | 无状态 worker + 检查点 | 问题 2、本课 Demo 第 3 节、第 13 课 |
| A 公司的员工看到了 B 公司的制度 | 多租户隔离 | 问题 3、第 15 课 |
| 有人改了 prompt，整个下午所有人都得到错误的回答，回滚还要重新发版 | 配置中心、灰度、回滚 | 第 16 课 |
| 月底财务问：AI 花了多少钱，分别算哪个部门的？ | 成本归因 | 本课 Demo、第 14 课 |

这一课是第二部分的**架构总览**：先给出一张完整的参考架构图，说清每个组件在解决什么问题；再讨论 5 个架构层面的关键决策；更深入的主题（并发、成本、RAG、发布运维）分别在第 13-16 课展开。

## 1. 核心概念

### 1.1 参考架构

```mermaid
flowchart TB
    U["用户 / 业务系统"] --> GW["API 网关<br/>鉴权 · 租户识别 · 限流"]
    subgraph CORE["Agent 平台"]
        GW --> SS["会话服务<br/>对话历史 · 流式推送"]
        SS --> Q["任务队列"]
        Q --> W["Agent 运行时 worker<br/>无状态 · 可横向扩展"]
        W <--> ST[("状态存储<br/>检查点 · 会话 · 审批")]
        W <--> VS[("向量库 / 记忆<br/>按租户和权限隔离")]
        W --> MG["模型网关<br/>路由 · 配额 · 计费 · 降级"]
        W --> TS["工具服务<br/>MCP servers"]
        TS --> SB["沙箱<br/>代码执行 · 浏览器"]
    end
    MG --> P1["模型提供方 A"]
    MG --> P2["模型提供方 B"]
    TS --> BIZ["企业系统<br/>HR · 工单 · ERP"]
    subgraph GOV["治理与运维"]
        CFG["Prompt / 配置中心"]
        OBS["可观测性"]
        EV["评估流水线"]
        AUD["审计日志"]
    end
    CFG -.-> W
    W -.-> OBS
    W -.-> AUD
    OBS -.-> EV
```

### 1.2 组件地图：每个组件解决什么问题

| 组件 | 解决什么问题 | agentkit 里的对应 | 深入 |
|---|---|---|---|
| API 网关 | 验证身份、识别租户、限流、拦截超大请求 | 本课练习 `TenantRateLimiter`；迷你部署的 [`mini_api.py`](mini_api.py) | [第 13 课](../13_distributed_concurrency/README.md)（全局限流与背压） |
| 会话服务 | 多轮对话历史、流式推送、断线重连 | `RunResult.history` | 问题 1 |
| 任务队列 | 长任务异步化、削峰填谷、失败重试 | `agentkit.distributed.SQLiteJobQueue` | [第 13 课](../13_distributed_concurrency/README.md)（租约队列、投递语义） |
| Agent 运行时 worker | 执行 Agent 循环；无状态，可随时扩缩容 | [agent.py](../../agentkit/agent.py)；`run_worker` + `AgentJobHandler`（worker 进程） | 问题 2、[第 02 课](../02_agent_loop/README.md)、[第 13 课](../13_distributed_concurrency/README.md) |
| 状态存储 | 检查点、会话、审批状态 | [state.py](../../agentkit/state.py)；`SQLiteCheckpointer`（多进程共享，fence 接管） | [第 08 课](../08_reliability/README.md)、[第 13 课](../13_distributed_concurrency/README.md)（并发写） |
| 向量库 / 记忆 | 知识检索、长期记忆，按租户和权限隔离 | [memory.py](../../agentkit/memory.py) | [第 04 课](../04_context_memory/README.md)、[第 15 课](../15_enterprise_rag/README.md) |
| 模型网关 | 统一接口、密钥托管、路由、配额、计费、降级、缓存 | [llm.py](../../agentkit/llm.py)、[reliability.py](../../agentkit/reliability.py)、本课练习 `choose_model` | 问题 5、[第 14 课](../14_cost_latency/README.md) |
| 工具服务 | 把企业系统包装成标准化的工具（MCP） | [tools.py](../../agentkit/tools.py) | [第 03 课](../03_tools/README.md)、1.5 节 |
| 沙箱 | 隔离执行不可信的代码、浏览网页 | — | [第 09 课](../09_security/README.md) |
| 护栏与权限 | 注入检测、脱敏、最小权限、人工审批 | [guardrails.py](../../agentkit/guardrails.py)、[permissions.py](../../agentkit/permissions.py) | [第 09 课](../09_security/README.md) |
| Prompt / 配置中心 | prompt 和配置的版本管理、灰度发布、回滚 | — | [第 16 课](../16_release_ops/README.md) |
| 可观测性 | 追踪、指标、告警 | [tracing.py](../../agentkit/tracing.py)、[viewer.py](../../agentkit/viewer.py) | [第 10 课](../10_observability/README.md) |
| 评估流水线 | 离线评估、CI 门禁、在线抽样评估 | [evals.py](../../agentkit/evals.py) | [第 11 课](../11_evals/README.md) |
| 审计日志 | 谁、何时、以什么身份、做了什么 | [audit.py](../../agentkit/audit.py) | [第 09 课](../09_security/README.md) |

### 1.3 一次请求的旅程

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant G as API 网关
    participant W as Agent worker
    participant S as 状态存储
    participant M as 模型网关
    participant T as 工具服务
    U->>G: POST /runs（带 JWT）
    G->>G: 验证身份、识别租户、限流
    G->>W: 转发，注入可信的 tenant_id / user_id
    W->>S: 加载会话历史 / 检查点
    loop 每一步
        W->>M: chat（逻辑模型名）
        M-->>W: 工具调用 或 最终回答
        W->>T: 执行工具（身份随上下文传递）
        T-->>W: 工具结果
        W->>S: 写检查点
        W-->>U: 推送进度（SSE，经网关）
    end
    W-->>U: 最终回答
```

三个要点：

1. **身份只在网关验证一次，之后作为可信上下文向下传递**（第 03 课的 `ToolContext`）。模型永远没有机会"声明自己是谁"。
2. **每一步都写检查点**，所以任何一个 worker 随时都可以被替换掉（问题 2）。
3. **worker 只认识逻辑模型名**（`mini`、`pro`），背后用哪家的哪个模型由模型网关决定（问题 5）。

### 1.4 两个守门员：限流和模型路由（本课练习）

**限流：令牌桶（token bucket）。** 想象一个桶，最多装 b 个令牌，每秒匀速滴入 r 个，满了就溢出。每个请求要拿走令牌才能通过，拿不到就返回 `429 Too Many Requests`，并在 `Retry-After` 头里告诉客户端多久以后再试。

```mermaid
flowchart LR
    R["每秒匀速滴入 r 个令牌"] --> B[("桶：最多 b 个")]
    B --> C{"请求来了<br/>桶里的令牌够吗？"}
    C -- "够：拿走令牌" --> OK["放行"]
    C -- "不够" --> NO["429 + Retry-After"]
```

- **b 决定突发**：空闲一阵后，最多能连续放行多少请求；**r 决定长期平均速率**。
- 和"每分钟最多 N 次"的**固定窗口**相比，令牌桶没有窗口边界的问题：固定窗口在两个窗口交界处，短时间内最多能放进 2N 个请求。
- LLM 场景要同时限两样东西：请求数（RPM）和 token 数（TPM）。一个请求可以按预计消耗的 token 数扣令牌，`try_acquire(tokens=...)` 就是为此设计的。
- **每个租户一个桶**防止"吵闹的邻居"；外层再套一个全局桶，保护上游模型的总配额。多副本部署时，桶的状态要放进共享存储并原子更新：本课 Demo 第 4 节实测，两个 API 进程各用各的内存桶，放行了 16 个请求（配置的上限约 9 个）；共用一个 `SQLiteTokenBucket` 时放行 8 个。多机时放进 Redis，Stripe 的工程博客 *Scaling your API with rate limiters* 介绍过他们基于令牌桶和 Redis 的做法。分布式限流和背压在[第 13 课](../13_distributed_concurrency/README.md)展开。

**模型路由：够用就好。** 大多数请求是简单问题，交给便宜的模型；少数难题才用强模型。本课练习的规则顺序是：**先硬约束（要不要工具调用、上下文放不放得下），再质量要求（复杂度高就只用强模型），最后才在剩下的候选里挑最便宜的**。Demo 里，这样路由后每天的账单比"全部用强模型"低 69%。更进一步的做法是**级联**：先让便宜模型回答，校验不通过再升级到强模型，见[第 14 课](../14_cost_latency/README.md)。注意：路由改变了"谁来回答"，每个会被路由到的模型都要单独跑评估集（第 11 课）。

### 1.5 MCP 与 A2A：两个方向的标准接口

| | MCP（Model Context Protocol） | A2A（Agent2Agent） |
|---|---|---|
| 解决什么 | AI 应用 / Agent 如何**连接工具和数据**（纵向） | **不同 Agent 之间**如何协作（横向），即使它们用不同的框架、来自不同的厂商 |
| 发起与归属 | Anthropic 于 2024 年 11 月发布；2025 年 12 月捐给 Linux Foundation 旗下新成立的 Agentic AI Foundation | Google 于 2025 年 4 月发布；2025 年 6 月捐给 Linux Foundation |
| 核心概念 | server 提供 tools、resources、prompts；AI 应用里的 client 连接 server 并调用 | 用 Agent Card 描述一个 Agent 的能力；Agent 之间以"任务"为单位协作 |
| 在架构里的位置 | 工具服务层：把 HR、工单、ERP 系统包装成 MCP server，任何支持 MCP 的 Agent 都能用 | 你的 Agent 和其他团队、其他公司的 Agent 对接 |

A2A 官网对两者关系的概括是：用 MCP 给单个 Agent 配上它需要的工具，用 A2A 让这些 Agent 彼此协作。企业接入 MCP server 时要把它当作**供应链**来管理：只接入审核过的 server，锁定版本，并且意识到工具描述本身也可能藏着注入指令（第 09 课）。

## 2. 企业问题卡片

### 问题 1：一个任务要跑 2 分钟，结果怎么交给用户？

**场景**：HR 助手里，"帮我汇总部门上季度的请假情况并生成报告"平均要走 8 步、耗时 90 秒，最长 4 分钟，而公司负载均衡器的空闲超时是 60 秒。请假申请还要等主管审批，可能是几小时以后。

**为什么难**：同步请求会被网关超时掐断；用户盯着空白页面等 90 秒，会以为卡住了，于是反复点"重试"，结果重复提交；等待审批的几个小时里，任何网络连接都撑不住。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 同步请求-响应 | 一个 HTTP 请求，等 Agent 跑完再返回 | 最简单，客户端最好写 | 超过网关超时就失败；用户干等；长时间占用连接 | 几秒内能完成的问答 |
| B. 流式（SSE） | 服务端用 `text/event-stream` 持续推送事件：token、"正在查询请假记录…"、最终结果 | 体感快，第一个事件很快就到；比 WebSocket 简单；浏览器的 `EventSource` 断线会自动重连，并带上 `Last-Event-ID` | 仍然是一条长连接，受网关和代理的超时、缓冲影响；连接断了任务怎么办需要另外设计 | 交互式对话（几秒到一两分钟） |
| C. 异步任务 | 提交后立刻返回 `202 Accepted` 和 task_id；任务进队列由 worker 执行；客户端轮询状态，或服务端通过 webhook / 消息推送结果 | 不受连接时长限制；可以排队削峰、失败重试；天然支持"暂停等审批" | 架构更复杂（队列、状态存储、通知）；要设计"进行中"的交互 | 长任务、批处理、需要人工审批的流程 |

**怎么选**：按任务时长和是否需要人工介入来定。10 秒内的用 A；交互式对话默认用 B；超过一两分钟、或者可能暂停等审批的用 C。实践中最常见的是 **C + B 组合**：任务异步执行、状态落库，客户端通过 SSE 订阅进度，断线后凭 task_id 重新订阅。记住一点：**流式只是传输方式，任务的生命周期不能绑在一条连接上**，连接断了，任务要继续跑。

**本课实现**：迷你部署实现了方案 C（Demo 第 2 节）：API 进程收到 `POST /runs` 只做鉴权、限流、路由、入队，实测 8 毫秒就返回 `202` 和 run_id；worker 进程在后台跑，客户端轮询 `GET /runs/{run_id}`。对照组 `POST /runs/sync` 在请求里跑 Agent：客户端 2 秒超时断开，服务端却在 2.5 秒之后把它跑完 —— 钱花了，没人拿到结果。"暂停等审批、审批后由任意 worker 恢复"用的是同一套机制：`AgentJobHandler` 的 `resume` 任务（[第 13 课](../13_distributed_concurrency/README.md)），[capstone/server.py](../../capstone/server.py) 有 `POST /runs/{run_id}/approval` 的写法。任务队列、租约和投递语义见[第 13 课](../13_distributed_concurrency/README.md)。

### 问题 2：Agent worker 要不要把会话留在内存里？

**场景**：3 台机器跑 Agent 服务，每天两次滚动发布。某次发布时，200 个进行中的任务全部丢失，其中一些已经替用户提交了请假申请，却没来得及告诉用户结果。还有用户的第二轮对话被负载均衡到了另一台机器，Agent 完全不记得上一轮说过什么。

**为什么难**：状态放在内存里最快、最简单，但机器会重启、会扩缩容。状态放到外部又会带来新问题：两个 worker 同时处理同一个会话怎么办？工具已经执行了、检查点还没来得及写，这时候崩溃怎么办？

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 有状态 + 粘性会话 | 负载均衡按会话把请求固定到同一台机器，状态存在内存里 | 实现简单，延迟最低 | 重启或发布就丢状态；负载不均；没法随意扩缩容 | 原型、小型内部工具 |
| B. 无状态 worker + 外部状态 | 每一步都把状态写进 Postgres / Redis（检查点），任何 worker 都能接手任何任务 | 横向扩展、滚动发布、崩溃恢复都变得简单 | 每步多一次读写；要处理并发（锁或版本号）和"执行了但没记录"的窗口（幂等） | 大多数生产系统的默认选择 |
| C. 持久化执行引擎 | 用 Temporal 这类引擎：编排逻辑写成 workflow，调模型、调工具写成 activity，引擎记录事件历史，崩溃后重放恢复 | 重试、超时、长时间等待、崩溃恢复都由引擎保证 | 多一套基础设施；有编程约束（workflow 代码必须是确定性的）；学习成本高 | 长流程、跨多个系统、失败代价高（比如涉及资金） |

**怎么选**：默认用 B。流程很长、跨多个系统、失败代价高时考虑 C。A 只用于原型。不管选 B 还是 C，**写操作工具都必须幂等**（第 08 课）：恢复时重放一次工具调用，不能重复扣款、重复提交申请。

**本课实现**：agentkit 用检查点实现方案 B：主循环每走一步都会调用 `checkpointer.save`。迷你部署里，检查点存在所有进程共享的 `SQLiteCheckpointer` 里（Demo 第 3 节）：一个长任务跑到第 1 步时，持有它的 worker 进程被 `kill -9`；租约过期后另一个 worker 进程领走它（第 2 次领取，fence 1 → 2），从检查点接着跑完第 2、3 步，检查点的最后写入者换成了新 worker —— 已经做完的第 1 步没有重做。客户端全程只是在轮询，两个 API 进程谁来回答都一样，因为状态不在任何一个进程的内存里。生产中把 SQLite 换成 Postgres / Redis（第 26 课）；多个 worker 并发写同一个会话的问题（分布式锁、乐观锁、分区串行、fencing token）见[第 13 课](../13_distributed_concurrency/README.md)。

### 问题 3：多租户隔离要做到什么程度？

**场景**：一个 SaaS 形态的 HR 助手服务 300 家企业客户。其中 2 家银行要求"我们的数据不能和其他客户放在一起"，其余大多是看重价格的中小企业。某次事故中，语义缓存没有按租户区分，A 公司的员工收到了 B 公司的年假制度。

**为什么难**：隔离越彻底越安全，但成本和运维负担会随租户数线性增长；共享得越多越省钱，但每一层（数据库、向量库、缓存、记忆、日志、配额）都必须正确地按租户隔离，漏掉一处就是数据泄露。

下表的三种模式借用了 AWS 白皮书 *SaaS Tenant Isolation Strategies* 的术语：

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 独立部署（silo） | 每个租户一套独立的服务和存储 | 隔离最强，满足严格的合规要求；一个租户出问题不影响别人 | 成本高；几百套部署的升级和运维负担很重 | 少数大客户、强监管行业 |
| B. 共享部署 + 逻辑隔离（pool） | 所有租户共用服务和存储，每条数据都带 tenant_id，所有查询、缓存键、向量检索都按租户过滤 | 成本低、运维简单、资源利用率高 | 隔离全靠代码正确，漏一个过滤条件就泄露；有"吵闹的邻居" | 大量中小租户 |
| C. 混合（bridge） | 默认共享；对大客户或敏感组件单独部署（比如独立的数据库和向量库），计算层仍然共享 | 在成本和隔离之间按需取舍 | 两种模式并存，架构和运维更复杂 | 客户规模差异大的 SaaS |

**怎么选**：先按合规要求分级。有明确"物理隔离"要求的客户用 A，或者至少是数据层独立的 C；其余用 B。选 B 时，隔离必须是**框架层的默认行为**，而不是靠每个开发者记得加过滤条件：身份由网关注入；存储层强制带 tenant_id；缓存键里包含 tenant_id；每个租户有独立的限流桶和配额；成本按租户归因。Agent 系统里特别容易漏掉的隔离点有：长期记忆、语义缓存、检索索引、工具凭证（每个租户应该用自己的凭证访问自己的企业系统）以及 trace 数据。

**本课实现**：练习里的 `TenantRateLimiter` 给每个租户一个独立的桶；agentkit 的 `ToolContext` 由系统注入身份，`MemoryStore` 按租户和用户隔离（第 04 课）。迷你部署里：身份来自 API key（代替网关验证过的 JWT），入队时写进任务的 `tenant_id`，`AgentJobHandler` 只认它、不认 payload 里自称的租户；工具从 `ctx` 拿租户和用户；查别的租户的 run 返回 404（测试验证）；worker 里还有按租户的舱壁（Demo 第 5 节），成本按租户记账。权限感知的检索和索引隔离见[第 15 课](../15_enterprise_rag/README.md)。

### 问题 4：自建、用框架，还是用托管平台？

**场景**：一个 5 人团队要在一个季度内上线 3 个内部 Agent（HR、IT、财务报销）。公司有 Kubernetes 平台，但没有专门的 AI 平台团队；安全团队要求所有工具调用都可审计，高危操作必须人工审批。

**为什么难**：框架能省掉 Agent 循环、工具调用、多 Agent 编排这些样板代码，但在权限、审计、数据流这些地方可能限制你；自建的控制力最强，但持久化、追踪、评估都要自己做；托管平台最省运维，但数据和运行环境在别人那里，还有被锁定的风险。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 自建 | 自己写 Agent 循环、工具层、钩子，接入公司已有的存储、追踪和鉴权（本课程的 agentkit 就是一个教学版） | 完全可控，和现有基础设施无缝集成，没有多余的抽象 | 持久化执行、并发、评估等都要自己做，维护成本高 | 核心业务，安全合规要求高，需要深度定制 |
| B. 开源框架 | LangGraph、OpenAI Agents SDK、Claude Agent SDK、Google ADK 等（见下表） | 开发快，有社区生态和现成的最佳实践 | 抽象未必合适；版本变化快；出问题时要读框架源码 | 大多数新项目 |
| C. 托管平台 | 由云厂商或模型厂商托管运行时、记忆、身份、观测等。例如 Amazon Bedrock AgentCore（Runtime、Gateway、Memory、Identity、Observability 等组件），Anthropic 的 Managed Agents（托管的 agent 运行环境和沙箱） | 运维最少，能很快获得企业级能力 | 数据和执行环境在第三方；厂商锁定；定制受限 | 没有平台团队、希望尽快上线，并且合规允许 |

常见框架对照（只列确定的事实，细节以各自官方文档为准）：

| | 出品方 | 定位与核心抽象 | 持久化与恢复 | 语言 |
|---|---|---|---|---|
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | LangChain 团队 | 底层编排框架，用图（节点、边、共享状态）描述 Agent 和工作流 | checkpointer 持久化，支持人工介入后恢复 | Python、JavaScript |
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) | OpenAI（2025 年 3 月发布） | 轻量框架：Agent、handoff（移交）、guardrail、session，内置 tracing | session 管理对话记忆；可以和 Temporal 集成获得持久化执行 | Python、TypeScript |
| [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) | Anthropic（2025 年 9 月由 Claude Code SDK 更名而来） | 以库的形式提供 Claude Code 背后的 agent 循环：内置工具、hooks、子 Agent、MCP、权限控制 | session 可以恢复、可以分叉 | Python、TypeScript |
| [Google ADK](https://google.github.io/adk-docs/) | Google（2025 年 4 月发布） | 代码优先的开源框架：多 Agent 组合、工具生态、评估与开发 UI | 提供 session / state 管理 | Python、Java、Go、TypeScript 等 |
| [Temporal](https://docs.temporal.io) | Temporal（开源，MIT 许可） | 不是 Agent 框架，而是持久化执行平台：workflow 编排，activity 执行有副作用的操作 | 事件历史持久化，崩溃后重放恢复 | 多语言 SDK |

**怎么选**：先回答两个问题：这个 Agent 是不是核心业务、对权限和审计有没有特殊要求？团队有没有能力运维一套平台？一般路径是从 B 起步，选和你的模型、语言、云环境匹配的框架；核心业务、定制需求多的，用 A，或者"自建外壳 + 框架内核"；没有平台团队而且合规允许的，考虑 C。不管选哪种，**身份注入、权限、审计、评估这些横切能力都要自己掌握**。框架能帮你省掉"循环怎么写"，省不掉评估集、权限模型和成本控制。

**本课实现**：agentkit 就是方案 A 的教学版。它的核心设计（钩子、检查点、`ToolContext`、追踪、评估）在上面这些框架里都有对应的概念，吃透它，再上手任何一个框架都会很快。

### 问题 5：模型网关自建、用开源，还是用云厂商的？

**场景**：公司同时在用 3 家模型提供方，12 个团队各自在代码里写死了 API key。某天一个 key 泄露到了公开仓库，没人说得清有哪些服务在用它。月底财务问 AI 花了多少钱、分别算哪个部门的，也没人答得上来。

**为什么难**：模型网关位于所有模型调用的关键路径上：它挂了，全公司的 Agent 都停；它慢了，每一步都慢。它要承担的功能又很多：统一接口、密钥托管、路由、配额、计费、降级、缓存、审计。自己做工作量不小，用现成的又要评估性能、数据合规和可扩展性。

| 方案 | 怎么做 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 自建薄代理 | 自己写一个 OpenAI 兼容的代理：鉴权、转发、记账，再按需加路由和降级 | 完全可控，能和内部的鉴权、计费系统深度集成 | 要自己跟进各家 API 的差异和变化，功能要一点点补 | 有平台团队、需求特殊 |
| B. 开源网关 | 例如 [LiteLLM](https://github.com/BerriAI/litellm)：开源的 Python SDK 加代理服务，用 OpenAI 格式统一访问 100 多家模型提供方，支持虚拟 key、按 key / 用户 / 团队统计花费、预算、限流、负载均衡和降级 | 功能全、上线快；可以自托管，数据不出公司 | 要自己保证高可用；要评估它的性能和安全更新节奏 | 大多数企业的起点 |
| C. 云厂商的 AI 网关 | 例如 Azure API Management 的 AI 网关能力（按 token 限流的 `llm-token-limit` 策略、语义缓存）、Cloudflare AI Gateway（缓存、限流、分析、降级） | 托管、免运维，和同一家云的其他服务集成好 | 绑定云厂商；跨云、多提供方的灵活性受限 | 已经深度使用某一家云 |

**怎么选**："所有模型调用都必须经过网关"这条纪律，比选哪个产品更重要。起步通常用 B 或 C；规模很大或者有特殊需求时，在 B 的基础上二次开发，或者换成 A。不管选哪种，都要做到：业务代码只认识**逻辑模型名**（`mini`、`pro`），真实模型由网关配置决定；网关本身多副本部署；准备好绕过网关直连某家提供方的应急通道。

**本课实现**：agentkit 的 LLM 抽象（第 02 课）让业务代码只依赖 `chat(messages, tools)`；本课练习的 `choose_model` 相当于网关里的路由规则，迷你部署的 API 进程对每个请求调用它，把选中的逻辑模型写进任务；第 08 课的 `ResilientLLM` 相当于网关里的重试、熔断和降级。你本机 `.env` 指向的 cliproxyapi（`http://localhost:8317/v1`）就是一个 OpenAI 兼容的本地代理：代码只知道一个地址和一个 key，背后接的是哪个模型，由代理决定。

## 3. 从玩具到生产：agentkit 在参考架构里的位置

agentkit 的核心是 async 的：一个进程里同时推进几百个会话（第 02 课）；`agentkit.distributed` 负责多个进程之间的分工：共享的任务队列、带 fence 的检查点、跨进程的令牌桶和并发槽位、worker 进程和故障注入（第 13 课）。但它仍然是教学实现：存储是 SQLite（只能在一台机器上、同一时刻只有一个写者），没有真正的网关、配置中心和多机部署。上生产时，每一块要换成什么：

| 能力 | agentkit 教学实现 | 生产中换成 |
|---|---|---|
| Agent 循环 | [agent.py](../../agentkit/agent.py)，async，一个进程同时跑几百个会话；`agentkit.distributed` 的 worker 进程从队列领任务（本课迷你部署） | 无状态 worker 集群（K8s Deployment），由队列驱动、按积压量扩缩容（问题 1、2，第 31 课） |
| 任务队列 | `SQLiteJobQueue`：租约、fence、重试、死信（单机多进程） | Postgres `SKIP LOCKED` / SQS / Redis Streams（第 13、26 课） |
| 检查点 | `InMemoryCheckpointer` / `FileCheckpointer`（单进程）、`SQLiteCheckpointer`（多进程共享，版本号 CAS + fence 接管） | Postgres / Redis（第 26 课） |
| 工具执行 | async 工具直接 await，同步工具进线程池，`@tool(isolation="process")` 放进子进程（超时能真正杀掉） | 独立的工具服务（MCP server）+ 沙箱（容器、gVisor、Firecracker） |
| 模型调用 | `OpenAICompatLLM` + `ResilientLLM` | 模型网关（问题 5） |
| 限流 | 练习的 `TokenBucket` / `TenantRateLimiter`（纯算法）、`agentkit.limits.TokenBucket`（进程内）、`SQLiteTokenBucket`（单机多进程共享） | 网关层统一执行；Redis + Lua 原子脚本实现的分布式令牌桶（第 26 课） |
| 并发舱壁 | `agentkit.limits.KeyedLimiter`（进程内）、`SQLiteSemaphore`（跨进程，带租约） | 网关 / Redis 上的并发配额 |
| 追踪 | `Tracer` + JSONL + 查看器 | OTel SDK + Collector + 后端（第 10 课） |
| Prompt 与配置 | 代码里的字符串 | 配置中心 + 版本管理 + 灰度发布（第 16 课） |
| 审计 | `AuditLog` 写 JSONL | 只能追加、不可篡改的存储（第 09 课） |
| 评估 | `run_eval` | CI 流水线 + 在线抽样评估（第 11 课） |

### 3.1 迷你部署：参考架构在一台机器上跑起来

Demo 用真实的进程把参考架构的骨架搭了出来（[`deployment.py`](deployment.py)）：

```mermaid
flowchart LR
    C["客户端（httpx）<br/>轮流发给各个 API 进程"] -->|"HTTP"| A1["api-1 / api-2<br/>共享令牌桶"]
    C -->|"HTTP"| A2["mem-1 / mem-2<br/>进程内令牌桶（对照组）"]
    A1 -->|"入队 · 202"| DB[("jobs.db（SQLite）<br/>队列 · 检查点 · 令牌桶 · 槽位")]
    A2 -->|"入队 · 202"| DB
    DB <-->|"领取 · 心跳 · 检查点 · 提交"| W["worker-0 / worker-1<br/>python -m agentkit.distributed.worker"]
```

- **API 进程**：[`mini_api.py`](mini_api.py) 是一个 FastAPI 应用，每个进程是一条 `python -m uvicorn mini_api:app --app-dir lessons/12_production_architecture --port ...` 命令（真正的子进程，不是线程）。`POST /runs`：API key → 租户身份 → 按租户限流（不够就 `429` + `Retry-After`）→ `choose_model` 路由 → 入队 → `202`。`GET /runs/{id}` 读任务和检查点。同一份代码起了 4 个进程，只是限流后端不同（环境变量 `MINI_RATE_BACKEND`）。
- **worker 进程**：`WorkerPool` 拉起 2 个 `python -m agentkit.distributed.worker`，加载 [`worker_app.py`](worker_app.py)：先过两层按租户的舱壁，再交给 `AgentJobHandler` 跑 HR Agent，每一步把检查点写进同一个 SQLite 文件。
- **停机**：先对 API 进程发 SIGTERM（uvicorn 停止接新请求、处理完手上的请求、执行 FastAPI 的关闭逻辑），再对 worker 发 SIGTERM（`run_worker` 停止领取、排空在途任务），超时仍未退出的进程 SIGKILL。
- **和真实部署的差别**：所有进程在一台机器上；SQLite 同一时刻只有一个写者（[第 13 课 3.12 节](../13_distributed_concurrency/README.md)实测了上限）；没有负载均衡器，客户端自己轮流发给各个 API 进程；身份用一张 API key 表代替网关验证过的 JWT。多机版本见第 26、31 课。
- 需要可选依赖：`pip install -e ".[server]"`（FastAPI、uvicorn、httpx）。没装时 Demo 打印安装命令后以退出码 1 结束，测试里的部署用例自动跳过。

### 3.2 练习里的限流器，和它在多进程里的样子

**练习里的两个设计决策：**

- **令牌桶用"惰性补充"**：不开后台线程定时加令牌，而是每次访问时，根据距上次更新过去了多久，一次性算出该补多少。这样是 O(1) 的，没有定时器，也便于移植成 Redis 的原子脚本。**时钟可注入**，所以测试可以用假时钟，结果完全确定。还有一个容易忽略的边界：时钟倒退时，不能把"上次更新时间"也往回拨，否则时钟走回来时，这段时间会被重复计算、凭空多出令牌。
- **路由找不到合适的强模型时直接报错，而不是悄悄降级到弱模型**。"悄悄降级"会让质量问题无声无息地发生。要降级，应该由调用方显式决定，并记录到 trace 里。

练习是纯算法（普通的同步函数、注入的时钟），这是合理的单元测试方式：算法对不对，和它跑在几个进程里无关。但**状态放在哪里**决定了它在部署里是否成立。同一个"补充 + 扣减"的算法有三个版本：

| 版本 | 桶的状态在哪 | 几个进程共用一个桶 | 本课在哪里用 |
|---|---|---|---|
| 练习的 `TokenBucket` / `TenantRateLimiter` | 一个 Python 对象的属性 / dict | 一个进程 | Demo 第 4 节的 `mem-1`、`mem-2`（没写完时用参考答案） |
| `agentkit.limits.TokenBucket` | 进程内的 dict（按 key），`acquire` 等待时让出事件循环 | 一个进程 | 一个进程里自己限速（例如调模型前 `await bucket.acquire(key)`）；本课 Demo 没有用到 |
| `agentkit.distributed.SQLiteTokenBucket` | SQLite 表里的一行，"补充 + 扣减"在一个 `BEGIN IMMEDIATE` 写事务里 | 一台机器上的所有进程 | Demo 第 4 节的 `api-1`、`api-2` |
| Redis + Lua 脚本 | Redis 里的一个 key，脚本在服务端原子执行 | 所有机器 | [第 26 课](../26_state_and_queues/README.md) |

Demo 第 4 节的实测：free 套餐（容量 5、每秒补 2 个），2 秒内配置允许约 9 个；两个进程各用各的内存桶放行了 16 个（每个进程各 8 个 —— 每个进程都以为自己是唯一的那一个），共用 `SQLiteTokenBucket` 放行了 8 个。进程数翻倍，内存桶的实际配额就翻倍；K8s 自动扩容时，配额会跟着副本数悄悄变大。测试 `test_in_memory_buckets_multiply_the_limit_but_a_shared_bucket_holds_it` 把这一点固定了下来：内存桶至少放行 2 × 5 个，共享桶不超过"容量 + 速率 × 耗时"。

练习里的 `TenantRateLimiter` 还有一个问题：桶放在 dict 里只增不减。长期不活跃的租户要淘汰（LRU / TTL），否则内存会无限增长；`agentkit.limits.KeyedLimiter` 用引用计数在没人使用时回收。

**按租户的舱壁**（Demo 第 5 节）是另一种限流：不限"每秒多少次"，而是限"同时有几个在跑"。worker 在把任务交给 Agent 之前先拿两层槽位：进程内的 `KeyedLimiter`（每个租户在一个 worker 里最多 2 个）和跨进程的 `SQLiteSemaphore`（所有 worker 加起来最多 3 个）；拿不到就抛 `RetryLater`，任务放回队列、不消耗重试次数，worker 转去领别的任务。实测 hooli 的 24 个任务：所有 worker 加起来同时最多 3 个、每个 worker 里最多 2 个，被推迟 47 次，没有一个失败。只有进程内的舱壁时，上限是 2 × worker 数，扩容就会放大；跨进程的槽位不随副本数变化。

> 注意：这里的舱壁放在 worker 的 handler 里，而没有用 `Agent(limiter=..., limiter_timeout=...)`。实测发现后者有一个框架问题（已报告给维护者）：Agent 在把用户输入写进状态**之前**就去拿槽位，拿不到时以 `rate_limited` 结束并保存检查点，这个检查点里没有用户的问题；`AgentJobHandler` 推迟后再 `resume`，模型看到的是一段没有用户问题的对话。

> 🏭 **生产版**：放在 Redis 里、用 Lua 脚本保证原子性的跨实例令牌桶，以及 Postgres 上的检查点和任务队列，见[第 26 课](../26_state_and_queues/README.md)；用 LiteLLM Router 落地的模型网关、用 Cedar 写成策略文件的权限、分级的分类器护栏，见[第 29 课](../29_gateway_and_guardrails/README.md)；把参考架构真正组装起来的 API + 多 worker 服务、压测、故障注入和扩缩容，见[第 31 课](../31_deployment_and_scaling/README.md)。

## 4. 动手：运行 Demo

```bash
pip install -e ".[server]"                                     # 迷你部署需要 FastAPI、uvicorn、httpx
python lessons/12_production_architecture/demo.py --offline   # 剧本模型，约 15 秒
python lessons/12_production_architecture/demo.py             # worker 和同步接口调用真实模型（约 60 次模型调用）
```

第 0 节是纯计算（模型路由算账），两种模式输出相同；第 1–5 节起一个真实的迷你部署：4 个 API 进程 + 2 个 worker 进程。下面是离线模式的实际输出节选：

```
  合计：路由后每天 $36.73，全用强模型每天 $117.62，节省 69%
...
  ❌ POST /runs/sync → 2.0 秒后客户端超时（httpx.ReadTimeout），用户只看到失败
  ✅ POST /runs → 202，8 毫秒返回（api-1）：run_id=job-1，路由到逻辑模型 pro
...
  [+ 0.3s] api-2  任务 leased    run running   第 0 步  检查点最后写入者 worker-0  fence=1  第 1 次领取
  [+ 1.2s] api-1  任务 leased    run running   第 1 步  检查点最后写入者 worker-0  fence=1  第 1 次领取
  [+ 1.2s] 💥 kill -9 worker-0（pid 42729，退出码 -9）：它正跑到一半，内存里的一切都没了
  [+ 1.2s]    K8s 会拉起一个新 Pod 补上：worker-2（pid 42734）
  [+ 2.8s] api-2  任务 queued    run running   第 1 步  检查点最后写入者 worker-0  fence=1  第 1 次领取
  [+ 3.4s] api-2  任务 leased    run running   第 1 步  检查点最后写入者 worker-2  fence=2  第 2 次领取
  [+ 4.6s] api-2  任务 leased    run running   第 2 步  检查点最后写入者 worker-2  fence=2  第 2 次领取
  [+ 5.9s] api-2  任务 succeeded run completed 第 3 步  检查点最后写入者 worker-2  fence=2  第 2 次领取
  回头看那个同步请求：服务端的 run sync-demo-1 在客户端放弃 2.5 秒之后跑完了（状态 completed，3 步）
...
  进程内的桶（mem-1 + mem-2）    发出  39，放行 16（mem-1 放行 8，mem-2 放行 8），其余 429（Retry-After: 1）
  共享的桶（api-1 + api-2）      发出  39，放行  8（api-1 放行 4，api-2 放行 4），其余 429（Retry-After: 1）
  配置的上限：一个桶 5 + 2/秒 × 2.0 秒 ≈ 9 个。
...
  acme 的 3 个问题全部完成用了 1.2 秒（每个问题本身 2 次模型调用，约 0.5 秒），状态 succeeded, succeeded, succeeded
  hooli 的 24 个任务又过了 1.5 秒才全部做完；期间因为舱壁满了被推迟（RetryLater）47 次
  hooli 同时在跑的任务数峰值：所有 worker 加起来 3（跨进程上限 3），每个 worker 进程里 2（进程内上限 2）
...
  租户      放行  被限流  完成  tokens   成本（示例价格，按逻辑模型）
  hooli     24    54      24    19848    $0.003269
  acme      4     0       4     7644     $0.020719
...
  退出码：api-1=-15，api-2=-15，mem-1=-15，mem-2=-15，worker-0=-9，worker-1=0，worker-2=0
```

**重点观察：**

1. **第 0 节：绝大多数流量是简单请求**，交给便宜模型就够了；超出所有模型上下文的请求被直接拒绝，而不是硬塞进去。
2. **第 2 节：长任务不要绑在一个 HTTP 连接上。** 同步接口被 2 秒的客户端超时掐断，服务端却照样跑完（白花钱，用户多半还会再点一次）；异步接口 8 毫秒返回 `202`，任务在 worker 里跑。
3. **第 3 节：worker 无状态，是因为状态全在共享存储里。** kill -9 正在跑第 1 步的 worker 后，任务在租约（1.5 秒）过期后被回收、退避一小会儿，再被新 worker 领走（fence 1 → 2），从第 1 步之后接着跑，第 1 步没有重做。轮询请求交替打到 api-1 / api-2，回答一致。
4. **第 4 节：进程内的限流器在多副本下会放大配额**（16 vs 上限 9）；共享的桶守住了（8）。
5. **第 5 节：舱壁限的是"同时有几个在跑"。** 吵闹的 hooli 同时最多 3 个（跨进程）、每个 worker 里最多 2 个，被挡住的任务推迟而不是失败；acme 的问题照常在 1.2 秒内做完。
6. **记账**：每个 run 的 token 用量在检查点里，按 API 路由时选中的逻辑模型定价，每一分钱都能归到租户。
7. **退出码**：uvicorn 收到 SIGTERM 后先优雅停机（日志里有 `Application shutdown complete`），再按惯例用收到的信号结束自己，所以是 -15；worker 排空后正常退出是 0，被 kill -9 的是 -9。

## 5. 练习

打开 [exercise.py](exercise.py)，实现下面这些。它们都是**普通的同步代码**（纯算法，不需要 `async` / `await`），时钟可注入，测试用假时钟、结果完全确定：

| 要写的 | 要点 |
|---|---|
| `TokenBucket._refill / try_acquire / retry_after` | 惰性补充、封顶 capacity；不够时不扣令牌；时钟倒退时不回拨；`retry_after` 在永远等不到时返回 `math.inf` |
| `TenantRateLimiter._bucket` | 按需创建、缓存每个租户的桶；未登记的租户用默认套餐；所有桶共用同一个时钟 |
| `choose_model(task, models)` | 能力 → 容量（输入 + 输出）→ 质量 → 成本；成本相同时按列表顺序；候选为空时抛 `NoModelAvailable`，并说明是哪条规则排除的 |

```bash
make lesson N=12
# 等价于 .venv/bin/python -m pytest lessons/12_production_architecture
```

写完后重新跑 Demo，第一行会显示"实现来自 exercise.py（你的实现）"：第 0 节的路由算账、API 进程里的 `choose_model` 和 `mem-1` / `mem-2` 的 `TenantRateLimiter` 都会换成你的代码 —— 你写的限流器会被真的放进两个 API 进程里，亲眼看它放行 2 倍。

测试文件后半部分的 3 个用例不是练习：它们起真实的 API 进程和 worker 进程，验证"内存桶在两个进程里放行至少 2 倍、共享桶守住上限""kill -9 之后另一个 worker 从检查点接着跑""按租户的舱壁在多个 worker 进程之间也成立"。它们用参考答案做路由和限流，所以不做练习也会通过；没装 `.[server]` 时自动跳过。

## 6. 深入：上线检查清单

每一项后面标注了对应的课程。

**可靠性**
- [ ] 模型调用有超时、重试、降级（08）
- [ ] 步数、token、金额都有上限（08）
- [ ] 每一步写检查点，写操作工具幂等（08、13）
- [ ] 按租户限流，有配额（12、13）

**安全**
- [ ] 身份由网关注入，模型拿不到身份参数（03、12）
- [ ] 最小权限，高危操作人工审批（09）
- [ ] 不可信代码在沙箱里执行（09）
- [ ] 输入、输出、工具输出三道护栏（09）
- [ ] 密钥托管在网关或密钥管理服务里（12）

**可观测**
- [ ] trace 覆盖模型和工具调用（10）
- [ ] 指标看板和告警（10）
- [ ] 用户能报出 run_id，能从投诉反查 trace（10）
- [ ] 有 PII 处理策略（10）

**评估**
- [ ] 评估集 + CI 门禁，安全用例一票否决（11）
- [ ] 线上抽样评估（11）

**发布与运维**
- [ ] prompt 和模型版本化（16）
- [ ] 灰度发布和一键回滚（16）
- [ ] kill switch：出事时能立刻关掉某个工具或整个 Agent（16）

**成本**
- [ ] 按租户、功能归因（12、14）
- [ ] 预算告警（10、14）
- [ ] 模型路由和缓存（12、14）

**合规**
- [ ] 审计日志完整、不可篡改（09）
- [ ] 数据保留期有明确规定（10）
- [ ] 评估过模型提供方的数据处理条款，以及数据出境问题

## 7. 常见坑与反模式

1. **把 Agent 当成普通的同步 API**：长任务被网关超时掐断，用户反复重试，导致重复提交。
2. **状态放在 worker 内存里**：每次发布都丢任务。
3. **让模型或客户端声明身份**：`user_id` 作为工具参数由模型填，或者直接信任客户端的请求头。
4. **只有一个全局限流**：一个租户就能让所有租户一起被 429。
5. **每个团队自己接模型提供方**：key 散落各处，成本算不清，出事时没法统一降级。
6. **共享缓存不带 tenant_id**：语义缓存把 A 公司的答案返回给了 B 公司。
7. **路由规则悄悄降级**：复杂任务被路由到弱模型，质量问题没人发现。
8. **先选框架，再想需求**：框架的抽象和你的权限、审计模型不匹配，最后只能绕着框架写代码。
9. **prompt 写死在代码里**：改一个字都要发版，出了问题也没法快速回滚。
10. **多个副本各用各的内存限流器**：实际放行的是"配置 × 副本数"（本课实测 2 个进程放行了约 2 倍），自动扩容时配额跟着悄悄变大。

## 8. 面试 & 设计评审问题

<details>
<summary>Q1：画出一个企业级 Agent 平台的架构，说明一次请求经过哪些组件。</summary>

- 网关（鉴权、租户识别、限流）→ 会话服务 → 队列 → 无状态 worker（Agent 循环）→ 模型网关 / 工具服务（MCP）/ 沙箱；
- 状态存储（检查点、会话、审批）、向量库和记忆（按租户、权限隔离）；
- 治理：配置中心、可观测性、评估流水线、审计日志；
- 关键点：身份在网关验证一次后注入上下文；每步写检查点；worker 只认逻辑模型名。
</details>

<details>
<summary>Q2：一个任务可能要跑几分钟，还可能要等人工审批，你怎么设计交互？</summary>

- 异步任务：提交后返回 202 和 task_id，任务进队列，状态落库；
- 进度通过 SSE 推送，断线后凭 task_id 重新订阅，也可以轮询；
- 等待审批时暂停并写检查点，审批通过后由任意 worker 恢复；
- 结果通过站内消息或 webhook 通知；写操作工具幂等，重复提交不会重复执行。
</details>

<details>
<summary>Q3：为什么 worker 要无状态？无状态之后会引入什么新问题？</summary>

- 好处：横向扩展、滚动发布、崩溃恢复都变得简单；
- 新问题：每一步多一次读写；多个 worker 并发处理同一个会话（需要锁、版本号或按会话分区串行）；工具执行了但检查点没写（需要幂等）；
- 流程很长、失败代价高时，可以考虑 Temporal 这类持久化执行引擎。
</details>

<details>
<summary>Q4：300 个租户，其中 2 个是银行，你怎么设计隔离？</summary>

- 混合模式：银行的数据层独立部署（独立的数据库、向量库，甚至独立的密钥），其余租户共享部署、逻辑隔离；
- 共享部分的隔离由框架默认保证：身份由网关注入、存储层强制带 tenant_id、缓存键包含 tenant_id；
- 每个租户独立的限流桶和配额，成本按租户归因；
- 容易漏的地方：记忆、语义缓存、检索索引、工具凭证、trace 数据。
</details>

<details>
<summary>Q5：团队要上线第一个 Agent，你会推荐自建还是用框架？</summary>

- 先看约束：是否是核心业务、权限和审计要求、团队的运维能力、数据合规；
- 一般从成熟的开源框架起步，选和模型、语言、云匹配的；
- 身份注入、权限、审计、评估这些横切能力无论如何都要自己掌握；
- 核心业务、定制需求多的考虑自建或"自建外壳 + 框架内核"；没有平台团队且合规允许，可以考虑托管平台。
</details>

<details>
<summary>Q6：为什么需要模型网关？它应该具备哪些能力？</summary>

- 统一接口，让业务代码和提供方解耦；密钥集中托管，泄露时能统一轮换；
- 路由（按任务选模型）、配额与限流、按租户 / 团队计费；
- 重试、熔断、降级，缓存，审计日志；
- 纪律：所有模型调用必须经过网关；网关本身多副本部署，并保留应急直连通道。
</details>

## 9. 自测清单

- [ ] 我能画出参考架构图，并说出每个组件解决什么问题
- [ ] 我能根据任务时长和是否需要审批，在同步、流式、异步之间做选择
- [ ] 我能解释为什么 worker 要无状态，以及无状态之后要解决哪些新问题
- [ ] 我能说出 silo、pool、bridge 三种隔离模式的取舍，以及 Agent 系统里容易漏掉的隔离点
- [ ] 我能说出选择自建、框架、托管平台的依据
- [ ] 我能说清模型网关的职责，以及为什么业务代码只该认逻辑模型名
- [ ] 我能解释令牌桶的两个参数分别控制什么，以及为什么要每个租户一个桶
- [ ] 我能解释为什么多副本部署时进程内的限流器会放行 N 倍，以及共享的令牌桶、跨进程的舱壁怎么守住上限
- [ ] 我完成了练习：`make lesson N=12` 全部通过

## 10. 纸面设计练习

**需求**：为一家 5000 人的公司设计"HR 政策问答 + 请假申请"Agent。

- 员工分布在 3 个城市，部分制度（比如某些假期规定）各城市不同；
- HR 制度文档约 300 篇，每季度更新一次；
- 请假需要直属主管审批，超过 5 天还需要 HR 审批；
- 高峰期是周一上午 9-10 点，约 800 次对话，每次对话平均 4 轮；
- 员工数据（薪资、病假原因）高度敏感；
- 要求：制度问答 p95 延迟 < 5 秒，回答必须引用制度原文。

**请回答下面这些设计问题**（先自己写，再展开参考要点）：

1. 制度问答和请假申请分别用同步、流式还是异步？
2. 画出你的架构图：用到了参考架构里的哪些组件？
3. 身份和权限：谁能看谁的请假记录？模型在哪一步拿到身份？
4. 请假审批：等待期间状态放在哪里？worker 重启了怎么办？主管一天后才审批怎么办？
5. 容量估算：高峰期每秒多少次模型调用？每月大概多少 token、多少钱？限流怎么设？
6. 模型路由：问答、请假、复杂的政策解读，分别用什么档位的模型？
7. 知识库：制度更新后，怎么保证不再引用旧版本？不同城市的制度差异怎么处理？
8. 评估与可观测：上线前评估集怎么建？上线后看哪些指标？
9. 隐私：病假原因这类敏感信息，在 trace、日志、模型提供方那里分别怎么处理？
10. 发布：prompt 改动怎么灰度？出了问题怎么止血？

<details>
<summary>参考要点（展开前请先自己写一遍）</summary>

1. **交互方式**：制度问答用流式（SSE），p95 < 5 秒的要求主要靠首个事件尽快到达、减少步数来满足；请假申请用异步任务（C + B 组合），提交后进入"等待审批"状态，审批结果通过 IM 或站内信通知。
2. **架构**：网关（SSO / JWT 鉴权、限流）→ 会话服务 → worker（Agent 循环）→ 模型网关；工具服务包括"制度检索"（向量库 + 按城市过滤）、"查询假期余额"、"提交请假"（写操作，需要审批）；状态存储保存检查点和审批状态；配置中心、可观测性、评估、审计齐备。
3. **身份和权限**：身份来自 SSO，由网关注入 `ToolContext`；"查询余额"只能查本人；主管只能看直属下属的申请；HR 可以看全部，但要留审计记录；模型只能看到工具返回的、当前用户有权看到的数据。
4. **审批**：`PauseRun` 后检查点写入 Postgres；审批系统回调时，任意 worker 通过 `approve(run_id, by=审批人)` 恢复；超过 5 天的申请是两级审批，可以建模成两次暂停；设置审批超时提醒和自动过期；`submit_leave` 用 run_id + 调用 id 作为幂等键。
5. **容量估算**（假设每轮平均 2.5 次模型调用，每次输入 3000 token、输出 300 token）：高峰小时 800 × 4 = 3200 轮，约每秒 0.9 轮、2.2 次模型调用；考虑分钟级的突发（3-5 倍），按每秒约 10 次模型调用设计。按利特尔法则（并发数 = 到达率 × 平均耗时），每次调用约 4 秒，需要支撑约 40 个并发的模型调用。假设每天 3000 次对话，每月 22 个工作日：输入约 20 亿 token、输出约 2 亿 token。按本课 Demo 的示例价格，全用 `mini` 每月约 420 美元，全用 `pro` 约 9000 美元，模型路由的价值很明显。限流：每个员工每分钟若干次（防止脚本滥用），每个部门一个桶，外层一个全局桶，对齐模型提供方的配额。
6. **路由**：简单制度问答和查余额用便宜的模型；涉及多条制度交叉解读、跨城市对比的用强模型；提交请假的参数抽取用中档模型，并做严格的 schema 校验。每个会被路由到的模型都要跑一遍评估集。
7. **知识库**：文档带版本号和生效日期，更新时整篇替换并重建索引，旧版本标记为失效，检索时只取生效的版本；文档带城市标签，检索时按员工所在城市过滤（身份信息来自 ctx）；回答必须附带引用，引用要校验确实来自检索结果（第 15 课）。
8. **评估与可观测**：上线前由 HR 编写 50 条核心用例，覆盖三个城市的差异、边界（假期余额不足、跨年度请假）和对抗（冒充主管自己审批、查询别人的病假原因）；安全用例一票否决。上线后看的指标：成功率、p95 延迟、引用校验通过率、转人工率、每次对话成本、审批平均时长。
9. **隐私**：病假原因只存在业务系统里，Agent 不需要读取它（最小权限）；trace 默认不记录内容，工具参数脱敏；确认模型提供方的数据处理条款（是否用于训练、保留多久、存在哪个地区），敏感场景可以考虑私有化部署的模型。
10. **发布**：prompt 放在配置中心并做版本管理，改动先跑评估门禁，再按员工 ID 哈希灰度 5% → 25% → 100%，并盯住指标；准备 kill switch（一键关闭"提交请假"工具，退回人工流程）和一键回滚到上一个 prompt 版本（第 16 课）。
</details>

## 延伸阅读

- [Anthropic · Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)：什么时候用 workflow，什么时候用 Agent，以及"能简单就别复杂"
- [Model Context Protocol 官网](https://modelcontextprotocol.io)、[MCP 加入 Agentic AI Foundation](https://blog.modelcontextprotocol.io/posts/2025-12-09-mcp-joins-agentic-ai-foundation/)
- [A2A Protocol 官网](https://a2a-protocol.org)、[Google Cloud 将 A2A 捐给 Linux Foundation](https://developers.googleblog.com/en/google-cloud-donates-a2a-to-linux-foundation/)
- [AWS 白皮书 · SaaS Tenant Isolation Strategies](https://docs.aws.amazon.com/whitepapers/latest/saas-tenant-isolation-strategies/saas-tenant-isolation-strategies.html)：silo / pool / bridge 隔离模式
- [Stripe · Scaling your API with rate limiters](https://stripe.com/blog/rate-limiters)：令牌桶限流在生产中的实践
- [LiteLLM](https://github.com/BerriAI/litellm)：开源的 LLM 网关
- [Temporal 文档](https://docs.temporal.io)：持久化执行（durable execution）
