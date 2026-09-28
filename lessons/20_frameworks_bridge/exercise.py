"""第 20 课练习：用 agentkit 的积木，亲手实现三个框架的"核心原理"。

框架好用，但只有知道它在底下做了什么，出问题时你才查得动。这三道题都离线、零框架依赖：
  (a) 迷你 StateGraph（LangGraph 的核心）：节点、条件边、reducer、执行到 END、最大步数、
      每一步存检查点、interrupt 暂停 + resume 恢复（被中断的节点从头重跑）
        TODO (a1) MiniGraph.validate      (a2) CompiledGraph._merge
        TODO (a3) CompiledGraph._next     (a4) CompiledGraph._run
      写完之后，文件里已经给出的 build_agent_graph() 会用你的 MiniGraph + agentkit 的 LLM / ToolRegistry
      搭出一个"会等人工审批"的 Agent——和 impl_langgraph.py 同构，但一行框架代码都没有
      async：图的 invoke / resume 和 _run 都是 async def（对应 LangGraph 的 ainvoke），节点可以是普通函数或
      async 函数；你要写的 _run 里执行节点那一步是 `update = await self._call_node(...)`。
      validate / _merge / _next 是纯计算，写成普通 def
  (b) DSPy 风格的 Signature：从字段声明生成提示词，把 JSON 输出解析回结构
        TODO (b1) parse_signature   (b2) Signature.to_messages   (b3) Signature.parse_output
      三个都是纯计算（普通 def）；调用模型的 Predict 已经给出，它是 async 的
  (c) 概念对照查询 map_concept(agentkit 概念, 框架)，数据直接读本课 README.md 里的对照表
        TODO (c1) parse_concept_table   (c2) map_concept

把每个 `raise NotImplementedError("TODO ...")` 换成你的实现，然后运行：

    make lesson N=20
    # 或者：.venv/bin/python -m pytest lessons/20_frameworks_bridge/test_exercise.py -v

建议顺序：(c) 最简单，适合热身；(b) 次之；(a) 最有收获。卡住了？先重读 README 第 2 节，再看 solution.py。
"""

from __future__ import annotations

import contextvars
import copy
import difflib
import json
import operator
import re
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from agentkit.tools import ToolRegistry, maybe_await
from agentkit.tracing import Tracer
from agentkit.types import Message, calls_in, tool_message
from agentkit.workflows import extract_json

# =====================================================================
# (a) 迷你 StateGraph：节点、条件边、执行到 END、最大步数、每步检查点、interrupt / resume
# =====================================================================

START = "__start__"
END = "__end__"


class GraphInterrupt(Exception):
    """节点里调用 interrupt() 且还没有恢复值时抛出：图暂停，payload 交给调用方（通常是审批人）。"""

    trace_as_error = False  # agentkit.Tracer 看到这个标记，会把 Span 记成"中断"而不是"错误"

    def __init__(self, payload: Any):
        super().__init__("graph interrupted")
        self.payload = payload


class GraphRecursionError(RuntimeError):
    """执行的节点数超过 max_steps。和 LangGraph 的同名异常一样：防止图里的环无限转下去。"""


class _ResumeCursor:
    """一次节点执行里可用的恢复值。节点每调用一次 interrupt()，就按顺序消费一个。"""

    def __init__(self, values: list):
        self.values = values
        self.index = 0


_resume: contextvars.ContextVar[_ResumeCursor | None] = contextvars.ContextVar("minigraph_resume", default=None)


def interrupt(payload: Any) -> Any:
    """在节点内部暂停整张图，等外部给出一个值（比如审批结果）。

    第一次执行到这里：抛 GraphInterrupt，图暂停并存检查点。
    resume(value) 之后：**节点从头重新执行**，再次走到这里时直接返回 value。
    所以 interrupt() 之前的代码会被执行两次——那里不能有副作用。这是 LangGraph 的真实语义。
    """
    cursor = _resume.get()
    if cursor is None:
        raise RuntimeError("interrupt() 只能在 MiniGraph 的节点内部调用")
    if cursor.index < len(cursor.values):
        value = cursor.values[cursor.index]
        cursor.index += 1
        return value
    raise GraphInterrupt(payload)


