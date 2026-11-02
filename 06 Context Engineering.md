---
type: "concept"
tags:
  - "agent"
  - "agent/concept"
status: "待学习"
created: "2026-10-30"
updated: "2026-10-30"
aliases:
  - "Context Engineering"
  - "Context 上下文"
module: "06"
domain: "B 指令、上下文与知识"
level: "必学"
learning_layer: "应用主线"
directions:
  - "应用开发"
  - "知识工程"
---

# 06 Context Engineering

决定每轮模型实际看到哪些指令、历史、记忆、证据和工具结果，并控制顺序与预算。这里负责当轮信息的选择与组织；跨时间信息的写入、纠错、遗忘和删除由 Memory 管理。

[← 知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)

**先修**：[LLM 模型](03%20LLM%20%E4%B8%8E%E6%A8%A1%E5%9E%8B%E6%8E%A5%E5%85%A5.md) · [Prompt 指令](05%20Prompt%20%E5%B7%A5%E7%A8%8B.md)

## 学习大纲

- 消息组织、相关性选择、Token 预算、裁剪、摘要、压缩、按需加载、工具结果截取；指令与不可信内容的分区；长对话信息漂移。
- **设计产物**：上下文组装器，从 Prompt、历史、Memory、RAG 和工具结果中选取信息；重要事实保留来源，压缩后能回到原始证据。
- [ ] **06 验收**：长文档、超长工具结果、长会话都不会无限堆入模型；能解释每类内容为何入选、占多少预算。

参考：[Anthropic 上下文工程](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)。

## 相关专题

- [Prompt 指令](05%20Prompt%20%E5%B7%A5%E7%A8%8B.md)：指令是上下文的一部分。
- [Memory 记忆](07%20Memory%20%E8%AE%B0%E5%BF%86%E7%B3%BB%E7%BB%9F.md)：记忆按需进入本轮上下文。
- [RAG 检索增强](09%20Retrieval%20%E4%B8%8E%20RAG.md)：检索证据按预算进入上下文。
- [Agent 运行闭环](10%20Agent%20%E6%89%A7%E8%A1%8C%E5%BE%AA%E7%8E%AF%E4%B8%8E%E8%BF%90%E8%A1%8C%E6%9C%BA%E5%88%B6.md)：运行时逐轮组装上下文。

> [!note]- 我的学习记录
> - **用自己的话解释**：
> - **最小实验与结果**：
> - **失败案例与定位**：
> - **还没弄懂的问题**：

| [← 上一章 · 05 Prompt 工程](05%20Prompt%20%E5%B7%A5%E7%A8%8B.md) | [下一章 · 07 Memory 记忆系统 →](07%20Memory%20%E8%AE%B0%E5%BF%86%E7%B3%BB%E7%BB%9F.md) |
| :--- | ---: |
