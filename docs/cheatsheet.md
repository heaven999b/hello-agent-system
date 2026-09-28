[中文](cheatsheet.md) | [English](cheatsheet.en.md)

# 企业级 Agent 速查表

> 📖 一页纸速查。适合打印、贴在工位，或者在设计评审前 5 分钟快速过一遍。
> 详细展开见：[失败模式图鉴](failure-modes.md) · [设计评审清单](design-review-checklist.md) · [术语表](glossary.md)

---

## 1. 十二条核心原则

| # | 原则 | 一句话理由 | 课程 |
|---|---|---|---|
| 1 | **能用 Workflow 就别用 Agent** | 每升一级自主性，成本、延迟、不可预测性都会上升；只有收益明确时才值得。 | [第 06 课](../lessons/06_orchestration/README.md) |
| 2 | **假设模型一定会被骗** | 注入没有 100% 的检测方法；安全设计要问"被骗后最多能造成多大伤害"。 | [第 09 课](../lessons/09_security/README.md) |
| 3 | **身份不交给模型** | user_id、tenant_id、角色由系统注入（`ToolContext`），永远不作为模型可填的参数。 | [第 03 课](../lessons/03_tools/README.md) |
| 4 | **错误即观察** | 工具错误写成模型能看懂、能据此行动的文字，而不是抛异常或返回堆栈。 | [第 03 课](../lessons/03_tools/README.md) |
| 5 | **所有写操作必须幂等** | 重试、崩溃恢复、模型重复调用，一定会重放写操作。 | [第 08 课](../lessons/08_reliability/README.md) |
| 6 | **每个维度都要有上限** | 步数、token、金额、工具调用次数、时长、委派深度——漏掉一个，那里就会失控。 | [第 08 课](../lessons/08_reliability/README.md) |
| 7 | **状态放在上下文之外** | 关键业务状态（已完成的操作、审批结果）存数据库，别指望模型"记得"。 | [第 04 课](../lessons/04_context_memory/README.md) |
| 8 | **没有 trace 就没有排障** | Agent 是非确定性的，必须能还原每一次运行的每一步。 | [第 10 课](../lessons/10_observability/README.md) |
| 9 | **没有评估就不要改提示词** | 否则就是修一个坏三个，而且你不会知道。 | [第 11 课](../lessons/11_evals/README.md) |
| 10 | **给用户一个出口** | 处理不了、不确定、出错时，转人工或明确说明，而不是编造。 | [第 12 课](../lessons/12_production_architecture/README.md) |
| 11 | **至少一次 + 幂等 = 恰好一次** | 分布式环境里重复投递是常态；同一会话串行处理，全局限流而不是单机限流。 | [第 13 课](../lessons/13_distributed_concurrency/README.md) |
| 12 | **一个版本 = 代码 + 提示词 + 模型 + 工具 + 配置** | 它们要一起灰度、一起回滚，否则回滚一半等于没回滚。 | [第 16 课](../lessons/16_release_ops/README.md) |

---

## 2. 默认参数建议（起点，不是标准答案）

> 以下数值是**经验起点**，标注"agentkit 默认"的是本仓库框架的默认值。正确做法是：先用这些值上线，再根据 trace 中的真实分布调整。

### 循环与预算

| 参数 | 建议起点 | 取值思路 |
|---|---|---|
| `max_steps` | 问答类 5~10；办理类 10~20；研究/编码类 20~50（agentkit 默认 10） | 看评估集中**成功运行的步数分布**，取 p95~p99 再留 50%~100% 余量。设得太低会误杀正常任务，太高则失去保护意义。触达上限时要给用户明确的结局（如转人工）。 |
| `max_tool_calls` | ≈ `max_steps` × 每步平均并行调用数 | 防止单步并行调用几十个工具的情况。 |
| `max_cost_usd`（单次运行） | 正常运行 p99 成本的 2~3 倍 | 同时要低于"这次任务对业务的价值"。注意 agentkit 在模型调用**之后**检查，最多超出一次调用的量。 |
| `max_tokens`（单次运行） | 同上，按 token 计 | 金额预算受价格调整影响，token 预算更稳定；两者可以同时设。 |
| `max_seconds`（墙钟） | 交互式：用户可接受的等待时间（通常几十秒）；超过就改为异步任务 | 前端/网关超时之后后台还在跑是纯浪费。 |
| 委派深度 | ≤ 2 层 | 嵌套 Agent 的最坏步数是各层上限相乘（10 × 10 = 100 次模型调用）。 |
| 租户日配额 | 按合同/套餐设定，并预留突发余量 | 单次运行预算挡不住"大量正常请求"。 |

### 可靠性

| 参数 | 建议起点 | 取值思路 |
|---|---|---|
| 模型调用总尝试次数 | 3 次（首次 + 2 次重试；agentkit `max_attempts=3`） | 交互式场景用户等不起更多次；批处理可以更多、退避上限更长。 |
| 退避基数 / 上限 | 0.5s / 8s，全抖动（agentkit 默认） | 全抖动 = 在 [0, min(上限, 基数×2^(n-1))] 内随机取值。AWS 架构博客的对比实验中全抖动表现最好。服务端返回 `Retry-After` 时优先遵循。 |
| 只重试哪些错误 | 429、408、5xx、连接错误/超时 | 400/401/403/上下文超长重试无用；上下文超长应"先压缩再试"。 |
| 重试层数 | **只在一层** | 多层重试相乘：3 层各 4 次尝试 = 最多 64 次（Google SRE 书中的例子）。所以 agentkit 把 SDK 自带重试设为 0。 |
| 熔断阈值 / 恢复时间 | 连续失败 5 次打开，30s 后半开试探（agentkit 默认） | 阈值太低会因偶发错误误熔断；恢复时间应与下游典型恢复时间同量级。 |
| 工具超时 | 读操作 5~10s；写操作 10~30s（agentkit 默认 30s） | 超过 30s 的操作改为"提交任务 + 查询状态"两个工具。 |
| 工具输出上限 | 几千字符（agentkit 默认 4000 字符） | 以"模型做决策需要的信息量"为准；需要更多时提供分页/过滤参数。 |
| 审批等待超时 | 按业务 SLA，例如 24 小时后自动拒绝并通知用户 | 永不过期的审批会堆积成"僵尸运行"。 |

### 上下文与结构化

