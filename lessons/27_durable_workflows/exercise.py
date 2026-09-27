"""第 27 课练习：持久化工作流里的三块"纯逻辑"。

  (a) retry_policy_for(tool_risk, idempotent) -> dict   工具 Activity 的重试参数
  (b) on_event(state, event) -> ApprovalState            审批状态机（乱序、重复、超时、拒绝后再批准）
  (c) is_deterministic_safe(source_code) -> list[str]   用 ast 找出 workflow 代码里的非确定性调用

三道题都不需要 Temporal 服务器，也不需要安装 temporalio。把每个 `raise NotImplementedError("TODO: ...")`
换成你的实现，然后运行：

    make lesson N=27
    # 或者：.venv/bin/python -m pytest lessons/27_durable_workflows/test_exercise.py -v

卡住了？先重读 README 第 2 节的问题 2、3、4，再看 solution.py。
生产版分别在 agentkit/contrib/temporal.py 的 retry_policy_for、AgentWorkflow._decide 里；
(c) 在生产里对应"代码审查 + 重放测试（Replayer）"，本课 README 第 2 节问题 4 讲了为什么沙箱不够。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace  # noqa: F401  replace 在 on_event 里用得上

# ============================================================================================
# (a) 重试策略
# ============================================================================================

RISKS = ("read", "write", "dangerous")
# 由工具层主动抛出、重试也不会成功的错误类型：参数不合法、无权限
NON_RETRYABLE_ERROR_TYPES = ["InvalidArguments", "PermissionDenied"]


def retry_policy_for(tool_risk: str, idempotent: bool) -> dict:
    """根据工具风险和"是否幂等"给出 Temporal RetryPolicy 的参数。

    规则（与 agentkit.contrib.temporal.retry_policy_for 一致）：
        read                          maximum_attempts = 5   读操作天然幂等（idempotent 参数被忽略）
        write / dangerous，幂等        maximum_attempts = 3   下游用 workflow_id:call_id 去重，重试无害
        write / dangerous，不幂等      maximum_attempts = 1   不自动重试：宁可"结果未知"交给人，也不重复扣款
    其余字段对所有情况相同：initial_interval = 1.0（秒）、backoff_coefficient = 2.0、
    non_retryable_error_types = NON_RETRYABLE_ERROR_TYPES 的一个**新副本**。
    未知的 tool_risk 抛 ValueError。
    """
    # 提示：先校验 tool_risk；read 固定 5 次；写操作看 idempotent；列表要返回新副本（调用方可能会修改它）
    raise NotImplementedError("TODO: 按风险和幂等性返回 RetryPolicy 参数")


# ============================================================================================
# (b) 审批状态机
# ============================================================================================


@dataclass(frozen=True)
class ApprovalEvent:
    """kind: "request"（workflow 走到等待点）/ "decision"（审批 signal 到达）/ "timeout"（审批定时器触发）。"""

    kind: str
    call_id: str
    approved: bool = False
    by: str = ""


@dataclass(frozen=True)
class ApprovalState:
    """一次工具调用的审批状态。frozen：on_event 必须返回新对象，不能原地修改（重放时同一串事件必须得到同一个结果）。

    status：idle（还没走到等待点）→ waiting → approved / rejected / timed_out（三个终态）
    early：进入等待之前就收到的决定 (approved, by)
    decided_by：做出最终决定的人；超时为 "system:timeout"
    ignored：被忽略的事件说明，给审计看，例如 "duplicate:bob"、"late:alice"、"other_call:c9"
    """

    call_id: str
    status: str = "idle"
    early: tuple | None = None
    decided_by: str | None = None
    ignored: tuple = ()


TERMINAL = ("approved", "rejected", "timed_out")


def is_approved(state: ApprovalState) -> bool:
    """只有明确批准才放行；超时、拒绝、还在等，一律不放行（fail closed）。"""
    return state.status == "approved"


def on_event(state: ApprovalState, event: ApprovalEvent) -> ApprovalState:
    """纯函数：给定当前状态和一个事件，返回新状态。规则：

    1. event.call_id 与 state.call_id 不同 → 状态不变，ignored 追加 f"other_call:{event.call_id}"。
    2. request：
       - idle 且有 early → 直接进入终态（early 为 True → approved，否则 rejected），decided_by = early 的 by，early 清空；
       - idle 且没有 early → waiting；
       - 其他状态（重复的 request，比如重放）→ 原样返回。
    3. decision：
       - idle：还没有 early → 记为 early=(approved, by)；已经有 early → ignored 追加 f"duplicate:{by}"（第一个决定生效）；
       - waiting → approved / rejected，decided_by = by；
       - approved / rejected → ignored 追加 f"duplicate:{by}"（包括"已拒绝后再批准"：不生效）；
       - timed_out → ignored 追加 f"late:{by}"（超时已经按拒绝处理，迟到的批准不能复活它）。
    4. timeout：
       - waiting → timed_out，decided_by = "system:timeout"；
       - 其他状态 → 原样返回（过期的定时器）。
    5. 其他 kind → ValueError。
    """
    # 提示：用 dataclasses.replace(state, ...) 生成新状态；state.ignored 是元组，追加用 state.ignored + ("...",)
    raise NotImplementedError("TODO: 实现审批状态机")


# ============================================================================================
# (c) 确定性检查
# ============================================================================================

# 精确匹配的"完整名字"（导入别名已经展开）
BANNED_CALLS = {
    "time.time",
    "time.time_ns",
    "time.monotonic",
    "time.perf_counter",
    "time.sleep",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.datetime.today",
    "datetime.date.today",
    "uuid.uuid1",
    "uuid.uuid4",
    "os.urandom",
    "os.getenv",
    "urllib.request.urlopen",
    "open",
}
# 这些模块里的任何调用都算（随机数、HTTP、套接字）
BANNED_PREFIXES = ("random.", "secrets.", "requests.", "httpx.", "aiohttp.", "socket.")


def _dotted(node: ast.AST) -> str | None:
    """把 a.b.c 形式的表达式还原成字符串；遇到调用、下标等（如 workflow.random().randint）返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _aliases(tree: ast.AST) -> dict[str, str]:
    """本地名字 → 完整名字：import time as t → {"t": "time"}；from datetime import datetime → {"datetime": "datetime.datetime"}。"""
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    table[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    table[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                table[a.asname or a.name] = f"{node.module}.{a.name}"
    return table


def is_deterministic_safe(source_code: str) -> list[str]:
    """找出 workflow 代码里的非确定性调用，返回 ["line N: 完整名字", ...]，按 (行号, 列号) 排序。

    - 先展开导入别名：import time as t; t.time() → "time.time"；from uuid import uuid4; uuid4() → "uuid.uuid4"；
    - 如果源码里有用 @workflow.defn（或 @workflow.defn(...)）装饰的类，只检查这些类的内部
      （同一个文件里的 activity 可以合法地读时钟、发 HTTP）；否则检查整段源码；
    - 命中 BANNED_CALLS（精确匹配）或 BANNED_PREFIXES（前缀匹配）的调用都要报告；
    - workflow.now()、workflow.random().randint()、workflow.uuid4()、asyncio.sleep() 都是安全的。
    返回空列表表示没有发现问题（注意：静态检查不可能发现所有问题，见 README）。
    """
    # 提示：
    #   1. tree = ast.parse(source_code)；table = _aliases(tree)
    #   2. 找出被 @workflow.defn 装饰的类（装饰器可能是 ast.Call，也可能直接是属性；名字要先用 table 展开，
    #      展开后等于 "temporalio.workflow.defn" 或 "workflow.defn" 都算）；没有这样的类就检查整棵树
    #   3. 遍历 ast.Call 节点，_dotted(node.func) 得到名字，把第一段按 table 展开，再和 BANNED_* 比较
    #   4. 结果去重、按 (行号, 列号) 排序，格式 f"line {lineno}: {完整名字}"
    raise NotImplementedError("TODO: 用 ast 找出非确定性调用")
