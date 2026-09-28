"""agentkit.contrib.gateway：LiteLLM Router 适配器（第 29 课）。

全部离线：用 LiteLLM 自带的 mock_response（写在部署的 litellm_params 里或调用参数里）模拟上游，
不会发出任何网络请求。多个进程共享冷却状态的测试用根 conftest 的 fakeredis（redis_url），另一个 Router 跑在真的子进程里。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # import litellm 时不联网拉价格表（子进程继承这个环境变量）
litellm = pytest.importorskip("litellm")

from agentkit import Agent, StreamDone, TextDelta, tool, wait_for  # noqa: E402
from agentkit.contrib.gateway import LiteLLMRouterLLM, to_llm_error  # noqa: E402
from agentkit.llm import LLMError  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")  # litellm 序列化 mock ModelResponse 时的 pydantic 警告

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "lessons" / "29_gateway_and_guardrails" / "configs" / "litellm-config.yaml"
MSG = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
async def _drain_litellm_logging():
    """LiteLLM 把成功 / 失败回调放进一个全局的后台日志队列，队列绑定在当时的事件循环上。
    pytest-asyncio 每个测试一个新的事件循环：测试结束前停掉后台任务（它会先跑完队列里剩下的回调），
    否则下一个测试换了循环，没跑的回调协程被直接丢弃，报 "coroutine ... was never awaited"。
    （服务进程里只有一个事件循环，不会遇到这个问题。）"""
    yield
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    for _ in range(3):
        await asyncio.sleep(0)  # 先让 LiteLLM 在调用结束时创建的后台任务跑起来：是它们把回调放进队列的
    await GLOBAL_LOGGING_WORKER.stop()


def dep(name: str, mock, **extra) -> dict:
    return {"model_name": name, "litellm_params": {"model": f"openai/{name}", "api_key": "sk-test-placeholder", "mock_response": mock, **extra}}


TOOL_CALL_RESPONSE = {
    "id": "x", "model": "gpt-mock",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}}]}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
              "prompt_tokens_details": {"cached_tokens": 64}, "completion_tokens_details": {"reasoning_tokens": 12}},
}


# ------------------------------------------------------------------ 映射、降级、错误


async def test_maps_tool_calls_and_detailed_usage():
    llm = LiteLLMRouterLLM([dep("m", TOOL_CALL_RESPONSE)], num_retries=0)
    r = await llm.chat(MSG, tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}}}])
    assert r.finish_reason == "tool_calls" and r.model == "gpt-mock"
    assert [(c.id, c.name, c.parsed_args()) for c in r.tool_calls] == [("call_1", "get_weather", {"city": "北京"})]
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cached_input_tokens, r.usage.reasoning_tokens) == (100, 20, 64, 12)


@pytest.mark.parametrize("error", ["litellm.RateLimitError", "litellm.InternalServerError"])
async def test_falls_back_when_primary_fails(error):
    llm = LiteLLMRouterLLM([dep("gpt-5.5", error), dep("gpt-5.6-luna", "backup says hi")],
                           fallbacks=[{"gpt-5.5": ["gpt-5.6-luna"]}], num_retries=0)
    r = await llm.chat(MSG)
    assert r.content == "backup says hi" and r.model == "gpt-5.6-luna"
    assert llm.last_route["model_group"] == "gpt-5.6-luna" and llm.last_route["attempted_fallbacks"] == 1
    assert llm.events == ["fallback gpt-5.5 -> gpt-5.6-luna"]


async def test_agent_is_unchanged_when_llm_is_swapped_for_the_gateway():
    @tool
    def get_weather(city: str) -> str:
        """查天气"""
        return f"{city} 晴"

    llm = LiteLLMRouterLLM([dep("m", "unused")], num_retries=0)
    responses = iter([TOOL_CALL_RESPONSE, "北京晴"])  # 调用时传入的 mock_response 覆盖部署里的
    original = llm.chat
    llm.chat = lambda messages, tools=None, **kw: original(messages, tools, mock_response=next(responses), **kw)
    res = await Agent(llm, [get_weather]).run("北京天气")
    assert res.ok and res.output == "北京晴" and res.tools_called() == ["get_weather"]


async def test_retries_happen_before_fallback_and_cost_wall_clock_time():
    """num_retries=1：主模型先被请求 2 次（带退避），然后才降级 —— 用 LiteLLM 回调数真实的上游请求。"""
    from litellm.integrations.custom_logger import CustomLogger

    attempts = []

    class Counter(CustomLogger):
        def log_pre_api_call(self, model, messages, kwargs):
            attempts.append(model)

    counter = Counter()
    litellm.callbacks.append(counter)
    try:
        llm = LiteLLMRouterLLM([dep("gpt-5.5", "litellm.InternalServerError"), dep("gpt-5.6-luna", "ok")],
                               fallbacks=[{"gpt-5.5": ["gpt-5.6-luna"]}], num_retries=1)
        t0 = time.perf_counter()
        assert (await llm.chat(MSG)).content == "ok"
        took = time.perf_counter() - t0
    finally:
        litellm.callbacks.remove(counter)
    assert attempts == ["gpt-5.5", "gpt-5.5", "gpt-5.6-luna"]
    assert took > 0.3  # 重试前有退避等待（asyncio.sleep 不会提前返回，这是下界）
    assert llm.last_route["attempted_retries"] == 0  # 反直觉：这个头只统计最终成功的模型组，看不到主模型组的重试


async def test_no_fallback_raises_llmerror_with_retryability():
    with pytest.raises(LLMError) as e:
        await LiteLLMRouterLLM([dep("m", "litellm.RateLimitError")], num_retries=0).chat(MSG)
    assert e.value.status_code == 429 and e.value.retryable
    with pytest.raises(LLMError) as e:
        await LiteLLMRouterLLM([dep("m", "litellm.ContextWindowExceededError")], num_retries=0).chat(MSG)
    assert not e.value.retryable
    with pytest.raises(LLMError) as e:  # 请求了不存在的模型组：配置错误，不可重试
        await LiteLLMRouterLLM([dep("m", "ok")], model="no-such-group", num_retries=0).chat(MSG)
    assert e.value.status_code == 400 and not e.value.retryable


def test_error_mapping_table():
    def mk(cls, **kw):
        return cls(message="x", llm_provider="openai", model="m", **kw)

    assert to_llm_error(mk(litellm.NotFoundError)).retryable is False
    assert to_llm_error(mk(litellm.AuthenticationError)).retryable is False
    assert to_llm_error(mk(litellm.BadRequestError)).retryable is False
    assert to_llm_error(mk(litellm.InternalServerError)).retryable is True
    assert to_llm_error(mk(litellm.ServiceUnavailableError)).retryable is True
    assert to_llm_error(mk(litellm.Timeout)).retryable is True
    assert to_llm_error(litellm.APIConnectionError(message="refused", llm_provider="openai", model="m")).retryable is True
    quota = to_llm_error(litellm.RateLimitError(message="insufficient_quota", llm_provider="openai", model="m"))
    assert quota.status_code == 429 and quota.retryable is False  # 额度用完：重试没用
    from litellm.types.router import RouterRateLimitError

    cooled = to_llm_error(RouterRateLimitError(model="g", cooldown_time=30, enable_pre_call_checks=False, cooldown_list=[]))
    assert cooled.retryable and cooled.retry_after == 30.0 and cooled.status_code == 429


def test_from_env_keeps_key_out_of_repr_and_skips_self_fallback(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-should-never-print")
    monkeypatch.setenv("LLM_MODEL", "gpt-5.5")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "gpt-5.5")
    llm = LiteLLMRouterLLM.from_env()
    assert "sk-test-secret" not in repr(llm) and llm.fallbacks == []
    llm = LiteLLMRouterLLM.from_env(primary="gpt-x", fallback="gpt-y")
    assert llm.model == "gpt-x" and llm.fallbacks == [{"gpt-x": ["gpt-y"]}]
    params = [d["litellm_params"] for d in llm.router.model_list]
    assert [p["model"] for p in params] == ["openai/gpt-x", "openai/gpt-y"]


# ------------------------------------------------------------------ 多进程：通过 Redis 共享冷却状态


def cooldown_model_list() -> list[dict]:
    return [
        {**dep("g", "litellm.InternalServerError"), "model_info": {"id": "dep-a"}},
        {**dep("g", "from dep-b"), "model_info": {"id": "dep-b"}},
    ]


# 实例 B：另一个操作系统进程里的另一个 Router，只和实例 A 共享同一个 Redis
INSTANCE_B = textwrap.dedent(
    """
    import asyncio, json, os, sys, time
    from agentkit.contrib.gateway import LiteLLMRouterLLM

    host, port, model_list = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])


    async def main():
        b = LiteLLMRouterLLM(model_list, num_retries=0, redis_host=host, redis_port=port, allowed_fails=0, cooldown_time=30)
        cooldowns = b.router.cooldown_cache.get_active_cooldowns
        deadline = time.time() + 10
        while time.time() < deadline and not cooldowns(model_ids=["dep-a"], parent_otel_span=None):
            await asyncio.sleep(0.05)
        cooled = [c[0] for c in cooldowns(model_ids=["dep-a", "dep-b"], parent_otel_span=None)]
        answers = [(await b.chat([{"role": "user", "content": "hi"}])).content for _ in range(8)]
        print(json.dumps({"pid": os.getpid(), "cooled": cooled, "answers": answers}))


    asyncio.run(main())
    """
)


async def test_router_instances_in_two_processes_share_cooldown_through_redis(redis_url):
    """实例 A 发现部署 dep-a 坏了并冷却它；实例 B（另一个进程里的另一个 Router）通过 Redis 看到同一个冷却，
    从第一个请求起就不再打 dep-a。不配 Redis 时，每个实例都要自己踩一遍坑。"""
    host, port = redis_url.split("//")[1].split("/")[0].split(":")
    a = LiteLLMRouterLLM(cooldown_model_list(), num_retries=0, redis_host=host, redis_port=int(port), allowed_fails=0, cooldown_time=30)
    hit_dep_a = False
    for _ in range(50):  # 负载均衡是随机的：一直打到 A 撞上 dep-a 为止（allowed_fails=0：撞上一次就被冷却）
        try:
            await a.chat(MSG)
        except LLMError:
            hit_dep_a = True
            break
    assert hit_dep_a
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", INSTANCE_B, host, port, json.dumps(cooldown_model_list()),
        cwd=REPO, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await wait_for(proc.communicate(), 120)  # 子进程要导入 litellm：机器忙时要好几秒
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode == 0, err.decode()[-3000:]
    b = json.loads(out.decode().strip().splitlines()[-1])
    assert b["pid"] != os.getpid()
    assert b["cooled"] == ["dep-a"]
    assert b["answers"] == ["from dep-b"] * 8


# ------------------------------------------------------------------ 部署配置文件


def test_proxy_config_is_valid_yaml_has_no_plaintext_keys_and_loads_into_router(monkeypatch):
    yaml = pytest.importorskip("yaml")
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert {"model_list", "router_settings", "litellm_settings", "general_settings"} <= set(cfg)
    secrets = [d["litellm_params"].get("api_key") for d in cfg["model_list"]]
    secrets += [cfg["general_settings"]["master_key"], cfg["general_settings"]["database_url"], cfg["router_settings"]["redis_password"]]
    assert all(str(s).startswith("os.environ/") for s in secrets)
    for name in ("CLIPROXY_API_BASE", "CLIPROXY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9" if name.endswith("BASE") else "sk-placeholder")
    rs = cfg["router_settings"]
    router = litellm.Router(
        model_list=cfg["model_list"],
        **{k: rs[k] for k in ("routing_strategy", "num_retries", "allowed_fails", "cooldown_time", "fallbacks", "context_window_fallbacks")},
    )
    assert sorted(router.model_names) == ["gpt-5.5", "gpt-5.6-luna"]
    assert len([d for d in router.model_list if d["model_name"] == "gpt-5.5"]) == 2  # 同名两个部署 = 组内负载均衡
    assert router.fallbacks == [{"gpt-5.5": ["gpt-5.6-luna"]}]


# ------------------------------------------------------------------ 并发、流式


async def test_chat_runs_concurrently():
    llm = LiteLLMRouterLLM([dep("m", "pong", mock_delay=0.2)], num_retries=0)
    real, probe = llm.router.acompletion, {"in_flight": 0, "peak": 0}

    async def counting(**params):  # 数一数同时在途的上游请求：并发的确定性证据
        probe["in_flight"] += 1
        probe["peak"] = max(probe["peak"], probe["in_flight"])
        try:
            return await real(**params)
        finally:
            probe["in_flight"] -= 1

    llm.router.acompletion = counting
    t0 = time.perf_counter()
    results = await asyncio.gather(*(llm.chat(MSG) for _ in range(10)))
    took = time.perf_counter() - t0
    assert [r.content for r in results] == ["pong"] * 10
    assert probe["peak"] == 10  # 10 个请求同时在途（mock_delay 用 asyncio.sleep 等待，不占线程）
    assert took < 1.8, took  # 宽松兜底：串行需要 ≥ 2 秒


async def test_stream_yields_text_deltas_then_done():
    llm = LiteLLMRouterLLM([dep("m", "模型网关是所有模型调用的统一出口。")], num_retries=0)
    events = [ev async for ev in llm.stream(MSG)]
    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert len(deltas) > 1 and isinstance(events[-1], StreamDone)
    assert "".join(deltas) == events[-1].response.content == "模型网关是所有模型调用的统一出口。"
    assert llm.last_route["ttft_s"] is not None and llm.last_route["chunks"] >= len(deltas)


async def test_stream_falls_back_before_first_token():
    llm = LiteLLMRouterLLM([dep("gpt-5.5", "litellm.RateLimitError"), dep("gpt-5.6-luna", "来自备用模型")],
                           fallbacks=[{"gpt-5.5": ["gpt-5.6-luna"]}], num_retries=0)
    events = [ev async for ev in llm.stream(MSG)]
    assert events[-1].response.content == "来自备用模型"


def tc(index, id_=None, name=None, args=""):
    return SimpleNamespace(index=index, id=id_, function=SimpleNamespace(name=name, arguments=args))


def chunk(content=None, tool_calls=None, finish=None, usage=None):
    return SimpleNamespace(model="gpt-5.5", usage=usage,
                           choices=[SimpleNamespace(finish_reason=finish, delta=SimpleNamespace(content=content, tool_calls=tool_calls))])


class FakeStream:
    """按给定分片逐个产出的异步迭代器，形状与 LiteLLM 的流式响应一致。"""

    def __init__(self, chunks):
        self._it = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


async def test_stream_assembles_interleaved_parallel_tool_call_chunks():
    """用和真实网关实测一致的分片形状（先 {id,name,""}，再若干段 arguments；两个调用交错到达）驱动 stream()。"""
    usage = SimpleNamespace(prompt_tokens=351, completion_tokens=48, prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=0))
    chunks = [
        chunk(tool_calls=[tc(0, "call_a", "get_weather", "")]),
        chunk(tool_calls=[tc(1, "call_b", "get_weather", "")]),
        chunk(tool_calls=[tc(0, args='{"city":')]),
        chunk(tool_calls=[tc(1, args='{"city":"上海"}')]),
        chunk(tool_calls=[tc(0, args='"北京"}')]),
        chunk(finish="tool_calls"),
        SimpleNamespace(model="gpt-5.5", usage=usage, choices=[]),
    ]

    llm = LiteLLMRouterLLM([dep("gpt-5.5", "unused")], num_retries=0)

    async def fake_acompletion(**params):
        assert params["stream"] is True and params["stream_options"] == {"include_usage": True}
        return FakeStream(chunks)

    llm.router.acompletion = fake_acompletion
    done = [ev async for ev in llm.stream(MSG)][-1].response
    assert [(c.id, c.parsed_args()) for c in done.tool_calls] == [("call_a", {"city": "北京"}), ("call_b", {"city": "上海"})]
    assert done.finish_reason == "tool_calls" and done.usage.input_tokens == 351 and done.content is None


async def test_agent_streams_through_the_gateway():
    @tool
    async def get_weather(city: str) -> str:
        """查天气"""
        return f"{city} 晴"

    # 第一轮：工具调用的流式分片（LiteLLM 的 mock 不支持流式工具调用，这里直接喂分片）；第二轮：真正走 Router 的 mock 流式文本
    llm = LiteLLMRouterLLM([dep("m", "北京今天晴。")], num_retries=0)
    real_acompletion = llm.router.acompletion
    turns = iter([[chunk(tool_calls=[tc(0, "call_1", "get_weather", "")]), chunk(tool_calls=[tc(0, args='{"city": "北京"}')]),
                   chunk(finish="tool_calls")], None])

    async def acompletion(**params):
        scripted = next(turns)
        return FakeStream(scripted) if scripted is not None else await real_acompletion(**params)

    llm.router.acompletion = acompletion
    events = []
    async with contextlib.aclosing(Agent(llm, [get_weather]).stream("北京天气", metadata={"tenant_id": "t", "user_id": "u"})) as s:
        async for ev in s:
            events.append(ev)
    final = events[-1].result
    assert final.ok and final.tools_called() == ["get_weather"]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "北京今天晴。"
