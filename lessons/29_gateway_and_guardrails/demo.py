"""第 29 课 Demo：模型网关、策略即代码与护栏服务。

    python lessons/29_gateway_and_guardrails/demo.py              # 真实模型：经 LiteLLM Router 调本地 OpenAI 兼容网关
    python lessons/29_gateway_and_guardrails/demo.py --offline    # 离线：LiteLLM mock_response + 回放一次真实运行的录制结果
    python lessons/29_gateway_and_guardrails/demo.py --record     # 真实运行，并把护栏评估结果写进 data/guard_eval_recording.json
    python lessons/29_gateway_and_guardrails/demo.py --only 1,4   # 只跑指定场景

四个场景（全部 async：一个事件循环，asyncio.run(main())）：
  1. 网关降级：主模型不可用时自动切到备用；降级前的重试要多等几秒；并发与流式首 token 延迟；网关部署配置
  2. Cedar 策略即代码：同一个请求，员工 / IT 管理员 / 跨租户用户分别得到什么判定；触发审批；async 审批函数；explain 输出；
     一个反直觉的坑：策略求值出错会被跳过（fail open），适配器必须 fail closed
  3. 护栏级联：正则 / LLM 分类器 / 级联 三种方式在带标签集合上的精确率、召回率、调用成本、延迟；阈值扫描
  4. 输入护栏的延迟代价："先判定再调主模型" vs "与主模型并行"

离线模式里"模拟"的部分都标了出来：网关的 429 / 500 / 延迟用的是 LiteLLM 自带的 mock_response（Router 的重试、
退避、降级逻辑照常真实执行，只是上游不发网络请求）；护栏评估回放一次真实运行的录制；场景 4 用 ScriptedLLM 扮演两个模型。
缺少可选依赖（litellm / cedarpy / pyyaml）时，对应场景会打印安装命令并跳过，整个 demo 仍以退出码 0 结束。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # import litellm 时不联网拉价格表（离线 / 内网环境必备）

from agentkit import Agent, ScriptedLLM, call_tool, reply, tool  # noqa: E402
from agentkit.config import env  # noqa: E402
from agentkit.state import RunState  # noqa: E402
from agentkit.types import LLMResponse  # noqa: E402

HERE = Path(__file__).resolve().parent
CONFIGS = HERE / "configs"
DATA = HERE / "data"
RECORDING = DATA / "guard_eval_recording.json"
INSTALL_HINT = 'pip install -e ".[prod,prod-local]"'


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 72 + f"\n  {title}\n" + "═" * 72, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def short(text: str | None, n: int = 100) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def missing(*modules: str) -> list[str]:
    return [m for m in modules if importlib.util.find_spec(m) is None]


def skip_section(mods: list[str]) -> None:
    info(f"⏭  缺少可选依赖 {mods}，跳过本场景。安装：{INSTALL_HINT}")


def real_llm():
    """真实模式的模型：装了 litellm 就经 LiteLLM Router，否则退回 agentkit 自带的 OpenAI 兼容客户端（两者都是 async 的）。"""
    if missing("litellm"):
        from agentkit import default_llm

        return default_llm()
    from agentkit.contrib.gateway import LiteLLMRouterLLM

    return LiteLLMRouterLLM.from_env()


# =====================================================================
# 场景 1：模型网关
# =====================================================================


def mock_model_list(primary_error: str, delay: float | None = None) -> list[dict]:
    """离线用的部署列表：LiteLLM 的 mock_response（LiteLLM 自带的模拟机制）可以写在每个部署的 litellm_params 里。
    "litellm.RateLimitError" / "litellm.InternalServerError" 这类字符串会让 LiteLLM 抛出对应的异常（已在 1.83 源码中核实）；
    mock_delay 让这次"调用"等若干秒（异步路径里是 asyncio.sleep）。被模拟的只有上游：Router 的重试、退避、冷却、
    降级都是真实执行的同一段代码。"""
    extra = {"mock_delay": delay} if delay else {}
    return [
        {"model_name": "gpt-5.5", "litellm_params": {"model": "openai/gpt-5.5", "api_key": "sk-offline-placeholder",
                                                     "mock_response": primary_error, **extra}},
        {"model_name": "gpt-5.6-luna", "litellm_params": {"model": "openai/gpt-5.6-luna", "api_key": "sk-offline-placeholder",
                                                          "mock_response": "（离线 mock）你好，我是备用模型 gpt-5.6-luna。", **extra}},
    ]


class AttemptCounter:
    """用 LiteLLM 的回调数"每次真正发往上游的请求"（包括重试），按模型记录时间点。"""

    def __init__(self):
        from litellm.integrations.custom_logger import CustomLogger

        outer = self

        class _Logger(CustomLogger):
            def log_pre_api_call(self, model, messages, kwargs):
                outer.attempts.append((round(time.perf_counter() - outer.t0, 2), model))

        self.logger = _Logger()
        self.attempts: list[tuple[float, str]] = []
        self.t0 = time.perf_counter()

    def __enter__(self):
        import litellm

        litellm.callbacks.append(self.logger)
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        import litellm

        litellm.callbacks.remove(self.logger)


async def scenario_gateway(offline: bool, async_n: int) -> None:
    banner("场景 1：模型网关 —— 主模型挂了，业务代码一行不改")
    if m := missing("litellm"):
        skip_section(m)
        return
    from agentkit import LLMError
    from agentkit.contrib.gateway import LiteLLMRouterLLM

    # ---- 1a / 1b / 1c：正常路径、降级、没有备用
    if offline:
        step("1a-1c（离线）：用 LiteLLM 的 mock_response 模拟主模型 gpt-5.5 返回 429，看 Router 如何降级")
        llm = LiteLLMRouterLLM(mock_model_list("litellm.RateLimitError"), fallbacks=[{"gpt-5.5": ["gpt-5.6-luna"]}], num_retries=0)
        resp = await llm.chat([{"role": "user", "content": "你好"}])
        info(f"请求的模型组：gpt-5.5 → 实际回答：{resp.model}  内容：{resp.content}")
        info(f"路由信息：{llm.last_route}")
        info(f"降级事件：{llm.events}")
        nofb = LiteLLMRouterLLM(mock_model_list("litellm.RateLimitError")[:1], num_retries=0)
        try:
            await nofb.chat([{"role": "user", "content": "你好"}])
        except LLMError as e:
            info(f"没有备用时：LLMError(status={e.status_code}, retryable={e.retryable}) —— 429 可重试，交给上层决定要不要再试")
    else:
        step("1a：LiteLLMRouterLLM.from_env() —— 主模型 LLM_MODEL，备用 LLM_FALLBACK_MODEL，key 只在内存里")
        llm = LiteLLMRouterLLM.from_env()
        info(f"{llm!r}（repr 里故意不打印 model_list：那里面有 api_key）")
        resp = await llm.chat([{"role": "user", "content": "用一句话说明什么是模型网关，不超过 30 字。"}])
        info(f"实际回答的模型：{resp.model}  用时 {llm.last_route['latency_s']}s  tokens {resp.usage.total}")
        info(f"🤖 {short(resp.content)}")

        step("1b：故意把主模型配成一个不存在的名字，看 Router 自动降级到备用模型")
        bad = LiteLLMRouterLLM.from_env(primary="gpt-5.5-does-not-exist")
        resp = await bad.chat([{"role": "user", "content": "只回复两个字：你好"}])
        info(f"请求的模型组：gpt-5.5-does-not-exist → 实际回答：{resp.model}  内容：{resp.content}")
        info(f"路由信息：{bad.last_route}")
        info(f"降级事件：{bad.events}")

        step("1c：同样的错误、但没有配置备用模型")
        nofb = LiteLLMRouterLLM.from_env(primary="gpt-5.5-does-not-exist", fallback="")
        try:
            await nofb.chat([{"role": "user", "content": "你好"}])
        except LLMError as e:
            info(f"LLMError(status={e.status_code}, retryable={e.retryable})：{short(str(e), 110)}")
            info("注意：上游返回的是 400，LiteLLM 却把它归类成 NotFoundError（按错误文本判断的）—— 映射时要看状态码和异常类型两样。")

    # ---- 1d：降级前的重试（mock，两种模式一样）
    step("1d（LiteLLM mock_response，不调用模型）：主模型返回 500 时，num_retries 决定了用户要多等多久才降级")
    for nr in (0, 2):
        llm = LiteLLMRouterLLM(mock_model_list("litellm.InternalServerError"), fallbacks=[{"gpt-5.5": ["gpt-5.6-luna"]}], num_retries=nr)
        with AttemptCounter() as counter:
            t0 = time.perf_counter()
            await llm.chat([{"role": "user", "content": "你好"}])
            took = time.perf_counter() - t0
        info(f"num_retries={nr}：共 {len(counter.attempts)} 次上游请求 {counter.attempts}，总耗时 {took:.2f}s；"
             f"x-litellm-attempted-retries 头 = {llm.last_route['attempted_retries']}")
    takeaway("重试发生在降级之前，而且带指数退避：num_retries=2 让用户多等了好几秒（等待期间让出事件循环，别的会话不受影响）。"
             "那个 attempted-retries 响应头只统计最终成功的模型组，主模型组的重试它看不到——监控要用回调或网关日志。")

    # ---- 1e：并发
    step(f"1e：一个事件循环里同时挂 {async_n} 个请求 vs 一个一个来")
    await _async_concurrency(offline, async_n, LiteLLMRouterLLM)

    # ---- 1f：流式首 token 延迟
    step("1f：流式输出 —— 首 token 延迟（TTFT）vs 总耗时")
    await _stream_ttft(offline, LiteLLMRouterLLM)

    # ---- 1g：网关部署配置
    step("1g：LiteLLM Proxy 部署配置（configs/litellm-config.yaml）")
    if m := missing("yaml"):
        skip_section(m)
    else:
        import yaml

        cfg = yaml.safe_load((CONFIGS / "litellm-config.yaml").read_text(encoding="utf-8"))
        groups: dict[str, int] = {}
        for d in cfg["model_list"]:
            groups[d["model_name"]] = groups.get(d["model_name"], 0) + 1
        keys = [d["litellm_params"].get("api_key", "") for d in cfg["model_list"]]
        info(f"YAML 语法 ✅；模型组 {groups}（同名多项 = 组内负载均衡）")
        info(f"降级链：{cfg['router_settings']['fallbacks']}；Redis 共享限流计数：{'redis_host' in cfg['router_settings']}")
        info(f"所有 api_key 都是 os.environ/ 占位：{all(str(k).startswith('os.environ/') for k in keys)}")
        info("⚠️ 本机未启动 proxy 验证：.venv 里的 litellm 没装 proxy 额外依赖（pip install 'litellm[proxy]'）。")
        info("   model_list / router_settings 的写法由 tests/contrib/test_gateway.py 用 litellm.Router 实际加载验证。")
    takeaway("业务代码只依赖 await llm.chat()：从 ResilientLLM 换成 LiteLLMRouterLLM，再换成'把 base_url 指向网关'，Agent 一行都不用改。")


async def _async_concurrency(offline: bool, n: int, cls) -> None:
    if offline:
        llm = cls(mock_model_list("（离线 mock）2", delay=0.2)[:1], num_retries=0)
        info("（离线：LiteLLM 的 mock_response + mock_delay=0.2s，每次调用在上游前 asyncio.sleep 0.2 秒）")
    else:
        llm = cls.from_env()
    prompts = [f"只回复一个数字：{i} 加 1 等于几？" for i in range(n)]
    t0 = time.perf_counter()
    serial_lat = []
    for p in prompts:
        s = time.perf_counter()
        await llm.chat([{"role": "user", "content": p}])
        serial_lat.append(time.perf_counter() - s)
    serial = time.perf_counter() - t0

    async def one(p: str) -> float:
        s = time.perf_counter()
        await llm.chat([{"role": "user", "content": p}])
        return time.perf_counter() - s

    t0 = time.perf_counter()
    results = await asyncio.gather(*(one(p) for p in prompts), return_exceptions=True)
    conc = time.perf_counter() - t0
    ok = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    info(f"串行 {n} 个：总耗时 {serial:.2f}s（单个平均 {statistics.mean(serial_lat):.2f}s）")
    info(f"并发 {n} 个：总耗时 {conc:.2f}s，成功 {len(ok)}、失败 {len(errors)}；单个最慢 {max(ok) if ok else 0:.2f}s")
    if errors:
        info(f"失败示例：{short(str(errors[0]), 100)}")
    info(f"加速比 {serial / conc:.1f}×（上限取决于网关和上游的并发限额，不是你的事件循环）")


async def _stream_ttft(offline: bool, cls) -> None:
    from agentkit import StreamDone, TextDelta

    if offline:
        llm = cls(mock_model_list("（离线 mock）模型网关是所有模型调用的统一出口，负责密钥、路由、降级、预算和审计。")[:1], num_retries=0)
    else:
        llm = cls.from_env()
    t0 = time.perf_counter()
    first = None
    pieces = 0
    done: LLMResponse | None = None
    async for ev in llm.stream([{"role": "user", "content": "用三句话介绍模型网关的作用。"}]):
        if isinstance(ev, TextDelta):
            pieces += 1
            if first is None:
                first = time.perf_counter() - t0
        elif isinstance(ev, StreamDone):
            done = ev.response
    total = time.perf_counter() - t0
    info(f"首 token {first or 0:.2f}s，总耗时 {total:.2f}s，文本分片 {pieces} 个，模型 {done.model}，tokens {done.usage.total}"
         f"（其中推理 {done.usage.reasoning_tokens}）")
    info(f"🤖 {short(done.content, 90)}")
    takeaway("流式不减少总耗时，但把'用户盯着空白屏幕'的时间从总耗时降到首 token 延迟。推理模型的首 token 往往要等它先想完。")


# =====================================================================
# 场景 2：Cedar 策略即代码
# =====================================================================


@tool
def search_kb(query: str) -> str:
    """搜索公司 IT 知识库"""
    return "【VPN 指南】先检查网络，再重新登录客户端。"


@tool(risk="dangerous")
def reset_password(target_user_id: str) -> str:
    """为指定员工发送密码重置链接（链接只发到该员工本人的邮箱）"""
    return f"已向 {target_user_id} 的邮箱发送重置链接（模拟）"


@tool(risk="write")
def export_payroll(month: str) -> str:
    """导出某月工资单"""
    return f"{month} 工资单已导出（模拟）"


TOOLS = [search_kb, reset_password, export_payroll]
PEOPLE = {
    "alice（acme 普通员工，销售部）": {"tenant_id": "acme", "user_id": "alice", "roles": ["employee"], "department": "sales", "tenant_plan": "enterprise"},
    "ian（acme IT 管理员）": {"tenant_id": "acme", "user_id": "ian", "roles": ["it_admin"], "department": "it", "tenant_plan": "enterprise"},
    "mallory（globex 的 IT 管理员，跨租户）": {"tenant_id": "globex", "user_id": "mallory", "roles": ["it_admin"], "department": "it", "tenant_plan": "enterprise"},
}


def make_cedar_policy(audit=None, approver=None):
    from agentkit.contrib.policy import CedarPolicy, entity_args_context

    return CedarPolicy(
        CONFIGS / "policies.cedar",
        CONFIGS / "schema.cedarschema",
        tools=TOOLS,
        tool_tenants={"reset_password": "acme", "export_payroll": "acme"},  # acme 自己的 AD / HR 连接器
        context_fn=entity_args_context({"reset_password": {"target_user_id": ("target_user", "User")}}),
        audit=audit,
        approver=approver,
    )


async def scenario_cedar(offline: bool) -> None:
    banner("场景 2：Cedar 策略即代码 —— 同一个请求，三个人三种结果")
    if m := missing("cedarpy"):
        skip_section(m)
        return
    import cedarpy

    from agentkit.contrib.policy import CedarPolicy, build_entities, entity_ref, validate

    step("2a：用 schema 静态校验策略（放进 CI：写错属性名的策略合不进主干）")
    errors = validate(CONFIGS / "policies.cedar", CONFIGS / "schema.cedarschema")
    info(f"configs/policies.cedar 对 configs/schema.cedarschema 校验：{'✅ 0 个错误' if not errors else errors}")
    typo = 'permit (principal, action == Action::"call_tool", resource) when { principal.role.contains("it_admin") };'
    info(f"故意写错一个属性名（role 少了 s）：{validate(typo, CONFIGS / 'schema.cedarschema')}")

    audit: list[dict] = []
    policy = make_cedar_policy(audit.append)

    step('2b：请求 reset_password(target_user_id="bob")，三个人分别得到什么？')
    ctx = {"target_user": entity_ref("User", "bob")}
    for who, md in PEOPLE.items():
        d_call = policy.authorize(md, reset_password, "call_tool", ctx)
        d_auto = policy.authorize(md, reset_password, "call_tool_unattended", ctx)
        verdict = "❌ 拒绝" if not d_call.allowed else ("✅ 直接执行" if d_auto.allowed else "⏸ 允许，但需人工审批")
        info(f"{who:<30} {verdict}")
        info(f"{'':<30} call_tool → {d_call.decision}，explain={CedarPolicy.explain(d_call)}；"
             f"call_tool_unattended → {d_auto.decision}，explain={CedarPolicy.explain(d_auto)}")

    step("2c：alice 重置自己的密码；以及 IT 管理员导出工资单（forbid 优先于 permit）")
    alice, ian = PEOPLE["alice（acme 普通员工，销售部）"], PEOPLE["ian（acme IT 管理员）"]
    d = policy.authorize(alice, reset_password, "call_tool", {"target_user": entity_ref("User", "alice")})
    d2 = policy.authorize(alice, reset_password, "call_tool_unattended", {"target_user": entity_ref("User", "alice")})
    info(f"alice → reset_password(alice)：call_tool={d.decision} {policy.explain(d)}，unattended={d2.decision} {policy.describe(d2)}")
    d = policy.authorize(ian, export_payroll, "call_tool")
    info(f"ian → export_payroll：{d.decision}，explain={policy.explain(d)} —— it-admin-all-tools 也命中了，但 forbid 一票否决")
    finance = {**alice, "user_id": "fiona", "department": "finance"}
    d = policy.authorize(finance, export_payroll, "call_tool")
    info(f"fiona（财务部员工）→ export_payroll：{d.decision}，explain={policy.explain(d)}")

    step("2d：接进 Agent —— visible_tools 过滤 + before_tool 判定 + 审批暂停")
    for who, md in PEOPLE.items():
        info(f"{who:<30} 能看到的工具：{policy.visible_tools(RunState(metadata=md), [t.name for t in TOOLS])}")
    if offline:
        llm = ScriptedLLM([call_tool("reset_password", target_user_id="bob"), reply("已为 bob 发送重置链接。")])
        info("（离线：ScriptedLLM 剧本让模型调用 reset_password）")
    else:
        llm = real_llm()
    agent = Agent(llm, TOOLS, hooks=[policy], system_prompt="你是公司的 IT 助手。用户要求重置密码时直接调用 reset_password 工具。")
    res = await agent.run("请帮同事 bob 重置一下密码，他的账号被锁了。", metadata=ian)
    info(f"ian 的运行：status={res.status}  {short(res.output, 110)}")
    if res.status == "paused":
        res = await agent.approve(res.run_id, True, by="sec-oncall", comment="已电话核实 bob 本人")
        info(f"审批通过后：status={res.status}  🤖 {short(res.output, 80)}")
    info("审计日志（每条判定都带命中的策略 id；最近 4 条）：")
    for r in audit[-4:]:
        info(f"  {r['principal']:<13} {r['action']:<21} {r['resource']:<24} allowed={r['allowed']!s:<5} policy_ids={r['policy_ids']}")

    step("2d'：审批函数也可以是 async 的（例如去审批系统查一条记录）—— 它的返回值会被 await")

    async def approval_service(call, state) -> bool:
        await asyncio.sleep(0.05)  # 查询审批系统（这里用 sleep 代替一次网络往返）：等待期间让出事件循环
        return False  # 审批系统里没有这张单子的批准记录

    probe = approval_service(None, None)
    info(f"bool(approval_service(...)) = {bool(probe)} —— 协程对象恒为真：如果不 await，'不批准'会被当成'批准'")
    probe.close()  # 这个协程只是拿来演示 bool()，关掉它，避免 "never awaited" 警告
    guarded = make_cedar_policy(approver=approval_service)
    scripted = ScriptedLLM([call_tool("reset_password", target_user_id="bob"), reply("抱歉，这次重置没有获得批准。")])
    res = await Agent(scripted, TOOLS, hooks=[guarded]).run("请帮同事 bob 重置一下密码。", metadata=ian)
    tool_msg = next(m["content"] for m in res.messages if m["role"] == "tool")
    info(f"（剧本模型调用 reset_password）status={res.status}；工具结果：{short(tool_msg, 60)}")
    info("async 审批函数被 await，拿到 False → 调用被拒绝，reset_password 没有执行（等人批的场景仍用 2d 的 PauseRun）")

    step("2e：一个反直觉的坑 —— Cedar 求值出错的策略会被【跳过】")
    ents = [e for e in build_entities({**ian, "tenant_plan": "free"}, {"reset_password": {"risk": "dangerous"}}) if e["uid"]["type"] != "Tenant"]
    req = {"principal": {"type": "User", "id": "ian"}, "action": {"type": "Action", "id": "call_tool"},
           "resource": {"type": "Tool", "id": "reset_password"}, "context": {}}
    raw = cedarpy.is_authorized(req, policy.policies_text, ents, policy.schema_text)
    info("免费版租户禁止危险操作（free-plan-no-dangerous 要读 principal.tenant.plan）。如果实体里漏了 Tenant：")
    info(f"裸调 cedarpy：decision={raw.decision.name}  errors={[e.splitlines()[0][:80] for e in raw.diagnostics.errors]}")
    leaky = CedarPolicy(CONFIGS / "policies.cedar", CONFIGS / "schema.cedarschema", tools=TOOLS,
                        entities_fn=lambda md, cat: [e for e in build_entities(md, cat) if e["uid"]["type"] != "Tenant"])
    d = leaky.authorize({**ian, "tenant_plan": "free"}, reset_password, "call_tool")
    info(f"CedarPolicy：allowed={d.allowed}（{short(policy.describe(d), 80)}）")
    takeaway("Cedar 官方语义是 skip on error：出错的 forbid 不生效，结果可能变成 Allow。schema 校验挡不住'实体没传全'，所以适配器必须把错误当拒绝。")

    step("2f：判定本身是纯 CPU 计算：量一下它有多快（为什么 authorize / visible_tools 是普通方法）")
    n = 2000
    t0 = time.perf_counter()
    for i in range(n):
        policy.authorize(alice if i % 2 else ian, reset_password, "call_tool", ctx)
    per = (time.perf_counter() - t0) / n * 1000
    info(f"{n} 次判定（每次都重新构造实体、带 schema），平均 {per:.3f} ms/次 —— 纯 CPU、无 I/O，比一次模型调用快四个数量级")
    takeaway("Cedar 判定是亚毫秒级的纯计算：authorize / visible_tools 保持普通方法，在事件循环里直接算，比丢进线程池还快；"
             "before_tool 是 async 的，只因为审批函数可能要 await（2d'）。真正要异步的是'去哪儿取实体'"
             "（查用户目录、查数据库），那一步在请求入口 await 完，结果放进 metadata。")


# =====================================================================
# 场景 3：护栏级联
# =====================================================================


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ReplayClassifier:
    """回放一次真实运行里 LLM 分类器的判定（按用例 id 取），并报告当时实测的延迟。"""

    def __init__(self, recorded: dict[str, dict], cases: list[dict], name: str = "llm"):
        self.by_text = {c["text"]: recorded[c["id"]] for c in cases if c["id"] in recorded}
        self.name = name
        self.calls = 0
        self.replayed_ms: list[float] = []

    def classify(self, text: str):
        from agentkit.contrib.guards import Verdict

        r = self.by_text[text]
        self.calls += 1
        self.replayed_ms.append(r["latency_ms"])
        return Verdict(r["label"], r["score"], r["reason"], self.name, latency_ms=r["latency_ms"])


def print_table(rows: list[dict]) -> None:
    head = f"   {'方式':<22}{'精确率':>7}{'召回率':>7}{'F1':>7}{'误报':>5}{'漏报':>5}{'LLM调用':>8}{'tokens':>8}{'p50ms':>9}{'p90ms':>9}"
    print(head)
    print("   " + "-" * (len(head) + 6))
    for r in rows:
        print(f"   {r['name']:<24}{r['precision']:>8.2f}{r['recall']:>8.2f}{r['f1']:>7.2f}{r['fp']:>6}{r['fn']:>6}"
              f"{r['llm_calls']:>9}{r['tokens']:>9}{r['p50_ms']:>10.1f}{r['p90_ms']:>9.1f}")


def cascade_thresholds() -> list[tuple[float, float]]:
    # 第一级（正则）：score ≤ 0.1（没命中、也没可疑词）→ 直接放行；≥ 0.95 → 直接拦（正则的 0.7 不会触发）；其余交给 LLM
    # 第二级（LLM）：0.5 一刀切，保证出结论
    return [(0.1, 0.95), (0.5, 0.5)]


async def scenario_guards(offline: bool, record: bool) -> None:
    banner("场景 3：护栏级联 —— 便宜的先筛，拿不准再问贵的")
    from agentkit.contrib.guards import CascadeClassifier, EvalReport, LLMClassifier, RegexClassifier, evaluate

    cases = load_jsonl(DATA / "guard_eval.jsonl")
    n_attack = sum(c["label"] == "attack" for c in cases)
    step(f"评估集 data/guard_eval.jsonl：{len(cases)} 条（攻击 {n_attack}、正常 {len(cases) - n_attack}）")
    info("故意偏向难例：13 条正常请求里有 7 条是正则的已知误报（含第 09 课的'请忽略我之前的要求，改成周五送货'），")
    info("11 条攻击里有 6 条正则抓不到（换说法、藏在文档里、编码、冒充管理员、针对检测器本身）。")

    rows: list[dict] = []
    reports: dict[str, EvalReport] = {}
    regex_report = await evaluate(RegexClassifier(), cases, name="正则 detect_injection")
    reports["regex"] = regex_report

    if offline:
        if not RECORDING.exists():
            info(f"⏭  找不到录制文件 {RECORDING.name}，跳过 LLM 与级联两行（用 --record 真实运行一次即可生成）。")
            print_table([regex_report.row()])
            return
        rec = json.loads(RECORDING.read_text(encoding="utf-8"))
        meta = rec["meta"]
        info(f"（离线：回放 {meta['recorded_at']} 用 {meta['model']} 的一次真实运行；延迟与 token 是当时实测值）")
        llm_rep = await evaluate(ReplayClassifier(rec["llm"], cases), cases, name="LLM 分类器")
        llm_rep.latency_ms = [rec["llm"][c["id"]]["latency_ms"] for c in cases]
        llm_rep.llm_calls, llm_rep.tokens = meta["llm_calls"], meta["llm_tokens"]
        replay = ReplayClassifier(rec["cascade"], cases)
        casc = CascadeClassifier([RegexClassifier(), replay], cascade_thresholds())
        casc_rep = await evaluate(casc, cases, name="级联 正则→LLM")
        # 级联的延迟 = 正则（微秒级）+ 升级到 LLM 的那部分当时实测的延迟
        casc_rep.latency_ms = [v["latency_ms"] + (rec["cascade"][c["id"]]["latency_ms"] if v["stages"] == 2 else 0.0)
                               for c, v in zip(cases, casc_rep.verdicts)]
        casc_rep.llm_calls, casc_rep.tokens = meta["cascade_llm_calls"], meta["cascade_tokens"]
    else:
        info("（真实模式：LLM 分类器经 LiteLLMRouterLLM 调用网关，最多 2 个并发，约需 1 分钟）")
        llm_rep = await evaluate(LLMClassifier(real_llm(), name="llm"), cases, name="LLM 分类器", concurrency=2)
        casc = CascadeClassifier([RegexClassifier(), LLMClassifier(real_llm(), name="llm")], cascade_thresholds())
        casc_rep = await evaluate(casc, cases, name="级联 正则→LLM", concurrency=2)
        if record:
            save_recording(cases, llm_rep, casc_rep)
    reports["llm"], reports["cascade"] = llm_rep, casc_rep
    rows = [regex_report.row(), llm_rep.row(), casc_rep.row()]
    step("三种方式的对比（攻击为正类；延迟是单条判定的 p50 / p90）")
    print_table(rows)
    for key, rep in reports.items():
        if rep.mistakes:
            info(f"{rep.name} 判错的 {len(rep.mistakes)} 条：")
            for m in rep.mistakes:
                info(f"   [{m['id']}] 真实={m['truth']:<6} 判为={m['pred']:<9} {short(m['text'], 40)}")
    escalated = sum(v["stages"] == 2 for v in casc_rep.verdicts)
    takeaway(f"级联让 {len(cases) - escalated}/{len(cases)} 条在正则一级就放行，其余 {escalated} 条才花一次模型调用；"
             "在这个偏向难例的集合上省得不多，看下面日常流量的比例。")

    step("日常流量里，正则一级能直接放行多少？（data/ordinary_traffic.jsonl，20 条模拟的日常 IT 请求，零成本）")
    ordinary = load_jsonl(DATA / "ordinary_traffic.jsonl")
    first = RegexClassifier()
    settled = [c for c in ordinary if first.classify(c["text"]).score <= cascade_thresholds()[0][0]]
    info(f"{len(settled)}/{len(ordinary)} 条直接放行，只有 {len(ordinary) - len(settled)} 条需要升级到 LLM："
         f"{[short(c['text'], 16) for c in ordinary if c not in settled]}")

    if not offline or RECORDING.exists():
        step("阈值扫描（回放 LLM 分数，零成本）：第一级的 (low, high) 怎么选？")
        rec = json.loads(RECORDING.read_text(encoding="utf-8")) if RECORDING.exists() else None
        if rec is not None:
            for label, th in [("正则命中→交给 LLM（默认）", (0.1, 0.95)), ("正则命中→直接拦截", (0.1, 0.6)),
                              ("只有正则命中才问 LLM", (0.45, 0.95))]:
                casc = CascadeClassifier([RegexClassifier(), ReplayClassifier(rec["llm"], cases)], [th, (0.5, 0.5)])
                r = (await evaluate(casc, cases, name=label)).row()
                info(f"{label:<22} (low={th[0]}, high={th[1]})：精确率 {r['precision']:.2f}  召回率 {r['recall']:.2f}  "
                     f"LLM 调用 {casc.calls['llm']}/{len(cases)}")
            takeaway("让正则'一票否决'会把它的误报原样继承下来；只在正则命中时才问 LLM，会把正则的漏报原样继承下来。")


def save_recording(cases: list[dict], llm_rep, casc_rep) -> None:
    """录制真实运行里 LLM 分类器的每条判定，供 --offline 回放。第一级是确定性的正则，回放时现算，不录制。"""

    def entry(v: dict, strip_prefix: bool) -> dict:
        reason = v["reason"].split("] ", 1)[-1] if strip_prefix else v["reason"]
        # 级联里的 label 是按阈值重新判过的；回放需要的是 LLM 自己的分数，label 按 0.5 还原
        return {"label": "attack" if v["score"] >= 0.5 else "benign", "score": v["score"], "reason": reason,
                "latency_ms": v["latency_ms"]}

    llm_part = {c["id"]: entry(v, False) for c, v in zip(cases, llm_rep.verdicts)}
    casc_part = {c["id"]: entry(v, True) for c, v in zip(cases, casc_rep.verdicts) if v["stages"] == 2}
    RECORDING.write_text(json.dumps({
        "meta": {"recorded_at": time.strftime("%Y-%m-%d"), "model": env("LLM_MODEL", "gpt-5.5"),
                 "llm_calls": llm_rep.llm_calls, "llm_tokens": llm_rep.tokens,
                 "cascade_llm_calls": casc_rep.llm_calls, "cascade_tokens": casc_rep.tokens},
        "llm": llm_part, "cascade": casc_part,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    info(f"📼 已录制到 {RECORDING.relative_to(HERE)}")


async def scenario_guard_latency(offline: bool) -> None:
    banner("场景 4：输入护栏的延迟代价 —— 先判定再调主模型 vs 与主模型并行（ClassifierGuard）")
    import contextlib

    from agentkit import RunFinished, TextDelta
    from agentkit.contrib.guards import ClassifierGuard, LLMClassifier

    if offline:
        verdict = '{"is_attack": false, "confidence": 0.9, "reason": "正常的 IT 求助"}'
        judge = ScriptedLLM(responder=lambda m: LLMResponse(content=verdict), latency=0.8)
        main = ScriptedLLM(responder=lambda m: LLMResponse(content="（离线）证书过期时，先同步系统时间，再重新导入 VPN 证书。"), latency=1.2)
        info("（离线：ScriptedLLM 扮演分类器 0.8s、主模型 1.2s）")
    else:
        judge, main = real_llm(), real_llm()
    text = "VPN 连不上，提示证书过期，怎么办？请用两句话回答。"
    for mode in ("serial", "parallel"):
        guard = ClassifierGuard(LLMClassifier(judge), on="input", mode=mode)
        agent = Agent(main, [], hooks=[guard])
        t0 = time.perf_counter()
        first = None
        result = None
        async with contextlib.aclosing(agent.stream(text, metadata={"tenant_id": "acme", "user_id": "alice"})) as events:
            async for ev in events:
                if isinstance(ev, TextDelta) and first is None:
                    first = time.perf_counter() - t0
                elif isinstance(ev, RunFinished):
                    result = ev.result
        took = time.perf_counter() - t0
        v = (result.metadata.get("guard_verdicts") or [{}])[-1]
        info(f"mode={mode:<8} 首字 {first or 0:.2f}s，总耗时 {took:.2f}s（分类器自身 {v.get('latency_ms', 0) / 1000:.2f}s）→ {result.status}")
    takeaway("串行：首字要等分类器判完，但被拦的请求不花主模型的钱；并行：首字几乎不受影响，"
             "但被拦时主模型那次调用白花了，而且 Agent.stream() 会在判定出来之前就把文字推给用户（tests/contrib/test_guards.py 实测）。")


# =====================================================================


async def amain(args) -> None:
    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    if "1" in wanted:
        await scenario_gateway(args.offline, args.async_n)
    if "2" in wanted:
        await scenario_cedar(args.offline)
    if "3" in wanted:
        await scenario_guards(args.offline, args.record)
    if "4" in wanted:
        await scenario_guard_latency(args.offline)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="第 29 课 Demo：模型网关、策略即代码与护栏服务")
    parser.add_argument("--offline", action="store_true", help="离线：LiteLLM mock_response + 回放录制结果，不调用真实模型")
    parser.add_argument("--record", action="store_true", help="真实运行，并把护栏评估结果写进 data/guard_eval_recording.json")
    parser.add_argument("--async-n", type=int, default=8, help="场景 1e 的并发请求数（默认 8；主维护者验证时用过 20）")
    parser.add_argument("--only", default="1,2,3,4", help="只运行指定场景，如 --only 1,4")
    args = parser.parse_args()
    if args.offline and args.record:
        sys.exit("--record 需要真实模型，不能和 --offline 一起用")

    if args.offline:
        print("模式：离线 —— 网关部分用 LiteLLM 的 mock_response，护栏部分回放一次真实运行的录制结果；零成本、结果确定。")
    else:
        if not env("LLM_API_KEY"):
            print("❌ 没有找到 LLM_API_KEY。没有 API key 也没关系：加上 --offline 参数运行离线版本。")
            return
        print(f"模式：真实模型（主 {env('LLM_MODEL', 'gpt-5.5')}，备用 {env('LLM_FALLBACK_MODEL', '无')}），经 LiteLLM Router 调用")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
