---
type: "concept"
tags:
  - "agent"
  - "agent/concept"
status: "待学习"
created: "2026-10-30"
updated: "2026-10-30"
aliases:
  - "Agent 执行循环与运行机制"
  - "Agent Loop 与 Harness"
  - "10 Agent Loop 与 Harness"
  - "Agent 运行闭环"
  - "Agent"
  - "Harness"
  - "Runtime"
module: "10"
domain: "C 决策与编排"
level: "必学"
learning_layer: "应用主线"
directions:
  - "应用开发"
  - "平台工程"
---

# 10 Agent 执行循环与运行机制

把模型判断组织成可执行、可停止、可观测的决策—行动—观察闭环，并理解运行生命周期及能力组合如何承载它。明确的依赖和固定转移可交给 Workflow；Loop、Runtime、Harness 的名称与范围随框架而异，应对照实际职责理解。

[← 知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)

**先修**：[LLM 模型](03%20LLM%20%E4%B8%8E%E6%A8%A1%E5%9E%8B%E6%8E%A5%E5%85%A5.md) · [结构化输出](04%20%E7%BB%93%E6%9E%84%E5%8C%96%E8%BE%93%E5%85%A5%E8%BE%93%E5%87%BA.md) · [Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md)

## 学习大纲

### 先分清三个层次

| 层次 | 学习重点 | 与相邻模块的分工 |
| --- | --- | --- |
| Agent Loop | 模型决策 → 动作请求 → 校验执行 → 观察结果 → 更新状态 → 再决策；何时结束 | 决定下一步；固定依赖、路由与转移见 [Workflow 编排](11%20%E8%A7%84%E5%88%92%E3%80%81%E8%B7%AF%E7%94%B1%E4%B8%8E%20Workflow.md) |
| Runtime | 执行生命周期、事件与流式输出、取消、状态保存、暂停与恢复 | 运行承载机制；检查点与恢复语义见 [状态与恢复](12%20%E7%8A%B6%E6%80%81%E3%80%81%E6%8C%81%E4%B9%85%E6%89%A7%E8%A1%8C%E4%B8%8E%E6%81%A2%E5%A4%8D.md) |
| Harness | 为实际任务组合模型、上下文、工具、规划、子 Agent、运行约束等能力的应用骨架 | 组合与约束各模块；上下文策略见 [Context 上下文](06%20Context%20Engineering.md)，工具契约见 [Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md) |

> [!note] 读框架文档时先核对术语
> LangChain 将 Runtime、Framework、Harness 分层说明；Microsoft 的 Harness 则组合既有 Agent Framework 能力，并非另外引入一套独立 Runtime。因此不要把这三个词当作所有框架统一的组件规范，应对照实际职责和调用链。

### 执行管线：Middleware、Hooks 与 Callbacks

- **介入位置**：运行开始／结束、模型调用前后、工具执行前后；各框架支持的钩子与修改权限不同。
- **典型用途**：动态选择模型、上下文裁剪、策略检查、限流与预算、重试与回退、Trace 和指标。区分能够修改／阻断执行的中间件，与主要接收事件的回调。
- **工程约束**：明确执行顺序、异常传播、取消和重复触发语义；记录原始请求与干预原因。管线里的策略检查仍需实际执行端的授权校验，沙箱隔离由运行环境提供。

### 闭环设计与最小实验

- 终止条件；步数／时间／Token／费用预算；重复动作检测；工具失败、用户取消与完成状态；区分模型能力、框架机制和应用策略。
- **设计产物**：画出一次运行的调用链，标注每个介入点、状态归属与预算检查位置；明确 Context、Tools、状态恢复、权限与观测的接口，不在 Harness 中重复实现它们。
- **实践**：在两个只读工具的最小 Agent 上加入调用日志和模型调用次数上限；制造工具连续失败，观察中间件、Loop 停止条件和返回结果各自如何工作。
- [ ] **10 验收**：实现单 Agent 加两个只读工具；正常任务能结束，反复失败或循环调用时能停止并说明原因。
- [ ] **补充自测**：能指出“模型决定下一步”“进程重启后恢复”“调用前插入策略检查”分别由哪些机制负责，并用所选框架的实际 API 解释边界。