@dataclass
class Checkpoint:
    thread_id: str
    step: int  # 已经执行完的节点数
    next: str  # 下一个要执行的节点（END 表示已结束；中断时是被中断的那个节点）
    state: dict
    status: str  # running / interrupted / done
    interrupt: Any = None  # 中断时交给调用方的 payload
    resume_values: list = field(default_factory=list)  # 被中断节点已经拿到的恢复值（支持一个节点多次 interrupt）


class MemoryCheckpointer:
    """内存检查点：每个 thread 一串 JSON 快照。存 JSON 而不是对象引用——和 agentkit.InMemoryCheckpointer 一个道理：
    保证存进去的东西之后不会被意外改掉，也逼着状态保持"可序列化"（换成数据库时不会踩坑）。"""

    def __init__(self):
        self._data: dict[str, list[str]] = {}

    def put(self, cp: Checkpoint) -> None:
        self._data.setdefault(cp.thread_id, []).append(json.dumps(asdict(cp), ensure_ascii=False))

    def latest(self, thread_id: str) -> Checkpoint | None:
        rows = self._data.get(thread_id)
        return Checkpoint(**json.loads(rows[-1])) if rows else None

    def history(self, thread_id: str) -> list[Checkpoint]:
        """最新的在前（和 LangGraph 的 get_state_history 顺序一致）。"""
        return [Checkpoint(**json.loads(r)) for r in reversed(self._data.get(thread_id, []))]


Node = Callable[[dict], "dict | None | Awaitable[dict | None]"]  # 普通函数或 async 函数都行
Router = Callable[[dict], str]


class MiniGraph:
    """图的"设计图"：只登记节点和边，compile() 之后才能运行（LangGraph 也是 builder → compile 两段式）。"""

    def __init__(self, reducers: dict[str, Callable[[Any, Any], Any]] | None = None):
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, str] = {}
        self.branches: dict[str, tuple[Router, dict[str, str] | None]] = {}
        self.entry: str | None = None
        self.reducers = dict(reducers or {})

    def add_node(self, name: str, fn: Node) -> "MiniGraph":
        if name in (START, END) or name in self.nodes:
            raise ValueError(f"节点名不合法或重复：{name!r}")
        self.nodes[name] = fn
        return self

    def add_edge(self, src: str, dst: str) -> "MiniGraph":
        if src == START:
            self.entry = dst
        else:
            self.edges[src] = dst
        return self

    def add_conditional_edges(self, src: str, router: Router, path_map: dict[str, str] | None = None) -> "MiniGraph":
        self.branches[src] = (router, dict(path_map) if path_map is not None else None)
        return self

    def compile(self, *, checkpointer: MemoryCheckpointer | None = None, max_steps: int = 25, tracer: Tracer | None = None) -> "CompiledGraph":
        self.validate()
        return CompiledGraph(self, checkpointer or MemoryCheckpointer(), max_steps, tracer)

    # ---------------------------------------------------------------- TODO (a1)
    def validate(self) -> None:
        """检查图的结构，发现问题就抛 ValueError（compile() 会先调用它——错误越早暴露越好）。

        规则：
          1. 必须有入口：self.entry 不是 None，而且是已注册的节点（add_edge(START, x) 会设置 entry）
          2. self.edges 和 self.branches 的每个起点都必须是已注册节点
          3. 普通边的终点必须是已注册节点或 END
          4. 条件边的 path_map（如果有）里每个值都必须是已注册节点或 END
          5. 每个节点恰好有一种出边：要么在 self.edges 里，要么在 self.branches 里；
             两种都有、或者一种都没有，都抛 ValueError（我们选择"显式优于隐式"：结束也要写 add_edge(x, END)）

        提示：检查 3 要在检查 5 之前做，这样"终点写错"的报错更准确。
        """
        raise NotImplementedError("TODO (a1): 实现 MiniGraph.validate，要求见上面的 docstring")


