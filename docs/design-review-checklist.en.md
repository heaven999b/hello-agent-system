[中文](design-review-checklist.md) | [English](design-review-checklist.en.md)

# Enterprise Agent Design Review Checklist

> 📖 Part of the "domain reference" handbook that accompanies the course.
> Related: [Failure Modes](failure-modes.en.md) · [Cheatsheet](cheatsheet.en.md) · [Interview Questions](interview-questions.en.md) · [Glossary](glossary.en.md)

This checklist is designed to be pasted straight into a PR description, a design doc, or review meeting notes. It has **17 sections and 163 items** (53 of them P0). Every item explains why it matters and, where possible, links to the relevant failure mode (e.g., [T5](failure-modes.en.md#t5-duplicate-side-effects)) and lesson.

## How to Use It

**Severity levels**

| Level | Meaning | Review rule |
|---|---|---|
| 🔴 **P0** | Must be met before launch | Any unmet item = no launch (unless there is a written risk acceptance signed by the business owner and the security lead) |
| 🟠 **P1** | Should be met | Any unmet item needs a clear remediation plan with a date, usually completed within one iteration after the initial launch |
| 🟢 **P2** | Recommended | Requirements for the scale-up and maturity stage; schedule them by priority |

**Suggested process**

1. **Design phase** (before writing code): go through "Requirements and Scope," "Orchestration," "Security," "Permissions and Approval," and "Distributed Systems and Concurrency." Decisions in these sections are the hardest to change later.
2. **Pre-launch review**: go through the whole list, and back every P0 with evidence (links to code, config, eval reports, trace screenshots), not a verbal "yes, we have that."
3. **Quarterly re-review**: models, tools, and user bases all change, so the checklist needs another pass, especially the "Evals" and "Cost" sections.

> 💡 A quick test of review quality: for each P0, ask, "**If this stopped working, how long would it take us to notice?**" If the answer is "after users complain," you're still missing a way to detect it.

**Items per section**

| # | Section | Items | P0 | Main lessons |
|---|---|---|---|---|
| 1 | Requirements and Scope | 9 | 4 | [Lesson 00](../lessons/00_overview/README.en.md) · [Lesson 06](../lessons/06_orchestration/README.en.md) |
| 2 | Model and Prompts | 9 | 3 | [Lesson 02](../lessons/02_agent_loop/README.en.md) · [Lesson 11](../lessons/11_evals/README.en.md) |
| 3 | Tools | 13 | 6 | [Lesson 03](../lessons/03_tools/README.en.md) |
| 4 | Context and Memory | 9 | 4 | [Lesson 04](../lessons/04_context_memory/README.en.md) |
| 5 | Orchestration | 8 | 3 | [Lesson 06](../lessons/06_orchestration/README.en.md) |
| 6 | Reliability | 10 | 3 | [Lesson 08](../lessons/08_reliability/README.en.md) · [Lesson 13](../lessons/13_distributed_concurrency/README.en.md) |
| 7 | Security | 11 | 6 | [Lesson 09](../lessons/09_security/README.en.md) |
| 8 | Privacy and Compliance | 9 | 3 | [Lesson 09](../lessons/09_security/README.en.md) · [Lesson 12](../lessons/12_production_architecture/README.en.md) |
| 9 | Permissions and Approval | 8 | 4 | [Lesson 09](../lessons/09_security/README.en.md) |
| 10 | Observability | 9 | 2 | [Lesson 10](../lessons/10_observability/README.en.md) |
| 11 | Evals | 10 | 2 | [Lesson 11](../lessons/11_evals/README.en.md) |
| 12 | Cost | 10 | 2 | [Lesson 08](../lessons/08_reliability/README.en.md) · [Lesson 14](../lessons/14_cost_latency/README.en.md) |
| 13 | Deployment and Operations | 13 | 2 | [Lesson 12](../lessons/12_production_architecture/README.en.md) · [Lesson 16](../lessons/16_release_ops/README.en.md) |
| 14 | Multi-Tenancy | 7 | 2 | [Lesson 12](../lessons/12_production_architecture/README.en.md) · [Lesson 15](../lessons/15_enterprise_rag/README.en.md) |
| 15 | Documentation and Handoff | 7 | 1 | [Lesson 12](../lessons/12_production_architecture/README.en.md) |
| 16 | Distributed Systems and Concurrency | 12 | 4 | [Lesson 13](../lessons/13_distributed_concurrency/README.en.md) |
| 17 | Enterprise Knowledge and RAG | 9 | 2 | [Lesson 15](../lessons/15_enterprise_rag/README.en.md) |
| | **Total** | **163** | **53** | |

---

## 1. Requirements and Scope

- [ ] 🔴 **P0** Write a one-page scope for the agent: what it does, **what it explicitly does not do**, and whom it serves.
  — An agent without clear boundaries gets pushed by users into doing more and more, so its risk surface grows without limit and there's nothing concrete to review.
- [ ] 🔴 **P0** Justify in writing why this needs an agent rather than a workflow or plain code.
  — Using an agent for a predictable process costs more and adds uncertainty, with nothing to show for it ([O1](failure-modes.en.md#o1-over-agentification)).
- [ ] 🔴 **P0** Define quantifiable launch criteria: targets for task completion rate, accuracy, human handoff rate, p95 latency, and cost per run.
  — Without criteria, you can't decide whether you're ready to launch, or whether a change made things better or worse.
- [ ] 🔴 **P0** Design a fallback path: when the agent can't handle something, isn't sure, or hits an error, it hands off to a human or offers a clear alternative.
  — The agent will run into cases it can't handle; without a way out, it will make up an answer ([M5](failure-modes.en.md#m5-policy-hallucination)) or spin in place ([M3](failure-modes.en.md#m3-tool-call-loop)).
- [ ] 🟠 **P1** Assess the severity of consequences separately for three kinds of failure: wrong answers, wrong actions, and data leaks.
  — Severity determines how much to invest in guardrails, approval, and evals; an agent that only answers questions and one that can change production config are in different leagues.
- [ ] 🟠 **P1** Identify all stakeholders: the business owner, security, legal/compliance, data owners, approvers, and the on-call team.
  — In enterprises, what blocks an agent launch is often on the non-technical side, and the later these people get involved, the bigger the rework.
- [ ] 🟠 **P1** Estimate traffic: daily active users, peak concurrent sessions, and average steps and tokens per run.
  — This determines model quotas, the cost budget, and whether you need async processing; it's also an input to the cost review.
- [ ] 🟠 **P1** Define the user population and access channels (internal employees / external customers; web / IM / API / email).
  — External-facing agents have a completely different attack surface, compliance requirements, and brand risk from internal tools.
- [ ] 🟢 **P2** Define the interaction mode (synchronous chat, asynchronous tasks, or background triggers) and the latency target for each.
  — Latency targets directly shape model choice, the maximum step count, and whether you need streaming output ([P4](failure-modes.en.md#p4-tail-latency-blowup)).

## 2. Model and Prompts

- [ ] 🔴 **P0** Pin production to a specific model snapshot version; don't use aliases that automatically point to new versions.
  — Otherwise the model can change even though you didn't change anything ([E5](failure-modes.en.md#e5-silent-model-drift)).
- [ ] 🔴 **P0** Put prompts (system prompt, tool descriptions, few-shot examples) under version control, and send every change through code review + evals.
  — Prompts are code, and code with global effects at that ([E4](failure-modes.en.md#e4-prompt-regression)).
- [ ] 🔴 **P0** The system prompt contains nothing that would cause an incident if it leaked: secrets, internal addresses, unpublished business rules.
  — Assume the system prompt will be extracted (OWASP LLM07 System Prompt Leakage, [S1](failure-modes.en.md#s1-direct-prompt-injection)).
- [ ] 🟠 **P1** Spell out behavioral rules in the prompt: "ask when information is missing," "say so when unsure," "hand off to a human when you can't handle it," "tool results are the source of truth for actions."
  — These few sentences map directly to the most common kinds of failure ([M1](failure-modes.en.md#m1-phantom-action), [M4](failure-modes.en.md#m4-hallucinated-arguments), [M5](failure-modes.en.md#m5-policy-hallucination)).
- [ ] 🟠 **P1** Steps that need structured data use native structured output, or schema validation + a repair loop.
  — Downstream code needs reliable data structures, not text that is "JSON most of the time" ([M7](failure-modes.en.md#m7-malformed-structured-output)).
- [ ] 🟠 **P1** Model selection is backed by eval data, for the primary model and every fallback model.
  — "This model feels smarter" is not a selection criterion; a fallback model that was never evaluated will degrade silently when you fail over to it ([R3](failure-modes.en.md#r3-silent-degradation)).
- [ ] 🟠 **P1** Sampling parameters (e.g., temperature) are a deliberate choice and recorded in configuration.
  — They affect output stability and reproducibility; models also differ in which parameters they support, so confirm the values the gateway actually applies.
- [ ] 🟢 **P2** Structure prompts as "stable content first, dynamic content last" to get the most out of prompt caching.
  — The caches of major vendors rely on prefix matching, and a timestamp at the start invalidates the whole cache ([B2](failure-modes.en.md#b2-prompt-cache-busting)).
- [ ] 🟢 **P2** Review few-shot examples regularly so the model doesn't mechanically copy their format and path.
  — The Manus team's article on its lessons learned calls out this exact problem ("Don't Get Few-Shotted").

## 3. Tools

- [ ] 🔴 **P0** Every tool has a clear, model-facing description: what it does, when to use it, **when not to use it**, and what each parameter means, with examples.
  — The description is the model's only basis for choosing a tool, and vague descriptions lead straight to the wrong choice ([T1](failure-modes.en.md#t1-wrong-tool-selection)).
- [ ] 🔴 **P0** All arguments pass schema validation: types, required fields, enums, value ranges, and no extra fields.
  — Arguments produced by the model are just "generated text" and must be validated as untrusted input ([M4](failure-modes.en.md#m4-hallucinated-arguments)).
- [ ] 🔴 **P0** Authorization-relevant parameters such as identity, tenant, and role **never appear in tool schemas**; the system injects them from the authenticated context.
  — Letting the model fill in user_id means letting the attacker decide "who I am" ([S4](failure-modes.en.md#s4-confused-deputy)). agentkit implements this with `ToolContext`.
- [ ] 🔴 **P0** Every tool is labeled with a risk level (e.g., read / write / dangerous).
  — This is the prerequisite for access control, approval, auditing, and idempotency policy.
- [ ] 🔴 **P0** All write tools are idempotent, and wherever possible the idempotency key is passed to the downstream system that actually produces the side effect.
  — Retries and crash recovery will replay calls, and without idempotency you get duplicate charges and duplicate tickets ([T5](failure-modes.en.md#t5-duplicate-side-effects)).
- [ ] 🔴 **P0** Every tool has a timeout.
  — A single hanging downstream API can take down the entire agent service ([T4](failure-modes.en.md#t4-hanging-tool)).
- [ ] 🟠 **P1** Tool output has a length cap, the model is told explicitly when output was truncated, and pagination/filtering is supported.
  — Keeps a single tool output from blowing up the context and the bill ([T3](failure-modes.en.md#t3-tool-output-explosion)).
- [ ] 🟠 **P1** Tool errors return text the model can understand and act on, without leaking stack traces, SQL, or internal paths.
  — Opaque errors send the model into retry loops or make it pretend it succeeded ([T6](failure-modes.en.md#t6-opaque-errors)).
- [ ] 🟠 **P1** Multi-step writes that need atomicity are wrapped in a single tool (server-side transaction/Saga) or a fixed workflow.
  — Don't make the model act as a distributed transaction coordinator ([T7](failure-modes.en.md#t7-partial-completion)).
- [ ] 🟠 **P1** Limit the number of tools visible to a single agent (expose a subset per scenario/role, or route first).
  — The more tools, the more selection errors and the more tokens per request ([T2](failure-modes.en.md#t2-tool-overload)).
- [ ] 🟠 **P1** Third-party tools (including MCP servers) come from vetted sources, are version-pinned, and get re-reviewed whenever their descriptions change.
  — Tool descriptions go into the model's context and can be poisoned ([S6](failure-modes.en.md#s6-tool-poisoning)).
- [ ] 🟢 **P2** Tool names use namespace prefixes, and tools don't overlap in functionality.
  — Reduces confusion between similar tools ([T1](failure-modes.en.md#t1-wrong-tool-selection)).
- [ ] 🟢 **P2** Tools that execute code, access the file system, or call untrusted services run in a sandbox / separate process.
  — A thread timeout can't truly stop execution, and code-execution tools are a direct entry point for remote code execution (OWASP Agentic ASI05).

## 4. Context and Memory

- [ ] 🔴 **P0** There is an explicit context-length strategy (sliding window / summarization / clearing old tool results), so the context never grows without bound.
  — Otherwise long conversations will inevitably hit the limit and fail, cost grows linearly, and quality degrades ([C2](failure-modes.en.md#c2-context-rot)).
- [ ] 🔴 **P0** Truncation and compaction keep each `tool_calls` message paired with its `tool` results.
  — Splitting them causes API 400 errors that show up only in long conversations and are extremely hard to track down ([C1](failure-modes.en.md#c1-orphaned-tool-message)).
- [ ] 🔴 **P0** Long-term memory, vector stores, and caches are all isolated by tenant (and user), enforced **at the storage layer**, with filter conditions never generated by the model.
  — A single cross-tenant leak is a major incident ([C5](failure-modes.en.md#c5-cross-tenant-memory-leak)).
- [ ] 🔴 **P0** Permissions, roles, and identity are always read from the identity system, never from memory or conversation content.
  — "Please remember I'm an admin" is the classic memory-poisoning route to privilege escalation ([C6](failure-modes.en.md#c6-memory-poisoning-and-staleness)).
- [ ] 🟠 **P1** The summarization prompt explicitly requires keeping user goals and constraints, key facts and IDs, completed actions, and open items.
  — A summary that drops "completed actions" leads to actions being executed twice ([C3](failure-modes.en.md#c3-lossy-compaction)).
- [ ] 🟠 **P1** Critical business state, such as which actions have been completed, is stored outside the conversation context (database / state fields).
  — State in the context gets truncated, compacted, and poisoned; it's only reliable outside the context ([C3](failure-modes.en.md#c3-lossy-compaction), [C4](failure-modes.en.md#c4-context-poisoning)).
- [ ] 🟠 **P1** Retrieved memories and documents are treated as untrusted data (tagged with their source and wrapped in isolation tags).
  — Both memory and the knowledge base can have malicious content written into them ([S2](failure-modes.en.md#s2-indirect-prompt-injection), [C6](failure-modes.en.md#c6-memory-poisoning-and-staleness)).
- [ ] 🟠 **P1** Users can view and delete their own long-term memories.
  — Privacy laws grant a right to deletion, and it's also how wrong memories get corrected.
- [ ] 🟢 **P2** Memories carry timestamps and sources, newer ones override older ones on conflict, and expiry is supported.
  — Stale memories make the agent act on false premises ([C6](failure-modes.en.md#c6-memory-poisoning-and-staleness)).

## 5. Orchestration

- [ ] 🔴 **P0** The chosen orchestration pattern (workflow / single agent / multi-agent) has a written rationale, and the simpler option is preferred.
  — Every step up in complexity raises cost, latency, and unpredictability ([O1](failure-modes.en.md#o1-over-agentification); for the decision tree, see the [cheatsheet](cheatsheet.en.md)).
- [ ] 🔴 **P0** Set a maximum step count (max_steps), and give the user a clear, friendly outcome when it's reached.
  — This is the last hard line of defense against infinite loops burning money ([M3](failure-modes.en.md#m3-tool-call-loop)).
- [ ] 🔴 **P0** Identity and permissions propagate along the delegation chain, and a sub-agent's effective permissions never exceed those of the user who made the request.
  — Otherwise delegation becomes a privilege-escalation channel ([S8](failure-modes.en.md#s8-privilege-escalation-via-delegation)).
- [ ] 🟠 **P1** In multi-agent setups, the budget is shared across the entire call tree, and delegation depth is capped.
  — Nested agents' step counts multiply, and when handoffs form a cycle, there's no upper bound ([O4](failure-modes.en.md#o4-unbounded-delegation)).
- [ ] 🟠 **P1** Delegation passes along the full task context: the goal, known facts, constraints, and the expected output format.
  — Sub-agents can't see the supervisor's conversation history ([O2](failure-modes.en.md#o2-delegation-context-starvation)).
- [ ] 🟠 **P1** Parallel branches only read and analyze; writes are executed serially by a single decision-maker.
  — Parallel writes produce conflicting implicit decisions ([O3](failure-modes.en.md#o3-conflicting-parallel-decisions)).
- [ ] 🟠 **P1** Critical outputs go through a verification step, preferably a deterministic check (tests, validation, reconciliation).
  — Output that nobody verifies comes with no quality guarantee ([O5](failure-modes.en.md#o5-missing-verification)).
- [ ] 🟢 **P2** Generate-and-review loops have a round limit; beyond it, hand off to a human instead of shipping whatever you've got.
  — When the reviewer never passes the output, the loop keeps burning money ([O5](failure-modes.en.md#o5-missing-verification)).

## 6. Reliability

- [ ] 🔴 **P0** Model calls are retried, but only for retryable errors (429, 5xx, timeouts), with exponential backoff + jitter.
  — Rate limiting and flakiness are routine for model APIs, but retrying a 400 or 401 just wastes time and money ([R2](failure-modes.en.md#r2-retrying-non-retryable-errors)).
- [ ] 🔴 **P0** Retries happen at exactly one layer (disable the SDK's built-in retries or route everything through one place), and every retry is logged.
  — Stacked retry layers multiply the impact of an outage ([R1](failure-modes.en.md#r1-retry-storm)).
- [ ] 🔴 **P0** Run state is checkpointed at every step (in durable storage) and can be recovered after a crash or a deploy.
  — Long tasks and tasks waiting for approval must not all be lost because of a single restart ([R4](failure-modes.en.md#r4-lost-progress)).
- [ ] 🟠 **P1** A circuit breaker fails fast when a downstream dependency keeps failing.
  — Making users wait out the full timeout before failing wastes resources and keeps the downstream from recovering ([R1](failure-modes.en.md#r1-retry-storm)).
- [ ] 🟠 **P1** There is a fallback plan (backup model / cache / rule-based fallback / human handoff), and every fallback path passes the same eval suite.
  — An unevaluated fallback just trades "errors" for "silently wrong answers" ([R3](failure-modes.en.md#r3-silent-degradation)).
- [ ] 🟠 **P1** Checkpoints are written atomically and record the versions of the code, prompts, and toolset.
  — A half-written checkpoint can't be restored, and resuming on a mismatched version produces "tool not found" ([R6](failure-modes.en.md#r6-version-skew-on-resume)).
- [ ] 🟠 **P1** Every abnormal ending (step limit, budget exceeded, blocked, model unavailable) resolves to a clear status and a user-friendly message.
  — Users should never see a 500 or a stack trace, and callers need the status to decide what to do next (agentkit's `RunResult.status`).
- [ ] 🟠 **P1** Paused runs (e.g., waiting for approval) have timeouts and expiry handling.
  — Otherwise you pile up runs that will never finish ([R5](failure-modes.en.md#r5-approval-limbo)).
- [ ] 🟠 **P1** Background runs can be canceled when the client disconnects or the user cancels.
  — The user has left, yet the agent keeps spending money and taking actions.
- [ ] 🟢 **P2** Run failure drills regularly: simulate rate limiting, timeouts, downstream outages, and killed processes.
  — Until you drill retries, circuit breakers, and recovery, you don't know whether they actually work.

## 7. Security

- [ ] 🔴 **P0** Complete a threat model that lists **every untrusted source** the agent reads (user input, web pages, emails, documents, tickets, third-party APIs, memory).
  — These sources are the entry points for indirect injection, and you can't defend what you haven't listed ([S2](failure-modes.en.md#s2-indirect-prompt-injection)).
- [ ] 🔴 **P0** Check for the "lethal trifecta": does any single agent have access to private data, exposure to untrusted content, and the ability to communicate externally all at once? If so, break at least one leg by design.
  — With all three present, data exfiltration is just one successful injection away ([S3](failure-modes.en.md#s3-lethal-trifecta-exfiltration)).
- [ ] 🔴 **P0** After reading untrusted content, actions with side effects require human confirmation or are tightly restricted.
  — Detection will miss things; limiting what the agent can do once it's been fooled is the real floor ([S2](failure-modes.en.md#s2-indirect-prompt-injection)).
- [ ] 🔴 **P0** The frontend doesn't render arbitrary external images or links from model output, or allows only allowlisted domains.
  — Markdown images are one of the most common zero-click exfiltration channels ([S3](failure-modes.en.md#s3-lethal-trifecta-exfiltration)).
- [ ] 🔴 **P0** Final output is scanned for secrets and sensitive information.
  — The model may repeat secrets or personal information from its context verbatim ([S7](failure-modes.en.md#s7-sensitive-information-disclosure)).
- [ ] 🔴 **P0** Secrets live only in a secrets manager or environment variables, never in code, prompts, tool return values, or the context.
  — Anything that enters the context can end up in outputs, logs, and traces ([S7](failure-modes.en.md#s7-sensitive-information-disclosure)).
- [ ] 🟠 **P1** The input layer has injection-pattern detection and a length limit.
  — It's the cheapest first line of defense and stops plenty of low-effort attacks, but know that it will miss things ([S1](failure-modes.en.md#s1-direct-prompt-injection)).
- [ ] 🟠 **P1** Tool output is marked as untrusted data (e.g., wrapped in isolation tags), with protection against tag escape.
  — It helps the model tell "data" from "instructions" (spotlighting), but it lowers the probability of an attack rather than eliminating the risk ([S2](failure-modes.en.md#s2-indirect-prompt-injection)).
- [ ] 🟠 **P1** A red-team test set (direct injection, indirect injection, privilege escalation, data exfiltration) runs continuously in CI.
  — Security protections regress as prompts and models change, so they need continuous verification.
- [ ] 🟠 **P1** A kill switch can disable a specific tool, or the entire agent, globally within minutes.
  — When you discover a tool vulnerability or an attack in progress, you need to stop the bleeding immediately (agentkit `PermissionPolicy(deny_tools=...)`).
- [ ] 🟢 **P2** Check item by item against the OWASP Top 10 for LLM Applications 2025 and the Top 10 for Agentic Applications (2026).
  — These industry-consensus risk lists make a useful complementary lens for security reviews.

## 8. Privacy and Compliance

- [ ] 🔴 **P0** Draw a data-flow diagram: which personal or sensitive data the agent touches, which systems it flows to (including model vendors), and for what purpose.
  — If you don't know where the data goes, you can't protect it, and you can't answer a compliance review.
- [ ] 🔴 **P0** Confirm the model vendor's data terms: the retention period, whether data is used for training, and where data is stored.
  — Sending user data to a third-party model is itself a data processing/sharing activity and may involve cross-border data transfer.
- [ ] 🔴 **P0** Personal information in logs, traces, audit records, and eval datasets is redacted or under strict access control.
  — These places usually have looser access control than the production database, yet they hold the same data ([S7](failure-modes.en.md#s7-sensitive-information-disclosure)).
- [ ] 🟠 **P1** Every store (conversation history, memory, checkpoints, logs, traces) has a retention period and automatic cleanup.
  — The longer data is kept, the larger the exposure, and most privacy laws require data minimization.
- [ ] 🟠 **P1** Users' deletion and access requests reach memory, conversation history, checkpoints, and logs.
  — Laws such as GDPR and PIPL (China's Personal Information Protection Law) give individuals a right to deletion, and an agent system's data is scattered across many places.
- [ ] 🟠 **P1** For external users, clearly disclose that they are interacting with AI.
  — This is the foundation of user trust, and some regulations (e.g., Article 50 of the EU AI Act) explicitly require it.
- [ ] 🟠 **P1** Commitment-type outputs (prices, refunds, compensation, policy interpretations) must be backed by tool results; otherwise, they go to a human for confirmation.
  — Companies can be held liable for what their agents say (*Moffatt v. Air Canada*, [M5](failure-modes.en.md#m5-policy-hallucination)).
- [ ] 🟢 **P2** Cross-border data transfers meet local regulatory requirements (e.g., in-country deployment or a dedicated compliance assessment where required).
  — Get legal sign-off, especially when choosing a model service hosted in another country.
- [ ] 🟢 **P2** Legal/compliance has reviewed the agent's liability boundaries, disclaimers, and human review process.
  — Beyond technical guardrails, you also need a backstop at the process and legal level.

## 9. Permissions and Approval

- [ ] 🔴 **P0** Enforce least privilege by role: tools a role isn't allowed to use are **hidden from the model** and **blocked at execution time**.
  — One layer isn't enough: hiding alone can be bypassed by guessing the tool's name and calling it directly, and blocking alone makes the model keep trying ([S5](failure-modes.en.md#s5-excessive-agency)).
- [ ] 🔴 **P0** High-risk (dangerous) actions require human approval, and the risk level is declared in the tool definition, not judged by the model.
  — Irreversible actions can't be left entirely to a model that can be fooled ([S5](failure-modes.en.md#s5-excessive-agency)).
- [ ] 🔴 **P0** The approval flow is asynchronous (pause → persist state → notify the approver → resume after approval), not a thread blocked while it waits.
  — The approver may not see the request for an hour; blocking exhausts resources, and the wait is lost on restart ([R5](failure-modes.en.md#r5-approval-limbo)).
- [ ] 🔴 **P0** Approval decisions (approver identity, time, decision, reason) are recorded in full in the audit log.
  — After an incident, the first question is always "Who approved this?" ([P5](failure-modes.en.md#p5-broken-audit-trail)).
- [ ] 🟠 **P1** The approval UI shows a human-readable summary of the action and its impact, not raw JSON.
  — Approval requests nobody can read get rubber-stamped, and the approval step becomes meaningless.
- [ ] 🟠 **P1** After approval and before execution, re-validate the preconditions (does the resource still exist, are the permissions still valid, has the state changed).
  — Between the pause and the approval, the world may have changed ([R5](failure-modes.en.md#r5-approval-limbo)).
- [ ] 🟠 **P1** The approver of a high-risk action can't be the person who requested it (separation of duties).
  — Otherwise approval is just one more button for the attacker to click.
- [ ] 🟢 **P2** Monitor approval rates and approval times, and watch out for rubber-stamping.
  — An approval rate near 100% with approvals taking only seconds means approvers have stopped reading; cut unnecessary approvals or improve the information shown to approvers.

## 10. Observability

- [ ] 🔴 **P0** Every run has a complete trace: every model call (model, tokens, latency, finish reason) and every tool call (arguments, result, latency, error type).
  — Agents are nondeterministic, so without traces you can't reproduce or debug anything ([P2](failure-modes.en.md#p2-unreproducible-incident)).
- [ ] 🔴 **P0** Run status and stop reason (e.g., completed / max_steps / stopped / failed / paused) are reported as first-class metrics, with dashboards.
  — Most agent failures look like normal responses, so the HTTP success rate tells you very little ([P1](failure-modes.en.md#p1-silent-failure)).
- [ ] 🟠 **P1** Trace fields follow common conventions (e.g., the OpenTelemetry GenAI semantic conventions).
  — This makes it easy to switch observability backends and to integrate with your existing APM stack.
- [ ] 🟠 **P1** The trace ID is returned to the frontend and the support system, so a complaint can be located in one click.
  — It turns "the user says it answered wrong" into "open this trace and see what it saw" ([P2](failure-modes.en.md#p2-unreproducible-incident)).
- [ ] 🟠 **P1** Key signals have alerts: error rate, max_steps rate, cost spikes, fallback events, injection-detection hits, and approval backlog.
  — These are all early warning signs of an incident.
- [ ] 🟠 **P1** Sensitive data in traces and logs is redacted, and large arguments are truncated.
  — Observability data is a hotspot for sensitive data leaks ([S7](failure-modes.en.md#s7-sensitive-information-disclosure)).
- [ ] 🟠 **P1** Every run records the model version actually used in the response, the prompt version, and the toolset version.
  — When behavior changes, the first question is "What changed?" ([E5](failure-modes.en.md#e5-silent-model-drift)).
- [ ] 🟢 **P2** There's a business-level dashboard: task completion rate, human handoff rate, user feedback, repeat-question rate.
  — Healthy technical metrics don't mean satisfied users ([P1](failure-modes.en.md#p1-silent-failure)).
- [ ] 🟢 **P2** Define an explicit sampling policy: debug traces can be sampled; audit logs cannot.
  — They serve different purposes: one is for debugging, the other for compliance traceability.

## 11. Evals

- [ ] 🔴 **P0** An eval set covers the main intents, edge cases (missing information, no answer available), malicious input, and high-risk action scenarios.
  — Without evals, developing an agent means tweaking prompts by feel.
- [ ] 🔴 **P0** Evals are wired into CI: a pass rate below the threshold, or any regression (previously passing, now failing), blocks the merge or release.
  — Fix one thing, break three: that's the norm in prompt engineering ([E4](failure-modes.en.md#e4-prompt-regression)).
- [ ] 🟠 **P1** The eval set is continuously replenished with production bad cases (negative ratings, human handoffs, failed runs).
  — The questions developers imagine and the questions real users ask follow different distributions ([E1](failure-modes.en.md#e1-eval-production-skew)).
- [ ] 🟠 **P1** Evaluate both the final result and the execution trajectory (required / forbidden tool calls, call order).
  — Looking only at results misses "right answer, but it took a dangerous action along the way" and "says it did something it didn't do" ([M1](failure-modes.en.md#m1-phantom-action)).
- [ ] 🟠 **P1** Run each case multiple times and track both pass@k and pass^k.
  — A single passing run can be luck ([E2](failure-modes.en.md#e2-flaky-single-run-evals)).
- [ ] 🟠 **P1** LLM judges use concrete rubrics, run on a different model from the one under test, and are calibrated regularly against human labels.
  — LLM judges have position, verbosity, and self-preference biases ([E3](failure-modes.en.md#e3-llm-as-judge-bias)).
- [ ] 🟠 **P1** Changing the model, prompts, or tool descriptions, or upgrading dependencies, triggers the full eval suite.
  — Each of these changes affects behavior globally.
- [ ] 🟠 **P1** Evals cover every model in the fallback chain.
  — It's a disaster if the backup model gets "tested" for the first time on the day you actually fail over to it ([R3](failure-modes.en.md#r3-silent-degradation)).
- [ ] 🟢 **P2** Eval reports show accuracy, cost, latency, and step count side by side.
  — A 2% accuracy gain that doubles cost isn't necessarily worth it.
- [ ] 🟢 **P2** After launch, there are online evals: sampled scoring, user feedback, A/B tests.
  — Offline evals are only ever an approximation of production.

## 12. Cost

- [ ] 🔴 **P0** Every run has multi-dimensional budgets: steps, tokens, dollars, tool calls, and wall-clock time.
  — Capping only one dimension always leaves a gap ([B1](failure-modes.en.md#b1-runaway-cost)).
- [ ] 🔴 **P0** There are quotas per user, per tenant, and per day.
  — Per-run caps can't stop a flood of legitimate requests or malicious traffic ([B1](failure-modes.en.md#b1-runaway-cost), [P3](failure-modes.en.md#p3-noisy-neighbor)).
- [ ] 🟠 **P1** Cost can be attributed by tenant, feature, model, and prompt version.
  — If you can't see where the money goes, you can't optimize it or price the product ([B3](failure-modes.en.md#b3-unattributable-cost)).
- [ ] 🟠 **P1** Monitor the prompt cache hit rate.
  — Caching can cut cost and latency significantly, but it's easy to bust by accident ([B2](failure-modes.en.md#b2-prompt-cache-busting)).
- [ ] 🟠 **P1** Choose models per subtask, using small models for simple tasks, backed by eval data.
  — Running intent classification on the most expensive model is a common waste ([B4](failure-modes.en.md#b4-model-over-provisioning)).
- [ ] 🟢 **P2** Build a cost forecast (per-run cost distribution × expected traffic) and weigh it against the business value.
  — Know before launch whether this agent makes economic sense.
- [ ] 🟢 **P2** Alert on cost anomalies (hour-over-hour changes, per-tenant spikes).
  — Runaway costs can do real damage within hours.
- [ ] 🟠 **P1** When using caches, distinguish the three kinds (exact caching, semantic caching, and prompt caching) and evaluate them separately; semantic caching has a correctness eval (the share of hits that return a wrong answer).
  — The three have completely different benefits and risks, and a semantic cache's "similar but not identical" hit can serve an outright wrong answer ([D5](failure-modes.en.md#d5-cross-tenant-cache-leak)).
- [ ] 🟠 **P1** Hedged requests are used only for read-only, idempotent calls, with a trigger threshold and a cancellation mechanism.
  — Hedging duplicates calls: on writes, that means duplicate side effects; used too aggressively, it doubles your cost ([D10](failure-modes.en.md#d10-hedging-side-effects)).
- [ ] 🟢 **P2** The escalation conditions for model cascading/routing (small model first, escalating to a large model when needed) are backed by eval data.
  — Get the escalation conditions wrong, and either quality drops or you don't save any money ([B4](failure-modes.en.md#b4-model-over-provisioning)).

## 13. Deployment and Operations

- [ ] 🔴 **P0** Support progressive rollout (by percentage / by tenant) and fast rollback.
  — Changes in agent behavior are hard to predict fully in an offline environment.
- [ ] 🔴 **P0** Changes to prompts, model versions, and tool definitions are treated like code changes: versioned, reviewed, and reversible.
  — Plenty of production incidents start with "I only changed one line of the prompt."
- [ ] 🟠 **P1** Long-running tasks have a dedicated release strategy (old and new versions side by side / runs pinned to the version they started on / graceful draining).
  — Agents that are running during a deploy must not be interrupted or resumed on a mismatched version ([R6](failure-modes.en.md#r6-version-skew-on-resume)).
- [ ] 🟠 **P1** A runbook covers model service outages, cost explosions, injection attacks, data leaks, and rolling back erroneous actions.
  — The on-call engineer at 3 a.m. needs steps, not an architecture diagram.
- [ ] 🟠 **P1** Model access goes through a unified gateway that centralizes rate limiting, metering, routing, and key management.
  — Model calls scattered across services can't be governed consistently ([P3](failure-modes.en.md#p3-noisy-neighbor)).
- [ ] 🟠 **P1** Long tasks use streaming output or async notifications to avoid frontend and gateway timeouts.
  — Agents' tail latency is far longer than that of ordinary APIs ([P4](failure-modes.en.md#p4-tail-latency-blowup)).
- [ ] 🟢 **P2** Regularly clean up orphaned runs, expired checkpoints, and expired approvals.
  — Otherwise storage keeps growing and data outlives its retention period ([R5](failure-modes.en.md#r5-approval-limbo)).
- [ ] 🟢 **P2** Monitor the health of external dependencies (model vendor status, MCP servers, downstream APIs).
  — Many "agent outages" are really dependency outages, and the sooner you know, the better.
- [ ] 🟠 **P1** Code + prompts + model version + tool schemas + key configuration form **one versioned release unit** that rolls out together and rolls back together.
  — Release them separately and you'll roll them back separately, and a half rollback is no rollback at all ([D11](failure-modes.en.md#d11-incomplete-rollback)).
- [ ] 🟠 **P1** Rollouts and experiments bucket users by hashing a stable identifier (user, tenant, session), and sessions and runs stay locked to one version for their entire lifetime.
  — Random per-request splitting makes a session bounce between old and new versions and makes experiment data untrustworthy ([D6](failure-modes.en.md#d6-unstable-canary-bucketing)).
- [ ] 🟠 **P1** Set metric-based automatic rollback conditions (e.g., task completion rate, error rate, or cost crossing a threshold).
  — By the time a human notices and rolls back manually, a lot of users have usually been affected already.
- [ ] 🟠 **P1** Major incidents get a blameless postmortem, and the bad cases it surfaces go into the eval set (the data flywheel).
  — The same kind of incident shouldn't happen twice, and the eval set is where the lessons get locked in.
- [ ] 🟢 **P2** Be clear about when to use shadow mode, canaries, and A/B tests (shadow mode validates safety and correctness, canaries validate stability, and A/B tests validate business impact).
  — They answer different questions, and mixing them up leads to wrong conclusions.

## 14. Multi-Tenancy

- [ ] 🔴 **P0** The tenant ID comes from the authentication system and flows through every tool call, store, retrieval, and cache key.
  — Any gap is a channel for cross-tenant leaks ([C5](failure-modes.en.md#c5-cross-tenant-memory-leak)).
- [ ] 🔴 **P0** Automated cross-tenant authorization tests exist (e.g., plant a unique "canary" string in each tenant's data and verify that no other tenant can ever retrieve it).
  — Isolation has to be proven by tests, not by a code review that "looks fine."
- [ ] 🟠 **P1** Per-tenant rate limits and quotas, with separate queues for interactive and batch requests.
  — Keeps one tenant from dragging down all the others ([P3](failure-modes.en.md#p3-noisy-neighbor)).
- [ ] 🟠 **P1** Per-tenant configuration (available tools, models, prompt customizations) is controlled and auditable.
  — A tenant's custom prompt is itself an injection entry point.
- [ ] 🟠 **P1** Provide usage and cost reports per tenant.
  — They're the basis for billing, capacity planning, and spotting anomalies ([B3](failure-modes.en.md#b3-unattributable-cost)).
- [ ] 🟢 **P2** Highly sensitive tenants can opt for a dedicated deployment or dedicated data storage.
  — The choice between logical and physical isolation depends on the customer's compliance requirements.
- [ ] 🟢 **P2** When a tenant offboards, all of its data (memory, history, checkpoints, logs, eval data) can be deleted completely.
  — It's a contractual and regulatory requirement, and very hard to retrofit.

## 15. Documentation and Handoff

- [ ] 🔴 **P0** Architecture and data-flow diagrams exist and mark the trust boundaries (which data comes from trusted sources and which doesn't).
  — Security reviews and incident investigations both start from this diagram.
- [ ] 🟠 **P1** Every tool has a clear owner and documented downstream dependencies.
  — When a tool breaks, you need to know whom to call; when a downstream API changes, you need to know which agents are affected.
- [ ] 🟠 **P1** Record key design decisions and their rationale in ADRs (architecture decision records): why an agent, why this model, why permissions are split this way.
  — Six months from now, nobody will remember why things were done this way, and the constraints may have changed.
- [ ] 🟠 **P1** On-call staff are trained: they can read traces and handle common incidents by following the runbook.
  — A system that only its designers can maintain can't scale.
- [ ] 🟠 **P1** The eval set's structure, labeling guidelines, and contribution process are documented, so new team members can add cases on their own.
  — The eval set needs ongoing maintenance and can't depend on any one person.
- [ ] 🟢 **P2** Maintain a list of "known limitations and failure modes" (you can tailor one from this repo's [failure modes guide](failure-modes.en.md)).
  — It gives product, support, and users accurate expectations about what the agent can and can't do.
- [ ] 🟢 **P2** User-facing documentation explains what the agent can do, what it can't, how to reach a human, and how data is used.
  — Accurate expectations prevent a lot of complaints and misuse.

## 16. Distributed Systems and Concurrency

- [ ] 🔴 **P0** Workers are stateless: run state, session history, and checkpoints all live in shared storage.
  — This is the prerequisite for horizontal scaling and failover; if state stays inside the process, both scaling out and restarts cause problems.
- [ ] 🔴 **P0** Concurrent writes to the same session have an explicit control scheme: per-session serialization through partitioning, optimistic concurrency (version-number CAS), or locks with fencing tokens.
  — A user sending several messages in a row, or an approval arriving at the same time as a new message, both trigger concurrent writes ([D1](failure-modes.en.md#d1-lost-update)).
- [ ] 🔴 **P0** Message delivery semantics are explicit (usually at-least-once), and consumers are idempotent by message ID.
  — Duplicate delivery is the norm, not the exception ([D3](failure-modes.en.md#d3-duplicate-delivery)).
- [ ] 🔴 **P0** Calls to the model provider are rate-limited **globally** (across all instances), with weighted fair allocation across tenants.
  — Per-instance rate limiting breaks down once you scale out ([D9](failure-modes.en.md#d9-local-only-rate-limiting)), and without fair allocation you get noisy neighbors ([P3](failure-modes.en.md#p3-noisy-neighbor)).
- [ ] 🟠 **P1** Task leases come with heartbeat renewal and fencing tokens, and the heartbeat interval is well below the lease duration.
  — Otherwise a GC pause or network partition leaves two workers processing the same task ([D2](failure-modes.en.md#d2-zombie-worker)).
- [ ] 🟠 **P1** Backpressure and admission control are in place: reject new requests when the queue exceeds a threshold; give messages deadlines; monitor the age of the oldest message.
  — Keeps the system from grinding through old requests nobody is waiting for anymore after recovering from an outage ([D4](failure-modes.en.md#d4-queue-backlog-avalanche)).
- [ ] 🟠 **P1** Configure a dead-letter queue, with alerts and regular processing for messages that land in it.
  — Redelivering poison messages forever drags consumers down, and silently dropping them loses requests.
- [ ] 🟠 **P1** "Write to the database + publish an event" uses a transactional outbox rather than two separate writes.
  — A crash between two independent writes leaves them inconsistent ([D7](failure-modes.en.md#d7-dual-write-inconsistency)).
- [ ] 🟠 **P1** Coalesce identical requests (singleflight), and add random jitter to cache expiry times.
  — Prevents a request storm the moment a cache entry expires ([D8](failure-modes.en.md#d8-cache-stampede)).
- [ ] 🟠 **P1** Choose the delivery mechanism by task duration (synchronous / SSE streaming / async queue + notification / workflow engine), and keep timeouts consistent across layers.
  — A backend that keeps running after the frontend has timed out is both waste and a hazard ([P4](failure-modes.en.md#p4-tail-latency-blowup)).
- [ ] 🟢 **P2** Long flows that span multiple services use Saga compensation or a workflow engine instead of letting the model coordinate them.
  — Models don't roll back reliably ([T7](failure-modes.en.md#t7-partial-completion)).
- [ ] 🟢 **P2** Load tests and failure drills cover concurrent messages in one session, a worker killed mid-execution, queue backlogs, and model provider rate limiting.
  — These scenarios never come up in a single-machine development environment.

## 17. Enterprise Knowledge and RAG

- [ ] 🔴 **P0** Retrieval applies **ACL pre-filtering** based on the trusted identity, instead of retrieving first and asking the model to "keep it confidential."
  — Anything that enters the context can end up in the answer ([C7](failure-modes.en.md#c7-post-filter-acl-leak)).
- [ ] 🔴 **P0** User identity propagates all the way to the retrieval service, and multi-tenant indexes are isolated at the storage layer.
  — A retrieval service that queries with a "super account" bypasses every permission ([C5](failure-modes.en.md#c5-cross-tenant-memory-leak)).
- [ ] 🟠 **P1** Update and delete events from source systems are synced to indexes, caches, and summaries, with regular reconciliation.
  — Otherwise retired policies and deleted documents keep getting cited ([C8](failure-modes.en.md#c8-deletion-not-propagated)).
- [ ] 🟠 **P1** Index entries carry a source ID, version, ACL, and expiry time.
  — They're the basis for incremental sync, per-source invalidation, and deletion propagation ([C8](failure-modes.en.md#c8-deletion-not-propagated)).
- [ ] 🟠 **P1** Citations are verified: they can come only from this run's retrieval results, and the cited passage must actually support the statement.
  — A wrong answer with a fake source is more dangerous than one with no source at all ([C9](failure-modes.en.md#c9-citation-hallucination)).
- [ ] 🟠 **P1** Retrieved document content is treated as untrusted data.
  — Anyone who can write a document can plant an injection in the knowledge base ([S2](failure-modes.en.md#s2-indirect-prompt-injection)).
- [ ] 🟠 **P1** Evaluate retrieval quality (recall, ranking) and answer quality (groundedness, correctness) separately.
  — Looking only at the final answer can't tell you whether the problem lies in retrieval or in generation.
- [ ] 🟢 **P2** The chunking strategy follows document structure (keeps the heading hierarchy; doesn't split clauses or tables) and is validated with evals.
  — Chunking determines whether the context the model sees is complete ([C9](failure-modes.en.md#c9-citation-hallucination)).
- [ ] 🟢 **P2** Monitor knowledge freshness: the share of stale documents in retrieval results.
  — Knowledge bases decay over time, and without a metric, nobody notices ([C8](failure-modes.en.md#c8-deletion-not-propagated)).

---

## Appendix: The Minimum P0 Set for Launch (Your Floor When Time Is Tight)

If you can only do 10 things, these 10 are the floor (for the full "10 pre-launch questions," see the [cheatsheet](cheatsheet.en.md)):

| # | Check | Related failure modes |
|---|---|---|
| 1 | Identity is injected from the authenticated context, never filled in by the model | [S4](failure-modes.en.md#s4-confused-deputy) |
| 2 | High-risk tools require human approval; least privilege by role | [S5](failure-modes.en.md#s5-excessive-agency) |
| 3 | No single agent has the full "lethal trifecta," or one leg has been cut | [S3](failure-modes.en.md#s3-lethal-trifecta-exfiltration) |
| 4 | Write tools are idempotent | [T5](failure-modes.en.md#t5-duplicate-side-effects) |
| 5 | max_steps + token/dollar budgets + tenant quotas | [M3](failure-modes.en.md#m3-tool-call-loop), [B1](failure-modes.en.md#b1-runaway-cost) |
| 6 | Tool timeouts + output truncation | [T4](failure-modes.en.md#t4-hanging-tool), [T3](failure-modes.en.md#t3-tool-output-explosion) |
| 7 | Data is isolated by tenant at the storage layer | [C5](failure-modes.en.md#c5-cross-tenant-memory-leak) |
| 8 | Complete traces + run status metrics | [P1](failure-modes.en.md#p1-silent-failure), [P2](failure-modes.en.md#p2-unreproducible-incident) |
| 9 | Eval set + CI regression gate | [E4](failure-modes.en.md#e4-prompt-regression) |
| 10 | Fallback path (human handoff) + kill switch | [M5](failure-modes.en.md#m5-policy-hallucination), [S5](failure-modes.en.md#s5-excessive-agency) |

In the [capstone project](../capstone/README.en.md) (ITBuddy), you can use this checklist as your acceptance criteria and verify it item by item.
