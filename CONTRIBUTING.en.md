[中文](CONTRIBUTING.md) | [English](CONTRIBUTING.en.md)

# Contributing

Thanks for helping make this course better! Whether you're fixing a typo, flagging an explanation that didn't click, or adding a whole new lesson, contributions are very welcome.

This project has exactly one goal: **help developers who have modest programming experience and have called an LLM API before master enterprise agent system design in 4 hours — and build every layer themselves.**
Every contribution is measured against that goal. Changes that make the course more accurate, easier to follow, or closer to production come first; "more" or "more complete" isn't necessarily better.

## Contents

- [Ways to contribute](#ways-to-contribute)
- [Set up your development environment](#set-up-your-development-environment)
- [Run the tests](#run-the-tests)
- [Add a new lesson](#add-a-new-lesson)
- [Change the agentkit framework](#change-the-agentkit-framework)
- [Code style](#code-style)
- [Commit messages](#commit-messages)
- [Open a pull request](#open-a-pull-request)
- [Security issues](#security-issues)

## Ways to contribute

| What you want to do | How |
|---|---|
| The notes are confusing, you're stuck on an exercise, or something is wrong | Open an issue with the **📚 课程反馈** (Lesson feedback) template |
| Code / a demo / a test is broken | Open an issue with the **🐛 Bug 报告** (Bug report) template |
| Suggest a new topic or tool | Open an issue with the **✨ 功能 / 新内容建议** (Feature / new content suggestion) template — **discuss it before you build it** |
| Fix typos, improve wording, add examples | Open a PR directly |
| Translation | Open an issue first describing the language and scope, to avoid duplicated effort |

> 💡 In your issue, explaining how you understood the material and exactly where you got stuck is far more useful than "I don't get it."

## Set up your development environment

You need Python 3.10+ and `make` (on Windows, WSL is recommended).

```bash
git clone https://github.com/<your-username>/hello-agent-system.git
cd hello-agent-system
make setup        # creates .venv and installs agentkit (in editable mode) and pytest
```

**Development and testing don't need any API key**: every test runs offline with `ScriptedLLM` (a scripted model), and every demo supports `--offline`.
If you want to check a demo against a real model, copy `.env.example` to `.env`, fill it in, and run `make check-env`.
**`.env` is already in `.gitignore`. Never commit any secrets.**

## Run the tests

| Command | What it does |
|---|---|
| `make test-solutions` | Runs every test against the reference solutions (framework tests + each lesson's exercise tests). **It must be green before you open a PR — this is what CI runs** |
| `make test` | Runs the tests against `exercise.py`. Failures are expected while the exercises are unfinished |
| `make lesson N=08` | Runs only Lesson 08's exercise tests |
| `.venv/bin/python lessons/08_reliability/demo.py --offline` | Runs one lesson's demo offline |
| `.venv/bin/python scripts/progress.py` | Progress dashboard: checks exercise completion lesson by lesson |
| `.venv/bin/python -m agentkit.viewer traces.jsonl -o trace.html` | Renders exported traces as an interactive HTML page |

How `make test-solutions` works: the tests load the exercise module through `agentkit.testing.load_exercise(__file__)`.
When the environment variable `AGENTKIT_SOLUTION=1` is set, it loads `solution.py` from the same directory instead. That's how CI proves that the tests themselves are correct and that the reference solutions pass.

CI (`.github/workflows/ci.yml`) runs on Python 3.10 / 3.11 / 3.12: the same tests as `make test-solutions`, every lesson's `demo.py --offline`, a smoke test of the trace viewer, and the progress dashboard.

## Add a new lesson

**Before you start, read the [lesson authoring guide](docs/lesson-template.en.md)**, which defines the directory layout, the README's section structure, and the writing principles. Then follow these steps:

### 1. Open an issue to discuss it first

Describe the topic, where it fits in the course, how long it should take, and what learners will build themselves. The 4-hour total is precious, so a new lesson has to make the case for why it's an unavoidable part of enterprise agents.

### 2. Create the directory and files

```text
lessons/NN_topic/
├── README.md          Lesson notes (following the structure in docs/lesson-template.md)
├── demo.py            Runnable demo: calls a real model by default; --offline uses ScriptedLLM
├── exercise.py        Exercises: each TODO raises NotImplementedError
├── solution.py        Reference solution: exactly the same interface as exercise.py
└── test_exercise.py   Exercise tests: offline, deterministic
```

### 3. Write the exercise: `exercise.py` and `solution.py`

- The two files have **exactly the same** function names, parameters, and return values; only the function bodies differ.
- Wherever learners need to write code, use `raise NotImplementedError("one-line hint: what to do and what to watch out for")`.
- The progress dashboard (`scripts/progress.py`) relies on this to detect "not started": **when an exercise isn't implemented yet, its tests should fail with `NotImplementedError`**.
- The module docstring should state clearly what to implement, how to verify it (`make lesson N=NN`), and where to look when stuck.

```python
def detect_loop(history: list[str], window: int = 3) -> bool:
    """If the last `window` tool calls are identical, treat it as an infinite loop."""
    raise NotImplementedError("Check whether the last window elements of history are all identical; return False if history is shorter than window")
```

### 4. Write the tests: `test_exercise.py`

```python
from agentkit import ScriptedLLM, call_tool, reply
from agentkit.testing import load_exercise

ex = load_exercise(__file__)  # loads exercise.py by default; loads solution.py when AGENTKIT_SOLUTION=1


def test_detects_three_identical_calls():
    assert ex.detect_loop(["search", "search", "search"]) is True
```

- **Offline and deterministic**: use `ScriptedLLM` to play the model; no network access, and no dependence on real time (inject a clock or use very short timeouts when you need to) or on randomness.
- Test names describe behavior (`test_rejects_cross_tenant_access`), and assertion messages should give the learner a hint.
- Cover edge cases, but don't test implementation details — a learner's correct solution may look different from the reference.

### 5. Write the demo: `demo.py`

- Use a real model by default (`agentkit.default_llm()`) and a `ScriptedLLM` script with `--offline`. **Both modes must run.**
- The output should tell a story: at every step, print what happened and what to look for, not just the results.
- Write run artifacts to `runs/` or `traces/` (both are ignored in `.gitignore`).

### 6. Self-check

```bash
AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/NN_topic   # the reference solution passes everything
.venv/bin/python -m pytest lessons/NN_topic                        # before implementation, fails with NotImplementedError
.venv/bin/python lessons/NN_topic/demo.py --offline                # the demo runs offline
make test-solutions                                                 # the whole repo is still green
```

Finally, add the lesson to the learning path table (the **📚 学习路线** section) in the root `README.md`.

## Change the agentkit framework

`agentkit/` is the course's "textbook source code." Learners read it line by line, so the bar is higher than for an ordinary project:

- **Readability first**: keep functions short, and explain key design decisions in Chinese comments — why it's done this way, and what goes wrong otherwise. Two extra lines of plain code beat a clever trick.
- **No new runtime dependencies**: the only dependencies are `openai` and `pydantic`. Put optional capabilities under `[project.optional-dependencies]`.
- **Every behavior change needs tests** (`tests/`), kept offline and deterministic.
- **Public API changes must be carried through the course**: search `lessons/`, `capstone/`, and `docs/` for every reference and update them in the same change.
- **There is one implementation, and it is async**: don't add a separate synchronous version "for readability" (two implementations drift apart, and every bug has to be fixed twice).
  `await` wherever you wait on I/O; keep pure computation as plain functions; never call a blocking function inside the event loop (Lesson 02, section 1.7).
- **Claimed capabilities must be really implemented and proven by tests**: "concurrent" needs an in-flight peak to prove it; "multi-process / crash takeover" needs real processes and real signals (`agentkit.distributed.WorkerPool`).
  Don't use threads to pretend to be processes, two objects in one process to pretend to be two machines, or sleeps and fake clocks to pretend to be failures. Whatever can't be done (e.g. multiple hosts, real Redis failover) must be written down honestly as a limitation.

## Code style

- Python 3.10+; start every file with `from __future__ import annotations`; add type annotations to public functions.
- **Identifiers in English; comments, docstrings, docs, and user-facing text in Chinese.**
- Line length around 120 characters; follow PEP 8; group imports as standard library / third-party / local.
- No metaclasses or other unnecessary advanced tricks (the course is aimed at developers with modest experience); async/await is the exception — it is a basic skill for server-side agents, taught from scratch in Lesson 02.
- Chinese writing: put a space between Chinese and English text and between Chinese and numbers (e.g. "调用 LLM API 3 次", meaning "call the LLM API 3 times"), use full-width Chinese punctuation, and give both the Chinese and English name for a term the first time it appears.
- Commands and code snippets in the docs must actually have been run; cited papers, blog posts, and incidents must be real — if you're not sure, leave it out.
- **Keep the Chinese and English docs in sync**: every doc in this repo is bilingual (`xxx.md` is the Chinese version, `xxx.en.md` the English one). When you add or change a doc, update the other language in the same PR, following the [translation guide](docs/translation-guide.md) (language switcher line, identical structure, glossary).

## Commit messages

Use the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) format. The description may be written in Chinese:

```text
<type>(<scope>): <short description>
```

| Type | Use it for | Example |
|---|---|---|
| `feat` | New lesson content, new features | `feat(lesson-06): add a "lethal trifecta" detection exercise` |
| `fix` | Bug fixes | `fix(agentkit): fix concurrent counting in the circuit breaker's half-open state` |
| `docs` | Lesson notes, documentation | `docs(lesson-03): add a failure case for summarization` |
| `test` | Tests | `test(viewer): cover U+2028 escaping` |
| `refactor` | Refactoring that doesn't change behavior | `refactor(tools): split out argument validation` |
| `chore` / `ci` | Build, CI, miscellaneous | `ci: add Python 3.12 to the matrix` |

Common scopes: `lesson-NN`, `capstone`, `agentkit`, `tools`, `tracing`, `viewer`, `docs`, `ci`.
One commit does one thing; one PR focuses on one topic.

## Open a pull request

1. Fork the repo and create a branch from `main`: `git checkout -b feat/lesson-06-policy-engine`.
2. Make your changes and run `make test-solutions`; make sure everything is green.
3. Push the branch, open a PR, and fill in the self-check list in the template.
4. Wait for CI to pass and for a maintainer's review. Review comments may be about whether learners can follow the material — that matters just as much as code correctness.

## Security issues

If what you've found is a security vulnerability (for example, a guardrail that can be bypassed, or sample code that leaks secrets), **please don't open a public issue**.
Report it privately through the GitHub repository's "Security → Report a vulnerability" page.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).

Thanks for helping more people learn to build reliable, secure agents! ⭐
