"""交互式体验：python -m agentkit.chat

一个带几个示例工具的对话 Agent。每轮结束打印链路追踪树，让你"看见" Agent 的每一步。
输入 /trace 切换是否显示追踪，/exit 退出。
"""

from __future__ import annotations

import ast
import datetime
import operator
from typing import Annotated

from pydantic import Field

from .agent import Agent
from .llm import default_llm
from .tools import ToolError, tool
from .tracing import render_tree

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.Mod: operator.mod}


def _safe_eval(node: ast.AST) -> float:
    """只允许数字和四则运算的安全求值 —— 永远不要对模型给的字符串用 eval()！"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ToolError("表达式只能包含数字和 + - * / % ** 运算")


@tool
def calculator(expression: Annotated[str, Field(description="数学表达式，例如 (3+5)*2/7")]) -> str:
    """计算数学表达式。涉及任何数值计算时都应该使用本工具，而不是心算。"""
    return str(_safe_eval(ast.parse(expression, mode="eval")))


@tool
def current_time() -> str:
    """获取当前的日期和时间。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


def main() -> None:
    agent = Agent(default_llm(), [calculator, current_time], name="chat")
    history: list = []
    show_trace = True
    print("🤖 agentkit chat（/trace 切换追踪显示，/exit 退出）")
    while True:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("/exit", "/quit"):
            break
        if text == "/trace":
            show_trace = not show_trace
            print(f"追踪显示：{'开' if show_trace else '关'}")
            continue
        if not text:
            continue
        result = agent.run(text, history=history)
        history = result.history
        print(f"\n助手> {result.output}")
        if show_trace and result.trace:
            print("\n" + render_tree(result.trace))


if __name__ == "__main__":
    main()
