"""评估（Evals）：Agent 工程里最重要、也最常被忽略的一环。

没有评估的 Agent 开发 = 凭感觉改 prompt → 修好一个 case、悄悄搞坏三个 case。
企业级做法和写单元测试一样：
1. 维护一个评估集（golden dataset）：真实用户问题 + 期望行为，持续从线上 bad case 补充；
2. 多种评分器组合：
   - 规则评分（便宜、确定）：必须包含/不能包含某些内容、必须调用/不能调用某些工具、状态必须是 X
   - 轨迹评分：工具调用顺序是否合理（比如"先查询再修改"）
   - LLM 评委（贵、有噪声，但能评开放式质量）：按评分细则（rubric）打分
3. 每次改 prompt / 换模型 / 改工具都跑一遍，通过率低于阈值或出现回归就不许上线（CI 门禁）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Union

from pydantic import BaseModel, Field

from .agent import Agent, RunResult
from .llm import LLM
from .tools import maybe_await
from .workflows import complete_json, parallel


@dataclass
class EvalCase:
    id: str
    input: str
    expect: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)  # 运行时身份：tenant_id / user_id / roles
    tags: list[str] = field(default_factory=list)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


# 评分器：普通函数（规则评分，纯计算）或 async 函数（LLM 评委，要调用模型）都可以
Grader = Callable[[EvalCase, RunResult], Union[list[Check], Awaitable[list[Check]]]]


def is_subsequence(expected: list[str], actual: list[str]) -> bool:
    """expected 是否按顺序出现在 actual 中（中间可以夹杂其他调用）。"""
    it = iter(actual)
    return all(any(a == e for a in it) for e in expected)


def normalize_text(text: str) -> str:
    """评分用的宽松归一化：全角转半角、去空白、统一连接符、小写。
    否则 "1-3 个工作日" 和 "1～3个工作日" 会被判成不同。"""
    import unicodedata

    text = unicodedata.normalize("NFKC", text or "").lower()
    for dash in ("～", "~", "—", "–", "至", "到"):
        text = text.replace(dash, "-")
    return "".join(text.split())


def rule_grader(case: EvalCase, result: RunResult) -> list[Check]:
    """根据 case.expect 里的规则打分。支持的键：
    status, must_contain, must_not_contain, must_call, must_not_call, tool_order, max_steps
    """
    e, out, called = case.expect, (result.output or ""), result.tools_called()
    norm_out = normalize_text(out)
    checks: list[Check] = []
    if "status" in e:
        checks.append(Check("status", result.status == e["status"], f"期望 {e['status']}，实际 {result.status}"))
    for s in e.get("must_contain", []):
        hit = normalize_text(s) in norm_out
        checks.append(Check(f"contains:{s}", hit, "" if hit else f"输出中缺少 {s!r}"))
    for s in e.get("must_not_contain", []):
        checks.append(Check(f"not_contains:{s}", s.lower() not in out.lower(), f"输出中出现了禁止内容 {s!r}" if s.lower() in out.lower() else ""))
    for t in e.get("must_call", []):
        checks.append(Check(f"called:{t}", t in called, f"没有调用 {t}，实际调用 {called}" if t not in called else ""))
    for t in e.get("must_not_call", []):
        checks.append(Check(f"not_called:{t}", t not in called, f"调用了禁止的工具 {t}" if t in called else ""))
    if "tool_order" in e:
        ok = is_subsequence(e["tool_order"], called)
        checks.append(Check("tool_order", ok, "" if ok else f"期望顺序 {e['tool_order']}，实际 {called}"))
    if "max_steps" in e:
        ok = result.steps <= e["max_steps"]
        checks.append(Check("max_steps", ok, "" if ok else f"用了 {result.steps} 步，上限 {e['max_steps']}"))
    return checks


class JudgeVerdict(BaseModel):
    score: int = Field(ge=1, le=5, description="1-5 分，5 分最好")
    reason: str = Field(description="打分理由，引用回答中的具体内容")


def llm_judge(llm: LLM, rubric: str, pass_score: int = 4) -> Grader:
    """LLM 评委。注意事项：
    - 评分细则要具体（"是否给出了具体的操作步骤"而不是"回答好不好"）；
    - 评委模型最好不是被测模型（避免自我偏好）；
    - 定期抽样人工复核评委的判断，校准它。
    """

    async def grade(case: EvalCase, result: RunResult) -> list[Check]:
        prompt = (
            f"你是严格的质量评审员。请根据评分细则给 AI 助手的回答打分。\n\n"
            f"## 评分细则\n{rubric}\n\n## 用户问题\n{case.input}\n\n## 助手回答\n{result.output}"
        )
        v = await complete_json(llm, prompt, JudgeVerdict)
        return [Check("llm_judge", v.score >= pass_score, f"{v.score}/5：{v.reason}")]

    return grade


@dataclass
class CaseResult:
    id: str
    passed: bool
    checks: list[Check]
    status: str
    output: str
    tools: list[str]
    steps: int
    tokens: int
    cost_usd: float
    latency_ms: float
    tags: list[str] = field(default_factory=list)
    infra_error: bool = False  # 模型 API / 网关故障导致的失败：不代表 Agent 能力，不应和"答错"混在一起


@dataclass
class EvalReport:
    results: list[CaseResult]

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def infra_errors(self) -> list[str]:
        """因基础设施故障（而非 Agent 行为）失败的用例。它们存在时，通过率不可信，应重跑而不是下结论。"""
        return [r.id for r in self.results if r.infra_error]

    def summary(self) -> str:
        n = len(self.results)
        lines = [
            f"评估结果：{sum(r.passed for r in self.results)}/{n} 通过（{self.pass_rate:.0%}）",
            f"平均 token：{sum(r.tokens for r in self.results) / max(n, 1):.0f}   "
            f"平均耗时：{sum(r.latency_ms for r in self.results) / max(n, 1):.0f}ms   "
            f"总成本：${sum(r.cost_usd for r in self.results):.4f}",
        ]
        if self.infra_errors:
            lines.append(f"⚠️ {len(self.infra_errors)} 个用例因模型 API / 网关故障失败（{self.infra_errors}），通过率不可信，请重跑")
        for r in self.results:
            mark = "✅" if r.passed else "❌"
            lines.append(f"{mark} {r.id}  status={r.status}  tools={r.tools}")
            for c in r.checks:
                if not c.passed:
                    lines.append(f"     ✗ {c.name}: {c.detail}")
        return "\n".join(lines)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps([asdict(r) for r in self.results], ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EvalReport":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([CaseResult(**{**d, "checks": [Check(**c) for c in d["checks"]]}) for d in data])  # 旧报告没有 infra_error 字段也能加载

    def regressions(self, baseline: "EvalReport") -> list[str]:
        """以前通过、现在失败的 case —— 上线前必须为零（或逐个确认）。"""
        before = {r.id: r.passed for r in baseline.results}
        return [r.id for r in self.results if before.get(r.id) and not r.passed]


def load_cases(path: str | Path) -> list[EvalCase]:
    """从 JSONL 文件加载评估用例，每行一个 JSON 对象。"""
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.strip().startswith("//"):
            cases.append(EvalCase(**json.loads(line)))
    return cases


async def run_eval(
    make_agent: Callable[[], Agent],
    cases: Iterable[EvalCase],
    graders: Iterable[Grader] = (rule_grader,),
    *,
    concurrency: int = 4,
) -> EvalReport:
    """对每个用例新建一个 Agent（保证用例之间互不影响），运行并评分。

    concurrency：同时在跑的用例数。100 个用例 × 每个 5 秒，串行要 8 分钟，并发 8 个只要 1 分钟；
    上限别设太高——评估和线上服务共用模型配额时，会把网关打出 429，反而拖慢、还会产生"基础设施失败"。
    结果按用例顺序返回，和并发度无关。
    """
    graders = list(graders)

    async def one(case: EvalCase) -> CaseResult:
        agent = make_agent()
        t0 = time.perf_counter()
        res = await agent.run(case.input, metadata=case.metadata)
        latency = (time.perf_counter() - t0) * 1000
        checks = [c for g in graders for c in await maybe_await(g(case, res))]
        infra = res.status == "failed" and (res.stop_reason or "").startswith("llm_error")
        return CaseResult(
            id=case.id,
            # 模型 / 网关故障的用例一律不算通过：Agent 根本没跑起来，"没调用危险工具"之类的规则会被碰巧满足，
            # 把通过率抬高（第 11 课实测：4 个 429 里 3 个被判为通过，报告 86%）
            passed=not infra and all(c.passed for c in checks),
            checks=checks,
            status=res.status,
            output=res.output or "",
            tools=res.tools_called(),
            steps=res.steps,
            tokens=res.usage.total,
            cost_usd=res.cost_usd,
            latency_ms=latency,
            tags=case.tags,
            infra_error=infra,
        )

    return EvalReport(await parallel([lambda c=c: one(c) for c in cases], max_concurrency=concurrency))
