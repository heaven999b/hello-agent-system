[中文](framework-comparison.md) | [English](framework-comparison.en.md)

# agentkit 概念 ↔ 主流框架对照

> 📖 本文是"领域参考手册"的一部分。目标：**学完本课程后，你能在一两天内上手任何一个主流 Agent 框架**——因为你已经知道它们要解决的问题是什么，只需要找到"它在这个框架里叫什么"。
> 相关文档：[术语表](glossary.md) · [速查表](cheatsheet.md) · [延伸阅读](reading-list.md)

> ⚠️ **核实说明**：本文所有框架概念名称均于 **2026 年 9 月**对照各框架官方文档逐项核实（文末附官方链接）。Agent 框架迭代极快，API 改名、弃用很常见（本文就记录了好几处），**动手之前请以官方文档的当前版本为准**。无法确认的内容标注为"待核实"，找不到对应机制的标注为"—"（无直接对应）。

> 🧪 **想看框架代码实际跑起来是什么样？** [第 20 课：从 agentkit 到框架](../lessons/20_frameworks_bridge/README.md)用 agentkit、DSPy、LangGraph、OpenAI Agents SDK 四种写法实现了同一个任务（同一个 IT 服务台问题、同一组工具、同一个需要人工审批的操作），逐段对照，并实测了几处只看文档想不到的差异，比如 LangGraph 恢复审批时审批节点会从头再执行一遍。本文是"这个概念在框架里叫什么"的对照表，第 20 课是可运行的实物对照，两者配合着读。

---

## 0. 核心洞察：所有框架都在解决同一组问题

你在课程里亲手实现的每个 agentkit 模块，都对应一个**所有 Agent 框架都绕不开的问题**。框架之间的差异，主要在于"把哪些问题内置、用什么抽象表达、默认值怎么设"。

| agentkit 模块 | 它解决的问题 | 课程 |
|---|---|---|
| `Agent.run` / `max_steps` | 模型 ↔ 工具的循环，以及什么时候停 | [第 02 课](../lessons/02_agent_loop/README.md) |
| `@tool` / `ToolRegistry` | 把函数变成模型能调用的工具：Schema、校验、超时、截断 | [第 03 课](../lessons/03_tools/README.md) |
| `ToolContext` | 身份等可信信息由系统注入，不让模型填 | [第 03 课](../lessons/03_tools/README.md) |
| `Hook`（7 个钩子） | 在循环的关键节点插入横切逻辑（安全、预算、审计……） | [第 02 课](../lessons/02_agent_loop/README.md) |
| `SlidingWindow` / `SummarizingCompactor` | 上下文太长怎么办 | [第 04 课](../lessons/04_context_memory/README.md) |
| `MemoryStore` | 跨会话记住东西 | [第 04 课](../lessons/04_context_memory/README.md) |
| `workflows.py` | 代码控制流程 vs 模型控制流程；多 Agent | [第 06 课](../lessons/06_orchestration/README.md) |
| `ResilientLLM` / `BudgetHook` | 重试、熔断、降级、预算 | [第 08 课](../lessons/08_reliability/README.md) |
| `Checkpointer` / `RunState` / `resume` | 崩溃恢复、暂停等人 | [第 08 课](../lessons/08_reliability/README.md) |
| `InputGuard` / `ToolOutputGuard` / `OutputGuard` | 护栏 | [第 09 课](../lessons/09_security/README.md) |
| `PermissionPolicy` / `PauseRun` | 最小权限、人工审批 | [第 09 课](../lessons/09_security/README.md) |
| `Tracer` / `Span` | 追踪 | [第 10 课](../lessons/10_observability/README.md) |
| `evals.py` | 评估 | [第 11 课](../lessons/11_evals/README.md) |

> 课程第二部分的 [第 13 课](../lessons/13_distributed_concurrency/README.md)（分布式与高并发）、[第 14 课](../lessons/14_cost_latency/README.md)（成本与延迟）、[第 15 课](../lessons/15_enterprise_rag/README.md)（企业 RAG）、[第 16 课](../lessons/16_release_ops/README.md)（发布与运维）讨论的问题——会话并发写、投递语义、全局限流、缓存隔离、权限感知检索、灰度与回滚——**大多不在 Agent 框架的职责范围内**，而属于你的基础设施层。框架能帮上忙的部分见 2.7（持久化执行）、2.12（重试）、2.13（预算）和 2.9（记忆/检索）；其余需要自己设计，这正是这几节课的价值。

---

## 1. 先认清各框架的定位

| 框架 | 一句话定位 | 抽象层次 | 当前状态（2026-09 核实） |
|---|---|---|---|
| **LangGraph** + LangChain v1 | LangGraph 是低层的**图编排运行时**（状态、节点、边、检查点）；LangChain v1 的 `create_agent` 是构建在它之上的高层 Agent API，横切逻辑用**中间件**表达 | 低层 + 高层 | LangGraph v1 已弃用 `langgraph.prebuilt.create_react_agent`，官方迁移指南要求改用 `langchain.agents.create_agent` |
| **OpenAI Agents SDK** | 一组轻量原语：Agent、Runner、工具、handoff、护栏、Session、追踪 | 中层 | 活跃迭代；已支持人工审批与可序列化的 `RunState` |
| **Claude Agent SDK** | 把 Claude Code 的 Agent harness（内置文件/命令/搜索工具、权限、会话、上下文压缩、子代理）作为库提供 | 高层（"开箱即用的 Agent"） | 原名 Claude Code SDK，已更名；`ClaudeCodeOptions` → `ClaudeAgentOptions` |
| **Google ADK** | 代码优先的 Agent 开发工具包：LlmAgent、Runner、回调、插件、Session/Memory 服务、评估 CLI | 中高层 | 文档已迁至 adk.dev；ADK 2.0 引入图工作流 `Workflow`，并将 `SequentialAgent` 等模板工作流标为被取代（superseded） |
| **CrewAI** | 角色扮演式多 Agent 团队（Agent / Task / Crew / Process），外加事件驱动的 Flows | 高层 | 活跃迭代；记忆已统一为单一 `Memory` 类 |
| **Microsoft Agent Framework** | 微软官方的 Agent 框架（.NET / Python），Agent + 中间件 + Workflows + 多种编排模式 | 中层 + 工作流 | 1.0 已发布，官方称其为 Semantic Kernel 与 AutoGen 的直接继任者 |
| **AutoGen** | 多 Agent 对话框架（AgentChat / Core） | 中层 | GitHub README 声明已进入**维护模式**，不再增加新功能，并指向 Microsoft Agent Framework 作为继任者 |
| **Temporal** | **通用持久化执行引擎**，不是 Agent 框架；Workflow（确定性编排代码）+ Activity（有副作用的步骤） | 基础设施 | 提供与 OpenAI Agents SDK 等的官方集成 |

