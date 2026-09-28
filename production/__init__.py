"""production —— 第 31 课的参考服务：把第 26–30 课的组件装成一个能在多进程、多实例下正确运行的 Agent 服务。

    service/api.py      FastAPI：交互式 SSE、后台任务、审批、健康检查、指标
    service/worker.py   队列 worker：agentkit.distributed 的 run_worker + AgentJobHandler，外加 trace、停机归还、进度事件、探针
    run_local.py        本机无 Docker 一键启动（嵌入式 Postgres + fakeredis + API + N 个 worker）
    loadtest.py         压测与故障注入（kill -9、SIGTERM 滚动重启）并逐项验证
    deploy/             Dockerfile、docker-compose、Kubernetes 清单

讲义见 lessons/31_deployment_and_scaling/README.md。
"""
