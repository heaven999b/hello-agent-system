[中文](framework-comparison.md) | [English](framework-comparison.en.md)

# agentkit Concepts ↔ Mainstream Frameworks

> 📖 Part of the "domain reference handbook". The goal: **after this course, you can get productive in any mainstream agent framework within a day or two**, because you already know the problems these frameworks solve. You only need to find out what each concept is called in a given framework.
> Related docs: [Glossary](glossary.en.md) · [Cheatsheet](cheatsheet.en.md) · [Further reading](reading-list.en.md)

> ⚠️ **Verification note**: every framework concept name in this doc was checked item by item against each framework's official docs in **September 2026** (official links are at the end). Agent frameworks move fast, and API renames and deprecations are common (this doc records several of them), so **check the current official docs before you write code**. Anything we couldn't confirm is marked "to be verified"; where we found no corresponding mechanism, the cell says "—" (no direct equivalent).

---

## 0. Core Insight: Every Framework Solves the Same Problems

Every agentkit module you built by hand in this course maps to a **problem that no agent framework can avoid**. Frameworks differ mainly in which of these problems they build in, which abstractions they use to express them, and what defaults they choose.

| agentkit module | Problem it solves | Lesson |
|---|---|---|
| `Agent.run` / `max_steps` | The model ↔ tool loop, and when to stop | [Lesson 01](../lessons/01_agent_loop/README.en.md) |
| `@tool` / `ToolRegistry` | Turning functions into tools the model can call: schemas, validation, timeouts, truncation | [Lesson 02](../lessons/02_tools/README.en.md) |
| `ToolContext` | Trusted information such as identity is injected by the system, never filled in by the model | [Lesson 02](../lessons/02_tools/README.en.md) |
| `Hook` (7 hooks) | Inserting cross-cutting logic (security, budgets, audit, ...) at key points in the loop | [Lesson 01](../lessons/01_agent_loop/README.en.md) |
| `SlidingWindow` / `SummarizingCompactor` | What to do when the context gets too long | [Lesson 03](../lessons/03_context_memory/README.en.md) |
| `MemoryStore` | Remembering things across sessions | [Lesson 03](../lessons/03_context_memory/README.en.md) |
| `workflows.py` | Code-controlled vs. model-controlled flow; multi-agent systems | [Lesson 04](../lessons/04_orchestration/README.en.md) |
| `ResilientLLM` / `BudgetHook` | Retries, circuit breaking, fallbacks, budgets | [Lesson 05](../lessons/05_reliability/README.en.md) |
| `Checkpointer` / `RunState` / `resume` | Crash recovery, pausing to wait for a human | [Lesson 05](../lessons/05_reliability/README.en.md) |
| `InputGuard` / `ToolOutputGuard` / `OutputGuard` | Guardrails | [Lesson 06](../lessons/06_security/README.en.md) |
| `PermissionPolicy` / `PauseRun` | Least privilege, human approval | [Lesson 06](../lessons/06_security/README.en.md) |
| `Tracer` / `Span` | Tracing | [Lesson 07](../lessons/07_observability/README.en.md) |
| `evals.py` | Evals | [Lesson 08](../lessons/08_evals/README.en.md) |

> Part 2 of the course covers [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) (distributed systems and high concurrency), [Lesson 11](../lessons/11_cost_latency/README.en.md) (cost and latency), [Lesson 12](../lessons/12_enterprise_rag/README.en.md) (enterprise RAG), and [Lesson 13](../lessons/13_release_ops/README.en.md) (release and operations). The problems discussed there (concurrent writes to a session, delivery semantics, global rate limiting, cache isolation, permission-aware retrieval, progressive rollout and rollback) **mostly fall outside an agent framework's responsibilities** and belong to your infrastructure layer. Where frameworks can help is covered in 2.7 (durable execution), 2.12 (retries), 2.13 (budgets), and 2.9 (memory/retrieval). The rest you have to design yourself, which is exactly why those lessons exist.

---

## 1. Know What Each Framework Is For

| Framework | In one sentence | Abstraction level | Current status (verified 2026-09) |
|---|---|---|---|
| **LangGraph** + LangChain v1 | LangGraph is a low-level **graph orchestration runtime** (state, nodes, edges, checkpoints). LangChain v1's `create_agent` is a high-level agent API built on top of it, with cross-cutting logic expressed as **middleware** | Low level + high level | LangGraph v1 deprecated `langgraph.prebuilt.create_react_agent`; the official migration guide says to switch to `langchain.agents.create_agent` |
| **OpenAI Agents SDK** | A set of lightweight primitives: Agent, Runner, tools, handoffs, guardrails, Sessions, tracing | Mid level | Actively evolving; already supports human approval and a serializable `RunState` |
| **Claude Agent SDK** | Ships Claude Code's agent harness (built-in file/command/search tools, permissions, sessions, context compaction, subagents) as a library | High level ("an agent out of the box") | Formerly the Claude Code SDK, since renamed; `ClaudeCodeOptions` → `ClaudeAgentOptions` |
| **Google ADK** | A code-first agent development kit: LlmAgent, Runner, callbacks, plugins, Session/Memory services, an eval CLI | Mid-to-high level | Docs have moved to adk.dev; ADK 2.0 introduced the graph-based `Workflow` and marked template workflows such as `SequentialAgent` as superseded |
| **CrewAI** | Role-playing multi-agent teams (Agent / Task / Crew / Process), plus event-driven Flows | High level | Actively evolving; memory has been unified into a single `Memory` class |
| **Microsoft Agent Framework** | Microsoft's official agent framework (.NET / Python): agents + middleware + Workflows + several orchestration patterns | Mid level + workflows | 1.0 is out; Microsoft calls it the direct successor to Semantic Kernel and AutoGen |
| **AutoGen** | A multi-agent conversation framework (AgentChat / Core) | Mid level | The GitHub README says it is in **maintenance mode** with no new features planned, and points to Microsoft Agent Framework as its successor |
| **Temporal** | A **general-purpose durable execution engine**, not an agent framework: Workflows (deterministic orchestration code) + Activities (steps with side effects) | Infrastructure | Offers official integrations with the OpenAI Agents SDK and others |

