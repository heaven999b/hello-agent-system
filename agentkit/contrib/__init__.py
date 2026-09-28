"""agentkit.contrib —— 把 agentkit 的教学实现替换为成熟的生产组件（第四部分，第 26–29 课）。

agentkit 核心刻意保持"零依赖、看得懂"（单机多进程用 agentkit.distributed 的 SQLite 后端）；这里的适配器接口与核心一致，
但实现换成业界成熟组件，让同一个 Agent 可以多机、多 worker、高并发地运行：

    postgres     Postgres 检查点（版本号 CAS + fencing）、SKIP LOCKED 任务队列、worker   （第 26 课）
    redis_store  Redis 幂等存储、Lua 令牌桶限流、带 fencing token 的锁                （第 26 课）
    temporal     用 Temporal 持久化工作流运行 Agent：Activity 重试、Signal 审批       （第 27 课）
    otel         OpenTelemetry 追踪桥接（GenAI 语义约定）、Prometheus 指标            （第 28 课）
    gateway      LiteLLM 模型网关（路由、降级、预算）                                  （第 29 课）
    policy       Cedar 策略即代码，替换硬编码的 RBAC                                  （第 29 课）
    guards       可插拔的护栏分类器（注入检测、PII）                                  （第 29 课）

和核心一样，每个概念只有一个类、一套 async 实现，没有"同步版 / 异步版"两套：要等网络的方法
（Redis、数据库、模型网关、Temporal）都是 async def，Agent 直接 await，等待时让出事件循环；
纯计算（Cedar 判定、指标计数、正则分类）仍是普通方法 —— 调用方统一用 maybe_await，两种写法一视同仁。
组装成完整服务的参考实现见 production/（第 31 课）。

每个模块只在被导入时才需要对应的可选依赖，例如：pip install -e ".[postgres]"。
"""


def require(module: str, extra: str):
    """导入可选依赖；缺失时给出明确的安装提示，而不是一个莫名其妙的 ImportError。"""
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as e:  # pragma: no cover - 取决于环境
        raise ImportError(f"需要可选依赖 {module!r}：请运行 pip install -e \".[{extra}]\"") from e
