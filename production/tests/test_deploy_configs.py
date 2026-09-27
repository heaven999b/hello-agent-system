"""部署配置的静态检查（本机没有 Docker / Kubernetes，所以至少要保证：语法对、交叉引用对、三个时间对齐、没有真实密钥）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
REPO = DEPLOY.parents[1]


def docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def k8s() -> dict[tuple[str, str], dict]:
    out = {}
    for f in (DEPLOY / "k8s").glob("*.yaml"):
        for d in docs(f):
            out[(d["kind"], d.get("metadata", {}).get("name", ""))] = d
    return out


def test_every_yaml_file_parses():
    files = [*DEPLOY.rglob("*.yaml"), *DEPLOY.rglob("*.yml")]
    assert len(files) >= 10
    for f in files:
        assert docs(f), f


def test_kustomization_lists_every_manifest():
    kust = docs(DEPLOY / "k8s" / "kustomization.yaml")[0]
    manifests = {f.name for f in (DEPLOY / "k8s").glob("*.yaml")} - {"kustomization.yaml"}
    assert set(kust["resources"]) == manifests


def container(deploy: dict) -> dict:
    return deploy["spec"]["template"]["spec"]["containers"][0]


def test_shutdown_timeline_is_aligned():
    """terminationGracePeriodSeconds ≥ preStop + 排空 + 收尾；心跳 ≤ 租约 / 3。"""
    objs = k8s()
    cfg = objs[("ConfigMap", "itdesk-config")]["data"]
    cleanup = 5
    worker = objs[("Deployment", "itdesk-worker")]
    pre_stop = container(worker).get("lifecycle", {}).get("preStop", {}).get("sleep", {}).get("seconds", 0)
    assert worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] >= pre_stop + float(cfg["WORKER_GRACE_SECONDS"]) + cleanup
    assert float(cfg["WORKER_HEARTBEAT_SECONDS"]) * 3 <= float(cfg["WORKER_LEASE_SECONDS"])

    api = objs[("Deployment", "itdesk-api")]
    args = container(api)["args"]
    graceful = float(args[args.index("--timeout-graceful-shutdown") + 1])
    api_pre_stop = container(api)["lifecycle"]["preStop"]["sleep"]["seconds"]
    assert api["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] >= api_pre_stop + graceful + cleanup
    # uvicorn 的 keep-alive 要比前面负载均衡器的空闲超时（常见默认 60s）长，否则 LB 会复用一个刚被后端关掉的连接 → 502
    assert float(args[args.index("--timeout-keep-alive") + 1]) > 60


def test_probes_resources_and_security_context():
    objs = k8s()
    for name in ("itdesk-api", "itdesk-worker"):
        d = objs[("Deployment", name)]
        c = container(d)
        assert c["livenessProbe"]["httpGet"]["path"] == "/healthz" and c["readinessProbe"]["httpGet"]["path"] == "/readyz"
        assert c["resources"]["requests"]["memory"] and c["resources"]["limits"]["memory"]
        assert d["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
        assert c["securityContext"]["allowPrivilegeEscalation"] is False
        assert "replicas" not in d["spec"]  # 副本数交给 HPA / KEDA
        refs = {r.get("configMapRef", r.get("secretRef", {})).get("name") for r in c["envFrom"]}
        assert refs == {"itdesk-config", "itdesk-secrets"}
    for name in ("itdesk-api", "itdesk-worker"):
        pdb = objs[("PodDisruptionBudget", name)]
        assert pdb["apiVersion"] == "policy/v1" and pdb["spec"]["unhealthyPodEvictionPolicy"] == "AlwaysAllow"
    hpa = objs[("HorizontalPodAutoscaler", "itdesk-api")]
    assert hpa["apiVersion"] == "autoscaling/v2" and hpa["spec"]["minReplicas"] >= 2


def test_keda_scales_workers_on_queue_depth():
    objs = k8s()
    so = objs[("ScaledObject", "itdesk-worker")]
    trig = so["spec"]["triggers"][0]
    assert so["apiVersion"] == "keda.sh/v1alpha1" and trig["type"] == "postgresql"
    assert trig["authenticationRef"]["name"] == "itdesk-postgres"
    assert objs[("TriggerAuthentication", "itdesk-postgres")]["spec"]["secretTargetRef"][0]["parameter"] == "connection"
    assert "agent_jobs" in trig["metadata"]["query"]
    cfg = objs[("ConfigMap", "itdesk-config")]["data"]
    # 每个 worker 的目标负载 = 并发 × 目标利用率（0.5–1）
    target = float(trig["metadata"]["targetQueryValue"])
    assert 0.5 * int(cfg["WORKER_CONCURRENCY"]) <= target <= int(cfg["WORKER_CONCURRENCY"])
    assert so["spec"]["scaleTargetRef"]["name"] == "itdesk-worker"


def test_compose_services_and_referenced_files_exist():
    compose = docs(DEPLOY / "docker-compose.yml")[0]
    services = compose["services"]
    assert {"postgres", "redis", "otel-collector", "jaeger", "prometheus", "grafana", "litellm", "api", "worker", "migrate"} <= set(services)
    assert "pgvector" in services["postgres"]["image"]
    assert services["worker"]["deploy"]["replicas"] >= 2
    assert services["worker"]["stop_grace_period"] == "35s"
    for svc in services.values():  # 挂载的本地文件都要存在（复用第 28、29 课的配置）
        for vol in svc.get("volumes", []):
            src = vol.split(":")[0]
            if src.startswith("."):
                assert (DEPLOY / src).resolve().exists(), src


def test_dockerfile_is_multistage_and_non_root():
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert len(re.findall(r"^FROM ", text, re.M)) >= 2 and "COPY --from=build" in text
    assert re.search(r"^USER 10001", text, re.M)
    assert re.search(r'^CMD \["', text, re.M)  # exec 形式：进程直接收到 SIGTERM
    ignore = (DEPLOY / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore and ".venv" in ignore


def test_no_real_secrets_in_deploy_files():
    suspicious = re.compile(r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
    for f in [*DEPLOY.rglob("*"), REPO / "production" / ".env.example"]:
        if f.is_file():
            assert not suspicious.search(f.read_text(encoding="utf-8", errors="ignore")), f
    secret = k8s()[("Secret", "itdesk-secrets")]
    assert all("REPLACE_ME" in v or v in ("{}",) for v in secret["stringData"].values())
