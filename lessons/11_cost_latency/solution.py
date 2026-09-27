"""第 11 课练习参考答案。先自己写，卡住了再看。"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Callable

from agentkit.llm import LLM, LLMError
from agentkit.types import LLMResponse, Message, Usage

# =====================================================================
# 练习 (a)：cache_key
# =====================================================================


def cache_key(messages: list[Message], tools: list[dict] | None, model: str, tenant_id: str) -> str:
    """见 exercise.py 的说明。"""
    if not tenant_id:
        raise ValueError("缓存键必须包含 tenant_id")
    payload = {"tenant_id": tenant_id, "model": model, "tools": tools or [], "messages": messages}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# =====================================================================
# 练习 (b)：CascadeLLM.chat
# =====================================================================

Validator = Callable[[list[Message], LLMResponse], bool]


class CascadeLLM:
    """见 exercise.py 的说明。"""

    def __init__(self, small: LLM, large: LLM, validator: Validator):
        self.small = small
        self.large = large
        self.validator = validator
        self.model = f"cascade({small.model}→{large.model})"
        self.calls = 0
        self.escalations = 0
        self.reasons: dict[str, int] = {}
        self.wasted = Usage()
        self._lock = threading.Lock()

    @property
    def escalation_rate(self) -> float:
        return self.escalations / self.calls if self.calls else 0.0

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        with self._lock:
            self.calls += 1
        try:
            draft = self.small.chat(messages, tools, **kwargs)
        except LLMError:
            return self._escalate("error", messages, tools, kwargs)

        try:
            ok = bool(self.validator(messages, draft))
            reason = "rejected"
        except Exception:  # noqa: BLE001
            ok, reason = False, "validator_error"
        if ok:
            return draft
        with self._lock:
            self.wasted = self.wasted + draft.usage
        return self._escalate(reason, messages, tools, kwargs)

    def _escalate(self, reason: str, messages, tools, kwargs) -> LLMResponse:
        with self._lock:
            self.escalations += 1
            self.reasons[reason] = self.reasons.get(reason, 0) + 1
        return self.large.chat(messages, tools, **kwargs)


# =====================================================================
# 练习 (c)：cost_by_tenant
# =====================================================================


def cost_by_tenant(spans: list[dict]) -> dict[str, float]:
    """见 exercise.py 的说明。"""
    tenant_of_trace = {
        s["trace_id"]: s.get("attrs", {}).get("tenant.id") or "unknown" for s in spans if s.get("parent_id") is None
    }
    best: dict[str, dict] = {}  # run_id -> 成本最大的那个 span
    for s in spans:
        attrs = s.get("attrs", {})
        if "agent.cost_usd" not in attrs:
            continue
        run_id = attrs["run_id"]
        if run_id not in best or attrs["agent.cost_usd"] > best[run_id]["attrs"]["agent.cost_usd"]:
            best[run_id] = s

    totals: dict[str, float] = {}
    for s in best.values():
        tenant = tenant_of_trace.get(s["trace_id"], "unknown")
        totals[tenant] = totals.get(tenant, 0.0) + s["attrs"]["agent.cost_usd"]
    return {t: round(c, 6) for t, c in totals.items()}
