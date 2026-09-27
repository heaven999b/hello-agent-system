[中文](reading-list.md) | [English](reading-list.en.md)

# Curated Further Reading

> 📖 Part of the "domain reference handbook". It includes only resources that **actually exist, have working links, and have verified titles** (verified in September 2026).
> Related docs: [Framework comparison](framework-comparison.en.md) · [Failure-mode catalog](failure-modes.en.md) · [Glossary](glossary.en.md)

## How to Read This List

There's a lot here, and you don't need to read all of it. Pick a route based on your role first. In each group's table, the "Order" column gives the suggested reading order (items marked ⭐ are must-reads for that group).

| Route | Who it's for | Suggested reading | Estimated time |
|---|---|---|---|
| **A. One-hour intro** | You just finished Part 1 of the course | A1 → A2 → B1 → G2 | About 1 hour |
| **B. An engineer's week** | Developers taking an agent to production | Route A + C1 → D4 → E1 → E2 → I2 → J1 → J2 → the remaining ⭐ items in each group | 1 hour a day for about a week |
| **C. Security lead** | People who do security reviews and red teaming | All of group I (in order) + C4 + H2 | About half a day |
| **D. Platform engineer / architect** | People who own multi-tenant platforms and infrastructure | Groups E + F + G + L + D4 | About a day |
| **E. Eval lead** | People who own quality and the eval system | All of group J + B4 + D3 | About half a day |

> 💡 How to read a paper: start with the abstract and conclusion, then look at the figures and tables, and only then read the method details. For most papers on this list, you only need to understand what concept it introduced and why that concept matters.

---

