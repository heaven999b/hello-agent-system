"""ITBuddy —— 企业 IT 服务台 Agent（enterprise-agent-bootcamp 毕业项目）。

    backend.py   模拟的企业后端（多租户员工目录 / 工单 / 知识库 / 账号 / 系统状态）
    tools.py     6 个工具，按 read / write / dangerous 分级
    policies.py  参数级授权、增强审计、提示词泄露检测
    prompts.py   带版本号的系统提示词
    agent.py     build_agent()：把 agentkit 的全部企业级能力组装起来
"""

from .agent import ROLE_TOOLS, build_agent, build_llm, find_hook, next_history, visible_tools_for
from .backend import Backend
from .policies import ArgumentPolicy, CanaryGuard, ITBuddyAuditLog, check_reset_permission
from .prompts import PROMPT_CANARY, PROMPT_VERSION, SYSTEM_PROMPT
from .tools import make_tools

__all__ = [
    "ROLE_TOOLS", "build_agent", "build_llm", "find_hook", "next_history", "visible_tools_for",
    "Backend", "ArgumentPolicy", "CanaryGuard", "ITBuddyAuditLog", "check_reset_permission",
    "PROMPT_CANARY", "PROMPT_VERSION", "SYSTEM_PROMPT", "make_tools",
]
