## 这个 PR 做了什么？

<!-- 一两句话说明改动内容和原因。关联的 issue：Closes #xxx -->

## 改动类型

- [ ] 🐛 修复 bug
- [ ] 📚 课程内容（讲义 / demo / 练习）
- [ ] 🆕 新增一节课或新章节
- [ ] 🧰 agentkit 框架
- [ ] 🛠 工具与脚本（viewer、进度看板、CI 等）
- [ ] 📝 文档 / 错别字
- [ ] 🌐 翻译

## 自检清单

- [ ] `make test-solutions` 全部通过（与 CI 相同）
- [ ] 改动涉及的 demo 能用 `--offline` 运行，无需 API key
- [ ] 练习：`exercise.py` 与 `solution.py` 接口一致；未实现处 `raise NotImplementedError`，未实现时测试失败原因是 `NotImplementedError`
- [ ] 新增测试离线、确定性（用 `ScriptedLLM`，不依赖网络、真实时间、随机数）
- [ ] 讲义遵循课程编写规范 `docs/lesson-template.md`，文中的命令和代码片段都实际运行过
- [ ] 引用的论文、博客、事故真实存在并已核实
- [ ] 兼容 Python 3.10+，没有引入新的运行时依赖
- [ ] 没有提交密钥、`.env` 或运行产物（`runs/`、`traces/`、生成的 HTML）

## 如何验证

```bash
# 例如：
make test-solutions
.venv/bin/python lessons/NN_topic/demo.py --offline
```

## 截图 / 输出（可选）

<!-- 讲义渲染效果、demo 输出、trace 查看器截图等。 -->