> 💡 **选型第一原则**：先问"我缺的是哪一层"。缺 Agent 循环和工具 → 中/高层框架；缺"长时间运行、崩溃不丢、能等人几天" → 持久化执行（LangGraph checkpointer / Temporal）；两者都缺 → 组合使用（例如 Temporal + OpenAI Agents SDK 的官方集成）。

---

## 2. 逐概念对照

### 2.1 主循环与步数上限（agentkit：`Agent.run` + `max_steps`）

| 框架 | 对应机制 | 与 agentkit 的差异 / 注意 |
|---|---|---|
| LangGraph / LangChain | `create_agent`（模型 ↔ 工具循环）；图级配置 `recursion_limit`，超限抛 `GraphRecursionError`；中间件 `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` | `recursion_limit` 计的是图的 **super-step（超步）**，不是模型调用次数，当前文档写明默认 1000；要限制模型调用次数用 `ModelCallLimitMiddleware` |
| OpenAI Agents SDK | `Runner.run()` / `run_sync()` / `run_streamed()`；`max_turns`，超限抛 `MaxTurnsExceeded` | 与 agentkit 最像；可以用 `error_handlers` 在超限时返回兜底输出而不是抛异常 |
| Claude Agent SDK | `query()`（单次）/ `ClaudeSDKClient`（多轮会话）；`ClaudeAgentOptions(max_turns=...)` | 超限时结果消息的 `subtype` 为 `error_max_turns`；文档说明默认不设上限——**生产中务必显式设置** |
| Google ADK | `LlmAgent`（别名 `Agent`）+ `Runner`；`RunConfig(max_llm_calls=...)` | 限制的是单次运行的**模型调用总数**，文档写明默认 500 |
| CrewAI | Agent 参数 `max_iter`、`max_execution_time` | `max_iter` 到达后 Agent 会被要求给出"当前最佳答案" |
| MS Agent Framework | Python：`Agent`（原名 `ChatAgent`）+ `agent.run()`；函数调用配置中的 `max_iterations` 等参数 | C# 侧为 `AIAgent` / `ChatClientAgent` |
| Temporal | 没有 Agent 循环；你在 Workflow 代码里自己写 `while` 循环，每次模型调用/工具调用作为 Activity | 步数上限需自己实现 |

### 2.2 工具定义（agentkit：`@tool` + 自动 Schema + 校验 + 超时 + 截断 + 风险分级）

| 框架 | 对应机制 | 注意 |
|---|---|---|
| LangGraph / LangChain | `@tool`（`langchain.tools`）、`args_schema`；图中用 `ToolNode` 执行 | Schema 由类型注解 + docstring 生成 |
| OpenAI Agents SDK | `@function_tool`（文档中也出现了更短的 `@tool` 别名）、`FunctionTool`；托管工具如 `WebSearchTool` | 装饰器支持 `needs_approval`、`timeout`、`failure_error_function`（把工具异常转成给模型看的文字——即 agentkit 的"错误即观察"） |
| Claude Agent SDK | `@tool(name, description, input_schema)` + `create_sdk_mcp_server(...)`，以进程内 MCP 服务器的形式提供自定义工具；另有大量内置工具（Read、Edit、Bash 等） | 模型看到的工具名形如 `mcp__<server>__<tool>`，写权限规则时要用这个全名 |
| Google ADK | `FunctionTool`；普通函数放进 `tools` 会被自动包装；`LongRunningFunctionTool` 用于长时任务 | |
| CrewAI | `@tool("name")` 或继承 `BaseTool`（`name` / `description` / `args_schema` / `_run`） | |
| MS Agent Framework | Python：`@tool`（原名 `@ai_function`）、`FunctionTool`（原名 `AIFunction`）；C#：`AIFunctionFactory.Create` | `@tool` 支持 `approval_mode` |
| Temporal | Activity（`@activity.defn`）；OpenAI 集成中用 `activity_as_tool()` 把 Activity 变成工具 | 每次工具执行自动获得重试、超时和持久化 |

> **风险分级**（agentkit 的 `risk="read|write|dangerous"`）在各框架中大多没有统一字段，通常体现为"这个工具要不要审批"：OpenAI `needs_approval`、MAF `approval_mode`、ADK `require_confirmation`、LangChain `HumanInTheLoopMiddleware(interrupt_on=...)`。建议你在自己的工具注册层保留显式的风险等级，再映射到框架的审批机制。

### 2.3 可信上下文注入（agentkit：`ToolContext`，身份不交给模型）

