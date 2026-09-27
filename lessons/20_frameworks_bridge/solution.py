"""第 20 课练习参考答案。先自己做，再来对照。"""

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
from typing import Any, Callable

from agentkit.tools import ToolRegistry
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


Node = Callable[[dict], "dict | None"]
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
        if self.entry is None:
            raise ValueError("没有入口：请先 add_edge(START, 节点名)")
        if self.entry not in self.nodes:
            raise ValueError(f"入口节点 {self.entry!r} 不存在")
        for src in list(self.edges) + list(self.branches):
            if src not in self.nodes:
                raise ValueError(f"边的起点 {src!r} 不是已注册的节点")
        for src, dst in self.edges.items():
            if dst != END and dst not in self.nodes:
                raise ValueError(f"边 {src} → {dst}：终点不存在")
        for src, (_, path_map) in self.branches.items():
            for dst in (path_map or {}).values():
                if dst != END and dst not in self.nodes:
                    raise ValueError(f"条件边 {src} 的 path_map 指向不存在的节点 {dst!r}")
        for name in self.nodes:
            has_edge, has_branch = name in self.edges, name in self.branches
            if has_edge == has_branch:
                raise ValueError(f"节点 {name!r} 必须恰好有一种出边（普通边或条件边），当前{'两种都有' if has_edge else '没有出边'}")


class CompiledGraph:
    def __init__(self, graph: MiniGraph, checkpointer: MemoryCheckpointer, max_steps: int, tracer: Tracer | None):
        self.graph = graph
        self.checkpointer = checkpointer
        self.max_steps = max_steps
        self.tracer = tracer

    # ---------------------------------------------------------------- 公共 API（已给出）
    def invoke(self, inputs: dict, thread_id: str = "default") -> dict:
        state = json.loads(json.dumps(inputs, ensure_ascii=False))  # 复制一份，同时确认输入可序列化
        cp = Checkpoint(thread_id=thread_id, step=0, next=self.graph.entry, state=state, status="running")
        self.checkpointer.put(cp)  # 第 0 个检查点：输入本身
        return self._run(cp)

    def resume(self, thread_id: str, value: Any) -> dict:
        cp = self.checkpointer.latest(thread_id)
        if cp is None or cp.status != "interrupted":
            raise ValueError(f"thread {thread_id!r} 当前没有被中断，不能 resume")
        cp = replace(cp, status="running", interrupt=None, resume_values=cp.resume_values + [value])
        return self._run(cp)

    def get_state(self, thread_id: str) -> Checkpoint | None:
        return self.checkpointer.latest(thread_id)

    def get_state_history(self, thread_id: str) -> list[Checkpoint]:
        return self.checkpointer.history(thread_id)

    # ---------------------------------------------------------------- 已给出：执行一个节点
    def _call_node(self, name: str, state: dict, resume_values: list) -> dict:
        """执行节点：传入状态的深拷贝（节点只能靠"返回更新"改状态），并把恢复值放进 contextvar 给 interrupt() 用。"""
        token = _resume.set(_ResumeCursor(list(resume_values)))
        try:
            with self.tracer.span(f"node.{name}") if self.tracer else nullcontext():
                update = self.graph.nodes[name](copy.deepcopy(state))
        finally:
            _resume.reset(token)
        if update is None:
            return {}
        if not isinstance(update, dict):
            raise TypeError(f"节点 {name!r} 必须返回 dict（状态更新）或 None，实际返回了 {type(update).__name__}")
        return update

    # ---------------------------------------------------------------- TODO (a2)
    def _merge(self, state: dict, update: dict) -> dict:
        merged = dict(state)
        for key, value in update.items():
            reducer = self.graph.reducers.get(key)
            merged[key] = reducer(merged[key], value) if reducer is not None and key in merged else value
        return merged

    # ---------------------------------------------------------------- TODO (a3)
    def _next(self, node: str, state: dict) -> str:
        if node in self.graph.edges:
            return self.graph.edges[node]
        router, path_map = self.graph.branches[node]
        key = router(state)
        if path_map is not None:
            if key not in path_map:
                raise ValueError(f"条件边 {node!r} 的路由函数返回了 {key!r}，不在 path_map {sorted(path_map)} 里")
            return path_map[key]
        if key != END and key not in self.graph.nodes:
            raise ValueError(f"条件边 {node!r} 的路由函数返回了未知节点 {key!r}")
        return key

    # ---------------------------------------------------------------- TODO (a4)
    def _run(self, cp: Checkpoint) -> dict:
        thread_id, state, node, step = cp.thread_id, cp.state, cp.next, cp.step
        resume_values = list(cp.resume_values)
        while node != END:
            if step >= self.max_steps:
                raise GraphRecursionError(f"执行了 {step} 个节点仍未到达 END（max_steps={self.max_steps}），下一个节点是 {node!r}")
            try:
                update = self._call_node(node, state, resume_values)
            except GraphInterrupt as e:
                self.checkpointer.put(
                    Checkpoint(thread_id, step, node, state, "interrupted", interrupt=e.payload, resume_values=resume_values)
                )
                return {**state, "__interrupt__": e.payload}
            resume_values = []  # 节点正常执行完：恢复值用完了
            state = self._merge(state, update)
            step += 1
            node = self._next(node, state)
            self.checkpointer.put(Checkpoint(thread_id, step, node, state, "done" if node == END else "running"))
        return state


