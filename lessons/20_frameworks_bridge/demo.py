"""第 20 课 Demo：同一个 IT 服务台任务，agentkit / DSPy / LangGraph / OpenAI Agents SDK 四种实现，一张对比表。

    python lessons/20_frameworks_bridge/demo.py                   # 真实模型：四种实现依次运行（串行，模型并发 = 1）
    python lessons/20_frameworks_bridge/demo.py --offline         # 离线：只跑 agentkit（ScriptedLLM 剧本），其余框架打印说明
    python lessons/20_frameworks_bridge/demo.py --only dspy,langgraph

任务：员工 alice 说"VPN 提示证书过期；账号被锁了，帮我重置密码"。Agent 要查知识库、查账号状态，
并调用需要**人工审批**的 reset_password。审批人由 shared_tools.auto_approver 模拟（自动批准）。

某个框架没装时自动跳过，并打印安装命令。三个框架都装上：pip install dspy langgraph langchain-openai openai-agents
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import traceback
import unicodedata
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_sibling(name: str):
    """按文件路径加载同目录模块（课程目录名以数字开头，没法普通 import；加前缀避免和其他课重名）。"""
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


shared = _load_sibling("shared_tools")

# (展示名, 模块文件, 需要的发行包, 安装命令)
IMPLS = [
    ("agentkit", "impl_agentkit", [], ""),
    ("DSPy", "impl_dspy", ["dspy"], "pip install dspy"),
    ("LangGraph", "impl_langgraph", ["langgraph", "langchain-openai"], "pip install langgraph langchain-openai"),
    ("OpenAI Agents SDK", "impl_openai_agents", ["openai-agents"], "pip install openai-agents"),
]


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def missing_packages(dists: list[str]) -> list[str]:
    out = []
    for d in dists:
        try:
            version(d)
        except PackageNotFoundError:
            out.append(d)
    return out


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _width(s))


def print_table(rows: list[list[str]]) -> None:
    widths = [max(_width(r[i]) for r in rows) + 2 for i in range(len(rows[0]))]
    for n, row in enumerate(rows):
        print("  " + "".join(pad(cell, w) for cell, w in zip(row, widths)))
        if n == 0:
            print("  " + "-" * (sum(widths) - 2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="只跑 agentkit（ScriptedLLM），不需要 API key")
    parser.add_argument("--only", default="", help="逗号分隔：agentkit,dspy,langgraph,openai")
    args = parser.parse_args()
    only = {x.strip().lower() for x in args.only.split(",") if x.strip()}

    section("0. 任务：同一个问题、同一组工具、同一个审批人")
    print(f"  问题：{shared.QUESTION}")
    desk = shared.ITDesk()
    for name, fn in desk.functions().items():
        flag = "  ← 需要人工审批" if name in shared.NEEDS_APPROVAL else ""
        print(f"  工具 {name}：{fn.__doc__.strip().splitlines()[0]}{flag}")
    print("  身份：当前用户 alice 由系统通过闭包注入工具，模型看到的参数里没有\"用户名\"（第 03 课）。")
    print("  审批人：demo 里自动批准；真实系统里是工单 / IM 卡片，可能几小时后才回应。")

    results = []
    for idx, (label, module, dists, install) in enumerate(IMPLS, 1):
        if only and not any(k in label.lower().replace(" ", "") for k in only):
            continue
        section(f"{idx}. {label}")
        lacking = missing_packages(dists)
        if lacking:
            print(f"  ⏭  跳过：没有安装 {', '.join(lacking)}。安装：{install}")
            continue
        if args.offline and label != "agentkit":
            installed = ", ".join(f"{d} {version(d)}" for d in dists)
            print(f"  ⏭  离线模式跳过：{label} 需要真实模型（需要安装并联网）。已安装：{installed}。")
            print("     去掉 --offline 用真实模型运行；或者运行 test_integration.py，用该框架自带的假模型离线验证这份实现。")
            continue
        impl = _load_sibling(module)
        try:
            if label == "agentkit":
                res = impl.run(llm=impl.offline_llm()) if args.offline else impl.run()
            else:
                res = impl.run()
        except Exception as e:  # noqa: BLE001 —— 一个框架失败不影响其他框架的演示
            print(f"  ❌ 运行失败：{type(e).__name__}: {str(e)[:300]}")
            traceback.print_exc(limit=2)
            continue
        print(f"  版本：{res.version}")
        shared.print_result(res)
        results.append(res)

    section("5. 对比表")
    lines = {label: shared.count_code_lines(HERE / f"{module}.py") for label, module, _, _ in IMPLS}
    rows = [["框架", "版本", "模型调用", "工具调用", "tokens 入→出", "耗时", "有效代码行"]]
    for r in results:
        tokens = f"{r.input_tokens}→{r.output_tokens}" if r.input_tokens is not None else "n/a"
        rows.append([r.framework, r.version.split(" ")[0], str(r.llm_calls), str(len(r.tool_calls)), tokens, f"{r.seconds:.1f}s", str(lines[r.framework])])
    ran = {r.framework for r in results}
    for label, _, _, _ in IMPLS:
        if label not in ran:
            rows.append([label, "-", "-", "-", "-", "-", str(lines[label])])
    print_table(rows)
    print("\n  有效代码行 = 去掉空行、注释、docstring 和四个文件相同的 bootstrap 段之后的行数（工具本身在 shared_tools.py，不计入）。")
    if args.offline:
        print("  （离线模式：agentkit 的 token 数来自剧本里的假用量，耗时接近 0，不代表真实表现。）")

    section("6. 该观察什么")
    print(
        "  · 模型调用次数：DSPy 的 ReAct 总会多 1 次（最后用 ChainOfThought 从轨迹里抽答案），\n"
        "    而且它把工具说明、轨迹都写进提示词、不走原生 function calling，所以输入 token 通常明显更多。\n"
        "  · 审批：agentkit / LangGraph / OpenAI Agents SDK 都是\"暂停 → 状态落盘 → 恢复\"；DSPy 只能在工具里同步等。\n"
        "  · LangGraph 的 approval 节点执行次数比审批次数多：恢复时被中断的节点会从头重跑，所以它不能有副作用。\n"
        "  · OpenAI Agents SDK 的追踪默认上传到 OpenAI；这里用 set_trace_processors 换成了本地计数器。\n"
        "  · 同一个模型、同一段提示词，四次运行的工具调用顺序和次数也可能不同——这是模型的随机性，不是框架的差别。\n"
        "    想比较框架本身，要固定剧本（test_integration.py）或者多跑几次看分布（第 22 课）。"
    )


if __name__ == "__main__":
    main()
