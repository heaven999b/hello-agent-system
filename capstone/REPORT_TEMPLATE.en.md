[中文](REPORT_TEMPLATE.md) | [English](REPORT_TEMPLATE.en.md)

# Agent project report template

> 📝 **What it's for**: use this template to write the final report for your own agent project (a course project, a capstone, an internal proposal). It complements [DESIGN.en.md](DESIGN.en.md): the design doc answers "what we plan to build and why"; the project report answers "what we built, **how good it is, and how we know**."
>
> 📏 **The evaluation criteria** draw on publicly available course project requirements, plus the enterprise concerns this course cares about: security, cost, and reproducibility. Aim for 6–8 pages of main text; references and appendices don't count.
>
> ✍️ **How to use it**: copy this file into your project repository and fill it in section by section. Every section has three parts: **questions to answer** (what reviewers will ask), **✅ good**, and **❌ bad**. Most examples come from ITBuddy in this repository; compare with the [capstone README](README.en.md) to see how it was done.

**The single most important point**: a project that only shows one successful run won't score well. Reviewers care most about Section 5: evaluating your system on a meaningful set of tasks, comparing it against baselines, running ablations, and analyzing **where and why** it fails.

---

## 0. Abstract

**Questions to answer**: in 4–6 sentences: what problem, what approach, on which eval set, how much better than the baseline, and the biggest limitation.

- ✅ "Enterprise IT help desks field a large volume of repetitive requests that users could resolve themselves. We built ITBuddy, a single-agent system with risk-tiered tools and human approval, which passed all 24 eval cases (10 of them security cases) in each of 3 runs. An ablation study shows that, assuming a fully compromised model, disabling approval lets an indirect injection attack succeed once, and removing argument-level authorization grows the approval queue from 1 request to 6. Main limitations: the eval set is small and written entirely by the developers, and we have not yet compared against a simpler workflow baseline."
- ❌ "We built an intelligent IT assistant with the latest LLM. It works very well and can solve all kinds of IT problems." (No numbers, no baseline, no limitations.)

## 1. Problem definition

**Questions to answer**:

