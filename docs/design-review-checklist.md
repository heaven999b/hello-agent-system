[中文](design-review-checklist.md) | [English](design-review-checklist.en.md)

# 企业级 Agent 设计评审清单

> 📖 本文是"领域参考手册"的一部分，配合课程使用。
> 相关文档：[失败模式图鉴](failure-modes.md) · [速查表](cheatsheet.md) · [面试题](interview-questions.md) · [术语表](glossary.md)

这是一份可以直接复制到 PR 描述、设计文档或评审会议纪要里的清单，共 **19 个分组、199 项**（其中 P0 62 项）。每一项都附了"为什么要查"，并尽量链接到对应的失败模式（如 [T5](failure-modes.md#t5-重复副作用duplicate-side-effects)）和课程。

## 怎么用

**严重级别**

| 级别 | 含义 | 评审规则 |
|---|---|---|
| 🔴 **P0** | 上线前必须满足 | 任何一项不满足 = 不能上线（或者有书面的风险接受记录，由业务 owner 和安全负责人签字） |
| 🟠 **P1** | 应该满足 | 不满足要有明确的补齐计划和日期，通常在首次上线后一个迭代内完成 |
| 🟢 **P2** | 建议满足 | 规模化、成熟化阶段的要求，按优先级排期 |

**建议流程**

1. **设计阶段**（写代码前）：过一遍"需求与范围""编排""安全""权限与审批""分布式与高并发"，这几组的决策最难事后修改；用到长期记忆、MCP、代码执行、编码 Agent 或主动提醒时，再加上第 19 组"扩展能力"；
2. **上线前评审**：全量过一遍，P0 逐条给出证据（链接到代码、配置、eval 报告、trace 截图），而不是口头说"有的"；
3. **季度复查**：模型、工具、用户群都会变，清单也要重新过——尤其是"评估""成本"和第 18 组"数据、评估方法论与优化"。

> 💡 一个判断评审质量的小技巧：对每个 P0，问"**如果它失效了，我们多久会发现？**"。如果答案是"用户投诉之后"，说明还缺检测手段。

**各组条目数一览**

| # | 分组 | 条目数 | 其中 P0 | 主要对应课程 |
|---|---|---|---|---|
| 1 | 需求与范围 | 9 | 4 | [第 00 课](../lessons/00_overview/README.md) · [第 06 课](../lessons/06_orchestration/README.md) |
| 2 | 模型与提示词 | 9 | 3 | [第 02 课](../lessons/02_agent_loop/README.md) · [第 11 课](../lessons/11_evals/README.md) |
| 3 | 工具 | 13 | 6 | [第 03 课](../lessons/03_tools/README.md) |
| 4 | 上下文与记忆 | 9 | 4 | [第 04 课](../lessons/04_context_memory/README.md) |
| 5 | 编排 | 8 | 3 | [第 06 课](../lessons/06_orchestration/README.md) |
| 6 | 可靠性 | 10 | 3 | [第 08 课](../lessons/08_reliability/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| 7 | 安全 | 11 | 6 | [第 09 课](../lessons/09_security/README.md) |
| 8 | 隐私与合规 | 9 | 3 | [第 09 课](../lessons/09_security/README.md) · [第 12 课](../lessons/12_production_architecture/README.md) |
| 9 | 权限与审批 | 8 | 4 | [第 09 课](../lessons/09_security/README.md) |
| 10 | 可观测性 | 9 | 2 | [第 10 课](../lessons/10_observability/README.md) |
| 11 | 评估 | 10 | 2 | [第 11 课](../lessons/11_evals/README.md) |
| 12 | 成本 | 10 | 2 | [第 08 课](../lessons/08_reliability/README.md) · [第 14 课](../lessons/14_cost_latency/README.md) |
| 13 | 部署与运维 | 13 | 2 | [第 12 课](../lessons/12_production_architecture/README.md) · [第 16 课](../lessons/16_release_ops/README.md) |
| 14 | 多租户 | 7 | 2 | [第 12 课](../lessons/12_production_architecture/README.md) · [第 15 课](../lessons/15_enterprise_rag/README.md) |
| 15 | 文档与交接 | 7 | 1 | [第 12 课](../lessons/12_production_architecture/README.md) |
| 16 | 分布式与高并发 | 12 | 4 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| 17 | 企业知识与 RAG | 9 | 2 | [第 15 课](../lessons/15_enterprise_rag/README.md) |
| 18 | 数据、评估方法论与优化 | 16 | 3 | [第 21 课](../lessons/21_agent_data/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) · [第 23 课](../lessons/23_optimization/README.md) |
| 19 | 扩展能力（检索 / 记忆 / MCP / 代码执行 / 编码 Agent / 主动式） | 20 | 6 | [第 17 课](../lessons/17_retrieval_quality/README.md) · [第 18 课](../lessons/18_memory_systems/README.md) · [第 19 课](../lessons/19_mcp_and_sandbox/README.md) · [第 20 课](../lessons/20_frameworks_bridge/README.md) · [第 24 课](../lessons/24_coding_agents/README.md) · [第 25 课](../lessons/25_proactive_and_frontier/README.md) |
| | **合计** | **199** | **62** | |

---

## 1. 需求与范围

- [ ] 🔴 **P0** 用一页纸写清 Agent 的职责边界：能做什么、**明确不做什么**、服务谁。
  —— 边界不清的 Agent 会被用户推着越做越多，风险面随之无限扩大，评审也无从下手。
- [ ] 🔴 **P0** 书面论证"为什么需要 Agent，而不是 Workflow 或普通代码"。
  —— 流程可预知却用 Agent，会付出更高的成本和不确定性，却拿不到收益（[O1](failure-modes.md#o1-过度-agent-化over-agentification)）。
- [ ] 🔴 **P0** 定义可量化的上线标准：任务完成率、准确率、转人工率、p95 延迟、单次成本的目标值。
  —— 没有标准，就无法判断"能不能上线"，也无法判断一次修改是变好还是变坏。
- [ ] 🔴 **P0** 设计兜底路径：Agent 处理不了、不确定、出错时，转人工或给出明确的替代方案。
  —— Agent 一定会遇到处理不了的情况，没有出口时它会编造答案（[M5](failure-modes.md#m5-政策幻觉policy-hallucination)）或原地打转（[M3](failure-modes.md#m3-循环与重复调用tool-call-loop)）。
- [ ] 🟠 **P1** 对"答错 / 做错 / 泄露"三类失败分别评估后果等级。
  —— 后果等级决定需要多少护栏、审批和评估投入；只会回答问题的 Agent 和能改生产配置的 Agent 不是一个量级。
- [ ] 🟠 **P1** 识别所有利益相关方：业务 owner、安全、法务/合规、数据 owner、审批人、值班团队。
  —— 企业里 Agent 上线的卡点往往在非技术方，越晚拉进来返工越大。
- [ ] 🟠 **P1** 估算流量：日活、峰值并发会话、平均每次运行的步数和 token。
  —— 这决定了模型配额、成本预算、是否需要异步化，也是成本评审的输入。
- [ ] 🟠 **P1** 明确用户群体和接入渠道（内部员工 / 外部客户；Web / IM / API / 邮件）。
  —— 面向外部用户的攻击面、合规要求和品牌风险与内部工具完全不同。
- [ ] 🟢 **P2** 定义交互形态：同步对话、异步任务、后台自动触发，各自的延迟目标是多少。
  —— 延迟目标直接影响模型选型、最大步数和是否需要流式输出（[P4](failure-modes.md#p4-长尾延迟爆炸tail-latency-blowup)）。

## 2. 模型与提示词

- [ ] 🔴 **P0** 生产环境固定具体的模型快照版本，不使用会自动指向新版本的别名。
  —— 否则模型会在你什么都没改的情况下变化（[E5](failure-modes.md#e5-模型静默漂移silent-model-drift)）。
- [ ] 🔴 **P0** 提示词（system prompt、工具描述、few-shot 示例）纳入版本控制，修改走代码评审 + 评估。
  —— 提示词就是代码，而且是影响全局的代码（[E4](failure-modes.md#e4-修一坏三prompt-regression)）。
- [ ] 🔴 **P0** system prompt 中不包含密钥、内部地址、未公开的业务规则等"泄露了就出事"的内容。
  —— 请假设 system prompt 一定会被套出来（OWASP LLM07 System Prompt Leakage，[S1](failure-modes.md#s1-直接提示词注入direct-prompt-injection)）。
- [ ] 🟠 **P1** 提示词中写明行为准则："信息不足时追问""不确定时说明""无法处理时转人工""操作以工具结果为准"。
  —— 这几句话直接对应最常见的几类失败（[M1](failure-modes.md#m1-编造行动phantom-action)、[M4](failure-modes.md#m4-参数幻觉hallucinated-arguments)、[M5](failure-modes.md#m5-政策幻觉policy-hallucination)）。
- [ ] 🟠 **P1** 需要结构化数据的环节使用原生结构化输出或 Schema 校验 + 修复循环。
  —— 下游代码需要可靠的数据结构，不是"大多数时候是 JSON"的文本（[M7](failure-modes.md#m7-结构化输出破损malformed-structured-output)）。
- [ ] 🟠 **P1** 模型选型有评估数据支撑，包括主模型和所有备用模型。
  —— "感觉这个模型更聪明"不是选型依据；备用模型没评估过，降级时就会静默变差（[R3](failure-modes.md#r3-降级后静默变差silent-degradation)）。
- [ ] 🟠 **P1** 采样参数（如 temperature）是有意识的选择，并记录在配置中。
  —— 影响输出稳定性和问题复现；不同模型对参数的支持也不同，要确认网关实际生效的值。
- [ ] 🟢 **P2** 提示词结构遵循"稳定内容在前、动态内容在后"，以利于提示词缓存。
  —— 主流厂商的缓存基于前缀匹配，开头放时间戳会让缓存全部失效（[B2](failure-modes.md#b2-缓存击穿prompt-cache-busting)）。
- [ ] 🟢 **P2** few-shot 示例定期复查，避免模型机械模仿示例的格式和路径。
  —— Manus 团队的经验文章专门提醒过这个问题（"Don't Get Few-Shotted"）。

## 3. 工具

- [ ] 🔴 **P0** 每个工具都有面向模型的清晰描述：做什么、何时用、**何时不用**、参数含义与示例。
  —— 工具描述是模型选工具的唯一依据，含糊的描述直接导致选错工具（[T1](failure-modes.md#t1-工具选错wrong-tool-selection)）。
- [ ] 🔴 **P0** 所有参数经过 Schema 校验：类型、必填、枚举、取值范围、禁止多余字段。
  —— 模型输出的参数只是"生成的文本"，必须当作不可信输入校验（[M4](failure-modes.md#m4-参数幻觉hallucinated-arguments)）。
- [ ] 🔴 **P0** 身份、租户、角色等授权相关参数**不出现在工具 Schema 中**，由系统从认证上下文注入。
  —— 让模型填 user_id 等于让攻击者决定"我是谁"（[S4](failure-modes.md#s4-身份由模型决定confused-deputy)）。agentkit 用 `ToolContext` 实现。
- [ ] 🔴 **P0** 每个工具标注风险等级（如 read / write / dangerous）。
  —— 这是权限控制、审批、审计、幂等策略的前提。
- [ ] 🔴 **P0** 所有写工具支持幂等，幂等键尽量传递到真正产生副作用的下游系统。
  —— 重试和崩溃恢复一定会重放调用，没有幂等就会重复扣款/建单（[T5](failure-modes.md#t5-重复副作用duplicate-side-effects)）。
- [ ] 🔴 **P0** 每个工具都有超时。
  —— 一个挂起的下游接口可以拖垮整个 Agent 服务（[T4](failure-modes.md#t4-慢工具与挂起hanging-tool)）。
- [ ] 🟠 **P1** 工具输出有长度上限，被截断时明确告诉模型；支持分页/过滤。
  —— 防止单次工具输出撑爆上下文和账单（[T3](failure-modes.md#t3-输出爆炸tool-output-explosion)）。
- [ ] 🟠 **P1** 工具错误返回模型能理解、能据此行动的文字，且不泄露堆栈、SQL、内部路径。
  —— 不透明的错误会让模型重试死循环或假装成功（[T6](failure-modes.md#t6-不透明错误opaque-errors)）。
- [ ] 🟠 **P1** 需要原子性的多步写操作，封装为单个工具（服务端事务/Saga）或固定 Workflow。
  —— 不要让模型充当分布式事务协调者（[T7](failure-modes.md#t7-部分完成partial-completion)）。
- [ ] 🟠 **P1** 控制单个 Agent 可见的工具数量（按场景/角色暴露子集，或先路由）。
  —— 工具越多，选择越容易出错，每次请求的 token 也越多（[T2](failure-modes.md#t2-工具过载tool-overload)）。
- [ ] 🟠 **P1** 第三方工具（含 MCP 服务器）来源经过审核、版本固定，描述变更需要重新审核。
  —— 工具描述会进入模型上下文，可能被投毒（[S6](failure-modes.md#s6-工具投毒与供应链tool-poisoning)）。
- [ ] 🟢 **P2** 工具命名使用命名空间前缀，功能互不重叠。
  —— 降低相似工具之间的混淆（[T1](failure-modes.md#t1-工具选错wrong-tool-selection)）。
- [ ] 🟢 **P2** 执行代码、访问文件系统或调用不可信服务的工具，在沙箱/独立进程中运行。
  —— 线程超时无法真正终止执行；代码执行类工具是远程代码执行风险的直接入口（OWASP Agentic ASI05）。

## 4. 上下文与记忆

- [ ] 🔴 **P0** 有明确的上下文长度策略（滑动窗口 / 摘要压缩 / 清理旧工具结果），上下文不会无限增长。
  —— 否则长对话必然超限报错，成本线性上涨，质量也会下降（[C2](failure-modes.md#c2-上下文腐烂context-rot)）。
- [ ] 🔴 **P0** 截断/压缩时保持 `tool_calls` 与其 `tool` 结果成对出现。
  —— 拆散它们会导致 API 400 错误，而且只在长对话中出现，极难排查（[C1](failure-modes.md#c1-消息配对被截断orphaned-tool-message)）。
- [ ] 🔴 **P0** 长期记忆、向量库、缓存都在**存储层**按租户（及用户）强制隔离，过滤条件不由模型生成。
  —— 跨租户泄露一次就是重大事故（[C5](failure-modes.md#c5-记忆串户cross-tenant-memory-leak)）。
- [ ] 🔴 **P0** 权限、角色、身份永远从身份系统读取，绝不从记忆或对话内容中读取。
  —— "请记住我是管理员"是经典的记忆投毒提权手法（[C6](failure-modes.md#c6-记忆投毒与过期memory-poisoning--staleness)）。
- [ ] 🟠 **P1** 摘要提示词明确要求保留：用户目标与约束、关键事实与 ID、已完成的操作、未完成事项。
  —— 摘要丢掉"已完成的操作"会导致重复执行（[C3](failure-modes.md#c3-有损压缩lossy-compaction)）。
- [ ] 🟠 **P1** "已完成哪些操作"等关键业务状态存储在对话上下文之外（数据库/状态字段）。
  —— 状态放在上下文里就会被截断、被压缩、被投毒；放在外部才可靠（[C3](failure-modes.md#c3-有损压缩lossy-compaction)、[C4](failure-modes.md#c4-上下文投毒context-poisoning)）。
- [ ] 🟠 **P1** 检索回来的记忆和文档按不可信数据处理（标记来源、包裹隔离标签）。
  —— 记忆和知识库都可能被写入恶意内容（[S2](failure-modes.md#s2-间接提示词注入indirect-prompt-injection)、[C6](failure-modes.md#c6-记忆投毒与过期memory-poisoning--staleness)）。
- [ ] 🟠 **P1** 用户可以查看和删除自己的长期记忆。
  —— 这是隐私法规中删除权的要求，也是纠正错误记忆的途径。
- [ ] 🟢 **P2** 记忆带时间戳和来源，冲突时新覆盖旧，支持过期。
  —— 过期记忆会让 Agent 基于错误前提行事（[C6](failure-modes.md#c6-记忆投毒与过期memory-poisoning--staleness)）。

## 5. 编排

- [ ] 🔴 **P0** 选择的编排形态（Workflow / 单 Agent / 多 Agent）有书面理由，且优先选择更简单的方案。
  —— 每升一级复杂度，成本、延迟、不可预测性都会上升（[O1](failure-modes.md#o1-过度-agent-化over-agentification)，决策树见[速查表](cheatsheet.md)）。
- [ ] 🔴 **P0** 设置最大步数（max_steps），触达上限时给用户明确、友好的结局。
  —— 这是防止死循环烧钱的最后一道硬防线（[M3](failure-modes.md#m3-循环与重复调用tool-call-loop)）。
- [ ] 🔴 **P0** 身份与权限沿委派链透传，子 Agent 的有效权限不超过发起用户的权限。
  —— 否则委派就成了提权通道（[S8](failure-modes.md#s8-委派中的权限放大privilege-escalation-via-delegation)）。
- [ ] 🟠 **P1** 多 Agent 场景下，预算在整棵调用树上共享，委派深度有上限。
  —— 嵌套 Agent 的步数是相乘的，转交关系成环时没有上限（[O4](failure-modes.md#o4-无界委派unbounded-delegation)）。
- [ ] 🟠 **P1** 委派时传递完整的任务上下文：目标、已知事实、约束、期望的输出格式。
  —— 子 Agent 看不到主管的对话历史（[O2](failure-modes.md#o2-委派上下文饥饿delegation-context-starvation)）。
- [ ] 🟠 **P1** 并行分支只做读取和分析，写操作由单一决策者串行执行。
  —— 并行写操作会产生相互冲突的隐含决策（[O3](failure-modes.md#o3-并行决策冲突conflicting-parallel-decisions)）。
- [ ] 🟠 **P1** 关键产出有验证环节，优先使用确定性检查（测试、校验、对账）。
  —— 没人验收的产出等于没有质量保证（[O5](failure-modes.md#o5-无人验收missing-verification)）。
- [ ] 🟢 **P2** 生成-评审类循环有轮数上限，超限后转人工而不是"凑合交付"。
  —— 评审永远不通过时，循环会一直烧钱（[O5](failure-modes.md#o5-无人验收missing-verification)）。

## 6. 可靠性

- [ ] 🔴 **P0** 模型调用有重试：只重试可重试的错误（429、5xx、超时），指数退避 + 抖动。
  —— 模型 API 限流和抖动是常态；但重试 400/401 只会浪费时间和钱（[R2](failure-modes.md#r2-重试了不该重试的错误retrying-non-retryable-errors)）。
- [ ] 🔴 **P0** 重试只在一层发生（关闭 SDK 自带重试或统一收口），并记录每次重试。
  —— 多层重试会让故障被成倍放大（[R1](failure-modes.md#r1-重试风暴retry-storm)）。
- [ ] 🔴 **P0** 运行状态每一步都写检查点（持久化存储），崩溃或发布后可以恢复。
  —— 长任务、等待审批的任务不能因为一次重启就全部丢失（[R4](failure-modes.md#r4-中断后从头重来lost-progress)）。
- [ ] 🟠 **P1** 有熔断器，下游持续失败时快速失败。
  —— 让用户等满超时再失败，既浪费资源，也让下游无法恢复（[R1](failure-modes.md#r1-重试风暴retry-storm)）。
- [ ] 🟠 **P1** 有降级方案（备用模型 / 缓存 / 规则兜底 / 转人工），且降级路径通过同一套评估。
  —— 未经评估的降级只是把"报错"换成了"静默答错"（[R3](failure-modes.md#r3-降级后静默变差silent-degradation)）。
- [ ] 🟠 **P1** 检查点原子写入，并记录代码 / 提示词 / 工具集的版本号。
  —— 半写入的检查点无法恢复；版本不匹配的恢复会出现"工具不存在"（[R6](failure-modes.md#r6-恢复时版本错位version-skew-on-resume)）。
- [ ] 🟠 **P1** 所有非正常结束（超步数、超预算、被拦截、模型不可用）都收敛为明确的状态和对用户友好的提示。
  —— 用户不应该看到 500 或堆栈；调用方需要根据状态决定下一步（agentkit 的 `RunResult.status`）。
- [ ] 🟠 **P1** 暂停中的运行（如等待审批）有超时和过期处理。
  —— 否则会堆积大量永远不会完成的运行（[R5](failure-modes.md#r5-审批悬挂approval-limbo)）。
- [ ] 🟠 **P1** 客户端断开或用户取消时，能取消后台运行。
  —— 用户已经离开，Agent 却还在继续花钱和执行操作。
- [ ] 🟢 **P2** 定期做故障演练：模拟限流、超时、下游宕机、进程被杀。
  —— 重试、熔断、恢复这些机制，不演练就不知道是否真的有效。

## 7. 安全

- [ ] 🔴 **P0** 完成威胁建模：列出 Agent 会读取的**所有不可信来源**（用户输入、网页、邮件、文档、工单、第三方 API、记忆）。
  —— 间接注入的入口就是这些来源，没列出来就无法防御（[S2](failure-modes.md#s2-间接提示词注入indirect-prompt-injection)）。
- [ ] 🔴 **P0** 检查"致命三要素"：同一个 Agent 是否同时具备私有数据访问、接触不可信内容、对外通信能力；如是，必须在设计上切断至少一个。
  —— 三者同时存在时，数据外泄只差一次成功的注入（[S3](failure-modes.md#s3-致命三要素外泄lethal-trifecta-exfiltration)）。
- [ ] 🔴 **P0** 读取不可信内容之后，有副作用的操作需要人工确认或受到严格限制。
  —— 检测一定会漏，限制"被骗后能做什么"才是底线（[S2](failure-modes.md#s2-间接提示词注入indirect-prompt-injection)）。
- [ ] 🔴 **P0** 前端不渲染模型输出中的任意外部图片/链接，或仅允许白名单域名。
  —— Markdown 图片是最常见的零点击外泄通道之一（[S3](failure-modes.md#s3-致命三要素外泄lethal-trifecta-exfiltration)）。
- [ ] 🔴 **P0** 最终输出经过密钥和敏感信息检测。
  —— 模型可能把上下文中的密钥、个人信息原样输出（[S7](failure-modes.md#s7-敏感信息泄露sensitive-information-disclosure)）。
- [ ] 🔴 **P0** 密钥只存放在密钥管理服务或环境变量中，不进代码、提示词、工具返回值和上下文。
  —— 进了上下文的东西，就可能出现在输出、日志和 trace 里（[S7](failure-modes.md#s7-敏感信息泄露sensitive-information-disclosure)）。
- [ ] 🟠 **P1** 输入层有注入特征检测和长度上限。
  —— 这是成本最低的第一层防线，能拦住大量低水平攻击——但要清楚它一定会漏（[S1](failure-modes.md#s1-直接提示词注入direct-prompt-injection)）。
- [ ] 🟠 **P1** 工具输出被标记为不可信数据（如包裹在隔离标签中），并防止标签逃逸。
  —— 帮助模型区分"数据"和"指令"（spotlighting），但它降低的是概率而不是消除风险（[S2](failure-modes.md#s2-间接提示词注入indirect-prompt-injection)）。
- [ ] 🟠 **P1** 有红队测试集（直接注入、间接注入、越权、数据外泄），并纳入 CI 持续运行。
  —— 安全防护会随着提示词和模型的修改而退化，必须持续验证。
- [ ] 🟠 **P1** 有紧急开关：可以在分钟级内全局禁用某个工具或整个 Agent。
  —— 发现工具漏洞或正在被攻击时，需要立刻止血（agentkit `PermissionPolicy(deny_tools=...)`）。
- [ ] 🟢 **P2** 对照 OWASP《Top 10 for LLM Applications 2025》和《Top 10 for Agentic Applications (2026)》逐项检查。
  —— 业界共识的风险清单，适合作为安全评审的补充视角。

## 8. 隐私与合规

- [ ] 🔴 **P0** 画出数据流图：Agent 会接触哪些个人信息/敏感数据、流向哪些系统（含模型供应商）、各自用途。
  —— 不知道数据去了哪里，就不可能保护它，也无法回答合规审查。
- [ ] 🔴 **P0** 确认模型供应商的数据条款：数据保留期、是否用于训练、数据存储地域。
  —— 把用户数据发给第三方模型本身就是一次数据处理/共享行为，可能涉及跨境传输。
- [ ] 🔴 **P0** 日志、trace、审计记录、评估数据集中的个人信息已脱敏或有严格的访问控制。
  —— 这些地方的访问控制通常比生产数据库松，却存着同样的数据（[S7](failure-modes.md#s7-敏感信息泄露sensitive-information-disclosure)）。
- [ ] 🟠 **P1** 所有存储（对话历史、记忆、检查点、日志、trace）都有保留期限并自动清理。
  —— 数据存得越久，泄露面越大；多数隐私法规要求数据最小化。
- [ ] 🟠 **P1** 用户的删除/查阅请求能覆盖到记忆、对话历史、检查点和日志。
  —— GDPR、《个人信息保护法》等法规都规定了个人的删除权，而 Agent 系统的数据散落在很多地方。
- [ ] 🟠 **P1** 面向外部用户时，明确告知用户正在与 AI 交互。
  —— 这是用户信任的基础，部分法规（如欧盟《人工智能法》第 50 条）对此有明确要求。
- [ ] 🟠 **P1** 承诺类输出（价格、退款、赔偿、政策解释）必须有工具返回的依据，否则转人工确认。
  —— 企业可能要为 Agent 说的话承担责任（Moffatt v. Air Canada 案，[M5](failure-modes.md#m5-政策幻觉policy-hallucination)）。
- [ ] 🟢 **P2** 跨境数据传输满足所在地法规要求（如需要境内部署或专门的合规评估）。
  —— 选择海外模型服务时尤其需要法务确认。
- [ ] 🟢 **P2** 法务/合规评审过 Agent 的责任边界、免责声明和人工复核流程。
  —— 技术护栏之外，还需要流程和法律层面的兜底。

## 9. 权限与审批

- [ ] 🔴 **P0** 按角色实施最小权限：无权使用的工具既**不展示给模型**，也**在执行时拦截**。
  —— 只做一层不够：只隐藏可能被猜到名字直接调用，只拦截则会让模型反复尝试（[S5](failure-modes.md#s5-过度授权excessive-agency)）。
- [ ] 🔴 **P0** 高风险（dangerous）操作必须人工审批，风险等级在工具定义处声明，不由模型判断。
  —— 不可逆操作不能完全交给一个可能被骗的模型（[S5](failure-modes.md#s5-过度授权excessive-agency)）。
- [ ] 🔴 **P0** 审批流程是异步的：暂停 → 状态落盘 → 通知审批人 → 批准后恢复，而不是阻塞线程等待。
  —— 审批人可能一小时后才看到，阻塞等待会耗尽资源，重启后还会丢失（[R5](failure-modes.md#r5-审批悬挂approval-limbo)）。
- [ ] 🔴 **P0** 审批决定（审批人身份、时间、决定、理由）完整进入审计日志。
  —— 出事后第一个问题就是"谁批准的"（[P5](failure-modes.md#p5-审计断链broken-audit-trail)）。
- [ ] 🟠 **P1** 审批界面展示人类可读的操作摘要和影响范围，而不是一段原始 JSON。
  —— 看不懂的审批请求只会被机械地点"同意"，审批形同虚设。
- [ ] 🟠 **P1** 批准后、执行前重新校验前置条件（资源是否存在、权限是否仍有效、状态是否已变化）。
  —— 从暂停到批准之间，世界可能已经变了（[R5](failure-modes.md#r5-审批悬挂approval-limbo)）。
- [ ] 🟠 **P1** 高风险操作的审批人不能是发起人本人（职责分离）。
  —— 否则审批只是让攻击者多点一次按钮。
- [ ] 🟢 **P2** 监控审批通过率和审批时长，警惕"橡皮图章"。
  —— 通过率接近 100%、审批时间只有几秒，说明审批人已经不看内容了，需要减少不必要的审批或改进审批信息。

## 10. 可观测性

- [ ] 🔴 **P0** 每次运行都有完整 trace：每次模型调用（模型、token、耗时、结束原因）和每次工具调用（参数、结果、耗时、错误类型）。
  —— Agent 是非确定性的，没有 trace 就无法复现和排查任何问题（[P2](failure-modes.md#p2-无法复现unreproducible-incident)）。
- [ ] 🔴 **P0** 运行状态和结束原因（如 completed / max_steps / stopped / failed / paused）作为一级指标上报并建看板。
  —— Agent 的失败大多以"正常响应"的形式出现，HTTP 成功率说明不了问题（[P1](failure-modes.md#p1-静默失败silent-failure)）。
- [ ] 🟠 **P1** trace 字段遵循通用约定（如 OpenTelemetry GenAI 语义约定）。
  —— 便于切换观测后端，也便于和现有 APM 体系打通。
- [ ] 🟠 **P1** trace ID 返回给前端/客服系统，用户投诉时能一键定位。
  —— 把"用户说它答错了"变成"打开这条 trace 看它当时看到了什么"（[P2](failure-modes.md#p2-无法复现unreproducible-incident)）。
- [ ] 🟠 **P1** 关键信号有告警：错误率、max_steps 比例、成本突增、降级事件、注入检测命中、审批积压。
  —— 这些都是事故的早期信号。
- [ ] 🟠 **P1** trace 和日志中的敏感数据已脱敏，大参数已截断。
  —— 观测数据是敏感数据泄露的高发地（[S7](failure-modes.md#s7-敏感信息泄露sensitive-information-disclosure)）。
- [ ] 🟠 **P1** 每次运行记录实际响应的模型版本、提示词版本、工具集版本。
  —— 行为变化时第一个问题就是"什么变了"（[E5](failure-modes.md#e5-模型静默漂移silent-model-drift)）。
- [ ] 🟢 **P2** 有业务级看板：任务完成率、转人工率、用户反馈、重复提问率。
  —— 技术指标正常不代表用户满意（[P1](failure-modes.md#p1-静默失败silent-failure)）。
- [ ] 🟢 **P2** 明确采样策略：调试 trace 可以采样，审计日志不能采样。
  —— 两者目的不同：一个为排查，一个为合规追溯。

## 11. 评估

- [ ] 🔴 **P0** 有评估集，覆盖主要意图、边界情况（信息不足、无答案）、恶意输入和高风险操作场景。
  —— 没有评估的 Agent 开发就是凭感觉改提示词。
- [ ] 🔴 **P0** 评估接入 CI：通过率低于阈值或出现回归（以前通过、现在失败）时阻止合并/发布。
  —— 改一处坏三处是提示词工程的常态（[E4](failure-modes.md#e4-修一坏三prompt-regression)）。
- [ ] 🟠 **P1** 评估集持续从线上 bad case（差评、转人工、失败运行）回流补充。
  —— 开发者想象的问题和真实用户的问题分布不同（[E1](failure-modes.md#e1-评估集脱节evalproduction-skew)）。
- [ ] 🟠 **P1** 同时评估最终结果和执行轨迹（必须调用 / 禁止调用的工具、调用顺序）。
  —— 只看结果会漏掉"答对了但做了危险操作"或"说做了其实没做"（[M1](failure-modes.md#m1-编造行动phantom-action)）。
- [ ] 🟠 **P1** 每个用例运行多次，同时关注 pass@k 和 pass^k。
  —— 单次运行的通过具有偶然性（[E2](failure-modes.md#e2-单次运行的假象flaky-single-run-evals)）。
- [ ] 🟠 **P1** LLM 评委使用具体的评分细则，与被测模型不同，并定期用人工标注校准。
  —— LLM 评委存在位置、冗长、自我偏好等偏差（[E3](failure-modes.md#e3-评委偏差llm-judge-bias)）。
- [ ] 🟠 **P1** 换模型、改提示词、改工具描述、升级依赖都会触发全量评估。
  —— 这几类变更对行为的影响都是全局性的。
- [ ] 🟠 **P1** 评估覆盖降级链上的所有模型。
  —— 备用模型在真正降级那天才第一次被"测试"是灾难（[R3](failure-modes.md#r3-降级后静默变差silent-degradation)）。
- [ ] 🟢 **P2** 评估报告同时包含正确率、成本、延迟、步数。
  —— 正确率提升 2% 但成本翻倍，不一定值得。
- [ ] 🟢 **P2** 上线后有线上评估：抽样打分、用户反馈、A/B 实验。
  —— 离线评估永远只是线上的近似。

## 12. 成本

- [ ] 🔴 **P0** 每次运行有多维预算：步数、token、金额、工具调用次数、墙钟时长。
  —— 只限制一个维度总有漏洞（[B1](failure-modes.md#b1-成本失控runaway-cost)）。
- [ ] 🔴 **P0** 有用户/租户/每日维度的配额。
  —— 单次运行的上限挡不住"大量正常请求"或恶意刷量（[B1](failure-modes.md#b1-成本失控runaway-cost)、[P3](failure-modes.md#p3-吵闹邻居noisy-neighbor)）。
- [ ] 🟠 **P1** 成本可以按租户、功能、模型、提示词版本归因。
  —— 看不清钱花在哪里，就无法优化，也无法定价（[B3](failure-modes.md#b3-成本不可归因unattributable-cost)）。
- [ ] 🟠 **P1** 监控提示词缓存命中率。
  —— 缓存能显著降低成本和延迟，但很容易被无意中击穿（[B2](failure-modes.md#b2-缓存击穿prompt-cache-busting)）。
- [ ] 🟠 **P1** 按子任务分层选型，简单任务用小模型，并有评估数据支撑。
  —— 意图分类用最贵的模型是常见的浪费（[B4](failure-modes.md#b4-杀鸡用牛刀model-over-provisioning)）。
- [ ] 🟢 **P2** 建立成本预测：单次运行成本分布 × 预期流量，并与业务价值对比。
  —— 在上线前就知道这个 Agent 在经济上是否成立。
- [ ] 🟢 **P2** 成本异常告警（按小时环比、按租户突增）。
  —— 成本失控通常在几小时内就能造成可观损失。
- [ ] 🟠 **P1** 使用缓存时区分三类并分别评估：精确缓存、语义缓存、提示词缓存；语义缓存有正确性评估（命中了但答错的比例）。
  —— 三者的收益和风险完全不同，语义缓存"相似但不相同"的命中可能直接给出错误答案（[D5](failure-modes.md#d5-缓存跨租户泄露cross-tenant-cache-leak)）。
- [ ] 🟠 **P1** 对冲请求（hedged requests）只用于只读、幂等的调用，并有触发阈值和取消机制。
  —— 对冲会复制调用，用在写操作上就是重复副作用，用得太激进就是成本翻倍（[D10](failure-modes.md#d10-对冲请求放大副作用hedging-side-effects)）。
- [ ] 🟢 **P2** 模型级联/路由（先小模型、必要时升级到大模型）的升级条件有评估数据支撑。
  —— 升级条件设得不对，要么质量下降，要么省不了钱（[B4](failure-modes.md#b4-杀鸡用牛刀model-over-provisioning)）。

## 13. 部署与运维

- [ ] 🔴 **P0** 支持灰度发布（按比例 / 按租户）和快速回滚。
  —— Agent 的行为变化难以完全在离线环境预测。
- [ ] 🔴 **P0** 提示词、模型版本、工具定义的变更与代码变更同等对待：版本化、评审、可回滚。
  —— 很多线上事故来自"只改了一句提示词"。
- [ ] 🟠 **P1** 长时间运行的任务有专门的发布策略（新旧版本并行 / 运行固定在启动时版本 / 优雅排空）。
  —— 发布时正在运行的 Agent 不能被打断或以错位的版本恢复（[R6](failure-modes.md#r6-恢复时版本错位version-skew-on-resume)）。
- [ ] 🟠 **P1** 有运行手册（runbook），覆盖：模型服务宕机、成本暴涨、注入攻击、数据泄露、错误操作回滚。
  —— 凌晨三点的值班人员需要的是步骤，而不是架构图。
- [ ] 🟠 **P1** 模型访问经过统一网关，集中做限流、计量、路由、密钥管理。
  —— 分散在各服务中的模型调用无法统一治理（[P3](failure-modes.md#p3-吵闹邻居noisy-neighbor)）。
- [ ] 🟠 **P1** 长任务使用流式输出或异步通知，避免前端/网关超时。
  —— Agent 的长尾延迟远大于普通 API（[P4](failure-modes.md#p4-长尾延迟爆炸tail-latency-blowup)）。
- [ ] 🟢 **P2** 定期清理孤儿运行、过期检查点和过期审批。
  —— 否则存储持续膨胀，数据也超出保留期（[R5](failure-modes.md#r5-审批悬挂approval-limbo)）。
- [ ] 🟢 **P2** 监控外部依赖的健康状况（模型供应商状态、MCP 服务器、下游 API）。
  —— 很多"Agent 故障"其实是依赖故障，越早知道越好。
- [ ] 🟠 **P1** 代码 + 提示词 + 模型版本 + 工具 Schema + 关键配置作为**一个版本化的发布单元**，一起灰度、一起回滚。
  —— 分开发布就会分开回滚，回滚一半等于没回滚（[D11](failure-modes.md#d11-回滚不彻底incomplete-rollback)）。
- [ ] 🟠 **P1** 灰度/实验按稳定标识（用户、租户、会话）哈希分桶，会话和运行在生命周期内锁定版本。
  —— 按请求随机分流会让同一会话在新旧版本间跳来跳去，实验数据也不可信（[D6](failure-modes.md#d6-灰度分桶不稳定unstable-canary-bucketing)）。
- [ ] 🟠 **P1** 设置基于指标的自动回滚条件（如任务完成率、错误率、成本超过阈值）。
  —— 等人发现问题再手动回滚，通常已经影响了大量用户。
- [ ] 🟠 **P1** 重大事故做无责复盘，复盘产出的 bad case 进入评估集（数据飞轮）。
  —— 同一类事故不应该发生第二次；评估集是把教训固化下来的地方。
- [ ] 🟢 **P2** 明确影子模式、金丝雀、A/B 测试各自的使用场景（影子验证安全性与正确性，金丝雀验证稳定性，A/B 验证业务效果）。
  —— 三者回答的是不同的问题，混用会得出错误结论。

## 14. 多租户

- [ ] 🔴 **P0** 租户 ID 来自认证系统，并贯穿所有工具调用、存储、检索和缓存键。
  —— 任何一处遗漏都是跨租户泄露的通道（[C5](failure-modes.md#c5-记忆串户cross-tenant-memory-leak)）。
- [ ] 🔴 **P0** 有自动化的跨租户越权测试（例如在各租户数据中埋入唯一的"金丝雀"字符串，验证其他租户永远查不到）。
  —— 隔离是否有效必须用测试证明，而不是靠代码审查"看起来没问题"。
- [ ] 🟠 **P1** 租户级限流与配额，交互式请求与批处理请求分队列。
  —— 防止一个租户拖垮所有租户（[P3](failure-modes.md#p3-吵闹邻居noisy-neighbor)）。
- [ ] 🟠 **P1** 租户级配置（可用工具、模型、提示词定制）受控、可审计。
  —— 租户自定义提示词本身也是一个注入入口。
- [ ] 🟠 **P1** 提供按租户的用量与成本报表。
  —— 这是计费、容量规划和发现异常的基础（[B3](failure-modes.md#b3-成本不可归因unattributable-cost)）。
- [ ] 🟢 **P2** 高敏感租户可选择独立部署或独立数据存储。
  —— 逻辑隔离与物理隔离的选择取决于客户的合规要求。
- [ ] 🟢 **P2** 租户注销时可以完整删除其所有数据（记忆、历史、检查点、日志、评估数据）。
  —— 合同和法规层面的要求，且事后补做非常困难。

## 15. 文档与交接

- [ ] 🔴 **P0** 有架构图和数据流图，标出信任边界（哪些数据来自可信来源，哪些不可信）。
  —— 安全评审和事故排查都从这张图开始。
- [ ] 🟠 **P1** 每个工具有明确的 owner 和下游依赖说明。
  —— 工具出问题时要知道找谁；下游接口变更时要知道影响哪些 Agent。
- [ ] 🟠 **P1** 用架构决策记录（ADR）记下关键设计决策及其理由（为什么用 Agent、为什么选这个模型、为什么这样划分权限）。
  —— 半年后没人记得当初为什么这么做，而约束条件可能已经变了。
- [ ] 🟠 **P1** 值班人员接受过培训：能看懂 trace，能按 runbook 处置常见事故。
  —— 系统只有设计者能维护，就无法规模化。
- [ ] 🟠 **P1** 评估集的结构、标注规范和补充流程有文档，新成员可以独立添加用例。
  —— 评估集需要持续维护，不能依赖某一个人。
- [ ] 🟢 **P2** 维护"已知限制与失败模式"清单（可以从本仓库的[失败模式图鉴](failure-modes.md)裁剪）。
  —— 让产品、客服、用户对 Agent 的能力边界有正确预期。
- [ ] 🟢 **P2** 有面向用户的说明：Agent 能做什么、不能做什么、如何转人工、数据如何使用。
  —— 正确的预期能减少大量投诉和误用。

## 16. 分布式与高并发

- [ ] 🔴 **P0** Worker 无状态，运行状态、会话历史、检查点全部外置到共享存储。
  —— 这是横向扩展和故障转移的前提；状态留在进程内，扩容和重启都会出问题。
- [ ] 🔴 **P0** 同一会话的并发写有明确的控制方案：按会话分区串行化、乐观锁（版本号 CAS），或带 fencing token 的锁。
  —— 用户连续发消息、审批与新消息同时到达，都会触发并发写（[D1](failure-modes.md#d1-丢失更新lost-update)）。
- [ ] 🔴 **P0** 明确消息投递语义（通常是至少一次），消费端按消息 ID 幂等。
  —— 重复投递是常态而不是异常（[D3](failure-modes.md#d3-重复投递duplicate-delivery)）。
- [ ] 🔴 **P0** 对模型服务商的调用做**全局**限流（跨所有实例），并在租户间加权公平分配。
  —— 单机限流在扩容后会失效（[D9](failure-modes.md#d9-限流只在单机生效local-only-rate-limiting)）；没有公平分配就会出现吵闹邻居（[P3](failure-modes.md#p3-吵闹邻居noisy-neighbor)）。
- [ ] 🟠 **P1** 任务租约配合心跳续约和 fencing token；心跳间隔远小于租约时长。
  —— 否则 GC 停顿或网络分区后会出现两个 worker 同时处理同一任务（[D2](failure-modes.md#d2-僵尸-workerzombie-worker)）。
- [ ] 🟠 **P1** 有背压和准入控制：队列超过阈值时拒绝新请求；消息带截止时间；监控"最老消息的年龄"。
  —— 防止故障恢复后忙着处理早已无人等待的旧请求（[D4](failure-modes.md#d4-队列积压雪崩queue-backlog-avalanche)）。
- [ ] 🟠 **P1** 配置死信队列，并对进入死信的消息告警和定期处理。
  —— 毒消息无限重投会拖垮消费者，默默丢弃又会丢失请求。
- [ ] 🟠 **P1** "写数据库 + 发事件"使用事务性发件箱（Outbox），而不是分别写入。
  —— 两次独立的写入之间崩溃就会不一致（[D7](failure-modes.md#d7-双写不一致dual-write-inconsistency)）。
- [ ] 🟠 **P1** 相同请求合并（singleflight），缓存过期时间加随机抖动。
  —— 防止缓存过期瞬间的请求风暴（[D8](failure-modes.md#d8-缓存未命中风暴cache-stampede)）。
- [ ] 🟠 **P1** 根据任务时长选择交付方式（同步 / SSE 流式 / 异步队列 + 通知 / 工作流引擎），各层超时一致。
  —— 前端超时了后台还在跑，是浪费也是隐患（[P4](failure-modes.md#p4-长尾延迟爆炸tail-latency-blowup)）。
- [ ] 🟢 **P2** 跨多个服务的长流程使用 Saga 补偿或工作流引擎，而不是让模型协调。
  —— 模型不会可靠地回滚（[T7](failure-modes.md#t7-部分完成partial-completion)）。
- [ ] 🟢 **P2** 压测和故障演练覆盖：同一会话并发消息、worker 在执行中被杀、队列积压、模型服务商限流。
  —— 这些场景在单机开发环境中永远不会出现。

## 17. 企业知识与 RAG

- [ ] 🔴 **P0** 检索时按可信身份做 **ACL 前过滤**，而不是检索后再让模型"保密"。
  —— 进入上下文的内容就可能出现在回答里（[C7](failure-modes.md#c7-权限后过滤泄露post-filter-acl-leak)）。
- [ ] 🔴 **P0** 用户身份一路透传到检索服务；多租户的索引在存储层隔离。
  —— 检索服务用一个"超级账号"查询，就等于绕过了所有权限（[C5](failure-modes.md#c5-记忆串户cross-tenant-memory-leak)）。
- [ ] 🟠 **P1** 源系统的更新和删除事件同步到索引、缓存和摘要；有定期对账。
  —— 否则已废止的政策、已删除的文档会继续被引用（[C8](failure-modes.md#c8-删除未传播deletion-not-propagated)）。
- [ ] 🟠 **P1** 索引条目带来源 ID、版本、ACL 和过期时间。
  —— 这是增量同步、按来源失效和删除传播的基础（[C8](failure-modes.md#c8-删除未传播deletion-not-propagated)）。
- [ ] 🟠 **P1** 引用校验：引用只能来自本次检索结果，且被引用的段落确实支持对应陈述。
  —— 带着假出处的错误答案比没有出处更危险（[C9](failure-modes.md#c9-引用失真citation-hallucination)）。
- [ ] 🟠 **P1** 检索到的文档内容按不可信数据处理。
  —— 任何能写文档的人，都能往知识库里埋注入（[S2](failure-modes.md#s2-间接提示词注入indirect-prompt-injection)）。
- [ ] 🟠 **P1** 分别评估检索质量（召回率、排序）和回答质量（有据性、正确性）。
  —— 只看最终答案无法判断问题出在检索还是生成。
- [ ] 🟢 **P2** 切块策略按文档结构设计（保留标题层级、不拆散条款和表格），并用评估验证。
  —— 切块决定了模型能看到的上下文是否完整（[C9](failure-modes.md#c9-引用失真citation-hallucination)）。
- [ ] 🟢 **P2** 监控知识新鲜度：检索结果中陈旧文档的比例。
  —— 知识库会随时间腐烂，没有指标就不会有人注意到（[C8](failure-modes.md#c8-删除未传播deletion-not-propagated)）。

## 18. 数据、评估方法论与优化

- [ ] 🔴 **P0** 评估数据按组划分 train / dev / test（用户、会话、种子、近重复组；对时间敏感的场景按时间切），test 冻结，只在最终报告时用一次。
  —— 看着数据改系统都算"训练"；泄漏会让分数虚高、上线就掉（[A1](failure-modes.md#a1-评估集泄漏eval-set-leakage)）。
- [ ] 🔴 **P0** 测试集以真实数据为主，标签经过人工；合成数据和 LLM 标注只用来补覆盖面。
  —— 否则测出来的只是"LLM 同意 LLM"（[A3](failure-modes.md#a3-合成数据分布偏移synthetic-data-distribution-shift)、[A4](failure-modes.md#a4-llm-评委未校准uncalibrated-llm-judge)）。
- [ ] 🔴 **P0** "B 比 A 好""可以上线"的结论，基于同一批任务上的配对检验（配对 bootstrap 或 McNemar），报告差值的置信区间，而不只是两个平均分。
  —— 45/50 对 43/50 在统计上几乎说明不了问题；区间含 0 就不能宣称提升（[A2](failure-modes.md#a2-优化器的赢家诅咒optimizer-winners-curse)、[E2](failure-modes.md#e2-单次运行的假象flaky-single-run-evals)）。
- [ ] 🟠 **P1** benchmark 过一遍探针审查：什么都不做、固定话术、偷看隐藏字段的探针 Agent 得分接近 0，参考解能通过全部任务，报告里写明平凡 Agent 的基线。
  —— 能被钻空子的评估，比出来的只是"谁更会钻空子"（[A12](failure-modes.md#a12-benchmark-漏洞leaky-benchmark)）。
- [ ] 🟠 **P1** 统计的独立单元是"任务"而不是"运行"：每个任务跑 3–5 次，先在任务内求平均，再以任务为单位算区间；动手之前先估算需要多少任务。
  —— 把多次运行当成独立样本，区间会窄得虚假；把通过率估到 ±5 个点，p ≈ 0.8 时就要约 246 个任务。
- [ ] 🟠 **P1** 每次试验从干净的环境开始；评分器看环境终态，而不是 Agent 说了什么；基础设施错误（如模型 API 返回 503）单独标记、重试，不记成 Agent 失败。
  —— 共享状态会造成相关的失败，甚至被 Agent 利用；把 503 算成 Agent 失败会污染结论（[A12](failure-modes.md#a12-benchmark-漏洞leaky-benchmark)）。
- [ ] 🟠 **P1** LLM 评委在留出的人工标注样本上校准，同时报告一致率、Cohen's kappa、TPR、TNR 及其区间；成对比较时交换顺序各评一次。
  —— 一致率 67% 的评委，kappa 可能只有 0.23；只问一次，位置偏差会直接变成假赢家（[A4](failure-modes.md#a4-llm-评委未校准uncalibrated-llm-judge)、[E3](failure-modes.md#e3-评委偏差llm-judge-bias)）。
- [ ] 🟠 **P1** 评分标准和标注指南有版本号和变更记录，每条标签记下所用版本；看着校准集改过 rubric 之后，换一批新样本复测。
  —— 给输出打分的过程本身会改变标准（标准漂移）；在改标准用过的数据上报告准确率会虚高（[A1](failure-modes.md#a1-评估集泄漏eval-set-leakage)、[A4](failure-modes.md#a4-llm-评委未校准uncalibrated-llm-judge)）。
- [ ] 🟠 **P1** 送标样本分层抽样（问题信号优先、稀有路径、高成本、少量纯随机），记录抽样权重，算整体指标时加权还原。
  —— 第 21 课的 Demo 里，"失败优先"样本的问题率是 33%，真实值只有 10%。
- [ ] 🟠 **P1** 选优化杠杆之前先做错误分析：稳定地错 → 改提示词、示例或补检索；时对时错且答案能验证 → 测试时计算；格式稳定、调用量大、数据充足 → 微调或蒸馏。
  —— 模型不知道的规则，采样 5 次会全票答错，加测试时计算没有用。
- [ ] 🟠 **P1** 提示词优化只在 dev 上挑选，平局规则和候选数在看 test 之前定好；优化产物（指令、示例、分数、数据版本）入库，指令 diff 人工审阅，并自动检查有没有逐字抄进数据原文。
  —— 赢家诅咒和背题；优化器还会写出和业务规定相反的规则（[A2](failure-modes.md#a2-优化器的赢家诅咒optimizer-winners-curse)、[A1](failure-modes.md#a1-评估集泄漏eval-set-leakage)）。
- [ ] 🟠 **P1** 评分器上线前想清楚"最偷懒的满分输出长什么样"；输出格式、安全规则不交给优化器改；规则检查 + LLM 评委 + 人工抽查组合使用。
  —— 优化器只认分数，评分器有漏洞它就会找到（[A9](failure-modes.md#a9-编码-agent-钻测试空子coding-agent-test-gaming)）。
- [ ] 🟢 **P2** 优化报告同时给出优化花费（调用次数）和优化后每次调用的 token 与成本变化。
  —— 第 23 课的 GEPA 把每次调用的输入从 127 token 涨到 811 token，上线后每次调用都要为此付钱。
- [ ] 🟢 **P2** 宣称"没有变差"时做非劣效检验（配对差值区间的下界 > −δ）；同时比较多个变体时，做多重比较校正或在留出集上验证最好的那个。
  —— "没有显著变差"通常只说明样本不够；试 20 个变体，就算都没效果，平均也会有 1 个"显著更好"。
- [ ] 🟢 **P2** 用公开基准选型时检查它的有效性（平凡 Agent 能拿多少分、测试是否充分、是否可能被污染、是否已经饱和）；上线决策只看自己的评估集。
  —— 基准成绩衡量的是模型在基准上的能力，不是你的场景（[A12](failure-modes.md#a12-benchmark-漏洞leaky-benchmark)）。
- [ ] 🟢 **P2** 模型升级或数据分布变化后，重跑优化过的提示词和评估集；评估集和 benchmark 本身带版本号。
  —— 优化出来的提示词可能反而拖后腿；任务、评分器、环境任何一个变了，分数就不再可比。

## 19. 扩展能力（检索 / 记忆 / MCP / 代码执行 / 编码 Agent / 主动式）

- [ ] 🟠 **P1** 有检索评估集：查询来自真实日志，难例按类别打标签，分级标注并记下证据句；按类别报告 Recall@k（k = 实际注入的条数）、MRR、nDCG，以及延迟和成本。
  —— 没有评估集，检索"优化"全凭感觉；只看总分会掩盖某一类查询变差（[A11](failure-modes.md#a11-融合挤掉好结果fusion-crowds-out-good-results)）。
- [ ] 🟠 **P1** 稀疏 + 稠密两路混合检索，用 RRF（或在评估集上调过权重的加权 RRF）融合，不直接相加原始分数；向量结果设相似度下限。
  —— 型号、错误码靠 BM25，口语化问题靠向量；向量检索永远"有结果"，噪声会挤掉好文档（[A11](failure-modes.md#a11-融合挤掉好结果fusion-crowds-out-good-results)）。
- [ ] 🟠 **P1** ANN 索引参数用暴力检索当标准答案测过召回，而不是用默认值直接上线。
  —— pgvector 的 `ivfflat.probes` 默认是 1，在第 17 课的数据上只找回不到三成的真正近邻。
- [ ] 🟢 **P2** 比较切块大小时固定上下文预算；换 embedding 模型时全量重建索引；HyDE 这类假文档只用于检索，不进回答。
  —— 只看 Recall@k，大块会"作弊"；新旧向量不在同一个空间，不能混用（[C9](failure-modes.md#c9-引用失真citation-hallucination)）。
- [ ] 🔴 **P0** 长期记忆只从用户本人明确表达的内容写入，工具输出、网页、邮件不自动写入；进入 system prompt 的核心记忆，每次写入都过注入和敏感信息检查并留审计。
  —— 记忆投毒一次写入、每次会话都生效；MINJA 只靠正常提问就能往记忆里注入恶意记录，平均成功率 98.2%（[C6](failure-modes.md#c6-记忆投毒与过期memory-poisoning--staleness)）。
- [ ] 🔴 **P0** "删掉我的数据"走物理删除，并沿血缘级联删除派生记忆（洞察、合并结果、摘要）以及索引和缓存里的副本。
  —— 软删除不等于被遗忘权；被删的信息会通过派生数据"复活"（[A5](failure-modes.md#a5-记忆矛盾残留lingering-contradictory-memory)、[C8](failure-modes.md#c8-删除未传播deletion-not-propagated)）。
- [ ] 🟠 **P1** 记忆写入有冲突消解：写时消解（ADD / UPDATE / DELETE / NOOP + 单值槽位规则兜底）或读时消解（带日期的完整历史 + 强模型），二选一并写进设计文档；UPDATE / DELETE 保留历史，临时信息带 TTL。
  —— 只追加的记忆会重复、矛盾、过期、膨胀（[A6](failure-modes.md#a6-只追加记忆腐化append-only-memory-rot)、[A5](failure-modes.md#a5-记忆矛盾残留lingering-contradictory-memory)）。
- [ ] 🟠 **P1** 记忆评估集覆盖更新、更正、删除和间接失效，写明"第 N 次会话应该想起什么、不应该想起什么"。
  —— 各家记忆基准的数字大多是自报的，只有自己的评估集靠得住（[A5](failure-modes.md#a5-记忆矛盾残留lingering-contradictory-memory)）。
- [ ] 🔴 **P0** MCP 工具的风险等级由自己的审查决定：不可信服务器的注解一律不作数，未审查的工具默认 dangerous、调用前审批；只导入需要的工具。
  —— `readOnlyHint: true` 只是服务器的自我介绍（[A7](failure-modes.md#a7-mcp-事后变脸mcp-rug-pull)、[S5](failure-modes.md#s5-过度授权excessive-agency)）。
- [ ] 🔴 **P0** 锁定 MCP 服务器版本和工具定义指纹（名字 + 描述 + 参数 + 注解的哈希），每次连接都比对，变化时拒绝加载并重新审查。
  —— 审查是一次性的，服务器更新却随时可以改定义（[A7](failure-modes.md#a7-mcp-事后变脸mcp-rug-pull)、[S6](failure-modes.md#s6-工具投毒与供应链tool-poisoning)）。
- [ ] 🟠 **P1** 启动 MCP 服务器时只传必需的环境变量，不继承父进程的全部环境。
  —— 否则你的 API key 会交给你启动的每一个服务器（[A7](failure-modes.md#a7-mcp-事后变脸mcp-rug-pull)）。
- [ ] 🔴 **P0** 模型生成的代码必须在沙箱里执行：默认无网络、沙箱里没有密钥、每次全新环境、超时后杀掉整棵进程树；面向外部用户或多租户时至少用 gVisor 或 microVM。
  —— 进程级沙箱管得住时间和资源，管不住身份和网络（[A8](failure-modes.md#a8-沙箱限制失效ineffective-sandbox-limits)、[S3](failure-modes.md#s3-致命三要素外泄lethal-trifecta-exfiltration)）。
- [ ] 🟠 **P1** 沙箱的每项资源限制都在目标平台上实测生效（启动时探测并记录）；CI 里有内存炸弹、孙进程、越界读文件、联网这类"坏代码"用例。
  —— macOS 上 `RLIMIT_AS` 设不上、RSS 会缩水、`RLIMIT_CPU` 会误杀（[A8](failure-modes.md#a8-沙箱限制失效ineffective-sandbox-limits)）。
- [ ] 🔴 **P0** 编码 Agent 在副本、容器或独立分支上工作，产出只有 diff；测试和测试配置对 Agent 只读（工具层拒绝 + 运行前哈希校验或只读挂载）。
  —— Agent 会改坏东西，也会改测试来"通过"（[A9](failure-modes.md#a9-编码-agent-钻测试空子coding-agent-test-gaming)、[S5](failure-modes.md#s5-过度授权excessive-agency)）。
- [ ] 🟠 **P1** 合并前做 diff 审查（测试里的具体数值、跳过测试、`sys.exit`、重载 `__eq__`）+ 完整 CI + 人工评审；Agent 有"报告需求矛盾"的出口。
  —— ImpossibleBench 里，给出这个出口后 GPT-5 的作弊率从 54% 降到 9%（[A9](failure-modes.md#a9-编码-agent-钻测试空子coding-agent-test-gaming)）。
- [ ] 🟠 **P1** 长任务的"完成"由 harness 亲自验证（新功能 + 回归）后才标记和提交；功能清单和进度文件由 harness 管理，或只允许 Agent 改特定字段。
  —— 把验收交给被验收的人，就会过早宣布完成（[M2](failure-modes.md#m2-过早宣布完成premature-completion)）。
- [ ] 🟠 **P1** 用 Agent 框架之前核对它的默认值并写合同测试：追踪数据的去向、响应缓存、暂停恢复时的重跑语义；锁定框架版本。
  —— LangGraph 恢复时节点从头重跑，OpenAI Agents SDK 的追踪默认上传到 OpenAI，DSPy 默认缓存响应，这些都不会报错（[T5](failure-modes.md#t5-重复副作用duplicate-side-effects)、[S7](failure-modes.md#s7-敏感信息泄露sensitive-information-disclosure)）。
- [ ] 🟠 **P1** 主动式功能的打扰决策用可测试的代码：收益 × 置信度 − 情境成本过了阈值才说，三档输出（现在说 / 攒进摘要 / 不说），加勿扰时段和频率上限；紧急通道要求可信来源和最低置信度。
  —— 几次无用的打扰，用户就会关掉整个功能（[A10](failure-modes.md#a10-主动式-agent-过度打扰over-interrupting-proactive-agent)）。
- [ ] 🟠 **P1** 用户模型的每条推断都能查看、纠正、删除（删除要拉黑，防止被重新学回来）；敏感推断默认不用；只把和当前事件相关的推断发给模型。
  —— 主动式 Agent 持续读邮件和日历，是致命三要素的典型场景（[S3](failure-modes.md#s3-致命三要素外泄lethal-trifecta-exfiltration)、[C6](failure-modes.md#c6-记忆投毒与过期memory-poisoning--staleness)）。
- [ ] 🟢 **P2** 主动式功能的线上指标不只看采纳率，还看每人每天的打扰次数、"别再提醒"率和关闭功能的比例。
  —— 只优化采纳率，会学出"标题党"式的提醒（[A10](failure-modes.md#a10-主动式-agent-过度打扰over-interrupting-proactive-agent)）。

---

## 附：上线前最小 P0 集合（时间紧时的底线）

如果只能做 10 件事，下面这 10 条是底线（完整版的"上线前 10 问"见[速查表](cheatsheet.md)）：

| # | 检查项 | 对应失败模式 |
|---|---|---|
| 1 | 身份从认证上下文注入，不由模型填写 | [S4](failure-modes.md#s4-身份由模型决定confused-deputy) |
| 2 | 高风险工具需要人工审批，按角色最小权限 | [S5](failure-modes.md#s5-过度授权excessive-agency) |
| 3 | 同一 Agent 不同时具备"致命三要素"，或已切断其一 | [S3](failure-modes.md#s3-致命三要素外泄lethal-trifecta-exfiltration) |
| 4 | 写工具幂等 | [T5](failure-modes.md#t5-重复副作用duplicate-side-effects) |
| 5 | max_steps + token/金额预算 + 租户配额 | [M3](failure-modes.md#m3-循环与重复调用tool-call-loop)、[B1](failure-modes.md#b1-成本失控runaway-cost) |
| 6 | 工具超时 + 输出截断 | [T4](failure-modes.md#t4-慢工具与挂起hanging-tool)、[T3](failure-modes.md#t3-输出爆炸tool-output-explosion) |
| 7 | 数据按租户在存储层隔离 | [C5](failure-modes.md#c5-记忆串户cross-tenant-memory-leak) |
| 8 | 完整 trace + 运行状态指标 | [P1](failure-modes.md#p1-静默失败silent-failure)、[P2](failure-modes.md#p2-无法复现unreproducible-incident) |
| 9 | 评估集 + CI 回归门禁 | [E4](failure-modes.md#e4-修一坏三prompt-regression) |
| 10 | 兜底路径（转人工）+ 紧急开关 | [M5](failure-modes.md#m5-政策幻觉policy-hallucination)、[S5](failure-modes.md#s5-过度授权excessive-agency) |

在 [综合实战](../capstone/README.md)（ITBuddy）中，你可以把这份清单当作验收标准逐条核对。
