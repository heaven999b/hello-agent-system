[中文](lesson-template.md) | [English](lesson-template.en.md)

# Lesson authoring guide (required reading for contributors & maintainers)

The goal of this repository: **give developers with modest experience a complete mental model of enterprise agent system design in 4 hours — and have them build every layer themselves.**
Every lesson must be easy for beginners to follow and still teach experienced engineers something new.

## Directory structure

```
lessons/NN_topic/
├── README.md          Lesson notes (the main content)
├── demo.py            Runnable demo: calls a real model by default; --offline uses ScriptedLLM, no API key needed
├── exercise.py        Exercises (learners fill them in; each TODO raises NotImplementedError)
├── solution.py        Reference solution (exactly the same interface as exercise.py)
└── test_exercise.py   Exercise tests (offline, deterministic; plain async def test_...; loaded with agentkit.testing.load_exercise)
```

To run: `python lessons/NN_topic/demo.py [--offline]`, `make lesson N=NN`, `make test-solutions`.

## README structure

```markdown
# Lesson NN: Title

> 🕐 Suggested time: N min · 🎯 You'll learn to: one sentence · 📦 Source: `agentkit/xxx.py`

## 0. In one sentence
(Open with an analogy, or a real scenario or incident, so beginners instantly see why this is needed)

## 1. Core concepts
(Include mermaid diagrams; intuition first, then precise definitions)

## 2. From toy to production: building it layer by layer
(Quote real agentkit code and explain, one by one, why each design decision was made)

## 3. Hands-on: run the demo
(Commands + an excerpt of the expected output + what to look for)

## 4. Exercises
(Describe the tasks, give hints, and explain how to verify: make lesson N=NN)

## 5. Going deeper (for the curious)
(Industry approaches compared, trade-offs, edge cases, what changes at scale)

## 6. Common pitfalls and anti-patterns

## 7. Interview & design review questions
(5-8 questions, with the key points of each answer inside <details>)

## 8. Self-check
- [ ] I can explain ...

## Further reading
(Only real sources you have verified)
```

## Writing principles

1. **Intuition before jargon**: the first time a term appears, give both its Chinese and English names and explain it in one plain sentence.
2. **Answer "why" for every design decision**: don't just write "add retries"; write what happens without them, and what happens if you get them wrong.
3. **Concrete beats abstract**: use specific numbers, specific error messages, and specific scenarios.
4. **Don't make things up**: cited papers, blog posts, and incidents must be real; if you're not sure, leave it out.
5. **Code must run**: every command and code snippet in the docs must actually have been run.
6. **Concurrency and multiple processes must really happen, with evidence**: the code is async (`await` wherever it waits on I/O, and nothing blocking on the event loop). If you claim concurrency, show the peak in-flight count (`max_in_flight` from `ScriptedLLM(latency=...)`); if you claim multiple processes or crash takeover, start real processes with `WorkerPool` and send real signals (kill -9 / SIGSTOP / SIGTERM). Don't pass off threads as processes, two objects in one process as two machines, or `sleep` and fake clocks as failures; state honestly what you can't show (multiple hosts, a real Redis failover, and so on) as a limitation. See [CONTRIBUTING](../CONTRIBUTING.en.md#change-the-agentkit-framework).

## The course has four parts

| Part | Lessons | Share | Approach |
|---|---|---|---|
| Part 1: Building Blocks | 00–07 | ~20% (~140 min) | Concepts → build from scratch → exercises |
| Part 2: Enterprise Problems and Solutions | 08–16 | ~23% (~160 min) | **Problem-driven**: a real problem → compare several solutions → when each one fits → recommended choice → code |
| Part 3: Advanced — Building Blocks in Depth, the ML Loop, and the Application Frontier | 17–25 | ~31% (~210 min) | Concepts → build from scratch → exercises + trade-off comparison |
| Part 4: Production on Mature Components | 26–31 | ~26% (~175 min) | Why the teaching version falls short → compare mature components (2–5 options: build / open source / managed) → how the adapter plugs in → operations and common pitfalls → how to switch to a managed service |

Part 3 is advanced, optional material for learners who have finished the first two parts and the capstone. Each lesson keeps the README structure above, implements the core mechanisms from scratch (they can live in lesson-local modules such as `xxx_kit.py`), and compares the trade-offs between industry solutions (in the relevant section or in "Going deeper") rather than presenting just one approach.

Part 4 is also advanced and optional. Each lesson opens §0 with one or two honest sentences on the teaching agentkit's limits in that area. The code has three layers with the same interfaces: core `agentkit` (async, one process driving many sessions at once) → `agentkit.distributed` (SQLite, real multiple processes on one machine, Lessons 12 and 13) → adapters in `agentkit/contrib/` plus the `production/` reference service (Postgres, Redis, Temporal, and so on, across machines); demos run for real on embedded infrastructure (real Postgres via pgserver, fakeredis, the Temporal dev server) and, when optional dependencies are missing, print the install command and exit with code 0; every number in the lesson must come from a measurement, and the lesson must spell out how the embedded infrastructure differs from a production cluster.

## Enterprise problem cards (the main format of every Part 2 lesson)

Each Part 2 lesson is built from several problem cards (at least 5 per lesson). Each card looks like this:

```markdown
### Problem N: one sentence describing a real enterprise problem

**Scenario**: a real scenario with concrete numbers ("A 2,000-person company peaks at 50 requests per second, and the model API is capped at 500 requests per minute…")

**Why it's hard**: why the intuitive solution doesn't work / where it breaks down

| Option | How it works | Pros | Cons | When to use |
|---|---|---|---|---|
| A. xxx | ... | ... | ... | ... |
| B. xxx | ... | ... | ... | ... |
| C. xxx | ... | ... | ... | ... |

**How to choose**: give the basis for the decision (scale, consistency requirements, team skills, cost) — not "it depends."
**This lesson's implementation**: which option this repo's code implements (with a link to the source), and what to swap in for production (e.g. SQLite → Postgres/Redis).
```