class CompiledGraph:
    def __init__(self, graph: MiniGraph, checkpointer: MemoryCheckpointer, max_steps: int, tracer: Tracer | None):
        self.graph = graph
        self.checkpointer = checkpointer
        self.max_steps = max_steps
        self.tracer = tracer

    # ---------------------------------------------------------------- 公共 API（已给出）
    # invoke / resume 是 async 的（对应 LangGraph 的 ainvoke）：节点可能要等模型、等工具
    async def invoke(self, inputs: dict, thread_id: str = "default") -> dict:
        state = json.loads(json.dumps(inputs, ensure_ascii=False))  # 复制一份，同时确认输入可序列化
        cp = Checkpoint(thread_id=thread_id, step=0, next=self.graph.entry, state=state, status="running")
        self.checkpointer.put(cp)  # 第 0 个检查点：输入本身
        return await self._run(cp)

    async def resume(self, thread_id: str, value: Any) -> dict:
        cp = self.checkpointer.latest(thread_id)
        if cp is None or cp.status != "interrupted":
            raise ValueError(f"thread {thread_id!r} 当前没有被中断，不能 resume")
        cp = replace(cp, status="running", interrupt=None, resume_values=cp.resume_values + [value])
        return await self._run(cp)

    def get_state(self, thread_id: str) -> Checkpoint | None:
        return self.checkpointer.latest(thread_id)

    def get_state_history(self, thread_id: str) -> list[Checkpoint]:
        return self.checkpointer.history(thread_id)

    # ---------------------------------------------------------------- 已给出：执行一个节点
    async def _call_node(self, name: str, state: dict, resume_values: list) -> dict:
        """执行节点：传入状态的深拷贝（节点只能靠"返回更新"改状态），并把恢复值放进 contextvar 给 interrupt() 用。

        节点可以是普通函数（纯计算，如路由、审批判断），也可以是 async 函数（要 await 模型或工具）：
        maybe_await 两种都接得住。contextvar 在同一个任务里跨 await 仍然有效，所以 async 节点里也能调 interrupt()。
        """
        token = _resume.set(_ResumeCursor(list(resume_values)))
        try:
            with self.tracer.span(f"node.{name}") if self.tracer else nullcontext():
                update = await maybe_await(self.graph.nodes[name](copy.deepcopy(state)))
        finally:
            _resume.reset(token)
        if update is None:
            return {}
        if not isinstance(update, dict):
            raise TypeError(f"节点 {name!r} 必须返回 dict（状态更新）或 None，实际返回了 {type(update).__name__}")
        return update

    # ---------------------------------------------------------------- TODO (a2)
    def _merge(self, state: dict, update: dict) -> dict:
        """把节点返回的"部分更新" update 合并进 state，返回**新的** dict（不要原地修改 state）。

          - 默认：update 里的值直接覆盖 state 里的同名键
          - 如果这个键在 self.graph.reducers 里有 reducer，并且 state 里已有这个键：
            新值 = reducer(旧值, update 里的值)。例如 reducers={"messages": operator.add} 就是"追加"
            （LangGraph 的 Annotated[list, add_messages] 是同一个思路）
          - update 里没出现的键保持不变
        """
        raise NotImplementedError("TODO (a2): 实现 CompiledGraph._merge，要求见上面的 docstring")

    # ---------------------------------------------------------------- TODO (a3)
    def _next(self, node: str, state: dict) -> str:
        """节点 node 执行完后，决定下一个节点。

          - node 有普通边：返回 self.graph.edges[node]
          - 否则取 router, path_map = self.graph.branches[node]，key = router(state)：
              - 有 path_map：key 必须在 path_map 里（否则 ValueError），返回 path_map[key]
              - 没有 path_map：key 本身就是节点名，必须是已注册节点或 END（否则 ValueError）
        """
        raise NotImplementedError("TODO (a3): 实现 CompiledGraph._next，要求见上面的 docstring")

    # ---------------------------------------------------------------- TODO (a4)
    async def _run(self, cp: Checkpoint) -> dict:
        """主循环：从检查点 cp 开始执行，直到 END 或被中断。invoke() 和 resume() 都调用它（都会 await 它）。

        变量：state = cp.state，node = cp.next，step = cp.step，resume_values = list(cp.resume_values)

        循环（node != END 时）：
          1. step >= self.max_steps → 抛 GraphRecursionError（防止环无限转下去）
          2. update = await self._call_node(node, state, resume_values)   ← _call_node 是 async 的，别忘了 await
             如果抛了 GraphInterrupt e：存一个检查点
                 Checkpoint(thread_id, step, node, state, "interrupted", interrupt=e.payload, resume_values=resume_values)
             然后返回 {**state, "__interrupt__": e.payload}（注意：不要把 "__interrupt__" 合并进 state）
          3. 节点正常结束：resume_values 清空；state = self._merge(state, update)；step += 1
          4. node = self._next(旧 node, state)
          5. 存检查点 Checkpoint(thread_id, step, node, state, "done" if node == END else "running")
        循环结束返回 state。

        为什么"每一步都存"？崩溃后可以从最后一个检查点继续，也可以用 get_state_history 回放整个执行过程。
        为什么中断时 next 仍是被中断的节点？因为 resume 时这个节点要**从头重新执行**（LangGraph 的真实语义）。
        """
        raise NotImplementedError("TODO (a4): 实现 CompiledGraph._run，要求见上面的 docstring")


