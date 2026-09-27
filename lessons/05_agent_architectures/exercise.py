"""第 05 课练习：亲手写出三种架构的"骨架代码"。

运行测试：make lesson N=05    （或 .venv/bin/python -m pytest lessons/05_agent_architectures）

和第 06 课的练习一样，这里的"模型"都被抽象成普通的 Python 函数（planner / executor / critique ……）。
架构的本质是**控制流**：谁在什么时候思考、结果回到哪里、什么时候停。把 LLM 关在函数接口后面，
控制流就变成了普通、确定、可单元测试的代码 —— 企业里写 Agent 架构也应该这样分层。

任务 1：PlanExecuteAgent     先规划 → 逐步执行 → 某步失败时重规划（有上限）→ 返回执行轨迹
任务 2：reflect_loop          生成 → 批评 → 修改 的循环；批评意见重复出现时提前停止
任务 3：choose_architecture   按任务特征，用明确的规则选出架构
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

# ---------------------------------------------------------------- 任务 1：Plan-and-Execute


@dataclass
class Step:
    """计划里的一步。（已提供，不需要修改）

    id:   步骤编号，在"已完成的步骤 + 当前计划"里必须唯一，例如 "s1"
    tool: 这一步要做什么：一个工具名（如 "get_weather"），或 "subtask" 表示交给一个子 Agent
    args: 参数。可以引用前面步骤的结果（怎么引用由 executor 决定，本练习不关心）
    """

    id: str
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class PlanExecuteResult:
    """一次运行的结果。（已提供，不需要修改）

    status:      "completed" 或 "failed"
    stop_reason: "done" | "invalid_plan" | "max_replans" | "gave_up" | "max_steps"
    results:     成功完成的步骤 {step.id: executor 的返回值}，按执行顺序插入
    trace:       执行轨迹（事件字典列表，格式见 PlanExecuteAgent.run 的说明）
    replans:     replanner 实际被调用的次数
    """

    status: str
    stop_reason: str
    results: dict[str, Any] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    replans: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "completed"


Planner = Callable[[str], list]
Executor = Callable[[Step, dict], Any]
Replanner = Callable[[str, dict, Step, str, list], list]


class PlanExecuteAgent:
    """先规划、再逐步执行、失败时重规划（有上限）的 Agent 骨架。

    三个可注入的函数（生产中它们背后是 LLM 或工具；测试里是假函数）：
        planner(task) -> list[Step]
            规划器：看到任务后一次性给出完整计划（结构化的步骤列表）。
        executor(step, results) -> Any
            执行器：执行一步（一次工具调用，或把子任务交给子 Agent）。
            results 是到目前为止成功步骤的结果 {id: 输出}（请传**副本**）。抛出任何异常 = 这一步失败。
        replanner(task, results, failed_step, error, remaining) -> list[Step]
            重规划器：某一步失败时被调用。results 是已成功的结果（副本），failed_step 是失败的那一步，
            error 是形如 "TimeoutError: 天气服务超时" 的字符串（异常类型名: 异常消息），
            remaining 是旧计划里排在失败步骤之后、还没执行的步骤（副本）。
            返回**剩余要做的**新步骤列表，它会整体替换掉失败步骤和 remaining；返回 [] 表示"放弃"。
    """

    def __init__(self, planner: Planner, executor: Executor, replanner: Replanner, max_replans: int = 2, max_steps: int = 20):
        """max_replans: 最多调用几次 replanner（0 表示不允许重规划）；max_steps: 最多调用几次 executor（含失败的）。

        配置错误要大声失败：max_replans < 0 或 max_steps < 1 时抛 ValueError。
        """
        raise NotImplementedError("TODO: 校验参数并保存 planner / executor / replanner / max_replans / max_steps")

    def run(self, task: str) -> PlanExecuteResult:
        """执行一次任务，返回 PlanExecuteResult。运行中的任何失败都结构化返回，不向外抛异常
        （planner / replanner 自己抛出的异常除外，本练习不要求处理）。

        规则：
        1. 调用 planner(task) 得到计划并**校验**。合法的计划必须同时满足：
             - 是一个非空的 list，且每个元素都是 Step；
             - 步骤 id 在计划内不重复。
           不合法 → status="failed"，stop_reason="invalid_plan"，一步都不执行。
        2. 按顺序执行每一步：executor(step, results 的副本)。
             - 成功：results[step.id] = 返回值；
             - 抛异常：这一步失败，进入第 3 条。失败步骤**不**写入 results。
        3. 某一步失败时：
             - 如果 replanner 已经被调用了 max_replans 次 → status="failed"，stop_reason="max_replans"；
             - 否则调用 replanner(task, results 的副本, 失败的 step, error, 剩余步骤的副本)，replans 加 1：
                 · 返回 [] → status="failed"，stop_reason="gave_up"；
                 · 返回的计划要按第 1 条的规则校验，并且新步骤的 id 不能与**已成功步骤**的 id 重复
                   （否则会覆盖已有结果）；不合法 → status="failed"，stop_reason="invalid_plan"；
                 · 合法 → 用它替换剩余计划，继续执行。已成功的步骤**不会**重新执行。
        4. 每次调用 executor 之前检查：如果已经调用了 max_steps 次 → status="failed"，stop_reason="max_steps"。
           （计划恰好有 max_steps 步时，全部执行完是成功的。）
        5. 所有步骤都成功 → status="completed"，stop_reason="done"。

        trace 事件格式（按发生顺序追加；只有通过校验的计划才记 plan / replan 事件）：
            {"type": "plan",   "steps": ["s1", "s2"]}                   初始计划的步骤 id
            {"type": "step",   "id": "s1", "ok": True}                  一步成功
            {"type": "step",   "id": "s2", "ok": False, "error": "..."} 一步失败（error 同上面的格式）
            {"type": "replan", "steps": ["s2b", "s3"]}                  重规划后的新步骤 id
            {"type": "finish", "status": "...", "stop_reason": "..."}   结束（永远是最后一个事件）

        提示：用一个 remaining 列表表示"还没执行的步骤"，while remaining: 每次 pop(0)；
             写一个小的 finish(status, reason) 辅助函数，统一追加 finish 事件并构造结果。
        """
        raise NotImplementedError("TODO: 实现 规划 → 校验 → 逐步执行 → 失败重规划（有上限）→ 返回轨迹")


# ---------------------------------------------------------------- 任务 2：Reflection 循环


@dataclass
class ReflectResult:
    """（已提供，不需要修改）

    draft:       最终稿（最后一次 generate 的输出）
    rounds:      实际进行的轮数（= critique 被调用的次数）
    stop_reason: "accepted" | "repeated_critique" | "max_rounds"
    history:     每一轮的 (稿子, 批评意见)
    """

    draft: str
    rounds: int
    stop_reason: str
    history: list[tuple[str, str]] = field(default_factory=list)


def normalize_critique(text: str) -> str:
    """把批评意见归一化后再比较：合并连续空白、去掉首尾空白、忽略大小写。（已提供）"""
    return " ".join(text.split()).lower()


def reflect_loop(
    generate: Callable[[str | None, str | None], str],
    critique: Callable[[str], str],
    max_rounds: int,
    stop_when: Callable[[str], bool],
) -> ReflectResult:
    """自我批评循环：生成 → 批评 → 按批评修改 → 再批评 ……

    参数：
        generate(draft, feedback) -> str
            第 1 轮调用 generate(None, None) 写初稿；之后每轮调用 generate(上一轮的稿子, 上一轮的批评) 修改。
        critique(draft) -> str
            对稿子给出批评意见。
        max_rounds
            最多几轮。一轮 = 一次 generate + 一次 critique。max_rounds < 1 时抛 ValueError。
        stop_when(critique_text) -> bool
            批评意见表示"已经合格"时返回 True（比如意见以 "LGTM" 开头）。

    规则：
    1. 每轮先 generate，再 critique，把 (稿子, 批评) 追加到 history。
    2. stop_when(批评) 为真 → 立即停止，stop_reason="accepted"。（这条优先于第 3 条。）
    3. 如果这条批评在归一化（用 normalize_critique）之后，和**之前任何一轮**的批评相同 → 立即停止，
       stop_reason="repeated_critique"。
       为什么：同样的意见又出现了，说明上一次修改没有解决它；继续改大概率是原地打转，
       只会白白烧钱。和"之前任何一轮"比（而不只是上一轮），还能抓住 A → B → A 这种来回摇摆。
    4. 跑满 max_rounds 轮仍未停止 → stop_reason="max_rounds"。
    5. 返回的 draft 总是最后一次 generate 的输出；rounds = len(history)。
       注意：因第 3、4 条停止时，最后一条批评**不会**再被拿去调用 generate。

    提示：用一个 set 记住见过的（归一化后的）批评意见。
    """
    raise NotImplementedError("TODO: 实现带 重复批评检测 的 generate → critique 循环")


# ---------------------------------------------------------------- 任务 3：架构选型


@dataclass(frozen=True)
class TaskProfile:
    """描述一个任务的特征。（已提供，不需要修改）

    predictability:    步骤的可预知程度
        "fixed"     —— 开发者写代码时就知道步骤（如：每张发票都走 识别 → 校验 → 入账）
        "plannable" —— 看到具体任务后，模型能一次列出全部步骤（如：查三个城市的天气再汇总）
        "unknown"   —— 必须边做边看，下一步取决于上一步的结果（如：排查一个没见过的线上故障）
    needs_exploration: 是否需要尝试多条路径、比较后择优或回溯（如：解谜、找最优方案）
    parallelizable:    子步骤是否大多相互独立、可以并行
    high_reliability:  出错代价是否很大（资金、合规、不可逆的写操作），关键动作前必须有人把关
    verifiable:        输出能否被**客观**检查（单元测试、校验器、业务规则），而不是只能靠模型"感觉"
    """

    predictability: Literal["fixed", "plannable", "unknown"]
    needs_exploration: bool = False
    parallelizable: bool = False
    high_reliability: bool = False
    verifiable: bool = False


def choose_architecture(profile: TaskProfile) -> str:
    """按规则为任务选出单 Agent 架构，返回形如 "react"、"plan_execute+reflection+hitl" 的字符串。

    返回值 = 基础架构 [+ "reflection"] [+ "hitl"]，用 "+" 连接，顺序固定。

    第一步：选基础架构（只看 predictability，再按下面的条件细分）
      1. "fixed"     → "workflow"
         步骤在写代码时就确定了，根本不需要让模型决定流程，用第 06 课的 Workflow 模式。
      2. "plannable" → parallelizable 为真时 "rewoo"，否则 "plan_execute"
         能一次规划完：步骤独立可并行时，用 ReWOO / LLMCompiler 类架构（规划时写好变量依赖，
         执行期间不再回到 LLM，还能并行）；步骤前后依赖时，用 Plan-and-Execute（逐步执行，失败可重规划）。
      3. "unknown"   → 同时满足 needs_exploration、verifiable、并且 **不是** high_reliability 时 "tree_search"，
         否则 "react"
         树搜索要有可靠的打分信号（verifiable）才能在多条路径里择优；它会执行大量试探性动作，
         所以出错代价大（high_reliability）的场景不能用。其余情况用 ReAct：边想边做。
      4. 其他取值 → 抛 ValueError（配置错误要大声失败）。

    第二步：叠加
      5. verifiable 为真，且基础架构不是 "tree_search" → 追加 "reflection"
         有客观的检查手段（测试、校验器）时，把检查结果喂回去让它改，是性价比最高的反思；
         没有外部信号的"自己批评自己"效果不稳定（见 README 的 Reflection 一节），所以不加。
         （树搜索本身已经包含评估，不重复叠加。）
      6. high_reliability 为真 → 追加 "hitl"
         出错代价大的动作之前加人工检查点。它是叠加层，不替代上面任何一种架构。

    例：
      TaskProfile("fixed")                                        → "workflow"
      TaskProfile("plannable", parallelizable=True)               → "rewoo"
      TaskProfile("unknown", needs_exploration=True, verifiable=True)
                                                                  → "tree_search"
      TaskProfile("unknown", needs_exploration=True, verifiable=True, high_reliability=True)
                                                                  → "react+reflection+hitl"
    """
    raise NotImplementedError("TODO: 按 docstring 里的规则选出基础架构，再叠加 reflection / hitl")