# ---------------------------------------------------------------- 已给出：用 MiniGraph + agentkit 积木搭一个"会等审批"的 Agent

REJECTED = "审批人拒绝了该操作。请告诉用户该操作未获批准，不要重试。"


def build_agent_graph(llm, registry: ToolRegistry, needs_approval: set[str], *, max_steps: int = 20, tracer: Tracer | None = None) -> CompiledGraph:
    """和 impl_langgraph.py 同构的三节点图：agent → approval → tools → agent …

    llm 是任何 agentkit LLM（测试里用 ScriptedLLM），工具由 agentkit 的 ToolRegistry 执行（校验、超时、截断都还在）。
    输入状态：{"messages": [system, user], "decisions": {}}。
    """

    def agent(state: dict) -> dict:
        response = llm.chat(state["messages"], tools=registry.schemas() or None)
        return {"messages": [response.to_message()]}

    def approval(state: dict) -> dict:
        decisions = dict(state.get("decisions") or {})
        for call in calls_in(state["messages"][-1]):
            if call.name in needs_approval and call.id not in decisions:
                decisions[call.id] = bool(interrupt({"tool": call.name, "arguments": call.parsed_args()}))
        return {"decisions": decisions}

    def tools(state: dict) -> dict:
        decisions = state.get("decisions") or {}
        out = []
        for call in calls_in(state["messages"][-1]):
            content = REJECTED if decisions.get(call.id, True) is False else registry.execute(call).content
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
    if spec.count("->") != 1:
        raise ValueError(f"签名必须恰好包含一个 '->'：{spec!r}")
    left, right = spec.split("->")

    def side(text: str, label: str) -> list[FieldSpec]:
        fields = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"{label}里有空字段：{spec!r}")
            name, _, type_name = part.partition(":")
            name, type_name = name.strip(), (type_name.strip() or "str")
            if not name.isidentifier():
                raise ValueError(f"字段名不合法：{name!r}")
            if type_name not in TYPES:
                raise ValueError(f"字段 {name} 的类型 {type_name!r} 不支持，可选：{', '.join(TYPES)}")
            fields.append(FieldSpec(name, TYPES[type_name]))
        return fields

    inputs, outputs = side(left, "输入"), side(right, "输出")
    names = [f.name for f in inputs + outputs]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"字段名重复：{dupes}")
    return inputs, outputs


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
        in_names = [f.name for f in self.inputs]
        missing = [n for n in in_names if n not in inputs]
        if missing:
            raise ValueError(f"缺少输入字段：{missing}")
        extra = sorted(set(inputs) - set(in_names))
        if extra:
            raise ValueError(f"未声明的输入字段：{extra}")

        def lines(fields: list[FieldSpec]) -> str:
            return "\n".join(f"- {f.name} ({f.type.__name__})" + (f": {f.desc}" if f.desc else "") for f in fields)

        out_names = [f.name for f in self.outputs]
        system = "\n\n".join(
            part
            for part in (
                self.instructions,
                "输入字段：\n" + lines(self.inputs),
                "输出字段：\n" + lines(self.outputs),
                f"只输出一个 JSON 对象，键为：{', '.join(out_names)}。不要输出任何其他文字。",
            )
            if part
        )
        messages: list[Message] = [{"role": "system", "content": system}]
        for demo in self.demos:
            messages.append({"role": "user", "content": _render(self.inputs, demo)})
            messages.append({"role": "assistant", "content": json.dumps({n: demo[n] for n in out_names}, ensure_ascii=False)})
        messages.append({"role": "user", "content": _render(self.inputs, inputs)})
        return messages

    # ---------------------------------------------------------------- TODO (b3)
    def parse_output(self, text: str) -> dict:
        data = json.loads(extract_json(text))  # extract_json 找不到 JSON 时抛 ValueError；JSONDecodeError 也是 ValueError
        if not isinstance(data, dict):
            raise ValueError("输出必须是 JSON 对象")
        result = {}
        for f in self.outputs:
            if f.name not in data:
                raise ValueError(f"缺少输出字段 {f.name}")
            result[f.name] = _coerce(data[f.name], f.type, f.name)
        return result


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
    """

    def __init__(self, signature: Signature, llm, max_repairs: int = 1):
        self.signature = signature
        self.llm = llm
        self.max_repairs = max_repairs

    def __call__(self, **inputs: Any) -> Prediction:
        messages = self.signature.to_messages(**inputs)
        last_error = ""
        for _ in range(self.max_repairs + 1):
            text = self.llm.chat(messages).content or ""
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
    if TABLE_START in markdown and TABLE_END in markdown:
        markdown = markdown.split(TABLE_START, 1)[1].split(TABLE_END, 1)[0]
    rows = []
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break  # 第一张表结束
    if len(rows) < 2:
        raise ValueError("没有找到 Markdown 表格")

    def cells(row: str) -> list[str]:
        row = row.replace("\\|", "\x00").strip().strip("|")
        return [c.replace("\x00", "|").strip() for c in row.split("|")]

    header = cells(rows[0])
    frameworks = header[1:]
    table: dict[str, dict[str, str]] = {}
    for row in rows[2:]:  # rows[1] 是 |---|---| 分隔行
        values = cells(row)
        names = re.findall(r"`([^`]+)`", values[0]) or [values[0]]
        mapping = {fw: (values[i + 1] if i + 1 < len(values) else "") for i, fw in enumerate(frameworks)}
        for name in names:
            table[name] = mapping
    return table


# ---------------------------------------------------------------- TODO (c2)
def map_concept(agentkit_concept: str, framework: str, table: dict[str, dict[str, str]] | None = None) -> str:
    table = load_default_table() if table is None else table
    concepts = {norm_concept(k): k for k in table}
    key = norm_concept(agentkit_concept)
    if key not in concepts:
        close = difflib.get_close_matches(key, list(concepts), n=3, cutoff=0.5)
        hint = [concepts[c] for c in close] or sorted(table)
        raise KeyError(f"对照表里没有 {agentkit_concept!r}。你是不是想找：{', '.join(hint)}")
    row = table[concepts[key]]
    wanted = norm_framework(framework)
    wanted = FRAMEWORK_ALIASES.get(wanted, wanted)
    for fw, cell in row.items():
        if norm_framework(fw) == wanted:
            return cell
    raise KeyError(f"对照表里没有框架 {framework!r}。可选：{', '.join(row)}")
