"""第 24 课练习测试：离线、确定、毫秒级（不启动子进程、不调用模型）。

运行：make lesson N=24    或    .venv/bin/python -m pytest lessons/24_coding_agents -v
跳过可选题 (d)：.venv/bin/python -m pytest lessons/24_coding_agents -k "not is_test_file"
"""

from __future__ import annotations

import os

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)

LINES_120 = [f"line {i}" for i in range(1, 121)]


# =====================================================================
# (a) view_window
# =====================================================================


def test_view_window_start_of_file():
    out = ex.view_window(LINES_120, 1, 50).split("\n")
    assert out[0] == "1: line 1"  # 贴着文件开头：上面没有行，不输出"上面还有"
    assert out[49] == "50: line 50"  # 仍然显示满 50 行
    assert out[-1] == "（下面还有 70 行）"
    assert len(out) == 51


def test_view_window_centered_in_middle():
    out = ex.view_window(LINES_120, 60, 10).split("\n")
    assert out[0] == "（上面还有 54 行）"
    assert out[1] == "55: line 55"
    assert out[-2] == "64: line 64"
    assert out[-1] == "（下面还有 56 行）"
    assert len(out) == 12


def test_view_window_end_of_file_shifts_up():
    # center=118 时，按居中算会是 113–122，超出了文件末尾 → 窗口整体上移成 111–120
    out = ex.view_window(LINES_120, 118, 10).split("\n")
    assert out[0] == "（上面还有 110 行）"
    assert out[1] == "111: line 111"
    assert out[-1] == "120: line 120"
    assert "下面还有" not in "\n".join(out)


def test_view_window_small_file_and_out_of_range_center():
    lines = ["def f():", "    return 1", ""]
    assert ex.view_window(lines, 999, 50) == "1: def f():\n2:     return 1\n3: "
    assert ex.view_window(lines, -5, 50) == "1: def f():\n2:     return 1\n3: "


def test_view_window_empty_file_and_bad_window():
    assert ex.view_window([], 1, 50) == "（空文件）"
    with pytest.raises(ValueError):
        ex.view_window(LINES_120, 1, 0)


# =====================================================================
# (b) safe_path
# =====================================================================


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n")
    (root / "pkg" / "b.py").write_text("y = 2\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOKEN=not-a-real-token\n")
    return root, outside


def test_safe_path_allows_paths_inside(workspace):
    root, _ = workspace
    real = root.resolve()
    assert ex.safe_path(root, "a.py") == real / "a.py"
    assert ex.safe_path(root, "pkg/b.py") == real / "pkg" / "b.py"
    assert ex.safe_path(root, "pkg/../a.py") == real / "a.py"  # 绕一圈仍在工作区里：允许
    assert ex.safe_path(root, "new_dir/new.py") == real / "new_dir" / "new.py"  # 还不存在的文件也可以
    assert ex.safe_path(root, ".") == real


def test_safe_path_rejects_dotdot_escape(workspace):
    root, _ = workspace
    for bad in ["../outside/secret.txt", "pkg/../../outside/secret.txt", "..", "pkg/../../../../etc/passwd"]:
        with pytest.raises(PermissionError):
            ex.safe_path(root, bad)


def test_safe_path_absolute_paths(workspace):
    root, outside = workspace
    assert ex.safe_path(root, str(root.resolve() / "pkg" / "b.py")) == root.resolve() / "pkg" / "b.py"
    with pytest.raises(PermissionError):
        ex.safe_path(root, str(outside / "secret.txt"))
    with pytest.raises(PermissionError):
        ex.safe_path(root, "/etc/passwd")
    with pytest.raises(ValueError):
        ex.safe_path(root, "a.py\x00.txt")


def test_safe_path_rejects_symlink_escape(workspace):
    root, outside = workspace
    try:
        os.symlink(outside, root / "link_to_outside")
        os.symlink(root / "pkg", root / "link_inside")
    except (OSError, NotImplementedError):
        pytest.skip("当前系统不支持创建符号链接")
    with pytest.raises(PermissionError):
        ex.safe_path(root, "link_to_outside/secret.txt")  # 字符串里没有 ".."，但真实目标在工作区外
    assert ex.safe_path(root, "link_inside/b.py") == root.resolve() / "pkg" / "b.py"  # 指向工作区内部：允许


# =====================================================================
# (c) apply_edit_with_lint
# =====================================================================

SRC = "def total(items):\n    s = 0\n    for it in items:\n        s += it\n    return s\n"


def test_apply_edit_replaces_inclusive_range():
    new, err = ex.apply_edit_with_lint(SRC, 3, 4, "    for it in items:\n        s += it * 2\n")
    assert err is None
    assert new == "def total(items):\n    s = 0\n    for it in items:\n        s += it * 2\n    return s\n"
    # replacement 不带结尾换行时自动补上
    new2, err2 = ex.apply_edit_with_lint(SRC, 5, 5, "    return s + 1")
    assert err2 is None
    assert new2.endswith("    return s + 1\n")


def test_apply_edit_syntax_error_returns_original():
    new, err = ex.apply_edit_with_lint(SRC, 4, 4, "        s += (it\n")
    assert new == SRC  # 原文一个字符都不能变
    assert err is not None and err.startswith("第 ") and "行：" in err
    new, err = ex.apply_edit_with_lint(SRC, 4, 4, "s += it\n")  # 缩进错误（IndentationError 也是 SyntaxError）
    assert new == SRC
    assert err is not None and "第 4 行" in err


def test_apply_edit_insert_and_delete():
    inserted, err = ex.apply_edit_with_lint(SRC, 2, 1, '    """求和。"""\n')  # end = start - 1：插入到第 2 行之前
    assert err is None
    assert inserted.split("\n")[1] == '    """求和。"""'
    assert inserted.split("\n")[2] == "    s = 0"
    appended, err = ex.apply_edit_with_lint("a = 1", 2, 1, "b = 2")  # 原文末尾没有换行；追加到末尾
    assert err is None and appended == "a = 1\nb = 2\n"
    deleted, err = ex.apply_edit_with_lint("a = 1\nb = 2\nc = 3\n", 2, 2, "")
    assert err is None and deleted == "a = 1\nc = 3\n"


def test_apply_edit_rejects_bad_line_numbers():
    for start, end in [(0, 1), (2, 7), (7, 7), (3, 1)]:
        with pytest.raises(ValueError):
            ex.apply_edit_with_lint(SRC, start, end, "x = 1\n")


# =====================================================================
# (d) is_test_file（可选）
# =====================================================================


def test_is_test_file_protects_tests_and_configs():
    for p in [
        "tests/test_pricing.py", "test_pricing.py", "pkg/pricing_test.py", "tests/helpers.py",
        "conftest.py", "pkg/conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
        "Tests\\Test_Pricing.py", "tests.py", "src/testing/fixtures.py",
    ]:
        assert ex.is_test_file(p), p


def test_is_test_file_allows_source_files():
    for p in ["pricing.py", "src/contest.py", "latest.py", "testing_utils.py", "docs/testing.md", "receipt.py", "README.md"]:
        assert not ex.is_test_file(p), p
