[中文](glossary.md) | [English](glossary.en.md)

# 中英术语表

> 📖 本文是"领域参考手册"的一部分。按主题分组（而不是按字母），因为同一组的术语放在一起读更容易建立联系。
> 相关文档：[速查表](cheatsheet.md) · [框架对照](framework-comparison.md) · [失败模式图鉴](failure-modes.md)

**用法**：每条术语给出一句"大白话"解释。带 `代码字体` 的是 agentkit 中对应的类或函数，可以直接去源码里找。遇到不懂的词，`Ctrl+F` 搜中文或英文都可以。

共收录 **185** 条术语（分 13 组），另附 17 组易混淆术语对比。

---

## 一、基础概念

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 智能体 | Agent | 让模型在一个循环里自己决定调用哪些工具、调用几次，直到它认为任务完成的程序。`Agent` | [第 01 课](../lessons/01_agent_loop/README.md) |
| 工作流 | Workflow | 步骤和顺序由代码写死、模型只负责其中某些步骤的程序；比 Agent 可预测、便宜、好测试。 | [第 04 课](../lessons/04_orchestration/README.md) |
| Agent 循环 | Agent Loop | "调模型 → 执行它要的工具 → 把结果喂回去 → 再调模型"的循环，是所有 Agent 的心脏。 | [第 01 课](../lessons/01_agent_loop/README.md) |
| 增强型 LLM | Augmented LLM | 配上了检索、工具、记忆能力的模型调用，是搭建 Workflow 和 Agent 的基本积木（Anthropic《Building Effective Agents》中的说法）。 | [第 04 课](../lessons/04_orchestration/README.md) |
| 系统提示词 | System Prompt | 每次对话开头给模型的"岗位说明书"，定义它是谁、该怎么做；要假设它可能被用户套出来。 | [第 01 课](../lessons/01_agent_loop/README.md) |
| 词元 | Token | 模型处理文本的最小单位，计费和上下文长度都按它算；中文大约一个字一个 token（因模型而异）。`estimate_tokens` | [第 01 课](../lessons/01_agent_loop/README.md) |
| 上下文窗口 | Context Window | 模型一次能"看到"的最大 token 数，包括系统提示词、历史、工具定义和工具结果。 | [第 03 课](../lessons/03_context_memory/README.md) |
| 工具调用 / 函数调用 | Tool Calling / Function Calling | 模型不直接回答，而是输出"我要调用某个函数、参数是这些"的结构化请求，由你的程序执行。 | [第 01 课](../lessons/01_agent_loop/README.md) |
| ReAct | Reasoning + Acting | 让模型交替进行"思考"和"行动（调用工具）"的范式，出自 Yao 等人 2022 年的论文，是现代 Agent 循环的思想来源。 | [第 01 课](../lessons/01_agent_loop/README.md) |
| 步数上限 | Max Steps / Max Turns | 一次运行最多允许的模型调用轮数，防止死循环烧钱的最后一道硬防线。`Agent(max_steps=...)` | [第 01 课](../lessons/01_agent_loop/README.md) |
| 结束原因 | Stop Reason / Finish Reason | 一次运行或一次模型调用为什么结束：给出答案、达到上限、被拦截、等待审批……是最重要的监控维度之一。`RunResult.stop_reason` | [第 01 课](../lessons/01_agent_loop/README.md) |
| 非确定性 | Non-determinism | 同样的输入，模型可能给出不同的输出、走不同的步骤；所以"试一次没问题"不等于没问题。 | [第 08 课](../lessons/08_evals/README.md) |
| 温度 | Temperature | 控制模型输出随机性的采样参数，越低越稳定；但即使设为 0 也不保证完全可复现。 | [第 01 课](../lessons/01_agent_loop/README.md) |
| 流式输出 | Streaming | 模型边生成边返回，用户不用等到全部完成才看到内容；对长任务的体验很关键。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 测试替身（剧本模型） | Test Double / Scripted LLM | 按预先写好的剧本返回结果的"假模型"，让 Agent 测试零成本、可复现。`ScriptedLLM` | [第 01 课](../lessons/01_agent_loop/README.md) |
| 模型抽象层 | LLM Abstraction | 业务代码只依赖一个很小的 `chat()` 接口，而不是某家厂商的 SDK，方便换模型和叠加能力。`LLM` 协议 | [第 01 课](../lessons/01_agent_loop/README.md) |