# ---------------------------------------------------------------- 已给出：用 MiniGraph + agentkit 积木搭一个"会等审批"的 Agent

REJECTED = "审批人拒绝了该操作。请告诉用户该操作未获批准，不要重试。"


def build_agent_graph(llm, registry: ToolRegistry, needs_approval: set[str], *, max_steps: int = 20, tracer: Tracer | None = None) -> CompiledGraph:
    """和 impl_langgraph.py 同构的三节点图：agent → approval → tools → agent …

    llm 是任何 agentkit LLM（测试里用 ScriptedLLM），工具由 agentkit 的 ToolRegistry 执行（校验、超时、截断都还在）。
    输入状态：{"messages": [system, user], "decisions": {}}。
    agent、tools 两个节点要 await 模型和工具，是 async 函数；approval 只读状态、调 interrupt()，是普通函数。
    """

    async def agent(state: dict) -> dict:
        response = await llm.chat(state["messages"], tools=registry.schemas() or None)
        return {"messages": [response.to_message()]}

    def approval(state: dict) -> dict:
        decisions = dict(state.get("decisions") or {})
        for call in calls_in(state["messages"][-1]):
            if call.name in needs_approval and call.id not in decisions:
                decisions[call.id] = bool(interrupt({"tool": call.name, "arguments": call.parsed_args()}))
        return {"decisions": decisions}

    async def tools(state: dict) -> dict:
        decisions = state.get("decisions") or {}
        out = []
        for call in calls_in(state["messages"][-1]):
            content = REJECTED if decisions.get(call.id, True) is False else (await registry.execute(call)).content
            out.append(tool_message(call.id, content))
        return {"messages": out}

    def route(state: dict) -> str:
        return "approval" if state["messages"][-1].get("tool_calls") else END

    g = MiniGraph(reducers={"messages": operator.add})
    g.add_node("agent", agent).add_node("approval", approval).add_node("tools", tools)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route)
    g.add_edge("approval", "tools")
    g.add_edge("tools", "agent")
    return g.compile(max_steps=max_steps, tracer=tracer)


# =====================================================================
# (b) DSPy 风格的 Signature：从字段描述生成提示词，把 JSON 输出解析回结构
# =====================================================================

TYPES: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool, "list": list}


@dataclass
class FieldSpec:
    name: str
    type: type = str
    desc: str = ""


# ---------------------------------------------------------------- TODO (b1)
def parse_signature(spec: str) -> tuple[list[FieldSpec], list[FieldSpec]]:
    """解析 "question, context: str -> answer, confidence: float" 这样的签名字符串。

    返回 (输入字段列表, 输出字段列表)，每个字段是 FieldSpec(name, type)，desc 留空。
      - 必须恰好有一个 "->"，否则 ValueError
      - 两边按逗号切分；每个字段形如 "名字" 或 "名字: 类型"，类型省略时是 str
      - 类型只能是 TYPES 里的键（str/int/float/bool/list），否则 ValueError
      - 字段名必须是合法标识符（str.isidentifier()），不能为空（"q, -> a" 这种要报错）
      - 输入和输出合起来不能有重名字段
    """
    raise NotImplementedError("TODO (b1): 实现 parse_signature，要求见上面的 docstring")