> 💡 **The first rule of choosing**: ask which layer you're missing. Missing the agent loop and tools → a mid- or high-level framework. Missing "runs for a long time, survives crashes, can wait days for a human" → durable execution (LangGraph checkpointers / Temporal). Missing both → combine them (e.g., the official Temporal + OpenAI Agents SDK integration).

---

## 2. Concept-by-Concept Comparison

### 2.1 Agent loop and step limit (agentkit: `Agent.run` + `max_steps`)

| Framework | Equivalent | Differences from agentkit / caveats |
|---|---|---|
| LangGraph / LangChain | `create_agent` (the model ↔ tool loop); graph-level `recursion_limit`, which raises `GraphRecursionError` when exceeded; middleware `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` | `recursion_limit` counts graph **super-steps**, not model calls; the current docs give a default of 1000. To cap model calls, use `ModelCallLimitMiddleware` |
| OpenAI Agents SDK | `Runner.run()` / `run_sync()` / `run_streamed()`; `max_turns`, which raises `MaxTurnsExceeded` when exceeded | The closest to agentkit; `error_handlers` can return fallback output when the limit is hit instead of raising |
| Claude Agent SDK | `query()` (one-shot) / `ClaudeSDKClient` (multi-turn sessions); `ClaudeAgentOptions(max_turns=...)` | When the limit is hit, the result message's `subtype` is `error_max_turns`. The docs say there is no limit by default, so **always set one explicitly in production** |
| Google ADK | `LlmAgent` (alias `Agent`) + `Runner`; `RunConfig(max_llm_calls=...)` | Caps the **total number of model calls** in a run; the docs give a default of 500 |
| CrewAI | Agent parameters `max_iter`, `max_execution_time` | When `max_iter` is reached, the agent is asked for its "best answer so far" |
| MS Agent Framework | Python: `Agent` (formerly `ChatAgent`) + `agent.run()`; parameters such as `max_iterations` in the function-calling configuration | On the C# side: `AIAgent` / `ChatClientAgent` |
| Temporal | No agent loop; you write the `while` loop yourself in Workflow code, with each model call/tool call as an Activity | You implement the step limit yourself |

### 2.2 Tool definitions (agentkit: `@tool` + auto-generated schema + validation + timeouts + truncation + risk tiers)

