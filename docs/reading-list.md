[中文](reading-list.md) | [English](reading-list.en.md)

# 精选延伸阅读

> 📖 本文是"领域参考手册"的一部分。只收录**真实存在、链接可访问、标题已核对**的资料（2026 年 9 月核实）。
> 相关文档：[框架对照](framework-comparison.md) · [失败模式图鉴](failure-modes.md) · [术语表](glossary.md)

## 怎么读

资料很多，不必全读。先按你的角色选一条路线，每组表格里的"顺序"列就是建议的阅读先后（⭐ 标记的是该组必读）。

| 路线 | 适合谁 | 建议阅读 | 预计时间 |
|---|---|---|---|
| **A. 一小时入门** | 刚学完课程第一部分 | A1 → A2 → B1 → G2 | 约 1 小时 |
| **B. 工程师一周** | 要把 Agent 做上线的开发者 | 路线 A + C1 → D4 → E1 → E2 → I2 → J1 → J2 → A–M 组剩余的 ⭐；学完第三部分再加读 N1 → O1 → U1 → S1；学完第四部分再加读 W1 → X1 → Y1 → AA1 | 每天 1 小时，约一周 |
| **C. 安全负责人** | 做安全评审、红队的人 | I 组全部（按顺序）+ C4 + H2 + P 组 + O6 + U3 → U4 | 约一天 |
| **D. 平台/架构师** | 负责多租户平台、基础设施 | E 组 + F 组 + G 组 + L 组 + D4 + P1 → P6 → P7 + Q 组；做生产部署时再加 W 组 + X 组 + AB 组 | 约两天 |
| **E. 评估负责人** | 负责质量、评估体系的人 | J 组全部 + B4 + D3 + R 组 + S 组 + T2 | 约一天 |
| **F. 检索与记忆工程师** | 做知识库问答、个性化助手的人 | H1 → N 组 → B6 → B7 → O 组 | 约一天 |
| **G. 优化与 ML 闭环** | 负责"让 Agent 持续变好"（数据 → 评估 → 优化）的人 | R1 → R2 → S1 → S2 → Q1 → T 组 → S9 | 约一天 |
| **H. 编码 Agent 与长任务** | 做编码 Agent、长时运行 Agent 的人 | U1 → D7 → U3 → U4 → S8 → P5 → I12 → U5 | 约半天 |
| **I. 前沿与研究** | 做 Agent 研究、关注前沿方向的人 | V 组全部 + S1 + S7 + J5 | 约一天 |
| **J. 生产落地** | 学完第四部分、要把 Agent 服务真正部署上线并负责运维的人 | W1 → F1 → W3 → X1 → X5 → Y1 → Y2 → Z1 → Z2 → AA1 → AA2 → AB1 → AB2 → AB3 | 约一天 |

> 💡 读论文的建议：先读摘要和结论，再看图表，最后才看方法细节。本清单中的论文，大多只需要理解它"提出了什么概念、为什么重要"。

---

