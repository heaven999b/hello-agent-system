"""检查仓库内所有 Markdown 的相对链接与锚点是否有效（CI 也会运行）。

用法：python scripts/check_links.py [--root .]
- 检查 [text](path) 与 [text](path#anchor)：目标文件必须存在；带锚点时，目标文档里必须有对应标题
- 锚点按 GitHub 规则从标题生成（小写、去标点、空格变 -、重复标题加 -1/-2 后缀）
- 外部链接（http/https/mailto）不检查
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

LINK_RE = re.compile(r"(?<!\!)\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
SKIP_DIRS = {".git", ".venv", "node_modules", "runs", "traces", "__pycache__", ".pytest_cache"}


def github_slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)  # 去 HTML 标签
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # 去图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接只留文字
    text = text.replace("`", "").strip().lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "-_ ":
            out.append("-" if ch == " " else ch)
        elif cat[0] in ("L", "N") or cat == "Mn":
            out.append(ch)
        # 其余标点、符号（含 emoji）去掉
    return "".join(out)


@lru_cache(maxsize=None)
def anchors_of(path: Path) -> frozenset[str]:
    seen: dict[str, int] = {}
    result = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if not m:
            continue
        slug = github_slug(m.group(2))
        n = seen.get(slug, 0)
        result.add(slug if n == 0 else f"{slug}-{n}")
        seen[slug] = n + 1
    for m in re.finditer(r'<a\s+(?:name|id)="([^"]+)"', path.read_text(encoding="utf-8")):
        result.add(m.group(1))
    return frozenset(result)


def check(root: Path) -> list[str]:
    problems = []
    for md in sorted(root.rglob("*.md")):
        if any(part in SKIP_DIRS for part in md.relative_to(root).parts):
            continue
        in_fence = False
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            if FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            line = re.sub(r"`[^`]*`", "", line)  # 行内代码里的示例链接不检查
            for m in LINK_RE.finditer(line):
                target = m.group(1).strip("<>")
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                    continue  # http:, https:, mailto: ...
                path_part, _, anchor = target.partition("#")
                dest = md if not path_part else (md.parent / path_part).resolve()
                where = f"{md.relative_to(root)}:{lineno}"
                if not dest.exists():
                    problems.append(f"{where}  missing file: {target}")
                    continue
                if anchor and dest.suffix == ".md" and anchor not in anchors_of(dest):
                    problems.append(f"{where}  missing anchor: {target}")
    problems += check_language_switchers(root)
    return problems


def check_language_switchers(root: Path) -> list[str]:
    """双语文档对：X.md 必须链接到 X.en.md（English），X.en.md 必须链接回 X.md（中文）。"""
    problems = []
    for en in sorted(root.rglob("*.en.md")):
        if any(part in SKIP_DIRS for part in en.relative_to(root).parts):
            continue
        zh = en.with_name(en.name[: -len(".en.md")] + ".md")
        if not zh.exists():
            problems.append(f"{en.relative_to(root)}  has no Chinese counterpart {zh.name}")
            continue
        head_en = "\n".join(en.read_text(encoding="utf-8").splitlines()[:20])
        head_zh = "\n".join(zh.read_text(encoding="utf-8").splitlines()[:20])
        if f"[中文]({zh.name})" not in head_en:
            problems.append(f"{en.relative_to(root)}  language switcher must link [中文]({zh.name})")
        if f"[English]({en.name})" not in head_zh:
            problems.append(f"{zh.relative_to(root)}  language switcher must link [English]({en.name})")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    problems = check(Path(args.root).resolve())
    for p in problems:
        print(p)
    print(f"{'❌' if problems else '✅'} {len(problems)} broken link(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
