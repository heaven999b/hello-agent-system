[中文](quiz.md) | [English](quiz.en.md)

# Lesson 00 quiz

13 questions, about 7 minutes. Answer on your own first, then expand the answers to check. For any you get wrong, go back and reread the relevant section of the [README](README.en.md).

---

### 1. (Single choice) Which option best describes the difference between a workflow and an agent?

- A. An agent uses a bigger, stronger model
- B. An agent can call tools; a workflow can't
- C. A workflow's flow is predefined in code; an agent's flow is decided by the model at runtime
- D. An agent must involve multiple models working together

<details>
<summary>Answer</summary>

**C.** The difference is who decides the control flow. B is wrong: workflows can call tools too — it's just that "when to call which tool" is hard-coded. A and D have nothing to do with the definition. (README 1.1, 1.2)
</details>

---

### 2. (Single choice) Which requirement is the **worst** fit for an agent?

- A. Troubleshooting network issues that differ from user to user
- B. Calculating employees' personal income tax each month using a fixed formula
- C. Finding related tickets and documents across multiple systems based on a user's description
- D. Researching an unfamiliar technical field and writing a summary

<details>
<summary>Answer</summary>

**B.** Income tax calculation is deterministic: the steps are fixed, the result must be exact, and code can implement it 100% correctly. Having a model do it only adds uncertainty and cost. In A, C, and D, the number of steps can't be known in advance — classic agent territory. (README 1.3)
</details>

---

### 3. (Multiple choice) Which of these capabilities must an enterprise agent have, yet demos often lack?

- A. Step and budget limits
- B. A longer, more detailed system prompt
- C. Human approval for high-risk actions
- D. Audit logs
- E. Checkpoints and resume-from-checkpoint

<details>
<summary>Answer</summary>

**A, C, D, E.** B isn't a "capability": no matter how long the prompt is, it can't replace limits, permissions, and records enforced in code. Above all, remember: **a prompt is not a security boundary.** (README 1.4 and section 6)
</details>

---

### 4. (Calculation) If each step of an agent is correct with 90% probability, roughly what is the probability that an 8-step task is fully correct? What does that mean for design?

<details>
<summary>Answer</summary>

0.9⁸ ≈ **43%**. Errors compound. Design implications:

- Cut unnecessary steps (design good tools; turn whatever you can into a workflow);
- Give every step validation and a chance to self-correct (errors as observations, Lesson 03);
- Add human confirmation to critical steps;
- Use evals to measure **end-to-end** success, not just per-step quality (Lesson 11).
</details>

---

### 5. (Single choice) In demo scenario 3, `InputGuard` blocked "Ignore all previous instructions…". Which statement is correct?

- A. With `InputGuard` in place, you no longer need access control or human approval
- B. Regex detection will always miss rephrased attacks; the real backstop is least privilege and human approval
- C. `InputGuard` first calls the model to decide whether the input is an injection
- D. A blocked request still incurs model-call costs

<details>
<summary>Answer</summary>

**B.** Input screening is cheap and stops obvious attacks, but it will always miss some. The real backstop is that even if the model is fooled, it can only access the user's own data (ctx identity), can't see tools it isn't allowed to use (RBAC), and can't perform high-risk actions (human approval). C and D are wrong: in the demo, the blocked request made 0 model calls and cost $0. (README section 3; covered in depth in Lesson 09)
</details>

---

### 6. (Short answer) In demo scenario 2, why doesn't approval work as "the program blocks until the approver clicks," but as "pause → write a checkpoint → another instance resumes later"?

<details>
<summary>Answer</summary>

- The approver might act in a few minutes, or not until the next day; blocking would tie up a process and its connections for a long time;
- In the meantime, the service may restart, go through a rolling deploy, or scale up or down, and in-memory state would be lost;
- Once the full state (message history, calls awaiting approval, usage) is in a checkpoint, any instance can resume it by `run_id` and continue from where it stopped, instead of asking the model again from scratch (which saves money and keeps the model from making a different decision).

This is the idea behind durable execution, covered in depth in Lesson 08.
</details>

---

### 7. (Single choice) Regarding multi-agent systems, which option is consistent with what Anthropic has shared publicly?

- A. Multi-agent always outperforms single-agent
- B. Multi-agent systems use about 15× the tokens of a regular chat, and are better suited to highly parallelizable tasks with more information than fits in a single context
- C. Multi-agent can significantly reduce costs
- D. Tasks where all agents need to share lots of context are the best fit for multi-agent

<details>
<summary>Answer</summary>

**B.** From Anthropic's [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system): agents use about 4× the tokens of chat, and multi-agent systems about 15×; domains that require sharing the same context, or with many dependencies between agents, aren't a good fit for multi-agent today (so D is exactly backwards). (README 1.2)
</details>

---

### 8. (Short answer) What is the "lethal trifecta"? Which of its elements does ITBuddy in the demo have?

<details>
<summary>Answer</summary>

The three elements: ① access to private data; ② exposure to untrusted content; ③ the ability to communicate externally. When all three are present, an attacker can use injection to make the agent send private data out.

