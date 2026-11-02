# Agent 应用开发学习图谱

一套面向 Agent 应用开发的中文学习资料，用 **7 个板块、33 个专题**串起模型接入、上下文、工具执行和生产交付。目标是建立清晰的模块边界，并通过实验与验收，逐步做出可评测、可恢复、可交付的应用。

适合已有编程基础、准备系统学习 Agent 开发、梳理项目架构或准备相关岗位面试的开发者。每篇专题包含核心要点、实践产物、验收任务与关联阅读；软件基础可查缺补漏，RAG、多 Agent、自动化、实时语音和模型适配按方向深入。

## 从这里开始

- **安排学习先后**：[学习路线与实战](./90%20%E5%AD%A6%E4%B9%A0%E8%B7%AF%E7%BA%BF%E4%B8%8E%E5%AE%9E%E6%88%98.md)，选择应用主线或岗位方向。
- **直接查知识**：从下方章节目录进入，或用 [概念辨析与自测](./91%20%E6%A6%82%E5%BF%B5%E8%BE%A8%E6%9E%90%E4%B8%8E%E8%87%AA%E6%B5%8B.md) 检查薄弱点。
- **查看进度和资料**：[学习工作台与资料](./92%20%E5%AD%A6%E4%B9%A0%E5%B7%A5%E4%BD%9C%E5%8F%B0%E4%B8%8E%E8%B5%84%E6%96%99.md)，包含验收查询、分类依据和官方来源。
- **动手练习**：[第二章 HTTP、SQLite 与异步实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch02-system-basics)，通过实际请求和失败案例理解系统基础。
- **校验模型候选**：[第四章结构化输入输出实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch04-structured-io)，离线观察严格解析、字段约束、跨字段规则与来源核验。
- **维护任务指令**：[第五章 Prompt 工程实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch05-prompt-engineering)，验证请求装配、版本摘要和人工回放中的改善与退化。
- **组装本轮上下文**：[第六章 Context Engineering 实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch06-context-engineering)，观察权限与时效过滤、必需项保留、原子分组和字节预算选择。
- **管理跨会话记忆**：[第七章 Memory 实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch07-memory)，观察幂等写入、版本纠正、用户隔离、到期失效与删除后的旧索引拒绝。
- **取得合格证据**：[第九章 Retrieval 与 RAG 实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch09-retrieval-rag)，观察 BM25、排名融合、权限与版本复核、完整证据组和引用编号校验。
- **控制执行闭环**：[第十章 Agent Loop 实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch10-agent-loop)，观察动作准入、工具配对、独立预算、无进展停止和完成核验的能力边界。
- **编排多步任务**：[第十一章 Workflow 实验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch11-workflow)，观察路由准入、DAG 校验、依赖就绪、有限重试、失败传播与只读结果复用。

## 两种阅读方式

- **在 GitHub 阅读**：点击下方专题名称进入文档；章节之间可通过页尾的上一章、下一章继续阅读。
- **在 Obsidian 学习**：克隆或下载仓库，将仓库目录作为知识库打开，再打开知识图谱。可使用 Canvas 导航、反向链接、局部关系图、Bases 进度表和折叠学习记录。

[打开知识图谱文件](./00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)（交互图谱需在 Obsidian 中查看）。仓库已附带图谱入口样式和章节导航，无需安装第三方插件。

## 章节目录

编号用于稳定定位；阅读顺序按板块组织，因此 Coding Agent（33）接在浏览器与桌面自动化（19）之后。

### A · 目标与模型基础

| 专题 | 重点 |
| --- | --- |
| [01 产品目标与自治边界](./01%20%E4%BA%A7%E5%93%81%E7%9B%AE%E6%A0%87%E4%B8%8E%E8%87%AA%E6%B2%BB%E8%BE%B9%E7%95%8C.md) | 任务目标、自治范围与完成标准 |
| [02 软件与系统基础](./02%20%E8%BD%AF%E4%BB%B6%E4%B8%8E%E7%B3%BB%E7%BB%9F%E5%9F%BA%E7%A1%80.md) | 编程、接口、状态与并发先修 |
| [03 LLM 与模型接入](./03%20LLM%20%E4%B8%8E%E6%A8%A1%E5%9E%8B%E6%8E%A5%E5%85%A5.md) | LLM 原理、多模态与选型、适配器／网关、成本核算 |
| [04 结构化输入输出](./04%20%E7%BB%93%E6%9E%84%E5%8C%96%E8%BE%93%E5%85%A5%E8%BE%93%E5%87%BA.md) | 输入信任边界、契约设计、分层校验、失败处理与离线实验 |