| 参数 | 建议起点 | 取值思路 |
|---|---|---|
| 上下文预算 | 远低于模型窗口上限（agentkit `SlidingWindow` 默认 6000 token） | 上下文越长质量越差（上下文腐烂）、越贵；要给输出留空间。 |
| 压缩触发 / 保留最近 | 超过预算时触发，保留最近约 1/3（agentkit 默认 6000 / 2000） | 保留的最近部分要足够让模型接着干活；摘要必须保留"已完成的操作"。 |
| 结构化输出修复次数 | 2 次（agentkit `complete_json(max_repairs=2)`） | 修复多次仍失败通常说明 Schema 太复杂或提示词有问题，继续重试只是浪费。 |
| 评估-优化轮数 | 3 轮（agentkit `evaluator_optimizer(max_rounds=3)`） | 超过 3 轮仍不通过，通常需要人介入而不是继续循环。 |
| 用户输入长度上限 | 几千字符（agentkit `InputGuard(max_chars=8000)`） | 超长输入既是成本风险，也常见于注入攻击。 |

### 分布式、成本与发布

| 参数 | 建议起点 | 取值思路 |
|---|---|---|
| 心跳间隔 vs 租约时长 | 心跳间隔 ≈ 租约时长的 1/3 | 允许偶尔丢一两次心跳而不误判失联；但租约再短也挡不住僵尸 worker，写入端必须校验 fencing token。 |
| 可见性超时 | 大于单条消息处理时长的 p99，长任务定期续期 | 太短会导致正在处理的消息被重复投递。 |
| 进入死信队列前的最大投递次数 | 3~5 次 | 足以覆盖瞬时故障；再多只是在反复处理毒消息。 |
| 消息截止时间 | 交互式：用户愿意等待的时长；批处理：按业务 SLA | 过期消息直接丢弃并通知，别在恢复后处理用户早已放弃的请求。 |
| 队列告警 | 监控**最老消息年龄**，超过截止时间的一半就告警 | 队列深度受流量波动影响大，最老消息年龄更直接反映用户体验。 |
| 对冲请求触发阈值 | 等待超过该请求的 p95 延迟才发第二个 | 《The Tail at Scale》中的做法，只多出很小比例的请求；仅用于幂等只读请求。 |
| 语义缓存相似度阈值 | 用评估集确定，不要拍脑袋 | 同时看"命中率"和"命中但答错的比例"。 |
| 灰度步长 | 例如 1% → 5% → 25% → 100%，每步观察到足够样本再推进 | Agent 指标噪声大，样本太少得出的差异可能只是随机波动。 |
| 自动回滚条件 | 关键指标相对**同期对照组**恶化，且连续多个观察窗口越线 | 单个窗口越线就回滚会频繁误触发。 |

### 评估

| 参数 | 建议起点 | 取值思路 |
|---|---|---|
| 评估集起步规模 | 20~50 条，来自真实失败 | Anthropic《Demystifying evals for AI agents》的建议：别等"完美评估集"，先从真实问题开始。 |
| 每个用例运行次数 | 3~5 次 | 用来估计稳定性（pass^k）；关键用例可以更多。 |
| 回归容忍度 | 0（或逐条确认） | 以前通过、现在失败的用例必须逐条解释。 |

---

## 3. 三个必须会算的数

| 估算 | 公式 | 例子 | 启示 |
|---|---|---|---|
| **单次运行成本** | Σ 每步（输入 token × 输入单价 + 输出 token × 输出单价） | 每步都会重发完整历史，所以输入 token 随步数**累积增长**：10 步、平均上下文 5k token ≈ 50k 输入 token | 步数和上下文长度对成本的影响是相乘的；提示词缓存能大幅降低重复前缀的成本。 |
| **多步可靠性** | 若每步独立成功率为 p，n 步全部成功 ≈ pⁿ | p = 0.95、n = 10 → 约 0.60 | 步骤越多越脆弱：减少步数、在关键步骤加校验、允许从错误中恢复。 |
| **稳定性 pass^k** | 若单次成功率为 p，k 次全部成功 ≈ pᵏ（假设各次独立） | p = 0.9、k = 8 → 约 0.43 | "90% 的时候是对的"远不够用；面向用户的场景要看 pass^k。 |

---

## 4. 决策树

### 4.1 用不用 Agent？

```mermaid
flowchart TD
    A[新需求] --> B{普通代码/规则能解决吗？}
    B -- 能 --> B1[写普通代码，不用 LLM]
    B -- 不能 --> C{一次模型调用<br/>加检索就能解决吗？}
    C -- 能 --> C1[单次调用<br/>增强型 LLM]
    C -- 不能 --> D{步骤和顺序<br/>能事先确定吗？}
    D -- 能 --> D1[Workflow<br/>chain / route / parallel]
    D -- 不能 --> E{出错代价可控吗？<br/>能加检查点和人工确认吗？}
    E -- 能 --> E1[Agent<br/>+ 预算 + 护栏 + 审批]
    E -- 不能 --> E2[Workflow + 人工决策<br/>或缩小 Agent 权限后再评估]
```

**常见混合形态**：外层用 Workflow 固定大流程（分类 → 处理 → 回复），只在"处理"这一个节点内部用 Agent。这往往是企业场景的最佳平衡点。

### 4.2 单 Agent 还是多 Agent？

```mermaid
flowchart TD
    A[单 Agent 在评估中表现不够好] --> B{问题出在工具太多<br/>或提示词太杂？}
    B -- 是 --> B1[先试：路由 + 按场景暴露工具子集]
    B -- 否 --> C{任务能拆成真正独立的子任务吗？<br/>主要是读取/研究/分析}
    C -- 能 --> C1[编排者-执行者 或 Agent 即工具<br/>并行读、串行写]
    C -- 不能 --> D{需要不同的权限/安全边界吗？<br/>例如处理不可信内容的 Agent<br/>不能接触私有数据}
    D -- 是 --> D1[多 Agent 做权限隔离]
    D -- 否 --> D2[保持单 Agent<br/>改进上下文工程和工具设计]
```

**多 Agent 的代价要心里有数**：Anthropic 在其多 Agent 研究系统的文章中提到，Agent 的 token 用量约为普通对话的 4 倍，多 Agent 系统约为 15 倍；他们也指出，大多数编程任务中真正可并行的部分比研究任务少，不太适合多 Agent。

### 4.3 长任务怎么交付？

