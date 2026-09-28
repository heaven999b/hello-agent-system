"""运行状态与检查点（Checkpoint）。

为什么企业级 Agent 需要检查点？
- Agent 一次运行可能几十步、几分钟甚至几小时（等人审批）；
- 进程会重启、机器会宕机、部署会滚动更新；
- 没有检查点 = 崩溃后从头再来 = 重复调用工具、重复花钱、重复打扰用户。

做法：每走一步就把完整状态（消息历史、步数、用量、待审批）序列化存起来，
崩溃后用 run_id 加载，从断点继续。这就是 "durable execution（持久化执行）"，
Temporal、LangGraph checkpointer 本质上都是这个思路。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Protocol

from .types import Message, Usage


@dataclass
class RunState:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: list[Message] = field(default_factory=list)
    status: str = "running"  # running / completed / paused / max_steps / stopped / failed / cancelled
    step: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    tool_calls_count: int = 0
    approvals: dict[str, bool] = field(default_factory=dict)  # tool_call_id -> 是否批准
    metadata: dict = field(default_factory=dict)  # tenant_id / user_id / roles 等可信身份信息
    started_at: float = field(default_factory=time.time)
    output: str | None = None
    stop_reason: str | None = None
    pending: dict | None = None  # 暂停时等待审批的工具调用 {"id","name","arguments"}
    tool_log: list[dict] = field(default_factory=list)  # 本次运行中模型请求过的工具调用 {"id","name"}（含被拒绝的）
    approval_log: list[dict] = field(default_factory=list)  # 审批记录：谁、何时、批没批、意见
    active_seconds: float = 0.0  # 实际执行耗时（不含暂停等待审批的时间），供时长预算使用
    segment_started_at: float = field(default_factory=time.time)  # 本段执行（run 或某次 resume）的开始时间

    def to_dict(self) -> dict:
        """深拷贝的字典快照（可以放心修改）。"""
        return asdict(self)

    def to_json(self, **kwargs) -> str:
        """直接序列化成 JSON，不做中间深拷贝：检查点每一步都要调用，asdict 的深拷贝在高并发下是 CPU 大头。"""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["usage"] = asdict(self.usage)
        return json.dumps(data, ensure_ascii=False, **kwargs)

    @classmethod
    def from_dict(cls, d: dict) -> "RunState":
        d = dict(d)
        d["usage"] = Usage(**d.get("usage", {}))
        return cls(**d)


class Checkpointer(Protocol):
    def save(self, state: RunState) -> None: ...

    def load(self, run_id: str) -> RunState | None: ...


class InMemoryCheckpointer:
    """内存检查点：进程内有效，适合测试和演示暂停/恢复。"""

    def __init__(self):
        self._data: dict[str, str] = {}

    def save(self, state: RunState) -> None:
        # 存序列化后的 JSON 而不是对象引用，保证和文件/数据库版本行为一致
        self._data[state.run_id] = state.to_json()

    def load(self, run_id: str) -> RunState | None:
        raw = self._data.get(run_id)
        return RunState.from_dict(json.loads(raw)) if raw else None


class FileCheckpointer:
    """文件检查点：每个 run 一个 JSON 文件。生产中换成 Postgres / Redis / DynamoDB。

    原子写入：先写临时文件再 os.replace，保证崩溃时文件要么是旧的完整版本、要么是新的完整版本，
    绝不会出现写了一半的损坏文件。
    """

    def __init__(self, directory: str | Path = "runs"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def save(self, state: RunState) -> None:
        path = self._path(state.run_id)
        # 临时文件名必须唯一：两个进程同时保存同一个 run 时，固定的 .tmp 会互相踩。
        # （更根本的问题——旧持有者覆盖新检查点——需要 fencing token，见第 13 课 6.7 节与第 26 课的 PostgresCheckpointer。）
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(state.to_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, run_id: str) -> RunState | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))