def _coerce(value: Any, typ: type, name: str) -> Any:
    """把 JSON 里的值转成字段声明的类型；转不了就抛 ValueError（错误信息会反馈给模型让它改）。"""
    if typ is str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "yes", "是"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("false", "no", "否"):
            return False
        raise ValueError(f"字段 {name} 应为 bool，实际是 {value!r}")
    if typ in (int, float):
        if isinstance(value, bool):
            raise ValueError(f"字段 {name} 应为 {typ.__name__}，实际是 bool")
        try:
            return typ(value)
        except (TypeError, ValueError):
            raise ValueError(f"字段 {name} 应为 {typ.__name__}，实际是 {value!r}") from None
    if typ is list:
        if not isinstance(value, list):
            raise ValueError(f"字段 {name} 应为 list，实际是 {type(value).__name__}")
        return value
    return value


def _render(fields: list[FieldSpec], values: dict) -> str:
    """把一组字段值渲染成 `字段名: 值` 的多行文本（非字符串值用 JSON）。"""
    return "\n".join(
        f"{f.name}: {values[f.name] if isinstance(values[f.name], str) else json.dumps(values[f.name], ensure_ascii=False)}"
        for f in fields
    )


class Signature:
    """声明"输入什么、输出什么"，而不是手写提示词。

    提示词优化器（第 23 课）改的主要就是两样：instructions（指令）和 demos（示例）。
    所以它们是 Signature 的"参数"，用 with_instructions / with_demos 生成新版本，而不是原地修改。
    """

    def __init__(self, spec: str, instructions: str = "", desc: dict[str, str] | None = None, demos: list[dict] | None = None):
        self.spec = spec
        self.instructions = instructions
        self.inputs, self.outputs = parse_signature(spec)
        desc = dict(desc or {})
        known = {f.name for f in self.inputs + self.outputs}
        unknown = set(desc) - known
        if unknown:
            raise ValueError(f"desc 里有未声明的字段：{sorted(unknown)}")
        for f in self.inputs + self.outputs:
            f.desc = desc.get(f.name, "")
        self.demos = [dict(d) for d in (demos or [])]
        self._desc = desc

    def with_instructions(self, instructions: str) -> "Signature":
        return Signature(self.spec, instructions, self._desc, self.demos)

    def with_demos(self, demos: list[dict]) -> "Signature":
        return Signature(self.spec, self.instructions, self._desc, demos)

    # ---------------------------------------------------------------- TODO (b2)
    def to_messages(self, **inputs: Any) -> list[Message]:
        """把签名 + 输入值变成发给模型的消息列表（这就是 DSPy 的 Adapter 在做的事）。

          - 缺少输入字段 → ValueError；传了未声明的输入字段 → ValueError
          - 第 1 条：system 消息，依次包含（用空行分隔，空的部分跳过）：
              self.instructions
              "输入字段：" + 每个输入字段一行 "- 名字 (类型名)"，有 desc 时后面加 ": desc"
              "输出字段：" + 同样格式的输出字段
              一句格式要求，例如 "只输出一个 JSON 对象，键为：answer, confidence。不要输出任何其他文字。"
          - 然后每个 demo 变成一对消息（few-shot 示例）：
              user：_render(self.inputs, demo)；assistant：json.dumps({输出字段: demo[输出字段]}, ensure_ascii=False)
          - 最后一条：user 消息，内容是 _render(self.inputs, inputs)，即每行 "字段名: 值"

        提示：_render 和 FieldSpec.type.__name__ 已经可以直接用。
        """
        raise NotImplementedError("TODO (b2): 实现 Signature.to_messages，要求见上面的 docstring")

    # ---------------------------------------------------------------- TODO (b3)
    def parse_output(self, text: str) -> dict:
        """把模型的文本输出解析回 {输出字段: 值}。

          - 用 agentkit 的 extract_json(text) 抠出 JSON（它能处理 ```json 代码块和前后的废话），再 json.loads
          - 解析结果必须是 dict，否则 ValueError
          - 每个输出字段都必须存在，缺了就抛 ValueError，消息里带上字段名（让模型知道改哪里）
          - 每个值用 _coerce(值, 字段类型, 字段名) 转成声明的类型（已给出）
          - 只返回声明过的输出字段，多余的键丢掉

        注意：json.JSONDecodeError 是 ValueError 的子类，不需要单独处理。
        """
        raise NotImplementedError("TODO (b3): 实现 Signature.parse_output，要求见上面的 docstring")


class Prediction(dict):
    """既能 pred["answer"] 也能 pred.answer，和 dspy.Prediction 的用法一样。"""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