参考：[LangChain 产品与分层概念](https://docs.langchain.com/oss/python/concepts/products)、[Microsoft Agent Framework Harness](https://learn.microsoft.com/en-us/agent-framework/concepts/harness)、[LangChain Middleware](https://docs.langchain.com/oss/python/langchain/middleware/overview)、[Anthropic 构建有效 Agent](https://www.anthropic.com/research/building-effective-agents)。

## 一次运行的全景

**Agent 应用可以理解为：围绕任务目标，把模型、上下文、工具、状态和执行约束组织起来，持续选择行动、观察结果并判断何时结束的应用系统。** ^agent-definition

Skills 提供可复用流程，Memory 管理跨时间信息，RAG 提供检索证据；可按任务组合或省略。

```text
目标与输入 → 上下文 → LLM 判断 → 回答或选择行动
行动请求 → 参数与权限校验 → 按需审批 → Tool → 外部系统或沙箱
执行结果 → 更新状态 → 检查预算与完成条件 → 继续或结束

支持：Prompt · Skills · Memory · RAG · 状态持久化
贯穿：权限 · 安全 · 人工控制 · 评测 · 观测 · 成本 · 版本
```

> [!example]- 可视化流程图（Mermaid）
> 若知识库首次显示 Mermaid 需要信任确认，可以先使用上方文字结构图。此处保留可维护的图表定义。
> 
> ```mermaid
> flowchart TB
>     U[用户目标与成功标准] --> R[应用入口与 Agent 运行时]
>     R --> C[组装上下文]
>     P[Prompt 与 Skills] --> C
>     M[历史与 Memory] --> C
>     K[知识库与 RAG] --> C
>     C --> L[LLM 判断下一步]
>     L --> D{结束或行动}
>     D -->|回答| O[展示结果与证据]
>     D -->|调用工具| V[参数校验与权限检查]
>     V --> H{是否需要人工确认}
>     H -->|需要| A[预览与审批]
>     H -->|无需| T[执行 Tool]
>     A -->|批准| T
>     A -->|拒绝或修改| S[更新运行状态]
>     T --> X[外部 API 或 Sandbox]
>     X --> S
>     S --> B{步数 时间 费用上限}
>     B -->|未达到| C
>     B -->|达到上限| O
>     E[评测 可观测性 成本与版本治理] -.覆盖全链路.-> R
> ```

### 一次运行里有哪些不同的数据

| 对象 | 典型内容 | 谁负责管理 |
| --- | --- | --- |
| 模型输入上下文 | 本轮指令、消息、证据、工具结果 | Context 组装层 |
| 会话与运行状态 | 当前步骤、待执行动作、检查点、审批状态 | Agent Runtime 与状态库 |
| 长期记忆 | 用户偏好、经确认的事实、历史经验 | Memory 生命周期管理 |
| 知识资料 | 文档、表格、索引、版本、访问权限 | 数据管道与检索系统 |
| 业务真实状态 | 工单是否创建、订单是否提交 | 业务系统；以回执或查询结果为准 |
| 工作区与产物 | 临时文件、报告、图片、补丁及版本引用 | 后端产物服务与受控执行环境，见 [产物管理](25%20%E5%90%8E%E7%AB%AF%E3%80%81%E8%B0%83%E5%BA%A6%E4%B8%8E%E5%AD%98%E5%82%A8.md#%E5%B7%A5%E4%BD%9C%E5%8C%BA%E4%B8%8E%E4%BA%A7%E7%89%A9%E7%94%9F%E5%91%BD%E5%91%A8%E6%9C%9F) |
| 观测数据 | Trace、调用耗时、费用、错误 | 可观测性系统 |

## 相关专题

- [Coding Agent](33%20Coding%20Agent%20%E4%B8%8E%E8%BD%AF%E4%BB%B6%E5%B7%A5%E7%A8%8B%E4%BB%BB%E5%8A%A1.md)：把通用执行机制组合成代码调查、修改与验证流程。
- [目标与自治边界](01%20%E4%BA%A7%E5%93%81%E7%9B%AE%E6%A0%87%E4%B8%8E%E8%87%AA%E6%B2%BB%E8%BE%B9%E7%95%8C.md)：目标与完成标准约束运行闭环。
- [LLM 模型](03%20LLM%20%E4%B8%8E%E6%A8%A1%E5%9E%8B%E6%8E%A5%E5%85%A5.md)：模型提供闭环中的决策能力。
- [Context 上下文](06%20Context%20Engineering.md)：运行时逐轮组装上下文。
- [Workflow 编排](11%20%E8%A7%84%E5%88%92%E3%80%81%E8%B7%AF%E7%94%B1%E4%B8%8E%20Workflow.md)：动态闭环与确定性流程组合。
- [状态与恢复](12%20%E7%8A%B6%E6%80%81%E3%80%81%E6%8C%81%E4%B9%85%E6%89%A7%E8%A1%8C%E4%B8%8E%E6%81%A2%E5%A4%8D.md)：未完成运行需要保存与恢复。
- [Tools 工具](14%20Tools%20%E4%B8%8E%20Function%20Calling.md)：工具将决策转化为动作。

> [!note]- 我的学习记录
> - **用自己的话解释**：
> - **最小实验与结果**：
> - **失败案例与定位**：
> - **还没弄懂的问题**：

| [← 上一章 · 09 Retrieval 与 RAG](09%20Retrieval%20%E4%B8%8E%20RAG.md) | [下一章 · 11 规划、路由与 Workflow →](11%20%E8%A7%84%E5%88%92%E3%80%81%E8%B7%AF%E7%94%B1%E4%B8%8E%20Workflow.md) |
| :--- | ---: |