这是企业级最关键、也最容易被忽略的一条（见[失败模式 S4](failure-modes.md#s4-身份由模型决定confused-deputy)）。

| 框架 | 对应机制 | 注意 |
|---|---|---|
| LangGraph / LangChain | `ToolRuntime`（`runtime.state` / `runtime.context` / `runtime.store` / `runtime.tool_call_id`） | 运行时参数对模型不可见 |
| OpenAI Agents SDK | `RunContextWrapper[T]`，通过 `Runner.run(..., context=obj)` 传入 | 文档明确说明 context 对象**不会发送给模型**——与 agentkit 设计意图一致 |
| Claude Agent SDK | 未找到与 `ToolContext` 直接对应的注入对象（待核实） | 可行的做法：在创建会话时用闭包把可信身份"捕获"进工具函数，而不是作为工具参数暴露给模型 |
| Google ADK | `ToolContext`（可访问 Session state 等） | 身份应来自 Session（`user_id`）而非模型参数 |
| CrewAI | 待核实 | |
| MS Agent Framework | `FunctionInvocationContext` | |
| Temporal | Workflow 输入参数 / Activity 参数由你的代码传递 | 身份来自启动 Workflow 的调用方 |

### 2.4 钩子与中间件（agentkit：`Hook` 的 7 个钩子）

agentkit 的钩子时机：`on_run_start → [before_llm → LLM → after_llm → (before_tool → 工具 → after_tool)*]* → on_final → on_run_end`，外加 `visible_tools`。

| agentkit | LangChain v1 中间件 | OpenAI Agents SDK | Claude Agent SDK | Google ADK | MS Agent Framework | CrewAI |
|---|---|---|---|---|---|---|
| 抽象名称 | `AgentMiddleware` | `RunHooks` / `AgentHooks` | `hooks` + `HookMatcher` | Callbacks；全局用 Plugins | Middleware（agent / function / chat 三类） | Execution hooks |
| `on_run_start` | `before_agent` | `on_agent_start` | `UserPromptSubmit` | `before_agent_callback` | agent middleware | 待核实 |
| `before_llm` | `before_model` / `wrap_model_call` | `on_llm_start` | — | `before_model_callback` | chat middleware | `@before_llm_call`（新写法 `@on(InterceptionPoint.PRE_MODEL_CALL)`） |
| `after_llm` | `after_model` / `wrap_model_call` | `on_llm_end` | — | `after_model_callback` | chat middleware | `@after_llm_call` |
| `visible_tools` | `wrap_model_call` 中按状态过滤工具（官方称 dynamic tools） | `is_enabled`（工具级开关） | `allowed_tools` / `disallowed_tools` / `tools` | 待核实 | 待核实 | 待核实 |
| `before_tool` | `wrap_tool_call` | `on_tool_start` | `PreToolUse` | `before_tool_callback` | function middleware | `@before_tool_call` |
| `after_tool` | `wrap_tool_call` | `on_tool_end` | `PostToolUse` | `after_tool_callback` | function middleware | `@after_tool_call` |
| `on_final` / `on_run_end` | `after_agent` | `on_agent_end` | `Stop` | `after_agent_callback` | agent middleware | 待核实 |

两个值得注意的设计差异：

1. **观察型 vs 拦截型**：OpenAI 的 `RunHooks` 主要用于观察（日志、指标）；真正的拦截和改写走护栏或工具级配置。LangChain 的 `wrap_*` 和 MAF 的中间件是"洋葱模型"，可以在调用前后都插入逻辑，甚至不调用下一层（短路）。agentkit 的钩子介于两者之间：`before_tool` 返回字符串即拒绝，抛 `StopRun` / `PauseRun` 即中止或暂停。
2. **Claude Agent SDK 的钩子可以做权限决策**：`PreToolUse` 可以返回 allow / deny / ask 等决定，还能改写工具输入；它在权限评估链中排在最前面（见 2.6）。

### 2.5 护栏（agentkit：`InputGuard` / `ToolOutputGuard` / `OutputGuard`）

| 框架 | 对应机制 | 注意 |
|---|---|---|
| LangGraph / LangChain | 没有独立的 Guardrail 类，用中间件实现；内置 `PIIMiddleware`（redact / mask / hash / block 等策略） | 官方护栏文档分"确定性护栏"和"基于模型的护栏"两类 |
| OpenAI Agents SDK | `@input_guardrail` / `@output_guardrail`，返回 `GuardrailFunctionOutput(tripwire_triggered=...)`，触发时抛 `InputGuardrailTripwireTriggered` 等；另有工具级 `@tool_input_guardrail` / `@tool_output_guardrail` | **输入护栏只作用于链路中的第一个 Agent，输出护栏只作用于最后一个 Agent**；输入护栏默认与 Agent 并行运行（"乐观执行"），需要阻塞时要显式配置 |
| Claude Agent SDK | 没有名为 guardrail 的原语；用 `UserPromptSubmit`（输入）、`PreToolUse` / `PostToolUse`（工具前后）钩子，以及权限规则实现 | |
| Google ADK | 用 Callbacks 或 Plugins 实现；官方安全文档推荐用 Plugins 实现跨 Agent 的通用策略 | |
| CrewAI | Task 参数 `guardrail` / `guardrails`（函数或自然语言描述）、`guardrail_max_retries` | 校验失败时把错误反馈给 Agent 重试——类似 agentkit `complete_json` 的修复循环 |
| MS Agent Framework | 中间件中终止执行（`MiddlewareTermination`）；官方有专门的 "Termination & Guardrails" 文档 | |
| Temporal | 无内置；在 Activity 中实现规则校验 | |

> 无论哪个框架，都请记住第 09 课的结论：**护栏降低概率，权限限制后果**。框架提供的护栏是第 1、2、4 层，第 3 层（最小权限 + 审批）要靠 2.6 的机制。

### 2.6 权限与人工审批（agentkit：`PermissionPolicy` + `PauseRun` + `resume` / `approve`）

| 框架 | 对应机制 | 暂停/恢复的方式 |
|---|---|---|
| LangGraph / LangChain | 底层：节点内调用 `interrupt()`，恢复时 `invoke(Command(resume=...))`；高层：`HumanInTheLoopMiddleware(interrupt_on=...)`，决策类型含 approve / edit / reject | 依赖 checkpointer + `thread_id`。⚠️ **恢复时整个节点从头重新执行**，而不是从 `interrupt()` 那一行继续——所以 `interrupt()` 之前的代码必须幂等 |
| OpenAI Agents SDK | 工具上设置 `needs_approval=True`（或判定函数）；暂停时从 `result.interruptions` 取待审批项，`state = result.to_state()`，`state.approve(item)` / `state.reject(item)`，再 `Runner.run(agent, state)` 继续 | `RunState` 可序列化（`to_json()` / `from_json()`），可以跨进程、隔很久再恢复——与 agentkit 的"落盘 → resume"思路一致 |
| Claude Agent SDK | **权限模式** `permission_mode`：`default` / `acceptEdits` / `plan` / `dontAsk` / `bypassPermissions` / `auto`；规则 `allowed_tools` / `disallowed_tools`；回调 `can_use_tool` | 官方文档给出的评估顺序：**Hooks → deny 规则 → ask 规则 → 权限模式 → allow 规则 → `can_use_tool` 回调**。deny 规则即使在 `bypassPermissions` 模式下也生效。注意 `allowed_tools` 只是"自动批准"，并不限制可用工具；要让模型根本看不到某工具，用 `disallowed_tools` |
| Google ADK | 工具确认：`FunctionTool(fn, require_confirmation=True)` 或在工具内 `tool_context.request_confirmation(...)`；长时工具 `LongRunningFunctionTool`；可恢复运行（resumability） | 工具确认在文档中标注为实验性（Experimental），且有 Session 服务类型限制，使用前请查文档 |
| CrewAI | Task 参数 `human_input=True`；Flows 中的 `@human_feedback` | |
| MS Agent Framework | Agent 层：`@tool(approval_mode="always_require")`，运行返回待处理的审批请求，调用方回应后再次运行；Workflow 层：`ctx.request_info(...)` + `@response_handler` | C# 侧为 `ApprovalRequiredAIFunction` |
| Temporal | Signal / Update 发送审批决定，Workflow 中 `workflow.wait_condition(..., timeout=...)` 等待 | 等待期间不占计算资源，可以等几天；天然支持审批超时 |

**RBAC（按角色可见/可用）**：agentkit 用 `visible_tools` + `before_tool` 双重控制。框架中对应的是：LangChain 中间件的动态工具过滤、OpenAI 工具的 `is_enabled`、Claude 的 `disallowed_tools` / `tools`、ADK 的 `before_tool_callback`、MAF 的 function middleware。**大多数框架不会替你做基于角色的授权**——这部分通常要你自己实现。

### 2.7 检查点与持久化执行（agentkit：`Checkpointer` / `RunState` / `resume`）

| 框架 | 对应机制 | 粒度与注意 |
|---|---|---|
| LangGraph | Checkpointer：`InMemorySaver`、`SqliteSaver`、`PostgresSaver` 等；以 `thread_id` 为键；`get_state_history` 可"时间旅行"；持久化模式 `durability="exit" / "async" / "sync"` | 每个 super-step 保存；`sync` 最安全但最慢，`exit` 只在图退出时保存。Functional API（`@entrypoint` / `@task`）中已完成的 `@task` 恢复时不会重复执行 |
| OpenAI Agents SDK | **Sessions** 自动保存对话历史（`SQLiteSession`、`RedisSession`、`SQLAlchemySession` 等）；运行中途的状态用 `RunState` 序列化 | Session 保存的是"对话历史"，`RunState` 才是"运行到一半的状态"；两者用途不同 |
| Claude Agent SDK | 会话恢复：`resume="<session_id>"`、`fork_session`、`continue_conversation`；文件检查点：`enable_file_checkpointing` + `rewind_files(...)` | 会话转录默认写入本地 JSONL；可配置自定义会话存储（`session_store`） |
| Google ADK | `SessionService`（`InMemorySessionService` / `DatabaseSessionService` / `VertexAiSessionService`）；`Session` 含 `state` 与 `events`；可恢复运行配置 | ADK 文档提示：恢复时工具可能被执行不止一次——**幂等仍然是你的责任** |
| CrewAI | `CheckpointConfig`（Crew / Flow / Agent 均可配置）；Flows 的 `@persist` | |
| MS Agent Framework | Workflow 检查点：在每个 superstep 结束时创建，存储后端 `InMemoryCheckpointStorage` / `FileCheckpointStorage` 等；Agent 会话 `AgentSession` 可序列化 | |
| Temporal | **持久化执行（Durable Execution）**：Event History + Replay（重放）。Workflow 代码必须是确定性的；外部 I/O 放在 Activity | 最彻底的方案。Activity 可能被执行多次，官方建议 Activity 幂等（可用 Workflow Run ID + Activity ID 作为幂等键）——与 agentkit `run_id:call_id` 的思路完全相同 |

> 🔑 **多实例部署时还要注意**：检查点只解决"崩溃后能恢复"，不解决"同一会话被两个 worker 并发写"和"僵尸 worker 在租约过期后仍在写"——这些需要按会话串行化、版本号 CAS 或 fencing token（[第 13 课](../lessons/13_distributed_concurrency/README.md)，[失败模式 D1](failure-modes.md#d1-丢失更新lost-update)、[D2](failure-modes.md#d2-僵尸-workerzombie-worker)）。

> 🔑 **通用规律**：所有检查点方案都存在"副作用已执行、但检查点还没写入"的窗口，框架无法替你消除它。**写操作幂等**是唯一的解（见[失败模式 T5](failure-modes.md#t5-重复副作用duplicate-side-effects)）。

### 2.8 上下文管理（agentkit：`SlidingWindow` / `SummarizingCompactor`）

| 框架 | 对应机制 |
|---|---|
| LangGraph / LangChain | `SummarizationMiddleware`（按 token 阈值触发摘要）、`trim_messages`、`ContextEditingMiddleware`（清理旧工具输出） |
| OpenAI Agents SDK | `OpenAIResponsesCompactionSession`（调用服务端压缩）；handoff 时可用 `input_filter` 过滤传给下一个 Agent 的历史 |
| Claude Agent SDK | **自动压缩（compaction）**：接近上下文上限时自动摘要旧历史；`PreCompact` 钩子可在压缩前介入。需要长期生效的规则应写入 `CLAUDE.md` 等设置文件，而不是依赖对话历史 |
| Google ADK | `EventsCompactionConfig`（滑动窗口 / token 阈值触发的事件摘要） |
| CrewAI | Agent 参数 `respect_context_window=True`（默认），超限时自动摘要 |
| MS Agent Framework | `compaction_strategy`：`SlidingWindowStrategy`、`SummarizationStrategy`、`ToolResultCompactionStrategy` 等（文档标注为实验性） |
| Temporal | 无（Continue-As-New 解决的是 Event History 过大，不是模型上下文问题） |

> agentkit 第 04 课强调的"**按块截断，别拆散 tool_calls 和 tool 结果**"在所有框架中都成立——框架内置的策略大多已经处理了这一点，但如果你自己写过滤逻辑（如 OpenAI 的 `input_filter`、LangChain 的自定义中间件），要自己保证。

### 2.9 长期记忆（agentkit：`MemoryStore` + `memory_tools`）

| 框架 | 对应机制 | 注意 |
|---|---|---|
| LangGraph / LangChain | **Store**：`InMemoryStore`、`PostgresStore`；以 namespace + key 组织；工具中通过 `runtime.store` 访问 | namespace 天然适合放 `(tenant_id, user_id)`——但隔离要你自己保证 |
| OpenAI Agents SDK | 无通用长期记忆原语（Sandbox Agent 另有 `Memory` 能力） | 通常自己用向量库 + 工具实现 |
| Claude Agent SDK | 无专门的记忆 API；通过 `CLAUDE.md` 等设置文件（`setting_sources` 控制加载）提供持久指令 | |
| Google ADK | `MemoryService`（`InMemoryMemoryService`、`VertexAiMemoryBankService` 等）；`search_memory`；内置工具 `load_memory` / `PreloadMemoryTool` | 与 Session（短期）明确分离 |
| CrewAI | 统一的 `Memory` 类（`remember` / `recall` / `forget`），`Crew(memory=True)` 启用 | 旧版的 ShortTermMemory / LongTermMemory / EntityMemory 等分类已被统一 `Memory` 取代，旧教程可能过时 |
| MS Agent Framework | Context Providers（`ContextProvider`，有 `before_run` / `after_run`），并有多种第三方存储集成 | |
| Temporal | — | |

> 企业知识库场景（权限感知检索、ACL 前过滤、删除传播、引用校验）超出了各框架"记忆"组件的范围，需要在检索服务层设计，见 [第 15 课](../lessons/15_enterprise_rag/README.md)。

### 2.10 编排模式（agentkit：`workflows.py` 的 chain / route / parallel / orchestrator_workers / evaluator_optimizer）

| 框架 | 对应机制 |
|---|---|
| LangGraph | 用图（`StateGraph`：节点 + 边 + 条件边）或 Functional API 表达任意流程；官方"Workflows and agents"文档逐一实现了 prompt chaining、parallelization、routing、orchestrator-worker（用 `Send` 动态扇出）、evaluator-optimizer——与 agentkit 的五种模式一一对应 |
| OpenAI Agents SDK | 两种编排观：**由 LLM 编排**（handoff / agents as tools）和**由代码编排**（结构化输出 + 你自己的控制流，如 `asyncio.gather` 并行） |
| Claude Agent SDK | 不提供工作流引擎；在你的代码中编排多次 `query()`，或让主 Agent 调用子代理 |
| Google ADK | 1.x：`SequentialAgent` / `ParallelAgent` / `LoopAgent`（`max_iterations`）；2.0：基于图的 `Workflow`（节点 + 边），官方推荐替代前者 |
| CrewAI | `Process.sequential` / `Process.hierarchical`（需要 `manager_llm` 或 `manager_agent`）；**Flows**（`@start` / `@listen` / `@router`）用于代码控制的事件驱动流程 |
| MS Agent Framework | **Workflows**（`WorkflowBuilder`、Executor、边、superstep）；预置编排模式：sequential、concurrent、handoff、group chat、magentic |
| Temporal | Workflow 代码本身就是编排；Child Workflow 用于拆分 |

### 2.11 多 Agent：Agent 即工具 vs 转交（agentkit：`agent_as_tool`）

这是最容易混淆的一组概念。关键区别在于**控制权是否转移**：

- **Agent 即工具（agents as tools / 主管-专家）**：主 Agent 调用子 Agent，子 Agent 的结果**返回给主 Agent**，主 Agent 始终掌控对话。agentkit 的 `agent_as_tool` 就是这种。
- **转交（handoff / transfer）**：当前 Agent 把对话**整个交给**另一个 Agent，之后由对方直接面对用户。

| 框架 | Agent 即工具（控制权不转移） | 转交（控制权转移） |
|---|---|---|
| LangGraph / LangChain | 在 `@tool` 函数中调用子 Agent（官方称 subagents 模式） | 工具返回 `Command(goto=..., graph=Command.PARENT)`（官方称 handoffs 模式）；旧的 `langgraph-supervisor` 库已不再积极维护，官方推荐 subagents 模式 |
| OpenAI Agents SDK | `agent.as_tool(...)` | `handoffs=[...]` / `handoff(agent, input_filter=..., on_handoff=...)` |
| Claude Agent SDK | **子代理（subagents）**：`agents={name: AgentDefinition(...)}` 或 `.claude/agents/*.md`，主 Agent 通过 Agent 工具调用 | 无直接对应 |
| Google ADK | `AgentTool(agent=...)` | `sub_agents` + `transfer_to_agent` |
| CrewAI | `allow_delegation=True` 后 Agent 获得委派/提问同事的工具 | 待核实 |
| MS Agent Framework | `agent.as_tool()`（C#：`AsAIFunction()`） | `HandoffBuilder` 编排 |
| AutoGen（维护模式） | `AgentTool` / `TeamTool` | `Swarm` + `HandoffMessage` |

> 选择建议：需要**汇总多个专家结果**、需要主 Agent 统一把关（如安全审查）→ Agent 即工具；需要**专家长时间直接与用户对话**（如分诊后转给专门的客服）→ 转交。无论哪种，都要检查[失败模式 O2（委派上下文饥饿）](failure-modes.md#o2-委派上下文饥饿delegation-context-starvation)和 [S8（委派中的权限放大）](failure-modes.md#s8-委派中的权限放大privilege-escalation-via-delegation)。

### 2.12 可靠性：重试、超时、降级（agentkit：`ResilientLLM` / `retry_call` / `CircuitBreaker`）

| 框架 | 对应机制 | 注意 |
|---|---|---|
| LangGraph / LangChain | 节点级 `RetryPolicy`（有 `max_attempts`、退避、`jitter` 等参数）；中间件 `ModelRetryMiddleware`、`ToolRetryMiddleware`、`ModelFallbackMiddleware` | 模型客户端自身也有 `max_retries`——小心与节点重试叠加（[R1 重试风暴](failure-modes.md#r1-重试风暴retry-storm)） |
| OpenAI Agents SDK | 模型重试需显式开启：`ModelSettings(retry=ModelRetrySettings(...))`；工具有 `timeout` 和 `failure_error_function` | |
| Claude Agent SDK | API 重试由底层 CLI 处理；可配置 `fallback_model` | |
| Google ADK | `ReflectAndRetryToolPlugin`（工具失败后让模型反思再重试）；2.0 工作流节点支持重试配置 | |
| CrewAI | Agent 参数 `max_retry_limit`；LLM 参数 `timeout` / `max_retries` | |
| MS Agent Framework | 官方文档建议在中间件中实现重试逻辑 | |
| Temporal | `RetryPolicy`（初始间隔、退避系数、最大间隔、最大次数、不可重试错误类型）；Activity 超时：Start-To-Close、Schedule-To-Close 等 | ⚠️ **Activity 默认会无限次重试**（最大次数默认不限），调用模型时务必把 400/401 这类错误配置为不可重试，并设置合理上限 |

> 熔断器（circuit breaker）在上述框架中基本都**没有内置**，通常放在模型网关层实现；**跨实例的全局限流**、租户公平排队同理（[第 13 课](../lessons/13_distributed_concurrency/README.md)）。

### 2.13 预算与用量（agentkit：`BudgetHook(max_tokens, max_cost_usd, max_tool_calls, max_seconds)`）

| 框架 | 对应机制 | 缺什么 |
|---|---|---|
| LangGraph / LangChain | `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`（按次数） | 无内置 token/金额上限 |
| OpenAI Agents SDK | 用量统计 `result.context_wrapper.usage`；上限只有 `max_turns` | 无内置 token/金额上限 |
| Claude Agent SDK | **`max_budget_usd`**（金额上限，超限结果 `subtype` 为 `error_max_budget_usd`）；结果中的 `total_cost_usd` | 文档说明成本数字是客户端估算，不等于账单 |
| Google ADK | `RunConfig.max_llm_calls` | 无内置金额上限 |
| CrewAI | `max_iter`、`max_execution_time`、`max_rpm`（限速） | 无内置 token/金额总预算 |
| MS Agent Framework | 函数调用配置中的 `max_function_calls`、`max_duration_seconds` | 待核实 |
| Temporal | Workflow / Activity 超时 | — |

> 结论：**金额预算和租户级配额几乎都要自己实现**（钩子/中间件 + 网关）。这正是第 08 课手写 `BudgetHook` 的价值；模型级联、缓存、对冲请求等成本与延迟优化手段见 [第 14 课](../lessons/14_cost_latency/README.md)。

### 2.14 结构化输出（agentkit：`complete_json` + 修复循环）

| 框架 | 对应机制 |
|---|---|
| LangChain v1 | `create_agent(response_format=...)`，策略 `ToolStrategy` / `ProviderStrategy`，结果在 `structured_response` |
| OpenAI Agents SDK | `Agent(output_type=PydanticModel)`，结果 `result.final_output` |
| Claude Agent SDK | `output_format={"type": "json_schema", "schema": {...}}`，结果在 `structured_output` |
| Google ADK | `output_schema`；`output_key` 把最终输出写入 session state |
| CrewAI | Task 参数 `output_pydantic` / `output_json` |
| MS Agent Framework | `response_format=PydanticModel`，结果 `response.value` |

### 2.15 追踪（agentkit：`Tracer` / `Span`，字段参考 OpenTelemetry GenAI 语义约定）

| 框架 | 对应机制 |
|---|---|
| LangGraph / LangChain | LangSmith（设置 `LANGSMITH_TRACING=true` 等环境变量即可自动追踪） |
| OpenAI Agents SDK | **内置追踪，默认开启**；`trace()`、`custom_span()`、`add_trace_processor()` 接入第三方；`trace_include_sensitive_data` 控制是否记录敏感数据 |
| Claude Agent SDK | OpenTelemetry：通过 `CLAUDE_CODE_ENABLE_TELEMETRY` 及 `OTEL_*` 环境变量导出（traces 部分为 beta） |
| Google ADK | OpenTelemetry（官方称实现了 GenAI 语义约定） |
| CrewAI | `tracing=True`（CrewAI 自有平台）及多种第三方集成 |
| MS Agent Framework | OpenTelemetry，遵循 GenAI 语义约定 |
| Temporal | `TracingInterceptor`（OpenTelemetry） |

> ⚠️ 默认开启的追踪意味着**默认上传数据**。企业环境中请检查追踪数据发往何处、是否包含敏感信息（[失败模式 S7](failure-modes.md#s7-敏感信息泄露sensitive-information-disclosure)）。

### 2.16 评估（agentkit：`EvalCase` / `rule_grader` / `llm_judge` / `run_eval` / `regressions`）

| 框架 | 对应机制 |
|---|---|
| LangGraph / LangChain | LangSmith Evaluation（数据集、评估器、实验；支持离线与在线评估） |
| OpenAI Agents SDK | SDK 本身无评估模块（提供确定性测试替身，思路同 agentkit 的 `ScriptedLLM`） |
| Claude Agent SDK | 无评估模块 |
| Google ADK | **内置**：`adk eval` 命令、`*.evalset.json` 评估集、`*.test.json` 测试文件；内置指标包括工具轨迹分 `tool_trajectory_avg_score`、响应匹配分等 |
| CrewAI | `crewai test` 命令 |
| MS Agent Framework | `evaluate_agent()`、`LocalEvaluator` 等 |
| Temporal | — |

> 框架提供的评估工具可以省掉脚手架，但**评估集本身（真实问题 + 期望行为）只能你自己积累**——这是第 11 课的核心。

### 2.17 MCP 与 A2A

| 框架 | MCP | A2A |
|---|---|---|
| LangGraph / LangChain | 当前文档使用 `langchain.mcp.MCPAdapter`（标注 beta）；此前通过 `langchain-mcp-adapters` 包 | 待核实 |
| OpenAI Agents SDK | `Agent(mcp_servers=[MCPServerStdio(...), MCPServerStreamableHttp(...)])`、托管 `HostedMCPTool`；支持工具过滤与审批 | 待核实 |
| Claude Agent SDK | `mcp_servers`（stdio / SSE / HTTP / 进程内 SDK 服务器） | 待核实 |
| Google ADK | `McpToolset` | 支持（文档中有 A2A 专章） |
| CrewAI | Agent 字段 `mcps=[...]`；`MCPServerAdapter` | 支持 A2A 委派 |
| MS Agent Framework | `MCPStdioTool`、`MCPStreamableHTTPTool` 等 | `A2AAgent` |
| Temporal | OpenAI 集成中 MCP 调用作为 Activity 执行 | — |

---

## 3. 各框架快速上手路线（给学完本课程的你）

每个框架只列"你已经会了什么"和"需要重点学的新东西"。

### LangGraph / LangChain v1
- **你已经会了**：Agent 循环、工具、中间件（= 钩子）、检查点、人工审批、五种编排模式。
- **重点新学**：图的心智模型（State、reducer、super-step）；`interrupt()` 恢复时"节点从头重跑"的语义；`thread_id` 与 Store 的 namespace 设计；durability 模式的取舍。
- **从 agentkit 迁移**：`Hook` → `AgentMiddleware`；`Checkpointer` → checkpointer + `thread_id`；`PauseRun` → `interrupt()` 或 `HumanInTheLoopMiddleware`；`workflows.py` 的五种模式 → 官方 "Workflows and agents" 文档中的同名模式。

### OpenAI Agents SDK
- **你已经会了**：几乎全部——它的抽象和 agentkit 最接近。
- **重点新学**：handoff 与 `as_tool` 的区别；护栏只作用于首/尾 Agent 的范围规则；`RunState` 序列化与 `interruptions`；Session 与 RunState 的分工；追踪默认开启（注意数据去向）。
- **从 agentkit 迁移**：`ToolContext` → `RunContextWrapper`；`PermissionPolicy(ask_risks=...)` → `needs_approval`；`BudgetHook` → 自己用 `RunHooks` + usage 实现。

### Claude Agent SDK
- **你已经会了**：钩子、权限、会话恢复、子代理、上下文压缩。
- **重点新学**：它是"带电池的 Agent"——内置了文件、命令、搜索等强力工具，所以**权限配置是第一优先级**；六种权限模式与评估顺序；`allowed_tools` 只是自动批准而不限制可用工具；自定义工具以进程内 MCP 服务器形式提供；`max_turns` 默认不设上限，`max_budget_usd` 可直接做金额预算。
- **从 agentkit 迁移**：`PermissionPolicy` → `permission_mode` + 规则 + `can_use_tool`；`before_tool` → `PreToolUse`；`agent_as_tool` → subagents。

### Google ADK
- **你已经会了**：回调（= 钩子）、Session / Memory 分离、Agent 即工具 vs 转交、评估。
- **重点新学**：Plugins 做全局策略；`RunConfig.max_llm_calls`；Session state 前缀（如 `user:` / `app:` / `temp:`）；`adk eval` 与 evalset 格式；ADK 2.0 的图工作流。

### CrewAI
- **你已经会了**：多 Agent、委派、护栏的修复循环、记忆。
- **重点新学**：以"角色 + 任务 + 流程"建模的思路；Crews（自治协作）与 Flows（代码控制）的组合；注意它的高层抽象下隐藏了很多提示词——排查问题时要打开追踪看实际发给模型的内容。

### Microsoft Agent Framework
- **你已经会了**：中间件、审批、检查点、多 Agent 编排。
- **重点新学**：三类中间件的分工（agent / function / chat）；Workflows 的 superstep 与检查点；五种预置编排模式；Python API 近期有大量改名（如 `ChatAgent` → `Agent`、`@ai_function` → `@tool`），网上的旧示例可能跑不通。
- **从 AutoGen 迁移**：微软提供了官方迁移指南（见文末链接）。

### Temporal
- **你已经会了**：检查点、幂等、重试退避、暂停等人——这些正是 Temporal 的核心价值。
- **重点新学**：Workflow 确定性约束（不能在 Workflow 代码里直接调用模型、读时间、取随机数，这些都要走 Activity 或 SDK 提供的确定性 API）；Signal / Query / Update；Activity 的超时类型与默认无限重试；Event History 的大小限制与 Continue-As-New。
- **什么时候需要它**：Agent 任务要运行几小时到几天、要等人审批、绝对不能丢进度、需要跨服务的可靠编排（长任务交付方式的选择见 [第 13 课](../lessons/13_distributed_concurrency/README.md)）。

---

## 4. 选型速查

| 你的情况 | 可以优先考虑 | 理由 |
|---|---|---|
| 想要最少的抽象、最接近本课程的代码 | OpenAI Agents SDK，或者继续用自己的 agentkit 风格代码 | 原语少、概念一一对应 |
| 复杂的有状态流程、需要精细控制每一步 | LangGraph | 图 + 检查点 + interrupt 是它的强项 |
| 需要一个"能读写文件、跑命令"的通用 Agent | Claude Agent SDK | 内置工具与权限体系完整 |
| Google Cloud / Gemini 技术栈，想要内置评估 | Google ADK | 与 Vertex AI 集成，评估 CLI 开箱即用 |
| 快速搭多角色协作原型 | CrewAI | 高层抽象，上手快 |
| .NET 技术栈或微软生态；从 AutoGen / Semantic Kernel 迁移 | Microsoft Agent Framework | 官方继任者，.NET 与 Python 双栈 |
| 长时运行、必须可靠恢复、要等人几天 | Temporal（可与上面任一框架组合） | 持久化执行是它的本职 |

> 另外，没有一个框架会替你管理"代码 + 提示词 + 模型版本 + 工具 Schema"的整体发布、灰度和回滚——这部分见 [第 16 课](../lessons/16_release_ops/README.md)。

> 无论选哪个框架，本课程的[设计评审清单](design-review-checklist.md)都适用——框架帮你省掉的是"写循环"的工作，省不掉的是**权限设计、幂等、评估集、成本治理**这些企业级决策。

---

## 5. 常见误解与已改名/弃用的名称（2026-09 核实）

| 你可能在旧资料中看到 | 现在应该用 / 真实情况 |
|---|---|
| `langgraph.prebuilt.create_react_agent` | LangGraph v1 已弃用，改用 `langchain.agents.create_agent` |
| LangGraph 的 `pre_model_hook` / `post_model_hook` | 改用中间件的 `before_model` / `after_model` |
| `langgraph-supervisor` | 官方称不再积极维护，推荐 subagents 模式 |
| "Claude Code SDK"、`claude_code_sdk`、`ClaudeCodeOptions` | Claude Agent SDK、`claude_agent_sdk`、`ClaudeAgentOptions` |
| Claude Agent SDK 只有 4 种权限模式 | 当前文档列出 6 种：`default`、`acceptEdits`、`plan`、`dontAsk`、`bypassPermissions`、`auto` |
| "OpenAI Agents SDK 不支持人工审批" | 已支持：`needs_approval` + `interruptions` + 可序列化的 `RunState` |
| `google.github.io/adk-docs` | 已迁至 `adk.dev` |
| ADK 用 `SequentialAgent` / `ParallelAgent` / `LoopAgent` 编排 | ADK 2.0 起官方推荐基于图的 `Workflow`，模板工作流被标为 superseded |
| MAF Python 的 `ChatAgent`、`@ai_function`、`AIFunction` | 已改名为 `Agent`、`@tool`、`FunctionTool` |
| AutoGen 是微软主推的 Agent 框架 | AutoGen 已进入维护模式，继任者是 Microsoft Agent Framework |
| CrewAI 的短期/长期/实体记忆（ShortTermMemory 等） | 已统一为单一 `Memory` 类 |
| Temporal Activity "只执行一次" | 可能被执行多次（重试），必须幂等 |

---

## 附：本文核实所用的官方文档入口

| 框架 | 官方文档 |
|---|---|
| LangGraph / LangChain | https://docs.langchain.com/oss/python/langgraph/overview ；中间件 https://docs.langchain.com/oss/python/langchain/middleware/built-in ；人工审批 https://docs.langchain.com/oss/python/langgraph/interrupts ；检查点 https://docs.langchain.com/oss/python/langgraph/checkpointers ；v1 迁移 https://docs.langchain.com/oss/python/migrate/langgraph-v1 |
| OpenAI Agents SDK | https://openai.github.io/openai-agents-python/ ；人工审批 https://openai.github.io/openai-agents-python/human_in_the_loop/ ；护栏 https://openai.github.io/openai-agents-python/guardrails/ ；handoff https://openai.github.io/openai-agents-python/handoffs/ ；上下文 https://openai.github.io/openai-agents-python/context/ |
| Claude Agent SDK | https://code.claude.com/docs/en/agent-sdk/overview ；权限 https://code.claude.com/docs/en/agent-sdk/permissions ；钩子 https://code.claude.com/docs/en/agent-sdk/hooks ；子代理 https://code.claude.com/docs/en/agent-sdk/subagents ；迁移指南 https://code.claude.com/docs/en/agent-sdk/migration-guide |
| Google ADK | https://adk.dev/ ；回调 https://adk.dev/callbacks/ ；工具确认 https://adk.dev/tools-custom/confirmation/ ；评估 https://adk.dev/evaluate/ ；工作流 https://adk.dev/agents/workflow-agents/ |
| CrewAI | https://docs.crewai.com/en/concepts/agents ；任务与护栏 https://docs.crewai.com/en/concepts/tasks ；Flows https://docs.crewai.com/en/concepts/flows ；记忆 https://docs.crewai.com/en/concepts/memory |
| Microsoft Agent Framework | https://learn.microsoft.com/en-us/agent-framework/overview/ ；中间件 https://learn.microsoft.com/en-us/agent-framework/concepts/agents/middleware/ ；工具审批 https://learn.microsoft.com/en-us/agent-framework/agents/tools/tool-approval ；编排 https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/ ；从 AutoGen 迁移 https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen/ |
| AutoGen | https://github.com/microsoft/autogen （README 中的维护模式声明）；https://microsoft.github.io/autogen/stable/ |
| Temporal | https://docs.temporal.io/ ；消息传递（Signal/Query/Update）https://docs.temporal.io/encyclopedia/workflow-message-passing ；重试策略 https://docs.temporal.io/encyclopedia/retry-policies ；OpenAI Agents SDK 集成 https://docs.temporal.io/develop/python/integrations/openai-agents |
