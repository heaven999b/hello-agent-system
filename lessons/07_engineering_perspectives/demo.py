"""第 07 课 Demo：输入一个项目画像，输出"本项目必须考虑的工程清单"。

    python lessons/07_engineering_perspectives/demo.py             # 真实模型：从需求原文抽取画像（1 次调用，校验失败最多再修 2 次）
    python lessons/07_engineering_perspectives/demo.py --offline   # 离线：直接用人工标注的画像，不调用模型

可选参数：
    --lang en                 英文输出（真实模式下也用英文需求）
    --scenario voice          改用 README 第 3 节的某个预置场景画像（不调用模型）
    --requirement-file x.md   真实模式下，让模型读你自己的需求文档
    --all                     打印全部条目（默认只打印 P0 全部 + P1 前 12 条）
    --out checklist.md        把完整清单导出成可勾选的 Markdown

五步：
  1. 画像   真实模式让模型读需求、抽取画像（agentkit.workflows.complete_json），再和人工标注逐项对比
  2. 触发   triggered_considerations：通用必查点 + 被触发的情境点，附触发原因
  3. 排序   prioritize：P0 → P1 → P2，同级按场景权重
  4. 对比   同一套规则放到 9 个典型场景上，清单差多少
  5. 查漏   coverage_report：一份设计初稿漏了哪些维度？capstone/DESIGN.md 呢？
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))



def _load_sibling(name: str):
    """和 exercise.py / solution.py 用同一个模块名加载，保证大家拿到的是同一份目录。"""
    import importlib.util

    key = f"lesson07_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


P = _load_sibling("perspectives")

# ---------------------------------------------------------------- 界面文字

TEXT = {
    "zh": {
        "impl": "（规则引擎的实现来自 {name}）",
        "impl_ex": "exercise.py（你的实现）",
        "impl_sol": "solution.py（参考答案 —— 完成练习后会自动换成你的实现）",
        "s1": "1. 项目画像：把需求变成一组可以求值的事实",
        "s1_offline": "离线模式：使用人工标注的画像（第 4 节的完整小例子：销售邮件助理）",
        "s1_scenario": "使用预置场景画像：{name}",
        "s1_real": "真实模式：让模型读需求原文，抽取画像（complete_json + Pydantic 校验）",
        "requirement": "需求原文：",
        "calling": "调用模型中……",
        "llm_failed": "模型调用失败：{err}\n   可以先用 --offline 运行。",
        "true_facts": "成立的布尔事实：",
        "numbers": "数字：",
        "derived": "推导：peak_llm_rpm = peak_qps × llm_calls_per_request × 60 = {v}",
        "agree": "模型与人工标注一致 {n}/{total} 项。",
        "disagree": "不一致的事实（模型 → 人工）：",
        "assumptions": "模型自己说明的假设：",
        "diff_items": "因为这些差异，清单多出 {extra} 条、少了 {missing} 条，例如：",
        "take1": "画像里错一个事实，就会整组地多出或漏掉检查项 —— 所以抽取结果必须由人确认，而规则引擎对缺失事实要报错而不是默认 False。",
        "s2": "2. 触发：通用必查点 + 被触发的情境点",
        "summary": "通用必查 {g} 条 + 情境触发 {s} 条 = {n} 条（🔴 P0 {p0} · 🟠 P1 {p1} · 🟢 P2 {p2}）",
        "weights": "场景权重最高的维度（FOCUS_RULES 累加）：",
        "take2": "同样是 20 个维度，这个项目被触发的情境点集中在权重高的那几个维度上 —— 那里就是评审会该花时间的地方。",
        "s3": "3. 排序：先做什么",
        "p0_all": "全部 P0（上线前必须满足）：",
        "p1_top": "P1 前 {k} 条（应该满足，上线后一个迭代内补齐）：",
        "more": "…… 另有 {n} 条 P1、{m} 条 P2。加 --all 查看全部，或 --out checklist.md 导出。",
        "always": "通用",
        "trigger": "触发",
        "s4": "4. 对比：同一套规则，放到不同场景上",
        "col_scenario": "场景",
        "col_total": "总条数",
        "col_sit": "情境点",
        "col_top": "权重最高的维度",
        "this_project": "本项目",
        "take4": "通用必查点对所有场景都一样（{g} 条）；真正拉开差距的是情境点 —— 场景决定清单。",
        "s5": "5. 查漏：设计文档覆盖了哪些维度？",
        "draft": "(a) 一份典型的设计初稿（功能写得很细，风险几乎没写）：",
        "design_md": "(b) capstone/DESIGN.md（本仓库按真实评审格式写的设计文档）：",
        "covered": "覆盖 {c}/{t} 个维度（{pct:.0%}）",
        "missing": "缺失：",
        "by_heading": "标题",
        "by_keyword": "关键词",
        "take5": "关键词命中只说明\"提到了\"，不说明\"想清楚了\"：看「伦理与内容安全」那一行的证据 —— 它指的是知识库文章的审核，而不是输出的内容安全。工具负责找\"完全没提\"的维度，判断交给人。",
        "wrote": "完整清单已写入 {path}",
        "md_title": "# 工程考量清单",
        "md_profile": "画像",
    },
    "en": {
        "impl": "(rule engine implementation: {name})",
        "impl_ex": "exercise.py (your implementation)",
        "impl_sol": "solution.py (reference — switches to yours once the exercise is done)",
        "s1": "1. Project profile: turn the requirement into facts a rule engine can evaluate",
        "s1_offline": "Offline mode: using the hand-labeled profile (the worked example in section 4: a sales email assistant)",
        "s1_scenario": "Using a preset scenario profile: {name}",
        "s1_real": "Live mode: the model reads the requirement and extracts a profile (complete_json + Pydantic validation)",
        "requirement": "Requirement:",
        "calling": "Calling the model...",
        "llm_failed": "Model call failed: {err}\n   Try --offline first.",
        "true_facts": "Boolean facts that hold:",
        "numbers": "Numbers:",
        "derived": "Derived: peak_llm_rpm = peak_qps × llm_calls_per_request × 60 = {v}",
        "agree": "Model and human labels agree on {n}/{total} facts.",
        "disagree": "Disagreements (model → human):",
        "assumptions": "Assumptions stated by the model:",
        "diff_items": "Because of these differences the checklist gains {extra} and loses {missing} items, e.g.:",
        "take1": "One wrong fact adds or drops a whole group of checks. That is why a human must confirm the extracted profile, and why the rule engine raises on missing facts instead of treating them as False.",
        "s2": "2. Trigger: general checks + triggered situational checks",
        "summary": "{g} general + {s} situational = {n} items (🔴 P0 {p0} · 🟠 P1 {p1} · 🟢 P2 {p2})",
        "weights": "Dimensions with the highest scenario weight (sum of FOCUS_RULES):",
        "take2": "Every project has the same 20 dimensions, but the triggered items cluster in a few heavily weighted ones. That is where the design review should spend its time.",
        "s3": "3. Prioritize: what comes first",
        "p0_all": "All P0 items (must be met before launch):",
        "p1_top": "Top {k} P1 items (should be met; close within one iteration after launch):",
        "more": "... plus {n} more P1 and {m} P2 items. Use --all to see everything, or --out checklist.md to export.",
        "always": "general",
        "trigger": "trigger",
        "s4": "4. Compare: the same rules applied to different scenarios",
        "col_scenario": "Scenario",
        "col_total": "Total",
        "col_sit": "Situational",
        "col_top": "Highest-weighted dimensions",
        "this_project": "This project",
        "take4": "General checks are identical for every scenario ({g} items). The situational ones make the difference: the scenario shapes the checklist.",
        "s5": "5. Gap check: which dimensions does a design doc cover?",
        "draft": "(a) A typical first draft (features in detail, risks barely mentioned):",
        "design_md": "(b) capstone/DESIGN.md (this repo's design doc, written in real review format):",
        "covered": "{c}/{t} dimensions covered ({pct:.0%})",
        "missing": "Missing:",
        "by_heading": "heading",
        "by_keyword": "keyword",
        "take5": "A keyword hit means 'mentioned', not 'thought through'. Look at the evidence for 'Responsible AI & content safety': it refers to reviewing knowledge-base articles, not to output content safety. The tool finds dimensions that are not mentioned at all; judging the rest is a human's job.",
        "wrote": "Full checklist written to {path}",
        "md_title": "# Engineering checklist",
        "md_profile": "Profile",
    },
}

DRAFT_DOC = {
    "zh": """\
