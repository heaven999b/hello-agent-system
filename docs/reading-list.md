[中文](reading-list.md) | [English](reading-list.en.md)

# 精选延伸阅读

> 📖 本文是"领域参考手册"的一部分。只收录**真实存在、链接可访问、标题已核对**的资料（2026 年 9 月核实）。
> 相关文档：[框架对照](framework-comparison.md) · [失败模式图鉴](failure-modes.md) · [术语表](glossary.md)

## 怎么读

资料很多，不必全读。先按你的角色选一条路线，每组表格里的"顺序"列就是建议的阅读先后（⭐ 标记的是该组必读）。

| 路线 | 适合谁 | 建议阅读 | 预计时间 |
|---|---|---|---|
| **A. 一小时入门** | 刚学完课程第一部分 | A1 → A2 → B1 → G2 | 约 1 小时 |
| **B. 工程师一周** | 要把 Agent 做上线的开发者 | 路线 A + C1 → D4 → E1 → E2 → I2 → J1 → J2 → 各组剩余的 ⭐ | 每天 1 小时，约一周 |
| **C. 安全负责人** | 做安全评审、红队的人 | I 组全部（按顺序）+ C4 + H2 | 约半天 |
| **D. 平台/架构师** | 负责多租户平台、基础设施 | E 组 + F 组 + G 组 + L 组 + D4 | 约一天 |
| **E. 评估负责人** | 负责质量、评估体系的人 | J 组全部 + B4 + D3 | 约半天 |

> 💡 读论文的建议：先读摘要和结论，再看图表，最后才看方法细节。本清单中的论文，大多只需要理解它"提出了什么概念、为什么重要"。

---

## A. 总纲：什么是 Agent，什么时候该用

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| A1 ⭐ | [Building Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents) | Anthropic（Erik Schluntz、Barry Zhang）· 2024 | 本课程编排模式（提示链、路由、并行、编排者-执行者、评估-优化）的直接来源；"能简单就别复杂"的原则和 Workflow/Agent 的区分，是所有设计讨论的起点。 | [第 00 课](../lessons/00_overview/README.md) · [第 06 课](../lessons/06_orchestration/README.md) |
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
| B6 | [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) | Charles Packer 等 · 2023 | 借鉴操作系统的分层内存思想管理 Agent 记忆，是长期记忆设计的重要参考。 | [第 04 课](../lessons/04_context_memory/README.md) |
| B7 | [Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442) | Joon Sung Park 等 · 2023 | 用自然语言完整记录经历、把记忆综合成更高层的"反思"、需要时动态检索——这套记忆架构启发了很多后续的 Agent 记忆系统。 | [第 04 课](../lessons/04_context_memory/README.md) |

