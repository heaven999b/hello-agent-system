"""第 14 课场景 1b 的 worker 应用：每个 worker 进程这样加载它（WorkerPool 替你拼好这条命令）：

    python -m agentkit.distributed.worker --queue sqlite:///.../jobs.db \\
        --app lessons/14_cost_latency/cache_app.py:make_handler --opt cache=shared --opt latency=0.05

它运行在**独立的子进程**里。进程之间不共享任何内存，只通过同一个 SQLite 文件交流：任务队列，以及
cache=shared 时的响应缓存表（costkit.SQLiteResponseCache）。

任务的 payload：{"tenant": "acme", "question": "VPN 连不上怎么办？"}
返回值（写回任务表，Demo 从那里统计）：这次有没有命中缓存、调用了几次模型、耗时、成本、哪个进程处理的。

模型是剧本模型：ScriptedLLM(responder=..., latency=...)，每次真正的模型调用 asyncio.sleep(latency) 秒
（**延迟模型**，模拟"调一次模型要等一会儿"；命中缓存就不用等）。这里测的是缓存命中，不是模型。
"""

from __future__ import annotations

import os
import time

from agentkit import Agent, ScriptedLLM, reply
from agentkit.distributed import WorkerContext
from agentkit.testing import load_sibling
from agentkit.types import LLMResponse

costkit = load_sibling(__file__, "costkit")  # 课程目录不在 sys.path 上：按文件加载同目录的 costkit.py

SYSTEM = "你是企业 IT 服务台助手。只根据知识库回答，不超过 40 个字。"

ANSWERS = {
    "VPN 连不上怎么办？": "先确认没连访客 Wi-Fi，再用工号登录 GlobalConnect；报错 809 就重启客户端。",
    "怎么申请新显示器？": "在 IT 门户提交外设申请，主管审批后 3 个工作日内发放。",
    "忘记密码怎么办？": "打开 id.example.com 自助重置，需要手机验证码。",
    "打印机卡纸怎么处理？": "打开前盖取出卡纸，仍不行就在 IT 门户报修。",
    "怎么连公司 Wi-Fi？": "连接 ACME-Staff，用工号和域密码登录。",
    "邮箱满了怎么办？": "在 Outlook 里归档旧邮件，或申请扩容。",
    "怎么安装 Office？": "在软件中心搜索 Office，点击安装。",
    "会议室投影连不上？": "用会议室的 HDMI 转接头，或用 AirPlay 投屏。",
}


def faq_model(messages) -> LLMResponse:
    q = messages[-1]["content"]
    return reply(ANSWERS.get(q, "请在 IT 门户提交工单。"), input_tokens=380, output_tokens=40)


async def make_handler(wctx: WorkerContext):
    """worker 进程启动时调用一次。--opt cache=none|local|shared，latency=每次模型调用的模拟耗时（秒）。"""
    opts = wctx.options
    mode = opts.get("cache", "shared")
    base = ScriptedLLM(responder=faq_model, latency=float(opts.get("latency", 0.05)), model="demo-large")
    if mode == "local":
        cache = costkit.ResponseCache(ttl_s=600)  # 进程内：这个进程独有的一份
    elif mode == "shared":
        # 和任务队列共用一个 SQLiteDB（一个连接 + 一个专用线程）；writer 记下是谁写入的
        cache = costkit.SQLiteResponseCache(wctx.db, ttl_s=600, writer=f"{wctx.worker_id}/pid {os.getpid()}")
        await cache.setup()
    else:
        cache = None

    async def handler(job) -> dict:
        tenant, question = job.payload["tenant"], job.payload["question"]
        # 装饰器按请求创建（很轻），缓存存储是进程级的；作用域来自"认证系统"（这里是任务的租户）
        # 注意是 "is not None"：ResponseCache 定义了 __len__，空缓存的真值是 False
        llm = base if cache is None else costkit.CachingLLM(base, cache, scope={"tenant_id": tenant, "roles": ["employee"]})
        agent = Agent(llm, [], system_prompt=SYSTEM, name="it-helpdesk")
        t0 = time.perf_counter()
        res = await agent.run(question, metadata={"tenant_id": tenant, "user_id": "u1", "roles": ["employee"]})
        return {
            "hit": cache is not None and llm.hits > 0,
            "model_calls": 1 if cache is None else llm.misses,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "cost_usd": res.cost_usd,
            "worker": wctx.worker_id,
            "pid": os.getpid(),
            "status": res.status,
        }

    return handler