## 二、工具

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 工具 | Tool | Agent 与外部世界交互的唯一通道：查数据库、调 API、发邮件……本质是一个带说明书的函数。`@tool` | [第 02 课](../lessons/02_tools/README.md) |
| 工具描述 | Tool Description | 写给模型看的工具说明书：做什么、何时用、何时不用；模型选工具全靠它。 | [第 02 课](../lessons/02_tools/README.md) |
| JSON Schema | JSON Schema | 描述"参数长什么样"的标准格式：有哪些字段、什么类型、哪些必填、取值范围。 | [第 02 课](../lessons/02_tools/README.md) |
| 参数校验 | Argument Validation | 执行工具前检查模型给的参数是否合法；模型输出的参数只是"生成的文本"，必须当不可信输入对待。 | [第 02 课](../lessons/02_tools/README.md) |
| 错误即观察 | Errors as Observations | 工具出错时不抛异常搞崩 Agent，而是把错误写成模型能看懂的文字反馈给它，让它自己纠正。`ToolResult` | [第 02 课](../lessons/02_tools/README.md) |
| 风险分级 | Risk Tiering | 给每个工具标上 read / write / dangerous 等级，权限和审批据此决定是否放行。`Tool(risk=...)` | [第 02 课](../lessons/02_tools/README.md) |
| 幂等 | Idempotency | 同一个操作执行一次和执行多次效果相同；重试和崩溃恢复都依赖它，否则会重复扣款、重复建单。 | [第 05 课](../lessons/05_reliability/README.md) |
| 幂等键 | Idempotency Key | 标识"这是同一次操作"的唯一键，重放时据此去重。agentkit 用 `run_id:call_id`。`ToolContext.idempotency_key` | [第 05 课](../lessons/05_reliability/README.md) |
| 可信上下文 | Trusted Context | 由系统（而非模型）注入给工具的信息，如用户 ID、租户 ID、角色；模型看不到也改不了。`ToolContext` | [第 02 课](../lessons/02_tools/README.md) |
| 输出截断 | Output Truncation | 工具返回太长时只保留前 N 个字符，并告诉模型"已截断"，防止撑爆上下文。`Tool(max_output_chars=...)` | [第 02 课](../lessons/02_tools/README.md) |
| Agent-计算机接口 | ACI (Agent-Computer Interface) | 类比"人机界面（HCI）"：工具的名称、参数、描述、返回格式就是 Agent 的操作界面，值得同样用心设计。 | [第 02 课](../lessons/02_tools/README.md) |
| 防呆设计 | Poka-yoke | 源自制造业的"让人不可能犯错"的设计思想；用在工具上就是修改参数设计，让模型更难用错。 | [第 02 课](../lessons/02_tools/README.md) |
| 结构化输出 | Structured Output | 让模型输出符合指定 Schema 的 JSON，而不是自由文本，供下游代码可靠使用。`complete_json` | [第 04 课](../lessons/04_orchestration/README.md) |
| 模型上下文协议 | MCP (Model Context Protocol) | 连接 AI 应用与外部工具/数据源的开放协议，类似"AI 世界的 USB 接口"；服务器可提供工具、资源和提示词模板。 | [第 02 课](../lessons/02_tools/README.md) |
| 沙箱 | Sandbox | 隔离的执行环境（容器、虚拟机等），让不可信代码或高风险工具出事也影响不到外面。 | [第 02 课](../lessons/02_tools/README.md) |
| 工具投毒 | Tool Poisoning | 在工具描述里藏恶意指令；由于描述会进入模型上下文，这等于往提示词里注入内容。 | [第 06 课](../lessons/06_security/README.md) |