- Who has what problem, in what situation? How is it solved today, and at what cost?
- Why does it need an agent rather than a single call, retrieval, or a fixed workflow? (The complexity ladder in [Lesson 06](../lessons/06_orchestration/README.en.md): why isn't the previous rung enough?)
- What does success look like? Write it down as **measurable** metrics: task success rate, human hand-off rate, cost per task, latency, and any zero-tolerance security requirements.
- Scope and non-goals: say explicitly what you won't do.

- ✅ "An IT help desk serving 3,000 employees gets about 400 requests a day, and password resets, VPN, and printers account for more than half (hypothetical data). Goals: self-service resolution ≥ 40%; 100% pass rate on security cases; cost per request < $0.01. Non-goals: no hardware procurement, and no replacing engineers for diagnosing unknown faults. We chose an agent over a workflow because the troubleshooting path depends on intermediate results: check the outage board first, then decide whether to file a ticket."
- ❌ "IT support is inefficient, so we'll use AI to make it more efficient." (For whom? How inefficient? What counts as success?)

## 2. Related work

**Questions to answer**:

- How do existing products, open-source projects, and papers solve this problem (or a close one)?
- How is your approach different? Which design patterns did you borrow? Why not use an existing solution directly?

- ✅ "Off-the-shelf solutions for enterprise IT are mostly rule-based FAQ bots that can't take actions; general agent frameworks provide orchestration but no built-in tenant isolation or approvals. We follow the 'find the simplest solution' principle from Anthropic's Building Effective Agents, and the idea from Beurer-Kellner et al.'s (2025) security design patterns of constraining which actions are possible once the agent has read untrusted data…"
- ❌ Listing the titles of 10 papers without saying how any of them relate to your project.

## 3. Environment and data

**Questions to answer**:

- **Environment**: where does the agent run: real systems, a sandbox, or a simulated backend? Which tools does it have, and what risk tier is each one? **Which parts are real and which are simulated?** Put it in a table (see how ITBuddy does it in [README Section 2.3](README.en.md#23-whats-real-and-whats-simulated): processes, crashes, and leases are real; the 5 enterprise systems are simulated external services).
- **Where the data comes from and how it was collected**: sampled from production logs (how was it redacted, was it authorized)? Written by experts? Synthesized by a model (which model, what prompt, how was it filtered)? A public benchmark? How many cases from each source?
- **How it's split**: how are the development set and the held-out test set divided? How do you make sure you didn't tune your prompts against the test set?
- **Labeling**: who labeled it, following what guidelines? Did two people label a sample independently so you could measure agreement?
- **Coverage**: how many normal, edge, and attack cases are there? Is that enough to support your conclusions?
- Licensing and privacy of the data.

- ✅ "120 eval cases: 60 sampled from two weeks of production tickets, stratified by category, redacted, and approved by the data owner; 40 written by two IT engineers following a labeling guide to cover edge cases; 20 security cases written from the attack taxonomy in Lesson 09. Split 70/30 into development and held-out sets; the held-out set was run only once, right before the final report. Two annotators agreed on 0.86 of a 30-case sample."
- ❌ "We wrote some test questions." (How many? Written by whom? Covering what? How close to real requests?)

## 4. Methods

**Questions to answer**:

- What does the architecture look like (include a diagram)? What is each component responsible for?
- What are the key design decisions, **why** did you make them, and which alternatives did you reject? (You can cite your ADRs directly.)
- Which models, prompts, and tools did you use? How were the key parameters chosen?
- Approaches you tried that failed. This is often the most valuable part of the report.
- **Deployment shape**: single process, multiple processes on one machine, or multiple machines? Of the concurrency, fault tolerance, and crash takeover you claim, which actually happened, and which test or failure injection proves it? Anything without proof should say "not verified".

- ✅ "The hook order is: input guardrail → budget → argument-level authorization → RBAC and approval → output isolation → audit → output guardrail. Argument-level authorization must come before approval; otherwise requests that are bound to be rejected, like an employee resetting a colleague's password, land in the approval queue and cause approval fatigue. The ablation study confirmed it: without argument-level authorization, the approval queue grew from 1 request to 6. We tried summarization to compress context, but summaries lose the isolation tags around untrusted data, so we chose a sliding window instead (ADR-004)."
- ❌ "We used the ReAct framework and state-of-the-art prompt engineering." (Which decisions? Why?)
- ✅ (deployment shape) "The API process only enqueues and returns 202; the agent runs in 2 worker processes, and all state lives in shared SQLite. The end-to-end tests start real processes: the pause and the resume are handled by workers with different pids; a worker killed with kill -9 after the downstream created the ticket but before the result was recorded is taken over, the new holder replays the call, the downstream deduplicates by Idempotency-Key, and there is exactly 1 ticket. Limitation: every process runs on one machine; multiple machines are not verified."
- ❌ (deployment shape) "The system supports high concurrency and distributed deployment, with comprehensive fault tolerance." (How much concurrency? How many processes? Has anything ever crashed? How do you know?)

## 5. Results

This is the most important section of the report. Baseline comparison, ablation, and failure-mode error analysis are **all required**.

### 5.1 Evaluation setup

**Questions to answer**: on which eval set (Section 3)? With which metrics? How is it graded: code-based graders, an LLM judge, human ratings, or a combination? Was the LLM judge calibrated against human labels? How many times was each configuration run? Which model snapshot, on which date?

- ✅ "Metrics: task success rate (rule-based plus side-effect grading), zero-tolerance pass rate on security cases, cost per task, and P50/P95 latency. Open-ended answers are scored by an LLM judge on a different model from the one under test; it agreed with human scores on 0.82 of 40 samples. Each configuration was run 3 times; we report the mean and pass^3. Model gpt-5.5, 2026-09-27."
- ❌ "We tried a few questions by hand and the results looked good."

### 5.2 Baseline comparison (required)

**Questions to answer**: compared with what? Baselines should be **reasonable**: a simpler approach (a single call plus retrieval, a fixed workflow), the existing system, another model, or a published method. Every approach must be compared on **the same eval set** with the same grading. How much more does your approach cost, how much slower is it, and how much improvement does that buy?

| Approach | Task success rate | Security cases | Cost per task | P95 latency |
|---|---|---|---|---|
| Baseline A: retrieval + single generation (no tools) | | | | |
| Baseline B: fixed workflow (route → retrieve → file ticket) | | | | |
| Your system | | | | |

- ✅ "The retrieval + single-generation baseline matched the agent on knowledge questions (95% vs. 96%) at a quarter of the cost; all of the agent's advantage came from categories that require actions (filing tickets, resetting passwords). So we added a router in front: pure Q&A goes through the workflow."
- ❌ Reporting only your own system's numbers, or comparing against a deliberately weak baseline, such as giving a model no tools for a task that needs them.

### 5.3 Ablation study (required)

**Questions to answer**: remove **one** component at a time, keeping everything else the same: how much do the metrics change? What does each component contribute? Did removing any component make no difference, and if so, is it really redundant, or does the eval set just not exercise it? Which components' effects can't your method measure, and why?

- ✅ ITBuddy's ablation ([README §12.1](README.en.md#121-ablation-study-what-each-defense-actually-stops)): switch off each of 6 components in turn and compare three metrics on the 10 security cases: eval passes, attacks that got through, and requests sent to approval. Offline mode assumes a fully compromised model to test the deterministic defenses; real mode shows behavior with the real model; and the report says plainly that "ToolOutputGuard's effect didn't show up in either mode."
- ❌ Changing three things at once and reporting "a 10% improvement"; or reporting only the overall pass rate, so nobody can tell which component made the difference.

### 5.4 Failure-mode error analysis (required)

**Questions to answer**: **where and why** does the system fail? Read the failed samples one by one and categorize them (with the MAST taxonomy from [Lesson 06](../lessons/06_orchestration/README.en.md) §5.7, the [failure-mode field guide](../docs/failure-modes.en.md), or your own categories), count each category, find the root causes, and say what you fixed, how much the fix helped, and what's still unfixed.

| Failure category | Count | Representative example (case ID or trace link) | Root cause | Fix and effect |
|---|---|---|---|---|
| | | | | |

- ✅ "Of 36 failures: 14 were retrieval missing the right article (keyword mismatch), 9 were premature completion (saying 'submitted' without calling the ticket tool), 7 were the model accepting the user's claims about their own identity, and 6 were judge errors. We added a claim-evidence check for the second category; on retest it dropped to 1."
- ❌ "The model sometimes makes mistakes; a stronger model should fix this in the future."

### 5.5 Cost and latency

**Questions to answer**: what are the average and worst-case cost per task, and the P50/P95 latency? Where does the money go (input, output, reasoning tokens; which step)? At the expected traffic, what does a month cost?

- ✅ "3,046 tokens per task on average, about $0.0056 per task at the sample prices; P95 latency 10.7s. Input tokens dominate, mostly the system prompt and tool definitions, which is why prefix caching is our next step."
- ❌ "It's cheap."

## 6. Discussion

**Questions to answer**: what do the results show, and what **don't** they show? Which situations do the conclusions generalize to, and which not? What's the biggest limitation? If you had one more month, what would you do first?

- ✅ "A 100% pass rate only means the eval set isn't hard enough yet: all 24 cases are single-turn attacks, in Chinese, with known patterns, so the conclusions don't extend to multi-turn social engineering. Next: add 100 cases sampled from production, include multi-turn attacks, and compare against a workflow baseline."
- ❌ "Our system is ready for production."

## 7. Safety and ethical considerations

**Questions to answer**:

- Threat model: which inputs are untrusted? Which actions are dangerous? ([Lesson 09](../lessons/09_security/README.en.md))
- Is the lethal trifecta complete? If so, which leg did you cut?
- Permissions: where does identity come from? Can the model influence authorization decisions? Do high-risk operations require human approval?
- Privacy: what personal information do you process? Where does it flow (model provider, logs, third parties)? Did you apply data minimization and redaction? Do information flows fit their context (contextual integrity)?
- When the system fails or is abused, who is affected, and how? What's the residual risk, and who owns it?
- Red teaming: which attacks did you test? Did the findings make it into the eval set?

- ✅ "The knowledge base (untrusted content) and the employee directory (private data) coexist, but the system has no tool that sends anything outward, and reset links go only to the employee's own corporate mailbox (out of band), which cuts the 'external communication' leg. Residual risk: a poisoned article could still lead the model to give wrong steps in its answer; we plan to mitigate this by labeling source trust levels (homework #7)."
- ❌ "We use a safe LLM, so there are no security issues."

## 8. Authorship and AI-use statement

**Questions to answer**:

- What exactly did each member do (design, code, data, evaluation, writing)? Name specific modules and sections.
- Which parts of the project used AI tools (ideation, writing code, generating data, running experiments, analyzing results, writing), and what role did they play? What did people verify?

- ✅ "Zhang San: tool layer and permissions (`tools.py`, `policies.py`); Sections 4 and 7. Li Si: eval set and graders (`evals/`, `run_evals.py`); Sections 3 and 5. AI use: a coding assistant generated the test scaffolding, and a person checked every assertion; a model generated 30 synthetic eval cases, two people screened them, and 18 were kept."
- ❌ "All members contributed equally." Or no mention at all of how AI tools were used.

## 9. Reproducibility checklist

Reviewers will follow your README on a clean machine and run at least one task end to end. Check every item before you submit:

- [ ] The README states the environment requirements (Python version, OS) and installation steps, and dependency versions are pinned
- [ ] Model configuration comes from environment variables, with a `.env.example`; **there are no secrets anywhere in the repository**
- [ ] One "quick start" command runs a complete task, and the expected output is documented
- [ ] Every number in the report maps to a command and a result file (for example, `run_evals.py` → `runs/eval_report.json`)
- [ ] Everything that doesn't need an API key (offline tests, scripted mode) runs with a single command
- [ ] Every concurrency / fault-tolerance claim has a real process-level test or demo (for example, one command that starts the deployment and injects kill -9), and the parts that are simulated are stated
- [ ] The model snapshot version, run date, sampling parameters, and number of runs per configuration are documented
- [ ] Eval data ships with the repository or comes with a download script, and its license is stated
- [ ] The report says roughly how much money and time one full eval run takes to reproduce
- [ ] Someone outside the project followed the README from scratch, and you fixed every place they got stuck

## References

Use one consistent format: authors, title, venue (or URL), year. Every number you cite in the main text should be traceable to an entry here.

## Appendix

Good candidates: the full list of eval cases, full prompts, additional experimental results, complete traces of failed samples, and labeling guidelines.
