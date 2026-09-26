"""配置加载：从环境变量或 .env 读取模型连接信息。

企业里配置的三条铁律：
1. 密钥只放环境变量 / 密钥管理服务（Vault、KMS），绝不写进代码或提交到 git；
2. 代码里只读配置，不硬编码地址和模型名，方便按环境（dev/staging/prod）切换；
3. 已存在的环境变量优先于 .env 文件，方便在 CI / 容器里覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """极简 .env 加载器（不引入 python-dotenv 依赖）。返回实际加载的文件路径。"""
    candidates = [Path(path)] if path else [Path.cwd() / ".env", _REPO_ROOT / ".env"]
    for p in candidates:
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            return p
    return None


def env(name: str, default: str | None = None) -> str | None:
    load_dotenv()
    return os.environ.get(name, default)
