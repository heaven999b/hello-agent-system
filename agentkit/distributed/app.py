"""worker 进程的"应用"装配：按 URL 打开队列、按 "module:attr" / "file.py:attr" 加载 handler 工厂。

单独成一个模块（而不是放在 worker.py 里）：agentkit.distributed 的 __init__ 要导出这些名字，
如果它导入 worker.py，`python -m agentkit.distributed.worker` 启动时 runpy 会发现模块已经被导入过，打印一行 RuntimeWarning。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkerContext:
    """交给 --app 工厂的上下文。"""

    queue_url: str
    worker_id: str
    queue: Any  # 已 setup 的队列（SQLiteJobQueue / PostgresJobQueue）
    db: Any = None  # SQLite：队列用的 SQLiteDB，工厂里创建检查点等对象时传 db= 共用连接
    options: dict = field(default_factory=dict)  # --opt key=value
    # 停机信号：收到 SIGTERM 时被置位。健康检查可以据此在排空期间返回"未就绪"（K8s readinessProbe）
    stop_event: Any = None
    # 额外的事件回调 f(name, info)：除了打印 JSON 行，还想把事件转成指标 / 进度推送时，工厂里设置它
    on_event: Any = None


def load_attr(spec: str):
    """"pkg.mod:attr" 或 "path/to/file.py:attr" → 对象。"""
    target, _, attr = spec.rpartition(":")
    if not target or not attr:
        raise ValueError(f"--app 格式应为 module:attr 或 file.py:attr，收到 {spec!r}")
    if target.endswith(".py") or os.sep in target:
        path = Path(target).resolve()
        name = f"_agentkit_app_{path.stem}_{abs(hash(str(path)))}"
        mod_spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[name] = module
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return getattr(module, attr)


async def open_queue(queue_url: str, **kwargs):
    """按 URL 打开队列：sqlite:///相对或绝对路径，或 postgresql://...（需要 agentkit[postgres]）。返回 (queue, db)。"""
    if queue_url.startswith("sqlite:///"):
        from .sqlite import SQLiteDB, SQLiteJobQueue

        db = SQLiteDB(queue_url[len("sqlite:///"):])
        queue = SQLiteJobQueue(db, **kwargs)
        await queue.setup()
        return queue, db
    if queue_url.startswith(("postgres://", "postgresql://")):
        from ..contrib.postgres import PostgresJobQueue

        queue = PostgresJobQueue(queue_url, **kwargs)
        await queue.setup()
        return queue, None
    raise ValueError(f"不支持的队列 URL：{queue_url!r}（sqlite:///path 或 postgresql://...）")
