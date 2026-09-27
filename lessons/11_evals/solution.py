"""第 11 课练习（参考答案）：评估驱动开发的三件套。

    (a) pass_at_k / pass_hat_k      多次试验下的"能力"与"可靠性"
    (b) precedence_grader           轨迹评分：B 之前必须先做过 A（如"先验证身份，再重置密码"）
    (c) release_gate                CI 门禁：这个版本能不能上线？
"""

from __future__ import annotations

from math import comb

from agentkit.agent import RunResult
from agentkit.evals import Check, EvalCase, EvalReport, Grader


# ---------------------------------------------------------------- (a) pass@k 与 pass^k


def _validate(n: int, c: int, k: int) -> None:
    if n < 1:
        raise ValueError(f"试验次数 n 必须 ≥ 1，收到 {n}")
    if not 0 <= c <= n:
        raise ValueError(f"成功次数 c 必须在 [0, n] 之间，收到 c={c}, n={n}")
    if not 1 <= k <= n:
        raise ValueError(f"k 必须在 [1, n] 之间（样本不够就无法无偏估计），收到 k={k}, n={n}")


def pass_at_k(n: int, c: int, k: int) -> float:
    """pass@k：从 n 次试验里随机抽 k 次，**至少有 1 次成功**的概率（无偏估计）。

        pass@k = 1 - C(n - c, k) / C(n, k)

    直觉：C(n - c, k) / C(n, k) 是"抽到的 k 次全是失败"的概率，1 减去它就是"至少一次成功"。
    衡量的是**能力上限**："多给几次机会，它能不能做出来？" 适合有验证器、可以挑最好结果的场景
    （比如代码生成可以跑单测挑出正确答案）。来源：OpenAI Codex 论文（Chen et al., 2021）。

    例：n=5 次里成功 c=2 次，k=2 → 1 - C(3,2)/C(5,2) = 1 - 3/10 = 0.7

    边界：n ≥ 1；0 ≤ c ≤ n；1 ≤ k ≤ n，否则抛 ValueError。
         c == 0 → 0.0；n - c < k（失败次数不够凑满 k 次）→ 1.0。
    """
    _validate(n, c, k)
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k（读作 pass hat k）：从 n 次试验里随机抽 k 次，**k 次全部成功**的概率（无偏估计）。

        pass^k = C(c, k) / C(n, k)

    衡量的是**可靠性**："同一个任务让它做 k 次，每次都对的概率有多大？"
    客服、审批、运维这类面向真实用户的 Agent 没有"挑最好的一次"的机会，每一次都必须对，
    所以 pass^k 比 pass@k 更能反映线上体验。来源：τ-bench（Yao et al., 2024）。

    例：n=5 次里成功 c=2 次，k=2 → C(2,2)/C(5,2) = 1/10 = 0.1

    注意：它不等于 (c/n)^k。上面的例子里 (2/5)^2 = 0.16。组合公式对应"不放回抽样"，
    在 n 较小时对真实可靠性的估计是无偏的，而 (c/n)^k 会高估。

    边界：同 pass_at_k。c < k 时 C(c, k) = 0 → 0.0；c == n → 1.0。
    """
    _validate(n, c, k)
    return comb(c, k) / comb(n, k)


# ---------------------------------------------------------------- (b) 轨迹评分：先后顺序


def precedence_grader(rules: list[tuple[str, str]]) -> Grader:
    """返回一个评分器：对每条规则 (A, B)，如果调用了 B，那么 B **第一次**出现之前必须调用过 A。

    典型规则：("verify_identity", "reset_password")、("get_order", "refund")、("dry_run", "deploy")。

    返回的 grader(case, result) 对每条规则产出一个 Check：
        name   = f"precedence:{A}->{B}"
        passed = True / False
        detail = 失败时说明原因，例如 "reset_password 第 1 次出现在第 1 次工具调用，此前没有调用 verify_identity"

    规则细节：
    - 用 result.tools_called() 取得按顺序排列的工具名列表；
    - 没调用 B → 规则不适用，视为通过（detail 写明"未调用 B"）；
    - 只看 B 的第一次出现：第一次 B 之前没有 A 就算失败，即使后面补调了 A；
    - rules 为空 → grader 返回空列表；
    - 规则里 A == B 没有意义，构造时直接抛 ValueError（尽早暴露配置错误）。

    局限（写在 README 里）：它只检查"调用过"，不检查"调用成功"。验证码错误时 verify_identity
    调用失败，但顺序规则依然满足，所以还要配合 must_not_call 等规则一起用。
    """
    for a, b in rules:
        if a == b:
            raise ValueError(f"无意义的规则 ({a!r}, {b!r})：前后两个工具相同")
    rules = list(rules)

    def grade(case: EvalCase, result: RunResult) -> list[Check]:
        called = result.tools_called()
        checks = []
        for a, b in rules:
            name = f"precedence:{a}->{b}"
            if b not in called:
                checks.append(Check(name, True, f"未调用 {b}，规则不适用"))
                continue
            first_b = called.index(b)
            ok = a in called[:first_b]
            detail = "" if ok else f"{b} 第 1 次出现在第 {first_b + 1} 次工具调用，此前没有调用 {a}（实际顺序 {called}）"
            checks.append(Check(name, ok, detail))
        return checks

    return grade


# ---------------------------------------------------------------- (c) CI 门禁


def _avg_cost(report: EvalReport) -> float:
    return sum(r.cost_usd for r in report.results) / len(report.results) if report.results else 0.0


def release_gate(
    report: EvalReport,
    baseline: EvalReport | None,
    min_pass_rate: float,
    max_cost_increase: float,
    blocking_tags: tuple[str, ...] = ("safety",),
) -> tuple[bool, list[str]]:
    """上线门禁：返回 (是否放行, 不放行的原因列表)。放行时原因列表为空。

    依次检查（每条不通过都追加一条原因，**不要**遇到第一条就返回，方便一次看全所有问题）：
    1. 报告为空 → 直接返回 (False, ["评估报告为空..."])，其余检查跳过。
    2. 通过率：report.pass_rate < min_pass_rate → 不放行。等于门槛算通过。
    3. 一票否决：任何带有 blocking_tags 中任一标签的用例失败 → 不放行（不管整体通过率多高）。
       每个失败的这类用例追加一条原因，原因里包含用例 id。
    4. 回归：baseline 不为 None 时，report.regressions(baseline) 非空 → 不放行，原因里列出所有回归的用例 id。
       （基线里没有的新用例失败不算回归，它只影响通过率。）
    5. 成本：baseline 不为 None 且基线的"平均每用例成本" > 0 时，
       涨幅 = 新平均成本 / 基线平均成本 - 1；涨幅 > max_cost_increase → 不放行。等于上限算通过。
       max_cost_increase 用小数表示：0.2 表示最多允许上涨 20%。
       基线平均成本为 0（比如离线剧本）时无法计算涨幅，跳过这一项。

    为什么用"平均每用例成本"而不是总成本？因为新版本的评估集可能加了用例，总成本自然会涨。
    """
    if not report.results:
        return False, ["评估报告为空：没有任何用例结果，不能放行"]

    reasons: list[str] = []
    if report.pass_rate < min_pass_rate:
        reasons.append(f"通过率 {report.pass_rate:.1%} 低于门槛 {min_pass_rate:.1%}")

    blocking = set(blocking_tags)
    for r in report.results:
        hit = blocking.intersection(r.tags)
        if hit and not r.passed:
            reasons.append(f"一票否决：{'/'.join(sorted(hit))} 用例 {r.id} 失败")

    if baseline is not None:
        regressed = report.regressions(baseline)
        if regressed:
            reasons.append(f"回归：{len(regressed)} 个用例在基线中通过、现在失败：{', '.join(regressed)}")
        old, new = _avg_cost(baseline), _avg_cost(report)
        if old > 0:
            increase = new / old - 1
            if increase > max_cost_increase:
                reasons.append(f"平均每用例成本上涨 {increase:.0%}（${old:.5f} → ${new:.5f}），超过上限 {max_cost_increase:.0%}")

    return not reasons, reasons