### B · 指令、上下文与知识

| 专题 | 重点 |
| --- | --- |
| [05 Prompt 工程](./05%20Prompt%20%E5%B7%A5%E7%A8%8B.md) | 任务合同、指令与示例、模板工程、配对评测与发布回滚 |
| [06 Context Engineering](./06%20Context%20Engineering.md) | 来源与权限、完整请求预算、选择与压缩、证据血缘及失效传播 |
| [07 Memory 记忆系统](./07%20Memory%20%E8%AE%B0%E5%BF%86%E7%B3%BB%E7%BB%9F.md) | 记忆准入、来源与范围、并发纠正、检索复核和删除防复活 |
| [08 数据接入与知识加工](./08%20%E6%95%B0%E6%8D%AE%E6%8E%A5%E5%85%A5%E4%B8%8E%E7%9F%A5%E8%AF%86%E5%8A%A0%E5%B7%A5.md) | 多格式解析、保真分块、增量加工、制品发布与删除同步 |
| [09 Retrieval 与 RAG](./09%20Retrieval%20%E4%B8%8E%20RAG.md) | 全文与向量、混合召回、权限版本复核、证据引用与分层评测 |

### C · 决策与编排

| 专题 | 重点 |
| --- | --- |
| [10 Agent 执行循环与运行机制](./10%20Agent%20%E6%89%A7%E8%A1%8C%E5%BE%AA%E7%8E%AF%E4%B8%8E%E8%BF%90%E8%A1%8C%E6%9C%BA%E5%88%B6.md) | 动作准入、执行观察、预算停止、生命周期、完成核验与离线实验 |
| [11 规划、路由与 Workflow](./11%20%E8%A7%84%E5%88%92%E3%80%81%E8%B7%AF%E7%94%B1%E4%B8%8E%20Workflow.md) | 路由准入、计划合同、DAG 调度、分支汇合、失败传播与有限重规划 |
| [12 状态、持久执行与恢复](./12%20%E7%8A%B6%E6%80%81%E3%80%81%E6%8C%81%E4%B9%85%E6%89%A7%E8%A1%8C%E4%B8%8E%E6%81%A2%E5%A4%8D.md) | 检查点、暂停和持久恢复 |
| [13 Multi-Agent 协作](./13%20Multi-Agent%20%E5%8D%8F%E4%BD%9C.md) | 职责协作与 A2A 互操作支线 |

### D · 能力与执行环境

| 专题 | 重点 |
| --- | --- |
| [14 Tools 与 Function Calling](./14%20Tools%20%E4%B8%8E%20Function%20Calling.md) | 工具契约、调用与执行结果 |
| [15 Skills 可复用技能](./15%20Skills%20%E5%8F%AF%E5%A4%8D%E7%94%A8%E6%8A%80%E8%83%BD.md) | 可复用任务知识、流程与资源 |
| [16 MCP 能力连接协议](./16%20MCP%20%E8%83%BD%E5%8A%9B%E8%BF%9E%E6%8E%A5%E5%8D%8F%E8%AE%AE.md) | 能力发现、连接与协议边界 |
| [17 Connectors 与业务集成](./17%20Connectors%20%E4%B8%8E%E4%B8%9A%E5%8A%A1%E9%9B%86%E6%88%90.md) | 企业接口、字段、鉴权与回执 |
| [18 Sandbox 沙箱](./18%20Sandbox%20%E6%B2%99%E7%AE%B1.md) | 文件、网络、进程与资源隔离 |
| [19 浏览器与桌面自动化](./19%20%E6%B5%8F%E8%A7%88%E5%99%A8%E4%B8%8E%E6%A1%8C%E9%9D%A2%E8%87%AA%E5%8A%A8%E5%8C%96.md) | 网页和桌面中的观察、行动与核验 |
| [33 Coding Agent 与软件工程任务](./33%20Coding%20Agent%20%E4%B8%8E%E8%BD%AF%E4%BB%B6%E5%B7%A5%E7%A8%8B%E4%BB%BB%E5%8A%A1.md) | 仓库理解、代码修改、测试与交付 |

### E · 身份、安全与人工控制

| 专题 | 重点 |
| --- | --- |
| [20 身份、权限与多租户](./20%20%E8%BA%AB%E4%BB%BD%E3%80%81%E6%9D%83%E9%99%90%E4%B8%8E%E5%A4%9A%E7%A7%9F%E6%88%B7.md) | 身份、委托、授权与租户隔离 |
| [21 安全与 Guardrails](./21%20%E5%AE%89%E5%85%A8%E4%B8%8E%20Guardrails.md) | 信任边界、注入防护与行为约束 |
| [22 Human-in-the-loop](./22%20Human-in-the-loop.md) | 澄清、预览、审批、接管与恢复 |

