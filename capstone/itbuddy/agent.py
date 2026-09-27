"""把 agentkit 的企业级能力组装成 ITBuddy。

这个文件是整个毕业项目最值得细读的地方：**每一行装配都对应一个企业级决策**。
一个 Agent = 模型 + 工具 + 提示词 + 一串 Hook + 状态/追踪/幂等基础设施。

    用户输入
      │
      ▼  on_run_start          ① InputGuard       直接注入 / 超长输入 → 直接拦截，不花一分钱模型费
      ▼  context_strategy      SlidingWindow      历史过长时按"块"截断，不拆散 tool_calls 与结果
      ▼  before_llm/after_llm  ② BudgetHook       token / 金额 / 执行时长超限 → 优雅停止
      ▼  visible_tools         ④ PermissionPolicy RBAC：模型根本看不到无权使用的工具
      ▼  LLM（ResilientLLM：重试 → 熔断 → 降级到备用模型）
      ▼  before_tool           ② BudgetHook → ③ ArgumentPolicy → ④ PermissionPolicy（RBAC → 审批）
      ▼  工具执行（ToolRegistry：参数校验 / 超时 / 截断 / 幂等）
      ▼  after_tool            ⑤ ToolOutputGuard（包裹不可信数据）→ ⑥ AuditLog（记录）
      ▼  on_final              ⑦ CanaryGuard（提示词泄露）→ ⑧ OutputGuard（PII 脱敏）
      ▼  on_run_end            ⑥ AuditLog（run_end 含待审批操作 / 安全事件）
    最终回答
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from agentkit import (
    Agent,
    BudgetHook,
    FileCheckpointer,
    Hook,
    IdempotencyStore,
    InputGuard,
    OutputGuard,
    PermissionPolicy,
    ResilientLLM,
    RunResult,
    RunState,
    SlidingWindow,
    ToolCall,
    ToolOutputGuard,
    Tracer,
    default_llm,
    jsonl_exporter,
)
from agentkit.config import env
from agentkit.llm import LLM
from agentkit.state import Checkpointer
from agentkit.types import Message

from .backend import Backend
from .policies import ArgumentPolicy, CanaryGuard, ITBuddyAuditLog, reset_password_rule
from .prompts import PROMPT_CANARY, SYSTEM_PROMPT
from .tools import make_tools

DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"  # capstone/runs（已被 .gitignore 忽略）

# RBAC：角色 → 可用工具。原则是"默认拒绝"：没列出来的工具，这个角色就看不到、调不了。
# 注意 reset_password 对 employee 是开放的 —— "能不能重置别人"是参数级问题，交给 ArgumentPolicy。
ROLE_TOOLS: dict[str, set[str]] = {
    "employee": {"search_kb", "check_system_status", "get_my_tickets", "create_ticket", "reset_password"},
    "it_admin": {"*"},
}

Approver = Callable[[ToolCall, RunState], bool]
H = TypeVar("H", bound=Hook)


def build_llm(llm: LLM | None = None, fallback_llms: Sequence[LLM] | None = None) -> ResilientLLM:
    """模型层：主模型 + 可选备用模型，外面套一层 ResilientLLM（重试 → 熔断 → 降级）。

    传入 llm 时（比如测试用的 ScriptedLLM）也会被包一层，保证测试和生产走的是同一条代码路径。
    """
    if isinstance(llm, ResilientLLM):
        return llm
    if llm is None:
        llm = default_llm()
        if fallback_llms is None:
            name = env("LLM_FALLBACK_MODEL")
            # 备用模型名和主模型相同就没有意义：同一个模型宕了，"降级"到它自己也没用
            fallback_llms = [default_llm(name)] if name and name != llm.model else []
    return ResilientLLM(llm, list(fallback_llms or []), max_attempts=3, failure_threshold=5, reset_timeout=30)


def build_agent(
    llm: LLM | None = None,
    *,
    backend: Backend | None = None,
    runs_dir: str | Path | None = None,
    checkpointer: Checkpointer | None = None,
    approver: Approver | None = None,
    tracer: Tracer | None = None,
    idempotency_store: IdempotencyStore | None = None,
    fallback_llms: Sequence[LLM] | None = None,
    deny_tools: set[str] | None = None,
    max_steps: int = 8,
    max_tokens: int = 60_000,
    max_cost_usd: float = 0.10,
    max_tool_calls: int = 8,
    max_seconds: float = 120,
    context_max_tokens: int = 6000,
) -> Agent:
    """组装 ITBuddy。所有"外部依赖"都可以注入，这样测试可以做到离线、零成本、互不干扰。

    llm:        None → 按 .env 用真实模型（+ LLM_FALLBACK_MODEL 备用）；测试时传 ScriptedLLM。
    backend:    None → 新建一份种子数据。调用方需要查看后端状态（测试、评估）时自己创建再传进来。
    runs_dir:   审计 / 追踪 / 检查点的根目录，默认 capstone/runs；测试传 pytest 的 tmp_path。
    approver:   None → 高危操作抛 PauseRun、状态落盘，等外部审批后 agent.approve()（异步审批）；
                传函数 → 同步审批（适合脚本场景）。
    deny_tools: 紧急开关（kill switch）。默认读环境变量 ITBUDDY_DISABLED_TOOLS（逗号分隔），
                发现某个工具有漏洞时无需发版就能全局禁用它。
    """
    backend = backend or Backend()
    runs = Path(runs_dir) if runs_dir is not None else DEFAULT_RUNS_DIR
    runs.mkdir(parents=True, exist_ok=True)
    if deny_tools is None:
        deny_tools = {t.strip() for t in (os.environ.get("ITBUDDY_DISABLED_TOOLS") or "").split(",") if t.strip()}

    # ---------------------------------------------------------------- Hook 顺序（顺序即语义！）
    #
    # agentkit 的调用规则：
    #   - before_tool 按列表顺序执行，**第一个返回拒绝原因的 Hook 生效，后面的不再执行**；
    #     抛出 StopRun / PauseRun 则立刻中断。
    #   - after_tool / on_final 按顺序"链式"执行：前一个 Hook 的改写结果交给下一个。
    #   - 即使 before_tool 拒绝了，after_tool 仍然会对"拒绝结果"执行 —— 所以审计能记录到拒绝。
    #   - PauseRun（等待审批）时 after_tool 不会执行；"在等谁批什么"由 on_run_end 的 run_end 记录（pending_approval 字段）。
    hooks: list[Hook] = [
        # ① 输入护栏放最前：被拦截的请求连模型都不调用，零成本；也避免恶意输入进入历史和检查点。
        InputGuard(max_chars=4000),
        # ② 预算：before_tool 里它排在审批之前 —— 预算已耗尽的运行，不应该再去打扰审批人。
        #    max_seconds 只统计"实际执行时间"（state.active_seconds），异步审批等几个小时不算在内；
        #    默认 120s 约为评估实测最长单次运行（14.8s）的 8 倍：正常请求碰不到，卡死的运行会被截停。
        BudgetHook(max_tokens=max_tokens, max_cost_usd=max_cost_usd, max_tool_calls=max_tool_calls,
                   max_seconds=max_seconds),
        # ③ 参数级授权（ABAC）必须在审批之前：
        #    普通员工"重置同事密码"这种注定失败的请求，不应该出现在审批人的队列里。
        #    审批人每天被无效请求轰炸，最后就会不看内容一路点"同意"（审批疲劳）—— 这比没有审批更危险。
        ArgumentPolicy({"reset_password": reset_password_rule(backend)}),
        # ④ RBAC + 人工审批：先查"这个角色能不能用这个工具"，再对 dangerous 工具发起审批。
        #    同时负责 visible_tools：无权使用的工具根本不出现在发给模型的工具列表里。
        PermissionPolicy(role_tools=ROLE_TOOLS, ask_risks={"dangerous"}, deny_tools=deny_tools, approver=approver),
        # ⑤ 工具输出包进 <untrusted_data>，发现疑似指令再加警告（spotlighting）。拒绝/报错结果不包裹。
        ToolOutputGuard(),
        # ⑥ 审计放在 after_tool 链的最后：它记录的是"最终真正进入模型上下文的那个结果"。
        #    审计本身只写 ok / error_type / 脱敏后的参数，不写工具输出全文（避免审计日志变成数据泄露源）。
        ITBuddyAuditLog(runs / "audit.jsonl"),
        # ⑦ 先查提示词泄露（看原始输出），⑧ 再做 PII 脱敏。两者都只作用于最终回答。
        CanaryGuard(PROMPT_CANARY),
        OutputGuard(),
    ]

    return Agent(
        build_llm(llm, fallback_llms),
        make_tools(backend),
        system_prompt=SYSTEM_PROMPT,
        name="itbuddy",
        max_steps=max_steps,
        hooks=hooks,
        # 选 SlidingWindow 而不是 SummarizingCompactor：IT 服务台对话很短（实测单轮最多 3 步），截断几乎没有损失；
        # 摘要要多一次模型调用，而且摘要是模型对不可信工具输出的"转述"，会丢掉 <untrusted_data> 标签（见 DESIGN.md ADR-004）。
        context_strategy=SlidingWindow(max_tokens=context_max_tokens),
        # 检查点落盘：进程重启后仍能恢复；异步审批时状态就存在这里。
        checkpointer=checkpointer or FileCheckpointer(runs / "checkpoints"),
        # 追踪：agentkit 在写入 span 前已对 tool.arguments / tool.result_preview 做 PII 脱敏
        # （这是 ITBuddy 构建过程中发现并推动框架修复的问题，见 README 第 9 节）。
        tracer=tracer or Tracer(exporter=jsonl_exporter(runs / "traces.jsonl")),
        # 第一层幂等（进程内）。第二层在后端（见 tools.create_ticket）。
        idempotency_store=idempotency_store or IdempotencyStore(),
    )


# ---------------------------------------------------------------------------- 给 app / server 用的小工具


def find_hook(agent: Agent, cls: type[H]) -> H | None:
    """从 agent.hooks 里取出某类 Hook（比如拿到审计日志来记录审批人）。"""
    return next((h for h in agent.hooks if isinstance(h, cls)), None)


def visible_tools_for(agent: Agent, metadata: dict) -> list[str]:
    """某个身份能看到哪些工具 —— 和 Agent 运行时给模型展示的工具列表是同一套逻辑。"""
    state = RunState(metadata=dict(metadata))
    names = agent.registry.names()
    for h in agent.hooks:
        names = h.visible_tools(state, names)
    return names


def next_history(previous: list[Message], result: RunResult) -> list[Message]:
    """根据本轮结果，计算下一轮要传给 agent.run(history=...) 的历史。

    现在直接使用框架提供的 result.history：输入被拦截时它保留之前的历史，运行中止时未执行的
    tool_calls 也已补上"未执行"结果，保证下一轮消息协议合法（这两个坑都是 ITBuddy 发现并推动框架修复的）。

    保留这个函数，是为了让"会话历史策略"有一个统一的入口：以后要做"若干轮之后丢弃旧的工具输出"
    （DESIGN.md 未决问题）之类的策略，只改这里。previous 只在结果里没有任何消息时兜底使用。
    """
    return result.history or previous
