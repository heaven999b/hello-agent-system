---
name: 🐛 Bug 报告
about: agentkit 框架、demo、练习测试或脚本运行出错
title: "[Bug] "
labels: bug
assignees: ""
---

<!--
⚠️ 提交前请确认：
- 已搜索过现有 issue，没有重复；
- 贴出的日志、trace、截图里没有 API key、.env 内容或其他敏感信息。
如果问题是"练习做不出来 / 讲义看不懂"，请改用「📚 课程反馈」模板。
-->

## 问题描述

<!-- 一两句话说明发生了什么。 -->

## 复现步骤

```bash
# 例如：
.venv/bin/python lessons/05_reliability/demo.py --offline
```

1.
2.
3.

## 期望行为

## 实际行为

<!-- 请贴完整的报错信息 / traceback（用代码块包起来）。 -->

```text

```

## 环境

- 操作系统：<!-- macOS 14 / Ubuntu 22.04 / Windows 11 + WSL ... -->
- Python 版本（`python --version`）：
- 仓库版本（`git rev-parse --short HEAD`）：
- 运行方式：<!-- --offline 离线剧本 / 真实模型 -->
- 模型与接口（使用真实模型时）：<!-- 例如 gpt-4o-mini @ OpenAI、deepseek-chat @ 官方 API、本地 vLLM -->

## 补充信息

<!--
可选：相关的 trace（python -m agentkit.viewer traces.jsonl -o trace.html 生成的页面截图）、
你已经尝试过的排查方法、你认为可能的原因。
-->