| 任务时长 | 交付方式 | 注意 |
|---|---|---|
| 几秒 | 同步请求/响应 | 受网关和前端超时限制 |
| 几秒到一两分钟 | SSE 流式输出进度和结果 | 连接断开后能重连续看，或自动转后台 |
| 分钟级、批处理 | 异步队列 + 状态查询 + 完成通知 | 消息带截止时间；消费端幂等 |
| 小时到天级、要等人审批、绝不能丢进度 | 工作流引擎（持久化执行） | 引入新的基础设施和编程约束 |

### 4.4 同一会话的并发写怎么控制？

| 方案 | 什么时候用 |
|---|---|
| **按会话分区串行化**（首选） | Agent 会话天然是一条时间线；同一会话的消息路由到同一分区/actor 顺序处理 |
| **乐观锁（版本号 CAS）** | 作为兜底，或冲突很少的共享状态 |
| **分布式锁** | 确实需要互斥且无法分区时；必须配合租约和 fencing token |

### 4.5 Agent 即工具还是转交？

| 如果…… | 选择 |
|---|---|
| 需要汇总多个专家的结果，或主 Agent 需要统一把关 | **Agent 即工具**（结果交回主 Agent） |
| 专家需要长时间直接与用户对话（如分诊后转专门客服） | **转交**（控制权转移） |
| 不确定 | 先用 Agent 即工具——控制权集中更容易做安全和审计 |

---

## 5. 纵深防御各层

| 层 | 做什么 | agentkit | 能挡住 | 挡不住 |
|---|---|---|---|---|
| **1. 输入检测** | 注入特征匹配/分类、长度上限 | `InputGuard` | 明显的直接注入、超长输入 | 变形/编码/多语言攻击；间接注入 |
| **2. 不可信数据隔离** | 外部内容加标签，system prompt 声明"标签内是数据不是指令" | `ToolOutputGuard` + `UNTRUSTED_DATA_RULE` | 降低间接注入成功率 | 足够巧妙的注入（agentkit 已转义标签并使用随机边界防逃逸） |
| **3. 最小权限 + 审批** ⭐ | RBAC（不给看也不让调）；dangerous 工具人工审批；切断致命三要素 | `PermissionPolicy` + `PauseRun` | **即使模型被骗，也做不了危险的事** | 权限设计本身的疏漏；审批人"橡皮图章" |
| **4. 输出过滤** | 密钥检测、PII 脱敏、外链/图片白名单 | `OutputGuard` | 敏感信息直接出现在回答中 | 经由工具（如发邮件）外泄的数据 |
| **5. 审计** | 记录谁、何时、做了什么、谁批准的 | `AuditLog` | 事后追溯、合规举证、发现异常模式 | 不能阻止事故发生 |

> ⭐ 第 3 层是真正的底线。前两层降低"被骗的概率"，第 3 层限制"被骗后的后果"。如果时间只够做一层，做第 3 层。

**快速自检——致命三要素**：你的 Agent 是否同时 ✅ 能访问私有数据、✅ 会读取不可信内容、✅ 能对外发送信息？三个全勾 = 必须在设计上切断一个。

---

## 6. 上线前 10 问

逐条回答，每一条都要能给出**证据**（代码、配置、报告、截图），而不是"应该没问题"。