## C. 工具与协议

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| C1 ⭐ | [Writing effective tools for AI agents—using AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents) | Anthropic · 2025 | 工具设计最实用的一篇：选择合适的工具、命名空间、返回有意义的上下文、控制 token、把工具描述当提示词来打磨、用评估驱动工具改进。第 03 课的必读延伸。 | [第 03 课](../lessons/03_tools/README.md) |
| C2 | [The "think" tool: Enabling Claude to stop and think](https://www.anthropic.com/engineering/claude-think-tool) | Anthropic · 2025 | 一个"什么都不做"的工具如何提升复杂策略遵循场景下的表现，有助于理解工具设计的灵活性。 | [第 03 课](../lessons/03_tools/README.md) |
| C3 | [Code execution with MCP: building more efficient AI agents](https://www.anthropic.com/engineering/code-execution-with-mcp) | Anthropic · 2025 | 工具数量很多时，让 Agent 写代码调用工具以节省上下文——对"工具过载"问题的另一种解法。 | [第 03 课](../lessons/03_tools/README.md) · [第 14 课](../lessons/14_cost_latency/README.md) |
| C4 ⭐ | [Model Context Protocol 规范](https://modelcontextprotocol.io/specification/latest) | MCP 项目 · 当前版本 2026-07-28 | MCP 的权威定义：主机/客户端/服务器架构、JSON-RPC 基础协议、工具/资源/提示词等能力，以及规范自身列出的安全原则。 | [第 03 课](../lessons/03_tools/README.md) |
| C5 | [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) | MCP 项目 | 官方的 MCP 安全最佳实践，接入第三方 MCP 服务器前应读。 | [第 03 课](../lessons/03_tools/README.md) · [第 09 课](../lessons/09_security/README.md) |
| C6 | [A2A 协议规范](https://a2a-protocol.org/latest/specification/)（另见 [GitHub 仓库](https://github.com/a2aproject/A2A)） | A2A 项目 · v1.0.0 | Agent 之间互相通信的开放协议：Agent Card、Task、Message、Artifact 等概念。了解"MCP 连工具，A2A 连 Agent"的分工。 | [第 12 课](../lessons/12_production_architecture/README.md) |
| C7 | [Toolformer: Language Models Can Teach Themselves to Use Tools](https://arxiv.org/abs/2302.04761) | Timo Schick 等 · 2023 | 工具使用的早期代表性研究，了解"模型调用工具"这一能力的来路。 | [第 03 课](../lessons/03_tools/README.md) |

## D. 推理范式、编排与多 Agent

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| D1 ⭐ | [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629) | Shunyu Yao 等 · 2022 | 现代 Agent 循环"思考 → 行动 → 观察"的思想来源。 | [第 02 课](../lessons/02_agent_loop/README.md) |
| D2 | [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366) | Noah Shinn 等 · 2023 | 让 Agent 用语言形式的反思从失败中改进，是评估-优化模式的理论背景之一。 | [第 06 课](../lessons/06_orchestration/README.md) |
| D3 | [Self-Consistency Improves Chain of Thought Reasoning in Language Models](https://arxiv.org/abs/2203.11171) | Xuezhi Wang 等 · 2022 | 多次采样后投票的原始论文，对应 agentkit 的 `majority_vote`。 | [第 06 课](../lessons/06_orchestration/README.md) |
| D4 ⭐ | [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) | Anthropic · 2025 | 生产级多 Agent 系统的一手经验：何时适合多 Agent、token 成本（Agent 约为普通对话的 4 倍、多 Agent 约 15 倍）、提示词原则、评估方法、断点恢复与彩虹部署。 | [第 06 课](../lessons/06_orchestration/README.md) · [第 08 课](../lessons/08_reliability/README.md) |
| D5 ⭐ | [Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents) | Cognition（Walden Yan）· 2025 | 与 D4 对照阅读的"反方观点"：共享完整上下文、行动包含隐含决策——解释了多 Agent 为什么容易出错。 | [第 06 课](../lessons/06_orchestration/README.md) |
| D6 | [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657) | Mert Cemri 等 · 2025 | 基于大量真实轨迹的多 Agent 失败分类法（MAST），分为系统设计、Agent 间不对齐、任务验证三大类。 | [第 06 课](../lessons/06_orchestration/README.md) · [第 11 课](../lessons/11_evals/README.md) |
| D7 | [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | Anthropic · 2025 | 长时间运行的 Agent 如何跨越多个上下文窗口保持进度：初始化 Agent、进度文件、功能清单、增量提交——以及"过早宣布完成"等失败模式。 | [第 04 课](../lessons/04_context_memory/README.md) · [第 08 课](../lessons/08_reliability/README.md) |
| D8 | [Building agents with the Claude Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk) | Anthropic · 2025 | 以"收集上下文 → 采取行动 → 验证工作 → 重复"组织 Agent 循环的思路，可结合[框架对照](framework-comparison.md)阅读。 | [第 02 课](../lessons/02_agent_loop/README.md) |

## E. 可靠性与持久化执行

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| E1 ⭐ | [Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) | Marc Brooker（AWS 架构博客）· 2015 | 用模拟实验对比几种抖动策略，结论是"全抖动"最好——agentkit `backoff_delay` 的出处。 | [第 08 课](../lessons/08_reliability/README.md) |
| E2 ⭐ | [Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/) | Google SRE Book | 级联故障的成因与防范；其中"多层重试相乘"（三层各 4 次尝试 → 64 次）的例子是理解重试风暴的最佳材料。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| E3 | [Handling Overload](https://sre.google/sre-book/handling-overload/) | Google SRE Book | 过载处理：客户端节流、按客户限额、请求优先级——对应模型网关的限流与降级设计。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| E4 | [CircuitBreaker](https://martinfowler.com/bliki/CircuitBreaker.html) | Martin Fowler · 2014 | 熔断器模式最经典的短文，对应 agentkit `CircuitBreaker`。 | [第 08 课](../lessons/08_reliability/README.md) |
| E5 ⭐ | [Designing robust and predictable APIs with idempotency](https://stripe.com/blog/idempotency) | Stripe · 2017 | 幂等键设计的业界范本：为什么需要、客户端和服务端各做什么。理解"把幂等键传给下游"。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| E6 | [The definitive guide to Durable Execution](https://temporal.io/blog/what-is-durable-execution) | Temporal · 2025 | 持久化执行的概念介绍，理解检查点/事件重放为什么对长时间运行的 Agent 重要。 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) |

## F. 分布式与高并发

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| F1 ⭐ | [How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) | Martin Kleppmann · 2016 | 用 GC 停顿导致租约过期的例子，讲清楚为什么需要 fencing token——理解"僵尸 worker"问题的必读文章。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| F2 ⭐ | [Pattern: Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html) | microservices.io（Chris Richardson） | 事务性发件箱模式的标准描述，解决"写库成功、消息没发"的双写问题。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| F3 | [Pattern: Saga](https://microservices.io/patterns/data/saga.html) | microservices.io（Chris Richardson） | 跨服务长事务的补偿模式，对应"新员工入职"这类多系统流程。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| F4 | [singleflight 包文档](https://pkg.go.dev/golang.org/x/sync/singleflight) | Go 项目 | 请求合并模式的经典实现，文档很短，读完就能理解如何防止缓存未命中风暴。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 14 课](../lessons/14_cost_latency/README.md) |
| F5 | [Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) | MDN | SSE 的权威参考，流式输出 Agent 进度的常用技术。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |

## G. 成本与延迟

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| G1 ⭐ | [The Tail at Scale](https://research.google/pubs/the-tail-at-scale/) | Jeffrey Dean、Luiz André Barroso（CACM）· 2013 | 大规模系统长尾延迟的经典论文，对冲请求（hedged requests）等技术的出处。 | [第 14 课](../lessons/14_cost_latency/README.md) |
| G2 ⭐ | [Prompt caching（Anthropic 文档）](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | Anthropic | 提示词缓存的前缀层级（工具 → 系统提示词 → 消息）、哪些改动会让缓存失效，直接影响提示词结构设计。 | [第 14 课](../lessons/14_cost_latency/README.md) |
| G3 | [Prompt caching（OpenAI 文档）](https://developers.openai.com/api/docs/guides/prompt-caching) | OpenAI | 另一家厂商的实现，对照阅读可以理解"前缀完全匹配"这一共同原理和各家差异。 | [第 14 课](../lessons/14_cost_latency/README.md) |

## H. 企业知识与 RAG

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| H1 ⭐ | [Contextual Retrieval in AI Systems](https://www.anthropic.com/engineering/contextual-retrieval) | Anthropic · 2024 | 给切块补充上下文、结合 BM25 与向量检索、再加重排序，并给出了检索失败率的对比实验。企业 RAG 的实用参考。 | [第 04 课](../lessons/04_context_memory/README.md) · [第 15 课](../lessons/15_enterprise_rag/README.md) |
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
| I9 | [MCP Security Notification: Tool Poisoning Attacks](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) | Invariant Labs · 2025 | 工具描述中藏恶意指令的攻击演示，接入第三方 MCP 服务器前必看。 | [第 03 课](../lessons/03_tools/README.md) · [第 09 课](../lessons/09_security/README.md) |
| I10 | [GitHub MCP Exploited: Accessing private repositories via MCP](https://invariantlabs.ai/blog/mcp-github-vulnerability) | Invariant Labs · 2025 | 恶意 issue 劫持 Agent、泄露私有仓库信息的真实案例，是"致命三要素"的教科书示例。 | [第 09 课](../lessons/09_security/README.md) |
| I11 | [EchoLeak: The First Real-World Zero-Click Prompt Injection Exploit in a Production LLM System](https://arxiv.org/abs/2509.10540) | Pavan Reddy 等 · 2025 | 对 Microsoft 365 Copilot 零点击注入漏洞（CVE-2025-32711）的案例分析。 | [第 09 课](../lessons/09_security/README.md) |
| I12 | [Making Claude Code more secure and autonomous with sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) | Anthropic · 2025 | 用文件系统与网络隔离减少权限确认、同时提升安全性的工程实践，适合思考"沙箱 vs 审批"的取舍。 | [第 09 课](../lessons/09_security/README.md) |
| I13 | [AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework) | NIST | 美国国家标准与技术研究院的 AI 风险管理框架，企业做 AI 治理体系时常用的参考。 | [第 09 课](../lessons/09_security/README.md) · [第 12 课](../lessons/12_production_architecture/README.md) |

## J. 评估

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| J1 ⭐ | [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | Anthropic · 2026 | Agent 评估的系统性指南：三类评分器、pass@k 与 pass^k、能力评估与回归评估、从 20~50 个真实失败任务起步。第 11 课的必读延伸。 | [第 11 课](../lessons/11_evals/README.md) |
| J2 ⭐ | [Your AI Product Needs Evals](https://hamel.dev/blog/posts/evals/) | Hamel Husain · 2024 | 为什么评估是 AI 产品迭代的核心，以及如何从零搭建评估体系，实践性极强。 | [第 11 课](../lessons/11_evals/README.md) |
| J3 | [Using LLM-as-a-Judge For Evaluation: A Complete Guide](https://hamel.dev/blog/posts/llm-judge/) | Hamel Husain · 2024 | 如何构建一个与领域专家判断一致的 LLM 评委。 | [第 11 课](../lessons/11_evals/README.md) |
| J4 | [Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685) | Lianmin Zheng 等 · 2023 | LLM 评委的奠基论文，系统讨论了位置偏差、冗长偏差、自我增强偏差。 | [第 11 课](../lessons/11_evals/README.md) |
| J5 ⭐ | [τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045) | Shunyu Yao 等 · 2024 | 用模拟用户 + 领域工具 + 业务政策评估 Agent，提出衡量稳定性的 pass^k。客服类 Agent 的评估设计可以直接借鉴。 | [第 11 课](../lessons/11_evals/README.md) |
| J6 | [τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment](https://arxiv.org/abs/2506.07982) | Victor Barres 等 · 2025 | τ-bench 的后续：用户和 Agent 都能操作环境的"双控"场景，更接近真实的技术支持对话。 | [第 11 课](../lessons/11_evals/README.md) |
| J7 | [SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://arxiv.org/abs/2310.06770) | Carlos E. Jimenez 等 · 2023 | 用真实 GitHub issue 评估编程 Agent 的基准，理解"以最终状态（测试是否通过）评估"的思路。 | [第 11 课](../lessons/11_evals/README.md) |
| J8 | [AgentBench: Evaluating LLMs as Agents](https://arxiv.org/abs/2308.03688) | Xiao Liu 等 · 2023 | 多环境的 Agent 能力基准，了解学术界如何横向比较模型的 Agent 能力。 | [第 11 课](../lessons/11_evals/README.md) |
| J9 | [Patterns for Building LLM-based Systems & Products](https://eugeneyan.com/writing/llm-patterns/) | Eugene Yan · 2023 | 评估、RAG、护栏、缓存、用户反馈等模式的长文综述，适合当作 LLM 工程的"模式目录"。 | [第 11 课](../lessons/11_evals/README.md) · [第 12 课](../lessons/12_production_architecture/README.md) |

## K. 可观测性

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| K1 ⭐ | [OpenTelemetry GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions-genai) | OpenTelemetry 项目 | GenAI 相关的 Span、指标、事件的字段命名约定（agentkit 的 `gen_ai.*` 属性参考了它）。注意：该约定已从 OpenTelemetry 主语义约定仓库迁移到这个独立仓库。 | [第 10 课](../lessons/10_observability/README.md) |
| K2 | [Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/) | Google SRE Book | 监控的基本功：四个黄金信号、症状 vs 原因、告警设计原则。Agent 的监控同样适用。 | [第 10 课](../lessons/10_observability/README.md) |

## L. 发布与运维

| 顺序 | 资料 | 作者 · 年份 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| L1 ⭐ | [Canarying Releases](https://sre.google/workbook/canarying-releases/) | Google SRE Workbook | 金丝雀发布的系统方法：如何选择指标、样本和评估时长。Agent 的提示词/模型发布可以直接套用。 | [第 16 课](../lessons/16_release_ops/README.md) |
| L2 ⭐ | [Postmortem Culture: Learning from Failure](https://sre.google/sre-book/postmortem-culture/) | Google SRE Book | 无责复盘文化与复盘文档的写法，是"把事故变成改进"的方法论基础。 | [第 16 课](../lessons/16_release_ops/README.md) |

## M. 真实事故与合规参考

| 顺序 | 资料 | 来源 | 为什么值得读 | 课程 |
|---|---|---|---|---|
| M1 | [Incident 1152: LLM-Driven Replit Agent Reportedly Executed Unauthorized Destructive Commands During Code Freeze, Leading to Loss of Production Data](https://incidentdatabase.ai/cite/1152/) | AI Incident Database | 2025 年 Replit 编程 Agent 在代码冻结期间删除生产数据库事件的汇总记录——"过度授权"的真实代价。AI Incident Database 本身也值得收藏，可以按关键词检索更多 AI 事故。 | [第 09 课](../lessons/09_security/README.md) · [第 16 课](../lessons/16_release_ops/README.md) |
| M2 | [Transparency obligations under Article 50 of the AI Act](https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act) | 欧盟委员会 | 欧盟《人工智能法》第 50 条透明度义务的官方问答，包括"告知用户正在与 AI 交互"的要求。面向欧盟用户的 Agent 需要关注。 | [第 12 课](../lessons/12_production_architecture/README.md) |

---

## 收录原则

1. **只收录核实过的资料**：每个链接都在 2026 年 9 月实际访问过，标题与原文核对一致；有跳转的使用跳转后的最终地址。
2. **宁缺毋滥**：无法稳定访问或无法核实内容的资料（包括一些常被引用但原链接已失效或需要登录的页面）不收录。
3. **优先一手资料**：优先官方文档、原始论文和作者本人的文章，而不是二手转述。
4. **欢迎补充**：如果你发现某个链接失效，或者有值得收录的资料，欢迎提 Issue 或 PR——请附上"为什么值得读"。
