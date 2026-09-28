"""第 14 课练习：成本与延迟优化。

一共三题：
  (a) cache_key        缓存键：规范化 + 租户隔离（写错一次 = 一次跨租户数据泄露）
  (b) CascadeLLM.chat  级联的升级逻辑 + 记账（先小模型，不合格再升级到大模型）—— 写成 async def，await 两个模型
  (c) cost_by_tenant   从 trace 里按租户归因成本（成本治理的第一步：钱花在谁身上）

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=14
    # 或者：.venv/bin/python -m pytest lessons/14_cost_latency -v

同目录的 costkit.py 里有功能更全的教学实现（Demo 用的就是它）。建议先自己写，写完再对照。
"""

from __future__ import annotations

import hashlib  # noqa: F401  （练习 a 会用到）
import json  # noqa: F401  （练习 a 会用到）
from typing import Callable

from agentkit.llm import LLM, LLMError  # noqa: F401  （练习 b 会用到 LLMError）
from agentkit.types import LLMResponse, Message, Usage

# =====================================================================
# 练习 (a)：cache_key —— 精确缓存的键
# =====================================================================


def cache_key(messages: list[Message], tools: list[dict] | None, model: str, tenant_id: str) -> str:
    """为一次模型请求计算缓存键。两个请求的键相同 ⇔ 可以互相复用对方的响应。

    要求：
      1. 返回 SHA-256 的十六进制字符串（64 个字符），用 hashlib 计算。
         不能用内置 hash()：它每个进程的随机种子都不一样（PYTHONHASHSEED），
         多个服务实例共享一个 Redis 缓存时，同样的请求在不同实例上算出不同的键，永远对不上。
      2. 键必须同时覆盖 tenant_id、model、tools、messages —— 任何一个不同，键就要不同：
         - 漏了 tenant_id：A 公司问过的问题，B 公司再问就会拿到 A 公司的答案（跨租户泄露）；
         - 漏了 model：换了模型，却还在返回旧模型的答案；
         - 漏了 tools：可用工具不同（比如按角色裁剪过），模型的决定也会不同。
      3. 规范化：dict 的键顺序不影响结果。{"role": "user", "content": "hi"} 和
         {"content": "hi", "role": "user"} 是同一条消息。
         提示：把四样东西放进**同一个** dict，用
               json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
         序列化后再哈希。
      4. 列表顺序有意义：消息顺序变了就是另一段对话，不要对列表排序。
      5. tools 为 None 和 [] 都表示"没有工具"，应得到相同的键。
      6. tenant_id 为 None 或空字符串 → 抛 ValueError（fail-closed：宁可不缓存，也不能用不分租户的缓存）。

    陷阱：不要用字符串直接拼接，比如 f"{tenant_id}{model}"。
         租户 "ab" + 模型 "c" 和 租户 "a" + 模型 "bc" 会拼出同一个 "abc" —— 这就是"边界歧义"。
         把各字段放进一个 JSON 对象里，就天然没有这个问题。
    """
    raise NotImplementedError("TODO: 练习 (a) —— 实现缓存键")


# =====================================================================
# 练习 (b)：CascadeLLM.chat —— 级联的升级逻辑
# =====================================================================

Validator = Callable[[list[Message], LLMResponse], bool]


