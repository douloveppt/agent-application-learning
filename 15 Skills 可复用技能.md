---
type: "concept"
tags:
  - "agent"
  - "agent/concept"
status: "待学习"
created: "2026-10-30"
updated: "2026-10-30"
aliases:
  - "Skills 可复用技能"
  - "Skills 技能"
  - "Skills"
module: "15"
domain: "D 能力与执行环境"
level: "按需"
learning_layer: "按需深入"
directions:
  - "应用开发"
---

# 15 Skills 可复用技能

将某类任务的专业知识、步骤、参考资料与脚本组织成可复用技能，并管理触发、加载、版本和测试。技能可以指导多个工具，但技能中的文字不会自动增加数据访问或执行权限。

[← 知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)

**先修**：[Prompt 指令](05%20Prompt%20%E5%B7%A5%E7%A8%8B.md) · [Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md)

## 学习大纲

- 触发描述、任务边界、流程、输入输出、参考资料、脚本与模板；发现、按需加载、依赖、版本、测试与发布；识别互相冲突的指令。
- **设计产物**：若采用 Agent Skills 格式，建立以 `SKILL.md` 为核心的技能目录，按需包含 `scripts/`、`references/`、`assets/`；实际加载和执行由宿主实现。
- [ ] **15 验收**：同一技能能处理正常、缺输入、工具不可用三类任务；不会因技能文字而获得额外数据访问或执行权限。

参考：[Agent Skills 格式与渐进加载](https://agentskills.io/home)。

## 相关专题

- [Prompt 指令](05%20Prompt%20%E5%B7%A5%E7%A8%8B.md)：技能可以封装任务指令与流程。
- [Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md)：技能组织工具的使用流程。

> [!note]- 我的学习记录
> - **用自己的话解释**：
> - **最小实验与结果**：
> - **失败案例与定位**：
> - **还没弄懂的问题**：

| [← 上一章 · 14 Tools 与 Function Calling](14%20Tools%20%E4%B8%8E%20Function%20Calling.md) | [下一章 · 16 MCP 能力连接协议 →](16%20MCP%20%E8%83%BD%E5%8A%9B%E8%BF%9E%E6%8E%A5%E5%8D%8F%E8%AE%AE.md) |
| :--- | ---: |