### F · 产品与服务工程

| 专题 | 重点 |
| --- | --- |
| [23 交互体验与流式事件](./23%20%E4%BA%A4%E4%BA%92%E4%BD%93%E9%AA%8C%E4%B8%8E%E6%B5%81%E5%BC%8F%E4%BA%8B%E4%BB%B6.md) | 真实进度、流式事件与取消体验 |
| [24 多模态与实时语音](./24%20%E5%A4%9A%E6%A8%A1%E6%80%81%E4%B8%8E%E5%AE%9E%E6%97%B6%E8%AF%AD%E9%9F%B3.md) | 通用多模态与实时语音两条路线 |
| [25 后端、调度与存储](./25%20%E5%90%8E%E7%AB%AF%E3%80%81%E8%B0%83%E5%BA%A6%E4%B8%8E%E5%AD%98%E5%82%A8.md) | 任务服务、调度、存储与产物生命周期 |
| [26 可靠性与副作用一致性](./26%20%E5%8F%AF%E9%9D%A0%E6%80%A7%E4%B8%8E%E5%89%AF%E4%BD%9C%E7%94%A8%E4%B8%80%E8%87%B4%E6%80%A7.md) | 幂等、未知结果、对账与补偿 |

### G · 质量、交付与持续改进

| 专题 | 重点 |
| --- | --- |
| [27 测试、Evals 与质量门禁](./27%20%E6%B5%8B%E8%AF%95%E3%80%81Evals%20%E4%B8%8E%E8%B4%A8%E9%87%8F%E9%97%A8%E7%A6%81.md) | 契约测试、效果评测与最终状态核验 |
| [28 可观测性与调试](./28%20%E5%8F%AF%E8%A7%82%E6%B5%8B%E6%80%A7%E4%B8%8E%E8%B0%83%E8%AF%95.md) | Trace、日志、指标与失败定位 |
| [29 性能、成本与模型路由](./29%20%E6%80%A7%E8%83%BD%E3%80%81%E6%88%90%E6%9C%AC%E4%B8%8E%E6%A8%A1%E5%9E%8B%E8%B7%AF%E7%94%B1.md) | 延迟、吞吐、费用与模型路由 |
| [30 部署、版本与 LLMOps](./30%20%E9%83%A8%E7%BD%B2%E3%80%81%E7%89%88%E6%9C%AC%E4%B8%8E%20LLMOps.md) | 版本、灰度、部署与回滚 |
| [31 微调、蒸馏与模型适配](./31%20%E5%BE%AE%E8%B0%83%E3%80%81%E8%92%B8%E9%A6%8F%E4%B8%8E%E6%A8%A1%E5%9E%8B%E9%80%82%E9%85%8D.md) | 微调、蒸馏及模型行为适配 |
| [32 数据治理与持续改进](./32%20%E6%95%B0%E6%8D%AE%E6%B2%BB%E7%90%86%E4%B8%8E%E6%8C%81%E7%BB%AD%E6%94%B9%E8%BF%9B.md) | 数据生命周期、反馈与持续改进 |

## 如何使用这套资料

每次选择一个专题，完成一个最小实验并记录失败案例，再根据验收任务判断是否掌握。学习状态保存在专题属性中；完整学习顺序和贯穿项目统一放在“学习路线与实战”，避免在目录重复展开。

模块划分参考了企业工程实践、活跃开源 Agent 框架及岗位职责样本。具体判断和原始来源集中保存在学习工作台，框架与协议接口以实际采用版本的官方资料为准。

## 更新与发布

[协作与发布说明](https://github.com/douloveppt/agent-application-learning/blob/main/PUBLISHING.md)

每次由 Codex 完成一轮项目修改并验证后，会统一创建提交并推送 GitHub。发布结果会在该轮任务完成时说明。

本地 Obsidian 学习目录保存原稿，公开仓库提供适配 GitHub 的文档、目录和章节链接。发布范围为这套学习资料与项目所需文件；目录外的私人笔记保留在个人知识库中，配置与认证信息保存在本机。

| [← 返回知识图谱](./00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas) | [下一章 · 01 产品目标与自治边界 →](./01%20%E4%BA%A7%E5%93%81%E7%9B%AE%E6%A0%87%E4%B8%8E%E8%87%AA%E6%B2%BB%E8%BE%B9%E7%95%8C.md) |
| :--- | ---: |
