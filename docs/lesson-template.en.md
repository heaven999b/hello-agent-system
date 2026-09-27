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
└── test_exercise.py   Exercise tests (offline, deterministic; loaded with agentkit.testing.load_exercise)
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

## The course has two parts

| Part | Lessons | Share | Approach |
|---|---|---|---|
| Part 1: Building Blocks | 00–04 | ~1/3 | Concepts → build from scratch → exercises |
| Part 2: Enterprise Problems and Solutions | 05–13 | ~2/3 | **Problem-driven**: a real problem → compare several solutions → when each one fits → recommended choice → code |

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
