"""第 20 课：四种实现共用的"任务"——假知识库、工具、问题、审批人和结果格式。

为了让对比公平，四个实现（agentkit / DSPy / LangGraph / OpenAI Agents SDK）只在"框架怎么用"上不同：
  - 同一个问题 QUESTION、同一段系统提示 SYSTEM_PROMPT；
  - 同一组工具函数（普通 Python 函数，带类型注解和 docstring，四个框架都能直接包装）；
  - 同一个审批人 auto_approver（demo 里自动批准，模拟"审批人点了同意"）；
  - 同一种结果格式 FrameworkResult，方便 demo.py 打表对比。

这个文件**不依赖任何框架**，只用标准库和 agentkit 的配置读取。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agentkit.config import env

# ---------------------------------------------------------------- 任务

QUESTION = (
    "我是 alice。VPN 连不上，客户端提示“证书已过期”；另外我连续输错了几次密码，账号好像被锁了，"
    "麻烦帮我重置一下密码。"
)

SYSTEM_PROMPT = (
    "你是公司 IT 服务台助手。回答前先用 search_kb 查知识库，按知识库里的步骤回答，不要编造知识库里没有的内容。"
    "需要了解当前用户的账号情况时调用 get_account_status。"
    "用户明确要求重置密码时直接调用 reset_password（系统会自动转人工审批，你不需要事先征求确认）。"
    "最后用简洁的中文答复用户，并注明参考的知识库文章编号（如 KB-101）。"
)

# 需要人工审批的工具。框架各有各的写法：agentkit 的 risk="dangerous"、LangGraph 的 interrupt()、
# OpenAI Agents SDK 的 needs_approval=True；DSPy 没有暂停机制，只能在工具里同步问审批人。
NEEDS_APPROVAL = frozenset({"reset_password"})

KB_DOCS = [
    {
        "id": "KB-101",
        "title": "VPN 提示证书已过期",
        "keywords": ["vpn", "证书", "过期", "certificate"],
        "content": "打开 VPN 客户端 → 设置 → 证书 → 点击“更新证书”，完成后重新连接。"
        "若提示无权限，重启电脑后再试一次；仍失败请提交工单并附上客户端截图。",
    },
    {
        "id": "KB-102",
        "title": "VPN 连接失败通用排查",
        "keywords": ["vpn", "连不上", "连接失败", "断线"],
        "content": "先确认网络正常；再检查系统时间是否准确（时间偏差超过 5 分钟会导致证书校验失败）；"
        "最后确认客户端版本不低于 5.2。",
    },
    {
        "id": "KB-201",
        "title": "账号被锁定",
        "keywords": ["锁定", "被锁", "输错", "密码", "解锁"],
        "content": "连续 5 次输错密码会自动锁定 30 分钟。可以等待自动解锁，或由 IT 为你重置密码"
        "（重置会同时解除锁定，需要人工审批）。",
    },
    {
        "id": "KB-202",
        "title": "密码重置流程",
        "keywords": ["重置", "密码", "忘记密码"],
        "content": "IT 审批通过后，系统会向员工登记邮箱发送一次性重置链接，链接 15 分钟内有效。"
        "IT 人员不会通过电话或聊天索要你的密码。",
    },
    {
        "id": "KB-301",
        "title": "打印机卡纸",
        "keywords": ["打印机", "卡纸", "打印"],
        "content": "关闭电源，打开前盖取出卡住的纸张，确认纸盒里的纸没有受潮后重新开机。",
    },
    {
        "id": "KB-401",
        "title": "邮箱空间已满",
        "keywords": ["邮箱", "空间", "满了", "发不出"],
        "content": "清理“已发送”和“已删除”文件夹里的大附件，或在自助门户申请扩容到 10GB。",
    },
]

ACCOUNTS = {
    "alice": {"locked": True, "failed_logins": 5, "email": "a***@corp.example", "department": "财务部"},
    "bob": {"locked": False, "failed_logins": 0, "email": "b***@corp.example", "department": "研发部"},
}


class ITDesk:
    """一次请求一个 ITDesk：记录工具调用和副作用，方便四个实现用同一把尺子计数。

    **身份不交给模型**（第 03 课）：当前用户 user_id 在构造时由系统传入，工具函数通过闭包拿到它，
    模型看到的工具参数里根本没有"用户名"。这种"闭包注入"在四个框架里写法完全一样；
    各框架也有自己的上下文注入机制（agentkit 的 ToolContext、OpenAI 的 RunContextWrapper、
    LangGraph 的 Runtime context），DSPy 没有，闭包是最通用的办法。
    """

    def __init__(self, user_id: str = "alice"):
        self.user_id = user_id
        self.log: list[tuple[str, dict]] = []  # 每次工具调用（名字, 参数）
        self.resets: list[str] = []  # 真正执行了的密码重置（副作用）

    def functions(self) -> dict[str, Callable[..., str]]:
        """返回三个普通函数（不是方法、没有 self），四个框架都能直接包装成工具。"""
        desk = self

        def search_kb(query: str) -> str:
            """在公司 IT 知识库中搜索，返回最相关的 1-2 篇文章（带文章编号）。

            Args:
                query: 搜索关键词，例如“VPN 证书过期”
            """
            desk.log.append(("search_kb", {"query": query}))
            q = query.lower()
            scored = []
            for doc in KB_DOCS:
                score = sum(1 for kw in doc["keywords"] if kw in q)
                if score:
                    scored.append((score, doc))
            scored.sort(key=lambda x: -x[0])
            if not scored:
                return "知识库中没有找到相关文章。"
            return "\n".join(f"[{d['id']}] {d['title']}：{d['content']}" for _, d in scored[:2])

        def get_account_status() -> str:
            """查询当前用户（发起对话的员工本人）的账号状态：是否锁定、失败登录次数、登记邮箱。"""
            desk.log.append(("get_account_status", {}))
            acct = ACCOUNTS.get(desk.user_id)
            if acct is None:
                return f"未找到用户 {desk.user_id} 的账号。"
            return json.dumps({"user": desk.user_id, **acct}, ensure_ascii=False)

        def reset_password(reason: str) -> str:
            """为当前用户重置密码：解除锁定，并向登记邮箱发送一次性重置链接。

            Args:
                reason: 重置原因，一句话，会写进审计记录
            """
            desk.log.append(("reset_password", {"reason": reason}))
            desk.resets.append(desk.user_id)
            acct = ACCOUNTS.get(desk.user_id, {})
            return f"已为 {desk.user_id} 解除锁定，并向 {acct.get('email', '登记邮箱')} 发送一次性重置链接（15 分钟内有效）。"

        return {"search_kb": search_kb, "get_account_status": get_account_status, "reset_password": reset_password}

    def tool_names(self) -> list[str]:
        return [name for name, _ in self.log]


# ---------------------------------------------------------------- 审批人

Approver = Callable[[str, dict], bool]


def auto_approver(tool_name: str, args: dict) -> bool:
    """demo 用的"审批人"：打印审批请求并自动批准。真实系统里这里是工单 / IM 卡片 / 审批流。"""
    print(f"    [审批] {tool_name}({json.dumps(args, ensure_ascii=False)}) → 批准（demo 自动批准，模拟审批人点了同意）")
    return True


def auto_rejecter(tool_name: str, args: dict) -> bool:
    print(f"    [审批] {tool_name}({json.dumps(args, ensure_ascii=False)}) → 拒绝")
    return False


# ---------------------------------------------------------------- 结果与配置


@dataclass
class FrameworkResult:
    framework: str
    version: str
    answer: str
    llm_calls: int  # 实际发给模型的请求数（run + resume 合计）
    tool_calls: list[str]  # 按顺序真正执行了的工具（来自 ITDesk.log）
    approvals: list[tuple[str, dict, bool]]  # 收到的审批请求：(工具名, 参数, 是否批准)
    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    notes: list[str] = field(default_factory=list)  # 各框架特有的观察：检查点数、Span 数等（给人看）
    extra: dict = field(default_factory=dict)  # 同样的观察，结构化版本（给测试和 demo 用）


def gateway_config() -> dict[str, str]:
    """三个框架都连同一个 OpenAI 兼容网关：地址、密钥、模型全部从 .env / 环境变量读取，代码里不写死。"""
    base_url, api_key, model = env("LLM_BASE_URL"), env("LLM_API_KEY"), env("LLM_MODEL", "gpt-5.5")
    if not api_key:
        raise RuntimeError("没有找到 LLM_API_KEY。请把 .env.example 复制为 .env 并填写，或设置环境变量。")
    return {"base_url": base_url or "https://api.openai.com/v1", "api_key": api_key, "model": model or "gpt-5.5"}


def print_result(res: FrameworkResult, answer_chars: int = 400) -> None:
    answer = res.answer.strip().replace("\n\n", "\n")
    if len(answer) > answer_chars:
        answer = answer[:answer_chars] + "……"
    print("  答复：")
    for line in answer.splitlines():
        print(f"    │ {line}")
    tokens = f"{res.input_tokens}→{res.output_tokens}" if res.input_tokens is not None else "n/a"
    print(f"  模型调用 {res.llm_calls} 次 ｜ 工具 {res.tool_calls} ｜ tokens {tokens} ｜ 耗时 {res.seconds:.1f}s")
    for name, args, ok in res.approvals:
        print(f"  审批记录：{name}({json.dumps(args, ensure_ascii=False)}) → {'批准' if ok else '拒绝'}")
    for note in res.notes:
        print(f"  · {note}")


# ---------------------------------------------------------------- 代码行数统计

BOOTSTRAP_START = "# --- bootstrap"
BOOTSTRAP_END = "# --- end bootstrap"


def count_code_lines(path: str | Path) -> int:
    """有效代码行数：不算空行、注释、docstring，也不算四个文件里完全相同的 bootstrap 段。"""
    src = Path(path).read_text(encoding="utf-8")
    lines = src.splitlines()
    skip: set[int] = set()
    in_boot = False
    for i, line in enumerate(lines, 1):
        if line.strip().startswith(BOOTSTRAP_START):
            in_boot = True
        if in_boot:
            skip.add(i)
        if line.strip().startswith(BOOTSTRAP_END):
            in_boot = False
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                skip.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return sum(1 for i, line in enumerate(lines, 1) if i not in skip and line.strip() and not line.strip().startswith("#"))