## A. The Big Picture: What Agents Are and When to Use Them

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| A1 ⭐ | [Building Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents) | Anthropic (Erik Schluntz, Barry Zhang) · 2024 | The direct source of this course's orchestration patterns (prompt chaining, routing, parallelization, orchestrator-workers, evaluator-optimizer). Its "don't add complexity you don't need" principle and its workflow/agent distinction are the starting point for every design discussion. | [Lesson 00](../lessons/00_overview/README.en.md) · [Lesson 04](../lessons/04_orchestration/README.en.md) |
| A2 ⭐ | [A practical guide to building agents](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) (PDF) | OpenAI · 2025 | Written for product and engineering teams: when to build an agent, single-agent vs. multi-agent designs (the manager and decentralized patterns), layered guardrails, and when to bring in humans. A good complement to A1. | [Lesson 00](../lessons/00_overview/README.en.md) · [Lesson 06](../lessons/06_security/README.en.md) |
| A3 | [LLM Powered Autonomous Agents](https://lilianweng.github.io/posts/2023-06-23-agent/) | Lilian Weng · 2023 | The classic survey that breaks an agent into three components: planning, memory, and tool use. Great for building an overall mental map, and for tracing your way back to the important early papers. | [Lesson 00](../lessons/00_overview/README.en.md) |
| A4 | [Agents](https://huyenchip.com/2025/01/07/agents.html) | Chip Huyen · 2025 | A systematic look at tool selection, planning, and failure modes in agents. Engineering-oriented, with broad coverage. | [Lesson 00](../lessons/00_overview/README.en.md) |
| A5 | [12-Factor Agents](https://github.com/humanlayer/12-factor-agents) | HumanLayer · 2025 | 12 engineering principles modeled on the Twelve-Factor App, such as "Own your context window", "Launch/Pause/Resume with simple APIs", and "Make your agent a stateless reducer". They closely mirror this course's designs for checkpoints, context, and human approval. | [Lesson 05](../lessons/05_reliability/README.en.md) · [Lesson 09](../lessons/09_production_architecture/README.en.md) |

## B. Context Engineering and Memory

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| B1 ⭐ | [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Anthropic · 2025 | Frames context as an "attention budget" and systematically covers compaction, structured note-taking, sub-agent architectures, and just-in-time context loading. The essential follow-up to Lesson 03. | [Lesson 03](../lessons/03_context_memory/README.en.md) |
| B2 ⭐ | [Context Engineering for AI Agents: Lessons from Building Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) | Yichao 'Peak' Ji (Manus) · 2025 | Hard-won lessons from a frontline team: design around the KV cache, mask tools instead of removing them, use the file system as context, and keep errors in the context. Every point comes with actionable detail. | [Lesson 03](../lessons/03_context_memory/README.en.md) · [Lesson 11](../lessons/11_cost_latency/README.en.md) |
| B3 | [How Long Contexts Fail](https://www.dbreunig.com/2025/06/22/how-contexts-fail-and-how-to-fix-them.html) | Drew Breunig · 2025 | Sorts long-context failures into four types: poisoning, distraction, confusion, and clash. The clear names make team discussions easier. | [Lesson 03](../lessons/03_context_memory/README.en.md) |
| B4 | [Context Rot: How Increasing Input Tokens Impacts LLM Performance](https://www.trychroma.com/research/context-rot) | Chroma · 2025 | Shows experimentally that performance degrades as context grows, which gives you data to justify putting a budget on context. | [Lesson 03](../lessons/03_context_memory/README.en.md) |
| B5 | [Lost in the Middle: How Language Models Use Long Contexts](https://arxiv.org/abs/2307.03172) | Nelson F. Liu et al. · 2023 | The classic paper showing that models make the worst use of relevant information placed in the middle of a long context. It's where the advice to put key information at the beginning or end comes from. | [Lesson 03](../lessons/03_context_memory/README.en.md) |
| B6 | [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) | Charles Packer et al. · 2023 | Manages agent memory by borrowing the tiered-memory ideas of operating systems. An important reference for long-term memory design. | [Lesson 03](../lessons/03_context_memory/README.en.md) |
| B7 | [Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442) | Joon Sung Park et al. · 2023 | Records experiences in full as natural language, synthesizes memories into higher-level "reflections", and retrieves them dynamically when needed. This memory architecture inspired many later agent memory systems. | [Lesson 03](../lessons/03_context_memory/README.en.md) |

## C. Tools and Protocols

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| C1 ⭐ | [Writing effective tools for AI agents—using AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents) | Anthropic · 2025 | The most practical piece on tool design: choosing the right tools, namespacing, returning meaningful context, controlling token usage, polishing tool descriptions the way you polish prompts, and using evals to drive tool improvements. The essential follow-up to Lesson 02. | [Lesson 02](../lessons/02_tools/README.en.md) |
| C2 | [The "think" tool: Enabling Claude to stop and think](https://www.anthropic.com/engineering/claude-think-tool) | Anthropic · 2025 | How a tool that "does nothing" improves performance in complex policy-following scenarios. Broadens your sense of how flexible tool design can be. | [Lesson 02](../lessons/02_tools/README.en.md) |
| C3 | [Code execution with MCP: building more efficient AI agents](https://www.anthropic.com/engineering/code-execution-with-mcp) | Anthropic · 2025 | When there are many tools, let the agent write code that calls them, to save context. Another answer to the "tool overload" problem. | [Lesson 02](../lessons/02_tools/README.en.md) · [Lesson 11](../lessons/11_cost_latency/README.en.md) |
| C4 ⭐ | [Model Context Protocol specification](https://modelcontextprotocol.io/specification/latest) | MCP project · current version 2026-07-28 | The authoritative definition of MCP: the host/client/server architecture, the JSON-RPC base protocol, capabilities such as tools, resources, and prompts, and the security principles the spec itself lays out. | [Lesson 02](../lessons/02_tools/README.en.md) |
| C5 | [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) | MCP project | The official MCP security best practices. Read them before connecting any third-party MCP server. | [Lesson 02](../lessons/02_tools/README.en.md) · [Lesson 06](../lessons/06_security/README.en.md) |
| C6 | [A2A Protocol specification](https://a2a-protocol.org/latest/specification/) (see also the [GitHub repository](https://github.com/a2aproject/A2A)) | A2A project · v1.0.0 | The open protocol for agents to talk to each other, with concepts such as Agent Card, Task, Message, and Artifact. Clarifies the division of labor: MCP connects tools, A2A connects agents. | [Lesson 09](../lessons/09_production_architecture/README.en.md) |
| C7 | [Toolformer: Language Models Can Teach Themselves to Use Tools](https://arxiv.org/abs/2302.04761) | Timo Schick et al. · 2023 | Representative early research on tool use. Shows where the "model calls tools" capability came from. | [Lesson 02](../lessons/02_tools/README.en.md) |

## D. Reasoning Patterns, Orchestration, and Multi-Agent Systems

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| D1 ⭐ | [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629) | Shunyu Yao et al. · 2022 | Where the modern agent loop's "think → act → observe" cycle comes from. | [Lesson 01](../lessons/01_agent_loop/README.en.md) |
| D2 | [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366) | Noah Shinn et al. · 2023 | Lets agents learn from failure through reflection expressed in language. Part of the theoretical background for the evaluator-optimizer pattern. | [Lesson 04](../lessons/04_orchestration/README.en.md) |
| D3 | [Self-Consistency Improves Chain of Thought Reasoning in Language Models](https://arxiv.org/abs/2203.11171) | Xuezhi Wang et al. · 2022 | The original paper on sampling multiple times and voting. Corresponds to agentkit's `majority_vote`. | [Lesson 04](../lessons/04_orchestration/README.en.md) |
| D4 ⭐ | [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) | Anthropic · 2025 | First-hand lessons from a production multi-agent system: when multi-agent is a good fit, token costs (agents use about 4× the tokens of a regular chat, multi-agent systems about 15×), prompting principles, evaluation methods, resuming from failures, and rainbow deployments. | [Lesson 04](../lessons/04_orchestration/README.en.md) · [Lesson 05](../lessons/05_reliability/README.en.md) |
| D5 ⭐ | [Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents) | Cognition (Walden Yan) · 2025 | The counterpoint to read alongside D4: share the full context, and remember that actions carry implicit decisions. Explains why multi-agent systems are so error-prone. | [Lesson 04](../lessons/04_orchestration/README.en.md) |
| D6 | [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657) | Mert Cemri et al. · 2025 | A taxonomy of multi-agent failures (MAST) built from a large set of real traces, with three categories: system design, inter-agent misalignment, and task verification. | [Lesson 04](../lessons/04_orchestration/README.en.md) · [Lesson 08](../lessons/08_evals/README.en.md) |
| D7 | [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | Anthropic · 2025 | How long-running agents keep making progress across many context windows: an initializer agent, progress files, feature lists, and incremental commits, plus failure modes such as declaring the job done too early. | [Lesson 03](../lessons/03_context_memory/README.en.md) · [Lesson 05](../lessons/05_reliability/README.en.md) |
| D8 | [Building agents with the Claude Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk) | Anthropic · 2025 | Organizes the agent loop as "gather context → take action → verify work → repeat". Read it alongside the [framework comparison](framework-comparison.en.md). | [Lesson 01](../lessons/01_agent_loop/README.en.md) |

## E. Reliability and Durable Execution

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| E1 ⭐ | [Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) | Marc Brooker (AWS Architecture Blog) · 2015 | Compares several jitter strategies through simulation and concludes that "full jitter" works best. The source of agentkit's `backoff_delay`. | [Lesson 05](../lessons/05_reliability/README.en.md) |
| E2 ⭐ | [Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/) | Google SRE Book | What causes cascading failures and how to prevent them. Its example of retries multiplying across layers (three layers of 4 attempts each → 64 attempts) is the best material there is for understanding retry storms. | [Lesson 05](../lessons/05_reliability/README.en.md) · [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |
| E3 | [Handling Overload](https://sre.google/sre-book/handling-overload/) | Google SRE Book | Handling overload: client-side throttling, per-customer limits, and request criticality. Maps directly to rate limiting and fallback design in a model gateway. | [Lesson 05](../lessons/05_reliability/README.en.md) · [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |
| E4 | [CircuitBreaker](https://martinfowler.com/bliki/CircuitBreaker.html) | Martin Fowler · 2014 | The classic short article on the circuit breaker pattern. Corresponds to agentkit's `CircuitBreaker`. | [Lesson 05](../lessons/05_reliability/README.en.md) |
| E5 ⭐ | [Designing robust and predictable APIs with idempotency](https://stripe.com/blog/idempotency) | Stripe · 2017 | The industry reference for idempotency-key design: why you need it, and what the client and the server each have to do. Helps you understand why the idempotency key must be passed downstream. | [Lesson 05](../lessons/05_reliability/README.en.md) · [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |
| E6 | [The definitive guide to Durable Execution](https://temporal.io/blog/what-is-durable-execution) | Temporal · 2025 | An introduction to durable execution. Explains why checkpoints and event replay matter for long-running agents. | [Lesson 05](../lessons/05_reliability/README.en.md) · [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |

## F. Distributed Systems and High Concurrency

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| F1 ⭐ | [How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) | Martin Kleppmann · 2016 | Uses a GC pause that lets a lease expire to show exactly why you need fencing tokens. Essential reading for the "zombie worker" problem. | [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |
| F2 ⭐ | [Pattern: Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html) | microservices.io (Chris Richardson) | The standard description of the transactional outbox pattern, which solves the dual-write problem of "database written, message never sent". | [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |
| F3 | [Pattern: Saga](https://microservices.io/patterns/data/saga.html) | microservices.io (Chris Richardson) | The compensation pattern for long transactions that span services. A fit for multi-system flows such as onboarding a new employee. | [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |
| F4 | [singleflight package docs](https://pkg.go.dev/golang.org/x/sync/singleflight) | Go project | The classic implementation of request coalescing. The docs are short, and after reading them you'll understand how to prevent cache stampedes. | [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) · [Lesson 11](../lessons/11_cost_latency/README.en.md) |
| F5 | [Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) | MDN | The authoritative SSE reference. SSE is a common way to stream agent progress. | [Lesson 10](../lessons/10_distributed_concurrency/README.en.md) |

## G. Cost and Latency

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| G1 ⭐ | [The Tail at Scale](https://research.google/pubs/the-tail-at-scale/) | Jeffrey Dean, Luiz André Barroso (CACM) · 2013 | The classic paper on tail latency in large-scale systems, and the origin of techniques such as hedged requests. | [Lesson 11](../lessons/11_cost_latency/README.en.md) |
| G2 ⭐ | [Prompt caching (Anthropic docs)](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | Anthropic | The prompt-cache prefix hierarchy (tools → system prompt → messages) and which changes invalidate the cache. Directly shapes how you should structure prompts. | [Lesson 11](../lessons/11_cost_latency/README.en.md) |
| G3 | [Prompt caching (OpenAI docs)](https://developers.openai.com/api/docs/guides/prompt-caching) | OpenAI | Another vendor's implementation. Reading both shows you the shared "exact prefix match" principle and where the vendors differ. | [Lesson 11](../lessons/11_cost_latency/README.en.md) |

## H. Enterprise Knowledge and RAG

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| H1 ⭐ | [Contextual Retrieval in AI Systems](https://www.anthropic.com/engineering/contextual-retrieval) | Anthropic · 2024 | Adds context to each chunk, combines BM25 with vector search, then reranks, with experiments comparing retrieval failure rates. A practical reference for enterprise RAG. | [Lesson 03](../lessons/03_context_memory/README.en.md) · [Lesson 12](../lessons/12_enterprise_rag/README.en.md) |
| H2 | [LLM08:2025 Vector and Embedding Weaknesses](https://genai.owasp.org/llmrisk/llm082025-vector-and-embedding-weaknesses/) | OWASP · 2025 | Security risks around vector stores and embeddings, including information leaking across tenants and permission boundaries. Read it before building permission-aware RAG. | [Lesson 12](../lessons/12_enterprise_rag/README.en.md) · [Lesson 06](../lessons/06_security/README.en.md) |

## I. Security

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| I1 | [Prompt injection attacks against GPT-3](https://simonwillison.net/2022/Sep/12/prompt-injection/) | Simon Willison · 2022 | One of the earliest systematic discussions of prompt injection. Explains where the problem came from and why it's still so hard to fix. | [Lesson 06](../lessons/06_security/README.en.md) |
| I2 ⭐ | [The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) | Simon Willison · 2025 | The "lethal trifecta": private data + untrusted content + external communication. Use it to check every agent tool set you design. | [Lesson 06](../lessons/06_security/README.en.md) |
| I3 ⭐ | [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/) | OWASP · 2025 | The industry-consensus list of the top ten risks for LLM applications (prompt injection, sensitive information disclosure, excessive agency, unbounded consumption, and more). A good checklist for security reviews. | [Lesson 06](../lessons/06_security/README.en.md) |
| I4 ⭐ | [OWASP Top 10 for Agentic Applications for 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/) | OWASP · published December 2025 | The top ten risks specific to agents (goal hijacking, tool misuse, identity and privilege abuse, memory and context poisoning, cascading failures, and more). | [Lesson 06](../lessons/06_security/README.en.md) |
| I5 | [Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection](https://arxiv.org/abs/2302.12173) | Kai Greshake et al. · 2023 | The representative paper on indirect prompt injection. Drives home that the attacker never needs to talk to your agent. | [Lesson 06](../lessons/06_security/README.en.md) |
| I6 | [Defending Against Indirect Prompt Injection Attacks With Spotlighting](https://arxiv.org/abs/2403.14720) | Keegan Hines et al. · 2024 | The original paper on spotlighting (data marking), the idea behind agentkit's `ToolOutputGuard`. | [Lesson 06](../lessons/06_security/README.en.md) |
| I7 ⭐ | [Design Patterns for Securing LLM Agents against Prompt Injections](https://arxiv.org/abs/2506.08837) (see also Simon Willison's [commentary](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)) | Luca Beurer-Kellner et al. · 2025 | Six architectural patterns with provable security properties (Action-Selector, Plan-Then-Execute, LLM Map-Reduce, Dual LLM, Code-Then-Execute, Context-Minimization). They move injection defense from "detection" to "architecture". | [Lesson 06](../lessons/06_security/README.en.md) |
| I8 | [Defeating Prompt Injections by Design](https://arxiv.org/abs/2503.18813) | Edoardo Debenedetti et al. · 2025 | Google DeepMind's CaMeL: explicitly separates control flow from data flow, and uses "capabilities" to constrain where data can flow. | [Lesson 06](../lessons/06_security/README.en.md) |
| I9 | [MCP Security Notification: Tool Poisoning Attacks](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) | Invariant Labs · 2025 | A demonstration of attacks that hide malicious instructions in tool descriptions. Required reading before you connect third-party MCP servers. | [Lesson 02](../lessons/02_tools/README.en.md) · [Lesson 06](../lessons/06_security/README.en.md) |
| I10 | [GitHub MCP Exploited: Accessing private repositories via MCP](https://invariantlabs.ai/blog/mcp-github-vulnerability) | Invariant Labs · 2025 | A real case in which a malicious issue hijacked an agent and leaked private repository data. A textbook example of the lethal trifecta. | [Lesson 06](../lessons/06_security/README.en.md) |
| I11 | [EchoLeak: The First Real-World Zero-Click Prompt Injection Exploit in a Production LLM System](https://arxiv.org/abs/2509.10540) | Pavan Reddy et al. · 2025 | A case study of the zero-click injection vulnerability in Microsoft 365 Copilot (CVE-2025-32711). | [Lesson 06](../lessons/06_security/README.en.md) |
| I12 | [Making Claude Code more secure and autonomous with sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) | Anthropic · 2025 | Engineering practice for using file-system and network isolation to cut down permission prompts while improving security. Useful for thinking through the sandbox-vs.-approval trade-off. | [Lesson 06](../lessons/06_security/README.en.md) |
| I13 | [AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework) | NIST | The AI risk management framework from the U.S. National Institute of Standards and Technology. A common reference when enterprises build out AI governance. | [Lesson 06](../lessons/06_security/README.en.md) · [Lesson 09](../lessons/09_production_architecture/README.en.md) |

## J. Evals

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| J1 ⭐ | [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | Anthropic · 2026 | A systematic guide to agent evals: three kinds of graders, pass@k vs. pass^k, capability vs. regression evals, and starting from 20–50 real failed tasks. The essential follow-up to Lesson 08. | [Lesson 08](../lessons/08_evals/README.en.md) |
| J2 ⭐ | [Your AI Product Needs Evals](https://hamel.dev/blog/posts/evals/) | Hamel Husain · 2024 | Why evals are at the core of iterating on an AI product, and how to build an eval system from scratch. Extremely practical. | [Lesson 08](../lessons/08_evals/README.en.md) |
| J3 | [Using LLM-as-a-Judge For Evaluation: A Complete Guide](https://hamel.dev/blog/posts/llm-judge/) | Hamel Husain · 2024 | How to build an LLM judge whose verdicts agree with domain experts. | [Lesson 08](../lessons/08_evals/README.en.md) |
| J4 | [Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685) | Lianmin Zheng et al. · 2023 | The foundational LLM-judge paper, with a systematic discussion of position bias, verbosity bias, and self-enhancement bias. | [Lesson 08](../lessons/08_evals/README.en.md) |
| J5 ⭐ | [τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045) | Shunyu Yao et al. · 2024 | Evaluates agents with simulated users + domain tools + business policies, and introduces pass^k to measure consistency. Its design carries over directly to evaluating customer-service agents. | [Lesson 08](../lessons/08_evals/README.en.md) |
| J6 | [τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment](https://arxiv.org/abs/2506.07982) | Victor Barres et al. · 2025 | The follow-up to τ-bench: a "dual-control" setting in which both the user and the agent can act on the environment, much closer to real technical-support conversations. | [Lesson 08](../lessons/08_evals/README.en.md) |
| J7 | [SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://arxiv.org/abs/2310.06770) | Carlos E. Jimenez et al. · 2023 | A benchmark that evaluates coding agents on real GitHub issues. Illustrates evaluating by final state (do the tests pass?). | [Lesson 08](../lessons/08_evals/README.en.md) |
| J8 | [AgentBench: Evaluating LLMs as Agents](https://arxiv.org/abs/2308.03688) | Xiao Liu et al. · 2023 | A multi-environment benchmark of agent capabilities. Shows how academia compares models' agent abilities side by side. | [Lesson 08](../lessons/08_evals/README.en.md) |
| J9 | [Patterns for Building LLM-based Systems & Products](https://eugeneyan.com/writing/llm-patterns/) | Eugene Yan · 2023 | A long survey of patterns for evals, RAG, guardrails, caching, user feedback, and more. Works well as a "pattern catalog" for LLM engineering. | [Lesson 08](../lessons/08_evals/README.en.md) · [Lesson 09](../lessons/09_production_architecture/README.en.md) |

## K. Observability

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| K1 ⭐ | [OpenTelemetry GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions-genai) | OpenTelemetry project | Naming conventions for GenAI spans, metrics, and events (agentkit's `gen_ai.*` attributes follow them). Note: these conventions have moved out of the main OpenTelemetry semantic-conventions repository into this separate one. | [Lesson 07](../lessons/07_observability/README.en.md) |
| K2 | [Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/) | Google SRE Book | Monitoring fundamentals: the four golden signals, symptoms vs. causes, and principles of alert design. All of it applies to agents. | [Lesson 07](../lessons/07_observability/README.en.md) |

## L. Release and Operations

| Order | Resource | Author · Year | Why read it | Lessons |
|---|---|---|---|---|
| L1 ⭐ | [Canarying Releases](https://sre.google/workbook/canarying-releases/) | Google SRE Workbook | A systematic approach to canary releases: how to choose metrics, sample sizes, and evaluation windows. Applies directly to prompt and model releases for agents. | [Lesson 13](../lessons/13_release_ops/README.en.md) |
| L2 ⭐ | [Postmortem Culture: Learning from Failure](https://sre.google/sre-book/postmortem-culture/) | Google SRE Book | Blameless postmortem culture and how to write a postmortem. The methodological foundation for turning incidents into improvements. | [Lesson 13](../lessons/13_release_ops/README.en.md) |

## M. Real Incidents and Compliance

| Order | Resource | Source | Why read it | Lessons |
|---|---|---|---|---|
| M1 | [Incident 1152: LLM-Driven Replit Agent Reportedly Executed Unauthorized Destructive Commands During Code Freeze, Leading to Loss of Production Data](https://incidentdatabase.ai/cite/1152/) | AI Incident Database | The summary record of the 2025 incident in which Replit's coding agent deleted a production database during a code freeze: the real cost of excessive agency. The AI Incident Database itself is worth bookmarking; you can search it by keyword for more AI incidents. | [Lesson 06](../lessons/06_security/README.en.md) · [Lesson 13](../lessons/13_release_ops/README.en.md) |
| M2 | [Transparency obligations under Article 50 of the AI Act](https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act) | European Commission | The official FAQ on the transparency obligations in Article 50 of the EU AI Act, including the requirement to tell users they are interacting with AI. Relevant to any agent that serves users in the EU. | [Lesson 09](../lessons/09_production_architecture/README.en.md) |

---

## Selection Criteria

1. **Only verified resources**: every link was actually visited in September 2026 and its title checked against the original. Where a link redirects, we use the final URL.
2. **Quality over quantity**: we exclude anything we can't reliably access or verify (including some frequently cited pages whose original links are dead or require a login).
3. **Primary sources first**: official docs, original papers, and the authors' own writing take priority over secondhand summaries.
4. **Contributions welcome**: if you find a dead link, or a resource worth adding, open an issue or a PR, and include a note on why it's worth reading.
