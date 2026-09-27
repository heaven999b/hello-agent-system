"""课程练习的测试辅助。

每节课目录里有 exercise.py（你来写）和 solution.py（参考答案）。
测试默认加载 exercise.py；设置环境变量 AGENTKIT_SOLUTION=1 则加载 solution.py（CI 用它保证测试本身正确）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def load_sibling(anchor_file: str, name: str) -> ModuleType:
    """加载与 anchor_file 同目录的模块 name.py，模块名带上课程目录前缀，避免不同课程的同名模块互相覆盖。"""
    here = Path(anchor_file).resolve().parent
    mod_name = f"_lesson_{here.name}_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, here / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def load_exercise(test_file: str, name: str = "exercise") -> ModuleType:
    here = Path(test_file).resolve().parent
    use_solution = os.environ.get("AGENTKIT_SOLUTION") == "1"
    filename = "solution.py" if use_solution and name == "exercise" else f"{name}.py"
    path = here / filename
    mod_name = f"_lesson_{here.name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
