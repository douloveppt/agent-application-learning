---
type: "concept"
tags:
  - "agent"
  - "agent/concept"
status: "待学习"
created: "2026-10-30"
updated: "2026-10-30"
aliases:
  - "Retrieval 与 RAG"
  - "RAG 检索增强"
  - "RAG"
module: "09"
domain: "B 指令、上下文与知识"
level: "知识应用必学"
learning_layer: "按需深入"
directions:
  - "知识工程"
---

# 09 Retrieval 与 RAG

完成资料加工后，从适当来源找到证据，再让模型据此回答并提供引用，这是知识型应用的主线。源资料质量与索引更新由数据管道负责，用户历史偏好由 Memory 管理；普通工具执行型应用不必预先建设向量库。

[← 知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)

**先修**：[Context 上下文](06%20Context%20Engineering.md) · [知识加工](08%20%E6%95%B0%E6%8D%AE%E6%8E%A5%E5%85%A5%E4%B8%8E%E7%9F%A5%E8%AF%86%E5%8A%A0%E5%B7%A5.md)

## 学习大纲

- 全文／BM25、向量、混合检索、结构化查询；Embedding、相似度、索引、元数据过滤；查询改写、分解、多路召回、融合、Rerank、上下文补全和引用。
- **进阶分支**：SQL 问答、表格行检索、多模态检索、知识图谱／GraphRAG、Agentic RAG。先验证基础检索，再按失败类型增加复杂度。
- **设计产物**：检索与生成分开评测；权限在数据访问链路落实；更换 Embedding 时评估重建索引；无可靠证据时澄清或说明不足。
- [ ] **09 验收**：回答能定位证据；问到资料外内容不会编造出处；分别给出召回质量与答案质量结果。

参考：[LlamaIndex RAG 介绍](https://developers.llamaindex.ai/python/framework/understanding/rag/)。

## 相关专题

- [Context 上下文](06%20Context%20Engineering.md)：检索证据按预算进入上下文。
- [知识加工](08%20%E6%95%B0%E6%8D%AE%E6%8E%A5%E5%85%A5%E4%B8%8E%E7%9F%A5%E8%AF%86%E5%8A%A0%E5%B7%A5.md)：加工质量决定可检索证据质量。
- [Evals 评测](27%20%E6%B5%8B%E8%AF%95%E3%80%81Evals%20%E4%B8%8E%E8%B4%A8%E9%87%8F%E9%97%A8%E7%A6%81.md)：召回和生成分别评测。

> [!note]- 我的学习记录
> - **用自己的话解释**：
> - **最小实验与结果**：
> - **失败案例与定位**：
> - **还没弄懂的问题**：

| [← 上一章 · 08 数据接入与知识加工](08%20%E6%95%B0%E6%8D%AE%E6%8E%A5%E5%85%A5%E4%B8%8E%E7%9F%A5%E8%AF%86%E5%8A%A0%E5%B7%A5.md) | [下一章 · 10 Agent 执行循环与运行机制 →](10%20Agent%20%E6%89%A7%E8%A1%8C%E5%BE%AA%E7%8E%AF%E4%B8%8E%E8%BF%90%E8%A1%8C%E6%9C%BA%E5%88%B6.md) |
| :--- | ---: |