## 三、上下文与记忆

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 上下文工程 | Context Engineering | 决定每次调用模型时"让它看到什么"的一整套方法：放什么、删什么、压缩什么、什么时候检索。 | [第 03 课](../lessons/03_context_memory/README.md) |
| 短期记忆 | Short-term Memory | 当前这次对话的消息历史；会话结束就没了。 | [第 03 课](../lessons/03_context_memory/README.md) |
| 长期记忆 | Long-term Memory | 存在外部、跨会话保留的信息（如用户偏好），需要时检索出来放进上下文。`MemoryStore` | [第 03 课](../lessons/03_context_memory/README.md) |
| 滑动窗口 | Sliding Window | 上下文太长时只保留最近的若干条消息；简单便宜，但会丢掉早期信息。`SlidingWindow` | [第 03 课](../lessons/03_context_memory/README.md) |
| 压缩 / 摘要 | Compaction / Summarization | 把较早的对话交给模型写成摘要，替换原文，以节省上下文；代价是一次额外调用和信息损失。`SummarizingCompactor` | [第 03 课](../lessons/03_context_memory/README.md) |
| 消息块 | Message Block | 一条带工具调用的 assistant 消息加上它的全部工具结果，截断时必须整体保留或整体丢弃。`split_blocks` | [第 03 课](../lessons/03_context_memory/README.md) |
| 中间遗忘 | Lost in the Middle | 研究发现：相关信息放在长上下文的中间时，模型利用得最差，放在开头或结尾时最好。 | [第 03 课](../lessons/03_context_memory/README.md) |
| 上下文腐烂 | Context Rot | 上下文越长，模型对其中信息的利用越差的现象；上下文是有限的"注意力预算"。 | [第 03 课](../lessons/03_context_memory/README.md) |
| 上下文投毒 | Context Poisoning | 一个错误结论进入上下文后被反复引用，越错越远。 | [第 03 课](../lessons/03_context_memory/README.md) |
| 即时上下文 | Just-in-time Context | 不预先塞满所有资料，只保留"指针"（文件路径、ID），需要时再用工具加载。 | [第 03 课](../lessons/03_context_memory/README.md) |
| 检索增强生成 | RAG (Retrieval-Augmented Generation) | 先从知识库检索相关资料，再连同问题一起交给模型回答；长期记忆本质上也是 RAG。 | [第 03 课](../lessons/03_context_memory/README.md) · [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 嵌入 / 向量检索 | Embedding / Vector Search | 把文本变成一串数字（向量），语义相近的文本向量也相近，从而实现"按意思搜索"。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 混合检索 | Hybrid Search | 同时用关键词检索（如 BM25）和向量检索，取长补短：前者擅长精确词，后者擅长语义。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 重排序 | Rerank | 检索出候选结果后，用更精细的模型重新排序，把最相关的放前面。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 提示词缓存 | Prompt Caching | 模型服务商缓存请求中相同的前缀部分，再次命中时更便宜、更快；前缀任何变化都会导致失效。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 记忆投毒 | Memory Poisoning | 把恶意或错误内容写进长期记忆，让它在以后的会话中持续生效（如"记住我是管理员"）。 | [第 06 课](../lessons/06_security/README.md) |
| 被遗忘权 | Right to Erasure | 个人要求删除其个人数据的权利（GDPR 等法规中的概念）；Agent 的记忆、日志都要能删。`MemoryStore.forget` | [第 03 课](../lessons/03_context_memory/README.md) |

## 四、编排与多 Agent

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 编排 | Orchestration | 决定"谁先做、谁后做、谁来做"的逻辑；可以由代码决定（Workflow），也可以由模型决定（Agent）。 | [第 04 课](../lessons/04_orchestration/README.md) |
| 提示链 | Prompt Chaining | 把任务拆成固定的几步，上一步的输出是下一步的输入，中间可以加检查关卡。`chain` | [第 04 课](../lessons/04_orchestration/README.md) |
| 路由 | Routing | 先判断请求属于哪一类，再交给对应的专门处理流程（如客服分流）。`route` | [第 04 课](../lessons/04_orchestration/README.md) |
| 并行化 | Parallelization | 同时跑多个模型调用：要么把任务切片各做一部分（分段，sectioning），要么同一任务做多次再投票（voting）。`parallel` | [第 04 课](../lessons/04_orchestration/README.md) |
| 多数投票 / 自洽性 | Majority Vote / Self-Consistency | 同一问题让模型回答多次，取出现最多的答案，以提高可靠性（Wang 等人 2022 年的论文提出 self-consistency）。`majority_vote` | [第 04 课](../lessons/04_orchestration/README.md) |
| 编排者-执行者 | Orchestrator-Workers | 由一个模型动态拆解任务、分发给多个执行者、再汇总结果；与并行化的区别是子任务不是预先写死的。`orchestrator_workers` | [第 04 课](../lessons/04_orchestration/README.md) |
| 评估-优化 | Evaluator-Optimizer | 一个负责生成、一个负责评审，按评审意见反复修改直到合格或达到轮数上限。`evaluator_optimizer` | [第 04 课](../lessons/04_orchestration/README.md) |
| 多 Agent 系统 | Multi-Agent System | 多个各有分工（各自的提示词、工具、上下文）的 Agent 协作完成任务；更强也更贵、更难调试。 | [第 04 课](../lessons/04_orchestration/README.md) |
| Agent 即工具 / 主管模式 | Agent as Tool / Supervisor | 把专家 Agent 包装成工具给主管 Agent 调用，专家的结果交回主管，主管始终掌控对话。`agent_as_tool` | [第 04 课](../lessons/04_orchestration/README.md) |
| 转交 | Handoff | 当前 Agent 把对话整个交给另一个 Agent，由对方接管后续交互（控制权转移）。 | [第 04 课](../lessons/04_orchestration/README.md) |
| 反思 | Reflection | 让模型检查、批评自己（或他人）的输出并据此改进；Reflexion（Shinn 等人 2023）是代表性工作。 | [第 04 课](../lessons/04_orchestration/README.md) |
| 先规划后执行 | Plan-then-Execute | 先生成完整计划再逐步执行，而不是走一步看一步；可预测性更好，也是一种防注入的设计模式。 | [第 04 课](../lessons/04_orchestration/README.md) |
| 委派契约 | Delegation Contract | 把任务交给子 Agent 时必须提供的信息清单：目标、已知事实、约束、期望输出格式。 | [第 04 课](../lessons/04_orchestration/README.md) |

## 五、可靠性与成本

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 可重试错误 | Retryable Error | 重试有可能成功的错误（限流 429、服务端 5xx、超时）；400、401 这类重试也没用。`LLMError.retryable` | [第 05 课](../lessons/05_reliability/README.md) |
| 重试 | Retry | 失败后再试一次；只对可重试错误、只在一层、带退避地做。`retry_call` | [第 05 课](../lessons/05_reliability/README.md) |
| 指数退避 | Exponential Backoff | 每次重试前等待的时间按指数增长（0.5s、1s、2s……），给下游恢复的时间。`backoff_delay` | [第 05 课](../lessons/05_reliability/README.md) |
| 抖动 | Jitter | 在退避时间上加随机量，避免成千上万个客户端在同一时刻整齐地重试。 | [第 05 课](../lessons/05_reliability/README.md) |
| 惊群效应 | Thundering Herd | 大量客户端在同一时刻同时发起请求（比如同时重试），把刚恢复的服务再次打垮。 | [第 05 课](../lessons/05_reliability/README.md) |
| 重试风暴 | Retry Storm | 多层重试相乘、没有抖动，导致故障期间请求量被放大数倍甚至数十倍。 | [第 05 课](../lessons/05_reliability/README.md) |
| 熔断器 | Circuit Breaker | 下游持续失败时"跳闸"，一段时间内直接快速失败，过后放少量请求试探；有关闭/打开/半开三种状态。`CircuitBreaker` | [第 05 课](../lessons/05_reliability/README.md) |
| 降级 | Fallback / Graceful Degradation | 主方案不可用时切换到备用方案（备用模型、缓存、规则、转人工），宁可差一点也别完全不可用。`ResilientLLM` | [第 05 课](../lessons/05_reliability/README.md) |
| 检查点 | Checkpoint | 每走一步就把完整运行状态存盘，崩溃或暂停后可以从断点继续。`Checkpointer` | [第 05 课](../lessons/05_reliability/README.md) |
| 持久化执行 | Durable Execution | 保证一段程序即使经历崩溃、重启也能从断点继续执行完的运行方式；Temporal、LangGraph 的检查点都属于这一思路。 | [第 05 课](../lessons/05_reliability/README.md) · [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 原子写入 | Atomic Write | 写文件要么完整成功要么完全没发生，不会出现写了一半的损坏文件（先写临时文件再改名）。`FileCheckpointer` | [第 05 课](../lessons/05_reliability/README.md) |
| Saga / 补偿事务 | Saga / Compensation | 把长事务拆成多步，每一步都配一个"撤销动作"，中途失败时依次撤销已完成的步骤。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 预算 | Budget | 对一次运行的步数、token、金额、工具调用次数、时长设上限，超限就优雅停止。`BudgetHook` | [第 05 课](../lessons/05_reliability/README.md) |
| 钱包拒绝服务 | Denial of Wallet | 攻击者故意让你的 Agent 疯狂消耗 token，目的是让你付出高额账单，而不是让服务宕机。 | [第 05 课](../lessons/05_reliability/README.md) |
| 彩虹部署 | Rainbow Deployment | 新旧多个版本同时在线、流量逐步切换，让正在运行的长任务不被发布打断。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 成本归因 | Cost Attribution | 把每一分钱的模型成本记到具体的租户、用户、功能、模型上；这是 FinOps（云成本治理）和定价的基础。 | [第 11 课](../lessons/11_cost_latency/README.md) |

## 六、安全与治理

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 提示词注入 | Prompt Injection | 用输入里的文字篡改模型的行为（"忽略之前的指令……"）；因为模型分不清指令和数据，目前没有 100% 的防法。 | [第 06 课](../lessons/06_security/README.md) |
| 间接提示词注入 | Indirect Prompt Injection | 攻击指令不是用户直接输入的，而是藏在 Agent 会读到的网页、邮件、文档、工单里；比直接注入更危险。 | [第 06 课](../lessons/06_security/README.md) |
| 越狱 | Jailbreak | 用角色扮演、编码等技巧绕过模型的安全限制。 | [第 06 课](../lessons/06_security/README.md) |
| 纵深防御 | Defense in Depth | 多层防护叠加（输入检测、数据隔离、最小权限、审批、输出过滤、审计），任何一层被突破还有下一层。 | [第 06 课](../lessons/06_security/README.md) |
| 聚光灯（数据标记） | Spotlighting | 用标签或编码把外部数据明确标记为"数据而不是指令"，帮助模型区分来源；能降低注入成功率但不能根除。`ToolOutputGuard` | [第 06 课](../lessons/06_security/README.md) |
| 致命三要素 | Lethal Trifecta | Simon Willison 提出：Agent 同时能访问私有数据、接触不可信内容、对外通信时，数据外泄只差一次注入。 | [第 06 课](../lessons/06_security/README.md) |
| 数据外泄 | Data Exfiltration | 数据被悄悄送到外部，例如通过模型输出中的图片链接、对外发邮件的工具。 | [第 06 课](../lessons/06_security/README.md) |
| 护栏 | Guardrail | 对输入、输出或工具调用做检查和拦截的机制；降低风险的概率，但不能替代权限设计。`InputGuard` / `OutputGuard` | [第 06 课](../lessons/06_security/README.md) |
| 最小权限 | Least Privilege | 只给完成任务所必需的最少权限；假设模型一定会被骗，然后限制它被骗后能做的事。 | [第 06 课](../lessons/06_security/README.md) |
| 基于角色的访问控制 | RBAC (Role-Based Access Control) | 按用户角色决定能用哪些工具；agentkit 中既"不给看"也"不让调"。`PermissionPolicy(role_tools=...)` | [第 06 课](../lessons/06_security/README.md) |
| 人在回路 | HITL (Human-in-the-Loop) | 关键步骤由人来确认或决策，典型场景是高风险操作的人工审批。`PauseRun` / `agent.approve` | [第 06 课](../lessons/06_security/README.md) |
| 过度授权 | Excessive Agency | Agent 拥有超出任务所需的功能、权限或自主性（OWASP LLM Top 10 中的一类风险）。 | [第 06 课](../lessons/06_security/README.md) |
| 混淆代理人 | Confused Deputy | 有权限的程序被没权限的人"借用"了权限，例如让模型填 user_id 从而查到别人的数据。 | [第 06 课](../lessons/06_security/README.md) |
| 个人身份信息 | PII (Personally Identifiable Information) | 能识别到具体个人的信息：身份证号、手机号、邮箱等。 | [第 06 课](../lessons/06_security/README.md) |
| 脱敏 | Redaction | 把敏感信息替换成占位符（如"[手机号已脱敏]"）后再输出或存储。`redact_pii` | [第 06 课](../lessons/06_security/README.md) |
| 审计日志 | Audit Log | 记录"谁、何时、以什么身份、让 Agent 做了什么、结果如何"，给安全和合规用；必须完整且不可篡改。`AuditLog` | [第 06 课](../lessons/06_security/README.md) |
| 一次写入多次读取存储 | WORM (Write Once Read Many) | 写入后无法修改或删除的存储，常用于保存审计日志。 | [第 06 课](../lessons/06_security/README.md) |
| 紧急开关 | Kill Switch | 发现问题时能立即全局禁用某个工具或整个 Agent 的开关。`PermissionPolicy(deny_tools=...)` | [第 06 课](../lessons/06_security/README.md) |
| 红队测试 | Red Teaming | 站在攻击者角度主动攻击自己的系统，找出安全漏洞。 | [第 06 课](../lessons/06_security/README.md) |
| 职责分离 | Separation of Duties | 发起操作的人不能同时是批准操作的人。 | [第 06 课](../lessons/06_security/README.md) |
| OWASP LLM Top 10 | OWASP Top 10 for LLM Applications | OWASP 发布的大模型应用十大安全风险清单，2025 版包括提示词注入、敏感信息泄露、过度授权等。 | [第 06 课](../lessons/06_security/README.md) |

## 七、可观测性

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 可观测性 | Observability | 能从外部数据（日志、指标、追踪）推断系统内部发生了什么的能力。 | [第 07 课](../lessons/07_observability/README.md) |
| 链路追踪 | Tracing | 把一次请求拆成嵌套的步骤树，记录每一步的输入、输出、耗时，是排查 Agent 问题的主要手段。`Tracer` | [第 07 课](../lessons/07_observability/README.md) |
| 追踪 / 跨度 | Trace / Span | 一次完整运行叫一个 trace；其中每个步骤（一次模型调用、一次工具调用）叫一个 span，span 可以嵌套。`Span` | [第 07 课](../lessons/07_observability/README.md) |
| OpenTelemetry | OpenTelemetry (OTel) | 开源的可观测性标准和工具集；其 GenAI 语义约定规定了 `gen_ai.request.model` 等字段名，方便对接各种后端。 | [第 07 课](../lessons/07_observability/README.md) |
| 轨迹 | Trajectory | Agent 在一次运行中依次做了哪些动作（调用了哪些工具、顺序如何）；评估和排查都要看它。`RunResult.tools_called()` | [第 07 课](../lessons/07_observability/README.md) |
| 指标 | Metrics | 可聚合的数值（成功率、延迟、token 数、成本），用于看板和告警。 | [第 07 课](../lessons/07_observability/README.md) |
| 采样 | Sampling | 只保留一部分追踪数据以节省成本；调试数据可以采样，审计数据不能。 | [第 07 课](../lessons/07_observability/README.md) |
| 服务等级目标 | SLO / SLI | SLI 是衡量服务质量的指标（如任务完成率），SLO 是你对它承诺的目标值（如 ≥ 95%）。 | [第 09 课](../lessons/09_production_architecture/README.md) |
| 静默失败 | Silent Failure | 系统看起来一切正常（HTTP 200、没报错），但任务其实没完成；Agent 最常见的失败形态。 | [第 07 课](../lessons/07_observability/README.md) |

## 八、评估

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 评估 | Evals | 用一批有期望结果的测试用例系统地衡量 Agent 表现；相当于 Agent 的单元测试 + 回归测试。`run_eval` | [第 08 课](../lessons/08_evals/README.md) |
| 黄金数据集 / 评估集 | Golden Dataset / Eval Set | 真实用户问题 + 期望行为组成的用例集合，要持续从线上 bad case 补充。`EvalCase` | [第 08 课](../lessons/08_evals/README.md) |
| 评分器 | Grader | 给一次运行打分的函数：规则评分（便宜确定）、LLM 评委（灵活但有噪声）、人工评分（准但贵）。`rule_grader` | [第 08 课](../lessons/08_evals/README.md) |
| LLM 评委 | LLM-as-a-Judge | 用一个模型按评分细则给另一个模型的输出打分；存在位置、冗长、自我偏好等偏差，需要人工校准。`llm_judge` | [第 08 课](../lessons/08_evals/README.md) |
| 评分细则 | Rubric | 告诉评委"怎么算好"的具体、可检验的条目，如"是否给出了可执行的步骤"。 | [第 08 课](../lessons/08_evals/README.md) |
| 轨迹评估 | Trajectory Evaluation | 不只看最终答案，还检查过程：是否调用了必须的工具、是否没碰禁止的工具、顺序是否合理。 | [第 08 课](../lessons/08_evals/README.md) |
| 回归 | Regression | 以前通过、现在失败的用例；每次修改后都要检查，上线前应为零。`EvalReport.regressions` | [第 08 课](../lessons/08_evals/README.md) |
| pass@k | pass@k | k 次尝试中**至少一次**成功的概率；衡量"有没有能力做到"。 | [第 08 课](../lessons/08_evals/README.md) |
| pass^k | pass^k | k 次尝试**全部**成功的概率；衡量"是否稳定可靠"，由 τ-bench 论文提出，面向用户的场景更该看它。 | [第 08 课](../lessons/08_evals/README.md) |
| 能力评估 / 回归评估 | Capability Eval / Regression Eval | 前者用难题衡量能力上限（通过率本来就低），后者用已掌握的任务防止退化（通过率应接近 100%）。 | [第 08 课](../lessons/08_evals/README.md) |
| 离线评估 / 在线评估 | Offline / Online Eval | 离线：上线前用评估集跑；在线：上线后对真实流量抽样打分、收集反馈。 | [第 08 课](../lessons/08_evals/README.md) |
| CI 门禁 | CI Gate | 在持续集成流程中自动运行评估，通过率不达标或出现回归就禁止合并/发布。 | [第 08 课](../lessons/08_evals/README.md) |
| 基准测试 | Benchmark | 公开的标准化评测集（如 τ-bench、SWE-bench），适合横向比较模型，但不能替代你自己的业务评估集。 | [第 08 课](../lessons/08_evals/README.md) |

## 九、生产架构

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 模型网关 | LLM Gateway | 所有模型调用都经过的统一入口，集中做鉴权、限流、计量、路由、缓存和密钥管理。 | [第 09 课](../lessons/09_production_architecture/README.md) |
| 多租户 | Multi-tenancy | 一套系统同时服务多个客户（租户），它们的数据和配额必须严格隔离。 | [第 09 课](../lessons/09_production_architecture/README.md) |
| 租户隔离 | Tenant Isolation | 保证一个租户永远看不到、影响不到另一个租户的数据和资源；必须在存储/检索层强制执行。 | [第 09 课](../lessons/09_production_architecture/README.md) |
| 吵闹邻居 | Noisy Neighbor | 一个租户的大流量挤占共享资源，导致其他租户变慢或被限流。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 限流 / 令牌桶 | Rate Limiting / Token Bucket | 限制单位时间内的请求量；令牌桶是常用算法：按固定速率往桶里放令牌，请求要拿到令牌才能执行。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 配额 | Quota | 在较长周期（天、月）内允许的总用量上限，常按租户或用户设置。 | [第 09 课](../lessons/09_production_architecture/README.md) |
| 灰度发布 | Canary Release | 新版本先给一小部分流量用，观察指标没问题再逐步扩大。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 金丝雀数据 | Canary Token | 故意埋进数据中的唯一字符串，一旦在不该出现的地方出现，就说明发生了泄露。 | [第 09 课](../lessons/09_production_architecture/README.md) · [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 模型版本固定 | Model Pinning | 生产环境使用具体的模型快照版本，而不是会自动更新的别名，避免行为悄悄改变。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 运行手册 | Runbook | 写给值班人员的事故处置步骤：出现某种告警时，具体该做什么。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 架构决策记录 | ADR (Architecture Decision Record) | 用短文档记录一个重要设计决策的背景、选项和理由，方便以后回顾。 | [第 09 课](../lessons/09_production_architecture/README.md) |
| A2A 协议 | A2A (Agent2Agent Protocol) | 让不同团队、不同框架构建的 Agent 之间互相发现和通信的开放协议；MCP 连接"Agent 与工具"，A2A 连接"Agent 与 Agent"。 | [第 09 课](../lessons/09_production_architecture/README.md) |


## 十、分布式与高并发

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 横向扩展 | Horizontal Scaling | 通过增加机器/实例数量（而不是换更强的机器）来提升处理能力；前提是 worker 无状态。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 无状态 Worker | Stateless Worker | 进程内不保存任何会话或运行状态，状态都放在共享存储里；这样任何一个 worker 都能接手任何任务，挂了也不丢东西。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 会话粘性 | Session Affinity / Sticky Session | 把同一会话的请求总是路由到同一个实例；能减少并发冲突，但实例宕机时要能转移。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| Actor 模型 | Actor Model | 每个实体（如一个会话）是一个"演员"，有自己的信箱，逐条处理消息；同一会话的消息天然串行，不会并发写。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 服务器推送事件 | SSE (Server-Sent Events) | 服务器通过一个长连接持续向浏览器推送消息的标准，常用于流式输出和进度通知。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 异步任务队列 | Async Task Queue | 请求先放进队列立刻返回，由后台 worker 慢慢处理，完成后再通知用户；适合几十秒以上的长任务。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 工作流引擎 | Workflow Engine | 专门负责可靠地执行多步骤、长时间流程的系统（如 Temporal），自带重试、超时、状态持久化。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 投递语义 | Delivery Semantics | 消息系统对"一条消息会被处理几次"的承诺：最多一次、至少一次、恰好一次。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 至少一次 | At-least-once | 保证消息不丢，但可能重复投递；最常见的语义，所以消费者必须幂等。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 恰好一次 | Exactly-once | 每条消息只生效一次；端到端很难直接做到，实践中用"至少一次 + 幂等"达到效果上的恰好一次。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 可见性超时 | Visibility Timeout | 消息被某个消费者取走后，在这段时间内对其他消费者不可见；超时仍未确认就会被重新投递。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 死信队列 | DLQ (Dead Letter Queue) | 多次处理失败的消息被移到这里，等待人工排查，而不是无限重试拖垮系统。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 毒消息 | Poison Message | 无论重试多少次都会处理失败的消息（如格式错误），不隔离就会反复消耗资源。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 租约 | Lease | 带过期时间的"占有权"：worker 在租约有效期内独占一个任务，过期不续就会被别人接手。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 心跳 | Heartbeat | worker 定期发送"我还活着"的信号，用来续约或让系统判断它是否失联。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 防护令牌 | Fencing Token | 每次授予锁/租约时发放的单调递增编号，写入时携带；存储端拒绝旧编号的写入，从而挡住"以为自己还持有锁"的僵尸 worker。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 丢失更新 | Lost Update | 两个进程同时"读-改-写"同一份数据，后写的覆盖了先写的，先写的修改就丢了。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 分布式锁 | Distributed Lock | 跨多台机器的互斥锁；实现正确并不容易，需要配合租约和防护令牌。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 乐观锁 / 比较并交换 | Optimistic Locking / CAS (Compare-And-Swap) | 不加锁，写入时检查版本号是否还是自己读到的那个，不是就重读重试；冲突少时比加锁高效。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 按会话分区串行化 | Per-session Partitioning | 按会话 ID 把消息分到固定的分区/队列，同一会话的消息按顺序由一个消费者处理，从根源上避免并发写。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 背压 | Backpressure | 下游处理不过来时，把"慢一点"的信号传回上游（拒绝、排队、限速），而不是无限接收直到崩溃。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 准入控制 / 负载削减 | Admission Control / Load Shedding | 系统过载时主动拒绝一部分请求，保证其余请求能正常完成；比"大家一起超时"好。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 加权公平排队 | Weighted Fair Queuing | 多个租户共享资源时，按权重轮流服务各租户的队列，防止一个大租户独占。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 事务性发件箱 | Transactional Outbox | 把"要发的事件"和业务数据写在同一个数据库事务里，再由独立进程发布，避免"库写了、消息没发"的不一致。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 请求合并 | Singleflight / Request Coalescing | 同一个键的多个并发请求只放行一个去真正执行，其余等待并共享结果；防止缓存过期瞬间的请求风暴。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |
| 全局限流 | Global Rate Limiting | 在所有实例之间共享的限流（如集中式令牌桶），而不是每台机器各限各的。 | [第 10 课](../lessons/10_distributed_concurrency/README.md) |

## 十一、成本与延迟优化

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 模型级联 | Model Cascade | 先用便宜的小模型处理，判断不够好时再升级到贵的大模型。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 模型路由 | Model Routing | 根据请求的类型、难度，事先选择用哪个模型处理。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 精确缓存 | Exact-match Cache | 输入完全相同时直接返回之前的结果；安全但命中率低。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 语义缓存 | Semantic Cache | 输入"意思相近"时就返回之前的结果；命中率高，但可能返回不适用的答案，缓存键必须包含租户和权限范围。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 缓存未命中风暴 | Cache Stampede | 热点缓存过期瞬间，大量相同请求同时去计算，把后端打垮或让成本飙升。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 对冲请求 | Hedged Request | 第一个请求迟迟不返回时再发一个相同的请求，取先返回的那个；能压低长尾延迟，但只适合幂等请求。出自《The Tail at Scale》。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 批处理 | Batching | 把多个请求攒在一起处理以降低单位成本，代价是延迟变高；适合离线任务。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 首 token 延迟 | TTFT (Time to First Token) | 从发出请求到收到第一个输出 token 的时间，决定了用户"感觉"有多快。 | [第 11 课](../lessons/11_cost_latency/README.md) |
| 并行工具调用 | Parallel Tool Calls | 模型在一轮中同时请求多个互不依赖的工具，程序并发执行，减少总等待时间。 | [第 11 课](../lessons/11_cost_latency/README.md) |

## 十二、企业知识与 RAG

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 访问控制列表 | ACL (Access Control List) | 记录"谁可以访问这份文档"的列表，检索时据此过滤。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 前过滤 / 后过滤 | Pre-filtering / Post-filtering | 前过滤：检索时就只在用户有权访问的范围里找；后过滤：先检索再剔除无权的结果。前者更安全，后者容易漏或召回不足。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 身份透传 | Identity Propagation | 把发起请求的用户身份一路传到检索、工具、下游服务，让每一层都按"这个用户"的权限办事。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 切块 | Chunking | 把长文档切成适合检索的小段；切得不好（拆散条款、表格）会让模型看到残缺的上下文。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 有据性 | Groundedness | 回答中的每个陈述是否都能在检索到的资料中找到依据。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 引用校验 | Citation Verification | 检查回答给出的出处是否真实存在于本次检索结果中，并且确实支持对应的陈述。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 删除传播 | Deletion Propagation | 源数据删除后，把删除同步到所有副本：索引、缓存、摘要、评估集、日志。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |
| 知识新鲜度 | Knowledge Freshness | 知识库中的内容与源系统保持同步的程度；过期知识会让 Agent 自信地给出旧答案。 | [第 12 课](../lessons/12_enterprise_rag/README.md) |

## 十三、发布与运维

| 中文 | English | 大白话解释 | 课程 |
|---|---|---|---|
| 影子模式 | Shadow Mode | 新版本接收真实流量的副本并产生结果，但结果不返回给用户，只用于对比；零用户风险地验证新版本。 | [第 13 课](../lessons/13_release_ops/README.md) |
| A/B 测试 | A/B Testing | 把用户随机分成两组分别使用两个版本，比较业务指标，判断哪个更好。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 稳定分桶 | Sticky Bucketing | 按用户/租户 ID 的哈希分组，保证同一用户始终落在同一组，不会在新旧版本之间跳来跳去。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 发布单元 | Release Unit | 把代码、提示词、模型版本、工具 Schema、配置打包成一个版本号，一起发布、一起回滚。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 自动回滚 | Automated Rollback | 发布后关键指标（完成率、错误率、成本）越过阈值时，系统自动退回上一版本。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 功能开关 | Feature Flag | 不重新部署就能打开或关闭某个功能的配置开关；紧急开关是它的一种。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 模型漂移 | Model Drift | 在你没改任何东西的情况下，模型的行为发生了变化（如模型别名指向了新版本）。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 无责复盘 | Blameless Postmortem | 事故后聚焦"系统哪里让错误有机可乘"而不是"谁犯了错"的复盘方式，目的是防止再次发生。 | [第 13 课](../lessons/13_release_ops/README.md) |
| 数据飞轮 | Data Flywheel | 线上问题 → 标注 → 进入评估集和改进 → 上线更好的版本 → 产生新的数据，循环往复、越转越快。 | [第 13 课](../lessons/13_release_ops/README.md) |

---

## 附：容易混淆的术语对

| 术语对 | 区别 |
|---|---|
| Workflow vs Agent | 流程由代码决定 vs 由模型决定。 |
| Agent 即工具 vs 转交 | 结果交回调用者（调用者始终掌控） vs 控制权整个交给对方。 |
| 短期记忆 vs 长期记忆 | 当前会话的消息历史 vs 跨会话的外部存储。 |
| 滑动窗口 vs 压缩 | 直接丢弃旧消息 vs 把旧消息写成摘要。 |
| 护栏 vs 权限 | 检测并拦截可疑内容（降低概率） vs 限制能做的事（限制后果）。 |
| 直接注入 vs 间接注入 | 攻击来自用户输入 vs 攻击藏在 Agent 读取的外部内容里。 |
| 审计日志 vs 调试日志 | 给合规用、完整、不可篡改、不能采样 vs 给工程师用、可以采样和丢弃。 |
| pass@k vs pass^k | 至少一次成功 vs 每次都成功。 |
| 重试 vs 降级 | 同一方案再试一次 vs 换一个备用方案。 |
| 检查点 vs 会话历史 | 运行到一半的完整状态（可恢复执行） vs 对话记录（可继续聊天）。 |
| 限流 vs 配额 | 单位时间内的速率上限 vs 一个周期内的总量上限。 |
| MCP vs A2A | Agent 连接工具和数据源 vs Agent 连接其他 Agent。 |
| 至少一次 vs 恰好一次 | 可能重复但不丢 vs 只生效一次（实践中靠"至少一次 + 幂等"实现）。 |
| 分布式锁 vs 乐观锁 vs 分区串行化 | 先抢锁再写 vs 写时检查版本冲突 vs 让同一会话的消息根本不会并发。 |
| 前过滤 vs 后过滤 | 检索时就按权限圈定范围 vs 检索完再剔除（容易泄露或召回不足）。 |
| 影子模式 vs 金丝雀 vs A/B | 验证正确性（用户无感） vs 验证稳定性（小流量真实服务） vs 验证业务效果（分组对比）。 |
| 精确缓存 vs 语义缓存 vs 提示词缓存 | 输入完全相同才命中 vs 意思相近就命中 vs 模型服务商对相同前缀的计算复用。 |
