"""ITBuddy —— 企业 IT 服务台 Agent（hello-agent-system 毕业项目）。

    backend.py   模拟的企业后端（多租户员工目录 / 工单 / 知识库 / 账号 / 系统状态），SQLite，跨进程共享，下游幂等
    storage.py   检查点 / 幂等记录 / 审计日志（所有进程共享的 SQLite 表）
    tools.py     6 个 async 工具，按 read / write / dangerous 分级
    policies.py  参数级授权、增强审计、提示词泄露检测
    prompts.py   带版本号的系统提示词
    agent.py     build_agent()：把 agentkit 的全部企业级能力组装起来
    offline.py   离线剧本模型（部署演示、端到端测试用）
"""

from .agent import ROLE_TOOLS, ITBuddyAgent, build_agent, build_llm, find_hook, next_history, visible_tools_for
from .backend import Backend
from .offline import offline_llm
from .policies import ArgumentPolicy, CanaryGuard, ITBuddyAuditLog, check_reset_permission
from .prompts import PROMPT_CANARY, PROMPT_VERSION, SYSTEM_PROMPT
from .storage import AuditStore, ITBuddyStores
from .tools import make_tools

__all__ = [
    "ROLE_TOOLS", "ITBuddyAgent", "build_agent", "build_llm", "find_hook", "next_history", "visible_tools_for",
    "Backend", "offline_llm", "ArgumentPolicy", "CanaryGuard", "ITBuddyAuditLog", "check_reset_permission",
    "PROMPT_CANARY", "PROMPT_VERSION", "SYSTEM_PROMPT", "AuditStore", "ITBuddyStores", "make_tools",
]
