[中文](README.md) | [English](README.en.md)

# Lesson 12: Production architecture — from script to service

> 🕐 Time: 15 min | 🎯 You'll be able to: draw a reference architecture for an enterprise agent platform and explain what problem each component solves, and make sound calls on the key architectural decisions: sync vs async, stateful vs stateless, tenant isolation, framework choice, and model gateways | 📦 Source: this lesson's exercise (multi-tenant rate limiting, model routing); the mini deployment [`mini_api.py`](mini_api.py) (API process), [`worker_app.py`](worker_app.py) (worker process), [`deployment.py`](deployment.py) (starts them as real processes); [`agentkit/distributed/`](../../agentkit/distributed/__init__.py), [`agentkit/limits.py`](../../agentkit/limits.py), [`agentkit/state.py`](../../agentkit/state.py)
>
> 📖 Primary reading: [12-Factor Agents - Principles for building reliable LLM applications](https://github.com/humanlayer/12-factor-agents) (Dex Horthy, 2025) — HumanLayer's twelve principles for turning an agent into software you can put in front of customers, mapping directly onto this lesson's problem cards on async tasks, stateless workers, and framework choice; focus on Factor 5 (unify execution state and business state), Factor 6 (launch/pause/resume with simple APIs), and Factor 8 (own your control flow).

## 0. In one sentence

**Cooking dinner at home and running a restaurant chain that serves over ten thousand guests a day are two completely different jobs.** A restaurant chain needs a host stand and a queue-number system, cooks who can cover for one another, order tickets that record exactly what each table ordered, centralized purchasing, food-safety spot checks, and clean books for every branch.

Agents are the same. `python agent.py` runs just fine on your laptop. Then, at 9 a.m. on a Monday, 5,000 employees open the HR assistant at the same time:

| Symptom | What's missing | Where it's covered |
|---|---|---|
| The model provider returns 429 and half the requests fail | Rate limiting; retries and fallback in the model gateway | This lesson's exercise, Lesson 08, Problem 5 |
| One department's script calls the agent in an infinite loop and burns through the whole company's quota | Multi-tenant rate limiting | This lesson's exercise, demo Sections 4 and 5 |
| "Summarize my department's leave last quarter" takes 2 minutes, but the gateway times out at 60 seconds | Async tasks | Problem 1, demo Section 2 |
| A rolling deploy loses all 200 in-flight tasks | Stateless workers + checkpoints | Problem 2, demo Section 3, Lesson 13 |
| Company A's employees see Company B's policies | Multi-tenant isolation | Problem 3, Lesson 15 |
| Someone edits the prompt, everyone gets wrong answers all afternoon, and rolling back requires a new deploy | Config center, progressive rollout, rollback | Lesson 16 |
| At month-end, finance asks: how much did AI cost, and which department should pay for what? | Cost attribution | This lesson's demo, Lesson 14 |

This lesson is the **architecture overview** for Part 2. It starts with a complete reference architecture diagram and what problem each component solves, then works through 5 key architecture-level decisions. Deeper topics (concurrency, cost, RAG, release and operations) each get their own lesson in Lessons 13-16.

## 1. Core concepts

### 1.1 Reference architecture

```mermaid
flowchart TB
    U["Users / business systems"] --> GW["API gateway<br/>auth · tenant resolution · rate limiting"]
    subgraph CORE["Agent platform"]
        GW --> SS["Session service<br/>conversation history · streaming"]
        SS --> Q["Task queue"]
        Q --> W["Agent runtime workers<br/>stateless · horizontally scalable"]
        W <--> ST[("State store<br/>checkpoints · sessions · approvals")]
        W <--> VS[("Vector store / memory<br/>isolated by tenant and permission")]
        W --> MG["Model gateway<br/>routing · quotas · billing · fallback"]
        W --> TS["Tool services<br/>MCP servers"]
        TS --> SB["Sandbox<br/>code execution · browser"]
    end
    MG --> P1["Model provider A"]
    MG --> P2["Model provider B"]
    TS --> BIZ["Enterprise systems<br/>HR · ticketing · ERP"]
    subgraph GOV["Governance and operations"]
        CFG["Prompt / config center"]
        OBS["Observability"]
        EV["Eval pipeline"]
        AUD["Audit log"]
    end
    CFG -.-> W
    W -.-> OBS
    W -.-> AUD
    OBS -.-> EV
```

### 1.2 Component map: what each component solves

| Component | Problem it solves | In agentkit | Deep dive |
|---|---|---|---|
| API gateway | Authenticates callers, identifies the tenant, rate-limits, rejects oversized requests | This lesson's exercise: `TenantRateLimiter`; the mini deployment's [`mini_api.py`](mini_api.py) | [Lesson 13](../13_distributed_concurrency/README.en.md) (global rate limiting and backpressure) |
| Session service | Multi-turn conversation history, streaming, reconnecting after a dropped connection | `RunResult.history` | Problem 1 |
| Task queue | Makes long tasks async, smooths out traffic peaks, retries failures | `agentkit.distributed.SQLiteJobQueue` | [Lesson 13](../13_distributed_concurrency/README.en.md) (lease-based queues, delivery semantics) |
| Agent runtime workers | Run the agent loop; stateless, so they can scale up or down at any time | [agent.py](../../agentkit/agent.py); `run_worker` + `AgentJobHandler` (worker processes) | Problem 2, [Lesson 02](../02_agent_loop/README.en.md), [Lesson 13](../13_distributed_concurrency/README.en.md) |
| State store | Checkpoints, sessions, approval state | [state.py](../../agentkit/state.py); `SQLiteCheckpointer` (shared across processes, fence takeover) | [Lesson 08](../08_reliability/README.en.md), [Lesson 13](../13_distributed_concurrency/README.en.md) (concurrent writes) |
| Vector store / memory | Knowledge retrieval and long-term memory, isolated by tenant and permission | [memory.py](../../agentkit/memory.py) | [Lesson 04](../04_context_memory/README.en.md), [Lesson 15](../15_enterprise_rag/README.en.md) |
| Model gateway | Unified interface, key custody, routing, quotas, billing, fallback, caching | [llm.py](../../agentkit/llm.py), [reliability.py](../../agentkit/reliability.py), this lesson's exercise: `choose_model` | Problem 5, [Lesson 14](../14_cost_latency/README.en.md) |
| Tool services | Wrap enterprise systems as standardized tools (MCP) | [tools.py](../../agentkit/tools.py) | [Lesson 03](../03_tools/README.en.md), Section 1.5 |
| Sandbox | Isolated execution of untrusted code and web browsing | — | [Lesson 09](../09_security/README.en.md) |
| Guardrails and permissions | Injection detection, redaction, least privilege, human approval | [guardrails.py](../../agentkit/guardrails.py), [permissions.py](../../agentkit/permissions.py) | [Lesson 09](../09_security/README.en.md) |
| Prompt / config center | Versioning, progressive rollout, and rollback for prompts and config | — | [Lesson 16](../16_release_ops/README.en.md) |
| Observability | Tracing, metrics, alerting | [tracing.py](../../agentkit/tracing.py), [viewer.py](../../agentkit/viewer.py) | [Lesson 10](../10_observability/README.en.md) |
| Eval pipeline | Offline evals, CI gates, online sampled evaluation | [evals.py](../../agentkit/evals.py) | [Lesson 11](../11_evals/README.en.md) |
| Audit log | Who did what, when, and under which identity | [audit.py](../../agentkit/audit.py) | [Lesson 09](../09_security/README.en.md) |

### 1.3 The journey of a request

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant G as API gateway
    participant W as Agent worker
    participant S as State store
    participant M as Model gateway
    participant T as Tool service
    U->>G: POST /runs (with JWT)
    G->>G: Authenticate, resolve tenant, rate-limit
    G->>W: Forward, injecting trusted tenant_id / user_id
    W->>S: Load conversation history / checkpoint
    loop Each step
        W->>M: chat (logical model name)
        M-->>W: Tool call or final answer
        W->>T: Execute tool (identity passed along in context)
        T-->>W: Tool result
        W->>S: Write checkpoint
        W-->>U: Push progress (SSE, via the gateway)
    end
    W-->>U: Final answer
```

Three key points:

1. **Identity is verified once, at the gateway, and then passed down as trusted context** (`ToolContext` from Lesson 03). The model never gets a chance to "declare who it is."
2. **Every step writes a checkpoint**, so any worker can be replaced at any moment (Problem 2).
3. **Workers only know logical model names** (`mini`, `pro`). Which provider and which model sit behind each name is up to the model gateway (Problem 5).

### 1.4 Two gatekeepers: rate limiting and model routing (this lesson's exercise)

**Rate limiting: the token bucket.** Picture a bucket that holds at most b tokens, with r tokens dripping in every second at a steady rate; once it's full, the extra tokens spill over. Each request has to take tokens to get through. If there aren't enough, it gets `429 Too Many Requests`, with a `Retry-After` header telling the client how long to wait before trying again.

```mermaid
flowchart LR
    R["r tokens drip in per second"] --> B[("Bucket: at most b")]
    B --> C{"Request arrives<br/>enough tokens in the bucket?"}
    C -- "Yes: take the tokens" --> OK["Allow"]
    C -- "No" --> NO["429 + Retry-After"]
```

- **b sets the burst size**: after an idle stretch, how many requests can go through back to back. **r sets the long-term average rate.**
- Compared with a **fixed window** of "at most N per minute," a token bucket has no window-boundary problem. With a fixed window, up to 2N requests can get through in a short span straddling the boundary between two windows.
- For LLMs you need to limit two things at once: requests (RPM) and tokens (TPM). A request can deduct tokens based on its expected token usage; that's what `try_acquire(tokens=...)` is for.
- **One bucket per tenant** prevents the "noisy neighbor" problem; a global bucket around them protects the total upstream model quota. With multiple replicas, bucket state has to live in shared storage and be updated atomically: in demo Section 4, two API processes with their own in-memory buckets let through 16 requests (the configured limit is about 9), while a shared `SQLiteTokenBucket` let through 8. Across machines it goes into Redis; Stripe's engineering blog post *Scaling your API with rate limiters* describes their approach based on token buckets and Redis. Distributed rate limiting and backpressure are covered in [Lesson 13](../13_distributed_concurrency/README.en.md).

**Model routing: good enough is good enough.** Most requests are simple and can go to a cheap model; only the hard ones need a strong model. The rule order in this lesson's exercise is: **hard constraints first (does it need tool calling, does the context fit), then quality requirements (high complexity means strong models only), and only then pick the cheapest of the remaining candidates**. In the demo, routing this way cuts the daily bill by 69% compared with "use the strong model for everything." A step further is a **cascade**: let the cheap model answer first, and escalate to the strong model if the answer fails validation; see [Lesson 14](../14_cost_latency/README.en.md). Keep in mind that routing changes who answers, so every model that requests can be routed to needs its own run of the eval set (Lesson 11).

### 1.5 MCP and A2A: standard interfaces in two directions

| | MCP (Model Context Protocol) | A2A (Agent2Agent) |
|---|---|---|
| What it solves | How AI applications and agents **connect to tools and data** (vertical) | How **different agents** collaborate (horizontal), even when they use different frameworks and come from different vendors |
| Origin and stewardship | Released by Anthropic in November 2024; donated in December 2025 to the Agentic AI Foundation, newly formed under the Linux Foundation | Released by Google in April 2025; donated to the Linux Foundation in June 2025 |
| Core concepts | Servers expose tools, resources, and prompts; a client inside the AI application connects to servers and calls them | An Agent Card describes an agent's capabilities; agents collaborate in units of "tasks" |
| Place in the architecture | The tool service layer: wrap HR, ticketing, and ERP systems as MCP servers that any MCP-capable agent can use | Connecting your agents with agents from other teams and other companies |

The A2A website sums up the relationship like this: use MCP to give a single agent the tools it needs, and use A2A to let those agents work together. When an enterprise connects to MCP servers, it should manage them like a **supply chain**: connect only vetted servers, pin their versions, and remember that tool descriptions themselves can hide injected instructions (Lesson 09).

## 2. Enterprise problem cards

### Problem 1: A task takes 2 minutes. How do you get the result back to the user?

**Scenario**: In an HR assistant, "summarize my department's leave last quarter and generate a report" takes 8 steps and 90 seconds on average, and up to 4 minutes at worst, while the company load balancer's idle timeout is 60 seconds. Leave requests also have to wait for a manager's approval, which may come hours later.

**Why it's hard**: Synchronous requests get cut off by the gateway timeout. A user staring at a blank page for 90 seconds assumes it's stuck and keeps clicking "retry," which creates duplicate submissions. And no network connection will survive hours of waiting for approval.

| Option | How | Pros | Cons | Best for |
|---|---|---|---|---|
| A. Synchronous request-response | One HTTP request that waits for the agent to finish before returning | Simplest; easiest client code | Fails once it exceeds the gateway timeout; users wait with no feedback; ties up connections for a long time | Q&A that finishes within a few seconds |
| B. Streaming (SSE) | The server keeps pushing events over `text/event-stream`: tokens, "Looking up leave records…", the final result | Feels fast because the first event arrives quickly; simpler than WebSocket; the browser's `EventSource` reconnects automatically and sends `Last-Event-ID` | Still one long-lived connection, subject to gateway and proxy timeouts and buffering; what happens to the task when the connection drops needs a separate design | Interactive chat (a few seconds to a minute or two) |
| C. Async tasks | Return `202 Accepted` and a task_id immediately on submission; the task goes into a queue and a worker runs it; the client polls for status, or the server pushes the result via webhook or notification | Not bound by connection lifetime; queues absorb peaks and failures can be retried; naturally supports "pause for approval" | More complex architecture (queue, state store, notifications); you need to design the "in progress" user experience | Long tasks, batch jobs, flows that need human approval |

**How to choose**: Decide by task duration and whether a human needs to step in. Use A for anything that finishes within 10 seconds, default to B for interactive chat, and use C for anything longer than a minute or two, or anything that may pause for approval. The most common real-world pattern is **C + B combined**: the task runs asynchronously with its state persisted, the client subscribes to progress over SSE, and after a disconnect it resubscribes using the task_id. Remember: **streaming is just a transport, and a task's lifecycle must never be tied to a connection.** If the connection drops, the task keeps running.

**In this lesson**: The mini deployment implements option C (demo Section 2). On `POST /runs`, an API process only authenticates, rate-limits, routes, and enqueues, returning `202` and a run_id in a measured 8 ms; a worker process runs the task in the background while the client polls `GET /runs/{run_id}`. The control, `POST /runs/sync`, runs the agent inside the request: the client's 2-second timeout cuts it off, yet the server finishes the run 2.5 seconds later — the money is spent and nobody gets the result. "Pause for approval, then resume on any worker" uses the same machinery: `AgentJobHandler`'s `resume` jobs ([Lesson 13](../13_distributed_concurrency/README.en.md)); [capstone/server.py](../../capstone/server.py) shows how to write `POST /runs/{run_id}/approval`. Task queues, leases, and delivery semantics are covered in [Lesson 13](../13_distributed_concurrency/README.en.md).

### Problem 2: Should agent workers keep sessions in memory?

**Scenario**: The agent service runs on 3 machines, with rolling deploys twice a day. During one deploy, all 200 in-flight tasks are lost; some of them had already submitted leave requests on users' behalf but hadn't told the users the outcome yet. Meanwhile, some users' second turns get load-balanced to a different machine, and the agent has no memory of the previous turn.

**Why it's hard**: Keeping state in memory is the fastest and simplest option, but machines restart and scale up and down. Moving state out of process creates new problems: what if two workers handle the same session at once? What if the process crashes after a tool has run but before the checkpoint is written?

| Option | How | Pros | Cons | Best for |
|---|---|---|---|---|
| A. Stateful + sticky sessions | The load balancer pins each session to one machine, and state lives in memory | Simple; lowest latency | Restarts and deploys lose state; uneven load; can't scale freely | Prototypes, small internal tools |
| B. Stateless workers + external state | Write state to Postgres / Redis at every step (checkpoints), so any worker can pick up any task | Horizontal scaling, rolling deploys, and crash recovery all become simple | An extra read/write per step; you have to handle concurrency (locks or version numbers) and the "executed but not recorded" window (idempotency) | The default for most production systems |
| C. Durable execution engine | Use an engine like Temporal: write orchestration logic as workflows and model and tool calls as activities; the engine records event history and replays it to recover after a crash | Retries, timeouts, long waits, and crash recovery are guaranteed by the engine | One more piece of infrastructure; programming constraints (workflow code must be deterministic); steep learning curve | Long flows that span multiple systems where failure is costly (e.g. anything involving money) |

**How to choose**: Default to B. Consider C when flows are long, span multiple systems, and failure is costly. Use A only for prototypes. Whether you choose B or C, **write tools must be idempotent** (Lesson 08): replaying a tool call during recovery must never charge a customer twice or submit a request twice.

**In this lesson**: agentkit implements option B with checkpoints: the main loop calls `checkpointer.save` at every step. In the mini deployment, checkpoints live in a `SQLiteCheckpointer` shared by every process (demo Section 3): a long task is at step 1 when the worker process holding it gets `kill -9`; once the lease expires, another worker process claims it (second claim, fence 1 → 2), picks up from the checkpoint to finish steps 2 and 3, and the checkpoint's last writer changes to the new worker — step 1, already done, isn't redone. The client just keeps polling, and it doesn't matter which of the two API processes answers, because the state isn't in any process's memory. In production, swap SQLite for Postgres / Redis (Lesson 26). For multiple workers writing to the same session concurrently (distributed locks, optimistic concurrency, per-partition serialization, fencing tokens), see [Lesson 13](../13_distributed_concurrency/README.en.md).

### Problem 3: How far should multi-tenant isolation go?

**Scenario**: A SaaS HR assistant serves 300 enterprise customers. Two of them are banks that require "our data must not be stored alongside other customers' data"; most of the rest are price-sensitive small and midsize businesses. In one incident, the semantic cache wasn't partitioned by tenant, and Company A's employees received Company B's annual leave policy.

**Why it's hard**: The more complete the isolation, the safer, but cost and operational burden grow linearly with the number of tenants. The more you share, the cheaper it gets, but every layer (database, vector store, cache, memory, logs, quotas) has to isolate tenants correctly, and missing a single spot means a data leak.

The three models in the table below borrow their terms from the AWS whitepaper *SaaS Tenant Isolation Strategies*:

| Option | How | Pros | Cons | Best for |
|---|---|---|---|---|
| A. Dedicated deployment (silo) | Each tenant gets its own services and storage | Strongest isolation, meets strict compliance requirements; one tenant's problems don't affect others | Expensive; upgrading and operating hundreds of deployments is a heavy burden | A few large customers, heavily regulated industries |
| B. Shared deployment + logical isolation (pool) | All tenants share services and storage; every record carries a tenant_id, and every query, cache key, and vector search filters by tenant | Low cost, simple operations, high resource utilization | Isolation depends entirely on correct code, and a single missing filter leaks data; noisy neighbors | Large numbers of small and midsize tenants |
| C. Hybrid (bridge) | Shared by default; large customers or sensitive components get dedicated deployments (e.g. separate databases and vector stores), while the compute layer stays shared | Lets you trade cost against isolation case by case | Both models coexist, so architecture and operations get more complex | SaaS products whose customers vary widely in size |

**How to choose**: Tier customers by compliance requirements first. Customers with an explicit "physical isolation" requirement get A, or at least C with a dedicated data layer; everyone else gets B. With B, isolation must be the **framework's default behavior**, not something every developer has to remember to add: identity is injected by the gateway, the storage layer enforces tenant_id, cache keys include tenant_id, each tenant has its own rate-limit bucket and quota, and cost is attributed per tenant. Isolation points that agent systems are especially prone to miss: long-term memory, semantic caches, retrieval indexes, tool credentials (each tenant should use its own credentials to access its own enterprise systems), and trace data.

**In this lesson**: The exercise's `TenantRateLimiter` gives each tenant its own bucket; in agentkit, identity in `ToolContext` is injected by the system, and `MemoryStore` isolates data by tenant and user (Lesson 04). In the mini deployment, identity comes from an API key (standing in for a JWT verified by the gateway) and is written into the job's `tenant_id` at enqueue time; `AgentJobHandler` trusts only that, never a tenant claimed in the payload. Tools take the tenant and user from `ctx`; asking for another tenant's run returns 404 (checked by a test); the workers also have a per-tenant bulkhead (demo Section 5), and cost is booked per tenant. Permission-aware retrieval and index isolation are covered in [Lesson 15](../15_enterprise_rag/README.en.md).

### Problem 4: Build it yourself, use a framework, or use a managed platform?

**Scenario**: A 5-person team needs to launch 3 internal agents (HR, IT, and expense reimbursement) within a quarter. The company has a Kubernetes platform but no dedicated AI platform team. The security team requires every tool call to be auditable and every high-risk operation to go through human approval.

**Why it's hard**: Frameworks save you the boilerplate of the agent loop, tool calling, and multi-agent orchestration, but they may box you in on permissions, auditing, and data flow. Building it yourself gives you the most control, but you have to build durability, tracing, and evals on your own. Managed platforms save the most ops work, but your data and runtime live with someone else, and there's a risk of lock-in.

| Option | How | Pros | Cons | Best for |
|---|---|---|---|---|
| A. Build it yourself | Write your own agent loop, tool layer, and hooks, and plug into the company's existing storage, tracing, and auth (this course's agentkit is a teaching version of this) | Full control, seamless integration with existing infrastructure, no unnecessary abstractions | You build durable execution, concurrency, evals, and more yourself; high maintenance cost | Core business, strict security and compliance requirements, deep customization |
| B. Open-source framework | LangGraph, OpenAI Agents SDK, Claude Agent SDK, Google ADK, and others (see the table below) | Fast development, community ecosystem, ready-made best practices | The abstractions may not fit; versions change quickly; when something breaks, you end up reading framework source code | Most new projects |
| C. Managed platform | A cloud or model vendor hosts the runtime, memory, identity, observability, and more. Examples: Amazon Bedrock AgentCore (with components such as Runtime, Gateway, Memory, Identity, and Observability) and Anthropic's Managed Agents (a hosted agent runtime and sandbox) | Least ops work; you get enterprise-grade capabilities quickly | Data and execution environment sit with a third party; vendor lock-in; limited customization | No platform team, a need to launch fast, and compliance allows it |

Common frameworks compared (only well-established facts are listed; see each project's official docs for details):

| | Maker | Positioning and core abstractions | Durability and recovery | Languages |
|---|---|---|---|---|
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | The LangChain team | Low-level orchestration framework that models agents and workflows as graphs (nodes, edges, shared state) | Persistence through checkpointers; supports resuming after human intervention | Python, JavaScript |
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) | OpenAI (released March 2025) | Lightweight framework: Agent, handoff, guardrail, and session, with built-in tracing | Sessions manage conversation memory; can integrate with Temporal for durable execution | Python, TypeScript |
| [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) | Anthropic (renamed from the Claude Code SDK in September 2025) | Provides the agent loop behind Claude Code as a library: built-in tools, hooks, subagents, MCP, permission controls | Sessions can be resumed and forked | Python, TypeScript |
| [Google ADK](https://google.github.io/adk-docs/) | Google (released April 2025) | Code-first open-source framework: multi-agent composition, a tool ecosystem, evaluation, and a dev UI | Provides session / state management | Python, Java, Go, TypeScript, and more |
| [Temporal](https://docs.temporal.io) | Temporal (open source, MIT license) | Not an agent framework but a durable execution platform: workflows handle orchestration, activities perform side-effecting operations | Event history is persisted and replayed to recover after a crash | SDKs for many languages |

**How to choose**: Start by answering two questions. Is this agent core to the business, and does it have special permission and audit requirements? Does the team have the capacity to operate a platform? The usual path is to start with B, picking a framework that matches your model, language, and cloud. For core business with lots of custom needs, use A, or "your own shell around a framework core." If there's no platform team and compliance allows it, consider C. Whichever you choose, **own the cross-cutting capabilities yourself: identity injection, permissions, auditing, and evals.** A framework can save you from writing the loop; it can't save you from building eval sets, a permission model, and cost controls.

**In this lesson**: agentkit is a teaching version of option A. Its core design elements (hooks, checkpoints, `ToolContext`, tracing, evals) all have counterparts in the frameworks above. Once you really understand it, you'll pick up any of them quickly.

### Problem 5: Model gateway: build it, use open source, or use your cloud vendor's?

**Scenario**: The company uses 3 model providers, and 12 teams have each hard-coded API keys into their own code. One day a key leaks to a public repository, and nobody can say which services depend on it. At month-end, finance asks how much AI cost and which department should pay for what, and nobody can answer that either.

**Why it's hard**: The model gateway sits on the critical path of every model call. If it goes down, every agent in the company stops; if it's slow, every step is slow. And it has a long list of responsibilities: unified interface, key custody, routing, quotas, billing, fallback, caching, and auditing. Building one is significant work, and adopting an off-the-shelf one means evaluating its performance, data compliance, and extensibility.

| Option | How | Pros | Cons | Best for |
|---|---|---|---|---|
| A. Build a thin proxy | Write your own OpenAI-compatible proxy that handles auth, forwarding, and accounting, then add routing and fallback as needed | Full control; deep integration with internal auth and billing systems | You have to keep up with every provider's API differences and changes yourself, and add features piece by piece | Teams with a platform team and special requirements |
| B. Open-source gateway | For example, [LiteLLM](https://github.com/BerriAI/litellm): an open-source Python SDK plus proxy server that provides unified, OpenAI-format access to 100+ model providers, with virtual keys, spend tracking per key / user / team, budgets, rate limiting, load balancing, and fallback | Feature-rich and quick to launch; self-hostable, so data stays in-house | You're responsible for its high availability; you need to evaluate its performance and the cadence of its security updates | The starting point for most enterprises |
| C. Cloud vendor's AI gateway | For example, the AI gateway capabilities in Azure API Management (the token-based `llm-token-limit` policy, semantic caching) or Cloudflare AI Gateway (caching, rate limiting, analytics, fallback) | Managed, no ops work, integrates well with the same cloud's other services | Tied to one cloud vendor; less flexibility across clouds and providers | Teams already heavily invested in one cloud |

**How to choose**: The discipline that "every model call goes through the gateway" matters more than which product you choose. Most teams start with B or C; at very large scale or with special needs, they build on top of B or switch to A. Whichever you choose: business code knows only **logical model names** (`mini`, `pro`), with the real model decided by gateway config; the gateway itself runs with multiple replicas; and there's an emergency path that bypasses the gateway and connects directly to a provider.

**In this lesson**: agentkit's LLM abstraction (Lesson 02) means business code depends only on `chat(messages, tools)`. The exercise's `choose_model` plays the role of the gateway's routing rules — the mini deployment's API processes call it for every request and write the chosen logical model into the job — and Lesson 08's `ResilientLLM` plays the role of the gateway's retries, circuit breaking, and fallback. The cliproxyapi instance your local `.env` points to (`http://localhost:8317/v1`) is exactly this kind of OpenAI-compatible local proxy: your code only knows one address and one key, and the proxy decides which model sits behind them.

## 3. From toy to production: where agentkit fits in the reference architecture

agentkit's core is async: one process drives hundreds of sessions at once (Lesson 02). `agentkit.distributed` handles the division of labor across processes: a shared job queue, fenced checkpoints, cross-process token buckets and concurrency slots, worker processes, and fault injection (Lesson 13). It's still a teaching implementation, though: the storage is SQLite (one machine only, one writer at a time), and there's no real gateway, config center, or multi-machine deployment. Here's what each piece becomes in production:

| Capability | agentkit teaching implementation | In production |
|---|---|---|
| Agent loop | [agent.py](../../agentkit/agent.py), async, hundreds of sessions per process; `agentkit.distributed` worker processes claim jobs from a queue (this lesson's mini deployment) | A cluster of stateless workers (a Kubernetes Deployment) driven by a queue and scaled on backlog (Problems 1 and 2, Lesson 31) |
| Job queue | `SQLiteJobQueue`: leases, fences, retries, dead letters (many processes, one machine) | Postgres `SKIP LOCKED` / SQS / Redis Streams (Lessons 13 and 26) |
| Checkpoints | `InMemoryCheckpointer` / `FileCheckpointer` (single process), `SQLiteCheckpointer` (shared across processes, version CAS + fence takeover) | Postgres / Redis (Lesson 26) |
| Tool execution | Async tools are awaited directly, sync tools run in a thread pool, `@tool(isolation="process")` runs in a subprocess (which a timeout can actually kill) | Separate tool services (MCP servers) + sandboxes (containers, gVisor, Firecracker) |
| Model calls | `OpenAICompatLLM` + `ResilientLLM` | A model gateway (Problem 5) |
| Rate limiting | The exercise's `TokenBucket` / `TenantRateLimiter` (pure algorithm), `agentkit.limits.TokenBucket` (in-process), `SQLiteTokenBucket` (shared across processes on one machine) | Enforced centrally at the gateway; a distributed token bucket built on Redis + atomic Lua scripts (Lesson 26) |
| Concurrency bulkheads | `agentkit.limits.KeyedLimiter` (in-process), `SQLiteSemaphore` (cross-process, leased) | Concurrency quotas at the gateway / in Redis |
| Tracing | `Tracer` + JSONL + viewer | OTel SDK + Collector + backend (Lesson 10) |
| Prompts and config | Strings in code | Config center + versioning + progressive rollout (Lesson 16) |
| Auditing | `AuditLog` writing JSONL | Append-only, tamper-proof storage (Lesson 09) |
| Evals | `run_eval` | CI pipeline + online sampled evaluation (Lesson 11) |

### 3.1 The mini deployment: the reference architecture running on one machine

The demo builds the skeleton of the reference architecture out of real processes ([`deployment.py`](deployment.py)):

```mermaid
flowchart LR
    C["Client (httpx)<br/>round-robins across API processes"] -->|"HTTP"| A1["api-1 / api-2<br/>shared token bucket"]
    C -->|"HTTP"| A2["mem-1 / mem-2<br/>in-process token buckets (control group)"]
    A1 -->|"enqueue · 202"| DB[("jobs.db (SQLite)<br/>queue · checkpoints · token buckets · slots")]
    A2 -->|"enqueue · 202"| DB
    DB <-->|"claim · heartbeat · checkpoint · commit"| W["worker-0 / worker-1<br/>python -m agentkit.distributed.worker"]
```

- **API processes**: [`mini_api.py`](mini_api.py) is a FastAPI app, and each process is one `python -m uvicorn mini_api:app --app-dir lessons/12_production_architecture --port ...` command (a real subprocess, not a thread). `POST /runs`: API key → tenant identity → per-tenant rate limit (`429` + `Retry-After` if there aren't enough tokens) → `choose_model` routing → enqueue → `202`. `GET /runs/{id}` reads the job and the checkpoint. The same code runs as 4 processes that differ only in their rate-limit backend (the `MINI_RATE_BACKEND` environment variable).
- **Worker processes**: `WorkerPool` starts 2 `python -m agentkit.distributed.worker` processes that load [`worker_app.py`](worker_app.py): each job first passes two per-tenant bulkheads, then goes to `AgentJobHandler`, which runs the HR agent and writes a checkpoint to the same SQLite file at every step.
- **Shutdown**: SIGTERM to the API processes first (uvicorn stops accepting new requests, finishes the ones in hand, and runs FastAPI's shutdown logic), then SIGTERM to the workers (`run_worker` stops claiming and drains in-flight jobs); anything still running after the timeout gets SIGKILL.
- **How it differs from a real deployment**: every process is on one machine; SQLite has one writer at a time ([Lesson 13, section 3.12](../13_distributed_concurrency/README.en.md) measures the ceiling); there's no load balancer, so the client round-robins across the API processes itself; and identity comes from an API-key table instead of a JWT verified by the gateway. For the multi-machine version, see Lessons 26 and 31.
- Optional dependencies required: `pip install -e ".[server]"` (FastAPI, uvicorn, httpx). Without them, the demo prints the install command and exits with code 1, and the deployment tests are skipped.

### 3.2 The exercise's rate limiter, and what it looks like across processes

**Two design decisions in the exercise:**

- **The token bucket uses "lazy refill."** Instead of running a background thread that adds tokens on a timer, each access works out how many tokens to add in one go, based on how much time has passed since the last update. It's O(1), needs no timer, and ports easily to an atomic Redis script. **The clock is injectable**, so tests can use a fake clock and get fully deterministic results. There's also an easy-to-miss edge case: when the clock goes backward, don't move "last updated" backward with it. Otherwise, when the clock catches up again, that interval gets counted twice and tokens appear out of thin air.
- **When routing can't find a suitable strong model, it raises an error instead of silently falling back to a weak one.** Silent fallback lets quality problems creep in without anyone noticing. Falling back should be an explicit decision by the caller, and it should be recorded in the trace.

The exercise is a pure algorithm (plain synchronous functions with an injected clock), and that's a legitimate way to unit-test it: whether the algorithm is right has nothing to do with how many processes it runs in. But **where the state lives** decides whether it holds up in a deployment. The same "refill + take" algorithm comes in several versions:

| Version | Where the bucket state lives | Processes sharing one bucket | Used in this lesson |
|---|---|---|---|
| The exercise's `TokenBucket` / `TenantRateLimiter` | Attributes of a Python object / a dict | One process | `mem-1`, `mem-2` in demo Section 4 (the reference answer until you finish yours) |
| `agentkit.limits.TokenBucket` | An in-process dict (by key); `acquire` yields to the event loop while it waits | One process | Rate-limiting within one process (e.g. `await bucket.acquire(key)` before calling the model); not used in this lesson's demo |
| `agentkit.distributed.SQLiteTokenBucket` | One row in a SQLite table; "refill + take" happens inside a single `BEGIN IMMEDIATE` write transaction | Every process on one machine | `api-1`, `api-2` in demo Section 4 |
| Redis + a Lua script | One key in Redis; the script runs atomically on the server | Every machine | [Lesson 26](../26_state_and_queues/README.en.md) |

Measured in demo Section 4: on the free plan (capacity 5, 2 tokens per second), the configuration allows about 9 requests in 2 seconds. Two processes with their own in-memory buckets let through 16 (8 each — each process thinks it's the only one), while a shared `SQLiteTokenBucket` let through 8. Double the processes and the in-memory buckets' real quota doubles; when Kubernetes autoscales, the quota quietly grows with the replica count. The test `test_in_memory_buckets_multiply_the_limit_but_a_shared_bucket_holds_it` pins this down: the in-memory buckets let through at least 2 × 5, and the shared bucket never exceeds "capacity + rate × elapsed time."

The exercise's `TenantRateLimiter` has one more problem: its buckets live in a dict that only ever grows. Tenants that have been inactive for a long time need to be evicted (LRU / TTL), or memory grows without bound; `agentkit.limits.KeyedLimiter` uses reference counting to reclaim entries nobody is using.

**A per-tenant bulkhead** (demo Section 5) is a different kind of limit: not "how many per second," but "how many running at once." Before handing a job to the agent, a worker takes two slots: an in-process `KeyedLimiter` (at most 2 per tenant within one worker) and a cross-process `SQLiteSemaphore` (at most 3 per tenant across all workers). If it can't get them, it raises `RetryLater`: the job goes back to the queue without using up an attempt, and the worker moves on to other jobs. Measured on hooli's 24 jobs: at most 3 running at once across all workers and at most 2 within any one worker, 47 deferrals, and not a single failure. With only the in-process bulkhead, the limit is 2 × the number of workers and grows as you scale out; the cross-process slots don't change with the replica count.

> Note: the bulkhead lives in the worker's handler rather than using `Agent(limiter=..., limiter_timeout=...)`. While writing this lesson, measuring the latter turned up a framework bug at the time: the agent tried to take the slot **before** writing the user's input into its state; when it couldn't, the run ended as `rate_limited` and saved a checkpoint without the user's question, so when `AgentJobHandler` deferred the job and later called `resume`, the model saw a conversation with no user question in it. **This bug has been fixed in the framework**: a new run that can't get a slot now saves nothing and skips `on_run_end`, and the retry starts from scratch (regression test `tests/test_runtime.py::test_run_rejected_by_bulkhead_leaves_no_half_checkpoint`). The handler-based bulkhead is still correct and also counts across worker processes, so it stays.

> 🏭 **In production**: for a cross-instance token bucket kept in Redis and made atomic with a Lua script, plus checkpoints and a job queue on Postgres, see [Lesson 26](../26_state_and_queues/README.en.md). For a model gateway built on LiteLLM Router, permissions written as Cedar policy files, and tiered classifier guardrails, see [Lesson 29](../29_gateway_and_guardrails/README.en.md). For the reference architecture actually assembled into an API + multi-worker service, with load tests, failure injection, and scaling, see [Lesson 31](../31_deployment_and_scaling/README.en.md).

## 4. Hands-on: run the demo

```bash
pip install -e ".[server]"                                     # the mini deployment needs FastAPI, uvicorn, httpx
python lessons/12_production_architecture/demo.py --offline   # scripted model, about 15 seconds
python lessons/12_production_architecture/demo.py             # workers and the sync endpoint call a real model (about 60 model calls)
```

Section 0 is pure arithmetic (the routing bill), identical in both modes; Sections 1–5 start a real mini deployment: 4 API processes + 2 worker processes. Here's an excerpt of actual offline output. (Demo output translated from Chinese.)

```
  Total: $36.73/day with routing vs. $117.62/day using the strong model for everything, a 69% saving
...
  ❌ POST /runs/sync → client timed out after 2.0 s (httpx.ReadTimeout); the user only sees a failure
  ✅ POST /runs → 202, returned in 8 ms (api-1): run_id=job-1, routed to logical model pro
...
  [+ 0.3s] api-2  job leased    run running   step 0  checkpoint last written by worker-0  fence=1  claim #1
  [+ 1.2s] api-1  job leased    run running   step 1  checkpoint last written by worker-0  fence=1  claim #1
  [+ 1.2s] 💥 kill -9 worker-0 (pid 42729, exit code -9): it was halfway through, and everything in its memory is gone
  [+ 1.2s]    Kubernetes would start a new pod to replace it: worker-2 (pid 42734)
  [+ 2.8s] api-2  job queued    run running   step 1  checkpoint last written by worker-0  fence=1  claim #1
  [+ 3.4s] api-2  job leased    run running   step 1  checkpoint last written by worker-2  fence=2  claim #2
  [+ 4.6s] api-2  job leased    run running   step 2  checkpoint last written by worker-2  fence=2  claim #2
  [+ 5.9s] api-2  job succeeded run completed step 3  checkpoint last written by worker-2  fence=2  claim #2
  Looking back at the sync request: the server's run sync-demo-1 finished 2.5 s after the client gave up (status completed, 3 steps)
...
  in-process buckets (mem-1 + mem-2)   sent  39, allowed 16 (mem-1 allowed 8, mem-2 allowed 8), the rest got 429 (Retry-After: 1)
  shared bucket (api-1 + api-2)        sent  39, allowed  8 (api-1 allowed 4, api-2 allowed 4), the rest got 429 (Retry-After: 1)
  Configured limit: one bucket 5 + 2/s × 2.0 s ≈ 9.
...
  acme's 3 questions all finished in 1.2 s (each question is 2 model calls, about 0.5 s), status succeeded, succeeded, succeeded
  hooli's 24 jobs took another 1.5 s to finish; deferred by the full bulkhead (RetryLater) 47 times along the way
  hooli's peak concurrency: 3 across all workers (cross-process limit 3), 2 within each worker process (in-process limit 2)
...
  Tenant    Allowed  Rate-limited  Done  Tokens   Cost (sample prices, by logical model)
  hooli     24       54            24    19848    $0.003269
  acme      4        0             4     7644     $0.020719
...
  Exit codes: api-1=-15, api-2=-15, mem-1=-15, mem-2=-15, worker-0=-9, worker-1=0, worker-2=0
```

**What to look for:**

1. **Section 0: the vast majority of traffic is simple requests**, and a cheap model handles them fine. Requests that exceed every model's context window are rejected outright instead of being crammed in anyway.
2. **Section 2: don't tie a long task to one HTTP connection.** The sync endpoint is cut off by the client's 2-second timeout, yet the server runs it to completion anyway (money spent, and the user will probably click again); the async endpoint returns `202` in 8 ms and the task runs on a worker.
3. **Section 3: workers are stateless because all the state lives in shared storage.** After the worker at step 1 is kill -9'd, the job is reclaimed once its lease (1.5 s) expires, backs off briefly, and is claimed by the new worker (fence 1 → 2), which continues after step 1 — step 1 isn't redone. Polls alternate between api-1 and api-2, and the answers agree.
4. **Section 4: in-process rate limiters multiply the quota across replicas** (16 vs. a limit of 9); the shared bucket holds it (8).
5. **Section 5: a bulkhead limits "how many at once."** The noisy hooli gets at most 3 running at once across processes and at most 2 per worker; blocked jobs are deferred, not failed; acme's questions still finish within 1.2 s.
6. **Billing**: each run's token usage is in its checkpoint and is priced by the logical model the API chose at routing time, so every cent can be attributed to a tenant.
7. **Exit codes**: uvicorn shuts down gracefully on SIGTERM (its log shows `Application shutdown complete`) and then, by convention, ends itself with the signal it received, hence -15; workers exit with 0 after draining, and the kill -9'd one with -9.

## 5. Exercise

Open [exercise.py](exercise.py) and implement the following. They're all **plain synchronous code** (pure algorithms, no `async` / `await` needed) with an injectable clock, so the tests use a fake clock and get fully deterministic results:

| What to write | Key points |
|---|---|
| `TokenBucket._refill / try_acquire / retry_after` | Lazy refill, capped at capacity; don't deduct tokens when there aren't enough; don't move time backward when the clock goes backward; `retry_after` returns `math.inf` when the wait would be forever |
| `TenantRateLimiter._bucket` | Create each tenant's bucket on demand and cache it; unregistered tenants get the default plan; all buckets share the same clock |
| `choose_model(task, models)` | Capabilities → capacity (input + output) → quality → cost; on a cost tie, keep list order; when no candidates remain, raise `NoModelAvailable` and say which rule eliminated them |

```bash
make lesson N=12
# equivalent to .venv/bin/python -m pytest lessons/12_production_architecture
```

When you're done, run the demo again: the first line will report that the implementation comes from exercise.py (your implementation). The routing bill in Section 0, `choose_model` in the API processes, and the `TenantRateLimiter` in `mem-1` / `mem-2` all switch to your code — your rate limiter really does get put inside two API processes, and you can watch it let through 2×.

The 3 cases at the end of the test file aren't exercises: they start real API processes and worker processes to verify that "in-memory buckets across two processes let through at least 2×, while a shared bucket holds the limit," "after kill -9, another worker picks up from the checkpoint," and "the per-tenant bulkhead holds across worker processes." They use the reference answer for routing and rate limiting, so they pass whether or not you've done the exercises; without `.[server]` installed they're skipped.

## 6. Going deeper: launch checklist

Each item is tagged with the relevant lesson(s).

**Reliability**
- [ ] Model calls have timeouts, retries, and fallback (08)
- [ ] Steps, tokens, and spend are all capped (08)
- [ ] Every step writes a checkpoint, and write tools are idempotent (08, 13)
- [ ] Per-tenant rate limits and quotas (12, 13)

**Security**
- [ ] Identity is injected by the gateway; the model never gets identity parameters (03, 12)
- [ ] Least privilege, with human approval for high-risk operations (09)
- [ ] Untrusted code runs in a sandbox (09)
- [ ] Three layers of guardrails: input, output, and tool output (09)
- [ ] Secrets are held in the gateway or a key management service (12)

**Observability**
- [ ] Traces cover model and tool calls (10)
- [ ] Metrics dashboards and alerts (10)
- [ ] Users can report a run_id, and you can go from a complaint to the trace (10)
- [ ] A PII handling policy is in place (10)

**Evals**
- [ ] Eval set + CI gate, with a safety-case veto (11)
- [ ] Online sampled evaluation (11)

**Release and operations**
- [ ] Prompts and models are versioned (16)
- [ ] Progressive rollout and one-click rollback (16)
- [ ] Kill switch: shut off a single tool or the whole agent immediately when something goes wrong (16)

**Cost**
- [ ] Attribution by tenant and feature (12, 14)
- [ ] Budget alerts (10, 14)
- [ ] Model routing and caching (12, 14)

**Compliance**
- [ ] Complete, tamper-proof audit logs (09)
- [ ] Clearly defined data retention periods (10)
- [ ] The model providers' data processing terms, and any cross-border data transfer issues, have been reviewed

## 7. Common pitfalls and anti-patterns

1. **Treating an agent like an ordinary synchronous API**: long tasks get cut off by the gateway timeout, users keep retrying, and submissions get duplicated.
2. **Keeping state in worker memory**: every deploy loses tasks.
3. **Letting the model or the client declare identity**: `user_id` is a tool argument the model fills in, or client request headers are trusted blindly.
4. **Having only one global rate limit**: a single tenant can get every tenant 429'd.
5. **Every team integrating with model providers on its own**: keys scattered everywhere, costs impossible to account for, and no way to fall back uniformly during an incident.
6. **Shared caches without tenant_id**: the semantic cache returns Company A's answer to Company B.
7. **Routing rules that silently downgrade**: complex tasks get routed to a weak model, and nobody notices the quality problems.
8. **Picking a framework before working out the requirements**: the framework's abstractions don't match your permission and audit model, and you end up coding around the framework.
9. **Hard-coding prompts in code**: changing a single word requires a deploy, and there's no quick way to roll back when something goes wrong.
10. **Every replica with its own in-memory rate limiter**: what actually gets through is "the configured limit × the number of replicas" (in this lesson, 2 processes let through about 2×), and the quota quietly grows whenever you autoscale.

## 8. Interview & design review questions

<details>
<summary>Q1: Draw the architecture of an enterprise agent platform and explain which components a request passes through.</summary>

- Gateway (auth, tenant resolution, rate limiting) → session service → queue → stateless workers (agent loop) → model gateway / tool services (MCP) / sandbox.
- State store (checkpoints, sessions, approvals), plus a vector store and memory (isolated by tenant and permission).
- Governance: config center, observability, eval pipeline, audit log.
- Key points: identity is verified once at the gateway and injected into the context; every step writes a checkpoint; workers only know logical model names.
</details>

<details>
<summary>Q2: A task may run for several minutes and may need to wait for human approval. How do you design the interaction?</summary>

- Async task: return 202 and a task_id on submission; the task goes into a queue and its state is persisted.
- Push progress over SSE; after a disconnect, the client resubscribes with the task_id, or polls instead.
- While waiting for approval, pause and write a checkpoint; once approved, any worker can resume the run.
- Deliver the result via in-app message or webhook; write tools are idempotent, so duplicate submissions don't execute twice.
</details>

<details>
<summary>Q3: Why should workers be stateless? What new problems does that introduce?</summary>

- Benefits: horizontal scaling, rolling deploys, and crash recovery all become simple.
- New problems: an extra read/write per step; multiple workers handling the same session concurrently (which needs locks, version numbers, or per-session partitioned serialization); a tool that ran before its checkpoint was written (which needs idempotency).
- For long flows where failure is costly, consider a durable execution engine such as Temporal.
</details>

<details>
<summary>Q4: You have 300 tenants, 2 of which are banks. How do you design isolation?</summary>

- Hybrid model: the banks get a dedicated data layer (separate databases and vector stores, possibly even separate keys), while all other tenants share a deployment with logical isolation.
- In the shared part, isolation is guaranteed by the framework by default: identity injected by the gateway, tenant_id enforced by the storage layer, tenant_id included in cache keys.
- Each tenant gets its own rate-limit bucket and quota, and cost is attributed per tenant.
- Easy-to-miss spots: memory, semantic caches, retrieval indexes, tool credentials, trace data.
</details>

<details>
<summary>Q5: A team is launching its first agent. Would you recommend building it from scratch or using a framework?</summary>

- Start from the constraints: whether it's core business, the permission and audit requirements, the team's ops capacity, and data compliance.
- Usually start with a mature open-source framework that matches your model, language, and cloud.
- No matter what, own the cross-cutting capabilities yourself: identity injection, permissions, auditing, and evals.
- For core business with lots of custom needs, consider building it yourself or "your own shell around a framework core"; with no platform team and compliance permitting, consider a managed platform.
</details>

<details>
<summary>Q6: Why do you need a model gateway? What capabilities should it have?</summary>

- A unified interface decouples business code from providers; centralized key custody means keys can be rotated in one place after a leak.
- Routing (picking a model per task), quotas and rate limiting, billing per tenant / team.
- Retries, circuit breaking, fallback, caching, audit logs.
- Discipline: every model call goes through the gateway; the gateway itself runs with multiple replicas and keeps an emergency direct-connect path.
</details>

## 9. Self-check

- [ ] I can draw the reference architecture and say what problem each component solves
- [ ] I can choose between synchronous, streaming, and async based on task duration and whether approval is needed
- [ ] I can explain why workers should be stateless, and which new problems that creates
- [ ] I can explain the trade-offs between the silo, pool, and bridge isolation models, and the isolation points agent systems tend to miss
- [ ] I can explain how to decide between building it yourself, using a framework, and using a managed platform
- [ ] I can explain what a model gateway is responsible for, and why business code should only know logical model names
- [ ] I can explain what each of the token bucket's two parameters controls, and why each tenant needs its own bucket
- [ ] I can explain why in-process rate limiters let through N× across replicas, and how a shared token bucket and a cross-process bulkhead hold the limit
- [ ] I've finished the exercise: `make lesson N=12` passes

## 10. Design exercise on paper

**Requirements**: Design an "HR policy Q&A + leave requests" agent for a 5,000-person company.

- Employees are spread across 3 cities, and some policies (such as certain leave rules) differ from city to city.
- There are about 300 HR policy documents, updated once a quarter.
- Leave requires approval from the employee's direct manager; leave longer than 5 days also requires HR approval.
- Peak time is 9-10 a.m. on Mondays, with about 800 conversations averaging 4 turns each.
- Employee data (salaries, reasons for sick leave) is highly sensitive.
- Requirements: policy Q&A must have p95 latency < 5 seconds, and answers must cite the original policy text.

**Answer the following design questions** (write your own answers first, then expand the reference points):

1. Should policy Q&A and leave requests each be synchronous, streaming, or async?
2. Draw your architecture: which components from the reference architecture do you use?
3. Identity and permissions: who can see whose leave records? At which step does the model get the user's identity?
4. Leave approval: where does state live while waiting? What happens if a worker restarts? What if the manager approves a day later?
5. Capacity estimate: how many model calls per second at peak? Roughly how many tokens and how much money per month? How do you set the rate limits?
6. Model routing: which model tier do you use for Q&A, for leave requests, and for complex policy interpretation?
7. Knowledge base: after a policy update, how do you make sure the old version is no longer cited? How do you handle differences between cities?
8. Evals and observability: how do you build the eval set before launch? Which metrics do you watch after launch?
9. Privacy: how do you handle sensitive information such as sick-leave reasons in traces, in logs, and with the model provider?
10. Release: how do you roll out prompt changes progressively? How do you mitigate when something goes wrong?

<details>
<summary>Reference points (write your own answers before expanding)</summary>

1. **Interaction model**: policy Q&A uses streaming (SSE); the p95 < 5 s requirement is met mainly by getting the first event out as fast as possible and cutting the number of steps. Leave requests use async tasks (the C + B combination): after submission, the request enters an "awaiting approval" state, and the approval result is delivered via IM or in-app message.
2. **Architecture**: gateway (SSO / JWT auth, rate limiting) → session service → workers (agent loop) → model gateway. Tool services include "policy search" (vector store + filtering by city), "check leave balance," and "submit leave request" (a write operation that needs approval). The state store holds checkpoints and approval state. The config center, observability, evals, and auditing are all in place.
3. **Identity and permissions**: identity comes from SSO and is injected into `ToolContext` by the gateway. "Check balance" can only look up the requester's own balance; managers can only see requests from their direct reports; HR can see everything, but every access is audited. The model only sees data that tools return and that the current user is allowed to see.
4. **Approval**: after `PauseRun`, the checkpoint is written to Postgres. When the approval system calls back, any worker resumes the run via `approve(run_id, by=approver)`. Requests longer than 5 days need two levels of approval, which can be modeled as two pauses. Set up reminders for approvals that time out, plus automatic expiry. `submit_leave` uses run_id + call id as its idempotency key.
5. **Capacity estimate** (assuming an average of 2.5 model calls per turn, each with 3,000 input tokens and 300 output tokens): the peak hour has 800 × 4 = 3,200 turns, or about 0.9 turns and 2.2 model calls per second. Allowing for minute-level bursts (3-5×), design for about 10 model calls per second. By Little's Law (concurrency = arrival rate × average duration), with each call taking about 4 seconds, you need to support about 40 concurrent model calls. Assuming 3,000 conversations a day and 22 working days a month, that's about 2 billion input tokens and 200 million output tokens per month. At the example prices in this lesson's demo, that comes to about $420 a month using `mini` for everything versus about $9,000 using `pro`, so the value of model routing is obvious. Rate limits: a handful of requests per employee per minute (to stop script abuse), one bucket per department, and a global bucket on the outside aligned with the model provider's quota.
6. **Routing**: simple policy Q&A and balance lookups use a cheap model; anything that cross-references multiple policies or compares cities uses a strong model; parameter extraction for leave submissions uses a mid-tier model with strict schema validation. Every model that requests can be routed to must run the eval set.
7. **Knowledge base**: documents carry a version number and an effective date. On an update, replace the whole document and rebuild its index, mark the old version as inactive, and retrieve only active versions. Documents carry a city tag, and retrieval filters by the employee's city (with identity taken from ctx). Answers must include citations, and each citation must be verified to actually come from the retrieval results (Lesson 15).
8. **Evals and observability**: before launch, HR writes 50 core cases covering the differences between the three cities, edge cases (insufficient leave balance, leave spanning the new year), and adversarial cases (impersonating a manager to self-approve, asking for someone else's sick-leave reason); safety cases have veto power. Metrics to watch after launch: success rate, p95 latency, citation-verification pass rate, human-handoff rate, cost per conversation, and average approval time.
9. **Privacy**: sick-leave reasons live only in the business system, and the agent has no need to read them (least privilege). Traces don't record content by default, and tool arguments are redacted. Confirm the model provider's data processing terms (whether data is used for training, how long it's retained, and in which region it's stored); for sensitive scenarios, consider a privately deployed model.
10. **Release**: prompts live in the config center under version control. A change first goes through the eval gate, then rolls out progressively by employee ID hash, 5% → 25% → 100%, while you watch the metrics. Prepare a kill switch (one click to disable the "submit leave request" tool and fall back to the manual process) and one-click rollback to the previous prompt version (Lesson 16).
</details>

## Further reading

- [Anthropic · Building effective agents](https://www.anthropic.com/engineering/building-effective-agents): when to use workflows and when to use agents, and "don't make it complex when simple will do"
- [Model Context Protocol website](https://modelcontextprotocol.io), [MCP joins the Agentic AI Foundation](https://blog.modelcontextprotocol.io/posts/2025-12-09-mcp-joins-agentic-ai-foundation/)
- [A2A Protocol website](https://a2a-protocol.org), [Google Cloud donates A2A to the Linux Foundation](https://developers.googleblog.com/en/google-cloud-donates-a2a-to-linux-foundation/)
- [AWS whitepaper · SaaS Tenant Isolation Strategies](https://docs.aws.amazon.com/whitepapers/latest/saas-tenant-isolation-strategies/saas-tenant-isolation-strategies.html): the silo / pool / bridge isolation models
- [Stripe · Scaling your API with rate limiters](https://stripe.com/blog/rate-limiters): token-bucket rate limiting in production
- [LiteLLM](https://github.com/BerriAI/litellm): an open-source LLM gateway
- [Temporal docs](https://docs.temporal.io): durable execution
