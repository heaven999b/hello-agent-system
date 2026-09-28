<div align="center">

# 👋 Hello Agent System

### Design production-grade AI agent systems, from first principles

**From "I can call an LLM API" to "I can design production-grade agent systems": 32 lessons · bilingual (English / 中文) · every lesson has a tested exercise.**
No agent framework. You build every layer of an enterprise agent yourself: tools, context, architectures, orchestration, reliability, security, observability, evals, concurrency, cost, and release engineering — then retrieval, memory, MCP, data, eval methodology, optimization, coding agents, and proactive agents **It is async from Lesson 02 on** (one process serving hundreds to thousands of sessions at once) **and really multi-process from Lesson 12 on** (crash takeover, zombie workers, graceful shutdown — all verified with real processes and real signals); finally the same interfaces are backed by mature components (Postgres, Redis, Temporal, OpenTelemetry, LiteLLM, Cedar) to take it to production.

[![CI](https://github.com/heaven999b/hello-agent-system/actions/workflows/ci.yml/badge.svg)](https://github.com/heaven999b/hello-agent-system/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-offline%20%26%20deterministic-brightgreen)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-orange)](CONTRIBUTING.en.md)
![Built with Claude Opus 5.5](https://img.shields.io/badge/built%20with-Claude%20Opus%205.5-D97757)

This project's architecture was co-designed with **Claude Opus 5.5**

[中文](README.md) · **English**

[Quick start](#-quick-start) · [Curriculum](#-curriculum) · [Capstone](capstone/README.en.md) · [Failure-mode catalog](docs/failure-modes.en.md) · [Interview questions](docs/interview-questions.en.md)

</div>

---

## 🤔 Why this project?

A working agent loop is 20 lines of code. Put it in front of thousands of employees and the real questions show up:

> The model API returns 429, or goes down entirely. An agent loops all night and burns $2,000. Someone hides "ignore previous instructions" in a document and the agent resets an admin password. Company A's data shows up in Company B's answer. You change one line of the prompt and can't tell what else you broke. The process restarts halfway through a run and the agent files the same ticket twice.

**Those questions decide whether an agent can ship. Most tutorials stop at the 20 lines. This one starts there.**

| | Typical agent tutorial | This project |
|---|---|---|
| Goal | Get an agent running | Keep an agent running **reliably, safely, and under control in production** |
| Approach | Call a framework API (black box) | **Build every layer from scratch** (~3,800 lines of core code plus ~2,000 lines of multi-process code, all readable) |
| Coverage | Loop + tools | LLM essentials, the loop, tools, context, agent architectures, orchestration, an **engineering-perspectives map**; retries / circuit breakers / fallbacks, checkpoints, prompt-injection defense, RBAC, human approval, audit, tracing, evals; high concurrency and distributed execution, cost optimization, permission-aware RAG, progressive rollout and incident response; retrieval quality, memory systems, MCP and sandboxes, mainstream frameworks, agent data, eval methodology, prompt optimization and test-time compute, coding agents, proactive agents; **asyncio concurrency, real multi-process execution (leases, fencing, kill -9 takeover, network partitions), production adapters for Postgres/Redis/Temporal/OpenTelemetry/LiteLLM/Cedar, and a multi-worker reference service with load and failure-injection tests** |
| Teaching style | One way to do it | Enterprise problem cards: **every problem compares 2–5 solutions** and explains how to choose |
| Verification | "Looks like it works" | Every lesson has an **exercise with automated tests**: offline, deterministic, free. Claims of "concurrent" are proven with in-flight peaks; claims of "multi-process / crash takeover" with real processes and real signals |
| Models | Locked to one vendor | Any OpenAI-compatible endpoint (OpenAI, DeepSeek, Qwen, vLLM, any model gateway) |
| Language | Single language | **Bilingual**: every lecture and reference doc has an equivalent English version (`*.en.md`) |

## ⚖️ Being clear about what each layer does

The whole project has **one** async implementation, used from Lesson 02 through Lesson 31. Each layer above it only swaps where the state lives for a stronger component; the interfaces stay the same:

| Layer | What it is | What it does (all proven by tests) | Limits (honestly) |
|---|---|---|---|
| `agentkit/` core | The async agent runtime: loop, tools, context, orchestration, reliability, security, tracing, evals | Drives hundreds to thousands of sessions at once in one process; parallel read-only tools; real cancellation (stop on disconnect), timeouts, bulkheads, streaming; hard timeouts via subprocesses | State lives in memory or local files by default; injection detection and PII redaction are regex-based |
| [`agentkit/distributed/`](agentkit/distributed/) | Multi-process execution: a lease queue, fenced checkpoints, cross-process idempotency / rate limits / concurrency slots / circuit breakers, worker processes, `WorkerPool` | On real processes: kill -9 takeover without duplicate side effects, SIGSTOP zombies that can't write after waking up, SIGTERM graceful shutdown, no double claims across processes (Lessons 12–13) | Built on SQLite, so one machine only; one writer at a time |
| [`agentkit/contrib/`](agentkit/contrib/) | Adapters for mature components: Postgres, Redis, Temporal, OpenTelemetry, LiteLLM, Cedar, guardrail classifiers | The same queue / checkpoint interfaces on Postgres are shared across machines; under a real TCP network partition the old worker is fenced out (Lessons 26–29) | Tested against embedded Postgres, fakeredis, and the Temporal dev server; Redis failover, cluster sharding, and multi-region were not tested |
| [`production/`](production/) | A reference service tying it all together: API, workers, load test, failure injection, docker-compose, Kubernetes | Load-test and failure-injection numbers in Lesson 31 | A reference implementation, not a hosted product; there is no Docker on the author's machine, so the deployment configs have never actually been started |

For each module — what it does → what's missing → what to replace it with → how to migrate — see the [📋 production readiness guide](docs/production-readiness.en.md).

## 🗺 What you will build

```mermaid
flowchart TB
    U["User request + trusted identity<br/>tenant / user / roles"] --> API["API process<br/>enqueue → 202 (Lesson 12)"]
    API --> Q[("Durable job queue<br/>leases / fencing / dead letters")]
    Q --> W["Worker processes × N<br/>crash takeover · graceful shutdown (Lesson 13)"]
    W --> IG
    subgraph Hooks["🛡 Hook layer: cross-cutting concerns (Lessons 08-11)"]
        IG["InputGuard<br/>injection detection"]
        PP["PermissionPolicy<br/>RBAC + human approval"]
        TG["ToolOutputGuard<br/>untrusted-data isolation"]
        OG["OutputGuard<br/>PII redaction"]
        BG["BudgetHook<br/>tokens / cost / steps"]
        AU["AuditLog<br/>audit trail"]
    end
    IG --> LOOP
    subgraph Core["⚙️ Agent core (Lessons 01-07)"]
        LOOP["Agent loop<br/>LLM ⇄ tools"] --> CTX["Context strategy<br/>window / summary"]
        LOOP --> REG["Tool registry<br/>schema validation / timeouts / idempotency"]
        LOOP --> WF["Orchestration<br/>routing / parallel / multi-agent"]
    end
    LOOP --> RL["ResilientLLM<br/>retry → circuit breaker → fallback"]
    RL --> GW["Model gateway / any OpenAI-compatible API"]
    LOOP --> CP[("Checkpoints<br/>crash recovery / pause for approval")]
    LOOP --> TR["Tracer<br/>tracing → HTML viewer"]
    TR --> EV["Evals<br/>eval set + CI gate"]
    PP -.blocks.-> REG
    TG -.wraps.-> REG
```

Assembling an enterprise agent with `agentkit` looks like this. Every argument is a lesson:

```python
import asyncio
from agentkit import *

agent = Agent(
    llm=ResilientLLM(default_llm(), fallbacks=[default_llm("backup-model")]),  # Lesson 08: retry / breaker / fallback
    tools=[search_kb, create_ticket, reset_password],                           # Lesson 03: tool design
    system_prompt="You are the IT help desk assistant. " + UNTRUSTED_DATA_RULE, # Lesson 09: untrusted-data rule
    hooks=[
        InputGuard(),                                                           # Lesson 09: input screening
        PermissionPolicy(role_tools={"employee": {"search_kb", "create_ticket"},
                                     "it_admin": {"*"}}),                       # Lesson 09: RBAC + approval for dangerous tools
        ToolOutputGuard(), OutputGuard(),                                       # Lesson 09: isolation + redaction
        BudgetHook(max_cost_usd=0.10, max_tool_calls=20),                       # Lesson 08: budgets
        AuditLog("runs/audit.jsonl"),                                           # Lesson 09: audit log
    ],
    context_strategy=SlidingWindow(max_tokens=8000),                            # Lesson 04: context engineering
    checkpointer=FileCheckpointer("runs/"),                                     # Lesson 08: checkpoints
    idempotency_store=IdempotencyStore(),                                       # Lesson 08: idempotency
    tracer=Tracer(jsonl_exporter("runs/traces.jsonl")),                         # Lesson 10: tracing
)

async def main():
    me = {"tenant_id": "acme", "user_id": "alice", "roles": ["employee"]}
    result = await agent.run("reset my password", metadata=me)
    if result.status == "paused":      # dangerous tool → pause for human approval (hours later, another process)
        result = await agent.approve(result.run_id, approved=True, by="it_oncall")
    print(render_tree(result.trace))   # see every step the agent took
    # the same Agent instance, one process, 50 sessions at once (Lesson 02)
    results = await asyncio.gather(*(agent.run("How do I connect to the VPN?", metadata=me) for _ in range(50)))

asyncio.run(main())
```

A real run (`python -m agentkit.chat`, where the model calls two tools in parallel in one turn):

```text
agent.run  6231ms  tokens=973→106  status=completed steps=2 cost=$0.00228
├─ llm.chat  2681ms  tokens=440→52  → tool_calls: current_time, calculator
├─ tool.current_time  3ms  ok
├─ tool.calculator  1ms  ok
└─ llm.chat  3534ms  tokens=533→54  → final_answer
```

When one process isn't enough, or processes crash, let several worker processes take jobs from one durable queue (Lessons 12–13):

```python
# app.py — loaded once when each worker process starts
from agentkit.distributed import AgentJobHandler, SQLiteCheckpointer, SQLiteIdempotencyStore

async def make_handler(ctx):                         # ctx.db: this worker process's own database connection
    ckpt, idem = SQLiteCheckpointer(ctx.db), SQLiteIdempotencyStore(ctx.db)
    await ckpt.setup(); await idem.setup()
    agent = Agent(default_llm(), tools, checkpointer=ckpt, idempotency_store=idem)
    return AgentJobHandler(agent, ckpt)               # claim a job → run / resume after approval; fences guard checkpoints
```

```bash
# start as many as you want worker processes; kill -9 one and its jobs are taken over from the checkpoint once the lease expires
python -m agentkit.distributed.worker --queue sqlite:///runs/jobs.db --app app.py:make_handler --concurrency 16
```

```python
queue = SQLiteJobQueue("runs/jobs.db"); await queue.setup()
await queue.enqueue("run", {"op": "run", "input": "my printer is broken"}, tenant_id="acme", idempotency_key="req-42")
```

For several machines, change only `--queue postgresql://...` (Lesson 26). Beyond that, swap in mature components — **same interfaces** (this code was run end to end against embedded Postgres, fakeredis, and a real model):

```python
from agentkit import Agent, KeyedLimiter, ResilientLLM
from agentkit.contrib.gateway import LiteLLMRouterLLM
from agentkit.contrib.postgres import PostgresCheckpointer
from agentkit.contrib.redis_store import RedisIdempotencyStore, RedisTokenBucket, RateLimitHook
from agentkit.contrib.otel import OTelTracer, PrometheusHook, setup_tracing
from agentkit.contrib.policy import CedarPolicy

checkpointer = PostgresCheckpointer(dsn)
await checkpointer.setup()
agent = Agent(
    ResilientLLM(LiteLLMRouterLLM.from_env(), max_concurrency=20),   # gateway routing + fallback; in-process concurrency cap
    tools,
    hooks=[
        CedarPolicy("policies.cedar", tools=tools),                          # policy as code (Lesson 29)
        RateLimitHook(RedisTokenBucket(redis, rate_per_sec=5, capacity=10)),   # cross-instance per-tenant rate limit (Lesson 26)
        PrometheusHook(),                                                    # metrics (Lesson 28)
    ],
    checkpointer=checkpointer,                        # shared checkpoints with CAS and fencing (Lesson 26)
    idempotency_store=RedisIdempotencyStore(redis),   # cross-process idempotency (Lesson 26)
    tracer=OTelTracer(setup_tracing("itbuddy")),      # OpenTelemetry with GenAI semantic conventions (Lesson 28)
    limiter=KeyedLimiter(per_key=5, global_limit=200),  # at most 5 concurrent runs per tenant (Lesson 30)
    run_timeout=120,
)
result = await agent.run("How do I connect to the VPN?", metadata={"tenant_id": "acme", "user_id": "alice", "roles": ["employee"]})
```

## 🚀 Quick start

```bash
git clone https://github.com/heaven999b/hello-agent-system.git
cd hello-agent-system
make setup          # creates .venv; the only core dependencies are openai and pydantic
```

Edit `.env` and point it at any OpenAI-compatible endpoint:

```bash
LLM_BASE_URL=https://api.openai.com/v1     # or DeepSeek / Qwen / local vLLM / a model gateway
LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-4o-mini                       # must support function calling
```

```bash
make check-env                                  # checks connectivity and tool-calling support
.venv/bin/python lessons/00_overview/demo.py    # a preview of what you are about to build
```

> 💡 **No API key?** Every demo accepts `--offline` (it runs on the scripted model `ScriptedLLM`), and every exercise and test runs offline. You can finish the whole course for free.

## 📚 Curriculum

The course has four parts. **Part 1 teaches you how to build an agent. Part 2 teaches you which solution to pick when an enterprise problem shows up. Part 3 teaches you how to make an agent keep getting better. Part 4 teaches you how to run it in production on mature components.** Parts 1–2 plus the capstone take about 6 hours (the main track); Part 3 (~3.5 hours) and Part 4 (~3 hours) are the advanced tracks. If you are short on time, take the [4-hour fast track](lessons/00_overview/README.en.md) (read only the "core path" at the top of each lesson).

- **Part 1**: concept → build it from scratch → exercise. It ends with Lesson 07, the **engineering-perspectives map**: 20 engineering dimensions, each split into **universal checks** (every project needs them) and **situational triggers** ("when X, consider Y"). It is your map for Part 2.
- **Part 2**: every lesson is a set of **enterprise problem cards**. Each card gives a concrete scenario with real numbers, explains why the obvious fix breaks, compares 2–5 solutions (pros, cons, and the scale where each fits), says how to choose, and then shows code.
- **Part 3**: deeper building blocks (retrieval, memory, MCP, frameworks), the ML loop that keeps an agent improving (data → evaluation → optimization), and the application frontier: coding agents and proactive agents.
- **Part 4**: replace SQLite and in-process state with mature components, **keeping the same interfaces**. Every lesson covers why the single-machine version falls short → component options compared → how the adapter plugs in → operations and pitfalls, and proves it with real processes, real concurrency, and failure injection.

Every lesson follows the same loop: **read the notes → run the demo → write the exercise → `make lesson N=xx` until the tests pass → go through the self-check list**. Each lesson opens with one 📖 primary reading.

### Part 1 — Building blocks (~150 min)

| # | Lesson | Time | What you learn / build | Core source |
|---|---|---|---|---|
| 00 | [The big picture](lessons/00_overview/README.en.md) | 10m | When to use an agent and when not to; what enterprise-grade adds on top of a demo | — |
| 01 | [**LLM essentials for agent developers**](lessons/01_llm_essentials/README.en.md) | 20m | Tokens and pricing, non-determinism, how function calling really works, structured output, **reassembling streamed tool-call arguments**, reasoning models, prompt engineering | [llm.py](agentkit/llm.py) |
| 02 | [The agent loop](lessons/02_agent_loop/README.en.md) | 30m | **asyncio from scratch** (event loop, await, gather, cancellation, the blocking-call pitfall); write the async agent loop by hand; message protocol, stop conditions, hook middleware | [agent.py](agentkit/agent.py) |
| 03 | [Tool design](lessons/03_tools/README.en.md) | 20m | Production-grade tools: schemas, validation, identity injection, business errors; three timeout semantics (async = real cancellation / threads can't be killed / subprocess hard timeout) | [tools.py](agentkit/tools.py) |
| 04 | [Context engineering & memory](lessons/04_context_memory/README.en.md) | 15m | Safe truncation, summarization, clearing tool results, isolated long-term memory | [context.py](agentkit/context.py) · [memory.py](agentkit/memory.py) |
| 05 | [**Common agent architectures**](lessons/05_agent_architectures/README.en.md) | 20m | ReAct / Plan-and-Execute / ReWOO / Reflection / CodeAct, 5 multi-agent topologies, teardown of deep-research and coding agents | [workflows.py](agentkit/workflows.py) |
| 06 | [Orchestration: workflows & multi-agent](lessons/06_orchestration/README.en.md) | 15m | Routing, parallelization, orchestrator-workers, evaluator-optimizer, agents as tools | [workflows.py](agentkit/workflows.py) |
| 07 | [**Engineering perspectives**](lessons/07_engineering_perspectives/README.en.md) | 20m | 20 dimensions × universal checks / situational triggers (301 items), a 9-scenario priority matrix, how to run a design review | [perspectives.py](lessons/07_engineering_perspectives/perspectives.py) |

### Part 2 — Enterprise problems & solutions (~170 min)

| # | Lesson | Time | Typical problems (each with several solutions compared) | You build |
|---|---|---|---|---|
| 08 | [Reliability engineering](lessons/08_reliability/README.en.md) | 30m | Model 429s and outages, runaway loops, crashes mid-run, approvals that take hours | Loop detection, retry budgets, single-probe breaker; a real thundering-herd test against a separate gateway process, a circuit breaker shared across processes |
| 09 | [Security & governance](lessons/09_security/README.en.md) | 20m | Indirect prompt injection, over-privileged access, approval fatigue, PII leaks, code execution | Policy engine, redaction, lethal-trifecta detection |
| 10 | [Observability](lessons/10_observability/README.en.md) | 15m | Trace volume, PII in traces, alert noise | SLO metrics computed from traces |
| 11 | [Eval-driven development](lessons/11_evals/README.en.md) | 20m | No labeled data, unreliable LLM judges, expensive evals | pass^k, trajectory grading, release gates |
| 12 | [Production architecture](lessons/12_production_architecture/README.en.md) | 15m | Sync vs streaming vs async APIs, tenant isolation levels, build vs buy | Multi-tenant rate limiting, model routing; a mini deployment on one machine (several API processes + worker processes, kill -9 takeover, cross-process rate limits) |
| 13 | [**High concurrency & distributed execution**](lessons/13_distributed_concurrency/README.en.md) | 25m | Horizontal scaling, delivery semantics, concurrent session writes (distributed lock vs CAS vs partitioning), global rate limits and backpressure, sagas, retry storms | A SQLite lease queue + fencing tokens + CAS; kill -9 / SIGSTOP failure injection on real processes; `agentkit.distributed` |
| 14 | [Cost & latency](lessons/14_cost_latency/README.en.md) | 15m | Runaway bills, duplicate requests, tail latency, cost attribution | A cache shared across worker processes, model cascades, hedged requests that cancel the loser |
| 15 | [Permission-aware enterprise RAG](lessons/15_enterprise_rag/README.en.md) | 15m | ACL leaks, multi-tenant index isolation, stale knowledge, fabricated citations | ACL pre-filtered search, chunking, citation checks |
| 16 | [Release & operations](lessons/16_release_ops/README.en.md) | 15m | A prompt change causes an incident, silent model drift, how to stop the bleeding and roll back | Canary bucketing, automated rollback decisions, a kill switch honoured by several worker processes |

### Part 3 — Advanced: deeper building blocks, the ML loop, and the frontier (~210 min)

| # | Lesson | Time | What you learn / build |
|---|---|---|---|
| 17 | [Retrieval quality: vectors, hybrid search, reranking](lessons/17_retrieval_quality/README.en.md) | 20m | A teaching embedding, BM25, RRF fusion, LLM reranking, Recall@k / MRR / nDCG, compared step by step on a labeled eval set |
| 18 | [Advanced memory systems: from notebooks to MemGPT / Mem0](lessons/18_memory_systems/README.en.md) | 20m | ADD / UPDATE / DELETE memory writes, three-factor retrieval scoring, MemGPT-style tiered memory, consolidation |
| 19 | [MCP and code-execution sandboxes](lessons/19_mcp_and_sandbox/README.en.md) | 25m | **A hand-written MCP server and client** (both spec generations, interoperable with the official SDK), a process-level sandbox |
| 20 | [From agentkit to frameworks](lessons/20_frameworks_bridge/README.en.md) | 25m | One task implemented in DSPy / LangGraph / the OpenAI Agents SDK side by side; a mini StateGraph |
| 21 | [Agent data](lessons/21_agent_data/README.en.md) | 25m | Trace mining, dedup and clustering, stratified sampling, synthetic data and filtering, Cohen's kappa, leak-free splits |
| 22 | [Advanced eval methodology](lessons/22_eval_methodology/README.en.md) | 25m | Benchmark design checklists, position-debiased pairwise judging, confidence intervals and paired tests |
| 23 | [Optimization: prompts, test-time compute, and when to fine-tune](lessons/23_optimization/README.en.md) | 25m | OPRO-style instruction search, BootstrapFewShot, GEPA-style reflective mutation, best-of-N, Pareto fronts |
| 24 | [Coding agents and long-running harnesses](lessons/24_coding_agents/README.en.md) | 25m | **Build a coding agent that fixes bugs**: ACI tools, test protection, diff review, cross-session handoff |
| 25 | [Proactive agents and the frontier](lessons/25_proactive_and_frontier/README.en.md) | 20m | User models, a when-to-interrupt decider, frontier directions and open problems, a course recap |

### Part 4 — Production on mature components (~185 min)

| # | Lesson | Time | What you learn / verify yourself | Core source |
|---|---|---|---|---|
| 26 | [State, queues, and distributed coordination: Postgres and Redis](lessons/26_state_and_queues/README.en.md) | 30m | The same queue / checkpoint interfaces moved from SQLite to Postgres (`SKIP LOCKED`, CAS with fence takeover), Redis idempotency / Lua token bucket / fenced locks; kill -9 on real worker processes and a real TCP network partition | [postgres.py](agentkit/contrib/postgres.py) · [redis_store.py](agentkit/contrib/redis_store.py) |
| 27 | [Durable workflows: running agents on Temporal](lessons/27_durable_workflows/README.en.md) | 30m | Activity retries, Signal/Update approvals, continue-as-new, determinism and versioning; a new worker takes over after kill -9 | [temporal.py](agentkit/contrib/temporal.py) |
| 28 | [Production observability: OpenTelemetry, Prometheus, and LLM observability platforms](lessons/28_production_observability/README.en.md) | 25m | GenAI semantic conventions, trace propagation across real worker processes, tail sampling and redaction, burn-rate alerts, a Grafana dashboard; no cross-talk across concurrent runs | [otel.py](agentkit/contrib/otel.py) |
| 29 | [Model gateways, policy as code, and guardrail services](lessons/29_gateway_and_guardrails/README.en.md) | 30m | LiteLLM routing and fallback, Cedar policies (fail closed), cascaded guardrail classifiers (precision 0.42 → 0.92) | [gateway.py](agentkit/contrib/gateway.py) · [policy.py](agentkit/contrib/policy.py) · [guards.py](agentkit/contrib/guards.py) |
| 30 | [**A high-concurrency async runtime in production**](lessons/30_async_runtime/README.en.md) | 40m | 200 sessions: one by one 80.7 s → gather 0.43 s; 1 → 4 processes 348 → 846 jobs/s, beyond which the database write lock and model quota become the ceiling; disconnect-cancels-the-run on a real uvicorn subprocess; runtime concurrency bugs reproduced and fixed | [agent.py](agentkit/agent.py) · [limits.py](agentkit/limits.py) · [timeouts.py](agentkit/timeouts.py) |
| 31 | [Deployment and scaling: from one machine to a cluster](lessons/31_deployment_and_scaling/README.en.md) | 30m | API/worker split, queue-depth autoscaling, graceful-shutdown timelines, load tests and failure injection | [production/](production/) |

### 🎓 Capstone (30 min)

[**ITBuddy, an enterprise IT help-desk agent**](capstone/README.en.md) puts everything together into one working system and actually runs it as a service: an API process plus several worker processes sharing one durable queue (`deploy.py` starts them all); an approval can pause in one process and resume in another, and a kill -9 still creates exactly one ticket. It also has a CLI, a realistic design doc with a threat model and ADRs, 24 eval cases (10 of them security cases), a component ablation study, and a [report template](capstone/REPORT_TEMPLATE.en.md) for your own project (baselines, ablations, and error analysis required).

Check your progress:

```bash
.venv/bin/python scripts/progress.py
```

## 🎓 Studying alongside Stanford CS329Z

Stanford's Fall 2026 course [CS 329Z: Engineering AI Agents](https://cs329z.stanford.edu/) is about engineering agentic systems. The table maps each weekly topic on its public schedule to the matching lessons here, so you can study the two side by side:

| CS329Z week & topic | Matching lessons here |
|---|---|
| W1 Foundations & Landscape · Agentic Systems Spectrum | [00 The big picture](lessons/00_overview/README.en.md) |
| W2 LLMs for Builders | [01 LLM essentials](lessons/01_llm_essentials/README.en.md) (incl. constrained generation and decoding) |
| W2 Building Blocks: Retrieval-Augmented Generation | [04](lessons/04_context_memory/README.en.md) · [15 Permission-aware RAG](lessons/15_enterprise_rag/README.en.md) · [17 Retrieval quality](lessons/17_retrieval_quality/README.en.md) |
| W3 Tool Use & Function Calling | [03 Tool design](lessons/03_tools/README.en.md) · [19 MCP and sandboxes](lessons/19_mcp_and_sandbox/README.en.md) |
| W3 Frameworks & Agent Design | [02 The agent loop](lessons/02_agent_loop/README.en.md) · [20 From agentkit to frameworks](lessons/20_frameworks_bridge/README.en.md) |
| W4 Agent Design Patterns & Scaffolds | [05 Agent architectures](lessons/05_agent_architectures/README.en.md) · [06 Orchestration](lessons/06_orchestration/README.en.md) |
| W4–W5 Memory & Multi-Agent Systems | [18 Advanced memory](lessons/18_memory_systems/README.en.md) · [06 Orchestration](lessons/06_orchestration/README.en.md) (incl. why multi-agent systems fail) |
| W5 Optimization | [23 Optimization](lessons/23_optimization/README.en.md) |
| W6–W7 Data for Agentic Systems · Data Selection & Quality | [21 Agent data](lessons/21_agent_data/README.en.md) |
| W7 Evaluation Fundamentals & Benchmark Design | [11 Eval-driven development](lessons/11_evals/README.en.md) · [22 Advanced eval methodology](lessons/22_eval_methodology/README.en.md) |
| W8 LLM-as-Judge & Evaluation Infrastructure | [11](lessons/11_evals/README.en.md) · [21](lessons/21_agent_data/README.en.md) · [22](lessons/22_eval_methodology/README.en.md) |
| W8 Agent Safety & Guardrails | [09 Security & governance](lessons/09_security/README.en.md) (incl. privacy and red teaming) |
| W9 Coding Agents & Software Agents | [24 Coding agents](lessons/24_coding_agents/README.en.md) |
| W11 Proactive Agents · Open Problems | [25 Proactive agents and the frontier](lessons/25_proactive_and_frontier/README.en.md) |
| Project: baselines, ablations, error analysis, reproducibility | [Capstone](capstone/README.en.md) · [report template](capstone/REPORT_TEMPLATE.en.md) |

In the other direction, this project **leans harder into running agents in enterprise production**, so it complements CS329Z: [08 Reliability](lessons/08_reliability/README.en.md), [10 Observability](lessons/10_observability/README.en.md), [12 Production architecture](lessons/12_production_architecture/README.en.md), [13 Distributed execution](lessons/13_distributed_concurrency/README.en.md), [14 Cost](lessons/14_cost_latency/README.en.md), [16 Release ops](lessons/16_release_ops/README.en.md), all of Part 4 ([26](lessons/26_state_and_queues/README.en.md)–[31](lessons/31_deployment_and_scaling/README.en.md): Postgres/Redis, Temporal, OpenTelemetry, gateways and policy, async runtime, deployment and scaling), plus multi-tenancy, permissions, and auditing throughout.

Other Stanford agent courses: [CS 329A: Self-Improving AI Agents](https://cs329a.stanford.edu/) (research-oriented) and CS 222: AI Agents and Simulations.

> Note: this project is **not affiliated** with Stanford University or the teams behind these courses. The mapping is based on the public CS329Z course page (Fall 2026); check the official site for changes.

## 🧰 Reference docs (your handbook after the course)

| Doc | What's inside |
|---|---|
| [🩺 Failure-mode catalog](docs/failure-modes.en.md) | 79 failure modes seen in production: symptom → root cause → detection → fix |
| [✅ Design review checklist](docs/design-review-checklist.en.md) | 199 pre-launch checks, graded P0 / P1 / P2 |
| [🎤 System-design interview questions](docs/interview-questions.en.md) | 76 questions + 3 fully worked system-design answers |
| [🔁 Framework comparison](docs/framework-comparison.en.md) | agentkit concepts ↔ LangGraph / OpenAI Agents SDK / Claude Agent SDK / ADK … |
| [📄 Cheatsheet](docs/cheatsheet.en.md) | Principles, default parameters, decision trees; printable |
| [📖 Glossary](docs/glossary.en.md) | 246 terms, English ↔ Chinese, explained in plain words |
| [📋 Production readiness guide](docs/production-readiness.en.md) | What each teaching module lacks for production, what to replace it with, how to migrate, plus a P0 pre-launch list |
| [📚 Reading list](docs/reading-list.en.md) | 135 curated and verified papers, posts, and specs, with reading paths by role |

## 🧱 Repository layout

```text
hello-agent-system/
├── agentkit/            # the framework: ~3,800 lines of core code (one async implementation); one module per lesson; comments explain every "why"
│   ├── agent.py         #   the loop + hooks + checkpoints + tracing
│   ├── tools.py         #   tools: schema generation, validation, timeouts, idempotency, identity injection
│   ├── context.py       #   context-window strategies
│   ├── memory.py        #   long-term memory (tenant-isolated)
│   ├── workflows.py     #   5 orchestration patterns + multi-agent
│   ├── reliability.py   #   retry / circuit breaker / fallback
│   ├── guardrails.py    #   injection detection / data isolation / redaction
│   ├── permissions.py   #   RBAC + human approval
│   ├── tracing.py       #   tracing (OpenTelemetry GenAI style)
│   ├── viewer.py        #   self-contained HTML trace viewer
│   ├── evals.py         #   eval framework
│   ├── limits.py        #   bulkheads, token buckets; timeouts.py: cancellation-safe timeouts (Lesson 30)
│   ├── distributed/     #   multi-process: lease queue, fenced checkpoints, cross-process limits / breakers, worker processes, WorkerPool fault injection (Lessons 12–13)
│   └── contrib/         #   mature-component adapters: postgres / redis_store / temporal / otel / gateway / policy / guards (Lessons 26–29)
├── lessons/NN_topic/    # 32 lessons: README.md + README.en.md / demo.py / exercise.py / solution.py / test_exercise.py
├── capstone/            # ITBuddy (API process + worker processes + CLI + design doc + eval set)
├── production/          # production reference service: API + multiple workers + load test + failure injection + docker-compose / Kubernetes (Lesson 31)
├── docs/                # reference docs (bilingual)
├── scripts/             # progress board, link checker
└── tests/               # framework tests (all offline)
```

## 💡 Design principles

1. **No magic**: no agent framework. Messages are plain OpenAI-format dicts, so you can see every byte sent to the model.
2. **Production patterns, teaching-sized code**: checkpoints, idempotency keys, circuit breakers, RBAC, audit logs — the patterns are the ones real enterprise systems use, but each implementation is short enough to read in one go.
3. **Testability first**: `ScriptedLLM` makes agent tests as deterministic, offline, and free as ordinary unit tests.
4. **Vendor-neutral**: application code depends only on a single `await llm.chat()` interface, so you can swap models at any time.
5. **Transferable**: the [framework comparison](docs/framework-comparison.en.md) maps every concept to its name in LangGraph, the OpenAI Agents SDK, and others.
6. **Real, not simulated**: there is one implementation, async from Lesson 02 on; concurrency is proven with in-flight peaks, and multi-process behaviour with real processes and real signals (kill -9, SIGSTOP, SIGTERM, real TCP disconnects). What can't be done here (multiple hosts, real Redis failover) is written down as a limitation.

## ❓ FAQ

<details>
<summary><b>Why not just learn LangChain / LangGraph?</b></summary>

Frameworks change; the principles don't. Once you have built checkpoints, approval interrupts, and tool validation yourself, every framework becomes "oh, that's what they call it here." The reverse doesn't hold: people who only know a framework get stuck the moment production hits something the framework doesn't cover. After this course, the [framework comparison](docs/framework-comparison.en.md) will get you productive in any mainstream framework within an hour.
</details>

<details>
<summary><b>What background do I need?</b></summary>

You should be able to read Python functions and classes, and you should have called an LLM API at least once. The code uses async/await — a basic skill for server-side agents, taught from scratch in Lesson 02; beyond that it deliberately avoids advanced tricks (no metaprogramming). Lectures and docs are bilingual; code comments and demo output are currently in Chinese (contributions to internationalize them are welcome).
</details>

<details>
<summary><b>Which models are supported?</b></summary>

Any OpenAI-compatible endpoint that supports function calling. The author developed and validated the course against gpt-5.5 through a local cliproxyapi gateway.
</details>

<details>
<summary><b>Can I use agentkit in production?</b></summary>

It depends on the layer. The `agentkit` core is a real async runtime (real concurrency, real cancellation, bulkheads, streaming), but its state lives in memory or local files by default; `agentkit.distributed` runs it as several worker processes on one machine with crash takeover; for several machines, switch to `agentkit.contrib` (Postgres, Redis, Temporal, OpenTelemetry, LiteLLM, Cedar adapters), and `production/` is the reference service that ties them together. Every layer is tested with real concurrency, real processes, and failure injection, but they are still reference implementations, not a hosted product: for example, the Docker / Kubernetes configs have never actually been started on the author's machine, and Redis failover was not tested. What is done and what you still need to add is listed item by item in the [production readiness guide](docs/production-readiness.en.md).
</details>

## 🤝 Contributing

Found a mistake? Want to add a lesson or improve a translation? Please do! See [CONTRIBUTING.en.md](CONTRIBUTING.en.md), the [lesson template](docs/lesson-template.en.md), and the [translation guide](docs/translation-guide.md).

If this project helps you, a ⭐ helps others find it.

## 📜 License

[MIT](LICENSE)

[![Star History Chart](https://api.star-history.com/svg?repos=heaven999b/hello-agent-system&type=Date)](https://star-history.com/#heaven999b/hello-agent-system&Date)
