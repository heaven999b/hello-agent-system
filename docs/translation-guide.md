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
8. **Lesson references**: "第 08 课" → "Lesson 08" (always two digits). Keep the lesson numbers and directory names exactly as in the Chinese source.
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

### Part 3 terms (Lessons 17–25) / 第三部分术语（第 17–25 课）

| 中文 | English |
|---|---|
| 稠密检索 / 稀疏检索 | dense retrieval / sparse retrieval |
| 混合检索 / 倒数排名融合 | hybrid search / reciprocal rank fusion (RRF) |
| 召回 / 重排 | retrieval (first stage) / reranking |
| 双塔 / 交叉编码器 / 晚交互 | bi-encoder / cross-encoder / late interaction |
| 近似最近邻 / 暴力检索 | approximate nearest neighbor (ANN) / brute-force (flat) search |
| 查询改写 / 多查询 / 假设文档 | query rewriting / multi-query / HyDE (hypothetical document) |
| 词汇鸿沟 | vocabulary gap |
| 分级标注 / 未标注 ≠ 不相关 | graded relevance labels / unlabeled ≠ irrelevant |
| 固定上下文预算 | fixed context budget |
| Mem0 式写入 / 抽取 → 比对 → 决策 | Mem0-style write path / extract → compare → decide |
| 规则兜底 | rule-based guard |
| 单值槽位 | single-valued slot |
| 软删除 / 物理删除 | soft delete / hard delete |
| 写时消解 / 读时消解 | resolve at write time / resolve at read time |
| 分层记忆 / 核心记忆 / 归档记忆 / 回忆记忆 | tiered memory / core memory / archival memory / recall memory |
| 记忆巩固 / 反思 / 血缘 / 级联删除 | memory consolidation / reflection / lineage / cascading deletion |
| 近期性 / 重要性 / 相关性 | recency / importance / relevance |
| 双代兼容 / 旧版 / 现代版 | dual-era / legacy / modern (MCP protocol generations) |
| 能力协商 | capability negotiation |
| 协议错误 / 工具执行错误 | protocol error / tool execution error |
| 工具注解 / 工具定义指纹 | tool annotations / tool-definition fingerprint |
| 工具投毒 / 事后变脸 / 工具遮蔽 | tool poisoning / rug pull / tool shadowing |
| 进程级沙箱 / OS 级沙箱 | process-level sandbox / OS-level sandbox |
| Seatbelt（macOS 沙箱）/ bubblewrap | Seatbelt (`sandbox-exec`) / bubblewrap (keep the names) |
| 用户态内核 / 微虚拟机 | user-space kernel (gVisor) / microVM |
| 签名 / 模块 / 优化器 | signature / module / optimizer (DSPy) |
| 超步 / reducer | super-step / reducer (LangGraph) |
| 中断与恢复 | interrupt / resume |
| 数据飞轮 | data flywheel |
| 示范 / 反馈 / 人工标注 | demonstration / feedback / human label |
| 分层抽样 / 逆概率权重 | stratified sampling / inverse probability weight |
| 近重复 / 工具序列签名 | near-duplicate / tool signature |
| 合成数据 / 种子 × 维度 / 分布偏移 | synthetic data / seeds × dimensions / distribution shift |
| 模型崩溃 | model collapse |
| 数据泄漏 / 按组划分 | data leakage / split by group |
| 训练集 / 开发集 / 测试集 | train / dev / test set |
| 一致率 / Cohen's kappa / kappa 悖论 | percent agreement / Cohen's kappa / kappa paradox |
| 标准漂移 / 标注指南 | criteria drift / annotation guidelines |
| 评估四元组（请求、环境、停止条件、评分器） | four-tuple (request, environment, stopping criteria, scorer) |
| 基准有效性 / 数据污染 / 基准饱和 | benchmark validity / data contamination / benchmark saturation |
| 成对评委 / 位置偏差 | pairwise judge / position bias |
| 置信区间 / 配对检验 / 配对 bootstrap | confidence interval / paired test / paired bootstrap |
| 任务有效性 / 结果有效性 | task validity / outcome validity |
| ABC 清单 | ABC checklist (Agentic Benchmark Checklist) |
| 探针 Agent / 参考解 / 平凡 Agent | probe agent / reference solution (oracle solver) / trivial agent |
| 单点评分 / 成对比较 / 参考答案引导 | pointwise (single-answer) grading / pairwise comparison / reference-guided grading |
| 交换顺序去偏（保守做法） | swap the order to debias (the conservative approach) |
| Wilson 区间 / Wald 区间 | Wilson interval / Wald interval |
| McNemar 检验 / 精确二项检验 / 卡方近似 | McNemar's test / exact binomial test / chi-square approximation |
| 聚类标准误 / 功效 | clustered standard errors / power |
| 非劣效检验 / 多重比较 | non-inferiority / multiple comparisons |
| 评估 harness / Agent harness | evaluation harness / agent harness |
| 基础设施错误 | infrastructure error (`infra_error`) |
| 提示词优化 / 提议器 | prompt optimization / proposer |
| 测试时计算 / 自一致性 / 验证器 | test-time compute / self-consistency / verifier |
| 帕累托前沿 / 两级接受 | Pareto front / two-stage acceptance |
| 赢家诅咒 / 背题 | winner's curse / memorization |
| 蒸馏 / 监督微调 / 偏好优化 | distillation / supervised fine-tuning (SFT) / preference optimization (DPO) |
| 钻评分器空子 / 奖励投机 / 特判 | gaming the grader (specification gaming) / reward hacking / special-casing |
| 智能体-计算机接口 | agent-computer interface (ACI) |
| 运行框架（挽具） | harness |
| 功能清单 / 进度文件 / 接班 / 接班简报 | feature list / progress file / taking over / handover briefing |
| 测试保护 / diff 审查 | test protection / diff review |
| 主动式 / 被动式 | proactive / reactive |
| 混合主动 | mixed-initiative |
| 用户模型 / 推断 / 置信度 | user model / inference (belief) / confidence |
| 打扰成本 / 打扰决策器 | interruption cost / interruption decider |
| 现在说 / 攒着说（摘要）/ 不说 | interrupt now / defer (digest) / drop |
| 勿扰时段 / 频率上限 / 紧急通道 | quiet hours / rate limit / urgent override |
| 50% 时间跨度 | 50% time horizon |
| 可扩展的监督 | scalable oversight |
