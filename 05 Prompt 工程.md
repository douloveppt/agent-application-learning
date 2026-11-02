---
type: "concept"
tags:
  - "agent"
  - "agent/concept"
status: "待学习"
created: "2026-10-30"
updated: "2026-10-30"
aliases:
  - "Prompt 工程"
  - "Prompt 指令"
  - "Prompt"
module: "05"
domain: "B 指令、上下文与知识"
level: "必学"
learning_layer: "应用主线"
directions:
  - "应用开发"
---

# 05 Prompt 工程

把任务、约束、示例和期望结果表达为清晰、可维护的指令，并通过回归样例验证修改效果。完整输入中各类信息的选择、排序、裁剪与压缩，由 Context Engineering 负责。

[← 知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)

**先修**：[目标与自治边界](01%20%E4%BA%A7%E5%93%81%E7%9B%AE%E6%A0%87%E4%B8%8E%E8%87%AA%E6%B2%BB%E8%BE%B9%E7%95%8C.md) · [LLM 模型](03%20LLM%20%E4%B8%8E%E6%A8%A1%E5%9E%8B%E6%8E%A5%E5%85%A5.md)

## 学习大纲

- 指令优先级、角色、任务描述、成功标准、Few-shot、反例、边界条件、输出契约；模板变量与转义；提示词版本和回归样例。
- **设计产物**：一个模板拆成目标、约束、输入、示例、输出五部分；外部资料有明确来源与边界；请求必要的简明依据或证据，评测时检查可观察结果。
- [ ] **05 验收**：修改 Prompt 前后运行同一测试集，能指出改进了哪些案例、是否损害其他案例。

## 相关专题

- [LLM 模型](03%20LLM%20%E4%B8%8E%E6%A8%A1%E5%9E%8B%E6%8E%A5%E5%85%A5.md)：模型能力影响指令设计。
- [Context 上下文](06%20Context%20Engineering.md)：指令是上下文的一部分。
- [Skills 技能](15%20Skills%20%E5%8F%AF%E5%A4%8D%E7%94%A8%E6%8A%80%E8%83%BD.md)：技能可以封装任务指令与流程。

> [!note]- 我的学习记录
> - **用自己的话解释**：
> - **最小实验与结果**：
> - **失败案例与定位**：
> - **还没弄懂的问题**：

| [← 上一章 · 04 结构化输入输出](04%20%E7%BB%93%E6%9E%84%E5%8C%96%E8%BE%93%E5%85%A5%E8%BE%93%E5%87%BA.md) | [下一章 · 06 Context Engineering →](06%20Context%20Engineering.md) |
| :--- | ---: |
