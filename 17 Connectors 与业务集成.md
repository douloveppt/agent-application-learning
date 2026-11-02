---
type: "concept"
tags:
  - "agent"
  - "agent/concept"
status: "待学习"
created: "2026-10-30"
updated: "2026-10-30"
aliases:
  - "Connectors 与业务集成"
  - "Connectors 集成"
module: "17"
domain: "D 能力与执行环境"
level: "按需"
learning_layer: "按需深入"
directions:
  - "应用开发"
---

# 17 Connectors 与业务集成

需要读写搜索、数据库、工单、代码仓库等业务系统时，负责接口、字段、凭据与回执适配。集成可以直接提供业务能力，也可通过 MCP 暴露；一旦接入实际业务，鉴权和副作用一致性必须同步落实。

[← 知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)

**先修**：[软件与系统基础](02%20%E8%BD%AF%E4%BB%B6%E4%B8%8E%E7%B3%BB%E7%BB%9F%E5%9F%BA%E7%A1%80.md) · [身份与权限](20%20%E8%BA%AB%E4%BB%BD%E3%80%81%E6%9D%83%E9%99%90%E4%B8%8E%E5%A4%9A%E7%A7%9F%E6%88%B7.md)

## 学习大纲

- REST／GraphQL／SQL、Webhook、分页、增量同步、速率限制、访问令牌刷新、字段映射、沙盒账号、外部 API 版本与回执。
- **设计产物**：把供应商接口封装为业务语义清晰的适配器；读写分离；为失败、配额耗尽和字段变化准备契约样例。
- [ ] **17 验收**：分页不会漏数据；外部超时不会自动假定提交失败；凭据过期和接口变化能被监测与处理。

## 相关专题

- [Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md)：工具借适配器访问业务系统。
- [MCP 协议](16%20MCP%20%E8%83%BD%E5%8A%9B%E8%BF%9E%E6%8E%A5%E5%8D%8F%E8%AE%AE.md)：协议与具体业务集成各有职责。

> [!note]- 我的学习记录
> - **用自己的话解释**：
> - **最小实验与结果**：
> - **失败案例与定位**：
> - **还没弄懂的问题**：

| [← 上一章 · 16 MCP 能力连接协议](16%20MCP%20%E8%83%BD%E5%8A%9B%E8%BF%9E%E6%8E%A5%E5%8D%8F%E8%AE%AE.md) | [下一章 · 18 Sandbox 沙箱 →](18%20Sandbox%20%E6%B2%99%E7%AE%B1.md) |
| :--- | ---: |