class CascadeLLM:
    """级联：先问便宜的小模型，validator 判"合格"就直接用；否则升级到大模型。

    它本身也实现了 LLM 协议（有 .model 和 async 的 .chat），所以可以直接交给 Agent 使用：
        await Agent(CascadeLLM(small, large, validator), tools).run("...")

    validator(messages, response) -> bool：True 表示小模型的回答可以直接用。
    常见的校验：JSON 能否解析、字段是否齐全、工具名是否存在、有没有拒答话术、自报置信度是否够高……

    统计字段（__init__ 已经写好）：
        calls        chat 被调用的总次数
        escalations  升级到大模型的次数
        reasons      升级原因计数，例如 {"rejected": 2, "error": 1}
        wasted       升级时被丢弃的小模型回答的 token 用量（Usage）——这部分钱白花了，但照样要付
    """

    def __init__(self, small: LLM, large: LLM, validator: Validator):
        self.small = small
        self.large = large
        self.validator = validator
        self.model = f"cascade({small.model}→{large.model})"
        self.calls = 0
        self.escalations = 0
        self.reasons: dict[str, int] = {}
        self.wasted = Usage()
        # 不需要锁：同一个 CascadeLLM 会被很多并发的会话（asyncio 任务）同时调用，但协程只在 await 处让出控制权，
        # "self.calls += 1" 这种读-改-写中间没有 await，不会被别的协程插队（见 costkit.CascadeLLM 的说明）

    @property
    def escalation_rate(self) -> float:
        """升级率 = escalations / calls；一次都没调用过时为 0.0。"""
        return self.escalations / self.calls if self.calls else 0.0

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        """升级逻辑（这是一个 async 方法：两个模型的 chat 都是 async 的，调用时要 await），按顺序：

          1. calls += 1。
          2. await self.small.chat(messages, tools, **kwargs)：
             - 抛出 LLMError（限流、超时、模型不可用……）→ 升级，原因 "error"。
          3. 调用 self.validator(messages, 小模型的回答)：
             - 返回真值 → 直接返回小模型的回答（不升级）；
             - 返回假值 → 升级，原因 "rejected"；
             - validator 自己抛了任何异常 → 升级，原因 "validator_error"
               （校验器有 bug 不能让用户的请求失败，按"不合格"处理）。
             只要是"小模型回答了但被丢弃"（rejected / validator_error），就把它的 usage 累加到 self.wasted。
          4. 升级：escalations += 1，reasons[原因] += 1，然后返回 await self.large.chat(messages, tools, **kwargs)。
             大模型也抛异常 → 不要吞掉，直接抛出（交给外层的 ResilientLLM 或 Agent 处理）。

        提示：Usage 支持加法：self.wasted = self.wasted + draft.usage
             validator 是普通函数（纯计算），直接调用，不要 await。
             忘了 await 会怎样？self.small.chat(...) 只会返回一个"协程对象"而不是 LLMResponse，
             后面取 .usage 时报 AttributeError（还会有一条 "coroutine was never awaited" 警告）。
        """
        raise NotImplementedError("TODO: 练习 (b) —— 实现级联的升级逻辑")


# =====================================================================
# 练习 (c)：cost_by_tenant —— 按租户归因成本
# =====================================================================


def cost_by_tenant(spans: list[dict]) -> dict[str, float]:
    """从 trace 里算出每个租户花了多少钱。

    输入：agentkit.tracing.jsonl_exporter 导出的扁平 span 字典列表（每行一个），形如：
        {"name": "agent.run", "trace_id": "9f2c…", "span_id": "a1b2…", "parent_id": "e5f6…",
         "attrs": {"run_id": "7d1e…", "agent.status": "completed", "agent.cost_usd": 0.00042, ...}, ...}

    约定（和 demo.py 的写法一致，请严格按它实现）：
      1. 租户从哪来：服务层为每个请求开一个**根 span**（parent_id 为 None），把认证系统给出的
         租户写进它的 attrs["tenant.id"]，agent.run 于是成为这个根 span 的子孙：
             with tracer.span("request", **{"tenant.id": "acme", "app.feature": "faq"}):
                 await agent.run(...)
         同一个 trace 的所有 span 共享 trace_id —— 用 trace_id 找到根 span，就知道是哪个租户。
         （租户必须来自认证系统，绝不能从模型输出或用户输入里取。）
      2. 钱在哪：attrs 里带 "agent.cost_usd" 的 span（agent.run，以及审批后恢复时的 agent.resume）。
         其他 span（llm.chat、tool.xxx、request）不带这个字段，忽略即可。
      3. 不要重复计算：agent.cost_usd 是这次运行（run_id）的**累计**成本 —— 运行暂停后 resume，
         resume 的 span 上记的是"run 阶段 + resume 阶段"的总和。所以同一个 attrs["run_id"] 出现多次时，
         只取 agent.cost_usd 最大的那个 span，不要相加。
         （agentkit 的 agent.run / agent.resume span 一定带 run_id。）
      4. 归不了因的钱不能消失：根 span 没有 "tenant.id"（或该 trace 根本找不到根 span），
         就记到 "unknown" 名下 —— 让它在报表里显眼，逼着大家去补标签。

    返回 {租户: 总成本}，每个值 round(…, 6)。没有任何成本 span 时返回 {}。

    例：acme 有两次运行（0.001 和 0.002），globex 一次运行先 run（0.001）后 resume（累计 0.003），
        一个没打标签的运行 0.0005 → {"acme": 0.003, "globex": 0.003, "unknown": 0.0005}
    """
    raise NotImplementedError("TODO: 练习 (c) —— 实现按租户的成本归因")
