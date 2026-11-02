---
type: "concept"
tags:
  - "agent"
  - "agent/concept"
status: "待学习"
created: "2026-10-30"
updated: "2026-10-30"
aliases:
  - "MCP 能力连接协议"
  - "MCP 协议"
  - "MCP"
module: "16"
domain: "D 能力与执行环境"
level: "按需"
learning_layer: "按需深入"
directions:
  - "应用开发"
  - "平台工程"
---

# 16 MCP 能力连接协议

通过共同协议发现和使用外部工具、资源与上下文，理解连接角色、能力交换及认证边界。MCP 统一交换方式；具体动作的业务正确性和外部接口适配，仍由工具与集成实现负责。

[← 知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)

**先修**：[Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md) · [Connectors 集成](17%20Connectors%20%E4%B8%8E%E4%B8%9A%E5%8A%A1%E9%9B%86%E6%88%90.md)

## 学习大纲

- Host、Client、Server；Tools、Resources、Prompts；JSON-RPC、stdio 与 Streamable HTTP；能力发现、版本兼容、认证授权、错误、取消与连接故障。
- **设计产物**：一个可检查工具清单与权限的 MCP 集成；明确 Server 是本地进程还是远程服务，哪些数据会传输，凭据由谁持有。
- [ ] **16 验收**：能发现并调用能力；工具失效、权限不足、服务断开时有明确反馈；实现前锁定协议与 SDK 版本并核对对应文档。

参考：[MCP 官方架构](https://modelcontextprotocol.io/docs/learn/architecture)。协议会演进；学习时不要混用不同版本的握手、发现和授权细节。

## 相关专题

- [Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md)：MCP 可暴露标准化工具接口。
- [Connectors 集成](17%20Connectors%20%E4%B8%8E%E4%B8%9A%E5%8A%A1%E9%9B%86%E6%88%90.md)：协议与具体业务集成各有职责。
- [身份与权限](20%20%E8%BA%AB%E4%BB%BD%E3%80%81%E6%9D%83%E9%99%90%E4%B8%8E%E5%A4%9A%E7%A7%9F%E6%88%B7.md)：连接能力不等于授予访问权限。

> [!note]- 我的学习记录
> - **用自己的话解释**：
> - **最小实验与结果**：
> - **失败案例与定位**：
> - **还没弄懂的问题**：

| [← 上一章 · 15 Skills 可复用技能](15%20Skills%20%E5%8F%AF%E5%A4%8D%E7%94%A8%E6%8A%80%E8%83%BD.md) | [下一章 · 17 Connectors 与业务集成 →](17%20Connectors%20%E4%B8%8E%E4%B8%9A%E5%8A%A1%E9%9B%86%E6%88%90.md) |
| :--- | ---: |
