"""第 08 课练习：评估驱动开发的三件套。

你要实现 4 个函数（把 raise NotImplementedError 换成你的代码）：
    (a) pass_at_k / pass_hat_k      多次试验下的"能力"与"可靠性"
    (b) precedence_grader           轨迹评分：B 之前必须先做过 A（如"先验证身份，再重置密码"）
    (c) release_gate                CI 门禁：这个版本能不能上线？

验证：make lesson N=08   （或 .venv/bin/python -m pytest lessons/08_evals）

需要用到的 agentkit 类型（见 agentkit/evals.py）：
    Check(name, passed, detail)                    一条检查结果
    Grader = Callable[[EvalCase, RunResult], list[Check]]
    EvalReport.results: list[CaseResult]           每个用例一条：id / passed / cost_usd / tags ...
    EvalReport.pass_rate                           通过率（0~1）
    EvalReport.regressions(baseline) -> list[str]  基线通过、现在失败的用例 id
    RunResult.tools_called() -> list[str]          按顺序排列的工具调用名
"""

from __future__ import annotations

from math import comb  # noqa: F401  提示：comb(n, k) 就是组合数 C(n, k)，k > n 时返回 0

from agentkit.agent import RunResult  # noqa: F401
from agentkit.evals import Check, EvalCase, EvalReport, Grader  # noqa: F401


# ---------------------------------------------------------------- (a) pass@k 与 pass^k


def pass_at_k(n: int, c: int, k: int) -> float:
    """pass@k：从 n 次试验里随机抽 k 次，**至少有 1 次成功**的概率（无偏估计）。

        pass@k = 1 - C(n - c, k) / C(n, k)

    直觉：C(n - c, k) / C(n, k) 是"抽到的 k 次全是失败"的概率，1 减去它就是"至少一次成功"。
    衡量的是**能力上限**："多给几次机会，它能不能做出来？" 适合有验证器、可以挑最好结果的场景
    （比如代码生成可以跑单测挑出正确答案）。来源：OpenAI Codex 论文（Chen et al., 2021）。

    例：n=5 次里成功 c=2 次，k=2 → 1 - C(3,2)/C(5,2) = 1 - 3/10 = 0.7

    边界：n ≥ 1；0 ≤ c ≤ n；1 ≤ k ≤ n，否则抛 ValueError。
         c == 0 → 0.0；n - c < k（失败次数不够凑满 k 次）→ 1.0。

    提示：先写参数校验（pass_hat_k 也要用，可以抽成一个小函数），再套公式。
    """
    raise NotImplementedError("TODO: 实现 pass@k 的无偏估计")


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
    raise NotImplementedError("TODO: 实现 pass^k 的无偏估计")


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

    提示：这是一个"返回函数的函数"（闭包）。校验 rules 写在外层（构造时执行），
    打分逻辑写在内层 def grade(case, result) 里，最后 return grade。
    list.index(x) 返回 x 第一次出现的下标；called[:i] 是它之前的所有调用。
    """
    raise NotImplementedError("TODO: 返回一个检查工具调用先后顺序的 Grader")


# ---------------------------------------------------------------- (c) CI 门禁


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

    提示：最后 return (not reasons, reasons) —— 没有任何原因就放行。
    """
    raise NotImplementedError("TODO: 实现上线门禁：通过率 / 一票否决 / 回归 / 成本")
