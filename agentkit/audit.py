"""审计日志：谁、在什么时候、以什么身份、让 Agent 做了什么、结果如何。

审计日志 ≠ 调试日志：
- 调试日志给工程师排查问题，可以采样、可以丢；
- 审计日志给安全/合规/法务，必须完整、不可篡改（生产中写入 WORM 存储或追加式数据库），
  并且本身也要脱敏（审计日志里泄露身份证号同样违规）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .guardrails import redact_pii
from .hooks import Hook


class AuditLog(Hook):
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []

    def _write(self, record: dict) -> None:
        self.records.append(record)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def after_tool(self, state, call, result) -> None:
        self._write(
            {
                "ts": time.time(),
                "event": "tool_call",
                "run_id": state.run_id,
                "tenant_id": state.metadata.get("tenant_id"),
                "user_id": state.metadata.get("user_id"),
                "tool": call.name,
                "arguments": redact_pii(call.arguments),
                "ok": result.ok,
                "error_type": result.error_type,
                "approved": state.approvals.get(call.id),
                "approved_by": next((a["by"] for a in reversed(state.approval_log) if a["call_id"] == call.id), None),
            }
        )
        return None

    def on_run_end(self, state) -> None:
        self._write(
            {
                "ts": time.time(),
                "event": "run_end",
                "run_id": state.run_id,
                "tenant_id": state.metadata.get("tenant_id"),
                "user_id": state.metadata.get("user_id"),
                "status": state.status,
                "stop_reason": state.stop_reason,
                "steps": state.step,
                "tokens": state.usage.total,
                "cost_usd": round(state.cost_usd, 6),
                "pending_approval": state.pending,  # 暂停时：在等谁批什么
            }
        )