| Framework | Equivalent | Notes |
|---|---|---|
| LangGraph / LangChain | `@tool` (`langchain.tools`), `args_schema`; `ToolNode` executes tools in a graph | The schema is generated from type annotations + the docstring |
| OpenAI Agents SDK | `@function_tool` (the docs also show a shorter `@tool` alias), `FunctionTool`; hosted tools such as `WebSearchTool` | The decorator supports `needs_approval`, `timeout`, and `failure_error_function` (which turns tool exceptions into text for the model, i.e., agentkit's "errors as observations") |
| Claude Agent SDK | `@tool(name, description, input_schema)` + `create_sdk_mcp_server(...)`, which provides custom tools as an in-process MCP server; plus many built-in tools (Read, Edit, Bash, etc.) | The model sees tool names of the form `mcp__<server>__<tool>`; use the full name when writing permission rules |
| Google ADK | `FunctionTool`; plain functions placed in `tools` are wrapped automatically; `LongRunningFunctionTool` for long-running tasks | |
| CrewAI | `@tool("name")`, or subclass `BaseTool` (`name` / `description` / `args_schema` / `_run`) | |
| MS Agent Framework | Python: `@tool` (formerly `@ai_function`), `FunctionTool` (formerly `AIFunction`); C#: `AIFunctionFactory.Create` | `@tool` supports `approval_mode` |
| Temporal | Activities (`@activity.defn`); in the OpenAI integration, `activity_as_tool()` turns an Activity into a tool | Every tool execution automatically gets retries, timeouts, and durability |

> **Risk tiers** (agentkit's `risk="read|write|dangerous"`) mostly have no standard field in these frameworks. They usually show up as "does this tool need approval?": OpenAI `needs_approval`, MAF `approval_mode`, ADK `require_confirmation`, LangChain `HumanInTheLoopMiddleware(interrupt_on=...)`. We recommend keeping an explicit risk tier in your own tool-registration layer and mapping it onto the framework's approval mechanism.

### 2.3 Trusted context injection (agentkit: `ToolContext`; identity is never left to the model)

This is the most critical item for enterprises, and the easiest one to overlook (see [failure mode S4](failure-modes.en.md#s4-confused-deputy)).

| Framework | Equivalent | Notes |
|---|---|---|
| LangGraph / LangChain | `ToolRuntime` (`runtime.state` / `runtime.context` / `runtime.store` / `runtime.tool_call_id`) | Runtime parameters are invisible to the model |
| OpenAI Agents SDK | `RunContextWrapper[T]`, passed in via `Runner.run(..., context=obj)` | The docs state explicitly that the context object is **never sent to the model**, which matches agentkit's design intent |
| Claude Agent SDK | We found no injection object that directly corresponds to `ToolContext` (to be verified) | A workable approach: when you create the session, capture the trusted identity in the tool function with a closure instead of exposing it to the model as a tool parameter |
| Google ADK | `ToolContext` (gives access to Session state, among other things) | Identity should come from the Session (`user_id`), not from model arguments |
| CrewAI | To be verified | |
| MS Agent Framework | `FunctionInvocationContext` | |
| Temporal | Workflow inputs / Activity arguments are passed by your code | Identity comes from whoever starts the Workflow |

### 2.4 Hooks and middleware (agentkit: the 7 hooks of `Hook`)

agentkit's hook points: `on_run_start → [before_llm → LLM → after_llm → (before_tool → tool → after_tool)*]* → on_final → on_run_end`, plus `visible_tools`.

| agentkit | LangChain v1 middleware | OpenAI Agents SDK | Claude Agent SDK | Google ADK | MS Agent Framework | CrewAI |
|---|---|---|---|---|---|---|
| Abstraction name | `AgentMiddleware` | `RunHooks` / `AgentHooks` | `hooks` + `HookMatcher` | Callbacks; Plugins for global policies | Middleware (three kinds: agent / function / chat) | Execution hooks |
| `on_run_start` | `before_agent` | `on_agent_start` | `UserPromptSubmit` | `before_agent_callback` | agent middleware | To be verified |
| `before_llm` | `before_model` / `wrap_model_call` | `on_llm_start` | — | `before_model_callback` | chat middleware | `@before_llm_call` (newer syntax: `@on(InterceptionPoint.PRE_MODEL_CALL)`) |
| `after_llm` | `after_model` / `wrap_model_call` | `on_llm_end` | — | `after_model_callback` | chat middleware | `@after_llm_call` |
| `visible_tools` | Filter tools by state in `wrap_model_call` (officially called dynamic tools) | `is_enabled` (per-tool switch) | `allowed_tools` / `disallowed_tools` / `tools` | To be verified | To be verified | To be verified |
| `before_tool` | `wrap_tool_call` | `on_tool_start` | `PreToolUse` | `before_tool_callback` | function middleware | `@before_tool_call` |
| `after_tool` | `wrap_tool_call` | `on_tool_end` | `PostToolUse` | `after_tool_callback` | function middleware | `@after_tool_call` |
| `on_final` / `on_run_end` | `after_agent` | `on_agent_end` | `Stop` | `after_agent_callback` | agent middleware | To be verified |

Two design differences worth noting:

1. **Observing vs. intercepting**: OpenAI's `RunHooks` are mainly for observation (logging, metrics); real interception and rewriting go through guardrails or tool-level configuration. LangChain's `wrap_*` and MAF's middleware follow the "onion model": they can run logic both before and after a call, or even skip calling the next layer entirely (short-circuiting). agentkit's hooks sit in between: returning a string from `before_tool` denies the call, and raising `StopRun` / `PauseRun` aborts or pauses the run.
2. **Claude Agent SDK hooks can make permission decisions**: `PreToolUse` can return decisions such as allow / deny / ask, and it can also rewrite the tool input. It comes first in the permission evaluation chain (see 2.6).

### 2.5 Guardrails (agentkit: `InputGuard` / `ToolOutputGuard` / `OutputGuard`)

| Framework | Equivalent | Notes |
|---|---|---|
| LangGraph / LangChain | No standalone Guardrail class; built with middleware. Built-in `PIIMiddleware` (strategies such as redact / mask / hash / block) | The official guardrails docs split them into "deterministic guardrails" and "model-based guardrails" |
| OpenAI Agents SDK | `@input_guardrail` / `@output_guardrail`, which return `GuardrailFunctionOutput(tripwire_triggered=...)` and raise `InputGuardrailTripwireTriggered` and similar exceptions when tripped; plus tool-level `@tool_input_guardrail` / `@tool_output_guardrail` | **Input guardrails apply only to the first agent in the chain, and output guardrails only to the last one.** Input guardrails run in parallel with the agent by default ("optimistic execution"); if you need them to block, configure that explicitly |
| Claude Agent SDK | No primitive called "guardrail"; built with the `UserPromptSubmit` (input) and `PreToolUse` / `PostToolUse` (around tool calls) hooks, plus permission rules | |
| Google ADK | Built with Callbacks or Plugins; the official safety docs recommend Plugins for policies that apply across agents | |
| CrewAI | Task parameters `guardrail` / `guardrails` (a function or a natural-language description), `guardrail_max_retries` | On a validation failure, the error is fed back to the agent for a retry, similar to the repair loop in agentkit's `complete_json` |
| MS Agent Framework | Terminate execution in middleware (`MiddlewareTermination`); there's a dedicated official "Termination & Guardrails" doc | |
| Temporal | Nothing built in; implement rule checks in Activities | |

> Whatever the framework, remember the conclusion of Lesson 06: **guardrails lower the probability; permissions limit the consequences**. The guardrails that frameworks provide cover layers 1, 2, and 4. Layer 3 (least privilege + approval) depends on the mechanisms in 2.6.

### 2.6 Permissions and human approval (agentkit: `PermissionPolicy` + `PauseRun` + `resume` / `approve`)

| Framework | Equivalent | How pause/resume works |
|---|---|---|
| LangGraph / LangChain | Low level: call `interrupt()` inside a node and resume with `invoke(Command(resume=...))`. High level: `HumanInTheLoopMiddleware(interrupt_on=...)`, with decision types that include approve / edit / reject | Relies on a checkpointer + `thread_id`. ⚠️ **On resume, the whole node re-runs from the start**, not from the `interrupt()` line, so any code before `interrupt()` must be idempotent |
| OpenAI Agents SDK | Set `needs_approval=True` (or a predicate function) on the tool. When the run pauses, take the pending items from `result.interruptions`, call `state = result.to_state()`, then `state.approve(item)` / `state.reject(item)`, and continue with `Runner.run(agent, state)` | `RunState` is serializable (`to_json()` / `from_json()`), so a run can resume in another process much later. The same idea as agentkit's "persist → resume" |
| Claude Agent SDK | **Permission modes** via `permission_mode`: `default` / `acceptEdits` / `plan` / `dontAsk` / `bypassPermissions` / `auto`; rules `allowed_tools` / `disallowed_tools`; the `can_use_tool` callback | The evaluation order in the official docs: **hooks → deny rules → ask rules → permission mode → allow rules → `can_use_tool` callback**. Deny rules apply even in `bypassPermissions` mode. Note that `allowed_tools` only auto-approves; it doesn't restrict which tools are available. To hide a tool from the model entirely, use `disallowed_tools` |
| Google ADK | Tool confirmation: `FunctionTool(fn, require_confirmation=True)`, or `tool_context.request_confirmation(...)` inside the tool; long-running tools via `LongRunningFunctionTool`; resumable runs (resumability) | The docs mark tool confirmation as Experimental, with restrictions on Session service types. Check the docs before relying on it |
| CrewAI | Task parameter `human_input=True`; `@human_feedback` in Flows | |
| MS Agent Framework | Agent level: `@tool(approval_mode="always_require")`; the run returns pending approval requests, and the caller responds and runs again. Workflow level: `ctx.request_info(...)` + `@response_handler` | On the C# side: `ApprovalRequiredAIFunction` |
| Temporal | Send the approval decision with a Signal / Update, and wait in the Workflow with `workflow.wait_condition(..., timeout=...)` | Waiting consumes no compute and can last for days; approval timeouts are supported natively |

**RBAC (visibility and availability by role)**: agentkit uses `visible_tools` + `before_tool` for double control. The framework equivalents are dynamic tool filtering in LangChain middleware, OpenAI's per-tool `is_enabled`, Claude's `disallowed_tools` / `tools`, ADK's `before_tool_callback`, and MAF's function middleware. **Most frameworks won't do role-based authorization for you**; you usually have to build that part yourself.

### 2.7 Checkpoints and durable execution (agentkit: `Checkpointer` / `RunState` / `resume`)

| Framework | Equivalent | Granularity and caveats |
|---|---|---|
| LangGraph | Checkpointers: `InMemorySaver`, `SqliteSaver`, `PostgresSaver`, etc.; keyed by `thread_id`; `get_state_history` enables "time travel"; durability modes `durability="exit" / "async" / "sync"` | Saves at every super-step. `sync` is the safest but slowest; `exit` saves only when the graph exits. In the Functional API (`@entrypoint` / `@task`), a completed `@task` isn't re-executed on resume |
| OpenAI Agents SDK | **Sessions** save conversation history automatically (`SQLiteSession`, `RedisSession`, `SQLAlchemySession`, etc.); mid-run state is serialized with `RunState` | A Session stores "conversation history"; `RunState` is "a run caught halfway through". They serve different purposes |
| Claude Agent SDK | Session resumption: `resume="<session_id>"`, `fork_session`, `continue_conversation`. File checkpoints: `enable_file_checkpointing` + `rewind_files(...)` | Session transcripts are written to local JSONL by default; you can configure a custom session store (`session_store`) |
| Google ADK | `SessionService` (`InMemorySessionService` / `DatabaseSessionService` / `VertexAiSessionService`); a `Session` holds `state` and `events`; resumable-run configuration | The ADK docs warn that tools may be executed more than once on resume: **idempotency is still your job** |
| CrewAI | `CheckpointConfig` (configurable on Crews, Flows, and Agents); `@persist` for Flows | |
| MS Agent Framework | Workflow checkpoints: created at the end of every superstep, with storage backends such as `InMemoryCheckpointStorage` / `FileCheckpointStorage`; the agent session `AgentSession` is serializable | |
| Temporal | **Durable execution**: Event History + Replay. Workflow code must be deterministic; external I/O belongs in Activities | The most thorough solution. Activities may be executed more than once, and the official advice is to make Activities idempotent (the Workflow Run ID + Activity ID can serve as the idempotency key), exactly the same idea as agentkit's `run_id:call_id` |

> 🔑 **For multi-instance deployments, also note**: checkpoints only solve "recover after a crash". They don't solve "two workers writing the same session concurrently" or "a zombie worker still writing after its lease expired". Those need per-session serialization, version-number CAS, or fencing tokens ([Lesson 10](../lessons/10_distributed_concurrency/README.en.md), [failure mode D1](failure-modes.en.md#d1-lost-update), [D2](failure-modes.en.md#d2-zombie-worker)).

> 🔑 **The general rule**: every checkpointing scheme has a window where the side effect has already happened but the checkpoint hasn't been written yet, and no framework can close that window for you. **Idempotent writes** are the only fix (see [failure mode T5](failure-modes.en.md#t5-duplicate-side-effects)).

### 2.8 Context management (agentkit: `SlidingWindow` / `SummarizingCompactor`)

| Framework | Equivalent |
|---|---|
| LangGraph / LangChain | `SummarizationMiddleware` (summarizes when a token threshold is reached), `trim_messages`, `ContextEditingMiddleware` (clears out old tool outputs) |
| OpenAI Agents SDK | `OpenAIResponsesCompactionSession` (calls server-side compaction); on a handoff, `input_filter` can filter the history passed to the next agent |
| Claude Agent SDK | **Automatic compaction**: summarizes old history automatically as the context approaches its limit; the `PreCompact` hook lets you step in before compaction. Rules that must persist belong in settings files such as `CLAUDE.md`, rather than relying on conversation history |
| Google ADK | `EventsCompactionConfig` (event summarization triggered by a sliding window / token threshold) |
| CrewAI | Agent parameter `respect_context_window=True` (the default); summarizes automatically when the limit is exceeded |
| MS Agent Framework | `compaction_strategy`: `SlidingWindowStrategy`, `SummarizationStrategy`, `ToolResultCompactionStrategy`, etc. (marked experimental in the docs) |
| Temporal | None (Continue-As-New addresses an oversized Event History, not the model's context) |

> The rule Lesson 03 stresses, "**truncate by block; never separate tool_calls from their tool results**", holds in every framework. Most built-in strategies already handle it, but if you write your own filtering logic (e.g., OpenAI's `input_filter` or custom LangChain middleware), guaranteeing it is up to you.

### 2.9 Long-term memory (agentkit: `MemoryStore` + `memory_tools`)

| Framework | Equivalent | Notes |
|---|---|---|
| LangGraph / LangChain | **Store**: `InMemoryStore`, `PostgresStore`; organized by namespace + key; accessed from tools via `runtime.store` | A namespace is a natural place to put `(tenant_id, user_id)`, but isolation is still up to you |
| OpenAI Agents SDK | No general-purpose long-term memory primitive (Sandbox Agents have a separate `Memory` capability) | Usually built yourself with a vector store + tools |
| Claude Agent SDK | No dedicated memory API; persistent instructions come from settings files such as `CLAUDE.md` (loading is controlled by `setting_sources`) | |
| Google ADK | `MemoryService` (`InMemoryMemoryService`, `VertexAiMemoryBankService`, etc.); `search_memory`; built-in tools `load_memory` / `PreloadMemoryTool` | Clearly separated from the Session (short-term) |
| CrewAI | A unified `Memory` class (`remember` / `recall` / `forget`), enabled with `Crew(memory=True)` | The old ShortTermMemory / LongTermMemory / EntityMemory categories have been replaced by the unified `Memory`, so older tutorials may be out of date |
| MS Agent Framework | Context Providers (`ContextProvider`, with `before_run` / `after_run`), plus integrations with various third-party stores | |
| Temporal | — | |

> Enterprise knowledge-base scenarios (permission-aware retrieval, ACL pre-filtering, deletion propagation, citation verification) go beyond what any framework's "memory" component covers. They need to be designed at the retrieval-service layer; see [Lesson 12](../lessons/12_enterprise_rag/README.en.md).

### 2.10 Orchestration patterns (agentkit: chain / route / parallel / orchestrator_workers / evaluator_optimizer in `workflows.py`)

| Framework | Equivalent |
|---|---|
| LangGraph | Express any flow as a graph (`StateGraph`: nodes + edges + conditional edges) or with the Functional API. The official "Workflows and agents" doc implements prompt chaining, parallelization, routing, orchestrator-worker (dynamic fan-out with `Send`), and evaluator-optimizer one by one, mapping one-to-one onto agentkit's five patterns |
| OpenAI Agents SDK | Two views of orchestration: **LLM-driven** (handoffs / agents as tools) and **code-driven** (structured output + your own control flow, e.g., `asyncio.gather` for parallelism) |
| Claude Agent SDK | No workflow engine; orchestrate multiple `query()` calls in your own code, or have the main agent call subagents |
| Google ADK | 1.x: `SequentialAgent` / `ParallelAgent` / `LoopAgent` (`max_iterations`); 2.0: the graph-based `Workflow` (nodes + edges), which the docs recommend in place of the former |
| CrewAI | `Process.sequential` / `Process.hierarchical` (requires `manager_llm` or `manager_agent`); **Flows** (`@start` / `@listen` / `@router`) for code-controlled, event-driven flows |
| MS Agent Framework | **Workflows** (`WorkflowBuilder`, Executors, edges, supersteps); prebuilt orchestration patterns: sequential, concurrent, handoff, group chat, magentic |
| Temporal | The Workflow code itself is the orchestration; use Child Workflows to break things up |

### 2.11 Multi-agent: agent as tool vs. handoff (agentkit: `agent_as_tool`)

This is the most commonly confused pair of concepts. The key difference is **whether control transfers**:

- **Agent as tool (agents as tools / supervisor-expert)**: the main agent calls a subagent, and the subagent's result **comes back to the main agent**, which stays in control of the conversation throughout. agentkit's `agent_as_tool` works this way.
- **Handoff (handoff / transfer)**: the current agent **hands the entire conversation** to another agent, which then deals with the user directly.

| Framework | Agent as tool (control stays) | Handoff (control transfers) |
|---|---|---|
| LangGraph / LangChain | Call the subagent inside a `@tool` function (officially the subagents pattern) | The tool returns `Command(goto=..., graph=Command.PARENT)` (officially the handoffs pattern). The old `langgraph-supervisor` library is no longer actively maintained, and the docs recommend the subagents pattern |
| OpenAI Agents SDK | `agent.as_tool(...)` | `handoffs=[...]` / `handoff(agent, input_filter=..., on_handoff=...)` |
| Claude Agent SDK | **Subagents**: `agents={name: AgentDefinition(...)}` or `.claude/agents/*.md`, called by the main agent through the Agent tool | No direct equivalent |
| Google ADK | `AgentTool(agent=...)` | `sub_agents` + `transfer_to_agent` |
| CrewAI | With `allow_delegation=True`, an agent gets tools to delegate work to, or ask questions of, its coworkers | To be verified |
| MS Agent Framework | `agent.as_tool()` (C#: `AsAIFunction()`) | `HandoffBuilder` orchestration |
| AutoGen (maintenance mode) | `AgentTool` / `TeamTool` | `Swarm` + `HandoffMessage` |

> How to choose: if you need to **combine results from several experts**, or need the main agent to act as a single gatekeeper (e.g., for security review) → agent as tool. If an **expert needs to talk directly with the user for a long time** (e.g., triage, then transfer to a specialized support agent) → handoff. Either way, check [failure mode O2 (Delegation Context Starvation)](failure-modes.en.md#o2-delegation-context-starvation) and [S8 (Privilege Escalation via Delegation)](failure-modes.en.md#s8-privilege-escalation-via-delegation).

### 2.12 Reliability: retries, timeouts, fallbacks (agentkit: `ResilientLLM` / `retry_call` / `CircuitBreaker`)

| Framework | Equivalent | Notes |
|---|---|---|
| LangGraph / LangChain | Node-level `RetryPolicy` (with parameters such as `max_attempts`, backoff, and `jitter`); middleware `ModelRetryMiddleware`, `ToolRetryMiddleware`, `ModelFallbackMiddleware` | The model client has its own `max_retries` too, so watch out for stacking it with node retries ([R1 Retry Storm](failure-modes.en.md#r1-retry-storm)) |
| OpenAI Agents SDK | Model retries must be enabled explicitly: `ModelSettings(retry=ModelRetrySettings(...))`; tools have `timeout` and `failure_error_function` | |
| Claude Agent SDK | API retries are handled by the underlying CLI; `fallback_model` is configurable | |
| Google ADK | `ReflectAndRetryToolPlugin` (has the model reflect and retry after a tool failure); 2.0 workflow nodes support retry configuration | |
| CrewAI | Agent parameter `max_retry_limit`; LLM parameters `timeout` / `max_retries` | |
| MS Agent Framework | The official docs suggest implementing retry logic in middleware | |
| Temporal | `RetryPolicy` (initial interval, backoff coefficient, maximum interval, maximum attempts, non-retryable error types); Activity timeouts: Start-To-Close, Schedule-To-Close, etc. | ⚠️ **Activities retry indefinitely by default** (maximum attempts is unlimited by default). When calling a model, be sure to mark errors such as 400/401 as non-retryable and set a sensible cap |

> **None** of these frameworks really has a circuit breaker **built in**; it usually lives in the model gateway layer. The same goes for **global rate limiting across instances** and fair queuing across tenants ([Lesson 10](../lessons/10_distributed_concurrency/README.en.md)).

### 2.13 Budgets and usage (agentkit: `BudgetHook(max_tokens, max_cost_usd, max_tool_calls, max_seconds)`)

| Framework | Equivalent | What's missing |
|---|---|---|
| LangGraph / LangChain | `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` (by count) | No built-in token/dollar cap |
| OpenAI Agents SDK | Usage stats in `result.context_wrapper.usage`; the only cap is `max_turns` | No built-in token/dollar cap |
| Claude Agent SDK | **`max_budget_usd`** (a dollar cap; when exceeded, the result's `subtype` is `error_max_budget_usd`); `total_cost_usd` in the result | The docs note that cost figures are client-side estimates and don't equal your bill |
| Google ADK | `RunConfig.max_llm_calls` | No built-in dollar cap |
| CrewAI | `max_iter`, `max_execution_time`, `max_rpm` (rate limit) | No built-in overall token/dollar budget |
| MS Agent Framework | `max_function_calls`, `max_duration_seconds` in the function-calling configuration | To be verified |
| Temporal | Workflow / Activity timeouts | — |

> Bottom line: **you'll almost always have to build dollar budgets and tenant-level quotas yourself** (hooks/middleware + a gateway). That's exactly why Lesson 05 has you write `BudgetHook` by hand. For cost and latency techniques such as model cascades, caching, and hedged requests, see [Lesson 11](../lessons/11_cost_latency/README.en.md).

### 2.14 Structured output (agentkit: `complete_json` + repair loop)

| Framework | Equivalent |
|---|---|
| LangChain v1 | `create_agent(response_format=...)`, with the strategies `ToolStrategy` / `ProviderStrategy`; the result is in `structured_response` |
| OpenAI Agents SDK | `Agent(output_type=PydanticModel)`; the result is `result.final_output` |
| Claude Agent SDK | `output_format={"type": "json_schema", "schema": {...}}`; the result is in `structured_output` |
| Google ADK | `output_schema`; `output_key` writes the final output into session state |
| CrewAI | Task parameters `output_pydantic` / `output_json` |
| MS Agent Framework | `response_format=PydanticModel`; the result is `response.value` |

### 2.15 Tracing (agentkit: `Tracer` / `Span`, with fields based on the OpenTelemetry GenAI semantic conventions)

| Framework | Equivalent |
|---|---|
| LangGraph / LangChain | LangSmith (set environment variables such as `LANGSMITH_TRACING=true` for automatic tracing) |
| OpenAI Agents SDK | **Built-in tracing, on by default**; `trace()`, `custom_span()`, and `add_trace_processor()` for third-party integrations; `trace_include_sensitive_data` controls whether sensitive data is recorded |
| Claude Agent SDK | OpenTelemetry: export via `CLAUDE_CODE_ENABLE_TELEMETRY` and the `OTEL_*` environment variables (traces are in beta) |
| Google ADK | OpenTelemetry (the docs say it implements the GenAI semantic conventions) |
| CrewAI | `tracing=True` (CrewAI's own platform) plus a variety of third-party integrations |
| MS Agent Framework | OpenTelemetry, following the GenAI semantic conventions |
| Temporal | `TracingInterceptor` (OpenTelemetry) |

> ⚠️ Tracing that's on by default means data is **uploaded by default**. In an enterprise environment, check where trace data goes and whether it contains sensitive information ([failure mode S7](failure-modes.en.md#s7-sensitive-information-disclosure)).

### 2.16 Evals (agentkit: `EvalCase` / `rule_grader` / `llm_judge` / `run_eval` / `regressions`)

| Framework | Equivalent |
|---|---|
| LangGraph / LangChain | LangSmith Evaluation (datasets, evaluators, experiments; supports offline and online evals) |
| OpenAI Agents SDK | No eval module in the SDK itself (it provides deterministic test doubles, similar in spirit to agentkit's `ScriptedLLM`) |
| Claude Agent SDK | No eval module |
| Google ADK | **Built in**: the `adk eval` command, `*.evalset.json` eval sets, and `*.test.json` test files; built-in metrics include the tool trajectory score `tool_trajectory_avg_score` and a response match score |
| CrewAI | The `crewai test` command |
| MS Agent Framework | `evaluate_agent()`, `LocalEvaluator`, etc. |
| Temporal | — |

> Framework eval tooling saves you the scaffolding, but **the eval set itself (real questions + expected behavior) is something only you can build up**. That's the core of Lesson 08.

### 2.17 MCP and A2A

| Framework | MCP | A2A |
|---|---|---|
| LangGraph / LangChain | The current docs use `langchain.mcp.MCPAdapter` (marked beta); previously via the `langchain-mcp-adapters` package | To be verified |
| OpenAI Agents SDK | `Agent(mcp_servers=[MCPServerStdio(...), MCPServerStreamableHttp(...)])`, plus the hosted `HostedMCPTool`; supports tool filtering and approval | To be verified |
| Claude Agent SDK | `mcp_servers` (stdio / SSE / HTTP / in-process SDK servers) | To be verified |
| Google ADK | `McpToolset` | Supported (the docs have a dedicated A2A section) |
| CrewAI | Agent field `mcps=[...]`; `MCPServerAdapter` | Supports A2A delegation |
| MS Agent Framework | `MCPStdioTool`, `MCPStreamableHTTPTool`, etc. | `A2AAgent` |
| Temporal | In the OpenAI integration, MCP calls run as Activities | — |

---

## 3. Quick-Start Paths for Each Framework (Now That You've Finished This Course)

For each framework, we list only what you already know and the new things to focus on.

### LangGraph / LangChain v1
- **You already know**: the agent loop, tools, middleware (= hooks), checkpoints, human approval, and the five orchestration patterns.
- **New things to focus on**: the graph mental model (State, reducers, super-steps); the semantics of resuming after `interrupt()` (the node re-runs from the start); designing `thread_id` and Store namespaces; the trade-offs between durability modes.
- **Migrating from agentkit**: `Hook` → `AgentMiddleware`; `Checkpointer` → a checkpointer + `thread_id`; `PauseRun` → `interrupt()` or `HumanInTheLoopMiddleware`; the five patterns in `workflows.py` → the patterns of the same names in the official "Workflows and agents" doc.

### OpenAI Agents SDK
- **You already know**: nearly all of it. Its abstractions are the closest to agentkit's.
- **New things to focus on**: how handoffs differ from `as_tool`; the scoping rule that guardrails apply only to the first/last agent; `RunState` serialization and `interruptions`; the division of labor between Session and RunState; tracing is on by default (watch where the data goes).
- **Migrating from agentkit**: `ToolContext` → `RunContextWrapper`; `PermissionPolicy(ask_risks=...)` → `needs_approval`; `BudgetHook` → build your own with `RunHooks` + usage.

### Claude Agent SDK
- **You already know**: hooks, permissions, session resumption, subagents, context compaction.
- **New things to focus on**: it's an agent "with batteries included", shipping powerful built-in tools for files, commands, and search, so **permission configuration is priority number one**; the six permission modes and the evaluation order; `allowed_tools` only auto-approves and doesn't restrict which tools are available; custom tools are provided as in-process MCP servers; `max_turns` has no limit by default, and `max_budget_usd` gives you a dollar budget directly.
- **Migrating from agentkit**: `PermissionPolicy` → `permission_mode` + rules + `can_use_tool`; `before_tool` → `PreToolUse`; `agent_as_tool` → subagents.

### Google ADK
- **You already know**: callbacks (= hooks), the Session / Memory split, agent as tool vs. handoff, evals.
- **New things to focus on**: Plugins for global policies; `RunConfig.max_llm_calls`; Session state prefixes (such as `user:` / `app:` / `temp:`); `adk eval` and the evalset format; ADK 2.0's graph workflows.

### CrewAI
- **You already know**: multi-agent systems, delegation, the guardrail repair loop, memory.
- **New things to focus on**: modeling in terms of "roles + tasks + process"; combining Crews (autonomous collaboration) with Flows (code control). Note that its high-level abstractions hide a lot of prompting, so when you debug, turn on tracing to see what is actually sent to the model.

### Microsoft Agent Framework
- **You already know**: middleware, approvals, checkpoints, multi-agent orchestration.
- **New things to focus on**: the division of labor among the three kinds of middleware (agent / function / chat); Workflow supersteps and checkpoints; the five prebuilt orchestration patterns. The Python API has had many recent renames (e.g., `ChatAgent` → `Agent`, `@ai_function` → `@tool`), so older examples online may not run.
- **Migrating from AutoGen**: Microsoft provides an official migration guide (linked at the end of this doc).

### Temporal
- **You already know**: checkpoints, idempotency, retry with backoff, pausing to wait for a human. These are exactly Temporal's core value.
- **New things to focus on**: Workflow determinism constraints (you can't call models, read the clock, or generate random numbers directly in Workflow code; these must go through Activities or the SDK's deterministic APIs); Signals / Queries / Updates; Activity timeout types and the default of infinite retries; Event History size limits and Continue-As-New.
- **When you need it**: agent tasks that run for hours or days, wait for human approval, absolutely must not lose progress, or need reliable orchestration across services (for how to choose a delivery model for long-running tasks, see [Lesson 10](../lessons/10_distributed_concurrency/README.en.md)).

---

## 4. Choosing a Framework: Quick Reference

| Your situation | Consider first | Why |
|---|---|---|
| You want the fewest abstractions and code closest to this course | OpenAI Agents SDK, or keep writing your own agentkit-style code | Few primitives, and the concepts map one-to-one |
| Complex stateful flows that need fine-grained control over every step | LangGraph | Graph + checkpoints + interrupt is its strength |
| You need a general-purpose agent that can read and write files and run commands | Claude Agent SDK | Complete built-in tools and permission system |
| A Google Cloud / Gemini stack, and you want built-in evals | Google ADK | Integrates with Vertex AI; the eval CLI works out of the box |
| You want to prototype multi-role collaboration quickly | CrewAI | High-level abstractions, quick to pick up |
| A .NET stack or the Microsoft ecosystem; migrating from AutoGen / Semantic Kernel | Microsoft Agent Framework | The official successor, with both .NET and Python |
| Long-running work that must recover reliably and may wait days for a human | Temporal (can be combined with any framework above) | Durable execution is its core job |

> Also note that no framework will manage the combined release, progressive rollout, and rollback of "code + prompts + model version + tool schemas" for you. See [Lesson 13](../lessons/13_release_ops/README.en.md) for that.

> Whichever framework you choose, this course's [design review checklist](design-review-checklist.en.md) still applies. A framework saves you from writing the loop. It doesn't save you from the enterprise decisions: **permission design, idempotency, eval sets, and cost governance**.

---

## 5. Common Misconceptions and Renamed or Deprecated Names (verified 2026-09)

| What you may see in older material | What to use now / what's actually true |
|---|---|
| `langgraph.prebuilt.create_react_agent` | Deprecated in LangGraph v1; use `langchain.agents.create_agent` |
| LangGraph's `pre_model_hook` / `post_model_hook` | Use the middleware hooks `before_model` / `after_model` instead |
| `langgraph-supervisor` | Officially no longer actively maintained; the subagents pattern is recommended |
| "Claude Code SDK", `claude_code_sdk`, `ClaudeCodeOptions` | Claude Agent SDK, `claude_agent_sdk`, `ClaudeAgentOptions` |
| The Claude Agent SDK has only 4 permission modes | The current docs list 6: `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`, `auto` |
| "The OpenAI Agents SDK doesn't support human approval" | It does: `needs_approval` + `interruptions` + a serializable `RunState` |
| `google.github.io/adk-docs` | Moved to `adk.dev` |
| ADK orchestrates with `SequentialAgent` / `ParallelAgent` / `LoopAgent` | Since ADK 2.0, the docs recommend the graph-based `Workflow`; the template workflows are marked superseded |
| MAF Python's `ChatAgent`, `@ai_function`, `AIFunction` | Renamed to `Agent`, `@tool`, `FunctionTool` |
| AutoGen is Microsoft's flagship agent framework | AutoGen is in maintenance mode; its successor is Microsoft Agent Framework |
| CrewAI's short-term/long-term/entity memory (ShortTermMemory, etc.) | Unified into a single `Memory` class |
| Temporal Activities "run only once" | They may run more than once (retries), so they must be idempotent |

---

## Appendix: Official Documentation Used for Verification

| Framework | Official docs |
|---|---|
| LangGraph / LangChain | https://docs.langchain.com/oss/python/langgraph/overview ; middleware https://docs.langchain.com/oss/python/langchain/middleware/built-in ; human approval https://docs.langchain.com/oss/python/langgraph/interrupts ; checkpoints https://docs.langchain.com/oss/python/langgraph/checkpointers ; v1 migration https://docs.langchain.com/oss/python/migrate/langgraph-v1 |
| OpenAI Agents SDK | https://openai.github.io/openai-agents-python/ ; human approval https://openai.github.io/openai-agents-python/human_in_the_loop/ ; guardrails https://openai.github.io/openai-agents-python/guardrails/ ; handoffs https://openai.github.io/openai-agents-python/handoffs/ ; context https://openai.github.io/openai-agents-python/context/ |
| Claude Agent SDK | https://code.claude.com/docs/en/agent-sdk/overview ; permissions https://code.claude.com/docs/en/agent-sdk/permissions ; hooks https://code.claude.com/docs/en/agent-sdk/hooks ; subagents https://code.claude.com/docs/en/agent-sdk/subagents ; migration guide https://code.claude.com/docs/en/agent-sdk/migration-guide |
| Google ADK | https://adk.dev/ ; callbacks https://adk.dev/callbacks/ ; tool confirmation https://adk.dev/tools-custom/confirmation/ ; evaluation https://adk.dev/evaluate/ ; workflows https://adk.dev/agents/workflow-agents/ |
| CrewAI | https://docs.crewai.com/en/concepts/agents ; tasks and guardrails https://docs.crewai.com/en/concepts/tasks ; Flows https://docs.crewai.com/en/concepts/flows ; memory https://docs.crewai.com/en/concepts/memory |
| Microsoft Agent Framework | https://learn.microsoft.com/en-us/agent-framework/overview/ ; middleware https://learn.microsoft.com/en-us/agent-framework/concepts/agents/middleware/ ; tool approval https://learn.microsoft.com/en-us/agent-framework/agents/tools/tool-approval ; orchestrations https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/ ; migrating from AutoGen https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen/ |
| AutoGen | https://github.com/microsoft/autogen (see the maintenance-mode notice in the README); https://microsoft.github.io/autogen/stable/ |
| Temporal | https://docs.temporal.io/ ; message passing (Signal/Query/Update) https://docs.temporal.io/encyclopedia/workflow-message-passing ; retry policies https://docs.temporal.io/encyclopedia/retry-policies ; OpenAI Agents SDK integration https://docs.temporal.io/develop/python/integrations/openai-agents |
