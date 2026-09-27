"""反馈闭环 / 数据飞轮（第 16 课 问题 6）。

线上的每一个"👎"都是一条免费的测试用例 —— 前提是你把它接住了。流程：

    用户点踩 / 转人工 / 自动检测到异常
        → bad_case_to_inbox()   脱敏、带上版本和 trace 线索，进入"待标注收件箱"
        → 人工标注：这次到底错在哪、正确的行为是什么（填写 expect）
        → to_eval_case()        变成标准的评估用例，追加进回归评估集
        → 下一次发布前，第 11 课的 run_eval 会跑到它：同样的错误不会再犯第二次

为什么中间一定要有"人工标注"？点踩只说明"用户不满意"，不说明"正确答案是什么"：
可能是 Agent 错了，也可能是政策本来就不允许、用户不高兴。没有标注的 bad case 直接进评估集，只会制造噪声。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agentkit import RunResult, redact_pii
from agentkit.evals import EvalCase

EVAL_FIELDS = ("id", "input", "expect", "metadata", "tags")


def bad_case_to_inbox(result: RunResult, user_input: str, feedback: dict, *, prompt_version: str, trace_id: str | None = None) -> dict:
    """把一次被差评的运行变成一条"待标注"记录。

    - 输入和输出都先脱敏（redact_pii）：评估集会被很多人看、会进 git，不能带着手机号、身份证号；
    - metadata 只保留租户和角色，**丢掉 user_id**：复现问题需要的是"什么权限的人"，不是"哪个人"；
    - 记下 prompt 版本、run_id、trace_id：标注的人要能回到现场看完整轨迹。
    """
    return {
        "id": f"prod-{result.run_id}",
        "input": redact_pii(user_input),
        "expect": {},  # 留给人工填写
        "metadata": {k: v for k, v in result.metadata.items() if k in ("tenant_id", "roles")},
        "tags": ["from_prod", "needs_label", f"feedback:{feedback.get('reason', 'unknown')}"],
        "source": {
            "run_id": result.run_id,
            "trace_id": trace_id,
            "prompt_version": prompt_version,
            "status": result.status,
            "tools": result.tools_called(),
            "output": redact_pii(result.output or ""),
            "comment": redact_pii(feedback.get("comment", "")),
            "received_at": time.time(),
        },
    }


def to_eval_case(labeled: dict) -> EvalCase:
    """标注完成的记录 → 标准评估用例。没有填 expect 的记录拒绝入库。"""
    if not labeled.get("expect"):
        raise ValueError(f"{labeled.get('id')}: 还没有标注 expect，不能进入评估集")
    tags = [t for t in labeled.get("tags", []) if t != "needs_label"] + ["regression"]
    return EvalCase(**{**{k: labeled[k] for k in EVAL_FIELDS if k in labeled}, "tags": tags})


def append_jsonl(path: str | Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
