[中文](CONTRIBUTING.md) | [English](CONTRIBUTING.en.md)

# 参与贡献

感谢你愿意让这个课程变得更好！无论是修一个错别字、反馈一处没看懂的讲解，还是新增一整节课，都非常欢迎。

本项目的目标只有一个：**让编程基础一般、调过 LLM API 的开发者，在 4 小时内掌握企业级 Agent 系统设计，并亲手实现每一层。**
所有贡献都用这个目标来衡量——"更准确、更好懂、更贴近生产"的改动优先，"更多、更全"不一定更好。

## 目录

- [你可以怎么参与](#你可以怎么参与)
- [搭建开发环境](#搭建开发环境)
- [运行测试](#运行测试)
- [新增一节课](#新增一节课)
- [修改 agentkit 框架](#修改-agentkit-框架)
- [代码风格](#代码风格)
- [提交规范](#提交规范)
- [提交 Pull Request](#提交-pull-request)
- [安全问题](#安全问题)

## 你可以怎么参与

| 你想做的事 | 怎么做 |
|---|---|
| 讲义看不懂、练习卡住、内容有误 | 提 issue，选择「📚 课程反馈」模板 |
| 代码 / demo / 测试出错 | 提 issue，选择「🐛 Bug 报告」模板 |
| 建议新主题、新工具 | 提 issue，选择「✨ 功能 / 新内容建议」模板，**先讨论再动手** |
| 修错别字、改进措辞、补充例子 | 直接提 PR |
| 翻译 | 先开 issue 说明语言与范围，避免重复劳动 |

> 💡 在 issue 里说明"你原本是怎么理解的、卡在哪一步"，比"看不懂"有用得多。

## 搭建开发环境

需要 Python 3.10+ 和 `make`（Windows 建议使用 WSL）。

```bash
git clone https://github.com/<你的用户名>/hello-agent-system.git
cd hello-agent-system
make setup        # 创建 .venv，安装 agentkit（可编辑模式）和 pytest
```

**开发和测试不需要任何 API key**：所有测试都用 `ScriptedLLM`（剧本模型）离线运行，所有 demo 都支持 `--offline`。
如果你想用真实模型验证 demo，把 `.env.example` 复制为 `.env` 并填写，然后 `make check-env`。
**`.env` 已在 `.gitignore` 中，永远不要提交任何密钥。**

## 运行测试

| 命令 | 作用 |
|---|---|
| `make test-solutions` | 用参考答案跑全部测试（框架测试 + 每课练习测试）。**提交 PR 前必须全绿，CI 跑的就是它** |
| `make test` | 用 `exercise.py` 跑测试。练习没写完时会失败，这是正常的 |
| `make lesson N=08` | 只跑第 08 课的练习测试 |
| `.venv/bin/python lessons/08_reliability/demo.py --offline` | 离线运行某课 demo |
| `.venv/bin/python scripts/progress.py` | 学习进度看板：逐课检查练习完成情况 |
| `.venv/bin/python -m agentkit.viewer traces.jsonl -o trace.html` | 把导出的 trace 渲染成可交互的 HTML 页面 |

`make test-solutions` 的原理：测试通过 `agentkit.testing.load_exercise(__file__)` 加载练习模块；
设置环境变量 `AGENTKIT_SOLUTION=1` 时，它会改为加载同目录的 `solution.py`。这样 CI 可以证明"测试本身是对的、参考答案能通过"。

CI（`.github/workflows/ci.yml`）会在 Python 3.10 / 3.11 / 3.12 上运行：`make test-solutions` 同款测试、每节课 `demo.py --offline`、trace 查看器冒烟测试和进度看板。

## 新增一节课

**动手之前请先阅读 [课程编写规范](docs/lesson-template.md)**，它规定了目录结构、README 的章节结构和写作原则。然后按下面的步骤来：

### 1. 先开 issue 讨论

说明主题、放在课程的哪个位置、预计用时、学员能亲手实现什么。4 小时的总时长很宝贵，新课需要论证"为什么这是企业级 Agent 绕不开的一环"。

### 2. 创建目录与文件

```text
lessons/NN_topic/
├── README.md          讲义（按 docs/lesson-template.md 的结构）
├── demo.py            可运行演示：默认调用真实模型，--offline 用 ScriptedLLM
├── exercise.py        练习：TODO 处 raise NotImplementedError
├── solution.py        参考答案：与 exercise.py 接口完全一致
└── test_exercise.py   练习测试：离线、确定性
```

### 3. 写练习：`exercise.py` 与 `solution.py`

- 两个文件的函数名、参数、返回值**完全一致**，区别只在函数体。
- 学员要写的地方统一用 `raise NotImplementedError("一句话提示：要做什么、注意什么")`。
- 进度看板（`scripts/progress.py`）依靠这一点判断"未开始"：**练习未实现时，测试的失败原因应该是 `NotImplementedError`**。
- 模块 docstring 写清楚：要实现什么、怎么验证（`make lesson N=NN`）、卡住时看哪里。

```python
def detect_loop(history: list[str], window: int = 3) -> bool:
    """最近 window 次工具调用完全相同，就认为陷入了死循环。"""
    raise NotImplementedError("比较 history 最后 window 个元素是否全部相同；长度不足 window 时返回 False")
```

### 4. 写测试：`test_exercise.py`

```python
from agentkit import ScriptedLLM, call_tool, reply
from agentkit.testing import load_exercise

ex = load_exercise(__file__)  # 默认加载 exercise.py；AGENTKIT_SOLUTION=1 时加载 solution.py


def test_detects_three_identical_calls():
    assert ex.detect_loop(["search", "search", "search"]) is True
```

- **离线、确定性**：用 `ScriptedLLM` 扮演模型；不访问网络，不依赖真实时间（需要时注入时钟或用很短的超时）和随机数。
- 测试名描述行为（`test_rejects_cross_tenant_access`），断言失败信息能给学员提示。
- 覆盖边界情况，但不要测实现细节——学员的正确实现方式可能和参考答案不同。

### 5. 写 demo：`demo.py`

- 默认使用真实模型（`agentkit.default_llm()`），`--offline` 时用 `ScriptedLLM` 剧本，**两种模式都要能跑**。
- 输出要讲故事：每一步打印"发生了什么、该观察什么"，而不只是结果。
- 运行产物写到 `runs/` 或 `traces/`（已被 `.gitignore` 忽略）。

### 6. 自检

```bash
AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/NN_topic   # 参考答案全部通过
.venv/bin/python -m pytest lessons/NN_topic                        # 未实现时应失败，且原因是 NotImplementedError
.venv/bin/python lessons/NN_topic/demo.py --offline                # demo 离线可运行
make test-solutions                                                 # 全仓库仍然全绿
```

最后，在根目录 `README.md` 的「学习路线」表格里加上这一课。

## 修改 agentkit 框架

`agentkit/` 是课程的"教材源码"，学员会逐行阅读它，所以要求比普通项目更严格：

- **可读性优先**：函数短小，关键设计决策用中文注释解释"为什么"（不这么做会怎样）。宁可多写两行直白的代码，也不要炫技。
- **不引入新的运行时依赖**：目前只依赖 `openai` 和 `pydantic`。可选能力放进 `[project.optional-dependencies]`。
- **每个行为改动都要有测试**（`tests/`），并且保持离线、确定性。
- **公共 API 变化要同步课程**：搜索 `lessons/`、`capstone/`、`docs/` 中所有引用处一并修改。
- **只有一套实现，而且是 async 的**：不要为了"好懂"另写一个同步版本（两套实现迟早分叉，修一个 bug 要修两遍）。
  等待 I/O 的地方 `await`；纯计算保持普通函数；在事件循环里绝不能调用阻塞函数（第 02 课 1.7 节）。
- **声称的能力必须真实实现并有测试证明**：说"并发"就要用在途峰值证明，说"多进程 / 崩溃接手"就要起真实进程、发真实信号（`agentkit.distributed.WorkerPool`），
  不要用线程冒充进程、用同一进程里的两个对象冒充两台机器、用 sleep 或假时钟冒充故障。做不到的（例如多主机、真 Redis 故障切换）如实写成局限。

## 代码风格

- Python 3.10+，每个文件开头 `from __future__ import annotations`，公共函数写类型注解。
- **标识符用英文，注释、docstring、文档、界面文字用中文**。
- 行宽约 120 字符；遵循 PEP 8；导入按标准库 / 第三方 / 本地分组。
- 不使用元类等不必要的高级技巧（课程面向基础一般的开发者）；async/await 是例外——它是服务端 Agent 的基本功，第 02 课从零讲起。
- 中文写作：中英文、中文与数字之间加空格（"调用 LLM API 3 次"），使用全角中文标点；术语第一次出现时中英对照。
- 文档中的命令和代码片段必须真实运行过；引用的论文、博客、事故必须真实存在——不确定就不写。
- **中英文档同步**：本仓库的文档是中英双语的（`xxx.md` 为中文版，`xxx.en.md` 为英文版）。新增或修改任何文档时，必须在同一个 PR 里同步更新另一种语言的版本，并遵循 [翻译规范](docs/translation-guide.md)（语言切换行、结构一致、术语表）。

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/) 格式，描述可以用中文：

```text
<类型>(<范围>): <简短描述>
```

| 类型 | 用途 | 示例 |
|---|---|---|
| `feat` | 新课程内容、新功能 | `feat(lesson-06): 新增"致命三要素"检测练习` |
| `fix` | 修复错误 | `fix(agentkit): 修复熔断器半开状态下的并发计数` |
| `docs` | 讲义、文档 | `docs(lesson-03): 补充摘要压缩的失败案例` |
| `test` | 测试 | `test(viewer): 覆盖 U+2028 转义` |
| `refactor` | 不改变行为的重构 | `refactor(tools): 拆分参数校验逻辑` |
| `chore` / `ci` | 构建、CI、杂项 | `ci: 增加 Python 3.12 矩阵` |

常用范围：`lesson-NN`、`capstone`、`agentkit`、`tools`、`tracing`、`viewer`、`docs`、`ci`。
一个提交只做一件事；一个 PR 聚焦一个主题。

## 提交 Pull Request

1. Fork 仓库，从 `main` 创建分支：`git checkout -b feat/lesson-06-policy-engine`。
2. 修改并运行 `make test-solutions`，确保全绿。
3. 推送分支并发起 PR，按模板填写自检清单。
4. 等待 CI 通过和维护者 review。review 意见可能涉及"学员能否看懂"，这和代码正确性同样重要。

## 安全问题

如果你发现的是安全漏洞（例如某个防护可被绕过、示例代码会泄露密钥），**请不要公开提 issue**，
请通过 GitHub 仓库的 "Security → Report a vulnerability" 私下报告。

## 许可证

提交贡献即表示你同意你的贡献以 [MIT License](LICENSE) 授权。

感谢你让更多人学会构建可靠、安全的 Agent！⭐