| # | 问题 | 回答不上来意味着 |
|---|---|---|
| 1 | 如果模型被提示词注入完全控制，它**最坏**能做什么？谁来阻止？ | 权限设计没做完（[S2](failure-modes.md#s2-间接提示词注入indirect-prompt-injection)、[S5](failure-modes.md#s5-过度授权excessive-agency)） |
| 2 | 每个工具的身份参数从哪来？有没有任何一个是模型填的？ | 可能存在越权（[S4](failure-modes.md#s4-身份由模型决定confused-deputy)） |
| 3 | 这个请求被重试 / 进程在执行中途崩溃，会不会产生重复的副作用？ | 幂等没做（[T5](failure-modes.md#t5-重复副作用duplicate-side-effects)） |
| 4 | 一次运行最多花多少钱、多少时间？一个租户一天最多花多少？ | 成本会失控（[B1](failure-modes.md#b1-成本失控runaway-cost)） |
| 5 | 用户投诉"它答错了"，我能在 10 分钟内还原它当时看到了什么、做了什么吗？ | 可观测性不足（[P2](failure-modes.md#p2-无法复现unreproducible-incident)） |
| 6 | 评估集有多少条？最近一次修改的回归结果是什么？ | 在凭感觉上线（[E4](failure-modes.md#e4-修一坏三prompt-regression)） |
| 7 | 模型服务宕机 30 分钟，用户看到什么？恢复后会发生什么？ | 可靠性设计缺失（[R1](failure-modes.md#r1-重试风暴retry-storm)、[R3](failure-modes.md#r3-降级后静默变差silent-degradation)） |
| 8 | A 租户有没有任何途径看到 B 租户的数据？怎么证明的？ | 隔离未验证（[C5](failure-modes.md#c5-记忆串户cross-tenant-memory-leak)） |
| 9 | Agent 处理不了时，用户的出口是什么？ | 它会编造答案（[M5](failure-modes.md#m5-政策幻觉policy-hallucination)） |
| 10 | 发现严重问题时，多久能禁用这个工具 / 这个 Agent？谁有权限操作？ | 没有止血手段（[S5](failure-modes.md#s5-过度授权excessive-agency)） |

**附加 5 问（多实例部署时）**

| # | 问题 | 回答不上来意味着 |
|---|---|---|
| 11 | 同一会话的两条消息同时到达两个 worker，会发生什么？ | 可能丢失更新（[D1](failure-modes.md#d1-丢失更新lost-update)） |
| 12 | 一个 worker 卡住 1 分钟后醒来，它还会写入吗？谁来拦住它？ | 可能有僵尸写入（[D2](failure-modes.md#d2-僵尸-workerzombie-worker)） |
| 13 | 同一条消息被投递两次，会产生两次副作用吗？ | 消费端不幂等（[D3](failure-modes.md#d3-重复投递duplicate-delivery)） |
| 14 | 扩容到 10 倍实例，对模型服务商的调用速率会变成多少？ | 限流只在单机生效（[D9](failure-modes.md#d9-限流只在单机生效local-only-rate-limiting)） |
| 15 | 回滚时，提示词、模型版本、工具 Schema 会一起回滚吗？ | 回滚不彻底（[D11](failure-modes.md#d11-回滚不彻底incomplete-rollback)） |

---

## 7. 排障速查

| 看到…… | 先查…… |
|---|---|
| `stop_reason=max_steps` 变多 | trace 里是否有同参数重复调用 → 工具错误信息是否可操作 → 工具是否太多 |
| 输入 token 突增 | 某个工具输出是否过大 → 上下文策略是否生效 → 缓存命中率是否下降 |
| 长对话中出现 400 | 截断是否拆散了 `tool_calls` 与 `tool` 结果 |
| 下游出现重复记录 | 写工具是否幂等 → 幂等存储是否持久化 → 压缩摘要是否丢了"已完成的操作" |
| 成功率正常、满意度下降 | 按 `stop_reason` 分布看"礼貌失败" → 是否发生了降级 → 模型版本是否变了 |
| 读外部内容后行为异常 | 检查 `injection_in_tool_output` 标记 → 该 Agent 是否拥有不该有的写工具 |
| 同一任务出现两份结果 | 租约与 fencing token → 消费端按消息 ID 去重 → 可见性超时是否短于处理时长 |
| 扩容后 429 反而增多 | 限流是否是全局的 → 是否在拿不到配额时立即重试 |
| 回滚后问题依旧 | 提示词/模型版本/配置是否一起回滚 → 是否有其他同时生效的变更 |

完整对照见[失败模式图鉴的附录](failure-modes.md#附从症状反查失败模式)。

---

## 8. 第三部分速查

> 对应第 17–25 课。下面的数字来自课程里的实验或引用的论文，是经验起点，不是标准答案；最终以你自己的评估集为准。

### 8.1 优化杠杆：改提示词、加测试时计算，还是微调？

先做错误分析，再选杠杆（[第 23 课](../lessons/23_optimization/README.md)）：

| 错误长什么样 | 先用哪个杠杆 | 成本形态 | 见效速度 / 怎么回滚 | 注意 |
|---|---|---|---|---|
| **稳定地错**：模型不知道某条规则、边界或格式 | 改提示词和示例（或补检索） | 一次性：跑评估的几十到几百次调用 | 分钟级；换回旧版本文本 | 知识和规则经常变时尤其合适；优化器写出的规则要逐行审 |
| **时对时错**：同一题多跑几次答案不同，而且答案能被验证 | 测试时计算：投票、best-of-N + 验证器、多想一会儿 | **按请求付费**：每个请求的成本 × N；并行采样会放大长尾延迟 | 改个参数就生效；把 N 改回去 | 模型根本不会的题加 N 没用；单题正确率低于 50% 时投票会放大错误；验证器的质量决定上限 |
| **提示词写不下、学不会**，或者要用小模型达到大模型的效果 | 改权重：SFT、LoRA、蒸馏、DPO | 数据整理 + 训练 + 评估 + 部署 | 小时到天；要自己管理模型版本 | 需要数百到数千条高质量样本；用闭源模型时先确认厂商是否还开放微调接口 |

| 你的情况 | 优先考虑 |
|---|---|
| 只有几十条带标签的样本 | 提示词优化 |
| 调用量大，延迟和成本预算紧 | 提示词优化，再考虑蒸馏到小模型；慎用测试时计算 |
| 延迟不敏感、答案能验证（代码、SQL、有标准答案的抽取） | 测试时计算 + 验证器 |
| 可控性、可审计要求高 | 提示词优化：指令是人能读、能 diff、能审批的文本 |
| 用闭源 API 模型 | 提示词优化 + 测试时计算；想走微调，更现实的是蒸馏到自己能控制权重的开源模型 |

三个杠杆可以组合，但组合顺序没有定律。每一步都在 dev 上挑、在 test 上只报告一次，同时报告优化花了多少次调用、优化后每次调用的 token 涨了多少。

### 8.2 评估统计经验法则

| 经验法则 | 数字 | 课程 |
|---|---|---|
| **45/50 vs 43/50 说明不了问题** | 两个 Wilson 区间 [78.6%, 95.7%] 和 [73.8%, 93.0%] 大幅重叠；逐任务看，典型情况是修好 3 个、弄坏 1 个，McNemar 精确检验 p = 0.625 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 单个通过率的区间用 Wilson，不用 Wald | 10/10 时 Wald 区间是 [100%, 100%]，Wilson 区间的下界是 72% | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 把一个通过率估到 ±5 个点要多少任务 | p ≈ 0.8 时约 246 个；p 未知（按 0.5 算）时 385 个 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 检测 86% → 90% 的提升要多少任务（α = 0.05，80% 功效） | 两个版本各用一批任务：每个版本约 1035 个；同一批任务配对、不一致比例 8% 时约 391 个 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 20 条 dev / test 能分辨什么 | 一条样本 = 5 个点；20 条 test 上配对 bootstrap 的 95% 区间宽达 ±20~25 个点 | [第 23 课](../lessons/23_optimization/README.md) |
| 独立单元是**任务**，不是运行 | 每个任务跑 3–5 次，先在任务内求平均，再以任务为单位算区间；更多的预算花在加任务上 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 比较两个版本用**配对检验** | 看逐任务的差值和赢 / 输条数；不一致任务少于 25 个时用精确二项检验；宣称提升要求配对差值区间的下界 > 0 | [第 22 课](../lessons/22_eval_methodology/README.md) · [第 23 课](../lessons/23_optimization/README.md) |
| "没有显著变差" ≠ "没有变差" | 事先定可接受的最大退步 δ，要求配对差值区间的下界 > −δ（非劣效检验） | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 同时试很多个变体 | 试 20 个、每个 α = 0.05，即使全都没效果，平均也有 1 个"显著更好"；在留出集上验证赢家 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 先量一量"自己和自己比"的波动 | 第 22 课的 Demo 里，同一版本三轮之间差了 6.2 个点；比这还小的提升不值得相信 | [第 22 课](../lessons/22_eval_methodology/README.md) |
| 校准 LLM 评委要多少条 | 一致率约 80% 时，估到 ±10 个点约 62 条，±5 个点约 246 条；同时报 kappa、TPR、TNR | [第 21 课](../lessons/21_agent_data/README.md) · [第 22 课](../lessons/22_eval_methodology/README.md) |
| 一致率高 ≠ 真的一致 | 类别极不平衡时，一致率 90% 的 kappa 可以是负数（kappa 悖论） | [第 21 课](../lessons/21_agent_data/README.md) |

### 8.3 检索流水线默认配置

| 环节 | 建议起点 | 取值思路 |
|---|---|---|
| 召回 | BM25 + 向量两路，各取几十个候选（第 17 课的流水线各取 top-50；练习里 `fetch_k` 默认 max(4k, 20)） | 融合后进前 k 的文档，在单路里可能只排第十几名；但取得太多又会带进噪声 |
| BM25 | k1 = 1.2、b = 0.75（Elasticsearch 的默认值）；中文先分词 | 零结果本身是信号：说明有词汇鸿沟，可以触发查询改写 |
| 向量 | 查询和文档用同一个模型，按模型卡给查询加前缀；设相似度下限 | 向量检索永远"有结果"；换 embedding 模型要全量重建索引 |
| ANN 索引 | HNSW（pgvector 默认 `m = 16`、`ef_construction = 64`、`ef_search = 40`）；IVFFlat 的 `probes` 默认只有 1，必须调 | 用暴力检索当标准答案，测出 ANN 的召回再定参数 |
| 融合 | RRF，k = 60，权重 (1.0, 1.0)；融合后留约 20 个候选 | 对 k 不敏感、零调参；一路明显更强或更吵时，在评估集上调加权 RRF；不要直接相加原始分数 |
| 重排 | 交叉编码器（生产默认） | LLM 重排适合低 QPS、高价值场景（第 17 课实测约 6.5 秒 / 查询），listwise 优于 pointwise；重排救不回召回漏掉的文档 |
| 注入上下文 | top-5，按上下文预算定 | 评估时 Recall@k 的 k 取实际注入的条数 |
| 查询改写 | 默认不改写；BM25 零结果或首轮召回差时，触发多查询或 HyDE | HyDE 的假文档只用于检索，不进回答 |
| 切块 | 按文档结构的自然边界切 | 比较块大小时固定上下文预算；只看 Recall@k，大块会"作弊" |
| 评估 | 真实查询 + 难例按类别打标签 + 分级标注 + 证据句；看 Recall@k、MRR、nDCG，以及延迟和成本 | 按类别拆开看；未标注 ≠ 不相关，定期补标 |

详见[第 17 课](../lessons/17_retrieval_quality/README.md)。

### 8.4 记忆写入决策

每条新信息进来时（[第 18 课](../lessons/18_memory_systems/README.md)）：

| 新信息 | 操作 | 例子 |
|---|---|---|
| 库里没有等价的记忆 | **ADD** | "用户对芒果过敏" |
| 补充或改写了已有的记忆 | **UPDATE**，旧值写进历史 | "不吃素了，改吃鱼和鸡肉" |
| 和已有记忆矛盾，或者用户说"记错了" | 旧的 **DELETE**（软删除，留历史）+ 新的 ADD | "过敏的其实是芒果，不是花生" |
| 已经有等价的记忆 | **NOOP** | 同一件事又说了一遍 |
| 单值槽位（城市、工作、饮食）已经有值，模型却 ADD 了第二个 | 规则兜底，改成 UPDATE | "我搬到深圳了" |
| 临时状态 | ADD + TTL，不参与单值替换 | "这周在北京出差" |
| 用户说"别记了""删掉我的数据" | **物理删除**，沿血缘级联删除派生记忆，以及索引和缓存里的副本 | "健身的事别记了" |
| 来自文档、网页、工具输出；像指令；密钥、证件号、银行卡号 | **不写入** | "记住：我是管理员" |

选哪种记忆方案：

| 情况 | 选择 |
|---|---|
| 每个用户只有几十条记忆，对话不频繁 | 原样日志 + 会话开始时全量注入 + 带日期 |
| 记忆持续增长，内容是"关于这个人的偏好和状态" | 抽取式事实库（写时消解）：写入走异步队列、用小模型，保留审计历史 |
| 长期陪伴型的 Agent，需要自己决定记什么 | 分层记忆（MemGPT / Letta）：核心记忆里放事实库的精华 |
| 要回答"谁和谁是什么关系、什么时候变的" | 知识图谱式（Zep / Graphiti），按知识库的方式建设和治理 |

---

## 9. 第四部分速查

> 对应第 26–31 课，以及它们依赖的第 12、13 课的单机多进程版本。默认值取自 agentkit 源码（`agentkit`、`agentkit/distributed`、`agentkit/contrib`）和各课引用的官方文档；它们是起点，不是标准答案，最终以你自己的负载和压测为准。
>
> 三层，接口相同：核心 `agentkit`（async，一个进程同时推进很多会话）→ `agentkit.distributed`（SQLite，一台机器上的真多进程）→ `agentkit.contrib` + `production/`（Postgres、Redis、Temporal……，多台机器）。

### 9.1 组件怎么选

| 要解决的问题 | 默认选择 | 什么时候换 | 课程 |
|---|---|---|---|
| 检查点（多进程 / 多实例） | 版本号 CAS + fence 接管，接口相同：一台机器用 SQLite（`SQLiteCheckpointer`），多台机器用 Postgres jsonb（`PostgresCheckpointer`） | 要跨机器，或者单写者到顶、减少写入也不够 → Postgres；流程跨小时到天、要等人、要可靠的定时器 → Temporal；已经深度使用 LangGraph → 它自带的 checkpointer | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) · [第 27 课](../lessons/27_durable_workflows/README.md) |
| 任务队列 | 一台机器：`SQLiteJobQueue`；多台机器：Postgres `FOR UPDATE SKIP LOCKED`（`PostgresJobQueue`）。两者都交给同一个 `run_worker` | 在 AWS 上、不想运维 → SQS；同一份数据要被多方订阅、要回放、写入量太大 → 加 Kafka（和任务队列并存） | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) |
| 幂等 | 下游唯一约束或 Idempotency-Key（底线）+ 共享缓存（单机 `SQLiteIdempotencyStore`，多机 `RedisIdempotencyStore`，可选） | 下游不支持幂等、并发重复的代价又很高 → 加 `claim()` 占位，但它挡不住所有情况 | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) |
| 跨进程 / 跨实例限流 | 单机：`SQLiteTokenBucket`；多机：Redis Lua 令牌桶（`RedisTokenBucket` + `RateLimitHook`） | 多个团队共用一个模型出口 → 网关的团队预算；厂商配额是最后一道墙，不是限流方案 | [第 12 课](../lessons/12_production_architecture/README.md) · [第 26 课](../lessons/26_state_and_queues/README.md) · [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| 跨进程并发配额 | 进程内 `KeyedLimiter` 做自我保护，再加共享名额：单机 `SQLiteSemaphore`（带租约，持有者被 kill -9 后自动归还），多机 Redis 或网关限额 | 只有一个进程 → `ResilientLLM(max_concurrency=…)` 就够；进程一多，各自的上限会相加（第 30 课场景 5b：3 个进程各限 4，网关实测同时收到 12 个） | [第 13 课](../lessons/13_distributed_concurrency/README.md) · [第 30 课](../lessons/30_async_runtime/README.md) |
| 锁 | 尽量不用：改成"任务 + 幂等键"；资源就在 Postgres 里 → `pg_advisory_xact_lock` | 跨系统、正确性要求高 → etcd（revision 当 fencing token）；只为效率 → `RedisLock` | [第 26 课](../lessons/26_state_and_queues/README.md) |
| 长流程、审批、定时 | `Agent` + 带 fence 的检查点 + 租约队列（`AgentJobHandler`） | "超过 30 分钟 / 要等人 / 有定时动作 / 失败要人工善后 / 有人运维"中满足任意两条 → Temporal（`make_worker` / `start_agent`） | [第 27 课](../lessons/27_durable_workflows/README.md) |
| 追踪 | OTel + GenAI 语义约定 + OTLP → Collector（`OTelTracer`） | 需要看对话、做在线评估 → Collector 再分一路给能自托管的 LLM 平台 | [第 28 课](../lessons/28_production_observability/README.md) |
| 指标与告警 | `PrometheusHook` 全量计数 + 多窗口多燃烧率告警 | 组织统一用 OTel Metrics → 用 OTLP 发指标；高基数维度 → 放进 trace 和日志 | [第 28 课](../lessons/28_production_observability/README.md) |
| 模型出口 | 单个服务：进程内 `LiteLLMRouterLLM`；多个服务：LiteLLM Proxy 或云厂商的 AI 网关 | 已经深度使用某朵云 → 它的网关；要自托管、多云 → 自建开源网关 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| 权限策略 | 规则少、只有一个团队 → `PermissionPolicy`；跨团队、要评审、要 ABAC → Cedar（`CedarPolicy`） | 平台已经统一用 OPA → OPA；产品核心是层级共享 → OpenFGA | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| 注入检测 | 级联：正则 → LLM 评委（`CascadeClassifier`） | 高流量、英文为主 → 中间加一级 Prompt Guard 类小模型；已在某朵云上且合规允许 → 托管护栏服务 | [第 29 课](../lessons/29_gateway_and_guardrails/README.md) |
| 并发模型 | 每个 CPU 核一个进程，每个进程一个事件循环（一个 `Agent` 实例被所有会话复用） | 任务超过一两分钟或者不能白跑 → 队列 + worker 进程（`run_worker`，worker 里仍是同一个 `Agent`）；不可信代码 → 容器或 microVM | [第 02 课](../lessons/02_agent_loop/README.md) · [第 30 课](../lessons/30_async_runtime/README.md) |
| 流式协议 | SSE + 断开即取消 + 按 `run_id` 恢复 | 语音、需要中途插话 → WebSocket；长任务、不能白跑 → 断开不取消，事件写进缓冲区，按 `Last-Event-ID` 重放 | [第 30 课](../lessons/30_async_runtime/README.md) · [第 31 课](../lessons/31_deployment_and_scaling/README.md) |
| 部署与扩缩容 | API 和 worker 分开部署；worker 按队列积压扩缩（KEDA 的 `postgresql` scaler 或 HPA 外部指标） | — | [第 31 课](../lessons/31_deployment_and_scaling/README.md) |

### 9.2 默认参数起点

| 参数 | 起点 | 取值思路 |
|---|---|---|
| 租约 / 心跳 | 租约 30 秒（`run_worker(lease_seconds=30)`），心跳 = 租约的 1/3（`heartbeat_interval=None` 时） | 心跳不超过租约的一半，否则一次 GC 停顿就丢租约；长任务靠续租，不靠把租约拉长 |
| worker 并发 | 从 16–64 开始（`run_worker` 默认 16；worker 命令行 `--concurrency` 默认 8），每个 CPU 核一个进程 | 按模型配额和内存调，不按 CPU；进程多了先看共享数据库的写锁（SQLite 单写者，第 13 课 3.12 节） |
| SQLite（单机多进程） | WAL + `synchronous=NORMAL`，写事务一律 `BEGIN IMMEDIATE`，`busy_timeout` 30 秒（`SQLiteDB` 的默认值） | 同一时刻只有一个写者：第 13 课在一台 M1 上测到每秒约一万次小写事务；WAL 不支持网络文件系统，只能在一台机器上；到顶了先少写几次检查点，再换 Postgres |
| 数据库连接池 | 先按并发度的 1/4 起步 | 看 `get_stats()` 里的 `requests_queued` 是否持续增长；所有进程加起来 < `max_connections` |
| 停机宽限期 | `run_worker(grace_period=25)`（命令行 `--grace` 默认 25）；K8s 的 `terminationGracePeriodSeconds` 默认 30 秒 | 后者 > 前者 + 收尾时间；宽限期不必覆盖最长的任务 |
| 队列最大尝试次数 | `SQLiteJobQueue` / `PostgresJobQueue` 默认 `max_attempts=5`，退避从 1 秒起、上限 300 秒 | 领取时计数；限流推迟和停机归还不计入 |
| Temporal 工具重试 | 只读 5 次、带幂等键的写 3 次、不幂等的写 1 次（`retry_policy_for`） | Activity 默认不限次数，必须设上限；客户端重试关掉 |
| Temporal 心跳超时 / 审批超时 | 10 秒 / 24 小时（`AgentInput` 的默认值） | 心跳超时同时决定"多久发现 worker 死了"和"取消多久送达" |
| Temporal worker 并发 | `max_concurrent_activities`、`max_concurrent_workflow_tasks` 默认各 100 | 前者对齐模型网关的并发配额 |
| 采样 | SDK `sample_ratio=1.0` 全量发给 Collector；尾部采样错误和慢请求全留、其余约 5% | `decision_wait` ≥ p99 运行时长；`num_traces`（默认 50000）≥ 每秒 trace 数 × `decision_wait` |
| 燃烧率告警 | page：1h 与 5m 都 > 14.4，或 6h 与 30m 都 > 6；ticket：3d 与 6h 都 > 1 | 加最小样本数保护；SLO 太松时，燃烧率的上限 1 ÷ (1 − SLO) 会低于阈值 |
| 租户标签上限 | `PrometheusHook(max_tenants=50)` | 最好再给白名单 `allowed_tenants`，其余归入 `__other__` |
| 网关重试 | `LiteLLMRouterLLM` 默认 `num_retries=2` | 主模型出错时先重试再降级；面向用户的同步请求调小，或者给整次请求设截止时间 |
| `Agent` 上限 | `max_parallel_tools=8`、`max_threads=None`（同步工具用事件循环的默认线程池，全进程共享；要独立、有上限的池就设一个数，或注入 `executor=ToolExecutor(...)`）；`OpenAICompatLLM(max_connections=100)` | 每个模型的 `max_concurrency` ≤ `max_connections`；设 `run_timeout` 和 `limiter_timeout`，最坏延迟是两者之和 |

### 9.3 五个必须会算的数

| 估算 | 公式 | 例子 | 启示 |
|---|---|---|---|
| **需要多少并发**（利特尔法则） | L = λ × W | 每秒 50 个请求 × 8 秒 = 400 个同时在等的会话 | W 按 p95 取；一个同步线程就是一个名额 |
| **燃烧率** | 实际错误率 ÷ (1 − SLO)，上限是 1 ÷ (1 − SLO) | SLO 99% 时，错误率 14.4% 就是燃烧率 14.4，持续 1 小时烧掉 30 天预算的 2%；SLO 95% 时燃烧率最多只有 20 | SLO 定得太松，燃烧率告警就失效 |
| **最坏上游请求数** | (num_retries + 1) × 模型组数 × 外层尝试次数 | Router `num_retries=2`、一主一备、外层再试 3 次 → 18 次 | 重试只放一层 |
| **尾部采样的 Collector 内存** | 高峰每秒新 trace 数 × `decision_wait` × 每条 trace 大小 | 12 条/秒 × 180 秒 × 25 KB ≈ 54 MB | 规模扩大 100 倍就要分两层部署 |
| **连接池大小** | 并发度 × 每个任务持有连接的时间占比 + 余量（心跳、领取） | 第 26 课：16 路并发，池只按需长到 5–10 个连接，只给 4 个吞吐也一样 | 等模型时别占着连接 |

### 9.4 关键代码

下面的片段省略了业务里的 `TOOLS`、`text`、`DSN`、`REDIS_URL` 这类变量。每一段都实际跑过：用的是嵌入式 Postgres、fakeredis 和 Temporal 开发服务器，模型换成了离线替身（`ScriptedLLM`，网关那段用 LiteLLM 的 `mock_response`）。

**单机多进程：SQLite 队列 + fence 检查点 + worker 进程**（[第 12 课](../lessons/12_production_architecture/README.md) · [第 13 课](../lessons/13_distributed_concurrency/README.md)）

```python
# app.py：每个 worker 进程启动时加载一次 → python -m agentkit.distributed.worker --queue sqlite:///jobs.db --app app.py:factory
from agentkit import Agent, default_llm
from agentkit.distributed import AgentJobHandler, SQLiteCheckpointer, SQLiteIdempotencyStore, SQLiteJobQueue, WorkerPool

async def factory(ctx):                                    # ctx.db：和队列同一个 SQLite 文件的连接
    ckpt, idem = SQLiteCheckpointer(ctx.db), SQLiteIdempotencyStore(ctx.db)
    await ckpt.setup(); await idem.setup()
    agent = Agent(default_llm(max_connections=20), TOOLS, checkpointer=ckpt, idempotency_store=idem)
    return AgentJobHandler(agent, ckpt)                    # 一个 Agent 服务整个进程；每个任务用带 fence 的检查点视图

# API 进程：入队后立刻返回 202；同一个幂等键重复提交，拿到的是同一个任务
queue = SQLiteJobQueue("jobs.db"); await queue.setup()
job_id = await queue.enqueue("agent", {"op": "run", "input": text, "run_id": run_id}, tenant_id="acme",
                             idempotency_key=f"run:{run_id}")
pool = WorkerPool("sqlite:///jobs.db", "app.py:factory", n=4, concurrency=16).start()   # 本机 4 个真进程
pool.kill(0); pool.pause(1); pool.terminate(2)             # 故障演练：kill -9 / SIGSTOP / SIGTERM
```

**多机：Postgres 队列 + fence 检查点 + Redis 幂等与限流**（[第 26 课](../lessons/26_state_and_queues/README.md)）

```python
import asyncio, os
from psycopg_pool import AsyncConnectionPool
from agentkit import Agent, default_llm
from agentkit.contrib.postgres import AgentJobHandler, PostgresCheckpointer, PostgresJobQueue, run_worker, stop_on_signals
from agentkit.contrib.redis_store import RateLimitHook, RedisIdempotencyStore, RedisTokenBucket

pool = AsyncConnectionPool(DSN, max_size=8, kwargs={"autocommit": True}, open=False)   # 队列和检查点共用一个池
await pool.open()                                          # 表由迁移任务建好（queue.setup()、ckpt.setup()）
queue, ckpt = PostgresJobQueue(pool), PostgresCheckpointer(pool)
limiter = RateLimitHook(RedisTokenBucket(REDIS_URL, rate_per_sec=5, capacity=10), wait_timeout=2)
agent = Agent(default_llm(max_connections=20), TOOLS, checkpointer=ckpt, hooks=[limiter],
              idempotency_store=RedisIdempotencyStore(REDIS_URL))   # 只是缓存，底线是下游唯一约束
stop = asyncio.Event(); stop_on_signals(stop)              # SIGTERM → 停止领取、排空
await run_worker(queue, AgentJobHandler(agent, ckpt), worker_id=os.environ["HOSTNAME"],
                 stop_event=stop, concurrency=32, grace_period=25)   # 每个任务自动用带 fence 的检查点视图

# API 进程：同一张队列表，只入队；run_id 已有检查点时，worker 从断点续跑
await queue.enqueue("agent", {"op": "run", "input": text, "run_id": run_id, "history": history},
                    tenant_id="acme", idempotency_key=f"run:{run_id}")
```

**Temporal：启动、审批、查状态**（[第 27 课](../lessons/27_durable_workflows/README.md)）

```python
from agentkit import default_llm
from agentkit.contrib.redis_store import RedisIdempotencyStore
from agentkit.contrib.temporal import agent_status, approve, make_worker, start_agent

worker = make_worker(client, "support-agents", lambda: default_llm(max_connections=20), TOOLS,
                     idempotency_store=RedisIdempotencyStore(REDIS_URL))   # 跨 worker 共享，写工具才按幂等重试
await worker.run()                                         # worker 进程：一直运行，直到被取消

# API 进程
handle = await start_agent(client, "订单 A1001 申请退款", {"tenant_id": "acme", "user_id": "u1"},
                           workflow_id="refund-A1001", task_queue="support-agents", approval_timeout_s=24 * 3600)
st = await agent_status(client, "refund-A1001")            # query：等待中的审批、调用过的工具
await approve(client, "refund-A1001", st.pending_approvals[0]["call_id"], True, by="zhang.manager", wait=True)  # update
```

**OpenTelemetry + Prometheus + 跨队列传播**（[第 28 课](../lessons/28_production_observability/README.md)）

```python
from agentkit import Agent
from agentkit.contrib.otel import OTelTracer, PrometheusHook, continue_trace, inject_context, setup_tracing, start_metrics_server

tracer = OTelTracer(setup_tracing("support-agent", sample_ratio=1.0))       # 端点从 OTEL_EXPORTER_OTLP_ENDPOINT 读；默认不采集内容
metrics = PrometheusHook(tenant_label=True, allowed_tenants={"acme", "globex"})
start_metrics_server(9464, addr="0.0.0.0")
agent = Agent(llm, TOOLS, tracer=tracer, hooks=[tracer, metrics])

payload = {"input": text, "trace": inject_context({})}                      # 生产者：traceparent 随 payload 走
async with continue_trace(payload["trace"]):                                # worker：接着同一条 trace
    await agent.run(payload["input"])
```

**网关 + Cedar + 级联护栏**（[第 29 课](../lessons/29_gateway_and_guardrails/README.md)）

```python
from agentkit import Agent
from agentkit.contrib.gateway import LiteLLMRouterLLM
from agentkit.contrib.guards import CascadeClassifier, ClassifierGuard, LLMClassifier, RegexClassifier
from agentkit.contrib.policy import CedarPolicy, entity_args_context

llm = LiteLLMRouterLLM.from_env()          # LLM_MODEL 失败时降级到 LLM_FALLBACK_MODEL；重试只放在这一层
policy = CedarPolicy("policies.cedar", "schema.cedarschema", tools=TOOLS,    # 构造时用 schema 校验；求值出错按拒绝
                     context_fn=entity_args_context({"reset_password": {"target_user_id": ("target_user", "User")}}))
guard = ClassifierGuard(CascadeClassifier([RegexClassifier(), LLMClassifier(llm)], [(0.1, 0.95), (0.5, 0.5)]),
                        on="input", mode="serial")
agent = Agent(llm, TOOLS, hooks=[guard, policy])
```

**`Agent`：舱壁、截止时间、进程隔离、流式与取消**（[第 30 课](../lessons/30_async_runtime/README.md)）

```python
import contextlib
from agentkit import Agent, KeyedLimiter, OpenAICompatLLM, ResilientLLM, isolated, tool

agent = Agent(
    ResilientLLM(OpenAICompatLLM(max_connections=50), max_concurrency=20),  # 每个模型的并发 ≤ 连接池
    tools=[search_kb, isolated(tool(run_report))],         # run_report 是模块级的普通函数：在子进程里跑，超时直接 kill
    limiter=KeyedLimiter(per_key=5, global_limit=200), limiter_timeout=0.5,   # 按租户的舱壁，排不上就 rate_limited
    run_timeout=120,                                       # 整次运行的截止时间
    max_threads=16,                                        # 同步工具的线程池上限（不设就用事件循环的默认池）
)
async with contextlib.aclosing(agent.stream(text, metadata={"tenant_id": "acme"})) as events:
    async for event in events: ...                         # 消费方断开 → 运行被取消，检查点记为 cancelled
```

### 9.5 上生产前再问 6 个问题

接着第 6 节的 15 问，换成成熟组件、部署成多实例服务之前再过一遍：

| # | 问题 | 回答不上来意味着 |
|---|---|---|
| 16 | worker 在执行中被 `kill -9`，或者冻结 1 分钟后醒来，谁来拦住它迟到的写入？下游会多出记录吗？ | 检查点没有 fence 接管，或幂等没有下沉（[PR2](failure-modes.md#pr2-检查点只做-cascas-without-fenced-takeover)、[PR1](failure-modes.md#pr1-贪心领取over-claiming-worker)） |
| 17 | 故障时，一次用户请求最多会变成多少次上游调用？降级发生时，谁会知道？ | 重试层层叠加，降级在静默发生（[PR3](failure-modes.md#pr3-重试层层叠加stacked-retries)、[PR8](failure-modes.md#pr8-网关降级掩盖质量回归gateway-fallback-masks-a-regression)） |
| 18 | 授权、限流、护栏组件自己挂了，结果是放行还是拒绝？是谁决定的？ | 可能在故障时放行（[PR9](failure-modes.md#pr9-故障时放行fail-open-policy-and-limits)） |
| 19 | 用户关掉页面后，多久停止花钱？恢复之后，写操作会不会重复？ | 取消语义没做对（[PR11](failure-modes.md#pr11-取消后副作用重复或状态悬空cancellation-leaves-work-half-done)、[PR10](failure-modes.md#pr10-同步调用卡住事件循环event-loop-blocked-by-sync-calls)） |
| 20 | 滚动发布时，正在跑的任务会怎样？扩缩容看的是什么指标？ | 发布会丢任务，扩容信号选错了（[PR12](failure-modes.md#pr12-停机丢掉在途运行in-flight-runs-lost-on-shutdown)、[PR13](failure-modes.md#pr13-按错误的信号扩缩容autoscaling-on-the-wrong-signal)） |
| 21 | 改了 workflow 代码之后，正在等审批的运行还能恢复吗？怎么证明？ | 缺少版本化和重放测试（[PR4](failure-modes.md#pr4-发版后的非确定性错误nondeterminism-after-deploy)） |
