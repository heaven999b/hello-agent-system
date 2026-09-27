# syntax=docker/dockerfile:1
# 第 31 课：参考服务的镜像（API 和 worker 共用一个镜像，靠启动命令区分）。
#
# 构建（在仓库根目录，构建上下文是整个仓库）：
#   docker build -f production/deploy/Dockerfile -t itdesk:dev .
# 同目录下的 Dockerfile.dockerignore 只对这个 Dockerfile 生效：.env、.venv、runs/ 不会被发进构建上下文。
#
# ⚠️ 本机没有 Docker，这个文件没有实际构建过；语法由 production/tests/test_deploy_configs.py 做静态检查。
#
# 两阶段：
#   build   ：装编译依赖、建虚拟环境、pip install（产物只有 /opt/venv）
#   runtime ：干净的 slim 镜像 + 虚拟环境 + 源码，非 root 用户运行，没有编译器、没有 pip 缓存

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python -m venv /opt/venv
# 先只拷依赖声明和包源码：业务代码（production/）变了不会让依赖层的缓存失效
COPY pyproject.toml README.md ./
COPY agentkit ./agentkit
# 只装参考服务需要的可选依赖组（不装 temporal：参考服务走 Postgres 队列，Temporal 是第 27 课的另一条路线）
RUN /opt/venv/bin/pip install ".[postgres,redis,otel,policy,gateway,server]"

# 3.12 而不是 3.11：3.12 之前的 asyncio.wait_for 会在"结果和取消同时到达"时吞掉取消（CPython gh-86296），
# 本服务依赖的 redis-py、psycopg_pool 内部都在用它（本课实测，讲义 7.2）。代码兼容 3.10+，但生产镜像请用 3.12+。
FROM python:3.12-slim AS runtime
# 固定 UID/GID：K8s 的 securityContext.runAsUser 用同一个数字，文件权限才对得上
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=10001:10001 production ./production
# Cedar 策略与 schema 复用第 29 课的文件（单一来源）；生产中策略通常单独发布（见第 29 课"策略即代码"）
COPY lessons/29_gateway_and_guardrails/configs/policies.cedar lessons/29_gateway_and_guardrails/configs/schema.cedarschema ./policy/
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    CEDAR_POLICIES=/app/policy/policies.cedar \
    CEDAR_SCHEMA=/app/policy/schema.cedarschema
USER 10001:10001
EXPOSE 8000 8081 9100
# 默认启动 API；worker 用 command 覆盖：python -m production.service.worker；迁移：python -m production.service.migrate
# exec 形式（JSON 数组）：uvicorn 就是 1 号进程，能直接收到 SIGTERM（shell 形式会被 /bin/sh 挡住）
CMD ["uvicorn", "--factory", "production.service.api:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "20", "--timeout-keep-alive", "75", "--no-access-log"]
