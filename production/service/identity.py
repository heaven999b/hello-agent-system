"""身份：API key → (租户, 用户, 角色, 套餐)。

这是参考服务里**最不该照抄到生产**的一块，刻意写得很小：
- 生产环境应当由 API 网关 / Ingress（Envoy、Kong、云厂商网关）验证 JWT（OIDC），
  再把验证过的身份放进请求头（例如 X-Tenant-Id、X-User-Id、X-Roles）注入给本服务；
  本服务只信任来自网关的内部请求（mTLS 或网络策略保证），不自己解析 token。
- 本地和测试用 API key：配置里只存 key 的 SHA-256，不存原文（泄露配置不等于泄露 key）。
- 身份一旦确定，就进入 RunState.metadata（tenant_id、user_id、roles），工具通过 ToolContext 拿到，
  **模型填不了**（第 03、09 课）；worker 侧 AgentJobHandler 还会用任务上的 tenant_id 覆盖 payload 里的值。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    roles: tuple[str, ...]
    plan: str = "free"
    department: str = ""

    def metadata(self) -> dict:
        """写进 RunState.metadata 的可信身份（CedarPolicy 的 build_entities 读 tenant_plan / department）。"""
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "roles": list(self.roles),
            "tenant_plan": self.plan,
            "department": self.department,
        }

    @property
    def is_approver(self) -> bool:
        return "it_admin" in self.roles


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(prefix: str = "sk-itdesk") -> str:
    return f"{prefix}-{secrets.token_urlsafe(18)}"


class KeyRing:
    """{sha256(key): 身份} 的只读映射。常数时间比较，避免按字节泄露的计时侧信道。"""

    def __init__(self, hashed: dict[str, dict]):
        self._entries = {h.lower(): self._principal(v) for h, v in hashed.items()}

    @staticmethod
    def _principal(v: dict) -> Principal:
        roles = v.get("roles") or ["employee"]
        return Principal(v["tenant"], v["user"], tuple(roles), v.get("plan", "free"), v.get("department", ""))

    def resolve(self, key: str | None) -> Principal | None:
        if not key:
            return None
        digest = hash_key(key)
        found = None
        for h, p in self._entries.items():  # 数量很少（本地 / 测试）；生产里身份来自网关，不走这里
            if hmac.compare_digest(h, digest):
                found = p
        return found

    def __len__(self) -> int:
        return len(self._entries)


# 本地演示用的身份（run_local 为每个身份随机生成 key，只把哈希交给 API 进程）
DEMO_IDENTITIES: dict[str, dict] = {
    "acme-alice": {"tenant": "acme", "user": "alice", "roles": ["employee"], "plan": "enterprise", "department": "rnd"},
    "acme-bob": {"tenant": "acme", "user": "bob", "roles": ["employee", "it_admin"], "plan": "enterprise", "department": "it"},
    "globex-carol": {"tenant": "globex", "user": "carol", "roles": ["employee"], "plan": "pro", "department": "finance"},
    "globex-erin": {"tenant": "globex", "user": "erin", "roles": ["employee", "it_admin"], "plan": "pro", "department": "it"},
    "noisy-nick": {"tenant": "noisy", "user": "nick", "roles": ["employee"], "plan": "free", "department": "sales"},
}


def demo_keys() -> tuple[dict[str, str], dict[str, dict]]:
    """返回 ({身份名: 原始 key}, {sha256: 身份})：前者给客户端（压测、测试），后者给 API 的 API_KEYS_JSON。"""
    raw = {name: new_key() for name in DEMO_IDENTITIES}
    hashed = {hash_key(k): DEMO_IDENTITIES[name] for name, k in raw.items()}
    return raw, hashed
