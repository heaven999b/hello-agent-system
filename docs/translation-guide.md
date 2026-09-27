# Translation Guide / 翻译规范

This repository is bilingual. Every Markdown document has a Chinese version (`xxx.md`) and an English version (`xxx.en.md`) with **equivalent content and identical structure**.
本仓库是中英双语的：每个 Markdown 文档都有中文版（`xxx.md`）和英文版（`xxx.en.md`），内容等价、结构一致。

## Rules

1. **Language switcher**: the first line of both files is `[中文](xxx.md) | [English](xxx.en.md)`, followed by a blank line.
2. **Same structure**: same headings in the same order, same tables (same rows), same number of mermaid diagrams, same `<details>` blocks. A reviewer should be able to read the two files side by side.
3. **Idiomatic, not literal**: write the way a senior engineer writes English technical docs. Short sentences, active voice, no "translationese".
4. **Links**: links between Markdown docs point to the **same-language** version (`README.en.md`, `failure-modes.en.md`). Links to source code (`.py`) stay unchanged. Anchors (`#...`) must be updated to the English heading slugs.
5. **Code blocks**: keep code identical. Translate Chinese comments and Chinese string literals inside code blocks shown in the docs. When a doc quotes demo output (which prints Chinese), translate the output and add a short note once per document: "(Demo output translated from Chinese.)"
6. **Mermaid**: translate all labels; keep node IDs and structure; keep labels double-quoted.
7. **Chinese-specific content**: keep it, and add a few words of context for international readers, e.g. "Chinese mobile numbers (11 digits starting with 1)", "PIPL (China's Personal Information Protection Law)", "resident ID numbers".
8. **Lesson references**: "第 05 课" → "Lesson 05" (always two digits). Keep the lesson numbers and directory names exactly as in the Chinese source.
9. **Durations / numbers / citations**: keep them exactly; do not add or remove facts.

## Glossary (use consistently)

| 中文 | English |
|---|---|
| 企业问题卡片 | enterprise problem card |
| 通用必查点 / 情境触发点 | universal checks / situational triggers |
| 智能体 / Agent 主循环 | agent / agent loop |
| 工具调用 | tool call |
| 错误即观察 | errors as observations |
| 上下文工程 / 上下文窗口 | context engineering / context window |
| 滑动窗口 / 摘要压缩 | sliding window / summarization (compaction) |
| 长期记忆 | long-term memory |
| 编排 | orchestration |
| 检查点 / 持久化执行 | checkpoint / durable execution |
| 幂等 / 幂等键 | idempotency / idempotency key |
| 重试 / 指数退避 / 抖动 | retry / exponential backoff / jitter |
| 熔断器 / 降级 | circuit breaker / fallback |
| 预算 | budget |
| 人工审批 / 人在回路 | human approval / human-in-the-loop (HITL) |
| 护栏 | guardrail |
| 提示词注入 / 间接注入 | prompt injection / indirect prompt injection |
| 不可信数据隔离 | untrusted-data isolation (spotlighting) |
| 致命三要素 | lethal trifecta |
| 最小权限 | least privilege |
| 脱敏 | redaction |
| 审计日志 | audit log |
| 租户 / 多租户 / 吵闹的邻居 | tenant / multi-tenant / noisy neighbor |
| 链路追踪 / 可观测性 | tracing / observability |
| 评估 / 评估集 / LLM 评委 | evaluation (evals) / eval set / LLM-as-judge |
| 门禁 / 回归 | gate / regression |
| 租约 / 死信队列 / 背压 | lease / dead-letter queue / backpressure |
| 乐观锁 / 悲观锁 | optimistic concurrency / pessimistic locking |
| 灰度发布 / 金丝雀 / 影子模式 | progressive rollout / canary / shadow mode |
| 止血 / 回滚 / 紧急开关 | mitigation / rollback / kill switch |
| 复盘 | postmortem |
| 设计文档 / 架构决策记录 | design doc / ADR (architecture decision record) |