# 销售邮件助理 设计文档（初稿）
## 1. 背景与目标
销售每天花 2 小时回邮件。上线标准：草稿采纳率 ≥ 40%，p95 延迟 ≤ 8 秒。
## 2. 功能
读取来信 → 检索 CRM 与历史沟通 → 起草回复 → 销售点击发送；支持批量跟进。
## 3. 架构
单 Agent。模型选型：主模型 + 小模型做意图分类；提示词版本随代码发布。
## 4. 工具清单
search_crm、get_thread、draft_reply、send_email、bulk_follow_up。
## 5. 上下文策略
邮件线程超过 1 万 token 时只保留最近 5 封。
## 6. 评估方案
从 50 个真实邮件线程建评估集，人工打分。
## 7. 成本：TODO
## 8. 发布计划
先给 3 家客户灰度，再全量。
""",
    "en": """\
# Sales email assistant: design doc (first draft)
## 1. Background and goals
Reps spend 2 hours a day on email. Launch criteria: draft acceptance ≥ 40%, p95 latency ≤ 8 s.
## 2. Features
Read inbound mail → look up CRM and past conversations → draft a reply → rep clicks Send; bulk follow-ups.
## 3. Architecture
Single agent. Model selection: main model plus a small model for intent classification; prompt version ships with the code.
## 4. Tool list
search_crm, get_thread, draft_reply, send_email, bulk_follow_up.
## 5. Context strategy
When a thread exceeds 10k tokens, keep only the latest 5 emails.
## 6. Evaluation plan
Build an eval set from 50 real email threads, graded by people.
## 7. Cost: TODO
## 8. Rollout
Canary with 3 customers, then everyone.
""",
}


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 76 + f"\n  {title}\n" + "═" * 76, flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, n: int) -> str:
    """按显示宽度左对齐：中文字符在终端里占两个英文字符宽。"""
    return text + " " * max(0, n - width(text))


def short(text: str, n: int) -> str:
    out = ""
    for ch in text:
        if width(out + ch) > n - 1:
            return out + "…"
        out += ch
    return out


def fmt(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v)


# ---------------------------------------------------------------- 实现选择


def load_impl(t: dict):
    """优先用你在 exercise.py 里的实现；还没写完就用参考答案。"""
    import exercise  # noqa: E402

    try:
        exercise.triggered_considerations({}, [])
        exercise.prioritize([], {}, [])
        exercise.coverage_report("", [])
        return exercise, t["impl_ex"]
    except NotImplementedError:
        import solution  # noqa: E402

        return solution, t["impl_sol"]


# ---------------------------------------------------------------- 1. 画像


def fact_label(name: str, lang: str) -> str:
    f = P.FACT_BY_NAME[name]
    return f.zh if lang == "zh" else f.en


def show_profile(profile: dict, lang: str, t: dict) -> None:
    info(t["true_facts"])
    for f in P.FACTS:
        if f.kind == "bool" and profile[f.name] is True:
            info(f"  ✔ {pad(f.name, 24)}{short(fact_label(f.name, lang), 60)}")
    info(t["numbers"])
    for f in P.FACTS:
        if f.kind == "number":
            info(f"  · {pad(f.name, 24)}{pad(fmt(profile[f.name]), 10)}{short(fact_label(f.name, lang), 50)}")
    info(t["derived"].format(v=fmt(P.derive_facts(profile)["peak_llm_rpm"])))


def extract_profile(requirement: str, lang: str) -> tuple[dict, list[str]]:
    """真实模式：让模型把需求原文抽取成画像。一次模型调用；JSON 校验失败时最多修复 2 次。"""
    from pydantic import Field, create_model

    from agentkit import default_llm
    from agentkit.workflows import complete_json

    fields = {}
    for f in P.FACTS:
        typ = bool if f.kind == "bool" else float
        fields[f.name] = (typ, Field(description=f.zh if lang == "zh" else f.en))
    fields["assumptions"] = (
        list[str],
        Field(default_factory=list, description="需求里没有明确写、由你推断或估计的事实，每条一句话"
              if lang == "zh" else "facts not stated explicitly that you inferred or estimated, one sentence each"),
    )
    Model = create_model("ProjectProfile", **fields)

    if lang == "zh":
        system = "你是资深的 Agent 架构师，负责在设计评审前把需求整理成一份项目画像。只根据需求原文判断，不要臆造。"
        prompt = (
            "请逐项判断下面这个 Agent 项目的画像事实。\n"
            "规则：\n"
            "1. 布尔事实：需求明确或能直接推出时填 true/false；拿不准时按更保守（需要更多检查）的一侧填，并写进 assumptions；\n"
            "2. 数字事实：需求里有就照抄；没有就给出合理估计，并写进 assumptions；provider_rpm_limit 不知道就填 0；\n"
            "3. 注意区分：external_users 指公司外部的人在用；consumer_facing 仅指输出直接给普通消费者看；"
            "irreversible_actions 包括发出去就收不回的消息。\n\n"
            f"需求原文：\n{requirement}"
        )
    else:
        system = "You are a senior agent architect preparing a project profile before a design review. Judge only from the requirement; do not invent facts."
        prompt = (
            "Decide each profile fact for the agent project below.\n"
            "Rules:\n"
            "1. Boolean facts: answer true/false when stated or directly implied; when unsure, pick the more cautious side (the one that triggers more checks) and list it under assumptions.\n"
            "2. Numbers: copy them if stated; otherwise give a reasonable estimate and list it under assumptions; use 0 for provider_rpm_limit if unknown.\n"
            "3. Note: external_users means people outside the company use it; consumer_facing only means output is shown directly to general consumers; "
            "irreversible_actions includes messages that cannot be unsent.\n\n"
            f"Requirement:\n{requirement}"
        )
    result = complete_json(default_llm(), prompt, Model, system=system)
    data = result.model_dump()
    assumptions = data.pop("assumptions")
    for f in P.FACTS:  # 数字统一成 int（如果是整数），方便展示和比较
        if f.kind == "number" and float(data[f.name]).is_integer():
            data[f.name] = int(data[f.name])
    return data, assumptions


def compare_profiles(extracted: dict, labeled: dict, impl, lang: str, t: dict) -> None:
    diffs = [f.name for f in P.FACTS if extracted[f.name] != labeled[f.name]]
    info(t["agree"].format(n=len(P.FACTS) - len(diffs), total=len(P.FACTS)))
    if not diffs:
        return
    info(t["disagree"])
    for name in diffs:
        info(f"  ✘ {pad(name, 24)}{fmt(extracted[name])} → {fmt(labeled[name])}")
    got = {x.item.id for x in impl.triggered_considerations(P.derive_facts(extracted), P.CATALOG)}
    want = {x.item.id for x in impl.triggered_considerations(P.derive_facts(labeled), P.CATALOG)}
    extra, missing = sorted(got - want), sorted(want - got)
    if extra or missing:
        info(t["diff_items"].format(extra=len(extra), missing=len(missing)))
        for i in extra[:4]:
            info(f"  + {i}  {P.CATALOG_BY_ID[i].title(lang)}")
        for i in missing[:4]:
            info(f"  - {i}  {P.CATALOG_BY_ID[i].title(lang)}")


# ---------------------------------------------------------------- 2–3. 触发与排序


def focus_weights(impl, profile: dict) -> Counter:
    weights: Counter = Counter()
    for rule in P.FOCUS_RULES:
        if impl.evaluate(rule.when, profile)[0]:
            weights.update(rule.weights)
    return weights


def dim_name(dim: str, lang: str) -> str:
    d = P.DIMENSION_BY_ID[dim]
    return d.zh if lang == "zh" else d.en


def print_ranked(rows, lang: str, t: dict) -> None:
    for r in rows:
        icon = P.SEVERITY_ICON[r.item.severity]
        info(f"{r.rank:>4}. {icon} {pad('[' + short(dim_name(r.item.dimension, lang), 18) + ']', 20)}"
             f"{r.item.title(lang)}  ({r.item.id})")
        if r.item.when is not None:
            info(f"        ↳ {t['trigger']}: " + " · ".join(r.reasons))


def export_markdown(path: Path, ranked, profile: dict, lang: str, t: dict) -> None:
    lines = [t["md_title"], "", f"## {t['md_profile']}", ""]
    lines += [f"- `{k}` = {fmt(v)}" for k, v in profile.items()]
    for sev in P.SEVERITIES:
        lines += ["", f"## {P.SEVERITY_ICON[sev]} {sev}", ""]
        for r in ranked:
            if r.item.severity != sev:
                continue
            why = t["always"] if r.item.when is None else f"{t['trigger']}: " + " · ".join(r.reasons)
            lines.append(f"- [ ] **{r.item.title(lang)}** · {dim_name(r.item.dimension, lang)} · `{r.item.id}` — {why}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 5. 查漏


def show_coverage(impl, text: str, lang: str, t: dict, show_evidence: bool) -> None:
    report = impl.coverage_report(text, P.dimension_requirements(lang))
    info(t["covered"].format(c=len(report.covered), t=len(P.DIMENSIONS), pct=report.ratio))
    if show_evidence:
        for dim, ev in report.covered.items():
            how = t["by_heading"] if ev.how == "heading" else t["by_keyword"]
            info(f"  ✔ {pad(dim_name(dim, lang), 22)}{pad(how, 8)} L{ev.line_no:<4} {short(ev.line, 58)}")
    if report.missing:
        sep = "、" if lang == "zh" else ", "
        info(t["missing"] + " " + sep.join(dim_name(r.id, lang) for r in report.missing))


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="不调用模型，使用人工标注的画像")
    ap.add_argument("--lang", choices=["zh", "en"], default="zh")
    ap.add_argument("--scenario", choices=[s.id for s in P.SCENARIOS], help="使用预置场景画像")
    ap.add_argument("--requirement-file", type=Path, help="真实模式下，让模型读这份需求")
    ap.add_argument("--all", action="store_true", help="打印全部条目")
    ap.add_argument("--out", type=Path, help="导出 Markdown 清单")
    args = ap.parse_args()
    lang, t = args.lang, TEXT[args.lang]

    impl, impl_name = load_impl(t)
    print(t["impl"].format(name=impl_name))

    # ---- 1. 画像
    banner(t["s1"])
    labeled = P.EXAMPLE_PROFILE
    requirement = P.EXAMPLE_REQUIREMENT_ZH if lang == "zh" else P.EXAMPLE_REQUIREMENT_EN
    if args.scenario:
        s = P.SCENARIO_BY_ID[args.scenario]
        info(t["s1_scenario"].format(name=s.zh if lang == "zh" else s.en))
        profile = s.profile
        show_profile(profile, lang, t)
    elif args.offline:
        info(t["s1_offline"])
        info(t["requirement"])
        for line in requirement.strip().splitlines():
            info(f"  │ {line}")
        profile = labeled
        show_profile(profile, lang, t)
    else:
        info(t["s1_real"])
        if args.requirement_file:
            requirement = args.requirement_file.read_text(encoding="utf-8")
        info(t["requirement"])
        for line in requirement.strip().splitlines():
            info(f"  │ {line}")
        info(t["calling"])
        try:
            profile, assumptions = extract_profile(requirement, lang)
        except Exception as e:  # noqa: BLE001  演示程序：把错误讲清楚后退出
            info(t["llm_failed"].format(err=e))
            return 1
        problems = P.validate_profile(profile)
        assert not problems, problems  # Pydantic 已经保证了完整和类型；这里再兜一次底
        show_profile(profile, lang, t)
        if assumptions:
            info(t["assumptions"])
            for a in assumptions:
                info(f"  · {a}")
        if not args.requirement_file:
            compare_profiles(profile, labeled, impl, lang, t)
        takeaway(t["take1"])

    facts = P.derive_facts(profile)

    # ---- 2. 触发
    banner(t["s2"])
    triggered = impl.triggered_considerations(facts, P.CATALOG)
    sev = Counter(x.item.severity for x in triggered)
    n_general = sum(1 for x in triggered if x.item.when is None)
    info(t["summary"].format(g=n_general, s=len(triggered) - n_general, n=len(triggered),
                             p0=sev["P0"], p1=sev["P1"], p2=sev["P2"]))
    weights = focus_weights(impl, facts)
    info(t["weights"])
    per_dim = Counter(x.item.dimension for x in triggered if x.item.when is not None)
    for dim, w in weights.most_common(6):
        info(f"  {pad(dim_name(dim, lang), 24)}{'█' * int(w)} {w:g}   (+{per_dim[dim]})")
    takeaway(t["take2"])

    # ---- 3. 排序
    banner(t["s3"])
    ranked = impl.prioritize(triggered, facts)
    p0 = [r for r in ranked if r.item.severity == "P0"]
    p1 = [r for r in ranked if r.item.severity == "P1"]
    p2 = [r for r in ranked if r.item.severity == "P2"]
    if args.all:
        print_ranked(ranked, lang, t)
    else:
        info(t["p0_all"])
        print_ranked(p0, lang, t)
        k = 12
        info("")
        info(t["p1_top"].format(k=k))
        print_ranked(p1[:k], lang, t)
        info(t["more"].format(n=max(0, len(p1) - k), m=len(p2)))
    if args.out:
        export_markdown(args.out, ranked, profile, lang, t)
        info(t["wrote"].format(path=args.out))

    # ---- 4. 对比
    banner(t["s4"])
    rows = [(t["this_project"], facts)] + [
        (s.zh if lang == "zh" else s.en, P.derive_facts(s.profile)) for s in P.SCENARIOS
    ]
    info(f"{pad(t['col_scenario'], 34)}{pad(t['col_total'], 8)}{pad('P0', 6)}{pad(t['col_sit'], 12)}{t['col_top']}")
    for name, prof in rows:
        items = impl.triggered_considerations(prof, P.CATALOG)
        n_sit = sum(1 for x in items if x.item.when is not None)
        n_p0 = sum(1 for x in items if x.item.severity == "P0")
        top = ", ".join(dim_name(d, lang) for d, _ in focus_weights(impl, prof).most_common(3))
        info(f"{pad(short(name, 32), 34)}{pad(str(len(items)), 8)}{pad(str(n_p0), 6)}{pad(str(n_sit), 12)}{top}")
    takeaway(t["take4"].format(g=n_general))

    # ---- 5. 查漏
    banner(t["s5"])
    info(t["draft"])
    show_coverage(impl, DRAFT_DOC[lang], lang, t, show_evidence=False)
    info("")
    info(t["design_md"])
    show_coverage(impl, (ROOT / "capstone" / "DESIGN.md").read_text(encoding="utf-8"), lang, t, show_evidence=True)
    takeaway(t["take5"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
