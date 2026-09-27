[中文](README.md) | [English](README.en.md)

# Lesson 05: Common agent architectures — from ReAct to deep-research systems

> 🕐 Time: 20 minutes ｜ 🎯 You'll be able to: explain, for 7 single-agent architectures and 5 multi-agent topologies, *where the model thinks, where state lives, and who holds control*; take apart five kinds of real products (deep research, coding agents, computer use, customer support, agentic RAG); pick an architecture for a new requirement and defend the choice ｜ 📦 Source: `agentkit/agent.py` (ReAct), `agentkit/workflows.py` (Reflection, supervisor–expert), `agentkit/permissions.py` (human checkpoints), and this lesson's [demo.py](demo.py)
>
> 📖 Primary reading: [Cognitive Architectures for Language Agents](https://arxiv.org/abs/2309.02427) (Sumers et al., 2024) — the CoALA paper describes any language agent along three axes (memory modules, action space, decision-making procedure), in the same spirit as the "four questions" in §1.2 of this lesson; focus on the framework in §4 and Table 2 in §5, which puts ReAct, Voyager, Generative Agents, Tree of Thoughts, and others side by side.

> 📍 Part 1: Foundations. Lessons 02–04 built the parts (the loop, tools, context and memory). This lesson covers the common ways to **assemble** them. The next one, [Lesson 06: Orchestration](../06_orchestration/README.en.md), covers workflow patterns and how to implement multi-agent orchestration. The two lessons complement each other; §1.3 explains the split.
>
> 🧭 **Core path (20 min)**: §0 → §1 → §2, reading the one-liner and diagram for each architecture → §3.1 (three questions for any topology) → §3.7 (topology comparison) → §4, pick the product you care about most → §6 (summary table and decision tree) → §7 run the demo → §8 do the exercises.
> Sections marked **📖 Optional** can be skipped on a first read.

## 0. In one sentence

**An architecture is, at heart, a decision about where the model thinks.**

Think of renovating an apartment. There are several ways to run the job:

| Architecture | Renovation analogy | Where the model thinks |
|---|---|---|
| **ReAct** | The contractor opens up a wall, looks, then decides what to do next | At every step |
| **Plan-and-Execute** | Draw up blueprints, build to them, revise the blueprints when something unexpected turns up | Once up front, again when something breaks |
| **ReWOO** | Hand the crew a complete bill of materials and work order, then stay out of the way | Only up front (plus a final write-up) |
| **Reflection** | When the job is done, bring in an inspector to find problems, then fix them | Once more after the work is done |
| **CodeAct** | Stop giving instructions one at a time; hand the crew a script | At every step, but each step does a lot |
| **Tree search** | Try several designs in parallel, score them, keep the best | Across many branches at once |
| **HITL (human checkpoints)** | The homeowner signs off at key milestones | The model proposes, a human decides |

Multi-agent systems are the same idea, one level up: **how do several "brains" talk to each other, what do they share, and who has the final say?**

The demo runs one task — "check the weather in three cities and give me travel advice" — through three architectures on a real model (gpt-5.5). The tools are fakes, and the Guangzhou weather endpoint is deliberately "down for maintenance":

| Architecture | Model calls | Tool calls | Tokens | Time | Quality check (3 cities / typhoon warning / ≤5 lines) |
|---|---|---|---|---|---|
| ReAct (`agentkit.Agent`) | 3 | 4 | 2,705 | 10.7s | ✅ |
| Plan-and-Execute | 3 | 4 | 2,724 | 14.3s | ✅ |
| Reflection (ReAct + critique + revise) | 4 | 4 | 3,716 | 11.2s | ✅ |

One result runs against intuition: **on this small task, planning first did not save anything.** §7 explains why. It illustrates the single most important point of this lesson: **what an architecture buys you depends on the shape of the task — choose with data, not by name.**

## 1. Core concepts

### 1.1 A three-layer map

```mermaid
flowchart TB
    subgraph A["A Single-agent reasoning and control (§2)"]
        direction LR
        A1["ReAct"] --- A2["Plan-and-Execute"] --- A3["ReWOO"] --- A4["Reflection"]
        A5["CodeAct"] --- A6["Tree search"] --- A7["HITL checkpoints"]
    end
    subgraph B["B Multi-agent topologies (§3)"]
        direction LR
        B1["Router + experts"] --- B2["Supervisor / hierarchy"] --- B3["Network / swarm"]
        B4["Blackboard"] --- B5["Event-driven"]
    end
    subgraph C["C Real product architectures (§4)"]
        direction LR
        C1["Deep research"] --- C2["Coding agent"] --- C3["Computer use"]
        C4["Customer support"] --- C5["Agentic RAG"]
    end
    A -->|"what runs inside each agent"| C
    B -->|"how agents are wired together"| C
    M["Memory architecture (§5)"] -.->|"cuts across all three"| C
```

Real products are almost always **combinations**. A coding agent's main loop is ReAct; it keeps a to-do list tool for lightweight planning, hands search-heavy side tasks to subagents, uses test runs as reflection, and pauses for approval before dangerous commands. By the end of this lesson you should be able to describe any agent product in a sentence like that.

### 1.2 Four questions that decode any architecture

| Question | Possible answers | Why it matters |
|---|---|---|
| **1. Who plans, and when?** | Hard-coded / the model at every step / the model once up front / replanning on failure / a human | Sets flexibility and the number of model calls |
| **2. Where do observations go?** | Back to the model / back to code / to another agent / into shared state | Decides whether the system can adapt, and whether context bloats |
| **3. Where does state live?** | Message history / a structured plan / a shared blackboard / external storage | Decides persistence, recovery, and who can see what |
| **4. Who decides to stop?** | The model says it's done / the plan is finished / review passes / budget runs out / a human | Decides the worst case when things go wrong |

Every architecture below can be read through these four questions.

### 1.3 How this lesson splits the work with Lesson 06

| | This lesson (05, architectures) | [Lesson 06, orchestration](../06_orchestration/README.en.md) |
|---|---|---|
| Core question | How an agent reasons and controls itself **internally**; which **topology** connects multiple agents and how they communicate; what real products look like | Five workflow patterns for wiring LLM calls together in code; how to implement multi-agent systems and what they cost |
| Typical content | ReAct vs Plan-and-Execute vs ReWOO; blackboard vs supervisor vs handoff | Prompt chaining, routing, parallelization, orchestrator–workers, evaluator–optimizer; `agent_as_tool` |

A few names show up in both lessons:

- **Reflection ≈ evaluator–optimizer** (06 §2.5): Lesson 06 shows how to write that workflow; this lesson treats it as a reasoning architecture and asks when it works and when it doesn't (§2.4).
- **Plan-and-Execute ≠ orchestrator–workers** (06 §2.4): orchestrator–workers subtasks are **independent, run in parallel, and never replanned**; Plan-and-Execute steps are **dependent, run in order, and can be replanned on failure**.
- **Router + experts, supervisor–expert**: Lesson 06 covers making the classification step reliable, writing `agent_as_tool`, and what multi-agent systems cost; this lesson covers them as topologies — how they communicate and who holds control (§3).

## 2. Single-agent reasoning and control architectures

Every architecture follows the same format: one-liner → diagram → how it runs → a card (when to use it, strengths, weaknesses and failure modes, cost and latency, key papers or products) → how to build it with agentkit.

### 2.1 ReAct: think while you act

**One-liner**: think → act → observe, in a loop, until the model decides it knows enough to answer.

```mermaid
flowchart LR
    Q["Task"] --> T["Think<br/>what next"]
    T --> A["Act<br/>call a tool"]
    A --> O["Observe<br/>result goes into history"]
    O --> T
    T -->|"enough information"| F["Final answer"]
```

**How it runs**: ① send the task and tool list to the model; ② the model returns tool calls, you run them and append the results to the message history; ③ call the model again with the full history; ④ stop when the model makes no more tool calls. Lesson 02 covers the origins: today's function calling is simply a native, structured form of ReAct.

| Aspect | Details |
|---|---|
| Use it when | The steps can't be known in advance and the agent must react to intermediate results: incident triage, fixing a bug in an unfamiliar codebase, open-ended Q&A |
| Strengths | Simplest and most flexible; surprises are handled naturally (in the demo, the Guangzhou endpoint was down and the model switched to the backup on the next step); natively supported by every major API |
| Weaknesses and failure modes | **Myopia**: it only looks one step ahead and drifts on long tasks. **Spinning**: calls the same tool over and over. **Context bloat**: every observation piles into the history. **Quitting early**: declares victory before the work is done |
| Cost and latency | N steps = N model calls, and each call resends the whole history, so **total input tokens grow roughly quadratically with the number of steps** (step 1 sends one slice of history, step N sends N). Steps run in sequence, so latency adds up |
| Key work | [ReAct](https://arxiv.org/abs/2210.03629) (Yao et al., ICLR 2023): absolute success-rate gains of 34 and 10 points over imitation- and RL-based methods on ALFWorld and WebShop. It is the default loop in nearly every agent framework today |

**With agentkit**: `agentkit.Agent` is itself a ReAct loop with guardrails ([agentkit/agent.py](../../agentkit/agent.py)).

```python
agent = Agent(llm, [get_weather, get_weather_by_airport], max_steps=8)  # max_steps stops it from spinning forever
result = agent.run("Check the weather in Beijing, Shanghai and Guangzhou on my travel days and advise me")
```

### 2.2 Plan-and-Execute: plan, execute, replan when something breaks

**One-liner**: have the model write a complete plan first, then execute it step by step; when a step fails (or after each step), have the model revise what's left.

```mermaid
flowchart LR
    T["Task"] --> P["Planner LLM<br/>writes the step list"]
    P --> E["Executor<br/>runs the next step<br/>a tool call or a small agent"]
    E --> C{"Did the<br/>step succeed"}
    C -->|"yes and steps remain"| E
    C -->|"failed"| R["Replanner LLM<br/>rewrites the remaining plan"]
    R -->|"under the replan limit"| E
    C -->|"all done"| S["Summarizer LLM"] --> Out["Output"]
```

**How it runs**: ① the planner returns a **structured** list of steps (not free text); ② the executor runs them in order — each step can be a single tool call or a subtask handled by a small ReAct agent; ③ when a step fails, the replanner gets the completed results, the failure reason, and the remaining plan; ④ once everything is done, a final call writes the answer.

| Aspect | Details |
|---|---|
| Use it when | Multi-step tasks whose steps you can roughly anticipate, but where execution can go wrong: data reports, research, batch operations |
| Strengths | Forces the model to think through the whole task first; **the plan can be shown to a human and approved** (a natural hook for HITL); the executor can be a smaller model or plain code; the execution phase doesn't touch the big model |
| Weaknesses and failure modes | **Plan quality is the bottleneck**; plans rest on stale assumptions (step 2's result should have changed how step 3 is done); **fail → replan → fail** loops, so replanning needs a hard limit (Exercise 1); plans can be absurdly long, so execution steps need a cap too |
| Cost and latency | With a code-only executor: 1 plan + k replans + 1 summary. If the executor is an agent, add its calls per step. Demo: 3 calls |
| Key work | [Plan-and-Solve Prompting](https://arxiv.org/abs/2305.04091) (Wang et al., ACL 2023); LangChain's 2023 post [Plan-and-Execute Agents](https://www.langchain.com/blog/plan-and-execute-agents) (inspired by BabyAGI and Plan-and-Solve). In products, Gemini Deep Research shows its research plan so the user can revise or approve it |

**With agentkit**: `run_plan_execute` in [demo.py](demo.py) uses `complete_json` to get a Pydantic-validated plan from the planner, then runs each step through `ToolRegistry.execute`. Exercise 1's `PlanExecuteAgent` is the full version, with plan validation, replan and step limits, and an execution trace. The skeleton:

```python
class PlanStep(BaseModel):
    id: str
    tool: Literal["get_weather", "get_weather_by_airport"]   # an enum: the plan cannot name a tool that doesn't exist
    args: dict[str, str]

plan = complete_json(llm, PLANNER_PROMPT.format(task=task), Plan).steps
while remaining:
    step = remaining.pop(0)
    r = registry.execute(ToolCall(id=step.id, name=step.tool, arguments=json.dumps(step.args)))
    if not r.ok and replans < MAX_REPLANS:               # only go back to the model on failure
        remaining = complete_json(llm, REPLANNER_PROMPT.format(...), Plan).steps
```

> 💡 A shift worth noticing: **the plan is turning from a standalone architecture into a tool inside ReAct.** Claude Code has to-do list tools, and LangChain v1 ships a built-in to-do list middleware (the old LangGraph Plan-and-Execute tutorial URL now redirects there). The model keeps an explicit plan while running a ReAct loop, so it gets a global view without losing the ability to adapt.

### 2.3 ReWOO (and LLMCompiler): plan once, never go back to the model mid-run

**One-liner**: write every step's arguments at planning time, using variables (`#E1`, `#E2`) to refer to earlier results; execute without calling the model at all, then write the answer in one go.

```mermaid
flowchart LR
    T["Task"] --> P["Planner LLM<br/>E1 = weather in Beijing<br/>E2 = weather in Shanghai<br/>E3 = compare E1 and E2"]
    P --> W["Worker plain code<br/>runs steps and substitutes variables<br/>independent steps can run in parallel"]
    W --> S["Solver LLM<br/>answers from all the evidence"]
    S --> Out["Output"]
```

**How it runs**: ① the Planner writes the whole plan in one call, with later steps referencing earlier results through placeholders like `#E1`; ② the Worker runs the steps in code and fills in the placeholders; ③ the Solver reads the plan plus all the evidence and writes the answer. The model is called exactly twice.

| Aspect | Details |
|---|---|
| Use it when | Tool results only supply data and **don't change what to do next**: multi-hop lookups, bulk data retrieval, fixed-format reports |
| Strengths | A fixed number of model calls (usually 2); observations never enter the planner's context, so it's very token-efficient — the paper reports **5× token efficiency** and a 4% accuracy gain on HotpotQA, and shows the reasoning ability of 175B GPT-3.5 being offloaded to a 7B LLaMA; independent steps can run in parallel |
| Weaknesses and failure modes | **It can't adapt**: the original ReWOO has no replanning, so if an intermediate result is unexpected it has to answer with bad data (with the demo's Guangzhou outage, the Solver would just get an error message); placeholder substitution and parsing are new points of failure; the planner has to get it right in one shot |
| Cost and latency | 2 model calls + N tool calls; latency ≈ plan + tools (parallelizable) + solve |
| Key work | [ReWOO](https://arxiv.org/abs/2305.18323) (Xu et al., 2023, arXiv preprint); [LLMCompiler](https://arxiv.org/abs/2312.04511) (Kim et al., ICML 2024) represents the plan as a task dependency graph and executes it in parallel, reporting up to 3.7× lower latency, 6.7× lower cost, and ~9% higher accuracy than ReAct |

**With agentkit** (a sketch, verified locally with ScriptedLLM):

```python
class Step(BaseModel):
    var: str                      # "E1"
    tool: str
    args: dict[str, str]          # values may contain "#E1", replaced with E1's result at run time

plan = complete_json(llm, f"Write a complete plan. Refer to earlier results as #E1, #E2.\nTask: {task}", Plan)
evidence: dict[str, str] = {}
for s in plan.steps:              # no model calls during execution
    args = {k: re.sub(r"#(E\d+)", lambda m: evidence[m.group(1)], v) for k, v in s.args.items()}
    call = ToolCall(id=s.var, name=s.tool, arguments=json.dumps(args, ensure_ascii=False))
    evidence[s.var] = registry.execute(call).content
answer = complete(llm, f"Task: {task}\nEvidence: {json.dumps(evidence, ensure_ascii=False)}")
```

### 2.4 Reflection / Reflexion: find the problems, then fix them

**One-liner**: once the work is done, have the same model (or another one) check it against a standard and revise based on the feedback. Reflexion goes further and writes "lessons from failure" into memory so the next attempt can use them.

```mermaid
flowchart LR
    T["Task"] --> G["Generator<br/>writes a draft or does the task"]
    G --> C{"Critic<br/>checks against external evidence"}
    C -->|"problems + specific feedback"| G
    C -->|"passes"| Out["Output"]
    C -->|"repeated feedback or round limit"| H["Stop<br/>return current version or escalate to a human"]
```

**Two well-known variants**:

- **Self-Refine** ([Madaan et al.](https://arxiv.org/abs/2303.17651), NeurIPS 2023): one model plays generator, feedback provider and refiner in turn, iterating within a single task; about 20 points of absolute improvement on average across 7 tasks.
- **Reflexion** ([Shinn et al.](https://arxiv.org/abs/2303.11366), NeurIPS 2023): across multiple attempts, verbal reflections are stored in episodic memory and read back on the next try; combined with external signals such as unit tests, it reached 91% pass@1 on HumanEval (GPT-4 was at 80% at the time).

**The counterexample you need to know**: [Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/abs/2310.01798) (Huang et al., ICLR 2024) found that on reasoning tasks, "self-correction" **without external feedback** helps little and sometimes makes results worse. The takeaway: **reflection pays off mainly when it has external evidence** — test results, validators, data returned by tools, retrieved sources. "The model has a feeling something is off" is not reliable.

| Aspect | Details |
|---|---|
| Use it when | There's an objective way to check the output: code (run the tests), structured output (schema validation), fact-based reports (check against the data) |
| Strengths | General-purpose; can be layered on top of any architecture; with external feedback, the quality gains are well documented |
| Weaknesses and failure modes | Unreliable without an external signal; **spinning**: the same feedback comes back round after round; **oscillation**: A becomes B, then B goes back to A; a wrong or overly picky critic causes endless rework; cost doubles |
| Cost and latency | Each extra round = 1 critique + 1 revision. In the real demo run the first draft passed straight away and it still cost 1 more call and ~1,000 more tokens than ReAct — think of it as an insurance premium |
| Key work | Self-Refine, Reflexion; LangChain's post [Reflection Agents](https://www.langchain.com/blog/reflection-agents) compares basic reflection, Reflexion and LATS |

**With agentkit**: `evaluator_optimizer` ([agentkit/workflows.py](../../agentkit/workflows.py), 06 §2.5) is exactly this loop. The demo's `run_reflection` has two design choices worth copying:

1. **The critic has external evidence**: it gets the raw data the tools returned and checks the draft against it, rather than nitpicking on instinct;
2. **Code checks first**: line count, whether every city is mentioned, whether a typhoon warning in the data made it into the advice — code decides these for free, deterministically, and can't be talked out of it; only what code can't check goes to the model.

`evaluator_optimizer` doesn't notice when the same feedback comes back. Exercise 2's `reflect_loop` fixes that: when a critique repeats, stop instead of burning money.

### 2.5 CodeAct: code as the action space

**One-liner**: instead of emitting one JSON tool call at a time, the model writes a snippet of code that calls tools, loops, branches and processes data; the code's output or error becomes the observation.

```mermaid
flowchart LR
    T["Task"] --> M["Model<br/>writes a Python snippet"]
    M --> X["Sandbox runs it<br/>tool functions, loops, filters"]
    X --> O["Observation<br/>only what was printed, or the error"]
    O --> M
    M -->|"done"| F["Final answer"]
```

Compare the two for "which of these 20 cities will get rain tomorrow?":

- **JSON tool calling**: 20 calls (or one parallel batch of 20), and all 20 full weather reports land in the context;
- **CodeAct**: one action is enough; the intermediate data stays in the sandbox and only the result enters the context:

```python
rainy = [c for c in cities if get_weather(c, "10-16")["rain_chance"] > 50]
print(rainy)
```

| Aspect | Details |
|---|---|
| Use it when | Tasks that combine many tool calls, loops and data processing; lots of tools (import only what you need, in code); data analysis |
| Strengths | One action can express loops and conditionals, so fewer actions; intermediate data stays out of the context — in the example from Anthropic's [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp), token usage dropped from 150,000 to 2,000 (a 98.7% saving); models are already good at writing code; error messages make excellent feedback |
| Weaknesses and failure modes | **A sandbox is mandatory**: running model-written code is the biggest attack surface you can create (file system, network, resource exhaustion); **approvals get harder**: it's harder to review what a code snippet does than a single tool call; debugging is harder; smaller models write worse code |
| Cost and latency | Fewer steps, but longer outputs per step; you need sandbox infrastructure, and cold starts add latency |
| Key work | [CodeAct](https://arxiv.org/abs/2402.01030) (Wang et al., ICML 2024): across 17 models, up to 20% higher success rate than JSON- or text-format actions; Hugging Face [smolagents](https://huggingface.co/docs/smolagents/index) `CodeAgent`; Anthropic's Code execution with MCP (which notes that Cloudflare calls the same idea "Code Mode") |

**With agentkit**: agentkit has no built-in sandbox, and you should **never** `exec()` model-written code inside your own process. The right shape is a high-risk tool backed by an isolated environment (a container, gVisor, Firecracker). See [Lesson 09](../09_security/README.en.md) for the security details:

```python
@tool(risk="dangerous", timeout_s=30)
def run_python(code: Annotated[str, Field(description="Python code to run; print whatever you want returned")]) -> str:
    """Run Python code in an isolated sandbox and return stdout/stderr. get_weather(city, date) is preloaded."""
    return sandbox.run(code)   # your isolated execution service: no network (or an allowlist), read-only FS, CPU/memory/time limits
```

### 2.6 Tree search (Tree of Thoughts / LATS) (📖 Optional, a brief look)

**One-liner**: instead of following a single path, expand several candidates at each decision point, score them, keep going down the promising branches, and prune or backtrack from the rest.

```mermaid
flowchart TB
    R["Task"] --> A1["Option A<br/>score 0.8"]
    R --> A2["Option B<br/>score 0.3 pruned"]
    R --> A3["Option C<br/>score 0.6"]
    A1 --> B1["A-1<br/>tests pass"]
    A1 --> B2["A-2<br/>score 0.4"]
    A3 --> B3["C-1<br/>score 0.5"]
    B1 --> Out["Pick A-1"]
```

- [Tree of Thoughts](https://arxiv.org/abs/2305.10601) (Yao et al., NeurIPS 2023): on the Game of 24, GPT-4 with chain-of-thought solved 4% of problems; with ToT, 74%.
- [LATS](https://arxiv.org/abs/2310.04406) (Zhou et al., ICML 2024): applies Monte Carlo tree search to agents, combining model-based scoring with self-reflection; 92.7% pass@1 on HumanEval with GPT-4.

| Aspect | Details |
|---|---|
| Use it when | There's a reliable scoring signal and the environment **can be rolled back**: puzzles, code generation (scored by tests), offline planning |
| Strengths | Escapes local mistakes in a single line of reasoning; large gains on hard problems |
| Weaknesses and failure modes | Costs several to dozens of times more than ReAct; with an unreliable scorer, you're just "choosing wrong, precisely"; **the environment must be reversible**: you can't take back an email or an order you sent "tentatively" in a real system |
| Cost and latency | ≈ branches × depth × (generate + evaluate) |
| In practice | Full tree search is rare in production systems. The common simplification is **best-of-N**: generate N candidates in parallel and let tests or a validator pick the best one — tree search with depth 1 |

```python
def best_of_n(generate, score, n=4):
    candidates = parallel([generate] * n)   # agentkit.workflows.parallel
    return max(candidates, key=score)       # ideally score is code: run the tests, check the rules
```

### 2.7 HITL: architectures with human checkpoints

**One-liner**: at points that are irreversible, high-risk, or where the model is unsure, pause, persist the state, and continue only after a human approves, edits, or rejects.

In [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents), Anthropic writes that agents can "pause for human feedback at checkpoints or when encountering blockers." HITL isn't a separate way of reasoning — it's **a layer you add on top of any architecture**.

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as Checkpoint store
    participant Q as Approval queue
    participant H as Approver
    A->>A: model decides to call refund_order
    A->>S: risk level dangerous, save state, pause
    A->>Q: submit for approval with amount, reason, context
    Note over A: the process can exit and free its resources
    H->>Q: an hour later, approves
    Q->>A: approve run_id
    A->>S: load state, resume from the checkpoint
    A->>A: run refund_order and reply
```

**Where to put the checkpoint**:

| Where | How | Real example |
|---|---|---|
| Approve the plan | Show the plan first; execute only after it's been reviewed | Gemini Deep Research shows its research plan for the user to revise or approve; Claude Code's plan mode |
| Approve an action | Pause before high-risk tool calls | agentkit's `PermissionPolicy`; OpenAI's *A practical guide to building agents* recommends human intervention when **failure thresholds are exceeded** and for **high-risk actions** (such as canceling orders, large refunds, payments) |
| Review the output | Spot-check results before they go out | Outbound emails, contract clauses |
| The model asks for help | Ask when information is missing or confidence is low | The three interaction patterns in LangChain's [ambient agents](https://www.langchain.com/blog/introducing-ambient-agents) post: Notify, Question, Review |
| The human takes over | A person drives while the agent waits | OpenAI's Operator hands control back to the user for logins, payments and CAPTCHAs |

| Aspect | Details |
|---|---|
| Use it when | Money, compliance, sending things outside the company, deletions and other irreversible actions; low-confidence judgment calls |
| Strengths | Caps the worst-case damage at "a human looked at it"; the approval log doubles as audit evidence |
| Weaknesses and failure modes | **Approval fatigue**: if everything needs sign-off, people start clicking "approve" without reading — only gate genuinely high-risk actions, and use sandboxing to cut unnecessary prompts (Anthropic's [Claude Code sandboxing post](https://www.anthropic.com/engineering/claude-code-sandboxing) reports that sandboxing reduced permission prompts by 84% in internal use); **not enough context**: the approver sees `refund(order=A1001)` with no amount, reason or background; **synchronous blocking**: waiting on `input()` means every restart loses the pending approval |
| Cost and latency | Model calls don't change, but end-to-end time now includes waiting for a person — possibly hours |
| Key work | LangGraph's [`interrupt()`](https://docs.langchain.com/oss/python/langgraph/interrupts) + `Command(resume=...)` (requires a checkpointer); Claude Code's [permission modes](https://code.claude.com/docs/en/permission-modes) |

**A newer trend: letting a model do some of the approving.** In Claude Code's `auto` mode, a second model (a classifier) reviews actions in your place (checked September 2026; see the permission modes docs). In effect, HITL becomes tiered: low-risk actions go through automatically, medium-risk ones are reviewed by a model, and only high-risk ones reach a human.

**With agentkit** (verified locally): `PermissionPolicy` raises `PauseRun` before a high-risk tool call, the checkpointer saves the state, and `approve` resumes from where it stopped. Approval is asynchronous; see [Lesson 09](../09_security/README.en.md).

```python
agent = Agent(llm, [refund_order], hooks=[PermissionPolicy(ask_risks={"dangerous"})])
r = agent.run("Refund order A1001")                   # r.status == "paused"; r.pending_approval is the call awaiting approval
r = agent.approve(r.run_id, True, by="alice", comment="Delivery record verified")   # logged in approval_log, resumes from the checkpoint
```

### 2.8 Combining them: real systems rarely use just one

These aren't mutually exclusive options; they're building blocks:

- **ReAct + a to-do tool**: the main loop adapts as it goes while keeping an explicit plan (a common shape for coding agents);
- **Plan-and-Execute with a ReAct executor**: follow the plan globally, adapt within each step;
- **Any architecture + Reflection**: whenever there's an external check (tests, a validator), add a critique round before returning;
- **Any architecture + HITL**: add a checkpoint before irreversible actions;
- **ReAct + CodeAct**: the same agent calls ordinary tools, and writes code when it needs loops or data processing.

The summary table and decision tree in §6 put them side by side.

## 3. Multi-agent topologies

First, a reminder: multi-agent systems are expensive. Anthropic reports that multi-agent systems use about 15× the tokens of a chat interaction, and they bring new problems such as fragmented context and error propagation. Those costs, and when they're worth paying, are covered in [Lesson 06 §2.7](../06_orchestration/README.en.md) and not repeated here. This section answers one question: **once you've decided to use several agents, how should you connect them?**

### 3.1 Three questions for any topology

| Question | Possible answers |
|---|---|
| **Communication**: how do agents pass information? | Function calls (request/response), message passing (handing over the whole conversation), shared state (reading and writing the same blackboard), events (pub/sub, queues) |
| **State sharing**: who can see what? | Private context (only the task written for it), shared conversation history, shared structured state, external storage |
| **Control transfer**: who's in charge? | Retained (the supervisor stays in control), handed off (after a handoff the other agent talks to the user directly), decentralized (whoever can do it, takes it) |

Frameworks don't agree on names. Here's a mapping (checked September 2026; frameworks change fast, so treat the official docs as authoritative):

| This lesson | LangChain v1 docs | OpenAI guide / Agents SDK | Microsoft Agent Framework | Google ADK |
|---|---|---|---|---|
| Router + experts | Router | — (built with triage + handoffs) | — | Coordinator and dispatcher |
| Supervisor / hierarchy | Subagents | Manager (agents as tools) | Magentic | Hierarchical task decomposition |
| Network / swarm | Handoffs (plus the langgraph-swarm library) | Decentralized (handoffs) | Handoff | — |
| Blackboard / shared state | — | — | Group Chat (a shared conversation; the closest match) | — |
| Event-driven | — (LangChain's blog calls these ambient agents) | — | — | — |

Sources: [LangChain Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent), [OpenAI guide](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf), [MAF orchestrations](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/), [ADK patterns](https://adk.dev/workflows/patterns/).

### 3.2 Router + experts

**One-liner**: classify at the entrance and hand the whole request to one expert; the experts never talk to each other.

```mermaid
flowchart LR
    U["User request"] --> R{"Router<br/>rules first, small model as fallback"}
    R -->|"billing"| E1["Billing expert<br/>billing tools"]
    R -->|"technical"| E2["Tech expert<br/>logs and knowledge base"]
    R -->|"other"| E3["General assistant<br/>or a human"]
    E1 --> Out["Reply to the user"]
    E2 --> Out
    E3 --> Out
```

- **Communication**: a one-shot, one-way dispatch. **State**: each expert's context is private and usually contains just the original request. **Control**: handed to the expert after dispatch (LangChain's Router pattern also allows dispatching to several experts and merging the results).
- **Use it when**: request categories are clear-cut and handled very differently: support triage, sending easy questions to small models and hard ones to big models.
- **Strengths**: simple and cheap; each expert has a small, focused prompt and tool set; natural permission isolation.
- **Failure modes**: misclassify, and everything downstream is wrong; **cross-category requests** ("refund this, and while you're at it change my shipping address") can only land with one expert; an expert that realizes "this isn't mine" has nowhere to go — either give it a way back to the router, or move up to the network topology in §3.4.
- **Implementation**: 06 §2.2's `route()` and Exercise 1's `hybrid_route` in Lesson 06 handle classification; swap each branch for its own `Agent`.

### 3.3 Supervisor / hierarchical

**One-liner**: a supervisor agent delegates subtasks to experts (wrapped as tools), collects the results, and decides the next step and the final answer itself; when there are too many experts, supervisors manage supervisors and you get a hierarchy.

```mermaid
flowchart TB
    U["User"] <--> S["Top supervisor"]
    S -->|"task"| L1["Research lead"]
    S -->|"task"| L2["Writing lead"]
    L1 -->|"task"| W1["Searcher A"]
    L1 -->|"task"| W2["Searcher B"]
    L2 -->|"task"| W3["Writer"]
    W1 -->|"summary"| L1
    W2 -->|"summary"| L1
    W3 -->|"draft"| L2
    L1 -->|"findings"| S
    L2 -->|"final text"| S
```

- **Communication**: function-call style, request/response (task in, result out). **State**: experts only see the task their manager wrote and return a summary, which gives you context isolation for free. **Control**: always with the supervisor, and only the supervisor talks to the user.
- **Why a hierarchy**: a supervisor with too many experts runs into the same tool-overload problem as any agent (06 §2.7). Another layer helps, but every layer is one more round of "telephone," so information loss and latency accumulate.
- **Examples**:
  - Anthropic's research system: a lead agent plus parallel subagents (§4.1);
  - [Magentic-One](https://arxiv.org/abs/2411.04468) (Microsoft, 2024): the Orchestrator keeps a **Task Ledger** (facts, guesses, plan) and a **Progress Ledger** (a self-check on progress at every step), and directs four specialists — WebSurfer, FileSurfer, Coder and ComputerTerminal. When progress stalls, it reflects and updates the plan. It's a supervisor combined with Plan-and-Execute.
- **Failure modes**: the supervisor becomes a bottleneck — everything flows through it, so its context grows; tasks are written with missing context; the supervisor treats an expert's conclusion as fact; delegation depth spirals (experts delegating to experts delegating to…).
- **Implementation**: `agent_as_tool` (06 §2.7). A hierarchy is just an expert that is itself a supervisor with its own `agent_as_tool` experts. Remember to cap delegation depth.

### 3.4 Network / swarm (handoffs)

**One-liner**: there's no fixed supervisor; any agent can **hand off** the conversation and control to a better-suited peer, and whoever takes over talks to the user directly.

```mermaid
flowchart LR
    U(("User")) <-.->|"always talks to the currently active agent"| ACT["Active agent"]
    T["Triage"] -->|"hand off"| B["Rebooking"]
    T -->|"hand off"| C["Cancellation"]
    T -->|"hand off"| F["FAQ"]
    B -->|"hand off"| C
    B -->|"hand back"| T
    C -->|"hand back"| T
    F -->|"hand back"| T
```

- **Communication**: handoffs — passing over the whole conversation. In the OpenAI Agents SDK, a [handoff](https://openai.github.io/openai-agents-python/handoffs/) is represented as a tool; when the model calls it, control moves to the other agent. **State**: usually the full conversation history is shared, so the new agent sees everything that happened. **Control**: handed off — whoever takes over does the talking. The system has to remember who's currently serving the user and send the next user message straight to them ([langgraph-swarm](https://github.com/langchain-ai/langgraph-swarm-py) does exactly this).
- **Examples**: OpenAI's [Swarm](https://github.com/openai/swarm) (2024, labeled experimental and educational, since superseded by the Agents SDK; its companion cookbook explains the ideas as routines and handoffs); OpenAI's [customer service agents demo](https://github.com/openai/openai-cs-agents-demo) (§4.4); AutoGen's Swarm team.
- **Use it when**: in a conversational service, different stages need different specialists to serve the user **directly**.
- **Failure modes**: **ping-pong** (A hands to B, B hands back to A) — cap the number of handoffs and log the chain; state lost in the handoff; every agent has to recognize "this isn't mine"; nobody has the full picture, which makes consistent summaries and audits hard.
- **Implementation** (agentkit has no built-in handoff; this sketch runs): a hook intercepts `transfer_to_xxx` tool calls and stops the current agent, and an outer loop switches to the target agent, carrying the full history.

```python
class Handoff(Hook):
    """When the model calls transfer_to_xxx, stop this agent and report who should take over."""
    def before_tool(self, state, call, tool):
        if call.name.startswith("transfer_to_"):
            raise StopRun("handoff", call.name.removeprefix("transfer_to_"))

def run_swarm(agents: dict[str, Agent], active: str, user_input: str, max_handoffs: int = 3):
    history: list = []
    for _ in range(max_handoffs + 1):
        r = agents[active].run(user_input, history=history)
        if r.stop_reason != "handoff":
            return active, r                         # the next user message also goes to `active`
        active, history = r.output, r.history        # the new agent takes over with the full history
        user_input = "(System: this conversation has been handed to you. Please continue with the user's request above.)"
    raise RuntimeError("Too many handoffs: the agents may be playing ping-pong")
```

`StopRun` makes agentkit add a "not executed" result for the unexecuted `transfer_to_xxx` call, so the message protocol stays valid when you continue with `r.history` (Lesson 04 explains why an orphaned tool message gets you a 400).

### 3.5 Blackboard / shared state

**One-liner**: agents don't talk to each other directly; they read and write a shared "blackboard" of structured state. Each agent acts when it sees something it can handle and writes its results back, and a controller decides who goes next.

The idea predates LLMs by decades: the Hearsay-II speech understanding system (Erman et al., ACM Computing Surveys, 1980) used a blackboard to coordinate multiple "knowledge sources."

```mermaid
flowchart TB
    BB[("Blackboard<br/>tasks, facts, hypotheses, to-dos, conclusions<br/>each entry has author, version, source")]
    A1["Retrieval agent"] <-->|"read and write"| BB
    A2["Analysis agent"] <-->|"read and write"| BB
    A3["Review agent"] <-->|"read and write"| BB
    CT["Controller<br/>rules or an LLM"] -.->|"reads the board, picks who goes next"| A1
    CT -.-> A2
    CT -.-> A3
```

- **Communication**: indirect, only through shared state. **State**: shared, structured, persistent. **Control**: a controller picks the next agent based on what's on the board, or agents subscribe to the entries they care about and trigger themselves.
- **Examples**:
  - [MetaGPT](https://arxiv.org/abs/2308.00352) (Hong et al., ICLR 2024): a shared message pool with publish–subscribe, where each role subscribes only to relevant messages (the architect, for example, watches for the product manager's requirements document);
  - 2025 research revisited the architecture: Han and Zhang's [blackboard-based multi-agent system](https://arxiv.org/abs/2507.01701) was competitive on reasoning and math tasks while using fewer tokens; Salemi et al. reported 13–57% relative gains in end-to-end success on [data discovery tasks](https://arxiv.org/abs/2510.01285). Both are arXiv preprints.
- **Use it when**: several specialists collaborate on one evolving "working document"; the order of contributions can't be fixed in advance; you need auditable intermediate state.
- **Strengths**: decoupling — adding a specialist doesn't require changing the others; state you can observe, persist and replay; naturally asynchronous.
- **Failure modes**: **write conflicts** (two agents update the same entry) → version numbers or optimistic locking ([Lesson 13](../13_distributed_concurrency/README.en.md)); **board bloat**; **error propagation**: one agent writes a wrong conclusion and everyone treats it as fact → every entry needs a source and a confidence level; **stalls**: entries nobody picks up.
- **Implementation sketch**: the blackboard is just two tools, and writes record the source run_id for traceability (this runs):

```python
BOARD: dict[str, dict] = {}   # in production, a database with version, author, timestamp, and optimistic locking on writes

@tool
def read_board(section: Annotated[str, Field(description="Section: facts / hypotheses / todo / conclusions")]) -> str:
    """Read every entry in one section of the blackboard."""
    return json.dumps(BOARD.get(section, {}), ensure_ascii=False)

@tool(risk="write")
def write_board(section: Annotated[str, Field(description="Section name")], key: Annotated[str, Field(description="Entry name")],
                value: Annotated[str, Field(description="Content; for facts, state the source")], ctx: ToolContext) -> str:
    """Write or overwrite one entry in a section of the blackboard."""
    BOARD.setdefault(section, {})[key] = {"value": value, "by_run": ctx.run_id}   # provenance comes from the system, not the model
    return "written"
```

### 3.6 Event-driven / ambient agents

**One-liner**: the agent isn't woken up by someone typing in a chat box; it subscribes to event streams (new emails, new tickets, alerts, timers), handles events as they arrive, and involves a human only when it needs to.

Harrison Chase of LangChain defined the term in [Introducing ambient agents](https://www.langchain.com/blog/introducing-ambient-agents) (January 2025): ambient agents listen to event streams and act on them, possibly handling many events at once; humans stay involved through three patterns — Notify, Question and Review.

```mermaid
flowchart LR
    SRC["Event sources<br/>email, tickets, alerts, timers"] --> Q[("Message queue")]
    Q --> W["Agent workers × N<br/>one run per event"]
    W --> ACT["Take action<br/>read-only or suggest by default"]
    W -->|"needs a human"| IN["Human inbox<br/>notify, question, review"]
    IN -->|"human replies"| W
    ACT -.->|"tag events the agent itself causes<br/>so it doesn't trigger itself"| SRC
```

- **Communication**: asynchronous events (pub/sub, queues). **State**: one run per event, persisted through checkpoints; state shared across events lives in external storage. **Control**: event routing decides who handles what; humans take part asynchronously through an inbox.
- **What's different from a chat agent**:
  - Nobody is watching the screen → latency matters less, **throughput** matters more;
  - Many events can arrive at once → concurrency, rate limiting, and **idempotency** (a redelivered event must not run twice); see [Lesson 08](../08_reliability/README.en.md) and [Lesson 13](../13_distributed_concurrency/README.en.md);
  - Nobody sees mistakes as they happen → observability and alerting ([Lesson 10](../10_observability/README.en.md));
  - Human involvement is asynchronous → pausing and resuming must be durable.
- **Use it when**: first-pass alert triage, sorting email and drafting replies, ticket preprocessing, scheduled checks.
- **Failure modes**: **event storms** (one outage fires 1,000 alerts → 1,000 agent runs) → dedupe, batch, rate-limit; **redelivery** → use the event ID as an idempotency key; **wrong actions with nobody watching** → writes go through Review by default; **self-triggering loops** (the agent's action creates an event that triggers the agent again) → tag events with their source and ignore your own.
- **Implementation sketch** (this runs):

```python
def handle(event: dict) -> None:                        # called by the queue consumer
    run_id = f"evt-{event['id']}"                       # the event ID doubles as run_id, so redeliveries are recognizable
    if agent.checkpointer.load(run_id) is not None:     # already handled: skip (idempotency)
        return
    result = agent.run(f"New ticket: {event['text']}", run_id=run_id,
                       metadata={"tenant_id": event["tenant"], "user_id": "system:ambient"})
    if result.status == "paused":                       # needs a human: park it in the inbox, then agent.approve(run_id, ...)
        inbox.push(run_id, result.pending_approval)
```

In production the checkpoints have to live in shared storage (replace `FileCheckpointer` with a database) so every worker can see which events have been handled. There's still a race between "check" and "run"; strict deduplication needs a database unique constraint or a distributed lock (Lesson 13).

Event-driven design only answers "when does the agent wake up?" A proactive agent also has to answer "once awake, should it interrupt anyone?" For user models, interruption decisions, and privacy boundaries, see [Lesson 25](../25_proactive_and_frontier/README.en.md).

### 3.7 Topologies compared

| Topology | Communication | State sharing | Control | Who talks to the user | Good for | Main risks |
|---|---|---|---|---|---|---|
| Router + experts | One-shot dispatch | Private per expert | Handed over after dispatch | The expert | Clear-cut categories handled independently | Misrouting, cross-category requests |
| Supervisor / hierarchy | Function calls (task → result) | Private; experts return summaries | Always the supervisor | The supervisor | Unified answers, parallelizable subtasks | Supervisor bottleneck, loss at every hand-down |
| Network / swarm | Handoffs of the conversation | Shared conversation history | Handed off | The active agent | Multi-stage conversational service | Ping-pong, state lost in handoffs |
| Blackboard | Reading and writing shared state | Shared structured state | A controller, or agents claim work | Usually nobody directly | Collaboration on one working document | Write conflicts, wrong conclusions spreading |
| Event-driven | Async events, queues | External storage + checkpoints | Event routing | Nobody online; humans join via an inbox | Background automation | Event storms, duplicate execution, unnoticed errors |

When agents from different organizations or vendors need to call each other, you need a standard protocol. [A2A](https://a2a-protocol.org/latest/) (Agent2Agent) is built for this: an Agent Card describes capabilities, a Task represents a stateful unit of work, and the remote agent is a black box to the caller. Google has donated it to the Linux Foundation. See [Lesson 12](../12_production_architecture/README.en.md) for how it fits into a production architecture.

## 4. Taking apart real products

> Legend: ✅ backed by official public material (linked); 🔍 an inference from public material. Products change quickly; everything below was checked in September 2026.

### 4.1 Deep research agents

```mermaid
flowchart TB
    U["User question"] --> SC["Clarify the request, draft a research plan<br/>the user may revise or approve it"]
    SC --> LD["Lead agent<br/>splits the research, saves the plan to external memory"]
    LD --> S1["Subagent 1<br/>search, read, compress"]
    LD --> S2["Subagent 2"]
    LD --> S3["Subagents 3 to 5"]
    S1 -->|"condensed findings"| LD
    S2 --> LD
    S3 --> LD
    LD -->|"not enough yet"| LD
    LD -->|"enough"| CT["Citation step<br/>find a source for every claim"]
    CT --> R["Report with citations"]
```

**What's public**:

- ✅ **Anthropic Research** ([How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), June 2025): an orchestrator–worker design. The lead agent makes a plan and spins up 3–5 subagents in parallel, and each subagent uses 3+ tools in parallel; the subagents act as "intelligent filters," searching iteratively and sending back only what they found. Because context beyond 200,000 tokens gets truncated, the lead agent saves its plan to Memory. After the research, a CitationAgent attaches citations to the report. The multi-agent system with Opus 4 as lead and Sonnet 4 as subagents outperformed single-agent Opus 4 by 90.2% on an internal research eval, and parallelization cut research time by up to 90% on complex queries.
- ✅ **OpenAI deep research** ([Introducing deep research](https://openai.com/index/introducing-deep-research/), February 2025): powered by a version of o3 optimized for web browsing and data analysis, trained with end-to-end reinforcement learning to plan and execute multi-step trajectories and backtrack when needed; a run takes 5–30 minutes and the output includes citations. **OpenAI has not said whether it uses a multi-agent architecture.** In July 2025 it was folded, together with Operator, into ChatGPT agent.
- ✅ **Gemini Deep Research** ([Google blog](https://blog.google/products/gemini/google-gemini-deep-research/), December 2024; [product page](https://gemini.google/overview/deep-research/)): turns the question into a multi-step research plan for the user to revise or approve; Google describes an asynchronous task manager that keeps shared state between the planner and task models so it can recover from errors without restarting the whole task.
- ✅ **Open-source: open_deep_research** ([langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research)): three phases. Scope: clarify the request and write a research brief. Research: a supervisor spins up as many parallel sub-agents as needed, each with isolated context, and their results are compressed before coming back. Write: produce the report in one pass.

**Key design decisions**:

1. **Aim it at breadth-first problems**: sub-questions that can run in parallel are exactly where multi-agent pays off (06 §2.7).
2. **Subagents are filters**: they return condensed findings, not raw web pages — context isolation in practice.
3. **Keep the plan outside the context**: once the plan is in external memory, the lead agent doesn't lose track of what it was doing after truncation or compaction. That's the working memory of §5.
4. **Citations are their own step**: a dedicated pass verifies sources — reflection grounded in evidence.
5. **Clarify before researching**: plan approval (Gemini) and scoping (open_deep_research) are HITL at the plan level. A run can take tens of minutes; going in the wrong direction is expensive.
6. **Two routes**: explicit multi-agent orchestration (Anthropic, open_deep_research) versus training plan–search–backtrack behavior into a single model (OpenAI's public description). 🔍 Expect the two to converge: the stronger the model, the thinner the external orchestration.

**Architecture in one line**: supervisor/hierarchy + Plan-and-Execute + parallelism + reflection (citation checking) + plan-level HITL.

### 4.2 Coding agents (Claude Code, Codex and similar)

```mermaid
flowchart TB
    U["Developer"] <--> L["ReAct main loop"]
    MEM["Project memory<br/>CLAUDE.md or AGENTS.md"] --> L
    L <--> CTX["Context management<br/>automatic compaction"]
    L --> TD["To-do list tool<br/>lightweight planning"]
    L --> SUB["Subagents<br/>separate context windows"]
    L --> PERM{"Permission layer<br/>modes + rules + approval"}
    PERM --> TOOLS["Tools<br/>search, read, exact edits, run commands, web"]
    TOOLS --> SB["Sandbox<br/>filesystem isolation + network isolation"]
    SB -->|"test, build and lint output"| L
```

**What's public**:

- ✅ **Claude Code** (official docs at [code.claude.com/docs](https://code.claude.com/docs/en/tools-reference)):
  - Built-in tools include Read, Write, Edit, Glob, Grep, Bash, WebFetch, WebSearch, and the Agent tool for spawning subagents (formerly called Task);
  - [Permission modes](https://code.claude.com/docs/en/permission-modes): `default` (shown as Manual; asks before edits and shell commands), `acceptEdits`, `plan`, `auto` (a classifier model reviews actions in your place), `dontAsk` (runs only pre-approved tools; meant for CI), and `bypassPermissions` (only for isolated containers or VMs);
  - When the context fills up it compacts automatically: first clearing old tool output, then summarizing the conversation;
  - [CLAUDE.md](https://code.claude.com/docs/en/memory) memory files at several levels (organization, user, project);
  - [Subagents](https://code.claude.com/docs/en/sub-agents) run in their own context windows with their own system prompt, tools and permissions, and can run in parallel;
  - [Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) (October 2025): filesystem isolation plus network isolation; in internal use it cut permission prompts by 84%.
- ✅ **OpenAI Codex**:
  - Cloud ([Codex system card](https://cdn.openai.com/pdf/8df7697b-c1b2-4222-be00-1fd3298f351d/codex_system_card.pdf), May 2025): each task runs in its own cloud container, preloaded with the user's code and development environment, with internet access disabled after setup; codex-1 cites terminal logs and files as evidence of its work so users can verify it, and the system card explicitly lists "falsely claiming to have completed a task" as a risk. Project conventions go in AGENTS.md.
  - CLI ([sandboxing docs](https://learn.chatgpt.com/docs/sandboxing)): three sandbox modes — `read-only`, `workspace-write`, `danger-full-access` — and approval policies `on-request` and `never`; Seatbelt on macOS, bubblewrap on Linux; network access restricted by default.

**Key design decisions**:

1. **The core is still ReAct**, and the craft goes into the tools: search (Glob/Grep) before reading; edits are exact string replacements rather than whole-file rewrites (fewer tokens, less collateral damage).
2. **The plan is a tool**: a to-do list lets the model keep an explicit plan inside ReAct — a lightweight Plan-and-Execute (§2.2).
3. **Subagents for context isolation**: search and investigation side tasks run in separate windows and return only their conclusions. Anthropic's research-system post also notes that most coding tasks have too many interdependencies to split across parallel agents, which is why coding agents stay mostly single-agent.
4. **Context compaction is non-negotiable**: long sessions need automatic compaction (Lesson 04).
5. **Project memory = procedural memory**: CLAUDE.md / AGENTS.md tell the agent how to build and test the project and which conventions to follow (§5).
6. **Safety = permissions × sandbox**: permission modes decide whether to ask before acting; the sandbox decides what an action can reach once it runs. The two layers are independent, and you need both.
7. **Closing the loop with verification**: run tests, builds and linters, and feed the output back. That's reflection in its most effective form (§2.4), and the key defense against "claimed it was done."

**Architecture in one line**: ReAct + a to-do tool (lightweight planning) + subagents (supervisor–expert) + test feedback (reflection) + permission modes (HITL) + a sandbox (a CodeAct-style execution environment).

To build one yourself, see [Lesson 24](../24_coding_agents/README.en.md): it implements a coding agent's ACI tools, path boundaries, and test protection from scratch, plus a harness that lets it hand a long task off across sessions.

### 4.3 Computer-use and browser agents

```mermaid
flowchart LR
    SS["Screenshot"] --> M["Multimodal model<br/>look and reason"]
    M --> ACT["Action<br/>click at x,y, type, press keys, scroll"]
    ACT --> ENV["VM or browser<br/>isolated environment"]
    ENV --> SS
    M -->|"login, payment, CAPTCHA<br/>or consequential actions"| H["Hand back to a human<br/>or ask for confirmation"]
```

**What's public**:

- ✅ **Anthropic computer use** ([announcement](https://www.anthropic.com/news/3-5-models-and-computer-use), October 2024; [tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)): the model returns actions such as screenshot, click, type, key press and scroll; **your application executes them in an isolated environment** and sends back a screenshot as the result, in a loop. In other words, the developer implements the agent loop; the reference implementation is a Docker container with a virtual display. Anthropic recommends a dedicated, minimally privileged VM or container, no sensitive data, a domain allowlist, and human confirmation for consequential actions. At launch it scored 14.9% on OSWorld (screenshot-only).
- ✅ **OpenAI Computer-Using Agent / Operator** ([CUA](https://openai.com/index/computer-using-agent/), January 2025): combines GPT-4o's vision with reasoning trained through reinforcement learning, running a perceive → reason → act loop over screenshots; it hands control back to the user for logins, payments and CAPTCHAs, and asks for confirmation before consequential actions such as placing an order or sending an email; 38.1% on OSWorld, 58.1% on WebArena, 87% on WebVoyager. It was folded into ChatGPT agent in July 2025.
- ✅ **[OSWorld](https://arxiv.org/abs/2404.07972)** (Xie et al., 2024): 369 real computer tasks; humans complete 72.36%, the best model at publication managed 12.24%.

**Key design decisions**:

1. **The architecture is ReAct**, except observations are screenshots and actions are mouse and keyboard events. Every step is a multimodal call; screenshots are token-hungry and latency is high.
2. **The reliability gap is still large**: this is far slower and more fragile than calling an API. **If there's an API, don't drive the screen** (the ACI principle from Lesson 03); computer use is for legacy systems without APIs.
3. **The environment must be isolated**: any text on screen could be a prompt injection ([Lesson 09](../09_security/README.en.md)).
4. **HITL is part of the architecture**, not an add-on: takeover mode and confirmation before consequential actions.
5. 🔍 Many browser agents also use structured page information (the DOM or accessibility tree) to depend less on pixel coordinates. This is common industry practice; most products haven't published their exact approach.

**Architecture in one line**: ReAct (with multimodal observations) + an isolated execution environment + action-level HITL.

### 4.4 Customer support agents

```mermaid
flowchart TB
    U["User"] --> G["Input guardrails<br/>relevance, jailbreak detection"]
    G --> T["Triage agent"]
    T -->|"hand off"| E1["Orders and refunds expert<br/>ticket tools, refunds need approval"]
    T -->|"hand off"| E2["Policy and FAQ expert<br/>RAG over policy text"]
    E1 -->|"not mine"| T
    E2 -->|"not mine"| T
    E1 --> TK[("Ticket and order systems")]
    E1 -->|"escalation triggered"| HU["Human agent<br/>with a conversation summary"]
    E2 -->|"escalation triggered"| HU
    T -->|"user asks for a person"| HU
```

**What's public**:

- ✅ **OpenAI's customer service agents demo** ([openai-cs-agents-demo](https://github.com/openai/openai-cs-agents-demo), June 2025, built on the Agents SDK): an airline support scenario. The original version had five agents — Triage, Seat Booking, Flight Status, Cancellation and FAQ — handing off to each other, with specialists able to hand back to triage, and two input guardrails at the entrance, Relevance and Jailbreak. The repo has since been restructured with a different split of agents.
- ✅ **Intercom Fin** ([escalation docs](https://fin.ai/help/en/articles/12399729-manage-fin-ai-agent-s-escalation-guidance-and-rules)): escalation rules triggered by data attributes, plus escalation guidance written in natural language; by default it escalates when the customer explicitly asks for a human, shows strong frustration, or the conversation gets stuck in a loop. Escalation rules only decide **when** to escalate; **who** receives the conversation is decided by the helpdesk's assignment rules.
- ✅ **τ-bench** ([Yao et al.](https://arxiv.org/abs/2406.12045), Sierra, ICLR 2025): simulates the three-way tool–agent–user interaction in retail and airline support. Models like gpt-4o succeed on fewer than 50% of tasks and are highly inconsistent: in retail, the rate of succeeding on the same task 8 times in a row (pass^8) is below 25%.
- ✅ **Klarna** ([February 2024 press release](https://www.klarna.com/international/press/klarna-ai-assistant-handles-two-thirds-of-customer-service-chats-in-its-first-month/)): in its first month the AI assistant handled 2.3 million conversations — two-thirds of customer service chats, the work of 700 full-time agents — and cut resolution time from 11 minutes to under 2. A LangChain [case study](https://www.langchain.com/blog/customers-klarna) says it was built on LangGraph and LangSmith. In 2025, Klarna's CEO said publicly that focusing too hard on cost had lowered quality and that the company would reinvest in the quality of human support ([coverage](https://www.customerexperiencedive.com/news/klarna-reinvests-human-talent-customer-service-AI-chatbot/747586/)).

**Key design decisions**:

1. **The topology is triage + experts** (handoffs and supervisor–expert are both common), with each expert given the minimum tools and permissions.
2. **Knowledge via RAG, facts via tools**: policies and FAQs are retrieved and quoted; orders and shipments are looked up live through tools. Don't expect the model to "remember" business data.
3. **Writes are idempotent, approved and thresholded**: the high-risk examples in OpenAI's guide are precisely canceling orders, large refunds and payments.
4. **Escalation is a first-class feature**, not a failure: clear triggers, and a summary handed to the human so the customer doesn't have to explain everything again. Klarna's reversal shows the goal isn't full automation — it's good service.
5. **Evaluate with pass^k, not just the average success rate**: support needs to be right every time.

**Architecture in one line**: input guardrails + triage (router or network topology) + RAG + write tools behind approval + escalation to humans.

### 4.5 Agentic RAG

Classic RAG is a fixed pipeline: retrieve once, then generate. Agentic RAG hands several decisions to an agent: **whether to retrieve at all, what to retrieve (rewriting or splitting the query, choosing the source), whether the results are good enough (and if not, retrieving again or switching tools), and when it's ready to answer.**

```mermaid
flowchart TB
    Q["Question"] --> D{"Need to retrieve"}
    D -->|"no"| ANS["Answer directly"]
    D -->|"yes"| PL["Rewrite or split the query<br/>choose a source"]
    PL --> R["Retrieve<br/>permission filtering first"]
    R --> J{"Relevant and sufficient"}
    J -->|"not enough and under the limit"| PL
    J -->|"enough"| G["Generate answer + citations"]
    G --> V{"Is every claim sourced"}
    V -->|"no"| PL
    V -->|"yes"| OUT["Output"]
```

**The research lineage** (✅ all verified papers):

| Method | What it does | Architecture it maps to |
|---|---|---|
| [IRCoT](https://arxiv.org/abs/2212.10509) (Trivedi et al., ACL 2023) | Interleaves one reasoning step with one retrieval; up to 21 points better retrieval and up to 15 points better QA | ReAct (retrieval is a tool) |
| [Self-RAG](https://arxiv.org/abs/2310.11511) (Asai et al., ICLR 2024) | The model decides on demand whether to retrieve, using "reflection tokens," and critiques both the retrieved passages and its own output | Reflection trained into the model |
| [CRAG](https://arxiv.org/abs/2401.15884) (Yan et al., 2024) | A lightweight retrieval evaluator outputs a confidence score that triggers different actions; web search supplements poor retrieval results | Reflection + routing |
| [Adaptive-RAG](https://arxiv.org/abs/2403.14403) (Jeong et al., NAACL 2024) | A small classifier estimates query complexity and picks no retrieval, single-step, or multi-step retrieval | Routing |
| [Agentic RAG survey](https://arxiv.org/abs/2501.09136) (Singh et al., 2025) | A taxonomy of agentic RAG architectures | — |

**Key design decisions**:

1. **The simplest agentic RAG**: give a ReAct agent a `search` tool. The `recall` tool from Lesson 04 is one example.
2. **Route first**: don't run multi-hop retrieval for simple questions (the Adaptive-RAG idea); it saves money and time.
3. **Keep retrieval evaluation cheap**: judge relevance with a small model or rules, not the big model every time.
4. **Cap retrieval rounds**: "let me search once more" needs brakes too.
5. **Permissions before retrieval**: queries the agent rewrites itself must not bypass access control either; filtering has to be enforced in the retrieval layer ([Lesson 15](../15_enterprise_rag/README.en.md)).

**Architecture in one line**: ReAct (retrieval as a tool) or routing (pick a strategy by complexity) + reflection (relevance checks, citation checks).

## 5. Memory architecture

[Lesson 04, Context and Memory](../04_context_memory/README.en.md), covered how to implement short-term memory (truncation, summarization) and long-term memory (storage, retrieval, isolation). Here's a different angle: **architecturally, what kinds of memory does an agent have, and where does each one live?**

[CoALA](https://arxiv.org/abs/2309.02427) (Sumers et al., TMLR) splits language-agent memory into **working memory** and **long-term memory**, with long-term memory further divided into **episodic, semantic and procedural**. Combined with engineering practice:

```mermaid
flowchart LR
    LLM["LLM"] <--> WM["Working memory<br/>everything this call can see<br/>history, plan, to-dos, variables"]
    WM <-->|"retrieve and write"| EP[("Episodic memory<br/>what happened")]
    WM <-->|"retrieve and write"| SE[("Semantic memory<br/>what is known")]
    PR["Procedural memory<br/>how to do things<br/>system prompt, rule files, tool code, model weights"] --> WM
```

| Memory type | What it holds | Where it lives | Lifetime | Examples | In this course / agentkit |
|---|---|---|---|---|---|
| **Short-term** | Raw messages of the current session | Context window | Gone when the session ends | Multi-turn messages | `RunState.messages`; Lesson 04's sliding window and summarization |
| **Working** | Structured state **actively maintained** to finish the current task: plan, to-dos, known facts, intermediate variables | A structured block in context, or a scratch file | For the duration of the task | Plan-and-Execute's plan, ReWOO's `#E` variables, a coding agent's to-do list, the plan Anthropic's research system saves to Memory, Magentic-One's Task Ledger | Lesson 04's "structured task state"; `results` in Exercise 1 |
| **Episodic** | Specific past experiences: "last time we did this, it failed because…" | External storage, retrieved on demand | Long-term | Reflexion's reflections; the memory stream in [Generative Agents](https://arxiv.org/abs/2304.03442) (retrieved by recency, importance and relevance) | Run records and traces ([Lesson 10](../10_observability/README.en.md)) |
| **Semantic** | Facts about the world and the user: "the user is vegetarian" | Databases, vector stores | Long-term | User profiles, company knowledge bases | Lesson 04's `MemoryStore`; RAG |
| **Procedural** | How to do things: rules, skills, procedures | System prompts, rule files, tool code, model weights | Long-term, changes slowly | CLAUDE.md, AGENTS.md, skill libraries | `system_prompt`, the tools themselves |

Architectural points worth remembering:

1. **Think about short-term and working memory separately**: short-term memory is a raw record that piles up **passively** and gets truncated or summarized; working memory is structured state that's **actively** maintained and should always be kept intact. The classic long-task failure — "forgot what it was doing" — usually happens because the plan lived only in the conversation history, which got compacted.
2. **When to write is an architectural decision**: the model can call a `remember` tool mid-conversation (simple, but it costs latency and attention on the main path), or a background job can consolidate memories after the conversation ends (off the main path, but delayed).
3. **Changes to procedural memory carry the most risk**: one edited line in a system prompt or rule file affects every future action, so it should go through the same review and release process as code ([Lesson 16](../16_release_ops/README.en.md)).
4. **Every long-term memory must be isolated, deletable and poisoning-resistant**: the three hard requirements for enterprise memory in Lesson 04 apply equally to episodic, semantic and procedural memory. Designs that let the agent rewrite its own procedural memory (say, automatically updating a rule file) are especially exposed to poisoning.
5. [MemGPT](https://arxiv.org/abs/2310.08560) (Packer et al., 2023) borrows tiered memory from operating systems and lets the agent itself move data between the small, fast context and large, slow external storage — in effect, handing memory management to the agent as well.

For from-scratch implementations of these mechanisms (Mem0-style write decisions, Generative Agents-style retrieval scoring, MemGPT-style tiered memory), and how to meet the enterprise requirements of viewing, correcting, deleting, and poisoning defense, see [Lesson 18](../18_memory_systems/README.en.md).

## 6. Summary table and decision tree

### 6.1 Everything side by side

| Category | Architecture | Where the model thinks | Typical model calls | Latency profile | Predictability | Good for | Biggest risk |
|---|---|---|---|---|---|---|---|
| Single agent | ReAct | Every step | N (steps) | Adds up serially; slower as history grows | Low | Unknown paths | Myopia, spinning, context bloat |
| Single agent | Plan-and-Execute | Start + on failure + end | 2 + replans (code executor) | Fast execution phase | Medium | Multi-step tasks you can roughly plan | Bad plans, replan loops |
| Single agent | ReWOO / LLMCompiler | Start + end | 2 (fixed) | Tools can run in parallel; fastest | High | Data-gathering, parallelizable tasks | Can't adapt to surprises |
| Layer | Reflection | After the work | +2 per round | A few more serial rounds | Medium | Objectively checkable output | Useless without external evidence; spinning |
| Single agent | CodeAct | Every step, each doing more | Fewer than ReAct | Plus sandbox overhead | Low | Many tools, data processing | Security risk of running code |
| Single agent | Tree search | On every branch | Branches × depth × 2 | Slowest | Low | Hard problems with a scorer and a reversible environment | Cost explosion; needs rollback |
| Layer | HITL | Model proposes, human decides | Unchanged | Plus human wait time | High | Irreversible, high-risk actions | Approval fatigue |
| Multi-agent | Router + experts | Router once, then each expert | 1 + the expert's calls | Close to single agent | High | Clear-cut categories | Misrouting |
| Multi-agent | Supervisor / hierarchy | Supervisor + each expert | Sum of supervisor and experts | Slowest branch when parallel; serial when dependent | Medium | Parallelizable work needing one answer | Token multiplication, supervisor bottleneck |
| Multi-agent | Network / swarm | The active agent | Depends on handoffs | Close to single agent | Medium–low | Multi-stage conversational service | Ping-pong, lost state |
| Multi-agent | Blackboard | Each agent + the controller | Depends on rounds | Can be asynchronous | Medium–low | Collaboration on a shared document | Write conflicts, errors spreading |
| Multi-agent | Event-driven | One run per event | Each event = one single-agent run | Async; throughput is what matters | Medium | Background automation | Event storms, duplicate execution |

### 6.2 Decision tree

Pick the base single-agent architecture first, then decide what to layer on, and only then consider multiple agents:

```mermaid
flowchart TD
    S["New requirement"] --> Q0{"Can a developer<br/>hard-code the steps"}
    Q0 -->|"yes"| WF["Workflow<br/>Lesson 06"]
    Q0 -->|"no"| Q1{"Can the model list every step<br/>once it sees the task"}
    Q1 -->|"yes"| Q2{"Are the steps mostly<br/>independent and parallelizable"}
    Q2 -->|"yes"| RW["ReWOO or LLMCompiler"]
    Q2 -->|"no"| PE["Plan-and-Execute"]
    Q1 -->|"no"| Q3{"Does it need to explore several paths<br/>with a reliable scorer<br/>and tolerable cost of mistakes"}
    Q3 -->|"yes"| TS["Tree search or best-of-N"]
    Q3 -->|"no"| RA["ReAct"]
    WF --> O1{"Can the output be<br/>checked objectively"}
    RW --> O1
    PE --> O1
    RA --> O1
    TS --> O2
    O1 -->|"yes"| RF["Add Reflection<br/>feed the check results back"]
    O1 -->|"no"| O2{"Any irreversible or<br/>high-risk actions"}
    RF --> O2
    O2 -->|"yes"| HI["Add HITL checkpoints"]
    O2 -->|"no"| O3{"Is a single agent clearly failing<br/>from context or tool overload"}
    HI --> O3
    O3 -->|"no"| DONE["Stay with one agent"]
    O3 -->|"yes"| MA["Multiple agents<br/>pick a topology below"]
```

Two independent questions sit alongside the tree. **Action space**: with many tools, a need for loops and data processing, and a trustworthy sandbox, use CodeAct as the way the agent acts — it combines with any of the above. **Does planning need its own layer?** Often, giving ReAct a to-do list tool is enough.

Picking a multi-agent topology:

```mermaid
flowchart TD
    M["Why do you need several agents"] -->|"different request types<br/>each handled independently"| R["Router + experts"]
    M -->|"parallelizable subtasks<br/>that need one combined answer"| S["Supervisor<br/>add layers when there are too many experts"]
    M -->|"different stages of a conversation<br/>served directly by different specialists"| N["Network or handoffs"]
    M -->|"work on one shared document<br/>in no fixed order"| B["Blackboard"]
    M -->|"nobody online<br/>triggered by events"| E["Event-driven"]
```

Exercise 3's `choose_architecture` is the first decision tree written as code. **The point of a decision tree isn't to produce the one right answer; it's to force you to explain why the simpler option isn't enough.**

## 7. Hands-on: run the demo

```bash
.venv/bin/python lessons/05_agent_architectures/demo.py            # real model, about 40 seconds
.venv/bin/python lessons/05_agent_architectures/demo.py --offline  # scripted, no API key needed
```

An excerpt from a real run (gpt-5.5). The demo prints in Chinese; it's translated here for readability:

```text
Architecture 1: ReAct — think while you act (agentkit.Agent is ReAct)
  Trajectory:
    Step 1 (model) → get_weather(Beijing,10-15)  get_weather(Shanghai,10-16)  get_weather(Guangzhou,10-17)
        ← ❌ Error: the Guangzhou weather station API is under maintenance (503). Use get_weather_by_airport with the airport code instead; Guangzhou Baiyun is CAN.
    Step 2 (model) → get_weather_by_airport(CAN,10-17)
    Step 3 (model) → answers

Architecture 2: Plan-and-Execute — plan, execute, replan only on failure
  📋 Plan from the planner (1 model call):
    s1: get_weather(Beijing,10-15)
    s2: get_weather(Shanghai,10-16)
    s3: get_weather(Guangzhou,10-17)
  ⚙️  Execution (code calls the tools one by one, no model involved):
    ✅ s1: get_weather(Beijing,10-15)
    ✅ s2: get_weather(Shanghai,10-16)
    ❌ s3: get_weather(Guangzhou,10-17) → Error: the Guangzhou weather station API is under maintenance (503)...
    🔁 Replan (#1, 1 model call) → s3: get_weather_by_airport(CAN,10-17)
    ✅ s3: get_weather_by_airport(CAN,10-17)

Architecture 3: Reflection — draft, critique, revise
    Step 1 (model) → get_weather(Beijing,10-15)  get_weather(Shanghai,10-16)  get_weather(Guangzhou,10-17)
    Step 2 (model) → get_weather_by_airport(CAN,10-17)
    Step 3 (model) → answers
    🔍 Review round 1 · model check → ✅ passed

Comparison: one task, three architectures
  Architecture      Model calls  Tool calls  Tokens  Time    Quality check                 Where the model thinks
  ReAct             3            4           2705    10.7s   3/3 cities · typhoon✅ · 4 lines✅   every step
  Plan-and-Execute  3            4           2724    14.3s   3/3 cities · typhoon✅ · 4 lines✅   plan + replan on failure + summary
  Reflection        4            4           3716    11.2s   3/3 cities · typhoon✅ · 4 lines✅   ReAct draft + model/code critique + revise
```

**What to look for:**

1. **Same surprise, two responses**: ReAct stepped around the Guangzhou outage inside its loop, because every step goes back to the model. Plan-and-Execute's executor is plain code, so it had to stop and spend a model call on replanning.
2. **Planning first didn't save anything here**: ReAct called 3 tools **in parallel** on step 1 and finished in 3 steps while the history was still short. Meanwhile, every Plan-and-Execute planning call goes through `complete_json`, which puts the JSON Schema into the prompt, and the replanning prompt repeats the context. Plan-and-Execute pulls ahead on tasks with **many steps**: ReAct's input tokens grow roughly quadratically with step count, while Plan-and-Execute spends zero model tokens during execution. Add a few more cities to `WEATHER` and `TRIP` and rerun to watch the gap change.
3. **Plan-and-Execute was actually the slowest**: its 3 model calls are all serial, each carrying a long structured prompt. "No model during execution" saves time in the execution phase, not in the planning itself.
4. **Reflection's insurance premium**: in the real run the first draft passed straight away, and it still cost 1 more call and ~1,000 more tokens. In offline mode (`--offline`) the script deliberately leaves the typhoon warning out of the first draft: you'll see round 1 rejected by the **code check** without spending a single model call, and only round 2 checked by the model.
5. **The quality-check column** is a minimal evaluation written in a few lines of code. Architecture choices should rest on data like this, not on names ([Lesson 11](../11_evals/README.en.md)).

## 8. Exercises

Open [`exercise.py`](exercise.py) and implement skeletons for three architectures. The planner, executor and critic are plain functions, so the tests run fully offline and deterministically:

**Task 1: `PlanExecuteAgent`**
- The planner returns a structured list of steps (`Step`); validate the plan first: non-empty, correct element types, unique ids;
- The executor runs one step at a time — each step is a tool call or a subtask — and later steps can see earlier results;
- When a step fails, call the replanner with the completed results, the failed step, the error message and the remaining plan; the new plan must not overwrite completed steps;
- Both replans and executed steps are capped; return the status, the stop reason, the results and a complete execution trace.

**Task 2: `reflect_loop(generate, critique, max_rounds, stop_when)`**
- Generate → critique → revise based on the critique, until `stop_when` is satisfied or the round limit is reached;
- **Stop early when a critique repeats any earlier one** (compared after normalization), which catches both spinning in place and A → B → A oscillation.

**Task 3: `choose_architecture(task_profile) -> str`**
- Pick a base architecture from how predictable the steps are, whether exploration is needed, whether the work can run in parallel, whether high reliability is required, and whether the output can be checked objectively; then layer on `reflection` and `hitl`;
- The rules are spelled out in the docstring and mirror the decision tree in §6.2.

Check your work:

```bash
make lesson N=05                     # done when everything passes (30 tests)
AGENTKIT_SOLUTION=1 make lesson N=05 # run against the reference solution to confirm the tests themselves are right
```

## 9. Going deeper (📖 Optional)

### 9.1 Architectures are being trained into models

OpenAI deep research and CUA both emphasize reinforcement learning that teaches plan → act → backtrack to the model itself; Self-RAG trains "should I retrieve, and was the retrieval any good" into the model's outputs. The implication: **keep external architecture as thin as you can.** A task that needs Plan-and-Execute today may need nothing more than ReAct with a to-do tool next year. A good habit is to treat every layer of your architecture as scaffolding that compensates for a model weakness, and to check it regularly against your eval set: does quality actually drop if you remove this layer?

### 9.2 ReAct's quadratic cost and prompt caching

ReAct resends the whole history at every step, so N steps cost roughly N²/2 "per-step increments" of input tokens. Two mitigations: **prompt caching** (when the history prefix doesn't change, the repeated input is cheaper and faster; see [Lesson 14](../14_cost_latency/README.en.md)), and **compacting old tool results** (Lesson 04). This is also the root reason CodeAct and ReWOO save tokens: intermediate data never goes back into the model's context.

### 9.3 The cost of structured output

Plan-and-Execute, ReWOO and Reflection all rely on structured output (plans, reviews). `complete_json` puts the JSON Schema in the prompt and may need repair retries. The demo shows this overhead can wipe out the savings of "no model during execution" on small tasks. When your model or gateway supports native structured output, use it.

### 9.4 Why multi-agent systems fail

[Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657) (Cemri et al., 2025) analyzed a large set of real execution traces and grouped the failures into three categories: system design issues, inter-agent misalignment, and inadequate task verification. Mapping this onto §3 suggests (our inference, not the paper's finding) that supervisors need extra guarding against weak verification (trusting experts blindly), and networks against misalignment (context lost in handoffs).

### 9.5 Seeing architectures as state machines

Every architecture can be drawn as a state graph: nodes are "call the model / run a tool / wait for a human," and edges are "where to go next." ReAct is one node looping on itself; Plan-and-Execute is "plan → execute (looping) → replan"; HITL inserts a "wait for an external event" state on an edge. That's the starting point for graph-orchestration frameworks like LangGraph: once your architecture needs branches, loops, pauses and resumption, drawing it explicitly as a state graph makes it easier to see and test than burying it in a `while` loop (see [docs/framework-comparison.md](../../docs/framework-comparison.en.md) for a framework mapping).

## 10. Common pitfalls and anti-patterns (📖 Optional)

| Pitfall | What happens | Do this instead |
|---|---|---|
| Choosing by hype ("everyone's doing multi-agent") | Double the cost, not necessarily better results | Start with ReAct or a workflow and let eval data justify anything more complex |
| Plan-and-Execute with no replan limit | Fail → replan → fail, forever | Cap replans and executed steps (Exercise 1) |
| A plan that's a paragraph of free text | The executor has to guess; parsing fails | A structured plan with schema validation and an enum of tool names |
| ReWOO on tasks where results change the plan | When something unexpected happens, it answers with bad data | Use Plan-and-Execute, or add replanning |
| "Self-reflection" with no external evidence | Costs more, results unstable or worse | Give the critic test results, validators or raw data to check against |
| Reflection without repeat detection | The same feedback comes back round after round, burning money | Stop early when feedback repeats (Exercise 2) |
| CodeAct via in-process `exec()` | Model-written code can read and write your whole system | An isolated sandbox — containers, gVisor, Firecracker — with resource and network limits |
| Tree search in an environment with side effects | Emails and orders sent "tentatively" can't be undone | Search only where you can roll back; in production use best-of-N + a validator |
| Requiring approval for everything | Approval fatigue; people click "approve" without reading | Tier by risk; use sandboxing to cut unnecessary prompts |
| Waiting for approval with a blocking `input()` | A restart loses the approval, and the process sits idle | Persist state and resume asynchronously |
| Unlimited handoffs | Agents play ping-pong | Cap handoffs and record the chain |
| Blackboard entries without provenance | One agent's wrong conclusion becomes everyone's fact | Every entry records author, source and version |
| Event-driven agents without idempotency | A redelivered event runs its action twice | Use the event ID as the idempotency key |
| Keeping the plan only in conversation history | After compaction the agent "forgets what it was doing" | Put the plan in working memory (structured state or an external file) |

## 11. Interview and design-review questions (📖 Optional)

<details>
<summary>Q1: What's the essential difference between ReAct and Plan-and-Execute? When is each better?</summary>

- The difference is where the model thinks: ReAct goes back to the model at every step; Plan-and-Execute plans once, runs the execution phase without the (big) model, and replans only on failure;
- ReAct suits tasks whose path can't be predicted and that need to adapt; the cost is myopia, and input tokens grow roughly quadratically with steps;
- Plan-and-Execute suits long tasks you can roughly plan: fast execution, a plan that can be approved, and an executor that can be a small model; the cost is that plan quality becomes the bottleneck, so you need replanning and limits;
- With few steps and parallel tool calls, ReAct isn't necessarily more expensive (as measured in this lesson's demo);
- Common middle grounds: ReAct plus a to-do list tool, or Plan-and-Execute with a ReAct executor.
</details>

<details>
<summary>Q2: Why does ReWOO save tokens? What does it give up? What did LLMCompiler improve?</summary>

- The planner writes the complete plan, variable dependencies included, in one go; execution never returns to the model, so observations never enter the planner's context; model calls are fixed at 2 (the paper reports 5× token efficiency on HotpotQA);
- What it gives up is adaptability: when an intermediate result is unexpected, the original design has no chance to change the plan;
- LLMCompiler represents the plan as a task dependency graph, runs independent tasks in parallel, and supports replanning once intermediate results are in.
</details>

<details>
<summary>Q3: Does reflection always improve quality? How do you make it work?</summary>

- No. Huang et al. (ICLR 2024) found that on reasoning tasks, self-correction without external feedback helps little and sometimes hurts;
- Effective reflection depends on external evidence: test results, schema validation, business rules, tool data, retrieved sources;
- Design points: use code checks wherever possible; make feedback specific and actionable; cap the rounds; detect repeated feedback and oscillation; have a fallback at the limit (return the current version or escalate to a human).
</details>

<details>
<summary>Q4: What does CodeAct buy you? What do you need to watch for in an enterprise deployment?</summary>

- Benefits: one action can express loops, conditionals and data processing, so fewer actions; intermediate data stays in the execution environment instead of the context, cutting tokens dramatically; models are good at writing code;
- Risk: running model-written code is the biggest attack surface there is;
- Deployment: an isolated sandbox (containers / gVisor / Firecracker), no network or an allowlist, a read-only file system, CPU/memory/time limits; tools injected into the sandbox as controlled functions, with identity and credentials never exposed to the code; mark code execution as high-risk and log every snippet for audit.
</details>

<details>
<summary>Q5: How do you choose between supervisor–expert, handoffs and a blackboard?</summary>

- Ask the three questions: communication, state sharing, control;
- Supervisor–expert: control stays with the supervisor; experts see only their task and return summaries; good when you need one combined answer and subtasks can run in parallel; risks are a supervisor bottleneck and tasks missing context;
- Handoffs: control is transferred, and whoever takes over talks to the user directly with the shared conversation history; good for multi-stage conversational service; risks are ping-pong and lost state;
- Blackboard: indirect communication through shared structured state; good for collaboration on one working document in no fixed order; risks are write conflicts and wrong conclusions spreading, so you need versions, provenance and confidence.
</details>

<details>
<summary>Q6: How would you design a deep research agent?</summary>

- Clarify the request and draft a research plan for the user to confirm (plan-level HITL);
- A lead agent splits the research into directions and writes the plan to external memory so it survives context truncation;
- Launch several subagents in parallel, each searching and reading in its own context and returning only condensed findings with sources;
- The lead agent decides whether it has enough and dispatches more if not, under an overall budget and round limit;
- A separate citation check ensures every claim has a source;
- Evaluate with a set of research questions: factual accuracy, citation correctness, coverage, token cost;
- Trade-off: multi-agent uses about 15× the tokens of chat, so use it only when the question is broad enough to be worth it.
</details>

<details>
<summary>Q7: How does an event-driven agent differ architecturally from a conversational one?</summary>

- It's triggered by event streams, not user messages; nobody is waiting at a screen, so latency matters less and throughput matters more;
- It must handle concurrency and rate limits, deduplication and idempotency (event ID as the idempotency key), event storms (batching, rate limiting), and self-triggering loops (tag events by source);
- Humans take part asynchronously through an inbox (notify, question, review), so pausing and resuming must be durable;
- Default to read-only or suggestions, with writes behind approval; observability and alerting matter even more because nobody sees mistakes as they happen.
</details>

<details>
<summary>Q8: How many layers of memory should an agent have, and where does each live?</summary>

- Short-term: the raw messages of the current session, kept in context and subject to truncation or summarization;
- Working: structured state actively maintained for the current task (plan, to-dos, intermediate variables), which should be kept intact and never compacted;
- Long-term: episodic (past experiences, such as reflections), semantic (facts, such as user preferences and knowledge bases), procedural (how to do things, such as system prompts, rule files, tool code);
- All long-term memory must be isolated, deletable and resistant to poisoning; changes to procedural memory should go through review and release.
</details>

## 12. Self-check

- [ ] I can tell ReAct, Plan-and-Execute, ReWOO, Reflection, CodeAct and tree search apart in one sentence each, using "where the model thinks"
- [ ] I can explain why ReAct's cost grows roughly quadratically with steps, and why it wasn't more expensive than Plan-and-Execute in the demo
- [ ] I know when reflection works and can cite a counterexample
- [ ] I know what CodeAct buys you and why it absolutely requires a sandbox
- [ ] I can name five places to put a HITL checkpoint, and how to avoid approval fatigue
- [ ] I can compare the five multi-agent topologies by communication, state sharing and control
- [ ] I can describe deep research, coding agents, computer use, customer support and agentic RAG each as a one-line combination of architectures
- [ ] I can distinguish short-term, working, episodic, semantic and procedural memory and say where each lives
- [ ] I can use the decision tree to pick an architecture for a new requirement and explain why the simpler option isn't enough
- [ ] I've finished the exercises and `make lesson N=05` passes

## Further reading

**Single-agent architectures**

- Yao et al., [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629) (ICLR 2023)
- Wang et al., [Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning by Large Language Models](https://arxiv.org/abs/2305.04091) (ACL 2023)
- Xu et al., [ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models](https://arxiv.org/abs/2305.18323) (2023)
- Kim et al., [An LLM Compiler for Parallel Function Calling](https://arxiv.org/abs/2312.04511) (ICML 2024)
- Shinn et al., [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366) (NeurIPS 2023)
- Madaan et al., [Self-Refine: Iterative Refinement with Self-Feedback](https://arxiv.org/abs/2303.17651) (NeurIPS 2023)
- Huang et al., [Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/abs/2310.01798) (ICLR 2024)
- Wang et al., [Executable Code Actions Elicit Better LLM Agents](https://arxiv.org/abs/2402.01030) (CodeAct, ICML 2024)
- Yao et al., [Tree of Thoughts: Deliberate Problem Solving with Large Language Models](https://arxiv.org/abs/2305.10601) (NeurIPS 2023)
- Zhou et al., [Language Agent Tree Search Unifies Reasoning Acting and Planning in Language Models](https://arxiv.org/abs/2310.04406) (LATS, ICML 2024)
- LangChain blog: [Planning Agents](https://www.langchain.com/blog/planning-agents) (Plan-and-Execute, ReWOO and LLMCompiler compared), [Reflection Agents](https://www.langchain.com/blog/reflection-agents)

**Multi-agent topologies**

- Fourney et al., [Magentic-One: A Generalist Multi-Agent System for Solving Complex Tasks](https://arxiv.org/abs/2411.04468) (Microsoft, 2024)
- Hong et al., [MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework](https://arxiv.org/abs/2308.00352) (ICLR 2024)
- Erman et al., [The Hearsay-II Speech-Understanding System: Integrating Knowledge to Resolve Uncertainty](https://doi.org/10.1145/356810.356816) (ACM Computing Surveys, 1980) — the classic blackboard architecture
- Han and Zhang, [Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture](https://arxiv.org/abs/2507.01701) (2025)
- LangChain, [Introducing ambient agents](https://www.langchain.com/blog/introducing-ambient-agents) (2025)
- LangChain docs: [Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent); OpenAI Agents SDK: [Handoffs](https://openai.github.io/openai-agents-python/handoffs/)
- OpenAI, [A practical guide to building agents](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) (2025)
- [The A2A protocol](https://a2a-protocol.org/latest/)

**Product architectures**

- Anthropic, [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) (2025)
- Anthropic, [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents) (2024)
- Anthropic, [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) (2025)
- Anthropic, [Making Claude Code more secure and autonomous with sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) (2025)
- Claude Code docs: [permission modes](https://code.claude.com/docs/en/permission-modes), [subagents](https://code.claude.com/docs/en/sub-agents)
- OpenAI, [Codex system card](https://cdn.openai.com/pdf/8df7697b-c1b2-4222-be00-1fd3298f351d/codex_system_card.pdf) (2025); Codex [sandboxing docs](https://learn.chatgpt.com/docs/sandboxing)
- OpenAI, [Computer-Using Agent](https://openai.com/index/computer-using-agent/) (2025); Xie et al., [OSWorld](https://arxiv.org/abs/2404.07972) (2024)
- Yao et al., [τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045) (ICLR 2025)
- [openai-cs-agents-demo](https://github.com/openai/openai-cs-agents-demo); [open_deep_research](https://github.com/langchain-ai/open_deep_research)

**Agentic RAG and memory**

- Trivedi et al., [IRCoT](https://arxiv.org/abs/2212.10509) (ACL 2023); Asai et al., [Self-RAG](https://arxiv.org/abs/2310.11511) (ICLR 2024); Yan et al., [CRAG](https://arxiv.org/abs/2401.15884) (2024); Jeong et al., [Adaptive-RAG](https://arxiv.org/abs/2403.14403) (NAACL 2024); Singh et al., [Agentic RAG survey](https://arxiv.org/abs/2501.09136) (2025)
- Sumers et al., [Cognitive Architectures for Language Agents](https://arxiv.org/abs/2309.02427) (CoALA, TMLR)
- Park et al., [Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442) (UIST 2023); Packer et al., [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) (2023)