## A. 总纲：什么是 Agent，什么时候该用

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| A1 ⭐ | [Building Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents) | Anthropic（Erik Schluntz、Barry Zhang）· 2024 | 本课程编排模式（提示链、路由、并行、编排者-执行者、评估-优化）的直接来源；"能简单就别复杂"的原则和 Workflow/Agent 的区分，是所有设计讨论的起点。 | [第 00 课](../lessons/00_overview/README.md) · [第 06 课](../lessons/06_orchestration/README.md) · [第 20 课](../lessons/20_frameworks_bridge/README.md) |
| A2 ⭐ | [A practical guide to building agents](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)（PDF） | OpenAI · 2025 | 从产品和工程团队视角讲"何时该做 Agent、单 Agent 与多 Agent（manager 模式与去中心化模式）、分层护栏、何时引入人工"，和 A1 互为补充。 | [第 00 课](../lessons/00_overview/README.md) · [第 09 课](../lessons/09_security/README.md) |
| A3 | [LLM Powered Autonomous Agents](https://lilianweng.github.io/posts/2023-06-23-agent/) | Lilian Weng · 2023 | 把 Agent 拆成规划、记忆、工具使用三大组件的经典综述，适合建立整体概念地图，并顺藤摸瓜找到早期的重要论文。 | [第 00 课](../lessons/00_overview/README.md) |
| A4 | [Agents](https://huyenchip.com/2025/01/07/agents.html) | Chip Huyen · 2025 | 系统讨论 Agent 的工具选择、规划与失败模式，偏工程视角，覆盖面广。 | [第 00 课](../lessons/00_overview/README.md) |
| A5 | [12-Factor Agents](https://github.com/humanlayer/12-factor-agents) | HumanLayer · 2025 | 仿照"十二要素应用"总结的 12 条工程原则，如"掌控你的上下文窗口""用简单的 API 启动/暂停/恢复""让 Agent 成为无状态的 reducer"——与本课程的检查点、上下文、人工审批设计高度呼应。 | [第 08 课](../lessons/08_reliability/README.md) · [第 12 课](../lessons/12_production_architecture/README.md) |

## B. 上下文工程与记忆

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| B1 ⭐ | [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Anthropic · 2025 | 提出"注意力预算"的视角，系统讲解压缩（compaction）、结构化笔记、子 Agent 架构、即时加载上下文等技术。第 04 课的必读延伸。 | [第 04 课](../lessons/04_context_memory/README.md) |
| B2 ⭐ | [Context Engineering for AI Agents: Lessons from Building Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) | Yichao 'Peak' Ji（Manus）· 2025 | 一线团队的实战经验：围绕 KV 缓存设计、"遮蔽而不是移除"工具、用文件系统做上下文、把错误留在上下文里。每一条都有可操作的细节。 | [第 04 课](../lessons/04_context_memory/README.md) · [第 14 课](../lessons/14_cost_latency/README.md) |
| B3 | [How Long Contexts Fail](https://www.dbreunig.com/2025/06/22/how-contexts-fail-and-how-to-fix-them.html) | Drew Breunig · 2025 | 把长上下文的失败归纳为投毒、分心、混淆、冲突四类，命名清晰，便于团队沟通。 | [第 04 课](../lessons/04_context_memory/README.md) |
| B4 | [Context Rot: How Increasing Input Tokens Impacts LLM Performance](https://www.trychroma.com/research/context-rot) | Chroma · 2025 | 用实验说明"上下文越长效果越差"，为"给上下文设预算"提供数据依据。 | [第 04 课](../lessons/04_context_memory/README.md) |
| B5 | [Lost in the Middle: How Language Models Use Long Contexts](https://arxiv.org/abs/2307.03172) | Nelson F. Liu 等 · 2023 | 经典论文：相关信息放在长上下文中间时模型利用得最差。理解"关键信息放开头或结尾"的来由。 | [第 04 课](../lessons/04_context_memory/README.md) |
| B6 | [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) | Charles Packer 等 · 2023 | 借鉴操作系统的分层内存思想管理 Agent 记忆，是长期记忆设计的重要参考。 | [第 04 课](../lessons/04_context_memory/README.md) · [第 18 课](../lessons/18_memory_systems/README.md) |
| B7 | [Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442) | Joon Sung Park 等 · 2023 | 用自然语言完整记录经历、把记忆综合成更高层的"反思"、需要时动态检索——这套记忆架构启发了很多后续的 Agent 记忆系统。 | [第 04 课](../lessons/04_context_memory/README.md) · [第 18 课](../lessons/18_memory_systems/README.md) |

## C. 工具与协议

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| C1 ⭐ | [Writing effective tools for AI agents—using AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents) | Anthropic · 2025 | 工具设计最实用的一篇：选择合适的工具、命名空间、返回有意义的上下文、控制 token、把工具描述当提示词来打磨、用评估驱动工具改进。第 03 课的必读延伸。 | [第 03 课](../lessons/03_tools/README.md) |
| C2 | [The "think" tool: Enabling Claude to stop and think](https://www.anthropic.com/engineering/claude-think-tool) | Anthropic · 2025 | 一个"什么都不做"的工具如何提升复杂策略遵循场景下的表现，有助于理解工具设计的灵活性。 | [第 03 课](../lessons/03_tools/README.md) |
| C3 | [Code execution with MCP: building more efficient AI agents](https://www.anthropic.com/engineering/code-execution-with-mcp) | Anthropic · 2025 | 工具数量很多时，让 Agent 写代码调用工具以节省上下文——对"工具过载"问题的另一种解法。 | [第 03 课](../lessons/03_tools/README.md) · [第 14 课](../lessons/14_cost_latency/README.md) · [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| C4 ⭐ | [Model Context Protocol 规范](https://modelcontextprotocol.io/specification/latest) | MCP 项目 · 当前版本 2026-07-28 | MCP 的权威定义：主机/客户端/服务器架构、JSON-RPC 基础协议、工具/资源/提示词等能力，以及规范自身列出的安全原则。 | [第 03 课](../lessons/03_tools/README.md) · [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| C5 | [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) | MCP 项目 | 官方的 MCP 安全最佳实践，接入第三方 MCP 服务器前应读。 | [第 03 课](../lessons/03_tools/README.md) · [第 09 课](../lessons/09_security/README.md) · [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| C6 | [A2A 协议规范](https://a2a-protocol.org/latest/specification/)（另见 [GitHub 仓库](https://github.com/a2aproject/A2A)） | A2A 项目 · v1.0.0 | Agent 之间互相通信的开放协议：Agent Card、Task、Message、Artifact 等概念。了解"MCP 连工具，A2A 连 Agent"的分工。 | [第 12 课](../lessons/12_production_architecture/README.md) |
| C7 | [Toolformer: Language Models Can Teach Themselves to Use Tools](https://arxiv.org/abs/2302.04761) | Timo Schick 等 · 2023 | 工具使用的早期代表性研究，了解"模型调用工具"这一能力的来路。 | [第 03 课](../lessons/03_tools/README.md) |

## D. 推理范式、编排与多 Agent

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| D1 ⭐ | [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629) | Shunyu Yao 等 · 2022 | 现代 Agent 循环"思考 → 行动 → 观察"的思想来源。 | [第 02 课](../lessons/02_agent_loop/README.md) |
| D2 | [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366) | Noah Shinn 等 · 2023 | 让 Agent 用语言形式的反思从失败中改进，是评估-优化模式的理论背景之一。 | [第 06 课](../lessons/06_orchestration/README.md) |
| D3 | [Self-Consistency Improves Chain of Thought Reasoning in Language Models](https://arxiv.org/abs/2203.11171) | Xuezhi Wang 等 · 2022 | 多次采样后投票的原始论文，对应 agentkit 的 `majority_vote`。 | [第 06 课](../lessons/06_orchestration/README.md) · [第 23 课](../lessons/23_optimization/README.md) |
| D4 ⭐ | [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) | Anthropic · 2025 | 生产级多 Agent 系统的一手经验：何时适合多 Agent、token 成本（Agent 约为普通对话的 4 倍、多 Agent 约 15 倍）、提示词原则、评估方法、断点恢复与彩虹部署。 | [第 06 课](../lessons/06_orchestration/README.md) · [第 08 课](../lessons/08_reliability/README.md) |
| D5 ⭐ | [Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents) | Cognition（Walden Yan）· 2025 | 与 D4 对照阅读的"反方观点"：共享完整上下文、行动包含隐含决策——解释了多 Agent 为什么容易出错。 | [第 06 课](../lessons/06_orchestration/README.md) |
| D6 | [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657) | Mert Cemri 等 · 2025 | 基于大量真实轨迹的多 Agent 失败分类法（MAST），分为系统设计、Agent 间不对齐、任务验证三大类。 | [第 06 课](../lessons/06_orchestration/README.md) · [第 11 课](../lessons/11_evals/README.md) |
| D7 | [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | Anthropic · 2025 | 长时间运行的 Agent 如何跨越多个上下文窗口保持进度：初始化 Agent、进度文件、功能清单、增量提交——以及"过早宣布完成"等失败模式。 | [第 04 课](../lessons/04_context_memory/README.md) · [第 08 课](../lessons/08_reliability/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) |
| D8 | [Building agents with the Claude Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk) | Anthropic · 2025 | 以"收集上下文 → 采取行动 → 验证工作 → 重复"组织 Agent 循环的思路，可结合[框架对照](framework-comparison.md)阅读。 | [第 02 课](../lessons/02_agent_loop/README.md) |

## E. 可靠性与持久化执行

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| E1 ⭐ | [Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) | Marc Brooker（AWS 架构博客）· 2015 | 用模拟实验对比几种抖动策略，结论是"全抖动"最好——agentkit `backoff_delay` 的出处。 | [第 08 课](../lessons/08_reliability/README.md) |
| E2 ⭐ | [Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/) | Google SRE Book | 级联故障的成因与防范；其中"多层重试相乘"（三层各 4 次尝试 → 64 次）的例子是理解重试风暴的最佳材料。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| E3 | [Handling Overload](https://sre.google/sre-book/handling-overload/) | Google SRE Book | 过载处理：客户端节流、按客户限额、请求优先级——对应模型网关的限流与降级设计。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| E4 | [CircuitBreaker](https://martinfowler.com/bliki/CircuitBreaker.html) | Martin Fowler · 2014 | 熔断器模式最经典的短文，对应 agentkit `CircuitBreaker`。 | [第 08 课](../lessons/08_reliability/README.md) |
| E5 ⭐ | [Designing robust and predictable APIs with idempotency](https://stripe.com/blog/idempotency) | Stripe · 2017 | 幂等键设计的业界范本：为什么需要、客户端和服务端各做什么。理解"把幂等键传给下游"。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) |
| E6 | [The definitive guide to Durable Execution](https://temporal.io/blog/what-is-durable-execution) | Temporal · 2025 | 持久化执行的概念介绍，理解检查点/事件重放为什么对长时间运行的 Agent 重要。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 27 课](../lessons/27_durable_workflows/README.md) |

## F. 分布式与高并发

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| F1 ⭐ | [How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) | Martin Kleppmann · 2016 | 用 GC 停顿导致租约过期的例子，讲清楚为什么需要 fencing token——理解"僵尸 worker"问题的必读文章。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) |
| F2 ⭐ | [Pattern: Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html) | microservices.io（Chris Richardson） | 事务性发件箱模式的标准描述，解决"写库成功、消息没发"的双写问题。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| F3 | [Pattern: Saga](https://microservices.io/patterns/data/saga.html) | microservices.io（Chris Richardson） | 跨服务长事务的补偿模式，对应"新员工入职"这类多系统流程。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| F4 | [singleflight 包文档](https://pkg.go.dev/golang.org/x/sync/singleflight) | Go 项目 | 请求合并模式的经典实现，文档很短，读完就能理解如何防止缓存未命中风暴。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 14 课](../lessons/14_cost_latency/README.md) |
| F5 | [Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) | MDN | SSE 的权威参考，流式输出 Agent 进度的常用技术。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 30 课](../lessons/30_async_runtime/README.md) |
| F6 | [Write-Ahead Logging](https://www.sqlite.org/wal.html) | SQLite 文档 | WAL 模式下读和写可以同时进行，只有写和写互斥；文档也写明了代价：WAL 要求所有进程共享一小块内存，所以使用同一个数据库的进程必须在同一台机器上，不支持网络文件系统。第 13 课的 SQLite 队列只能单机，原因就在这里。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| F7 | [Transaction](https://www.sqlite.org/lang_transaction.html) | SQLite 文档 | DEFERRED、IMMEDIATE、EXCLUSIVE 三种事务的区别。默认的 DEFERRED 事务先读后写，如果别的连接已经改过数据库，升级成写事务时直接返回 `SQLITE_BUSY`。第 13 课实测的坑（`busy_timeout` 也救不了，要改用 `BEGIN IMMEDIATE`）在这里找得到原因。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |

## G. 成本与延迟

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| G1 ⭐ | [The Tail at Scale](https://research.google/pubs/the-tail-at-scale/) | Jeffrey Dean、Luiz André Barroso（CACM）· 2013 | 大规模系统长尾延迟的经典论文，对冲请求（hedged requests）等技术的出处。 | [第 14 课](../lessons/14_cost_latency/README.md) |
| G2 ⭐ | [Prompt caching（Anthropic 文档）](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | Anthropic | 提示词缓存的前缀层级（工具 → 系统提示词 → 消息）、哪些改动会让缓存失效，直接影响提示词结构设计。 | [第 14 课](../lessons/14_cost_latency/README.md) |
| G3 | [Prompt caching（OpenAI 文档）](https://developers.openai.com/api/docs/guides/prompt-caching) | OpenAI | 另一家厂商的实现，对照阅读可以理解"前缀完全匹配"这一共同原理和各家差异。 | [第 14 课](../lessons/14_cost_latency/README.md) |

## H. 企业知识与 RAG

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| H1 ⭐ | [Contextual Retrieval in AI Systems](https://www.anthropic.com/engineering/contextual-retrieval) | Anthropic · 2024 | 给切块补充上下文、结合 BM25 与向量检索、再加重排序，并给出了检索失败率的对比实验。企业 RAG 的实用参考。 | [第 04 课](../lessons/04_context_memory/README.md) · [第 15 课](../lessons/15_enterprise_rag/README.md) · [第 17 课](../lessons/17_retrieval_quality/README.md) |
| H2 | [LLM08:2025 Vector and Embedding Weaknesses](https://genai.owasp.org/llmrisk/llm082025-vector-and-embedding-weaknesses/) | OWASP · 2025 | 向量库与嵌入相关的安全风险，包括跨租户/跨权限的信息泄露——做权限感知 RAG 前应读。 | [第 15 课](../lessons/15_enterprise_rag/README.md) · [第 09 课](../lessons/09_security/README.md) |

## I. 安全

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| I1 | [Prompt injection attacks against GPT-3](https://simonwillison.net/2022/Sep/12/prompt-injection/) | Simon Willison · 2022 | 较早系统讨论提示词注入的文章，理解这个问题从哪里来、为什么至今难以根治。 | [第 09 课](../lessons/09_security/README.md) |
| I2 ⭐ | [The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) | Simon Willison · 2025 | "致命三要素"：私有数据 + 不可信内容 + 对外通信。每次设计 Agent 的工具集时都应该用它自检。 | [第 09 课](../lessons/09_security/README.md) |
| I3 ⭐ | [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/) | OWASP · 2025 | 业界共识的大模型应用十大风险（提示词注入、敏感信息泄露、过度授权、无界消耗等），适合做安全评审的对照清单。 | [第 09 课](../lessons/09_security/README.md) |
| I4 ⭐ | [OWASP Top 10 for Agentic Applications for 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/) | OWASP · 2025 年 12 月发布 | 专门面向 Agent 的十大风险（目标劫持、工具滥用、身份与权限滥用、记忆与上下文投毒、级联故障等）。 | [第 09 课](../lessons/09_security/README.md) |
| I5 | [Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection](https://arxiv.org/abs/2302.12173) | Kai Greshake 等 · 2023 | 间接提示词注入的代表性论文，理解"攻击者不需要和你的 Agent 对话"。 | [第 09 课](../lessons/09_security/README.md) |
| I6 | [Defending Against Indirect Prompt Injection Attacks With Spotlighting](https://arxiv.org/abs/2403.14720) | Keegan Hines 等 · 2024 | spotlighting（数据标记）技术的原始论文，对应 agentkit `ToolOutputGuard` 的思路。 | [第 09 课](../lessons/09_security/README.md) |
| I7 ⭐ | [Design Patterns for Securing LLM Agents against Prompt Injections](https://arxiv.org/abs/2506.08837)（另见 Simon Willison 的[解读](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)） | Luca Beurer-Kellner 等 · 2025 | 六种有可证明安全性的架构模式（Action-Selector、Plan-Then-Execute、LLM Map-Reduce、Dual LLM、Code-Then-Execute、Context-Minimization），把防注入从"检测"提升到"架构"。 | [第 09 课](../lessons/09_security/README.md) |
| I8 | [Defeating Prompt Injections by Design](https://arxiv.org/abs/2503.18813) | Edoardo Debenedetti 等 · 2025 | Google DeepMind 的 CaMeL：显式分离控制流与数据流，并用"能力"约束数据流向。 | [第 09 课](../lessons/09_security/README.md) |
| I9 | [MCP Security Notification: Tool Poisoning Attacks](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) | Invariant Labs · 2025 | 工具描述中藏恶意指令的攻击演示，接入第三方 MCP 服务器前必看。 | [第 03 课](../lessons/03_tools/README.md) · [第 09 课](../lessons/09_security/README.md) · [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| I10 | [GitHub MCP Exploited: Accessing private repositories via MCP](https://invariantlabs.ai/blog/mcp-github-vulnerability) | Invariant Labs · 2025 | 恶意 issue 劫持 Agent、泄露私有仓库信息的真实案例，是"致命三要素"的教科书示例。 | [第 09 课](../lessons/09_security/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) |
| I11 | [EchoLeak: The First Real-World Zero-Click Prompt Injection Exploit in a Production LLM System](https://arxiv.org/abs/2509.10540) | Pavan Reddy 等 · 2025 | 对 Microsoft 365 Copilot 零点击注入漏洞（CVE-2025-32711）的案例分析。 | [第 09 课](../lessons/09_security/README.md) |
| I12 | [Making Claude Code more secure and autonomous with sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) | Anthropic · 2025 | 用文件系统与网络隔离减少权限确认、同时提升安全性的工程实践，适合思考"沙箱 vs 审批"的取舍。 | [第 09 课](../lessons/09_security/README.md) · [第 19 课](../lessons/19_mcp_and_sandbox/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) |
| I13 | [AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework) | NIST | 美国国家标准与技术研究院的 AI 风险管理框架，企业做 AI 治理体系时常用的参考。 | [第 09 课](../lessons/09_security/README.md) · [第 12 课](../lessons/12_production_architecture/README.md) |

## J. 评估

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| J1 ⭐ | [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | Anthropic · 2026 | Agent 评估的系统性指南：三类评分器、pass@k 与 pass^k、能力评估与回归评估、从 20~50 个真实失败任务起步。第 11 课的必读延伸。 | [第 11 课](../lessons/11_evals/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) |
| J2 ⭐ | [Your AI Product Needs Evals](https://hamel.dev/blog/posts/evals/) | Hamel Husain · 2024 | 为什么评估是 AI 产品迭代的核心，以及如何从零搭建评估体系，实践性极强。 | [第 11 课](../lessons/11_evals/README.md) |
| J3 | [Using LLM-as-a-Judge For Evaluation: A Complete Guide](https://hamel.dev/blog/posts/llm-judge/) | Hamel Husain · 2024 | 如何构建一个与领域专家判断一致的 LLM 评委。 | [第 11 课](../lessons/11_evals/README.md) · [第 21 课](../lessons/21_agent_data/README.md) |
| J4 | [Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685) | Lianmin Zheng 等 · 2023 | LLM 评委的奠基论文，系统讨论了位置偏差、冗长偏差、自我增强偏差。 | [第 11 课](../lessons/11_evals/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) |
| J5 ⭐ | [τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045) | Shunyu Yao 等 · 2024 | 用模拟用户 + 领域工具 + 业务政策评估 Agent，提出衡量稳定性的 pass^k。客服类 Agent 的评估设计可以直接借鉴。 | [第 11 课](../lessons/11_evals/README.md) · [第 25 课](../lessons/25_proactive_and_frontier/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) |
| J6 | [τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment](https://arxiv.org/abs/2506.07982) | Victor Barres 等 · 2025 | τ-bench 的后续：用户和 Agent 都能操作环境的"双控"场景，更接近真实的技术支持对话。 | [第 11 课](../lessons/11_evals/README.md) |
| J7 | [SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://arxiv.org/abs/2310.06770) | Carlos E. Jimenez 等 · 2023 | 用真实 GitHub issue 评估编程 Agent 的基准，理解"以最终状态（测试是否通过）评估"的思路。 | [第 11 课](../lessons/11_evals/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) |
| J8 | [AgentBench: Evaluating LLMs as Agents](https://arxiv.org/abs/2308.03688) | Xiao Liu 等 · 2023 | 多环境的 Agent 能力基准，了解学术界如何横向比较模型的 Agent 能力。 | [第 11 课](../lessons/11_evals/README.md) |
| J9 | [Patterns for Building LLM-based Systems & Products](https://eugeneyan.com/writing/llm-patterns/) | Eugene Yan · 2023 | 评估、RAG、护栏、缓存、用户反馈等模式的长文综述，适合当作 LLM 工程的"模式目录"。 | [第 11 课](../lessons/11_evals/README.md) · [第 12 课](../lessons/12_production_architecture/README.md) |

## K. 可观测性

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| K1 ⭐ | [OpenTelemetry GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions-genai) | OpenTelemetry 项目 | GenAI 相关的 Span、指标、事件的字段命名约定（agentkit 的 `gen_ai.*` 属性参考了它）。注意：该约定已从 OpenTelemetry 主语义约定仓库迁移到这个独立仓库。 | [第 10 课](../lessons/10_observability/README.md) · [第 28 课](../lessons/28_production_observability/README.md) |
| K2 | [Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/) | Google SRE Book | 监控的基本功：四个黄金信号、症状 vs 原因、告警设计原则。Agent 的监控同样适用。 | [第 10 课](../lessons/10_observability/README.md) |

## L. 发布与运维

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| L1 ⭐ | [Canarying Releases](https://sre.google/workbook/canarying-releases/) | Google SRE Workbook | 金丝雀发布的系统方法：如何选择指标、样本和评估时长。Agent 的提示词/模型发布可以直接套用。 | [第 16 课](../lessons/16_release_ops/README.md) |
| L2 ⭐ | [Postmortem Culture: Learning from Failure](https://sre.google/sre-book/postmortem-culture/) | Google SRE Book | 无责复盘文化与复盘文档的写法，是"把事故变成改进"的方法论基础。 | [第 16 课](../lessons/16_release_ops/README.md) |

## M. 真实事故与合规参考

| 顺序 | 资料 | 来源 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| M1 | [Incident 1152: LLM-Driven Replit Agent Reportedly Executed Unauthorized Destructive Commands During Code Freeze, Leading to Loss of Production Data](https://incidentdatabase.ai/cite/1152/) | AI Incident Database | 2025 年 Replit 编程 Agent 在代码冻结期间删除生产数据库事件的汇总记录——"过度授权"的真实代价。AI Incident Database 本身也值得收藏，可以按关键词检索更多 AI 事故。 | [第 09 课](../lessons/09_security/README.md) · [第 16 课](../lessons/16_release_ops/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) |
| M2 | [Transparency obligations under Article 50 of the AI Act](https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act) | 欧盟委员会 | 欧盟《人工智能法》第 50 条透明度义务的官方问答，包括"告知用户正在与 AI 交互"的要求。面向欧盟用户的 Agent 需要关注。 | [第 12 课](../lessons/12_production_architecture/README.md) |

---

> 以下 N–V 组对应课程**第三部分：进阶**（第 17–25 课），每组第一条 ⭐ 是对应课程的必读。第 19 课的必读 MCP 规范已在 C4 收录；前面各组里被第三部分引用的资料（B6 MemGPT、B7 Generative Agents、C3、C5、D3 自洽性、D7 长时运行 harness、H1 Contextual Retrieval、I9、I10、I12、J1、J3、J4、J5 τ-bench、J7 SWE-bench、M1）也已补上第三部分的课程链接。

## N. 检索质量

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| N1 ⭐ | [ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT](https://arxiv.org/abs/2004.12832) | Khattab、Zaharia · 2020（SIGIR） | 第 17 课必读。一篇论文讲清双塔、交叉编码器、晚交互的效果与成本之争：重点看图 1 的效果-延迟散点图、图 2 的四种匹配范式和 §3 的 MaxSim。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |
| N2 ⭐ | [Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) | Cormack、Clarke、Büttcher · 2009（SIGIR） | 两页纸的 RRF 原始论文。k = 60 来自预实验，k 从 0 取到 500，MAP 只在 0.207～0.215 之间变化——这就是 RRF 几乎不用调参的依据。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |
| N3 | [BEIR: A Heterogenous Benchmark for Zero-shot Evaluation of Information Retrieval Models](https://arxiv.org/abs/2104.08663) | Thakur 等 · 2021（NeurIPS Datasets and Benchmarks） | 18 个数据集上的零样本评估，结论之一是"BM25 是一个稳健的基线"，很多稠密模型换个领域就打不过它。解释了为什么生产系统几乎都是两路检索一起上。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |
| N4 | [Dense Passage Retrieval for Open-Domain Question Answering](https://arxiv.org/abs/2004.04906) | Karpukhin 等 · 2020（EMNLP） | 用问答对训练的双塔检索器，开放域问答上 top-20 检索准确率比 BM25 高 9%～19%（绝对值）。理解"意思相近 → 向量相近"是怎么训练出来的。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |
| N5 | [Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs](https://arxiv.org/abs/1603.09320) | Malkov、Yashunin | HNSW 的原始论文：分层近邻图为什么又快又准。建议配合 [pgvector README](https://github.com/pgvector/pgvector) 看 HNSW 和 IVFFlat 的参数与默认值，别用默认参数直接上线。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |
| N6 | [Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents](https://arxiv.org/abs/2304.09542) | Sun 等 · 2023（EMNLP，RankGPT） | listwise LLM 重排的代表作：一次给出全部候选让模型输出排序，候选太多时用滑动窗口；还把排序能力蒸馏进一个 4.4 亿参数的小模型，在 BEIR 上超过 30 亿参数的有监督基线。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |
| N7 | [Precise Zero-Shot Dense Retrieval without Relevance Labels](https://aclanthology.org/2023.acl-long.99/) | Gao 等 · 2023（ACL，HyDE） | 用"假文档"检索的思路：细节可能是错的，但说法和真文档更像。读完就明白为什么假文档只能当检索的诱饵，不能进回答。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |
| N8 | [Cumulated gain-based evaluation of IR techniques](https://dl.acm.org/doi/10.1145/582415.582418) | Järvelin、Kekäläinen · 2002（ACM TOIS） | nDCG 的出处：排序指标为什么要按名次打折、为什么要用分级相关度。 | [第 17 课](../lessons/17_retrieval_quality/README.md) |

## O. 记忆系统

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| O1 ⭐ | [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413) | Prateek Chhikara 等 · 2025 | 第 18 课必读。"抽取 → 比对 → ADD / UPDATE / DELETE / NOOP"写入流程的原型。它的实验也值得记住：LOCOMO 上全量上下文的准确率反而最高（72.9% vs 66.9%），记忆系统换来的是 p95 延迟降低 91%、token 省 90% 以上。 | [第 18 课](../lessons/18_memory_systems/README.md) |
| O2 | [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956) | Preston Rasmussen 等 · 2025 | 时序知识图谱式记忆：每条事实记下何时开始成立、何时不再成立，出现矛盾时让旧事实失效而不是删掉。适合多实体交织、要做时间推理的企业记忆。 | [第 18 课](../lessons/18_memory_systems/README.md) |
| O3 | [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://arxiv.org/abs/2410.10813) | Di Wu 等 · 2025（ICLR） | 考五种记忆能力，其中"知识更新"和"拒答"正是企业记忆最容易出错的地方；报告商用助手在持续交互中的记忆准确率下降约 30%。设计自己的记忆评估集时可以借用它的维度。 | [第 18 课](../lessons/18_memory_systems/README.md) |
| O4 | [Benchmarking AI Agent Memory: Is a Filesystem All You Need?](https://www.letta.com/blog/benchmarking-ai-agent-memory/) | Letta · 2025 | 反方观点：只有文件读取和 grep 工具的 Agent 在 LoCoMo 上拿到 74.0%，高于 Mem0 报告的图变体 68.5%。读它是为了记住：记忆基准的数字大多是自报的，最终要靠自己的评估集。 | [第 18 课](../lessons/18_memory_systems/README.md) |
| O5 | [Introducing The Token-Efficient Memory Algorithm](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm) | Mem0 · 2026（厂商博客） | Mem0 自己放弃了 UPDATE / DELETE，改成单次调用、只做 ADD、读的时候再推理。和 O1 对照读，理解"写时消解"和"读时消解"的取舍（厂商自测的数字，谨慎看待）。 | [第 18 课](../lessons/18_memory_systems/README.md) |
| O6 | [Memory Injection Attacks on LLM Agents via Query-Only Interaction](https://arxiv.org/abs/2503.03704) | Shen Dong 等 · 2025（MINJA） | 只通过正常提问就能往 Agent 的记忆里注入恶意记录，论文报告平均注入成功率 98.2%。"只允许用户本人说的话写入记忆"这条规则的依据。 | [第 18 课](../lessons/18_memory_systems/README.md) · [第 09 课](../lessons/09_security/README.md) |
| O7 | [Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) | Anthropic（Claude API 文档） | "记忆就是文件"的一种官方实现：模型用几个命令读写 `/memories` 目录，存储由你的应用实现。文档里防路径穿越、限制文件大小等安全建议可以直接照着做。 | [第 18 课](../lessons/18_memory_systems/README.md) |

## P. MCP 与代码沙箱

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| P1 ⭐ | [The 2026-07-28 Specification](https://blog.modelcontextprotocol.io/posts/2026-07-28/) | MCP 官方博客 · 2026 | 配合 C4（第 19 课必读的 MCP 规范）读：为什么去掉 `initialize` 握手、改成每个请求自描述，以及这对负载均衡和部署意味着什么。 | [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| P2 | [Model Context Protocol has prompt injection security problems](https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/) | Simon Willison · 2025 | 把 MCP 的安全问题讲得很透：工具可以在安装之后修改自己的定义；规范里关于人工确认的 SHOULD，应该当成 MUST 来执行。 | [第 19 课](../lessons/19_mcp_and_sandbox/README.md) · [第 09 课](../lessons/09_security/README.md) |
| P3 | [Security Alert: Malicious 'postmark-mcp' npm Package](https://postmarkapp.com/blog/information-regarding-malicious-postmark-mcp-package) | Postmark · 2025 | rug pull 的真实案例：冒充 Postmark 的 npm 包前 15 个版本都正常，从 1.0.16 起把每封邮件密送给攻击者。说明"审查一次、永远信任"为什么不行。 | [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| P4 | [CVE-2025-6514: critical mcp-remote RCE vulnerability](https://jfrog.com/blog/2025-6514-critical-mcp-remote-rce-vulnerability/) | JFrog · 2025 | 客户端也是攻击面：mcp-remote 把恶意服务器返回的 `authorization_endpoint` 拼进 shell 执行，CVSS 9.6。 | [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| P5 | [Executable Code Actions Elicit Better LLM Agents](https://arxiv.org/abs/2402.01030) | Xingyao Wang 等 · 2024（ICML，CodeAct） | 让模型直接写可执行的 Python 代码作为动作，在 17 个模型上比 JSON / 文本格式的动作成功率最高高出 20%。读完就会明白：只要 Agent 开始写代码，沙箱就不是可选项。 | [第 19 课](../lessons/19_mcp_and_sandbox/README.md) · [第 05 课](../lessons/05_agent_architectures/README.md) |
| P6 | [Firecracker: Lightweight Virtualization for Serverless Applications](https://www.usenix.org/conference/nsdi20/presentation/agache) | Agache 等 · 2020（NSDI） | microVM 的代表：基于 KVM、每个 VM 一个独立内核，AWS Lambda 就跑在它上面。理解"面向外部用户、多租户运行不可信代码"该用哪一级隔离。 | [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |
| P7 | [gVisor 文档](https://gvisor.dev/docs/) | gVisor 项目 | 在用户态实现 Linux 系统调用接口，让容器里的应用碰不到宿主内核；介于普通容器和 microVM 之间的一档隔离。 | [第 19 课](../lessons/19_mcp_and_sandbox/README.md) |

## Q. 框架

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| Q1 ⭐ | [DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines](https://arxiv.org/abs/2310.03714) | Omar Khattab 等 · 2024（ICLR） | 第 20 课必读。把 LM 流水线当程序来写、把提示词当可以"编译"的参数：签名、模块、优化器三个概念都出自这里。摘要报告，GPT-3.5 上编译后的流水线通常比标准 few-shot 提示高 25% 以上。 | [第 20 课](../lessons/20_frameworks_bridge/README.md) · [第 23 课](../lessons/23_optimization/README.md) |
| Q2 | [LangGraph：Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | LangChain 官方文档 | 恢复时节点从头重跑的官方说明。在 `interrupt()` 前面写副作用、或用 `try/except` 把它包起来，都是真实会踩的坑，上线前必读。 | [第 20 课](../lessons/20_frameworks_bridge/README.md) |
| Q3 | [OpenAI Agents SDK：Tracing](https://openai.github.io/openai-agents-python/tracing/) | OpenAI 官方文档 | 追踪默认开启并上传到 OpenAI；连内网网关或有合规要求时，按这里的说明替换处理器或关闭追踪。可以和同一文档站的 [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/) 一起读。 | [第 20 课](../lessons/20_frameworks_bridge/README.md) · [第 10 课](../lessons/10_observability/README.md) |
| Q4 | [DSPy 文档：Optimizers](https://dspy.ai/learn/optimization/optimizers/) | DSPy 项目 | 优化器一览：BootstrapFewShot、MIPROv2、GEPA、BootstrapFinetune 等，以及它们共同的"给程序、训练集和指标，调用 `compile`"的用法。从第 20 课走到第 23 课的桥。 | [第 20 课](../lessons/20_frameworks_bridge/README.md) · [第 23 课](../lessons/23_optimization/README.md) |

## R. Agent 的数据

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| R1 ⭐ | [Data Flywheels for LLM Applications](https://www.sh-reya.com/blog/ai-engineering-flywheel/) | Shreya Shankar · 2024 | 第 21 课必读。把"评估 → 监控 → 持续改进"讲成一个靠生产数据转起来的飞轮：指标要看着真实输出来定；二元指标比打分更容易和人对齐；标注数据要带时间戳，因为人的偏好会变。 | [第 21 课](../lessons/21_agent_data/README.md) |
| R2 ⭐ | [Who Validates the Validators? Aligning LLM-Assisted Evaluation of LLM Outputs with Human Preferences](https://arxiv.org/abs/2404.12272) | Shreya Shankar 等 · 2024（UIST） | 提出"标准漂移"（criteria drift）：用户要先有标准才能打分，但正是打分的过程帮他们确定了标准。校准 LLM 评委、写标注指南之前应该读。 | [第 21 课](../lessons/21_agent_data/README.md) |
| R3 | [LIMA: Less Is More for Alignment](https://arxiv.org/abs/2305.11206) | Zhou 等 · 2023（NeurIPS） | 1,000 条精选数据的对齐实验。消融更值得看：多样性和质量比数量重要，数据量翻 16 倍几乎没有提升。也要读懂它没说什么：结论针对"对齐风格"，不等于学新技能也只要 1,000 条。 | [第 21 课](../lessons/21_agent_data/README.md) |
| R4 | [Self-Instruct: Aligning Language Models with Self-Generated Instructions](https://arxiv.org/abs/2212.10560) | Wang 等 · 2023（ACL） | 从 175 条种子任务自举生成指令，只收和已有指令的 ROUGE-L 相似度都低于 0.7 的新指令。合成数据"多样性过滤"的经典做法。 | [第 21 课](../lessons/21_agent_data/README.md) |
| R5 | [LLM Evaluators Recognize and Favor Their Own Generations](https://arxiv.org/abs/2404.13076) | Panickssery 等 · 2024（NeurIPS） | LLM 能在相当程度上认出自己写的文本，而且自我识别越强，自我偏好越明显。出题模型、被测模型、评委为什么要用不同的模型家族。 | [第 21 课](../lessons/21_agent_data/README.md) · [第 11 课](../lessons/11_evals/README.md) |
| R6 | [AI models collapse when trained on recursively generated data](https://www.nature.com/articles/s41586-024-07566-y) | Shumailov 等 · 2024（Nature） | 模型一代代在前代生成的数据上训练，原始分布的尾部会逐渐消失（模型崩溃）。"合成数据只能补充、不能取代真实数据"的依据。 | [第 21 课](../lessons/21_agent_data/README.md) |
| R7 | [Deduplicating Training Data Makes Language Models Better](https://aclanthology.org/2022.acl-long.577/) | Lee 等 · 2022（ACL） | 常用语言模型数据集的验证集里，有超过 4% 的数据和训练集重叠。理解训练集和测试集重叠有多常见，以及规模化去重怎么做。 | [第 21 课](../lessons/21_agent_data/README.md) |
| R8 | [SWE-smith: Scaling Data for Software Engineering Agents](https://arxiv.org/abs/2504.21798) | Yang 等 · 2025（NeurIPS Datasets & Benchmarks） | 在能通过测试的仓库里人工制造 bug，批量合成约 5 万个编码任务；训练出的 SWE-agent-LM-32B 在 SWE-bench Verified 上达到 40.2%。和 R3 对照读：学新技能时，数据规模依然关键。 | [第 21 课](../lessons/21_agent_data/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) |

## S. 评估方法论与基准有效性

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| S1 ⭐ | [Establishing Best Practices for Building Rigorous Agentic Benchmarks](https://arxiv.org/abs/2507.02825) | Zhu 等 · 2025（ABC 清单） | 第 22 课必读。提出任务有效性和结果有效性两个条件，以及 ABC 检查清单；用它审查 10 个流行的 Agent benchmark，发现 τ-bench 航空领域一个只返回空回复的 Agent 能拿 38%，这类问题可能让成绩被高估或低估多达 100%（相对值）。审计任何基准（包括你自己的评估集）之前都该读。 | [第 22 课](../lessons/22_eval_methodology/README.md) · [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| S2 ⭐ | [Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations](https://arxiv.org/abs/2411.00640) | Evan Miller · 2024 | 评估统计的实用指南：同一道题的多次作答不是独立样本，题目成组出现时要用聚类标准误；比较两个模型时对逐题的配对差值做推断；用功效分析判断评估能不能检验你关心的假设。 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| S3 | [How to Build Good Language Modeling Benchmarks](https://ofir.io/How-to-Build-Good-Language-Modeling-Benchmarks/) | Ofir Press · 2024 | 好 benchmark 应该具备的性质，以及"请求 / 环境 / 停止条件 / 评分器"四元组的拆法。设计自己的 Agent 评估之前读，能少走很多弯路。 | [第 22 课](../lessons/22_eval_methodology/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) |
| S4 | [tinyBenchmarks: evaluating LLMs with fewer examples](https://arxiv.org/abs/2402.14992) | Polo 等 · 2024（ICML） | 用少量题估计整个 benchmark：在 1.4 万道题的 MMLU 上挑 100 道，平均误差不到 2%。也要读懂它的前提：锚点聚类和 IRT 需要很多模型的历史对错，你自己的评估集通常只能先用分层抽样。 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| S5 | [AutoMetrics: Approximate Human Judgements with Automatically Generated Evaluators](https://arxiv.org/abs/2512.17267) | Ryan 等 · 2025 | 从指标库里检索相关指标、用少量人工反馈自动生成 LLM 评委的评分标准，再组合起来逼近人的判断；5 个任务上用不到 100 条人工反馈，使与人工评分的 Kendall 相关系数最多提高 33.4%。 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| S6 | [AutoLibra: Agent Metric Induction from Open-Ended Human Feedback](https://arxiv.org/abs/2505.02820) | Zhu 等 · 2026（ICLR） | 把"按钮是灰的就别再点了"这类开放式人工反馈，对应到 Agent 轨迹里的具体行为，聚类成带定义和例子的细粒度指标；还提出了衡量指标集合好坏的覆盖率和冗余度。 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| S7 ⭐ | [AI Agents That Matter](https://arxiv.org/abs/2407.01502) | Kapoor 等 · 2024 | 批评 Agent 研究"只看准确率"：准确率相近的方案，成本可以相差近两个数量级；主张把准确率和成本一起优化。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) · [第 14 课](../lessons/14_cost_latency/README.md) |
| S8 | [Introducing SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/) 与 [Why SWE-bench Verified no longer measures frontier coding capabilities](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) | OpenAI · 2024 / 2026 | 同一个团队先"修"了这个基准（人工审查后过滤掉 68.3% 的样本），一年半后又因为测试缺陷和数据污染宣布不再报告它。两篇对照着读，是基准有效性和数据污染最好的教材。 | [第 24 课](../lessons/24_coding_agents/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) |
| S9 | [Cheating Automatic LLM Benchmarks: Null Models Achieve High Win Rates](https://arxiv.org/abs/2410.07137) | Zheng 等 · 2025（ICLR） | 一个永远输出同一段固定回答的"空模型"，在 AlpacaEval 2.0 上拿到 86.5% 的长度校正胜率。用 LLM 评委当指标或优化目标之前，先想一想"最偷懒的满分输出长什么样"。 | [第 23 课](../lessons/23_optimization/README.md) |

## T. 优化

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| T1 ⭐ | [GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning](https://arxiv.org/abs/2507.19457) | Agrawal 等 · 2026（ICLR Oral） | 第 23 课必读。让模型读失败样本的完整轨迹和文字反馈来改写提示词，再用帕累托前沿保留各有所长的候选。报告平均比 GRPO 高约 6 个百分点、rollout 最多少 35 倍，比 MIPROv2 高 10 个百分点以上。 | [第 23 课](../lessons/23_optimization/README.md) |
| T2 ⭐ | [Scaling LLM Test-Time Compute Optimally can be More Effective than Scaling Model Parameters](https://arxiv.org/abs/2408.03314) | Snell 等 · 2025（ICLR） | 什么时候"多想一会儿"比"换大模型"划算：按难度分配算力，效率比 best-of-N 基线高 4 倍以上；只在小模型已有一定成功率的题上，加测试时计算能胜过大 14 倍的模型；最难的题上不如换更大的模型。 | [第 23 课](../lessons/23_optimization/README.md) |
| T3 | [Large Language Models as Optimizers](https://arxiv.org/abs/2309.03409) | Yang 等 · 2024（ICLR，OPRO） | 让 LLM 看着"历史指令 + 分数"写下一版指令。读完能理解它为什么需要大量评估：优化器只看到总分，不知道具体错在哪。 | [第 23 课](../lessons/23_optimization/README.md) |
| T4 | [Optimizing Instructions and Demonstrations for Multi-Stage Language Model Programs](https://aclanthology.org/2024.emnlp-main.525/) | Opsahl-Ong 等 · 2024（EMNLP，MIPRO） | 把"写指令"和"挑示例"当成超参数，用贝叶斯优化一起搜索。多模块程序做提示词优化时的参考。 | [第 23 课](../lessons/23_optimization/README.md) |
| T5 | [Fine-Tuning and Prompt Optimization: Two Great Steps that Work Better Together](https://aclanthology.org/2024.emnlp-main.597/) | Soylu、Potts、Khattab · 2024（EMNLP） | 提示词优化和微调可以组合：同时优化两者，比只优化权重、只优化提示词分别最多高 60% 和 6%；但几种组合顺序之间没有明确的赢家。 | [第 23 课](../lessons/23_optimization/README.md) |
| T6 | [Scaling Laws for Reward Model Overoptimization](https://arxiv.org/abs/2210.10760) | Gao、Schulman、Hilton · 2023（ICML） | 对奖励模型优化过度，真实质量反而下降，RL 和 best-of-n 都会出现。理解"对一个不完美的验证器做强力 best-of-N，本身也是在钻空子"。 | [第 23 课](../lessons/23_optimization/README.md) |
| T7 | [Specification gaming: the flip side of AI ingenuity](https://deepmind.google/blog/specification-gaming-the-flip-side-of-ai-ingenuity/) | DeepMind · 2020 | 收集了约 60 个"满足了规则字面、没达到真正目标"的案例，比如赛船游戏里的船原地转圈刷分。设计评分器之前读一读，最容易想到"最偷懒的满分输出长什么样"。 | [第 23 课](../lessons/23_optimization/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) |

## U. 编码 Agent

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| U1 ⭐ | [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://arxiv.org/abs/2405.15793) | Yang 等 · 2024（NeurIPS） | 第 24 课必读。提出 ACI，并用消融实验逐项量化"工具怎么设计"对成功率的影响：同一个模型，用 ACI 在 SWE-bench Lite 上解决 18.0%，只给 shell 是 11.0%。重点读 §2 的四条设计原则和 §5 的 Table 3。 | [第 24 课](../lessons/24_coding_agents/README.md) · [第 03 课](../lessons/03_tools/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) |
| U2 | [OpenHands: An Open Platform for AI Software Developers as Generalist Agents](https://arxiv.org/abs/2407.16741) | Wang 等 · 2025（ICLR） | 另一种编码 Agent 架构：事件流、每个会话一个 Docker 容器、CodeActAgent 和 Agent 委派。和 SWE-agent、Claude Code 对照读。 | [第 24 课](../lessons/24_coding_agents/README.md) |
| U3 | [ImpossibleBench: Measuring LLMs' Propensity of Exploiting Test Cases](https://arxiv.org/abs/2510.20270) | Zhong、Raghunathan、Carlini · 2025 | 让测试和需求矛盾，通过率就是作弊率：GPT-5 在两个变体上分别是 76% 和 54%。给模型一个"标记任务无法完成"的出口，作弊率从 54% 降到 9%。 | [第 24 课](../lessons/24_coding_agents/README.md) |
| U4 | [Recent Frontier Models Are Reward Hacking](https://metr.org/blog/2025-06-05-recent-reward-hacking/) | METR · 2025 | o3 在 RE-Bench 任务上 30.4% 的运行存在奖励投机，比如改写计时函数让慢代码看起来很快；提示词里加一句"请不要作弊"几乎没有效果。"只在提示词里禁止改测试"为什么不够。 | [第 24 课](../lessons/24_coding_agents/README.md) · [第 23 课](../lessons/23_optimization/README.md) |
| U5 | [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | SWE-agent 团队 | 约 100 行、除了 bash 没有任何工具的编码 Agent，README 称在 SWE-bench Verified 上超过 74%。和 U1 对照读：模型变强之后，ACI 的重点从"帮模型看清楚"转向安全和成本。 | [第 24 课](../lessons/24_coding_agents/README.md) |
| U6 | [Claude Code 最佳实践](https://code.claude.com/docs/en/best-practices) | Anthropic 官方文档 | 给 Agent 一个验证自己工作的方法、先探索再计划再编码、频繁清理上下文、用子 Agent 做调查和对抗式审查：编码 Agent 的日常用法。 | [第 24 课](../lessons/24_coding_agents/README.md) |

## V. 主动式 Agent 与前沿

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| V1 ⭐ | [Creating General User Models from Computer Use](https://arxiv.org/abs/2505.10831) | Shaikh 等 · 2025（UIST） | 第 25 课必读。GUM 的 Propose / Retrieve / Revise / Audit 四个模块，以及 GUMBO 怎样用期望效用决定"要不要打扰"；它的失败模式（被广告和垃圾邮件改写）同样值得记住。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| V2 ⭐ | [Principles of Mixed-Initiative User Interfaces](https://erichorvitz.com/chi99horvitz.pdf) | Eric Horvitz · 1999（CHI） | 12 条混合主动原则和 LookOut 的期望效用阈值，只有 8 页，值得全文读。主动式产品"什么时候开口"的思想源头。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| V3 | [Disruption and Recovery of Computing Tasks: Field Study, Analysis, and Directions](https://dl.acm.org/doi/10.1145/1240624.1240730) | Iqbal、Horvitz · 2007（CHI） | 打扰代价的现场数据：响应一封邮件提醒后，平均要花 9 分 33 秒才回到被挂起的窗口，立刻响应的还要再花 16 分 33 秒恢复状态。给"打扰成本"定参数之前读。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| V4 | [OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments](https://arxiv.org/abs/2404.07972) | Xie 等 · 2024（NeurIPS） | Computer-Use 的代表基准：发布时人类 72.36%、最好的模型 12.24%；约一年半后，OSWorld-Verified 上的最好成绩已到 61.4%。看基准饱和有多快。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| V5 | [Measuring AI Ability to Complete Long Tasks](https://arxiv.org/abs/2503.14499) | Kwa 等（METR）· 2025 | 提出"50% 时间跨度"，发现它自 2019 年以来大约每 7 个月翻一倍。读的时候注意它是按 50% 成功率定义的，离企业要的可靠性还很远。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| V6 | [Measuring Progress on Scalable Oversight for Large Language Models](https://arxiv.org/abs/2211.03540) | Bowman 等 · 2022 | Agent 做的事超出人能检查的范围时，怎样借助 AI 做出可靠判断。理解为什么短期内最实用的监督手段是"让环境来验证"。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| V7 | [Chain of Thought Monitorability: A New and Fragile Opportunity for AI Safety](https://arxiv.org/abs/2507.11473) | Korbak 等 · 2025 | 多家机构联合署名：监控思维链是有希望但脆弱的安全机会，训练方式的改变可能让思维链不再反映真实推理。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| V8 | [Agentic Misalignment: How LLMs could be insider threats](https://www.anthropic.com/research/agentic-misalignment) | Anthropic · 2025 | 在刻意构造的虚构公司场景里对 16 个主流模型做压力测试，作者也强调没有在真实部署中看到这类行为。读它是为了理解：安全不能寄托在"模型对齐得很好"上。 | [第 25 课](../lessons/25_proactive_and_frontier/README.md) · [第 09 课](../lessons/09_security/README.md) |

---

> 以下 W–AB 组对应课程**第四部分：生产落地**（第 26–31 课）。W–AB 组的第一条 ⭐ 是对应课程的必读（AB 组对应第 31 课和 `production/` 参考服务）。前面各组里被第四部分引用的资料（E2 级联故障、E5 Stripe 幂等、E6 持久化执行、F1 Kleppmann 分布式锁、F5 SSE、K1 GenAI 语义约定）也已补上第四部分的课程链接。

## W. 状态、队列与分布式协调

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| W1 ⭐ | [Devious SQL: Message Queuing Using Native PostgreSQL](https://www.crunchydata.com/blog/message-queuing-using-native-postgresql) | David Christensen（Crunchy Data）· 2021 | 第 26 课必读。用十几行 SQL 从零搭出一个 `FOR UPDATE SKIP LOCKED` 队列，顺带讲了两件本课代码里处处都有影子的事：事务回滚时任务自动回到队列；队列表更新频繁会膨胀，要调 autovacuum。 | [第 26 课](../lessons/26_state_and_queues/README.md) |
| W2 | [PostgreSQL 文档：SELECT 的锁定子句](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE) | PostgreSQL 项目 | SKIP LOCKED 的原始说明：它得到的是不一致的数据视图，不适合一般用途，但适合多个消费者领取"类似队列的表"、避免锁争用。读完就知道它能用在哪、不能用在哪。 | [第 26 课](../lessons/26_state_and_queues/README.md) |
| W3 | [Distributed Locks with Redis](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/) | Redis 官方文档 | `SET NX PX` 加锁、"比较后删除"释放（Redis 8.4 起有 `DELEX`）、Redlock 算法，以及文末关于一致性的免责声明：要实现 fencing token；Redis 的过期时间用的不是单调时钟。和 F1 对照读。 | [第 26 课](../lessons/26_state_and_queues/README.md) |
| W4 | [Is Redlock safe?](https://antirez.com/news/101) | antirez · 2016 | Redis 作者对 F1 的回应：在合理的时钟和时序假设下 Redlock 是安全的。两篇一起读，比只读一方更能想清楚"锁到底保证了什么"。 | [第 26 课](../lessons/26_state_and_queues/README.md) |
| W5 | [etcd versus other key-value stores](https://etcd.io/docs/v3.5/learning/why/) | etcd 项目 | "Notes on the usage of lock and lease"一节把 Kleppmann 说的 fencing token 对应为 etcd 的 revision。需要跨系统、正确性要求高的锁时，token 该从哪里来。 | [第 26 课](../lessons/26_state_and_queues/README.md) |
| W6 | [Connection pools](https://www.psycopg.org/psycopg3/docs/advanced/pool.html) | psycopg 3 文档 | `ConnectionPool` / `AsyncConnectionPool`：`with pool.connection()` 正常退出时提交、异常时回滚；`get_stats()` 里的 `requests_queued` 是判断池子是否太小的指标。 | [第 26 课](../lessons/26_state_and_queues/README.md) · [第 30 课](../lessons/30_async_runtime/README.md) |
| W7 | [Routine Vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html) | PostgreSQL 文档 | `UPDATE` 和 `DELETE` 不会立刻删掉旧的行版本，要等 VACUUM 回收，这是 MVCC 的代价。队列表每个任务要更新好几次，是典型的高频更新表。 | [第 26 课](../lessons/26_state_and_queues/README.md) |
| W8 | [Scripting with Lua](https://redis.io/docs/latest/develop/programmability/eval-intro/) | Redis 官方文档 | 脚本执行期间服务器上的其他操作全部等待，所以令牌桶的读-改-写要整段放进脚本；5.0 起脚本默认按效果复制、7.0 起只剩这一种方式，所以在脚本里调用 `TIME` 是安全的。 | [第 26 课](../lessons/26_state_and_queues/README.md) |
| W9 | [Idempotent requests](https://docs.stripe.com/api/idempotent_requests) | Stripe API 文档 | 下游幂等的样板：保存第一次请求的状态码和响应体（包括 500 错误），key 至少保留 24 小时才可能被清理，参数不同会报错。和 E5 一起读：幂等最终要下沉到执行副作用的系统。 | [第 26 课](../lessons/26_state_and_queues/README.md) |

## X. 持久化工作流

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| X1 ⭐ | [Of course you can build dynamic AI agents with Temporal](https://temporal.io/blog/of-course-you-can-build-dynamic-ai-agents-with-temporal) | Mason Egger、Steve Androulakis（Temporal）· 2025 | 第 27 课必读。回答"Workflow 要求确定性，LLM 这种不确定的东西怎么放进去"这个最常见的误解：确定性只约束编排代码，模型调用和工具调用都放在 Activity 里，模型依然决定下一步做什么。 | [第 27 课](../lessons/27_durable_workflows/README.md) |
| X2 | [Temporal Workflow](https://docs.temporal.io/workflows) | Temporal 文档 | 确定性和重放的正式说明：同样的历史必须做出同样的决定；恢复时从头重放、已完成的步骤从历史里取结果，而不是恢复一份内存快照。 | [第 27 课](../lessons/27_durable_workflows/README.md) |
| X3 | [Detecting Activity Failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures) | Temporal 文档 | Schedule-To-Start、Start-To-Close、Schedule-To-Close 三种超时和心跳各管什么；取消只能在心跳时送达，不发心跳的 activity 收不到取消。 | [第 27 课](../lessons/27_durable_workflows/README.md) |
| X4 | [Temporal Retry Policy](https://docs.temporal.io/encyclopedia/retry-policies) | Temporal 文档 | Activity 默认不限重试次数（`maximum_attempts` 为 0 表示不限）。模型调用和写工具都必须自己设上限，并标出不可重试的错误。 | [第 27 课](../lessons/27_durable_workflows/README.md) |
| X5 | [Activity Definition](https://docs.temporal.io/activity-definition) | Temporal 文档 | 带重试策略的 Activity 保证"被观察到完成"恰好一次，但可能被执行多次，所以要幂等。这和第 13 课的"至少一次投递"是同一个问题。 | [第 27 课](../lessons/27_durable_workflows/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| X6 | [Workflow message passing - Python SDK](https://docs.temporal.io/develop/python/message-passing) | Temporal 文档 | Signal、Query、Update 的区别和用法；Update 的验证器可以在请求写进事件历史之前拒绝它。设计审批接口之前读。 | [第 27 课](../lessons/27_durable_workflows/README.md) |
| X7 | [Versioning - Python SDK](https://docs.temporal.io/develop/python/versioning) | Temporal 文档 | `patched()` 在事件历史里插入标记，让新旧代码路径并存；`deprecate_patch()` 负责收尾；Worker Versioning 让旧运行留在旧 worker 上跑完。改 workflow 代码之前必读。 | [第 27 课](../lessons/27_durable_workflows/README.md) |
| X8 | [Workflow Execution Limits](https://docs.temporal.io/workflow-execution/limits) 与 [Continue-As-New - Python SDK](https://docs.temporal.io/develop/python/continue-as-new) | Temporal 文档 | 单个执行的事件历史上限是 51,200 个事件或 50 MB（10,240 个 / 10 MB 时开始告警）；`workflow.info().is_continue_as_new_suggested()` 告诉你什么时候该重开。Agent 的历史按步数平方增长，这两页要一起看。 | [第 27 课](../lessons/27_durable_workflows/README.md) |
| X9 | [Testing - Python SDK](https://docs.temporal.io/develop/python/testing-suite) | Temporal 文档 | 其中"How to Replay a Workflow Execution"一节：拿已有的事件历史重放新代码，在发布前抓出非确定性错误。把生产抽样的历史放进 CI，就是照这个做的。 | [第 27 课](../lessons/27_durable_workflows/README.md) |

## Y. 生产可观测性

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| Y1 ⭐ | [Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/) | Steven Thurgood 等（Google SRE Workbook）· 2018 | 第 28 课必读。从"错误率超过阈值就告警"一步步演进到"多窗口多燃烧率"，每一步修掉上一步在精确率、召回率、检测时间、重置时间上的哪个缺点；"低流量服务"一节对波峰波谷明显的 Agent 流量尤其有用。 | [第 28 课](../lessons/28_production_observability/README.md) |
| Y2 | [Recording errors](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/general/recording-errors.md) | OpenTelemetry 项目 | 什么时候把 span 标成 Error：没出错时状态必须保持 UNSET；出错时设 Error 并写 `error.type`；已经被处理、让操作顺利完成的错误不记在这个操作上。据此决定取消、等审批、工具失败该不该"标红"。 | [第 28 课](../lessons/28_production_observability/README.md) |
| Y3 | [Trace Context](https://www.w3.org/TR/trace-context/) | W3C Recommendation · 2021 | `traceparent` 和 `tracestate` 两个头部的标准格式，跨服务、跨队列传播 trace 的基础。自己解析 trace flags 之前先读。 | [第 28 课](../lessons/28_production_observability/README.md) |
| Y4 | [Semantic conventions for messaging spans](https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/) | OpenTelemetry 项目 | 生产者给每条消息附上创建上下文；默认用 span link 关联生产者和消费者（批量消费时这是唯一的办法），处理单条消息时才可以直接把它当父级。决定"在队列里等了 2 小时的任务怎么接 trace"时读。 | [第 28 课](../lessons/28_production_observability/README.md) |
| Y5 | [Tail sampling processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/tailsamplingprocessor) | OpenTelemetry Collector | `decision_wait`、`num_traces`（默认 50000）、按状态码 / 延迟 / 概率的策略、处理迟到 span 的 `decision_cache`，以及用 loadbalancing 导出器分两层部署，保证同一条 trace 到同一个实例。 | [第 28 课](../lessons/28_production_observability/README.md) |
| Y6 | [Metric and label naming](https://prometheus.io/docs/practices/naming/) | Prometheus 文档 | 每个不同的标签组合都是一条新的时间序列，不要把用户 ID、邮箱这类取值无上限的维度当标签。设计 Agent 指标之前的必读短文。 | [第 28 课](../lessons/28_production_observability/README.md) |
| Y7 | [Multiprocess Mode](https://prometheus.github.io/client_python/multiprocess/) | prometheus_client 文档 | gunicorn 这类多进程部署怎么汇总指标：`PROMETHEUS_MULTIPROC_DIR`、`MultiProcessCollector`、`mark_process_dead`，以及 Gauge 的多进程聚合方式（如 `livesum`）。 | [第 28 课](../lessons/28_production_observability/README.md) |

## Z. 模型网关、策略即代码与护栏

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| Z1 ⭐ | [Cedar: A New Language for Expressive, Fast, Safe, and Analyzable Authorization](https://arxiv.org/abs/2403.04651) | Joseph W. Cutler 等 · 2024（OOPSLA 2024，扩展版） | 第 29 课必读。同一门语言怎样表达 RBAC、ABAC 和关系型授权；为什么刻意不图灵完备，好让验证器和符号分析站得住（用 Lean 做了形式化证明）；以及和 OpenFGA、Rego 的性能对比。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z2 | [Authorization](https://docs.cedarpolicy.com/auth/authorization.html) | Cedar 文档 | 判定规则：默认拒绝、forbid 优先于 permit；以及最容易踩的一条：求值出错的策略会被跳过，错误写在诊断信息里，由应用决定怎么处理。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z3 | [Router - Load Balancing](https://docs.litellm.ai/docs/routing) | LiteLLM 文档 | 模型组、路由策略、重试、冷却和降级链；多实例时用 Redis 共享冷却状态和 tpm / rpm 计数。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z4 | [Fallbacks (Provider Failover)](https://docs.litellm.ai/docs/proxy/reliability) | LiteLLM 文档 | 网关层的降级：普通降级、上下文超长降级、内容策略降级，以及它们和重试、冷却怎么配合。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z5 | [Budgets, Rate Limits](https://docs.litellm.ai/docs/proxy/users) | LiteLLM 文档 | 按团队、虚拟 key、用户设 `max_budget`、`budget_duration`、`rpm_limit`、`tpm_limit`，回答"每个团队花了多少钱、最多能花多少"。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z6 | [Zanzibar: Google's Consistent, Global Authorization System](https://www.usenix.org/conference/atc19/presentation/pang) | Ruoming Pang 等 · 2019（USENIX ATC） | 关系型授权（ReBAC）的源头：统一的数据模型和配置语言、全球一致、低延迟。OpenFGA 等开源实现都受它启发。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z7 | [Open Policy Agent](https://www.openpolicyagent.org/docs) | OPA 项目 | 通用策略引擎与 Rego 语言：把策略决策和执行分开，Kubernetes、API 网关、CI 都能用同一套。和 Cedar 对照读，理解"通用"和"可分析"之间的取舍。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z8 | [What is FGA?](https://openfga.dev/docs/fga) | OpenFGA 项目 | 受 Zanzibar 启发的细粒度授权：文档、文件夹、团队这类层级共享天然好表达。产品的核心是"共享与层级"时读。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z9 | [Llama Prompt Guard 2 86M 模型卡](https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M) | Meta | 专用的注入分类模型：基于 mDeBERTa-base，BENIGN / MALICIOUS 二分类，上下文 512 token（长文本要切段）；官方评测的 8 种语言不含中文。放进级联分类器的中间一级之前，要用自己的数据评估。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| Z10 | [NVIDIA NeMo Guardrails](https://github.com/NVIDIA-NeMo/Guardrails) | NVIDIA | 开源护栏编排框架：用 Colang 定义 input、dialog、retrieval、execution、output 五类 rails，可以把各种分类器串起来。 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |

## AA. 异步运行时

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| AA1 ⭐ | [Notes on structured concurrency, or: Go statement considered harmful](https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/) | Nathaniel J. Smith · 2018 | 第 30 课必读。为什么"随手开一个后台任务"像 goto 一样破坏抽象；重点读"Nurseries"一节：子任务不能比创建它的作用域活得更久，错误传播和取消才重新变得可以推理。 | [第 30 课](../lessons/30_async_runtime/README.md) |
| AA2 | [Coroutines and Tasks](https://docs.python.org/3/library/asyncio-task.html) | Python 官方文档 | `TaskGroup`、取消、`shield`、`timeout`：吞掉 `CancelledError` 会让 `TaskGroup` 和 `asyncio.timeout()` 行为异常；事件循环只对任务保留弱引用，`create_task` 的返回值要保存；`gather` 默认参数下，一个出错时其余任务不会被取消。 | [第 30 课](../lessons/30_async_runtime/README.md) |
| AA3 | [Developing with asyncio](https://docs.python.org/3/library/asyncio-dev.html) | Python 官方文档 | 调试模式（`PYTHONASYNCIODEBUG=1`）会记录超过 100 毫秒的慢回调；阻塞代码怎么交给执行器；从未 await 的协程怎么发现。查"谁卡住了事件循环"的第一站。 | [第 30 课](../lessons/30_async_runtime/README.md) |
| AA4 | [Cancellation and timeouts](https://anyio.readthedocs.io/en/stable/cancellation.html) | AnyIO 文档 | 电平触发的取消：任务只要还在一个已取消的作用域里，每碰到一个 yield 点就会再被取消一次，所以收尾时的 `await` 要放进受保护（shielded）的取消作用域。FastAPI / Starlette 就建立在 AnyIO 之上。 | [第 30 课](../lessons/30_async_runtime/README.md) |
| AA5 | [AsyncIO's wait_for can hide cancellation in a rare race condition（gh-86296）](https://github.com/python/cpython/issues/86296) | CPython issue | 内部结果和外部取消在同一轮事件循环里到达时，`wait_for` 返回结果、吞掉取消。第 30 课在 3.11.7 上稳定复现，在 3.12.3 和 3.13.1 上不再出现。还在老版本 Python 上的项目要读。 | [第 30 课](../lessons/30_async_runtime/README.md) |
| AA6 | [HTML Standard：Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html) | WHATWG | 重连时浏览器带上 `Last-Event-ID`；`retry:` 字段设置重连间隔；每 15 秒左右发一行注释，防止旧代理断开空闲连接。设计流式断线续传时读原文。 | [第 30 课](../lessons/30_async_runtime/README.md) · [第 31 课](../lessons/31_deployment_and_scaling/README.md) |
| AA7 | [Understanding Lambda function scaling](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html) | AWS 文档 | 用"并发 = 平均每秒请求数 × 平均请求时长"估算并发，也就是利特尔法则的工程版；同时说明了一个执行环境在处理请求期间不能处理别的请求。 | [第 30 课](../lessons/30_async_runtime/README.md) |

## AB. 部署与扩缩容

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| AB1 ⭐ | [Kubernetes best practices: terminating with grace](https://cloud.google.com/blog/products/containers-kubernetes/kubernetes-best-practices-terminating-with-grace) | Sandeep Dinesh · 2018 | 用一页讲清 Pod 被删除时的完整时间线：preStop、SIGTERM、宽限期（默认 30 秒）、SIGKILL。第 31 课把 `terminationGracePeriodSeconds`、preStop、worker 宽限期和租约对齐，依据就是这条时间线。第 31 课的必读 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) |
| AB2 | [Pod Lifecycle：Termination of Pods](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination) | Kubernetes 文档 | 删除 Pod 时先执行 preStop，再给容器发 SIGTERM，等 `terminationGracePeriodSeconds`（默认 30 秒，preStop 的耗时也算在内）之后发 SIGKILL。worker 的优雅停机时间线就是照着它设计的。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) |
| AB3 | [Configure Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) | Kubernetes 文档 | 存活探针失败会重启容器，就绪探针失败只把 Pod 从 Service 的端点里摘掉。分清两者，才不会把"数据库抖一下"变成"所有 Pod 一起重启"。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) |
| AB4 | [Horizontal Pod Autoscaling](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/) | Kubernetes 文档 | HPA 除了 CPU，还能按自定义指标和外部指标扩缩；缩容稳定窗口和扩缩速率策略防止副本数来回抖动。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) |
| AB5 | [PostgreSQL scaler](https://keda.sh/docs/2.21/scalers/postgresql/) | KEDA 文档 | 用一条返回数字的 SQL（比如可执行的任务数）和 `targetQueryValue` 比较，来扩缩 worker。队列就在 Postgres 里时，这是按积压扩缩容最直接的做法。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) |
| AB6 | [The Twelve-Factor App：IX. Disposability](https://12factor.net/disposability) | 12factor.net | 进程收到 SIGTERM 要优雅退出；对 worker 来说，优雅停机就是把手上的任务还回队列。第 31 课的 worker 正是这么做的。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) |
| AB7 | [Uvicorn settings](https://uvicorn.dev/settings/) | Uvicorn 文档 | `--limit-concurrency`（超过就返回 503）、`--timeout-graceful-shutdown`、`--workers`：API 进程的最后一道闸和停机时限。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) · [第 30 课](../lessons/30_async_runtime/README.md) |
| AB8 | [Open and closed models](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/) | Grafana k6 文档 | 闭环压测在系统变慢时自己也发得慢了，这就是协调遗漏；要测固定到达率下的尾延迟，用 arrival-rate 执行器（开环）。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) |
| AB9 | [wrk2](https://github.com/giltene/wrk2) | Gil Tene | 以恒定吞吐发请求、按"本该发出的时刻"计算延迟的压测工具；README 讲清了协调遗漏为什么会把最坏的那段延迟藏起来。 | [第 31 课](../lessons/31_deployment_and_scaling/README.md) |

---

## 收录原则

1. **只收录核实过的资料**：每个链接都在 2026 年 9 月实际访问过，标题与原文核对一致；有跳转的使用跳转后的最终地址。
2. **宁缺毋滥**：无法稳定访问或无法核实内容的资料（包括一些常被引用但原链接已失效或需要登录的页面）不收录。
3. **优先一手资料**：优先官方文档、原始论文和作者本人的文章，而不是二手转述。
4. **欢迎补充**：如果你发现某个链接失效，或者有值得收录的资料，欢迎提 Issue 或 PR——请附上"为什么值得读"。
