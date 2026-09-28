"""第 27 课集成测试：用 WorkflowEnvironment.start_local() 拉起真实的 Temporal 开发服务器，把本课场景完整跑一遍。

不依赖 exercise.py（不计入"每个练习测试都因 NotImplementedError 失败"的要求）；没装 temporalio 时自动跳过。
contrib 模块更完整的测试在 tests/contrib/test_temporal.py。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("temporalio")

from agentkit.testing import load_sibling  # noqa: E402

sc = load_sibling(__file__, "scenario")
META = {"tenant_id": "acme", "user_id": "cs-agent-7", "roles": ["support"]}


async def _approval_flow():
    from temporalio.testing import WorkflowEnvironment

    from agentkit.contrib import temporal as kt

    try:
        env = await WorkflowEnvironment.start_local()
    except Exception as e:  # noqa: BLE001 —— 例如首次运行要下载 Temporal CLI，但 CI 没有网络
        pytest.skip(f"无法启动 Temporal 开发服务器：{e}")
    async with env:
        tq = f"it-{uuid.uuid4().hex[:8]}"
        async with kt.make_worker(env.client, tq, sc.offline_llm_factory(), sc.TOOLS):
            h = await kt.start_agent(env.client, "订单 A1001 申请退款", META, task_queue=tq, workflow_id=f"refund-{tq}")
            for _ in range(200):
                st = await kt.agent_status(env.client, h.id)
                if st.status == "waiting_approval":
                    break
                await asyncio.sleep(0.05)
            outcome = await kt.approve(env.client, h.id, st.pending_approvals[0]["call_id"], True, by="zhang.manager", wait=True)
            result = await h.result()
            history = await h.fetch_history()
        return st, outcome, result, kt.activity_attempts(history)


async def test_refund_workflow_waits_for_approval_then_refunds_exactly_once():
    before = set(sc.LEDGER)
    st, outcome, result, attempts = await _approval_flow()
    assert st.pending_approvals[0]["name"] == "refund" and st.tools_called == ["lookup_order", "refund"]
    assert outcome == "accepted" and result.status == "completed" and "已为订单 A1001 退款 99.0 元" in result.output
    new = [k for k in sc.LEDGER if k not in before]
    assert len(new) == 1 and new[0].startswith(f"{result.workflow_id}:")  # 幂等键 = workflow_id:call_id
    assert [name for name, _ in attempts] == ["describe_tools", "llm_step", "execute_tool", "llm_step", "execute_tool", "llm_step"]