class Predict:
    """最简单的"模块"：Signature + 一种调用策略（调一次模型，解析失败就把错误反馈回去重试）。

    DSPy 的 ChainOfThought / ReAct 本质上也是"同一个 Signature + 不同的调用策略"。
    调用是 async 的：pred = await Predict(sig, llm)(question=...)（DSPy 3 里对应 await module.acall(...)）。
    """

    def __init__(self, signature: Signature, llm, max_repairs: int = 1):
        self.signature = signature
        self.llm = llm
        self.max_repairs = max_repairs

    async def __call__(self, **inputs: Any) -> Prediction:
        messages = self.signature.to_messages(**inputs)
        last_error = ""
        for _ in range(self.max_repairs + 1):
            text = (await self.llm.chat(messages)).content or ""
            try:
                return Prediction(self.signature.parse_output(text))
            except ValueError as e:
                last_error = str(e)
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"你的输出没有通过校验：{last_error}。请只输出修正后的 JSON 对象。"},
                ]
        raise ValueError(f"修复 {self.max_repairs} 次后输出仍不合法：{last_error}")


# =====================================================================
# (c) 概念对照查询：数据来自 README.md 里的对照表（讲义就是唯一数据源）
# =====================================================================

TABLE_START = "<!-- concept-map:start -->"
TABLE_END = "<!-- concept-map:end -->"

# 规范化之后的别名 → 规范化之后的表头
FRAMEWORK_ALIASES = {
    "openai": "openaiagentssdk",
    "openaiagents": "openaiagentssdk",
    "agentssdk": "openaiagentssdk",
    "lg": "langgraph",
}


def norm_concept(text: str) -> str:
    """`@tool` / tool / Tool() → "tool"：去掉空白、反引号、开头的 @、结尾的 ()，再转小写。"""
    text = text.strip().strip("`").strip()
    text = text.lstrip("@")
    if text.endswith("()"):
        text = text[:-2]
    return text.lower()


def norm_framework(text: str) -> str:
    """"OpenAI Agents SDK" / openai-agents-sdk / openai_agents_sdk → "openaiagentssdk"。"""
    return re.sub(r"[\s\-_]", "", text.lower())


def load_default_table() -> dict[str, dict[str, str]]:
    return parse_concept_table((Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- TODO (c1)
def parse_concept_table(markdown: str) -> dict[str, dict[str, str]]:
    """把 README 里的 Markdown 对照表解析成 {agentkit 概念: {框架名: 单元格原文}}。

      - 如果文本里有 TABLE_START 和 TABLE_END 标记，只解析两个标记之间的内容；否则解析第一张表
      - 表格行是以 "|" 开头的行；遇到第一张表之后的非表格行就停
      - 第 1 行是表头：第 1 列是 agentkit，后面每列一个框架名；第 2 行是 |---|---| 分隔行，跳过
      - 切分单元格前先把转义的竖线 "\\|" 换成占位符，切完再还原成 "|"；每个单元格去掉首尾空白
      - 第 1 列里每个 `反引号` 包住的名字都作为一个键（`PauseRun` / `approve` 两个键指向同一行）；
        没有反引号就用整个单元格文本作为键
      - 行里缺的列用空字符串补齐
      - 一行表格都没找到 → ValueError
    """
    raise NotImplementedError("TODO (c1): 实现 parse_concept_table，要求见上面的 docstring")


# ---------------------------------------------------------------- TODO (c2)
def map_concept(agentkit_concept: str, framework: str, table: dict[str, dict[str, str]] | None = None) -> str:
    """查询 agentkit 概念在某个框架里叫什么，返回单元格原文。

      - table 为 None 时用 load_default_table()（读本目录 README.md）
      - 概念用 norm_concept 规范化后匹配（tool / @tool / `@tool` / Tool() 都能命中 "@tool"）
      - 框架用 norm_framework 规范化，再查 FRAMEWORK_ALIASES（"openai" → "openaiagentssdk"），和表头规范化后比较
      - 找不到概念：抛 KeyError，消息里带上 difflib.get_close_matches 找到的相近概念原名（找不到相近的就列出全部）
      - 找不到框架：抛 KeyError，消息里列出这一行所有的框架名
    """
    raise NotImplementedError("TODO (c2): 实现 map_concept，要求见上面的 docstring")
