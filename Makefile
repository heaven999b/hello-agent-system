PY ?= .venv/bin/python

.PHONY: setup test test-solutions lesson check-env chat progress viewer

setup:            ## 创建虚拟环境并安装依赖
	python3 -m venv .venv
	.venv/bin/pip install -q -e ".[dev]"
	@test -f .env || cp .env.example .env
	@echo "✅ 安装完成。编辑 .env 填入模型地址和 key，然后 make check-env"

check-env:        ## 检查模型连通性
	$(PY) -m agentkit.check

test:             ## 跑框架测试 + 你的练习（练习没写完会失败，这是正常的）
	$(PY) -m pytest

test-solutions:   ## 用参考答案跑所有练习测试（CI 用）
	AGENTKIT_SOLUTION=1 $(PY) -m pytest

lesson:           ## 跑某一课的练习测试：make lesson N=01
	$(PY) -m pytest lessons/$(N)_*

chat:             ## 交互式体验一个带追踪显示的 Agent
	$(PY) -m agentkit.chat

progress:         ## 查看学习进度看板
	$(PY) scripts/progress.py

viewer:           ## 把追踪渲染成 HTML：make viewer T=runs/traces.jsonl
	$(PY) -m agentkit.viewer $(T) -o trace.html