ITBuddy has ① (employee tickets, contact details) and ② (knowledge-base articles can be poisoned, as scenario 1 demonstrates). It currently has no external-communication tools such as "send an email" or "fetch an external URL" — if you ever add one, it must come with human approval and a strict allowlist of destinations. (README section 5)
</details>

---

### 9. (Short answer) In Moffatt v. Air Canada, the airline argued it shouldn't be responsible for its chatbot's misinformation, and the tribunal rejected that argument. What lessons does this hold for enterprise agent design? Give at least 2.

<details>
<summary>Answer</summary>

Key points (any well-reasoned answer counts):

- The agent's answers speak for the company, and the company is accountable for what it says;
- Answers about policies, prices, or legal terms must be grounded in authoritative sources (official policies retrieved via tools), not the model's memory; when unsure, the agent should say so or hand off to a human;
- Maintain an eval set that covers these high-risk questions, with continuous regression testing (Lesson 11);
- Keep complete traces and audit records so that, in a dispute, you can reconstruct what the agent saw and said at the time (Lessons 09, 10).
</details>

---

### 10. (Matching) Match each agentkit component to its layer in the reference architecture:

Components: `ResilientLLM`, `PermissionPolicy`, `SlidingWindow`, `Tracer`, `FileCheckpointer`, `ToolOutputGuard`

Layers: model layer, tool layer (permissions), context and memory layer, cross-cutting (observability), state layer, guardrail layer

<details>
<summary>Answer</summary>

| Component | Layer | Lesson |
|---|---|---|
| `ResilientLLM` | Model layer (retry, circuit breaker, fallback) | 08 |
| `PermissionPolicy` | Tool layer (RBAC + human approval) | 09 |
| `SlidingWindow` | Context and memory layer | 04 |
| `Tracer` | Cross-cutting (observability) | 10 |
| `FileCheckpointer` | State layer | 08 |
| `ToolOutputGuard` | Guardrail layer (tool output isolation) | 09 |

(README 1.5)
</details>

---

### 11. (Single choice) How does Part 2 of the course (08–16) differ from Part 1 (00–07)?

- A. Part 2 has no code, only concepts
- B. Part 2 is organized around real enterprise problems: it compares the trade-offs of several solutions, explains how to choose, and then implements one of them in code
- C. Part 2 presents only a single industry-standard approach
- D. Part 2 is about switching to stronger models

<details>
<summary>Answer</summary>

**B.** Part 1 is "learn how to build": concept → build from scratch → exercise. Part 2 is "learn how to choose": enterprise problems (rate limits, concurrent writes, retrieval that crosses permission boundaries, progressive rollouts…) mostly have no single right answer and require trade-offs among scale, consistency, cost, and team capability. So each lesson is made up of several "problem cards": scenario → compare the options → how to choose → this lesson's implementation. (README 1.6)
</details>

---

### 12. (Short answer) After ITBuddy goes live, the same employee sends it two messages almost simultaneously, one from the web and one from the corporate IM app. Two servers handle the two requests; both read the same conversation history, append to it, and write it back. What goes wrong? What families of solutions can you think of?

<details>
<summary>Answer</summary>

The problem: whichever request writes last overwrites the other (a lost update), so one turn of the conversation disappears from the history. If both turns triggered write operations, those may also be executed twice.

Families of solutions (Lesson 13 compares their costs and where each fits; for now, naming the general directions is enough):

- **Pessimistic locking**: lock the session before processing it, so only one request can handle it at a time;
- **Optimistic concurrency**: write back with a version number; if the version doesn't match, reject the write, re-read, and retry;
- **Partition-based serialization**: route requests by session ID to the same queue partition or the same worker, so they naturally queue up.

None of these is always best: conflict frequency, latency requirements, and your existing infrastructure decide which one to choose — exactly the kind of judgment Part 2 trains.
</details>

---

### 13. (Single choice) Lesson 07 splits the considerations in every engineering dimension into "general checks" and "situational checks". Which statement is correct?

- A. Situational checks are optional extras you can skip when you're short on time
- B. General checks apply to every agent project; situational checks apply only when a specific condition holds, but once it does, they're just as mandatory as the general ones
- C. Only high-traffic consumer products need to look at situational checks
- D. The difference is severity: general checks are all P0, situational checks are all P2

<details>
<summary>Answer</summary>

**B.** "Situational" is about *whether it applies*, not *whether it's optional*. "Every tool has a timeout" and "the model never supplies identity" are general checks, whatever the scenario. "When the agent can send information out, consider data exfiltration" is a situational check — if you ever give ITBuddy a tool that sends email, it gets triggered (think back to the lethal trifecta in question 8). A mistakes "situational" for "optional". C is wrong: every scenario, from internal tools to offline batch jobs, triggers its own set of checks. D is wrong: every item carries its own P0 / P1 / P2 severity, and both kinds include all three levels. ([Lesson 07 §1.3](../07_engineering_perspectives/README.en.md#13-general-checks-vs-situational-checks))
</details>

---

Got them all right? Move on to [Lesson 01: LLM essentials for agent developers](../01_llm_essentials/README.en.md).
