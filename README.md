# Agent 应用开发学习图谱

面向开发者的中文 Agent 应用开发学习资料，包含 **7 个板块、33 个专题、31 个配套实验**，覆盖从模型接入、知识检索和工具执行，到持久恢复、评测与生产交付的完整链路。

适合已有编程基础，希望系统学习 Agent 开发、梳理项目架构或准备相关岗位面试的读者。以**企业知识与工单助手**为贯穿案例，逐步练习只读查询、可信问答、审批写入和可恢复任务，并为每一步保留可复核的结果。

## 从这里开始

| 你现在想做什么 | 入口 | 先完成什么 |
| --- | --- | --- |
| 系统学习，安排先后顺序 | [学习路线与实战](90%20%E5%AD%A6%E4%B9%A0%E8%B7%AF%E7%BA%BF%E4%B8%8E%E5%AE%9E%E6%88%98.md) | 判断起点，选择当前阶段与一个项目任务 |
| 先运行一个例子 | [运行第一个实验](#运行第一个实验) | 观察工具调用、预算停止与结果核验 |
| 查某个模块，解决具体问题 | [章节目录](#章节目录) | 找到对应专题，进入正文与配套实验 |
| 准备面试或检查理解 | [概念辨析与自测](91%20%E6%A6%82%E5%BF%B5%E8%BE%A8%E6%9E%90%E4%B8%8E%E8%87%AA%E6%B5%8B.md) | 用情境题找出解释与证据的缺口 |
| 继续上次学习，查官方资料 | [学习工作台与资料](92%20%E5%AD%A6%E4%B9%A0%E5%B7%A5%E4%BD%9C%E5%8F%B0%E4%B8%8E%E8%B5%84%E6%96%99.md) | 找回下一步，更新状态与验收记录 |

每轮选一个问题：**读机制 → 跑实验 → 改一个条件或注入失败 → 对照验收 → 保存证据**。完整项目阶段与方向选择见学习路线页。

## 章节目录

按板块查阅，每章可从页尾继续阅读。编号用于稳定定位，Coding Agent（33）归入 D 板块，导航按 `19 → 33 → 20` 衔接。实践入口覆盖全部 31 个实验；第 01、03 章的练习随正文展开。

### A · 目标与模型基础

| 专题 | 核心问题 | 实践入口 |
| --- | --- | --- |
| [01 产品目标与自治边界](01%20%E4%BA%A7%E5%93%81%E7%9B%AE%E6%A0%87%E4%B8%8E%E8%87%AA%E6%B2%BB%E8%BE%B9%E7%95%8C.md) | 确定支持的任务、自治范围与完成证据 | 章内需求卡与演练 |
| [02 软件与系统基础](02%20%E8%BD%AF%E4%BB%B6%E4%B8%8E%E7%B3%BB%E7%BB%9F%E5%9F%BA%E7%A1%80.md) | 理解请求、校验、事务、异步与并发 | [HTTP / SQLite](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch02-system-basics) |
| [03 LLM 与模型接入](03%20LLM%20%E4%B8%8E%E6%A8%A1%E5%9E%8B%E6%8E%A5%E5%85%A5.md) | 理解模型能力，完成接入与成本核算 | 章内接入与选型练习 |
| [04 结构化输入输出](04%20%E7%BB%93%E6%9E%84%E5%8C%96%E8%BE%93%E5%85%A5%E8%BE%93%E5%87%BA.md) | 校验模型候选的结构、业务规则与来源 | [分层校验](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch04-structured-io) |

### B · 指令、上下文与知识

| 专题 | 核心问题 | 实践入口 |
| --- | --- | --- |
| [05 Prompt 工程](05%20Prompt%20%E5%B7%A5%E7%A8%8B.md) | 设计任务指令，比较版本效果与退化 | [Prompt 迭代](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch05-prompt-engineering) |
| [06 Context Engineering](06%20Context%20Engineering.md) | 按来源、权限与预算组装本轮上下文 | [上下文组装](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch06-context-engineering) |
| [07 Memory 记忆系统](07%20Memory%20%E8%AE%B0%E5%BF%86%E7%B3%BB%E7%BB%9F.md) | 管理跨会话记忆的写入、纠正、失效与删除 | [记忆治理](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch07-memory) |
| [08 数据接入与知识加工](08%20%E6%95%B0%E6%8D%AE%E6%8E%A5%E5%85%A5%E4%B8%8E%E7%9F%A5%E8%AF%86%E5%8A%A0%E5%B7%A5.md) | 解析、分块、增量加工与发布知识资料 | [知识加工](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch08-knowledge-ingestion) |
| [09 Retrieval 与 RAG](09%20Retrieval%20%E4%B8%8E%20RAG.md) | 召回合格证据，生成可追溯的引用 | [检索与引用](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch09-retrieval-rag) |

### C · 决策与编排

| 专题 | 核心问题 | 实践入口 |
| --- | --- | --- |
| [10 Agent 执行循环与运行机制](10%20Agent%20%E6%89%A7%E8%A1%8C%E5%BE%AA%E7%8E%AF%E4%B8%8E%E8%BF%90%E8%A1%8C%E6%9C%BA%E5%88%B6.md) | 控制动作准入、执行观察、预算与结束 | [执行循环](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch10-agent-loop) |
| [11 规划、路由与 Workflow](11%20%E8%A7%84%E5%88%92%E3%80%81%E8%B7%AF%E7%94%B1%E4%B8%8E%20Workflow.md) | 组织路由、DAG、依赖与失败传播 | [任务编排](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch11-workflow) |
| [12 状态、持久执行与恢复](12%20%E7%8A%B6%E6%80%81%E3%80%81%E6%8C%81%E4%B9%85%E6%89%A7%E8%A1%8C%E4%B8%8E%E6%81%A2%E5%A4%8D.md) | 在中断、等待与回执丢失后接续任务 | [持久恢复](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch12-durable-execution) |
| [13 Multi-Agent 协作](13%20Multi-Agent%20%E5%8D%8F%E4%BD%9C.md) | 约束委派，校验交接结果并归并证据 | [多 Agent 协作](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch13-multi-agent) |

### D · 能力与执行环境

| 专题 | 核心问题 | 实践入口 |
| --- | --- | --- |
| [14 Tools 与 Function Calling](14%20Tools%20%E4%B8%8E%20Function%20Calling.md) | 执行工具契约、逐次鉴权与业务回执核验 | [工具调用](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch14-tool-calling) |
| [15 Skills 可复用技能](15%20Skills%20%E5%8F%AF%E5%A4%8D%E7%94%A8%E6%8A%80%E8%83%BD.md) | 发现并按需加载专业任务方法与资源 | [技能加载](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch15-skills) |
| [16 MCP 能力连接协议](16%20MCP%20%E8%83%BD%E5%8A%9B%E8%BF%9E%E6%8E%A5%E5%8D%8F%E8%AE%AE.md) | 理解协议角色、能力发现与连接生命周期 | [MCP 连接](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch16-mcp) |
| [17 Connectors 与业务集成](17%20Connectors%20%E4%B8%8E%E4%B8%9A%E5%8A%A1%E9%9B%86%E6%88%90.md) | 连接业务系统，处理同步、写入与结果核实 | [业务集成](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch17-connectors) |
| [18 Sandbox 沙箱](18%20Sandbox%20%E6%B2%99%E7%AE%B1.md) | 限制执行环境，管理停止、回收与产物交付 | [沙箱控制](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch18-sandbox) |
| [19 浏览器与桌面自动化](19%20%E6%B5%8F%E8%A7%88%E5%99%A8%E4%B8%8E%E6%A1%8C%E9%9D%A2%E8%87%AA%E5%8A%A8%E5%8C%96.md) | 通过界面完成操作，核实身份与业务结果 | [浏览器自动化](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch19-browser-automation) |
| [33 Coding Agent 与软件工程任务](33%20Coding%20Agent%20%E4%B8%8E%E8%BD%AF%E4%BB%B6%E5%B7%A5%E7%A8%8B%E4%BB%BB%E5%8A%A1.md) | 理解代码库，交付有测试证据的可审查补丁 | [代码任务验收](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch33-coding-agent) |

### E · 身份、安全与人工控制

| 专题 | 核心问题 | 实践入口 |
| --- | --- | --- |
| [20 身份、权限与多租户](20%20%E8%BA%AB%E4%BB%BD%E3%80%81%E6%9D%83%E9%99%90%E4%B8%8E%E5%A4%9A%E7%A7%9F%E6%88%B7.md) | 绑定可信身份，落实租户与资源隔离 | [身份与权限](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch20-identity) |
| [21 安全与 Guardrails](21%20%E5%AE%89%E5%85%A8%E4%B8%8E%20Guardrails.md) | 防范提示注入，检查输入、动作与输出边界 | [安全控制](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch21-guardrails) |
| [22 Human-in-the-loop](22%20Human-in-the-loop.md) | 保存准确的人工决定，支持等待、修订与接管 | [审批与恢复](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch22-hitl) |

### F · 产品与服务工程

| 专题 | 核心问题 | 实践入口 |
| --- | --- | --- |
| [23 交互体验与流式事件](23%20%E4%BA%A4%E4%BA%92%E4%BD%93%E9%AA%8C%E4%B8%8E%E6%B5%81%E5%BC%8F%E4%BA%8B%E4%BB%B6.md) | 展示真实进度，处理流式事件、取消与重连 | [流式事件](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch23-streaming-events) |
| [24 多模态与实时语音](24%20%E5%A4%9A%E6%A8%A1%E6%80%81%E4%B8%8E%E5%AE%9E%E6%97%B6%E8%AF%AD%E9%9F%B3.md) | 组织媒体证据，管理实时轮次、打断与设备验收 | [实时交互](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch24-multimodal-voice) |
| [25 后端、调度与存储](25%20%E5%90%8E%E7%AB%AF%E3%80%81%E8%B0%83%E5%BA%A6%E4%B8%8E%E5%AD%98%E5%82%A8.md) | 承载后台任务，管理并发领取、租约与产物 | [后端调度](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch25-backend-scheduling) |
| [26 可靠性与副作用一致性](26%20%E5%8F%AF%E9%9D%A0%E6%80%A7%E4%B8%8E%E5%89%AF%E4%BD%9C%E7%94%A8%E4%B8%80%E8%87%B4%E6%80%A7.md) | 处理幂等、未知结果、对账与补偿 | [副作用一致性](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch26-reliability) |

### G · 质量、交付与持续改进

| 专题 | 核心问题 | 实践入口 |
| --- | --- | --- |
| [27 测试、Evals 与质量门禁](27%20%E6%B5%8B%E8%AF%95%E3%80%81Evals%20%E4%B8%8E%E8%B4%A8%E9%87%8F%E9%97%A8%E7%A6%81.md) | 设计可信测试、效果评测与发布门槛 | [Evals 门禁](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch27-evals) |
| [28 可观测性与调试](28%20%E5%8F%AF%E8%A7%82%E6%B5%8B%E6%80%A7%E4%B8%8E%E8%B0%83%E8%AF%95.md) | 关联运行与业务事实，定位耗时和失败 | [观测与诊断](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch28-observability) |
| [29 性能、成本与模型路由](29%20%E6%80%A7%E8%83%BD%E3%80%81%E6%88%90%E6%9C%AC%E4%B8%8E%E6%A8%A1%E5%9E%8B%E8%B7%AF%E7%94%B1.md) | 在质量约束下管理延迟、吞吐、费用与路由 | [性能与路由](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch29-performance-routing) |
| [30 部署、版本与 LLMOps](30%20%E9%83%A8%E7%BD%B2%E3%80%81%E7%89%88%E6%9C%AC%E4%B8%8E%20LLMOps.md) | 管理版本组合、兼容迁移、灰度与恢复 | [发布控制](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch30-llmops) |
| [31 微调、蒸馏与模型适配](31%20%E5%BE%AE%E8%B0%83%E3%80%81%E8%92%B8%E9%A6%8F%E4%B8%8E%E6%A8%A1%E5%9E%8B%E9%80%82%E9%85%8D.md) | 判断模型适配需求，检查数据与候选效果 | [模型适配评审](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch31-model-adaptation) |
| [32 数据治理与持续改进](32%20%E6%95%B0%E6%8D%AE%E6%B2%BB%E7%90%86%E4%B8%8E%E6%8C%81%E7%BB%AD%E6%94%B9%E8%BF%9B.md) | 管理用途、数据谱系、撤回删除与反馈改进 | [数据治理](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch32-data-governance) |

## 运行第一个实验

先用第十章的执行循环实验观察一次完整运行。它只需 **Python 3.11+ 标准库**，不需要 API Key；模型候选与工具数据由本地脚本模拟。

```bash
git clone https://github.com/douloveppt/agent-application-learning.git
cd agent-application-learning

python3.11 examples/ch10-agent-loop/lab.py
python3.11 -m unittest discover -s examples/ch10-agent-loop -p 'test_*.py' -v
```

已有仓库副本时，在仓库根目录执行最后两条命令。若本机通过 `python` 或 `python3` 启动 Python，先确认版本为 3.11 或更高，再替换命令中的解释器名称。

输出包含 10 个场景。先观察 `normal` 如何在 3 个模型回合、2 次工具派发后完成，再对比 `repeated_call` 与 `batch_budget` 为什么停止。实验验证的是程序的控制路径；真实模型效果与业务系统集成需要另做验收。详细说明见 [第十章实验 README](https://github.com/douloveppt/agent-application-learning/tree/main/examples/ch10-agent-loop)。

### 其他实验的环境

多数实验使用 Python 3.11+ 标准库。进入每个实验后，先读该目录 README，确认依赖、启动命令与实际覆盖范围。

| 实验 | 额外准备 |
| --- | --- |
| 第 04 章 · 结构化输入输出 | Pydantic v2，按实验的依赖文件安装 |
| 第 18 章 · Sandbox | 默认实验需要 POSIX 文件接口，可在 macOS / Linux 运行；Windows 使用 WSL。Docker 固定探针为可选项，单独准备环境 |
| 第 19 章 · 浏览器自动化 | 完整实验需要 Playwright 与 Chromium；无浏览器的合同测试可单独运行 |

## 阅读方式与学习记录

| 方式 | 如何使用 |
| --- | --- |
| 在 GitHub 阅读 | 通过上方目录进入专题，使用页尾上一章／下一章导航；从实践入口打开实验 README |
| 在 Obsidian 学习 | 克隆或下载仓库，将目录作为知识库打开，再进入 [知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas)；使用 Canvas、反向链接和局部关系图浏览关联 |

学习工作台的动态进度表与待办查询需要 Obsidian 核心插件 **Bases、Search**，GitHub 显示查询源码。图谱与进度视图无需第三方插件；学习状态在专题属性中手动维护，实验结果与失败记录保存在对应专题或自己的项目中。

配套图解位于 [assets](https://github.com/douloveppt/agent-application-learning/tree/main/assets)，实验代码位于 [examples](https://github.com/douloveppt/agent-application-learning/tree/main/examples)。图解用于理解机制，实验用于验证其声明的范围；接入自己的项目时，按章节验收补齐真实接口、环境和业务证据。

## 更新与发布

Obsidian 学习目录保存原稿，本仓库提供适配 GitHub 的公开副本。每轮项目修改完成并验证后，由 Codex 提交并推送；协作流程、导出方式与验证命令见 [协作与发布说明](https://github.com/douloveppt/agent-application-learning/blob/main/PUBLISHING.md)。

| [← 返回知识图谱](00%20Agent%20%E5%BC%80%E5%8F%91%E5%AD%A6%E4%B9%A0%E5%85%A5%E5%8F%A3.canvas) | [下一章 · 01 产品目标与自治边界 →](01%20%E4%BA%A7%E5%93%81%E7%9B%AE%E6%A0%87%E4%B8%8E%E8%87%AA%E6%B2%BB%E8%BE%B9%E7%95%8C.md) |
| :--- | ---: |
