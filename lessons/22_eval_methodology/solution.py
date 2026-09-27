"""第 22 课练习参考答案。先自己做，再来对照。

参考答案就是 evalstats.py 里的同名函数（讲义第 2 节逐段讲解了它们为什么这么写）：
    wilson_interval        → evalstats.wilson_interval（2.1 节）
    paired_bootstrap_diff  → evalstats.paired_bootstrap_diff（2.2 节）
    debiased_pairwise      → evalstats.debiased_pairwise（2.5 节）
这里直接复用，保证"讲义里的代码 = 参考答案 = demo 用的实现"只有一份。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（课程目录名以数字开头，没法写普通的 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"  # 例如 "22_eval_methodology__evalstats"，避免和其他课的同名模块冲突
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


evalstats = _load_sibling("evalstats")

Z95 = evalstats.Z95
Interval = evalstats.Interval
Judge = evalstats.Judge
wilson_interval = evalstats.wilson_interval
paired_bootstrap_diff = evalstats.paired_bootstrap_diff
debiased_pairwise = evalstats.debiased_pairwise
