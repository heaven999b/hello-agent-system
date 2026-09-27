[中文](README.md) | [English](README.en.md)

# 第 05 课：常见 Agent 架构 —— 从 ReAct 到深度研究系统

> 🕐 建议用时：20 分钟 ｜ 🎯 学完你能：说清 7 种单 Agent 架构和 5 种多 Agent 拓扑里，"模型在哪里思考、状态放在哪里、控制权在谁手里"；拆解深度研究、编码、Computer-Use、客服、Agentic RAG 五类真实产品；为一个新需求选出架构并说出理由 ｜ 📦 对应源码：`agentkit/agent.py`（ReAct）、`agentkit/workflows.py`（Reflection、主管-专家）、`agentkit/permissions.py`（人工检查点）、本课 [demo.py](demo.py)
>
> 📖 必读：[Cognitive Architectures for Language Agents](https://arxiv.org/abs/2309.02427)（Sumers 等, 2024）—— CoALA 框架论文，用"记忆模块、行动空间、决策过程"三个维度统一描述各种语言 Agent，和本课 §1.2 "只问四个问题"的思路一致；重点读 §4 的框架定义和 §5 的 Table 2（把 ReAct、Voyager、Generative Agents、Tree of Thoughts 等放进同一张表对比）。

> 📍 本课属于**第一部分：基础构建**。第 02~04 课造好了零件（循环、工具、上下文与记忆），本课讲这些零件常见的**组装方式**；下一课 [第 06 课 编排模式](../06_orchestration/README.md) 讲 Workflow 模式和多 Agent 编排的实现细节。两课互为补充，分工见 §1.3。
>
> 🧭 **核心路径（20 分钟）**：§0 → §1 → §2 每种架构先看"一句话"和图 → §3.1 看拓扑的三个问题 → §3.7 拓扑对比表 → §4 挑一个你最关心的产品看 → §6 总表和决策树 → §7 跑 Demo → §8 做练习。
> 标 **📖 选读** 的小节第一遍可以跳过。

## 0. 一句话讲清楚

**架构的本质，是"模型在哪里思考"。**

同样是装修一套房子，有好几种干法：

| 架构 | 装修类比 | 模型在哪里思考 |
|---|---|---|
| **ReAct** | 师傅边拆边看，看到什么再决定下一步 | 每一步都想 |
| **Plan-and-Execute** | 先出施工图，按图施工，遇到意外再改图 | 开头想一次，出错时再想 |
| **ReWOO** | 把材料清单和工序一次写死交给施工队，中间不再过问 | 只在开头想（最后汇总一次） |
| **Reflection** | 干完请监理挑毛病，然后返工 | 做完之后再想一遍 |
| **CodeAct** | 不再一件件吩咐，直接给施工队一份脚本 | 每一步都想，但一步能做很多事 |
| **树搜索** | 同时试几种方案，打分挑最好的 | 在多条分支上同时想 |
| **HITL（人工检查点）** | 关键节点请业主签字 | 模型想，人拍板 |

多 Agent 也一样，只是问题变成：**几个"大脑"之间怎么传话、共用什么、谁说了算**。

本课 Demo 用真实模型（gpt-5.5）把同一个任务"查三个城市的天气，给出差建议"用三种架构各跑了一遍。工具是假的，而且广州的天气接口被故意设成"维护中"：

| 架构 | 模型调用 | 工具调用 | tokens | 耗时 | 质量检查（3 城 / 台风预警 / ≤5 行） |
|---|---|---|---|---|---|
| ReAct（`agentkit.Agent`） | 3 | 4 | 2,705 | 10.7s | ✅ |
| Plan-and-Execute | 3 | 4 | 2,724 | 14.3s | ✅ |
| Reflection（ReAct + 批评 + 修改） | 4 | 4 | 3,716 | 11.2s | ✅ |

一个反直觉的结果：**在这个小任务上，"先规划"并没有更省**。原因在 §7 细说。它说明了本课最重要的一点：**架构的收益取决于任务的形状，要用数据选，而不是看名字选。**

## 1. 核心概念

### 1.1 三层地图

```mermaid
flowchart TB
    subgraph A["A 单 Agent 的推理与控制架构（§2）"]
        direction LR
        A1["ReAct"] --- A2["Plan-and-Execute"] --- A3["ReWOO"] --- A4["Reflection"]
        A5["CodeAct"] --- A6["树搜索"] --- A7["HITL 检查点"]
    end
    subgraph B["B 多 Agent 的拓扑（§3）"]
        direction LR
        B1["路由 + 专家"] --- B2["主管 / 层级"] --- B3["网络 / 群体"]
        B4["黑板"] --- B5["事件驱动"]
    end
    subgraph C["C 真实产品的架构形态（§4）"]
        direction LR
        C1["深度研究"] --- C2["编码 Agent"] --- C3["Computer-Use"]
        C4["客服"] --- C5["Agentic RAG"]
    end
    A -->|"每个 Agent 内部用哪种"| C
    B -->|"多个 Agent 之间怎么连"| C
    M["记忆架构（§5）"] -.->|"贯穿三层"| C
```

真实产品几乎都是**组合**：一个编码 Agent 的主循环是 ReAct，里面用一个待办列表工具做轻量规划，把搜索类子任务交给子 Agent，靠跑测试做反思，危险命令前停下来等人批准。学完本课，你应该能把任何一个 Agent 产品拆成这样一句话。

### 1.2 看懂任何架构，只问四个问题

| 问题 | 可能的答案 | 为什么重要 |
|---|---|---|
| **1. 谁规划、什么时候规划？** | 代码写死 / 模型每一步 / 模型开头一次 / 出错时重规划 / 人 | 决定了灵活性和模型调用次数 |
| **2. 观察结果回到哪里？** | 回到模型 / 回到代码 / 回到另一个 Agent / 写到共享状态 | 决定了能不能随机应变，也决定了上下文会不会膨胀 |
| **3. 状态放在哪里？** | 消息历史 / 结构化计划 / 共享黑板 / 外部存储 | 决定了能否持久化、能否恢复、谁能看到什么 |
| **4. 谁决定停？** | 模型说完了 / 计划执行完 / 评审通过 / 预算用完 / 人 | 决定了失控时的最坏情况 |

后面每种架构，你都可以拿这四个问题去套。

### 1.3 和第 06 课的分工

| | 本课（05 常见架构） | [第 06 课 编排模式](../06_orchestration/README.md) |
|---|---|---|
| 核心问题 | 一个 Agent **内部**怎么推理和控制；多个 Agent 之间是什么**拓扑**、怎么通信；真实产品长什么样 | 用代码把 LLM 调用编排成流程的 5 种 Workflow 模式；多 Agent 的落地实现和代价 |
| 典型内容 | ReAct vs Plan-and-Execute vs ReWOO；黑板 vs 主管 vs handoff | 提示链、路由、并行、编排者-执行者、评估-优化；`agent_as_tool` |

有几个名字两课都会出现，区别如下：

- **Reflection ≈ 评估-优化**（06 §2.5）：06 讲这个 Workflow 模式怎么写；本课讲它作为一种推理架构，什么时候有效、什么时候无效（§2.4）。
- **Plan-and-Execute ≠ 编排者-执行者**（06 §2.4）：编排者-执行者的子任务**相互独立、并行执行、不重规划**；Plan-and-Execute 的步骤**前后依赖、顺序执行、出错可以重规划**。
- **路由 + 专家、主管-专家**：06 讲分类那一步怎么做可靠、`agent_as_tool` 怎么写、多 Agent 的成本；本课讲它们作为拓扑的通信方式和控制权（§3）。

## 2. 单 Agent 的推理与控制架构

每种架构都按同一个格式讲：一句话本质 → 图 → 运行流程 → 一张卡片（适用场景、优点、缺点与失败模式、成本与延迟、代表论文或产品）→ 用 agentkit 怎么实现。

### 2.1 ReAct：边想边做

**一句话**：思考 → 行动 → 观察，一直循环，直到模型认为信息足够、给出回答。

```mermaid
flowchart LR
    Q["任务"] --> T["思考<br/>下一步做什么"]
    T --> A["行动<br/>调用工具"]
    A --> O["观察<br/>工具结果进入历史"]
    O --> T
    T -->|"信息足够"| F["最终回答"]
```

**运行流程**：① 把任务和工具列表发给模型；② 模型返回工具调用，执行后把结果追加进消息历史；③ 带着完整历史再问模型；④ 模型不再调用工具时结束。第 02 课讲过它的来历：今天的 function calling 就是 ReAct 的原生结构化版本。

| 维度 | 说明 |
|---|---|
| 适用 | 步骤无法预知、要根据中间结果随机应变：排查故障、在陌生代码库里改 bug、开放式问答 |
| 优点 | 最简单、最灵活；意外会被自然处理（Demo 里广州接口维护，模型下一步就换成了备用接口）；所有主流 API 原生支持 |
| 缺点与失败模式 | **短视**：只看下一步，长任务容易跑偏；**原地打转**：反复调用同一个工具；**上下文膨胀**：每一步的观察都堆进历史；**过早收工**：没做完就宣布完成 |
| 成本与延迟 | N 步 = N 次模型调用，而且每次都要重发全部历史，**总输入 token 随步数大致按平方增长**（第 1 步发 1 份历史，第 N 步发 N 份）；各步串行，延迟是逐步累加的 |
| 代表 | [ReAct](https://arxiv.org/abs/2210.03629)（Yao 等，ICLR 2023）：在 ALFWorld 和 WebShop 上，成功率比模仿学习和强化学习方法分别高出 34 和 10 个百分点；今天几乎所有 Agent 框架的默认循环 |

**用 agentkit 实现**：`agentkit.Agent` 本身就是一个带护栏的 ReAct 循环（[agentkit/agent.py](../../agentkit/agent.py)）。

```python
agent = Agent(llm, [get_weather, get_weather_by_airport], max_steps=8)  # max_steps 防止原地打转
result = agent.run("查北京、上海、广州出差当天的天气，给出差建议")
```

### 2.2 Plan-and-Execute：先规划，再执行，出错时重规划

**一句话**：先让模型写出完整计划，再一步步执行；某一步失败（或每步之后），再让模型修改剩下的计划。

```mermaid
flowchart LR
    T["任务"] --> P["规划器 LLM<br/>写出步骤列表"]
    P --> E["执行器<br/>执行下一步<br/>工具调用或小 Agent"]
    E --> C{"这一步<br/>成功了吗"}
    C -->|"成功 且还有步骤"| E
    C -->|"失败"| R["重规划器 LLM<br/>改写剩余计划"]
    R -->|"次数未超上限"| E
    C -->|"全部完成"| S["汇总 LLM"] --> Out["输出"]
```

**运行流程**：① 规划器输出**结构化**的步骤列表（不是一段自由文本）；② 执行器按顺序执行，每步可以是一次工具调用，也可以是一个小 ReAct Agent 处理的子任务；③ 某步失败时，把"已完成的结果 + 失败原因 + 剩余计划"交给重规划器；④ 全部完成后汇总。

| 维度 | 说明 |
|---|---|
| 适用 | 步骤能大致预先想清楚、但执行中可能出意外的多步任务：数据报表、调研、批量操作 |
| 优点 | 逼模型先想清全局；**计划可以展示给人看、让人批准**（HITL 的天然接口）；执行器可以用更小的模型甚至纯代码；执行阶段不经过大模型 |
| 缺点与失败模式 | **计划质量是瓶颈**；计划基于过时的假设（第 2 步的结果本该改变第 3 步怎么做）；**失败 → 重规划 → 再失败**的死循环，所以重规划必须有上限（练习 1）；计划可能长得离谱，所以执行步数也要有上限 |
| 成本与延迟 | 执行器是纯代码时：1 次规划 + k 次重规划 + 1 次汇总；执行器是 Agent 时再加上每步的调用。Demo：3 次调用 |
| 代表 | [Plan-and-Solve Prompting](https://arxiv.org/abs/2305.04091)（Wang 等，ACL 2023）；LangChain 2023 年的博客 [Plan-and-Execute Agents](https://www.langchain.com/blog/plan-and-execute-agents)（灵感来自 BabyAGI 和 Plan-and-Solve）；产品里，Gemini Deep Research 会先给出研究计划，让用户修改或批准 |

**用 agentkit 实现**：[demo.py](demo.py) 的 `run_plan_execute` 用 `complete_json` 让规划器输出 Pydantic 校验过的计划，由 `ToolRegistry.execute` 逐步执行；练习 1 的 `PlanExecuteAgent` 实现了带校验、重规划上限、步数上限和执行轨迹的完整版本。核心骨架：

```python
class PlanStep(BaseModel):
    id: str
    tool: Literal["get_weather", "get_weather_by_airport"]   # 用枚举收窄：计划里不可能出现不存在的工具
    args: dict[str, str]

plan = complete_json(llm, PLANNER_PROMPT.format(task=task), Plan).steps
while remaining:
    step = remaining.pop(0)
    r = registry.execute(ToolCall(id=step.id, name=step.tool, arguments=json.dumps(step.args)))
    if not r.ok and replans < MAX_REPLANS:               # 失败才回到模型
        remaining = complete_json(llm, REPLANNER_PROMPT.format(...), Plan).steps
```

> 💡 一个值得注意的演变：**"计划"正在从一种独立架构，变成 ReAct 里的一个工具。** Claude Code 有待办列表类工具，LangChain v1 有内置的 To-do list 中间件（LangGraph 旧的 Plan-and-Execute 教程页现在就跳转到这里）。模型在 ReAct 循环里自己维护一份显式计划，既保留了随机应变的能力，又有了全局视野。

### 2.3 ReWOO（以及 LLMCompiler）：一次规划，执行期间不回到模型

**一句话**：规划时就把每一步的参数写好，用变量（`#E1`、`#E2`）引用前面步骤的结果；执行期间完全不回到模型，最后一次性汇总。

```mermaid
flowchart LR
    T["任务"] --> P["Planner LLM<br/>E1 = 查北京<br/>E2 = 查上海<br/>E3 = 比较 E1 和 E2"]
    P --> W["Worker 纯代码<br/>按顺序执行 代入变量<br/>无依赖的步骤可并行"]
    W --> S["Solver LLM<br/>根据全部证据作答"]
    S --> Out["输出"]
```

**运行流程**：① Planner 一次写出整张计划，后面的步骤用 `#E1` 这样的占位符引用前面的结果；② Worker 用代码逐步执行、替换占位符；③ Solver 拿到计划和全部证据，写出答案。模型只被调用两次。

| 维度 | 说明 |
|---|---|
| 适用 | 工具结果只提供数据、**不会改变"下一步做什么"**的任务：多跳查询、批量取数、固定口径的报表 |
| 优点 | 模型调用次数固定（通常 2 次）；中间观察不进规划器的上下文，token 很省：论文在 HotpotQA 上报告了 **5 倍 token 效率**和 4% 的准确率提升，并演示了把推理能力从 175B 的 GPT-3.5 迁移到 7B 的 LLaMA；无依赖的步骤可以并行 |
| 缺点与失败模式 | **不能随机应变**：原版 ReWOO 没有重规划，中间结果出乎意料时只能带着错误结果硬答（换成 Demo 里的广州接口维护，Solver 拿到的是一条报错）；占位符替换和格式解析是新的故障点；规划器必须一次写对 |
| 成本与延迟 | 2 次模型调用 + N 次工具调用；延迟 ≈ 规划 + 工具（可并行）+ 汇总 |
| 代表 | [ReWOO](https://arxiv.org/abs/2305.18323)（Xu 等，2023，arXiv 预印本）；[LLMCompiler](https://arxiv.org/abs/2312.04511)（Kim 等，ICML 2024）把计划表示成任务依赖图并行执行，相比 ReAct 报告了最高 3.7 倍的延迟加速、6.7 倍的成本节省和约 9% 的准确率提升 |

**用 agentkit 实现**（草图，已在本地用 ScriptedLLM 跑通）：

```python
class Step(BaseModel):
    var: str                      # "E1"
    tool: str
    args: dict[str, str]          # 值里可以写 "#E1"，执行时替换成 E1 的结果

plan = complete_json(llm, f"为任务写出完整计划，后面的步骤用 #E1、#E2 引用前面步骤的结果。\n任务：{task}", Plan)
evidence: dict[str, str] = {}
for s in plan.steps:              # 执行期间不调用模型
    args = {k: re.sub(r"#(E\d+)", lambda m: evidence[m.group(1)], v) for k, v in s.args.items()}
    call = ToolCall(id=s.var, name=s.tool, arguments=json.dumps(args, ensure_ascii=False))
    evidence[s.var] = registry.execute(call).content
answer = complete(llm, f"任务：{task}\n证据：{json.dumps(evidence, ensure_ascii=False)}")
```

### 2.4 Reflection / Reflexion：做完之后挑错，再改

**一句话**：做完之后，让同一个或另一个模型对照标准挑错，再按意见修改；Reflexion 更进一步，把"失败的教训"写进记忆，下一次尝试时带上。

```mermaid
flowchart LR
    T["任务"] --> G["生成者<br/>写稿 或 执行任务"]
    G --> C{"批评者<br/>对照外部依据挑错"}
    C -->|"有问题 + 具体意见"| G
    C -->|"通过"| Out["输出"]
    C -->|"意见重复 或 到达轮数上限"| H["停止<br/>返回当前版本 或 转人工"]
```

**两种典型做法**：

- **Self-Refine**（[Madaan 等](https://arxiv.org/abs/2303.17651)，NeurIPS 2023）：同一个模型依次扮演生成者、反馈者、修改者，在单个任务内迭代；在 7 个任务上平均提升约 20 个百分点（绝对值）。
- **Reflexion**（[Shinn 等](https://arxiv.org/abs/2303.11366)，NeurIPS 2023）：跨多次尝试，把语言形式的反思存进情景记忆，下次尝试时读出来；配合单元测试这类外部信号，在 HumanEval 上 pass@1 达到 91%（当时 GPT-4 是 80%）。

**必须知道的反例**：[Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/abs/2310.01798)（Huang 等，ICLR 2024）发现，在推理任务上，**没有外部反馈**的"自我纠正"帮助不大，有时甚至让结果更差。所以结论是：**Reflection 的收益主要来自外部依据**，比如测试结果、校验器、工具返回的数据、检索到的证据；"模型自己觉得哪里不对"并不可靠。

| 维度 | 说明 |
|---|---|
| 适用 | 有客观检查手段的任务：代码（跑测试）、结构化输出（Schema 校验）、有事实依据的报告（对照数据） |
| 优点 | 通用，能叠加在任何架构之上；在有外部反馈时，质量提升有据可查 |
| 缺点与失败模式 | 没有外部信号时效果不稳定；**原地打转**：同一条意见反复出现；**来回摇摆**：A 改成 B、B 又改回 A；批评者本身出错或过于挑剔，导致无休止返工；成本翻倍 |
| 成本与延迟 | 每多一轮 = 1 次批评 + 1 次修改。Demo 真实运行时初稿一次通过，也比 ReAct 多花了 1 次调用、约 1,000 token，这是"保险费" |
| 代表 | Self-Refine、Reflexion；LangChain 博客 [Reflection Agents](https://www.langchain.com/blog/reflection-agents) 对比了基础 Reflection、Reflexion 和 LATS |

**用 agentkit 实现**：`evaluator_optimizer`（[agentkit/workflows.py](../../agentkit/workflows.py)，06 §2.5）就是这个循环。Demo 的 `run_reflection` 里有两个值得借鉴的设计：

1. **批评者手里有外部依据**：它拿到的是工具返回的原始数据，按数据核对，而不是凭感觉挑刺；
2. **代码检查优先**：行数、城市是否都提到、有台风预警时是否提醒了——这些用代码判断，免费、确定、不会被说服；代码查不出的才交给模型。

`evaluator_optimizer` 不会发现"同一条意见又出现了"。练习 2 的 `reflect_loop` 会补上这一点：意见重复时提前停止，不再白白烧钱。

### 2.5 CodeAct：用代码作为行动空间

**一句话**：模型不再每次输出一个 JSON 工具调用，而是写一段代码来调用工具、做循环、做条件判断和数据处理；代码的输出或报错作为观察返回。

```mermaid
flowchart LR
    T["任务"] --> M["模型<br/>写一段 Python"]
    M --> X["沙箱执行<br/>调用工具函数 循环 过滤"]
    X --> O["观察<br/>只返回 print 的结果 或 报错"]
    O --> M
    M -->|"完成"| F["最终回答"]
```

对比一下"找出 20 个城市里明天会下雨的"：

- **JSON 工具调用**：20 次调用（或一次并行 20 个），20 份完整的天气数据全部进入上下文；
- **CodeAct**：一个动作就够了，中间数据留在沙箱里，只有结果进入上下文：

```python
rainy = [c for c in cities if get_weather(c, "10-16")["降水概率"] > 50]
print(rainy)
```

| 维度 | 说明 |
|---|---|
| 适用 | 需要组合大量工具调用、循环、数据处理的任务；工具数量很多（用代码按需导入）；数据分析 |
| 优点 | 一个动作能表达循环和条件，动作更少；中间数据不进上下文——Anthropic 的 [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) 给出的例子里，token 从 150,000 降到 2,000（节省 98.7%）；模型本来就擅长写代码；报错信息本身就是很好的反馈 |
| 缺点与失败模式 | **必须有沙箱**：执行模型写的代码是最大的攻击面（文件系统、网络、资源耗尽）；**权限审批变难**：一段代码里做了什么，比一次工具调用难审；调试更难；小模型写代码的能力差 |
| 成本与延迟 | 步数更少，但每步输出更长；需要沙箱基础设施，冷启动会带来延迟 |
| 代表 | [CodeAct](https://arxiv.org/abs/2402.01030)（Wang 等，ICML 2024）：在 17 个模型上，相比 JSON 和文本格式的动作，成功率最高高出 20%；Hugging Face [smolagents](https://huggingface.co/docs/smolagents/index) 的 `CodeAgent`；Anthropic 的 Code execution with MCP（文中提到 Cloudflare 把同一思路叫作 "Code Mode"） |

**用 agentkit 实现**：agentkit 没有内置沙箱，也**不应该**在本进程里 `exec()` 模型写的代码。正确的形状是一个高风险工具，背后连着隔离环境（容器、gVisor、Firecracker），安全细节见 [第 09 课](../09_security/README.md)：

```python
@tool(risk="dangerous", timeout_s=30)
def run_python(code: Annotated[str, Field(description="要执行的 Python 代码，用 print 输出结果")]) -> str:
    """在隔离沙箱里执行 Python 代码，返回 stdout/stderr。沙箱里预置了 get_weather(city, date) 函数。"""
    return sandbox.run(code)   # sandbox 是你的隔离执行服务：无网络或网络白名单、只读文件系统、CPU/内存/时间上限
```

### 2.6 树搜索（Tree of Thoughts / LATS）（📖 选读，简要了解）

**一句话**：不只走一条路，而是在每个决策点展开多个候选，给它们打分，沿着有希望的分支继续，没希望的就剪掉或回溯。

```mermaid
flowchart TB
    R["任务"] --> A1["方案 A<br/>评分 0.8"]
    R --> A2["方案 B<br/>评分 0.3 剪枝"]
    R --> A3["方案 C<br/>评分 0.6"]
    A1 --> B1["A-1<br/>测试通过"]
    A1 --> B2["A-2<br/>评分 0.4"]
    A3 --> B3["C-1<br/>评分 0.5"]
    B1 --> Out["选中 A-1"]
```

- [Tree of Thoughts](https://arxiv.org/abs/2305.10601)（Yao 等，NeurIPS 2023）：在"24 点"游戏上，GPT-4 用思维链只解出 4%，用 ToT 解出 74%。
- [LATS](https://arxiv.org/abs/2310.04406)（Zhou 等，ICML 2024）：把蒙特卡洛树搜索用在 Agent 上，结合模型打分和自我反思；HumanEval 上 GPT-4 的 pass@1 达到 92.7%。

| 维度 | 说明 |
|---|---|
| 适用 | 有可靠评分信号、而且**可以回退**的问题：解谜、代码生成（用测试打分）、离线规划 |
| 优点 | 能跳出单条推理路径的局部错误；难题上提升明显 |
| 缺点与失败模式 | 成本是 ReAct 的数倍到数十倍；评分函数不可靠时，就是在"精确地选错"；**要求环境可以回退**：在真实系统里"试探性地"发一封邮件、下一个单，是撤不回来的 |
| 成本与延迟 | ≈ 分支数 × 深度 × (生成 + 评估) |
| 企业里怎么用 | 线上系统很少直接用完整的树搜索。最常见的简化版是 **best-of-N**：并行生成 N 个候选，用测试或校验器挑最好的一个，相当于深度为 1 的树搜索 |

```python
def best_of_n(generate, score, n=4):
    candidates = parallel([generate] * n)   # agentkit.workflows.parallel，并行生成
    return max(candidates, key=score)       # score 最好是代码：跑测试、查规则
```

### 2.7 HITL：带人工检查点的架构

**一句话**：在"不可逆、高风险、模型没把握"的节点暂停，把状态存盘，等人批准、修改或拒绝之后再继续。

Anthropic 在 [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents) 里写道，Agent 可以 "pause for human feedback at checkpoints or when encountering blockers"。HITL 不是一种独立的推理方式，而是**叠加在任何架构上的一层**。

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as 检查点存储
    participant Q as 审批队列
    participant H as 审批人
    A->>A: 模型决定调用 refund_order
    A->>S: 风险等级 dangerous 状态落盘 暂停
    A->>Q: 提交审批 金额 理由 上下文
    Note over A: 进程可以退出 不占资源
    H->>Q: 一小时后 批准
    Q->>A: approve run_id
    A->>S: 读取状态 从断点继续
    A->>A: 执行 refund_order 生成回复
```

**检查点放在哪里**：

| 位置 | 做法 | 真实例子 |
|---|---|---|
| 审批计划 | 计划先给人看，改好再执行 | Gemini Deep Research 先给研究计划让用户修改或批准；Claude Code 的 plan 模式 |
| 审批动作 | 高风险工具调用前暂停 | agentkit 的 `PermissionPolicy`；OpenAI《A practical guide to building agents》建议在**超过失败阈值**和**高风险动作**（如取消订单、大额退款、付款）时让人介入 |
| 审阅输出 | 结果发出之前人工抽检 | 对外邮件、合同条款 |
| 模型主动求助 | 缺信息或没把握时提问 | LangChain 在 [ambient agents](https://www.langchain.com/blog/introducing-ambient-agents) 一文里总结的三种人机交互：Notify（通知）、Question（提问）、Review（审批） |
| 人直接接手 | 人来操作，Agent 暂停 | OpenAI Operator 遇到登录、支付、验证码时交还给用户 |

| 维度 | 说明 |
|---|---|
| 适用 | 资金、合规、对外发送、删除等不可逆动作；模型置信度低的判断 |
| 优点 | 把最坏情况的损失限制在"人看过"的范围内；审批记录本身就是审计证据 |
| 缺点与失败模式 | **审批疲劳**：什么都要批，人就会闭眼点同意——只对真正高风险的动作审批，并用沙箱减少不必要的审批（Anthropic 的 [Claude Code 沙箱文章](https://www.anthropic.com/engineering/claude-code-sandboxing) 报告，内部使用中沙箱让权限提示减少了 84%）；**审批信息不足**：只给看 `refund(order=A1001)`，没有金额、理由和上下文；**同步阻塞**：用 `input()` 等人，一重启审批就丢了 |
| 成本与延迟 | 模型调用不变，但端到端时间加上了"等人"的时间，可能是几小时 |
| 代表 | LangGraph 的 [`interrupt()`](https://docs.langchain.com/oss/python/langgraph/interrupts) + `Command(resume=...)`（必须配置 checkpointer）；Claude Code 的 [权限模式](https://code.claude.com/docs/en/permission-modes) |

**一个新趋势：用模型代替人审批一部分动作。** Claude Code 的 `auto` 模式由第二个模型（分类器）替你审查动作（2026-09 核实，见其权限模式文档）。这相当于把 HITL 分了层：低风险自动放行，中风险由模型审，高风险才找人。

**用 agentkit 实现**（已在本地跑通）：`PermissionPolicy` 在高风险工具调用前抛 `PauseRun`，状态由检查点保存，之后用 `approve` 从断点继续。审批是异步的，详见 [第 09 课](../09_security/README.md)。

```python
agent = Agent(llm, [refund_order], hooks=[PermissionPolicy(ask_risks={"dangerous"})])
r = agent.run("给订单 A1001 退款")                    # r.status == "paused"，r.pending_approval 是待批的调用
r = agent.approve(r.run_id, True, by="alice", comment="已核实签收记录")   # 审批记录写入 approval_log，从断点继续
```

### 2.8 组合：真实系统很少只用一种

上面这些不是互斥的选项，而是可以拼装的积木：

- **ReAct + 待办工具**：主循环边做边看，同时维护一份显式计划（编码 Agent 的常见形态）；
- **Plan-and-Execute，执行器是 ReAct**：全局按计划走，每一步内部随机应变；
- **任意架构 + Reflection**：只要有外部检查手段（测试、校验器），就在输出前加一轮挑错；
- **任意架构 + HITL**：在不可逆动作前加检查点；
- **ReAct + CodeAct**：同一个 Agent 既能调普通工具，也能在需要循环和数据处理时写代码。

§6 的总表和决策树会把它们放在一起比较。

## 3. 多 Agent 的拓扑

先说清楚：多 Agent 很贵。Anthropic 报告多 Agent 系统消耗的 token 约为普通聊天的 15 倍，而且有上下文割裂、错误传播等问题，这些代价和"什么时候值得"在 [第 06 课 §2.7](../06_orchestration/README.md) 讲过，本课不重复。本节只回答一个问题：**决定要用多个 Agent 之后，它们之间怎么连？**

### 3.1 看拓扑的三个问题

| 问题 | 可能的答案 |
|---|---|
| **通信方式**：Agent 之间怎么传话？ | 函数调用（一问一答）、消息传递（转交整段对话）、共享状态（读写同一块黑板）、事件（发布/订阅、队列） |
| **状态共享**：谁能看到什么？ | 私有上下文（只看到别人写给它的任务）、共享对话历史、共享结构化状态、外部存储 |
| **控制权转移**：谁说了算？ | 保留（主管始终掌控）、移交（handoff 之后由对方直接面对用户）、无中心（谁能做谁接手） |

各家框架对这些拓扑的叫法不统一，下面是对照（2026-09 核实，框架迭代很快，以官方文档为准）：

| 本课 | LangChain v1 文档 | OpenAI 指南 / Agents SDK | Microsoft Agent Framework | Google ADK |
|---|---|---|---|---|
| 路由 + 专家 | Router | —（分诊 + handoff 实现） | — | Coordinator and dispatcher |
| 主管 / 层级 | Subagents | Manager（agents as tools） | Magentic | Hierarchical task decomposition |
| 网络 / 群体 | Handoffs（另有 langgraph-swarm 库） | Decentralized（handoffs） | Handoff | — |
| 黑板 / 共享状态 | — | — | Group Chat（共享对话，最接近） | — |
| 事件驱动 | —（LangChain 博客称之为 ambient agents） | — | — | — |

来源：[LangChain Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent)、[OpenAI 指南](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)、[MAF 编排模式](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/)、[ADK 模式](https://adk.dev/workflows/patterns/)。

### 3.2 路由 + 专家

**一句话**：入口先分类，把请求整个交给一个专家处理；专家之间互不通信。

```mermaid
flowchart LR
    U["用户请求"] --> R{"路由器<br/>规则优先 小模型兜底"}
    R -->|"账单"| E1["账单专家<br/>账单工具"]
    R -->|"技术"| E2["技术专家<br/>日志和知识库"]
    R -->|"其他"| E3["通用助手<br/>或转人工"]
    E1 --> Out["回复用户"]
    E2 --> Out
    E3 --> Out
```

- **通信**：一次性分发，单向；**状态**：专家各自私有，通常只拿到原始请求；**控制权**：分发之后就交给专家（LangChain 的 Router 模式也允许分发给多个专家，再合成结果）。
- **适用**：请求类别清晰、各类的处理方式差别很大：客服分流、按难度分给大小模型。
- **优点**：简单、便宜；每个专家的提示词和工具集都小而专；天然的权限隔离。
- **失败模式**：分错类，后面全错；**跨类请求**（"退款，顺便改一下收货地址"）只能落到一个专家手里；专家发现"这不归我管"时无路可退——要么给它一个"退回路由器"的出口，要么升级成 §3.4 的网络拓扑。
- **实现**：06 §2.2 的 `route()` 和 06 练习 1 的 `hybrid_route` 负责分类，把各分支换成不同的 `Agent` 即可。

### 3.3 主管 / 层级（Supervisor / Hierarchical）

**一句话**：一个主管 Agent 把子任务委派给专家（专家被包装成工具），收回结果后自己决定下一步和最终答复；专家太多时，就让主管去管主管，形成层级。

```mermaid
flowchart TB
    U["用户"] <--> S["总主管"]
    S -->|"task"| L1["研究组长"]
    S -->|"task"| L2["写作组长"]
    L1 -->|"task"| W1["检索员 A"]
    L1 -->|"task"| W2["检索员 B"]
    L2 -->|"task"| W3["撰稿人"]
    W1 -->|"摘要"| L1
    W2 -->|"摘要"| L1
    W3 -->|"草稿"| L2
    L1 -->|"结论"| S
    L2 -->|"成稿"| S
```

- **通信**：函数调用式，一问一答（task 进，结果出）；**状态**：专家只看到上级写的 task，返回摘要，天然做到上下文隔离；**控制权**：始终在主管手里，只有主管面对用户。
- **为什么要层级**：主管要管的专家太多，本身就会出现"工具过载"（06 §2.7）。再分一层可以缓解，代价是每多一层就多一次"传话"，信息损耗和延迟逐层累加。
- **代表**：
  - Anthropic 的研究系统：主导 Agent 加并行子 Agent（§4.1）；
  - [Magentic-One](https://arxiv.org/abs/2411.04468)（微软，2024）：Orchestrator 维护一份 **Task Ledger**（事实、猜测、计划）和一份 **Progress Ledger**（每一步的进展自检），指挥 WebSurfer、FileSurfer、Coder、ComputerTerminal 四个专家；发现进展停滞时先反思，再更新计划。这是"主管 + Plan-and-Execute"的组合。
- **失败模式**：主管成为瓶颈，所有信息都经过它，它的上下文会膨胀；task 写得不完整；专家的结论被主管当成事实继续推理；委派深度失控（专家又委派专家……）。
- **实现**：`agent_as_tool`（06 §2.7）。层级就是：专家本身也是一个挂着 `agent_as_tool` 的主管。别忘了限制委派深度。

### 3.4 网络 / 群体（Network / Swarm，handoff）

**一句话**：没有固定的主管，每个 Agent 都可以把对话和控制权**转交**给更合适的同伴，接手的 Agent 直接面对用户。

```mermaid
flowchart LR
    U(("用户")) <-.->|"始终和当前活跃的 Agent 对话"| ACT["当前活跃 Agent"]
    T["分诊"] -->|"转交"| B["改签"]
    T -->|"转交"| C["取消"]
    T -->|"转交"| F["常见问题"]
    B -->|"转交"| C
    B -->|"转回"| T
    C -->|"转回"| T
    F -->|"转回"| T
```

- **通信**：handoff，也就是转交整段对话。在 OpenAI Agents SDK 里，[handoff](https://openai.github.io/openai-agents-python/handoffs/) 被表示成一个工具，模型调用它就把控制权交出去。**状态**：通常共享完整的对话历史，接手者能看到之前发生的一切。**控制权**：移交，谁接手谁说话；系统要记住"当前是谁在服务"，下一轮用户消息直接交给它（[langgraph-swarm](https://github.com/langchain-ai/langgraph-swarm-py) 就是这么做的）。
- **代表**：OpenAI [Swarm](https://github.com/openai/swarm)（2024，标注为实验性、教学用途，已被 Agents SDK 取代；配套 cookbook 用 routines 和 handoffs 两个概念讲解）；OpenAI 的 [客服 Agent Demo](https://github.com/openai/openai-cs-agents-demo)（§4.4）；AutoGen 的 Swarm 团队。
- **适用**：对话式服务里，不同阶段需要不同的专家**直接**服务用户。
- **失败模式**：**踢皮球**（A 转给 B，B 又转回 A）——要限制转交次数、记录转交链；转交时丢状态；每个 Agent 都得能判断"这不归我管"；没有一个地方掌握全局，难以统一汇总和审计。
- **实现**（agentkit 没有内置 handoff，下面是一个已跑通的草图）：用一个钩子拦截 `transfer_to_xxx` 工具调用，中止当前 Agent，由外层循环切换到目标 Agent，并带上完整历史。

```python
class Handoff(Hook):
    """模型调用 transfer_to_xxx 时，中止当前 Agent，把"转给谁"带出去。"""
    def before_tool(self, state, call, tool):
        if call.name.startswith("transfer_to_"):
            raise StopRun("handoff", call.name.removeprefix("transfer_to_"))

def run_swarm(agents: dict[str, Agent], active: str, user_input: str, max_handoffs: int = 3):
    history: list = []
    for _ in range(max_handoffs + 1):
        r = agents[active].run(user_input, history=history)
        if r.stop_reason != "handoff":
            return active, r                         # 下一轮用户消息继续交给 active
        active, history = r.output, r.history        # 新 Agent 接手，带着完整对话历史
        user_input = "（系统：对话已转交给你，请继续处理上面用户的请求）"
    raise RuntimeError("转交次数过多：可能在踢皮球")
```

`StopRun` 会让 agentkit 给没执行的 `transfer_to_xxx` 补一条"未执行"结果，所以带着 `r.history` 继续对话时消息协议仍然合法（第 04 课讲过孤立的 tool 消息为什么会导致 400）。

### 3.5 黑板 / 共享状态（Blackboard）

**一句话**：Agent 之间不直接对话，而是读写同一块"黑板"（共享的结构化状态）；每个 Agent 看到自己能处理的内容就动手，把结果写回黑板；由控制器决定下一个谁上。

这个思想比大模型早得多：1980 年的 Hearsay-II 语音理解系统（Erman 等，ACM Computing Surveys）就用黑板让多个"知识源"协同工作。

```mermaid
flowchart TB
    BB[("黑板<br/>任务 事实 假设 待办 结论<br/>每条带作者 版本 来源")]
    A1["检索 Agent"] <-->|"读写"| BB
    A2["分析 Agent"] <-->|"读写"| BB
    A3["审核 Agent"] <-->|"读写"| BB
    CT["控制器<br/>规则 或 LLM"] -.->|"看黑板状态 选下一个"| A1
    CT -.-> A2
    CT -.-> A3
```

- **通信**：间接，只通过共享状态；**状态**：共享、结构化、可持久化；**控制权**：控制器根据黑板状态选下一个，或者各 Agent 订阅自己关心的条目、自行触发。
- **代表**：
  - [MetaGPT](https://arxiv.org/abs/2308.00352)（Hong 等，ICLR 2024）：共享消息池 + 发布-订阅，每个角色只订阅和自己相关的消息（比如架构师关注产品经理写的需求文档）；
  - 2025 年的研究重新拾起这个架构：Han 与 Zhang 的[黑板式多 Agent 系统](https://arxiv.org/abs/2507.01701)在推理和数学任务上表现有竞争力，同时 token 更少；Salemi 等在[数据发现任务](https://arxiv.org/abs/2510.01285)上报告端到端成功率相对提升 13%~57%。两篇都是 arXiv 预印本。
- **适用**：多个专家围绕同一份不断演进的"工作底稿"协作；贡献顺序没法预先确定；需要可审计的中间状态。
- **优点**：解耦，新增一个专家不用改其他专家；状态可观察、可持久化、可回放；天然支持异步。
- **失败模式**：**写冲突**（两个 Agent 同时改一条）→ 加版本号或乐观锁（[第 13 课](../13_distributed_concurrency/README.md)）；**黑板膨胀**；**错误扩散**：一个 Agent 写入的错误结论被所有人当成事实 → 每条都要带来源和置信度；**调度停滞**：没人认领的条目一直挂着。
- **实现草图**：黑板就是两个工具，写入记录来源 run_id，方便追溯（已跑通）：

```python
BOARD: dict[str, dict] = {}   # 生产中放数据库：加版本号、作者、时间，写入用乐观锁

@tool
def read_board(section: Annotated[str, Field(description="分区：facts / hypotheses / todo / conclusions")]) -> str:
    """读取黑板上某个分区的全部条目。"""
    return json.dumps(BOARD.get(section, {}), ensure_ascii=False)

@tool(risk="write")
def write_board(section: Annotated[str, Field(description="分区名")], key: Annotated[str, Field(description="条目名")],
                value: Annotated[str, Field(description="内容，事实要写明来源")], ctx: ToolContext) -> str:
    """在黑板的某个分区写入或覆盖一条记录。"""
    BOARD.setdefault(section, {})[key] = {"value": value, "by_run": ctx.run_id}   # 来源由系统注入，不让模型填
    return "已写入"
```

### 3.6 事件驱动 / 环境 Agent（Ambient / Event-driven）

**一句话**：Agent 不是被人的聊天消息唤醒，而是订阅事件流（新邮件、新工单、告警、定时器），事件来了就处理，只在需要时才找人。

LangChain 的 Harrison Chase 在 2025 年 1 月的 [Introducing ambient agents](https://www.langchain.com/blog/introducing-ambient-agents) 里给了定义：ambient agent 监听事件流并据此行动，可能同时处理很多事件；人通过 Notify（通知）、Question（提问）、Review（审批）三种方式参与。

```mermaid
flowchart LR
    SRC["事件源<br/>邮件 工单 告警 定时器"] --> Q[("消息队列")]
    Q --> W["Agent 工作进程 × N<br/>每个事件一次运行"]
    W --> ACT["执行动作<br/>默认只读或只建议"]
    W -->|"需要人"| IN["人工收件箱<br/>通知 提问 审批"]
    IN -->|"人回复"| W
    ACT -.->|"动作产生的新事件要打标记<br/>防止自己触发自己"| SRC
```

- **通信**：异步事件（发布/订阅、队列）；**状态**：每个事件一次运行，运行状态靠检查点持久化，跨事件的共享状态放外部存储；**控制权**：事件路由决定谁处理，人以"收件箱"的形式异步参与。
- **和对话式 Agent 的关键区别**：
  - 没人在屏幕前等 → 对延迟不敏感，但对**吞吐**敏感；
  - 同一时刻可能有大量事件 → 并发、限流、**幂等**（同一事件重复投递不能执行两次），见 [第 08 课](../08_reliability/README.md) 和 [第 13 课](../13_distributed_concurrency/README.md)；
  - 出错时没人当场发现 → 可观测性和告警（[第 10 课](../10_observability/README.md)）；
  - 人的参与是异步的 → 暂停和恢复必须持久化。
- **适用**：告警自动初诊、邮件分拣和草拟回复、工单预处理、定时巡检。
- **失败模式**：**事件风暴**（一次故障触发 1,000 条告警 → 1,000 次 Agent 运行）→ 去重、合并、限流；**重复投递** → 用事件 ID 做幂等键；**无人值守的错误动作** → 写操作默认走 Review；**自我触发的循环**（Agent 的动作产生新事件，又触发了自己）→ 给事件标记来源，忽略自己产生的事件。
- **实现草图**（已跑通）：

```python
def handle(event: dict) -> None:                        # 由队列消费者调用
    run_id = f"evt-{event['id']}"                       # 事件 ID 当 run_id：重复投递时认得出来
    if agent.checkpointer.load(run_id) is not None:     # 处理过了就跳过（幂等）
        return
    result = agent.run(f"新工单：{event['text']}", run_id=run_id,
                       metadata={"tenant_id": event["tenant"], "user_id": "system:ambient"})
    if result.status == "paused":                       # 需要人：放进收件箱，人批了再 agent.approve(run_id, ...)
        inbox.push(run_id, result.pending_approval)
```

生产中检查点要放在共享存储里（`FileCheckpointer` 换成数据库），多个工作进程才能看到彼此处理过哪些事件。"先查再跑"之间还有并发窗口，严格的去重需要数据库唯一约束或分布式锁（第 13 课）。

事件驱动只回答了"什么时候醒来"；主动式 Agent 还要回答"醒来之后该不该打扰人"。用户模型、打扰决策和隐私边界，见[第 25 课](../25_proactive_and_frontier/README.md)。

### 3.7 拓扑对比

| 拓扑 | 通信方式 | 状态共享 | 控制权 | 谁面对用户 | 适合 | 主要风险 |
|---|---|---|---|---|---|---|
| 路由 + 专家 | 一次性分发 | 专家各自私有 | 分发后交出 | 专家 | 类别清晰、各自独立处理 | 分错类、跨类请求 |
| 主管 / 层级 | 函数调用（task → 结果） | 专家私有，返回摘要 | 始终在主管 | 主管 | 需要统一汇总、子任务可并行 | 主管成瓶颈、层层传话有损耗 |
| 网络 / 群体 | handoff 转交对话 | 共享对话历史 | 移交 | 当前活跃的 Agent | 多阶段的对话式服务 | 踢皮球、转交丢状态 |
| 黑板 | 读写共享状态 | 共享的结构化状态 | 控制器或自主认领 | 通常不直接面对 | 围绕同一份工作底稿协作 | 写冲突、错误结论扩散 |
| 事件驱动 | 异步事件、队列 | 外部存储 + 检查点 | 事件路由 | 没人在线，人通过收件箱参与 | 后台自动化 | 事件风暴、重复执行、错误没人发现 |

跨组织、跨厂商的 Agent 互相调用时，通信需要一个标准协议。[A2A](https://a2a-protocol.org/latest/)（Agent2Agent）就是为此设计的：用 Agent Card 描述能力，用 Task 表示有状态的工作单元，远端 Agent 对调用方是黑盒。它已由 Google 捐给 Linux 基金会。生产架构里怎么用它，见 [第 12 课](../12_production_architecture/README.md)。

## 4. 真实产品的架构拆解

> 标注说明：✅ 表示有官方公开资料（附链接）；🔍 表示基于公开资料的推断。产品迭代很快，以下内容于 2026 年 9 月核实。

### 4.1 深度研究 Agent（Deep Research）

```mermaid
flowchart TB
    U["用户问题"] --> SC["澄清需求 制定研究计划<br/>可让用户修改或批准"]
    SC --> LD["主导 Agent<br/>拆分研究方向 把计划存入外部记忆"]
    LD --> S1["子研究员 1<br/>搜索 阅读 压缩"]
    LD --> S2["子研究员 2"]
    LD --> S3["子研究员 3 到 5"]
    S1 -->|"浓缩后的发现"| LD
    S2 --> LD
    S3 --> LD
    LD -->|"信息还不够"| LD
    LD -->|"足够了"| CT["引用环节<br/>给每个结论找出处"]
    CT --> R["带引用的报告"]
```

**公开资料**：

- ✅ **Anthropic Research**（[How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)，2025-06）：采用编排者-执行者模式。主导 Agent 制定计划后，并行启动 3~5 个子 Agent，每个子 Agent 又会并行调用 3 个以上工具；子 Agent 像"智能过滤器"一样迭代搜索，只把结果交回来。因为上下文超过 200,000 token 会被截断，主导 Agent 会把计划存进 Memory。研究结束后，由一个 CitationAgent 给报告标出引用。Opus 4 做主导、Sonnet 4 做子 Agent 的多 Agent 系统，在内部研究评估上比单 Agent 的 Opus 4 高出 90.2%；并行化让复杂查询的研究时间最多缩短了 90%。
- ✅ **OpenAI deep research**（[Introducing deep research](https://openai.com/index/introducing-deep-research/)，2025-02）：由一个针对网页浏览和数据分析优化的 o3 版本驱动，通过端到端强化学习训练，学会了规划和执行多步轨迹、必要时回溯；一次任务耗时 5~30 分钟，输出带引用。**官方没有公开它是否使用多 Agent 架构。** 2025 年 7 月，它和 Operator 一起并入了 ChatGPT agent。
- ✅ **Gemini Deep Research**（[Google 博客](https://blog.google/products/gemini/google-gemini-deep-research/)，2024-12；[产品页](https://gemini.google/overview/deep-research/)）：先把问题变成多步研究计划，由用户修改或批准；官方提到一个异步任务管理器，在规划器和任务模型之间维护共享状态，出错时可以恢复，不必整个任务重跑。
- ✅ **开源实现 open_deep_research**（[langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research)）：分三个阶段。Scope：澄清需求、写研究简报；Research：主管按需启动若干并行子 Agent，各自上下文隔离，结果压缩后再返回；Write：一次性写出报告。

**关键设计决策**：

1. **用在广度优先的问题上**：子问题可以并行，这正是多 Agent 最划算的场景（06 §2.7）。
2. **子 Agent 是过滤器**：交回浓缩的发现，而不是原始网页。这本质上是上下文隔离。
3. **计划外置**：计划写进外部记忆，上下文被截断或压缩后，主导 Agent 也不会"忘了自己在干什么"。这就是 §5 的工作记忆。
4. **引用单独做**：由专门的环节核对出处，是一种以证据为外部依据的 Reflection。
5. **先澄清、再研究**：计划审批（Gemini）和需求澄清（open_deep_research）是放在计划层面的 HITL。一次研究要跑几十分钟，方向错了代价很大。
6. **两条路线**：显式的多 Agent 编排（Anthropic、open_deep_research），和把规划-搜索-回溯训练进单个模型（OpenAI 的公开描述）。🔍 可以预期两者会融合：模型越强，外部编排越薄。

**架构拆解**：主管 / 层级 + Plan-and-Execute + 并行 + Reflection（引用核对）+ 计划层面的 HITL。

### 4.2 编码 Agent（Claude Code / Codex 类）

```mermaid
flowchart TB
    U["开发者"] <--> L["ReAct 主循环"]
    MEM["项目记忆<br/>CLAUDE.md 或 AGENTS.md"] --> L
    L <--> CTX["上下文管理<br/>自动压缩"]
    L --> TD["待办列表工具<br/>轻量规划"]
    L --> SUB["子 Agent<br/>独立上下文窗口"]
    L --> PERM{"权限层<br/>模式 + 规则 + 审批"}
    PERM --> TOOLS["工具<br/>搜索 读 精确编辑 执行命令 网页"]
    TOOLS --> SB["沙箱<br/>文件系统隔离 + 网络隔离"]
    SB -->|"测试 构建 lint 的输出"| L
```

**公开资料**：

- ✅ **Claude Code**（官方文档 [code.claude.com/docs](https://code.claude.com/docs/en/tools-reference)）：
  - 内置工具包括 Read、Write、Edit、Glob、Grep、Bash、WebFetch、WebSearch、派生子 Agent 的 Agent 工具（早期叫 Task）等；
  - [权限模式](https://code.claude.com/docs/en/permission-modes)：`default`（界面上叫 Manual，编辑和执行命令前都会问你）、`acceptEdits`、`plan`、`auto`（由一个分类器模型替你审查动作）、`dontAsk`（只运行预先批准的工具，面向 CI）、`bypassPermissions`（只应在隔离的容器或虚拟机里使用）；
  - 上下文快满时自动压缩：先清理旧的工具输出，再对对话做摘要；
  - [CLAUDE.md](https://code.claude.com/docs/en/memory) 记忆文件分多个层级（组织、用户、项目）；
  - [子 Agent](https://code.claude.com/docs/en/sub-agents) 在自己的上下文窗口里运行，有独立的系统提示词、工具和权限，可以并行；
  - [沙箱](https://www.anthropic.com/engineering/claude-code-sandboxing)（2025-10）：文件系统隔离加网络隔离，内部使用中权限提示减少了 84%。
- ✅ **OpenAI Codex**：
  - 云端版（[Codex 系统卡](https://cdn.openai.com/pdf/8df7697b-c1b2-4222-be00-1fd3298f351d/codex_system_card.pdf)，2025-05）：每个任务运行在独立的云端容器里，预装用户的代码和开发环境，环境初始化完成后断开网络；codex-1 会引用终端日志和文件作为完成任务的证据，方便用户核验；系统卡专门把"谎称已完成任务"列为风险之一。项目约定写在 AGENTS.md 里。
  - 命令行版（[沙箱文档](https://learn.chatgpt.com/docs/sandboxing)）：沙箱模式有 `read-only`、`workspace-write`、`danger-full-access` 三档，审批策略有 `on-request` 和 `never`；macOS 用 Seatbelt，Linux 用 bubblewrap；默认限制网络访问。

**关键设计决策**：

1. **核心仍是 ReAct**，功夫都花在工具上：先搜索（Glob/Grep）再读取；编辑是精确的字符串替换，而不是整个文件重写（省 token，也不容易改坏别处）。
2. **计划是一个工具**：待办列表让模型在 ReAct 里维护显式计划，相当于轻量版的 Plan-and-Execute（§2.2）。
3. **子 Agent 用来做上下文隔离**：搜索、调研类的旁支任务在独立窗口里完成，只交回结论。Anthropic 的研究系统文章也指出，大多数编程任务之间依赖很多，**不适合**拆成并行的多 Agent，所以编码 Agent 的主体仍是单 Agent。
4. **上下文压缩是刚需**：长会话必须自动压缩（第 04 课）。
5. **项目记忆 = 程序性记忆**：CLAUDE.md / AGENTS.md 告诉 Agent 这个项目怎么构建、怎么测试、有哪些约定（§5）。
6. **安全 = 权限 × 沙箱**：权限模式决定"做之前要不要问"，沙箱决定"做了能碰到什么"。两层相互独立，缺一不可。
7. **验证闭环**：跑测试、构建、lint，把输出作为外部反馈。这是 Reflection 最有效的形态（§2.4），也是防止"谎称完成"的关键。

**架构拆解**：ReAct + 待办工具（轻量规划）+ 子 Agent（主管-专家）+ 测试反馈（Reflection）+ 权限模式（HITL）+ 沙箱（CodeAct 式的执行环境）。

想亲手搭一个：[第 24 课](../24_coding_agents/README.md)从零实现了编码 Agent 的 ACI 工具、路径边界和测试保护，以及让它跨会话接力干长任务的 harness。

### 4.3 Computer-Use / 浏览器 Agent

```mermaid
flowchart LR
    SS["截图"] --> M["多模态模型<br/>看图 推理"]
    M --> ACT["动作<br/>点击坐标 输入 按键 滚动"]
    ACT --> ENV["虚拟机 或 浏览器<br/>隔离环境"]
    ENV --> SS
    M -->|"登录 支付 验证码<br/>或重要操作"| H["交还给人<br/>或请求确认"]
```

**公开资料**：

- ✅ **Anthropic computer use**（[发布公告](https://www.anthropic.com/news/3-5-models-and-computer-use)，2024-10；[工具文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)）：模型返回截图、点击、输入、按键、滚动等动作，**由你的应用在隔离环境里执行**，再把截图作为结果返回，如此循环。也就是说，Agent 循环是开发者自己实现的，参考实现是一个带虚拟显示器的 Docker 容器。官方建议使用最小权限的专用虚拟机或容器、不放敏感数据、用域名白名单、重要操作要人工确认。发布时 OSWorld 得分是 14.9%（仅截图）。
- ✅ **OpenAI Computer-Using Agent / Operator**（[CUA](https://openai.com/index/computer-using-agent/)，2025-01）：把 GPT-4o 的视觉能力和强化学习训练出的推理能力结合起来，在截图上做"感知 → 推理 → 行动"的循环；遇到登录、支付、验证码时交还给用户，提交订单、发邮件等重要操作前会请求确认；OSWorld 38.1%、WebArena 58.1%、WebVoyager 87%。2025 年 7 月并入 ChatGPT agent。
- ✅ **[OSWorld](https://arxiv.org/abs/2404.07972)**（Xie 等，2024）：369 个真实电脑任务，人类能完成 72.36%，发布时最好的模型只有 12.24%。

**关键设计决策**：

1. **架构就是 ReAct**，只不过观察是截图、动作是鼠标键盘。每一步都是一次多模态调用，截图很费 token，延迟也高。
2. **可靠性差距仍然很大**：比直接调 API 慢得多、脆弱得多。**能用 API 就别用屏幕**（第 03 课的 ACI 原则）；Computer-Use 适合没有 API 的遗留系统。
3. **环境必须隔离**：屏幕上的任何文字都可能是提示词注入（[第 09 课](../09_security/README.md)）。
4. **HITL 是架构的一部分**，不是附加功能：接管模式和重要操作确认。
5. 🔍 很多浏览器 Agent 会同时利用页面的结构化信息（如 DOM 或无障碍树），减少对像素坐标的依赖。这是业界常见做法，具体产品的实现大多没有公开。

**架构拆解**：ReAct（多模态观察）+ 隔离执行环境 + 动作级 HITL。

### 4.4 客服 Agent

```mermaid
flowchart TB
    U["用户"] --> G["输入护栏<br/>相关性 越狱检测"]
    G --> T["分诊 Agent"]
    T -->|"转交"| E1["订单与退款专家<br/>工单工具 退款要审批"]
    T -->|"转交"| E2["政策与 FAQ 专家<br/>RAG 检索政策原文"]
    E1 -->|"不归我管"| T
    E2 -->|"不归我管"| T
    E1 --> TK[("工单 订单系统")]
    E1 -->|"触发转人工条件"| HU["人工坐席<br/>带上对话摘要"]
    E2 -->|"触发转人工条件"| HU
    T -->|"用户要求真人"| HU
```

**公开资料**：

- ✅ **OpenAI 客服 Agent Demo**（[openai-cs-agents-demo](https://github.com/openai/openai-cs-agents-demo)，2025-06，基于 Agents SDK）：航空客服场景，最初的版本包含 Triage（分诊）、Seat Booking、Flight Status、Cancellation、FAQ 五个 Agent，通过 handoff 互相转交，专家可以转回分诊；入口有 Relevance（相关性）和 Jailbreak（越狱）两个输入护栏。仓库后来重构过，Agent 的划分有变化。
- ✅ **Intercom Fin**（[转人工规则文档](https://fin.ai/help/en/articles/12399729-manage-fin-ai-agent-s-escalation-guidance-and-rules)）：既有按数据属性触发的转人工规则，也有用自然语言写的转人工指引；默认在用户明确要求真人、表现出强烈不满、或对话陷入循环时转人工。转人工规则只决定**何时**转，**转给谁**由客服系统的分配规则决定。
- ✅ **τ-bench**（[Yao 等](https://arxiv.org/abs/2406.12045)，Sierra，ICLR 2025）：模拟零售和航空客服里"工具-Agent-用户"的三方交互。gpt-4o 这样的模型成功率不到 50%，而且很不稳定：零售场景下同一任务连续 8 次都成功（pass^8）的比例不到 25%。
- ✅ **Klarna**（[2024-02 新闻稿](https://www.klarna.com/international/press/klarna-ai-assistant-handles-two-thirds-of-customer-service-chats-in-its-first-month/)）：AI 助手第一个月处理了 230 万次对话，占客服对话的三分之二，工作量相当于 700 名全职客服，解决时间从 11 分钟降到 2 分钟以内。LangChain 的[案例](https://www.langchain.com/blog/customers-klarna)说它基于 LangGraph 和 LangSmith 构建。到了 2025 年，Klarna CEO 公开表示过于看重成本导致了质量下降，要重新投入人工客服的质量（[报道](https://www.customerexperiencedive.com/news/klarna-reinvests-human-talent-customer-service-AI-chatbot/747586/)）。

**关键设计决策**：

1. **拓扑是分诊 + 专家**（handoff 或主管-专家都常见），每个专家的工具和权限最小化。
2. **知识用 RAG，事实用工具**：政策、FAQ 走检索并引用原文；订单、物流走工具实时查询。不要让模型"记住"业务数据。
3. **写操作走幂等 + 审批 + 金额阈值**：OpenAI 指南举的高风险动作例子正是取消订单、大额退款、付款。
4. **转人工是一等公民**，不是失败：触发条件要明确，交接时带上对话摘要，别让用户把问题再讲一遍。Klarna 的反复说明，全自动不是目标，服务质量才是。
5. **评估看 pass^k，不只看平均成功率**：客服要的是"每次都对"。

**架构拆解**：输入护栏 + 分诊（路由或网络拓扑）+ RAG + 带审批的写工具 + 转人工。

### 4.5 Agentic RAG

传统 RAG 是一条固定流水线：检索一次 → 生成。Agentic RAG 把几个决定交给 Agent：**要不要检索、检索什么（改写或拆分查询、选哪个库）、结果够不够（不够就再检索或换工具）、什么时候可以回答。**

```mermaid
flowchart TB
    Q["问题"] --> D{"需要检索吗"}
    D -->|"不需要"| ANS["直接回答"]
    D -->|"需要"| PL["改写或拆分查询<br/>选择数据源"]
    PL --> R["检索<br/>先按权限过滤"]
    R --> J{"结果相关且足够吗"}
    J -->|"不够 且未达上限"| PL
    J -->|"够了"| G["生成答案 + 引用"]
    G --> V{"每个结论都有出处吗"}
    V -->|"没有"| PL
    V -->|"有"| OUT["输出"]
```

**研究脉络**（✅ 均为已核实的论文）：

| 方法 | 做法 | 对应的架构 |
|---|---|---|
| [IRCoT](https://arxiv.org/abs/2212.10509)（Trivedi 等，ACL 2023） | 推理一步、检索一次，交替进行；检索指标最高提升 21 个点，问答最高提升 15 个点 | ReAct（检索就是工具） |
| [Self-RAG](https://arxiv.org/abs/2310.11511)（Asai 等，ICLR 2024） | 模型通过"反思 token"按需决定是否检索，并评价检索结果和自己的输出 | 训练进模型的 Reflection |
| [CRAG](https://arxiv.org/abs/2401.15884)（Yan 等，2024） | 一个轻量的检索评估器给出置信度，据此触发不同动作；检索结果不理想时用网页搜索补充 | Reflection + 路由 |
| [Adaptive-RAG](https://arxiv.org/abs/2403.14403)（Jeong 等，NAACL 2024） | 一个小分类器判断问题复杂度，决定不检索、单步检索还是多步检索 | 路由 |
| [Agentic RAG 综述](https://arxiv.org/abs/2501.09136)（Singh 等，2025） | 对 Agentic RAG 架构的系统分类 | — |

**关键设计决策**：

1. **最简单的 Agentic RAG**：把 `search` 做成工具交给 ReAct Agent。第 04 课的 `recall` 工具就是一个例子。
2. **先路由**：简单问题别走多跳检索（Adaptive-RAG 的思路），省钱也省时间。
3. **检索评估要便宜**：相关性判断用小模型或规则，别每次都动用大模型。
4. **设检索上限**："再搜一次"也需要刹车。
5. **权限先于检索**：Agent 自己改写的查询同样不能绕过访问控制，过滤必须在检索层强制执行（[第 15 课](../15_enterprise_rag/README.md)）。

**架构拆解**：ReAct（检索作为工具）或路由（按复杂度选策略）+ Reflection（相关性评估、引用核对）。

## 5. 记忆架构

第 04 课 [上下文与记忆](../04_context_memory/README.md) 讲了怎么实现短期记忆（截断、摘要）和长期记忆（存储、检索、隔离）。这里换一个角度：**从架构上看，一个 Agent 都有哪几种记忆，各自放在哪里。**

[CoALA](https://arxiv.org/abs/2309.02427)（Sumers 等，TMLR）把语言 Agent 的记忆分成**工作记忆**和**长期记忆**，长期记忆又分为**情景、语义、程序性**三种。结合工程实践：

```mermaid
flowchart LR
    LLM["LLM"] <--> WM["工作记忆<br/>这一次调用看到的全部内容<br/>对话历史 计划 待办 中间变量"]
    WM <-->|"检索 写入"| EP[("情景记忆<br/>发生过什么")]
    WM <-->|"检索 写入"| SE[("语义记忆<br/>知道什么")]
    PR["程序性记忆<br/>怎么做事<br/>系统提示词 规则文件 工具代码 模型权重"] --> WM
```

| 记忆类型 | 存什么 | 放在哪 | 生命周期 | 例子 | 课程 / agentkit 对应 |
|---|---|---|---|---|---|
| **短期记忆** | 当前会话的原始消息 | 上下文窗口 | 会话结束即失 | 多轮对话的 messages | `RunState.messages`；第 04 课的滑动窗口和摘要 |
| **工作记忆** | 为完成当前任务而**主动维护**的结构化状态：计划、待办、已知事实、中间变量 | 上下文里的结构化块，或草稿文件 | 任务期间 | Plan-and-Execute 的计划、ReWOO 的 `#E` 变量、编码 Agent 的待办列表、Anthropic 研究系统存进 Memory 的计划、Magentic-One 的 Task Ledger | 第 04 课的"结构化任务状态"；练习 1 的 `results` |
| **情景记忆** | 过去的具体经历："上次这么做失败了，因为……" | 外部存储，按需检索 | 长期 | Reflexion 的反思记录；[Generative Agents](https://arxiv.org/abs/2304.03442) 的记忆流（按新近度、重要性、相关性检索） | 运行记录、trace（[第 10 课](../10_observability/README.md)） |
| **语义记忆** | 关于世界和用户的事实："用户吃素" | 数据库、向量库 | 长期 | 用户画像、企业知识库 | 第 04 课的 `MemoryStore`；RAG |
| **程序性记忆** | 怎么做事：规则、技能、流程 | 系统提示词、规则文件、工具代码、模型权重 | 长期，变化慢 | CLAUDE.md、AGENTS.md、技能库 | `system_prompt`、工具本身 |

几个架构层面的要点：

1. **"短期"和"工作"记忆要分开想**：短期记忆是**被动**堆积的原始记录，会被截断、被摘要；工作记忆是**主动**维护的结构化状态，应该永远完整保留。长任务最常见的失败"忘了自己在干什么"，往往是因为把计划只放在了会被压缩的对话历史里。
2. **写入时机是一个架构决策**：由模型在对话中调用 `remember` 工具实时写入（简单，但占用主流程的延迟和注意力），还是在对话结束后由后台任务整理写入（不影响主流程，但有延迟）。
3. **程序性记忆的变更风险最大**：改一行系统提示词或规则文件，会影响之后所有的行为，应该走和代码一样的评审和发布流程（[第 16 课](../16_release_ops/README.md)）。
4. **所有长期记忆都要隔离、可删除、防投毒**：第 04 课"企业级记忆的三条硬要求"对情景、语义、程序性记忆同样适用。能让 Agent 自己改写程序性记忆（比如自动更新规则文件）的设计，投毒风险尤其大。
5. [MemGPT](https://arxiv.org/abs/2310.08560)（Packer 等，2023）借鉴操作系统的分层内存，让 Agent 自己在"快而小"的上下文和"慢而大"的外部存储之间搬运数据，可以看作把记忆管理本身也交给了 Agent。

这些机制的从零实现（Mem0 式的写入决策、Generative Agents 式的检索打分、MemGPT 式的分层记忆），以及查看、纠正、删除、防投毒这些企业要求怎么落地，见[第 18 课](../18_memory_systems/README.md)。

## 6. 总对比表与选型决策树

### 6.1 总对比表

| 类别 | 架构 | 模型在哪里思考 | 典型模型调用次数 | 延迟特征 | 可预测性 | 适合 | 最大风险 |
|---|---|---|---|---|---|---|---|
| 单 Agent | ReAct | 每一步 | N（步数） | 串行累加，历史越长越慢 | 低 | 路径未知 | 短视、打转、上下文膨胀 |
| 单 Agent | Plan-and-Execute | 开头 + 失败时 + 结尾 | 2 + 重规划次数（执行器是代码时） | 执行阶段快 | 中 | 能大致规划的多步任务 | 计划错误、重规划死循环 |
| 单 Agent | ReWOO / LLMCompiler | 开头 + 结尾 | 2（固定） | 工具可并行，最快 | 高 | 取数型、可并行的任务 | 意外时无法调整 |
| 叠加层 | Reflection | 做完之后 | 每轮 +2 | 多几轮串行 | 中 | 有客观检查手段 | 没有外部依据时无效、原地打转 |
| 单 Agent | CodeAct | 每一步，一步做很多事 | 少于 ReAct | 加上沙箱开销 | 低 | 多工具组合、数据处理 | 代码执行的安全风险 |
| 单 Agent | 树搜索 | 在每个分支上 | 分支 × 深度 × 2 | 最慢 | 低 | 有评分信号、可回退的难题 | 成本爆炸、需要可回退的环境 |
| 叠加层 | HITL | 模型想，人拍板 | 不变 | 加上等人的时间 | 高 | 不可逆、高风险动作 | 审批疲劳 |
| 多 Agent | 路由 + 专家 | 路由一次，专家各自 | 1 + 专家的调用 | 与单 Agent 相近 | 高 | 类别清晰 | 分错类 |
| 多 Agent | 主管 / 层级 | 主管 + 各专家 | 主管 + 各专家之和 | 并行时取最慢的那个；有依赖时串行 | 中 | 可并行、需汇总 | token 成倍、主管瓶颈 |
| 多 Agent | 网络 / 群体 | 当前活跃的 Agent | 视转交次数而定 | 与单 Agent 相近 | 中低 | 多阶段对话服务 | 踢皮球、丢状态 |
| 多 Agent | 黑板 | 各 Agent + 控制器 | 视轮次而定 | 可异步 | 中低 | 围绕共享底稿协作 | 写冲突、错误扩散 |
| 多 Agent | 事件驱动 | 每个事件一次运行 | 每个事件 = 一次单 Agent 运行 | 异步，看吞吐 | 中 | 后台自动化 | 事件风暴、重复执行 |

### 6.2 选型决策树

先选单 Agent 的基础架构，再决定叠加什么，最后才考虑要不要多 Agent：

```mermaid
flowchart TD
    S["新需求"] --> Q0{"开发者能提前<br/>写死步骤吗"}
    Q0 -->|"能"| WF["Workflow<br/>第 06 课"]
    Q0 -->|"不能"| Q1{"模型看到任务后<br/>能一次列出全部步骤吗"}
    Q1 -->|"能"| Q2{"步骤大多相互独立<br/>可以并行吗"}
    Q2 -->|"是"| RW["ReWOO 或 LLMCompiler"]
    Q2 -->|"否"| PE["Plan-and-Execute"]
    Q1 -->|"不能"| Q3{"需要多路径探索<br/>有可靠的打分<br/>且出错代价可承受"}
    Q3 -->|"是"| TS["树搜索 或 best-of-N"]
    Q3 -->|"否"| RA["ReAct"]
    WF --> O1{"输出能被<br/>客观检查吗"}
    RW --> O1
    PE --> O1
    RA --> O1
    TS --> O2
    O1 -->|"能"| RF["叠加 Reflection<br/>把检查结果喂回去"]
    O1 -->|"不能"| O2{"有不可逆或<br/>高风险动作吗"}
    RF --> O2
    O2 -->|"有"| HI["叠加 HITL 检查点"]
    O2 -->|"没有"| O3{"单 Agent 因上下文<br/>或工具过载明显不够用吗"}
    HI --> O3
    O3 -->|"否"| DONE["保持单 Agent"]
    O3 -->|"是"| MA["多 Agent<br/>按下一张图选拓扑"]
```

另外两个独立的问题：**动作空间**——工具很多、需要循环和数据处理、而且有可靠的沙箱时，用 CodeAct 作为行动方式，它可以和上面任何一种组合；**计划要不要单独一层**——很多时候，给 ReAct 加一个待办列表工具就够了。

选多 Agent 拓扑：

```mermaid
flowchart TD
    M["为什么需要多个 Agent"] -->|"请求类别不同<br/>各自独立处理"| R["路由 + 专家"]
    M -->|"子任务可并行<br/>需要统一汇总"| S["主管<br/>专家太多时分层"]
    M -->|"对话的不同阶段<br/>由不同专家直接服务用户"| N["网络 或 handoff"]
    M -->|"围绕同一份工作底稿<br/>贡献顺序不确定"| B["黑板"]
    M -->|"没人在线<br/>由事件触发"| E["事件驱动"]
```

练习 3 的 `choose_architecture` 就是把第一张决策树写成了代码。**决策树的意义不是选出唯一答案，而是逼你回答"为什么更简单的那个不够用"。**

## 7. 动手：运行 Demo

```bash
.venv/bin/python lessons/05_agent_architectures/demo.py            # 真实模型，约 40 秒
.venv/bin/python lessons/05_agent_architectures/demo.py --offline  # 离线剧本，无需 API key
```

真实模型运行节选（gpt-5.5）：

```text
架构 1：ReAct —— 边想边做（agentkit.Agent 本身就是 ReAct）
  执行轨迹：
    第 1 步（模型）→ get_weather(北京,10-15)  get_weather(上海,10-16)  get_weather(广州,10-17)
        ← ❌ 错误：广州气象站接口维护中（503）。可以改用 get_weather_by_airport 按机场三字码查询，例如广州白云机场是 CAN。
    第 2 步（模型）→ get_weather_by_airport(CAN,10-17)
    第 3 步（模型）→ 给出回答

架构 2：Plan-and-Execute —— 先规划，再执行，失败才重规划
  📋 规划器给出的计划（1 次模型调用）：
    s1: get_weather(北京,10-15)
    s2: get_weather(上海,10-16)
    s3: get_weather(广州,10-17)
  ⚙️  执行（由代码逐步调用工具，不经过模型）：
    ✅ s1: get_weather(北京,10-15)
    ✅ s2: get_weather(上海,10-16)
    ❌ s3: get_weather(广州,10-17) → 错误：广州气象站接口维护中（503）……
    🔁 重规划（第 1 次，1 次模型调用）→ s3: get_weather_by_airport(CAN,10-17)
    ✅ s3: get_weather_by_airport(CAN,10-17)

架构 3：Reflection —— 先写初稿，再挑错，再修改
    第 1 步（模型）→ get_weather(北京,10-15)  get_weather(上海,10-16)  get_weather(广州,10-17)
    第 2 步（模型）→ get_weather_by_airport(CAN,10-17)
    第 3 步（模型）→ 给出回答
    🔍 第 1 轮评审 · 模型检查 → ✅ 通过

对比：同一个任务，三种架构的价格和结果
  架构              模型调用  工具调用  tokens   耗时    质量检查                    模型在哪里思考
  ReAct             3         4         2705     10.7s   3/3 城 · 台风✅ · 4 行✅    每一步都由模型决定
  Plan-and-Execute  3         4         2724     14.3s   3/3 城 · 台风✅ · 4 行✅    开头规划 + 失败重规划 + 最后汇总
  Reflection        4         4         3716     11.2s   3/3 城 · 台风✅ · 4 行✅    ReAct 写稿 + 模型/代码挑错 + 修改
```

**该观察什么：**

1. **同一个意外，两种应对**：ReAct 在循环里"顺手"绕过了广州接口维护，因为每一步都回到模型；Plan-and-Execute 的执行器是代码，只能停下来花一次模型调用重规划。
2. **"先规划"在这里没有更省**：ReAct 第 1 步就**并行**调用了 3 个工具，总共只走了 3 步，历史还很短；而 Plan-and-Execute 的每次规划都通过 `complete_json` 把 JSON Schema 塞进提示词，重规划提示词又重复了一遍上下文。Plan-and-Execute 的优势要在**步数多**的任务上才显现：ReAct 的输入 token 随步数大致按平方增长，而 Plan-and-Execute 执行阶段的模型开销是零。可以在 `WEATHER` 和 `TRIP` 里多加几个城市再跑一次，观察两者差距怎么变化。
3. **Plan-and-Execute 反而最慢**：3 次模型调用全部串行，每次都带着较长的结构化提示词。"不经过模型"省的是执行阶段，不是规划本身。
4. **Reflection 的保险费**：真实运行时初稿一次就通过了，仍然多花了 1 次调用、约 1,000 token。离线模式（`--offline`）的剧本故意让初稿漏掉台风预警，你会看到第 1 轮由**代码检查**直接退回，没花一分钱模型调用；第 2 轮才由模型核对。
5. **质量检查那一栏**是几行代码写的最小评估。选架构要看这类数据，而不是看名字（[第 11 课](../11_evals/README.md)）。

## 8. 练习

打开 [`exercise.py`](exercise.py)，实现三种架构的骨架。planner、executor、critique 都是普通函数，测试完全离线、结果确定：

**任务 1：`PlanExecuteAgent`**
- 规划器返回结构化的步骤列表（`Step`）；计划要先校验：非空、元素类型正确、id 不重复；
- 执行器逐步执行，每一步是一次工具调用或一个子任务，后面的步骤能看到前面的结果；
- 某一步失败时调用重规划器，传入已完成的结果、失败的步骤、错误信息和剩余计划；新计划不能覆盖已完成的步骤；
- 重规划次数和执行步数都有上限；返回状态、停止原因、结果和完整的执行轨迹。

**任务 2：`reflect_loop(generate, critique, max_rounds, stop_when)`**
- 生成 → 批评 → 按批评修改，直到 `stop_when` 满足或达到轮数上限；
- **批评意见和之前任何一轮重复时提前停止**（归一化后比较），既能抓住"原地打转"，也能抓住 A → B → A 的来回摇摆。

**任务 3：`choose_architecture(task_profile) -> str`**
- 按步骤可预知程度、是否需要探索、是否可并行、是否需要高可靠、输出能否客观检查，选出基础架构，再叠加 `reflection` 和 `hitl`；
- 规则写在 docstring 里，和 §6.2 的决策树一一对应。

验证：

```bash
make lesson N=05                     # 全部通过即完成（共 30 个测试）
AGENTKIT_SOLUTION=1 make lesson N=05 # 用参考答案跑，确认测试本身没问题
```

## 9. 深入（📖 选读，给有余力的你）

### 9.1 架构正在被训练进模型

OpenAI deep research 和 CUA 都强调用强化学习训练，把"规划 → 行动 → 回溯"学进了模型本身；Self-RAG 把"要不要检索、检索得好不好"也训练成了模型的输出。这意味着：**外部架构应该尽量薄**。今天需要 Plan-and-Execute 才能做好的任务，明年可能一个带待办工具的 ReAct 就够了。好的做法是把架构里的每一层都当成"为弥补模型不足而加的脚手架"，定期用评估集检验：去掉这一层，效果会变差吗？

### 9.2 ReAct 的平方成本与提示词缓存

ReAct 每一步都重发全部历史，所以 N 步的总输入 token 大约是 N²/2 份"单步增量"。两个缓解手段：**提示词缓存**（历史前缀不变时，重复部分的输入更便宜、更快，见 [第 14 课](../14_cost_latency/README.md)）；**压缩旧的工具结果**（第 04 课）。这也是 CodeAct 和 ReWOO 省 token 的根本原因：中间数据不回到模型的上下文。

### 9.3 结构化输出的代价

Plan-and-Execute、ReWOO、Reflection 都依赖结构化输出（计划、评审意见）。`complete_json` 把 JSON Schema 放进提示词，还可能需要修复重试。Demo 的数据显示，这部分开销在小任务上足以抵消"执行阶段不经过模型"的节省。模型或网关支持原生结构化输出时应该优先使用。

### 9.4 多 Agent 系统为什么会失败

[Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657)（Cemri 等，2025）基于大量真实运行轨迹，把失败归为三大类：系统设计问题、Agent 之间不对齐、任务验证不足。对照 §3 的拓扑可以做一个推断（这是本课的推断，不是论文的结论）：主管型更要防"验证不足"（主管盲信专家），网络型更要防"不对齐"（转交时丢了上下文）。

### 9.5 用状态机的眼光看架构

所有架构都可以画成一张状态图：节点是"调用模型 / 执行工具 / 等人"，边是"下一步去哪"。ReAct 是一个节点的自循环；Plan-and-Execute 是"规划 → 执行（自循环）→ 重规划"；HITL 是在边上插入一个"等待外部事件"的状态。这也是 LangGraph 这类图编排框架的出发点：当你的架构开始需要分支、循环、暂停和恢复时，把它显式写成状态图，比埋在 `while` 循环里更容易看清和测试（框架对照见 [docs/framework-comparison.md](../../docs/framework-comparison.md)）。

## 10. 常见坑与反模式（📖 选读）

| 坑 | 后果 | 正确做法 |
|---|---|---|
| 按名气选架构（"大家都在用多 Agent"） | 成本翻倍，效果未必更好 | 从 ReAct 或 Workflow 开始，用评估数据证明需要更复杂的架构 |
| Plan-and-Execute 不限重规划次数 | "失败 → 重规划 → 失败"死循环 | 重规划上限 + 执行步数上限（练习 1） |
| 计划是一段自由文本 | 执行器只能靠猜，解析失败 | 结构化计划 + Schema 校验，工具名用枚举 |
| ReWOO 用在中间结果会改变计划的任务上 | 意外发生时只能带着错误结果硬答 | 换 Plan-and-Execute，或加重规划 |
| 没有外部依据的"自我反思" | 多花钱，效果不稳定甚至变差 | 让批评者拿着测试结果、校验器、原始数据去挑错 |
| Reflection 没有重复检测 | 同一条意见反复出现，白白烧钱 | 意见重复时提前停止（练习 2） |
| CodeAct 在本进程里 `exec()` | 模型写的代码能读写你的整个系统 | 隔离沙箱：容器、gVisor、Firecracker，加资源和网络限制 |
| 树搜索用在有副作用的环境 | "试探性"地发邮件、下单，撤不回来 | 只在可回退的环境里搜索；线上用 best-of-N + 验证器 |
| 什么动作都要人审批 | 审批疲劳，人闭眼点同意 | 按风险分级；用沙箱减少不必要的审批 |
| 用同步 `input()` 等审批 | 一重启审批就丢，还占着进程 | 状态落盘 + 异步 resume |
| handoff 不限转交次数 | Agent 之间踢皮球 | 转交次数上限 + 记录转交链 |
| 黑板条目没有来源 | 错误结论被所有 Agent 当成事实 | 每条记录带作者、来源、版本 |
| 事件驱动 Agent 不做幂等 | 重复投递导致动作执行两次 | 事件 ID 作为幂等键 |
| 计划只放在对话历史里 | 上下文压缩后 Agent "忘了自己在干什么" | 计划放进工作记忆（结构化状态或外部文件） |

## 11. 面试 & 设计评审问题（📖 选读）

<details>
<summary>Q1：ReAct 和 Plan-and-Execute 的本质区别是什么？各自在什么情况下更好？</summary>

- 本质区别是模型在哪里思考：ReAct 每一步都回到模型；Plan-and-Execute 开头规划一次，执行阶段不经过（大）模型，只在失败时重规划；
- ReAct 适合路径无法预知、需要随机应变的任务；代价是短视，且输入 token 随步数大致按平方增长；
- Plan-and-Execute 适合能大致规划的长任务：执行快、计划可审批、执行器可以用小模型；代价是计划质量成为瓶颈，需要重规划和上限；
- 步数少、能并行调用工具时，ReAct 未必更贵（本课 Demo 的实测）；
- 常见折中：ReAct + 待办列表工具，或者 Plan-and-Execute 的执行器用 ReAct。
</details>

<details>
<summary>Q2：ReWOO 为什么省 token？它的代价是什么？LLMCompiler 做了什么改进？</summary>

- 规划器一次写出带变量依赖的完整计划，执行期间不回到模型，中间观察不进入规划器的上下文；模型调用固定为 2 次（论文在 HotpotQA 上报告了 5 倍 token 效率）；
- 代价是无法随机应变：中间结果出乎意料时，原版没有机会调整计划；
- LLMCompiler 把计划表示成任务依赖图，无依赖的任务并行执行，并支持在中间结果出来后重新规划。
</details>

<details>
<summary>Q3：Reflection 一定能提升质量吗？怎么设计才有效？</summary>

- 不一定。Huang 等（ICLR 2024）发现，在推理任务上，没有外部反馈的自我纠正帮助不大，有时甚至更差；
- 有效的 Reflection 依赖外部依据：测试结果、Schema 校验、业务规则、工具返回的数据、检索到的证据；
- 设计要点：能用代码检查的先用代码；反馈要具体、可执行；设轮数上限；检测重复意见和来回摇摆；达到上限时有兜底（返回当前版本或转人工）。
</details>

<details>
<summary>Q4：CodeAct 有什么优势？在企业里落地要注意什么？</summary>

- 优势：一个动作能表达循环、条件和数据处理，动作更少；中间数据留在执行环境里，不进上下文，token 大幅减少；模型擅长写代码；
- 风险：执行模型写的代码是最大的攻击面；
- 落地：隔离沙箱（容器 / gVisor / Firecracker），无网络或网络白名单，只读文件系统，CPU、内存、时间上限；工具以受控的函数形式注入沙箱，身份和凭证不暴露给代码；把代码执行标记为高风险，并记录每段代码以便审计。
</details>

<details>
<summary>Q5：主管-专家、handoff、黑板三种拓扑怎么选？</summary>

- 看三个问题：通信方式、状态共享、控制权；
- 主管-专家：控制权始终在主管，专家只看到 task、返回摘要；适合需要统一汇总、子任务可并行的场景；风险是主管瓶颈和 task 写漏上下文；
- handoff：控制权移交，接手者直接面对用户、共享对话历史；适合多阶段的对话服务；风险是踢皮球和丢状态；
- 黑板：通过共享结构化状态间接通信；适合围绕同一份工作底稿、贡献顺序不定的协作；风险是写冲突和错误结论扩散，需要版本号、来源和置信度。
</details>

<details>
<summary>Q6：设计一个深度研究 Agent，你会怎么做？</summary>

- 先澄清需求、生成研究计划，并让用户确认（计划层面的 HITL）；
- 主导 Agent 拆分研究方向，把计划写进外部记忆，防止上下文截断后丢失；
- 并行启动若干子 Agent，各自在独立上下文里搜索、阅读，只返回浓缩的发现和出处；
- 主导 Agent 判断信息是否足够，不够就再派；设总预算和轮数上限；
- 单独做引用核对，确保每个结论都有出处；
- 评估：准备研究问题集，看事实准确性、引用正确率、覆盖度、token 成本；
- 权衡：多 Agent 约为聊天的 15 倍 token，只在问题足够宽、值这个钱时使用。
</details>

<details>
<summary>Q7：事件驱动的 Agent 和对话式 Agent 在架构上有什么不同？</summary>

- 触发方式：事件流而不是用户消息；没人在屏幕前等，延迟不敏感但吞吐敏感；
- 必须处理：并发和限流、事件去重与幂等（事件 ID 做幂等键）、事件风暴（合并、限流）、自我触发的循环（标记事件来源）；
- 人通过收件箱异步参与（通知、提问、审批），所以暂停和恢复必须持久化；
- 默认只读或只给建议，写操作要审批；可观测性和告警要更强，因为出错时没人当场发现。
</details>

<details>
<summary>Q8：一个 Agent 的记忆应该分几层？各放在哪里？</summary>

- 短期记忆：当前会话的原始消息，放在上下文里，会被截断或摘要；
- 工作记忆：为当前任务主动维护的结构化状态（计划、待办、中间变量），应该完整保留，不被压缩；
- 长期记忆：情景（过去的经历，如反思记录）、语义（事实，如用户偏好、知识库）、程序性（怎么做事，如系统提示词、规则文件、工具代码）；
- 长期记忆都要隔离、可删除、防投毒；程序性记忆的变更要走评审和发布流程。
</details>

## 12. 自测清单

- [ ] 我能用"模型在哪里思考"一句话区分 ReAct、Plan-and-Execute、ReWOO、Reflection、CodeAct、树搜索
- [ ] 我能说出 ReAct 的成本为什么随步数大致按平方增长，以及 Demo 里它为什么没有比 Plan-and-Execute 更贵
- [ ] 我知道 Reflection 在什么条件下有效，并能引用一个反例
- [ ] 我知道 CodeAct 的收益和它对沙箱的硬性要求
- [ ] 我能说出 HITL 检查点可以放在哪五个位置，以及如何避免审批疲劳
- [ ] 我能用"通信方式、状态共享、控制权"比较五种多 Agent 拓扑
- [ ] 我能把深度研究、编码 Agent、Computer-Use、客服、Agentic RAG 各拆成一句话的架构组合
- [ ] 我能区分短期、工作、情景、语义、程序性记忆，并说出它们各放在哪里
- [ ] 我能用决策树为一个新需求选出架构，并回答"为什么更简单的不够用"
- [ ] 我完成了练习，`make lesson N=05` 全部通过

## 延伸阅读

**单 Agent 架构**

- Yao 等，[ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)（ICLR 2023）
- Wang 等，[Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning by Large Language Models](https://arxiv.org/abs/2305.04091)（ACL 2023）
- Xu 等，[ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models](https://arxiv.org/abs/2305.18323)（2023）
- Kim 等，[An LLM Compiler for Parallel Function Calling](https://arxiv.org/abs/2312.04511)（ICML 2024）
- Shinn 等，[Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)（NeurIPS 2023）
- Madaan 等，[Self-Refine: Iterative Refinement with Self-Feedback](https://arxiv.org/abs/2303.17651)（NeurIPS 2023）
- Huang 等，[Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/abs/2310.01798)（ICLR 2024）
- Wang 等，[Executable Code Actions Elicit Better LLM Agents](https://arxiv.org/abs/2402.01030)（CodeAct，ICML 2024）
- Yao 等，[Tree of Thoughts: Deliberate Problem Solving with Large Language Models](https://arxiv.org/abs/2305.10601)（NeurIPS 2023）
- Zhou 等，[Language Agent Tree Search Unifies Reasoning Acting and Planning in Language Models](https://arxiv.org/abs/2310.04406)（LATS，ICML 2024）
- LangChain 博客：[Planning Agents](https://www.langchain.com/blog/planning-agents)（Plan-and-Execute、ReWOO、LLMCompiler 对比）、[Reflection Agents](https://www.langchain.com/blog/reflection-agents)

**多 Agent 拓扑**

- Fourney 等，[Magentic-One: A Generalist Multi-Agent System for Solving Complex Tasks](https://arxiv.org/abs/2411.04468)（微软，2024）
- Hong 等，[MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework](https://arxiv.org/abs/2308.00352)（ICLR 2024）
- Erman 等，[The Hearsay-II Speech-Understanding System: Integrating Knowledge to Resolve Uncertainty](https://doi.org/10.1145/356810.356816)（ACM Computing Surveys，1980）：黑板架构的经典
- Han、Zhang，[Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture](https://arxiv.org/abs/2507.01701)（2025）
- LangChain，[Introducing ambient agents](https://www.langchain.com/blog/introducing-ambient-agents)（2025）
- LangChain 文档：[Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent)；OpenAI Agents SDK：[Handoffs](https://openai.github.io/openai-agents-python/handoffs/)
- OpenAI，[A practical guide to building agents](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)（2025）
- [A2A 协议](https://a2a-protocol.org/latest/)

**产品架构**

- Anthropic，[How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)（2025）
- Anthropic，[Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)（2024）
- Anthropic，[Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)（2025）
- Anthropic，[Making Claude Code more secure and autonomous with sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing)（2025）
- Claude Code 文档：[权限模式](https://code.claude.com/docs/en/permission-modes)、[子 Agent](https://code.claude.com/docs/en/sub-agents)
- OpenAI，[Codex 系统卡](https://cdn.openai.com/pdf/8df7697b-c1b2-4222-be00-1fd3298f351d/codex_system_card.pdf)（2025）；Codex [沙箱文档](https://learn.chatgpt.com/docs/sandboxing)
- OpenAI，[Computer-Using Agent](https://openai.com/index/computer-using-agent/)（2025）；Xie 等，[OSWorld](https://arxiv.org/abs/2404.07972)（2024）
- Yao 等，[τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045)（ICLR 2025）
- [openai-cs-agents-demo](https://github.com/openai/openai-cs-agents-demo)；[open_deep_research](https://github.com/langchain-ai/open_deep_research)

**Agentic RAG 与记忆**

- Trivedi 等，[IRCoT](https://arxiv.org/abs/2212.10509)（ACL 2023）；Asai 等，[Self-RAG](https://arxiv.org/abs/2310.11511)（ICLR 2024）；Yan 等，[CRAG](https://arxiv.org/abs/2401.15884)（2024）；Jeong 等，[Adaptive-RAG](https://arxiv.org/abs/2403.14403)（NAACL 2024）；Singh 等，[Agentic RAG 综述](https://arxiv.org/abs/2501.09136)（2025）
- Sumers 等，[Cognitive Architectures for Language Agents](https://arxiv.org/abs/2309.02427)（CoALA，TMLR）
- Park 等，[Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442)（UIST 2023）；Packer 等，[MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)（2023）
