# 第十六章 MCP 协议边界离线实验

这个实验让你观察企业 IT 助手与能力服务之间的三个动作：`server/discover`、分页 `tools/list`、只读 `tools/call`。它采用 **MCP 2026-07-28** 的逐请求元数据和显式 `resultType`，不执行旧版 `initialize` 握手。

环境要求：**Python 3.11+ 标准库**。没有模型、API Key、网络连接或真实企业系统；不会创建工单、修改业务记录或写入数据库。两个工具只读取内置样本，Principal 是测试代码构造的可信身份夹具。

## 运行

在仓库根目录执行：

```bash
python3.11 examples/ch16-mcp/lab.py scenarios all
python3.11 examples/ch16-mcp/lab.py scenarios read_retry
python3.11 -m unittest discover -s examples/ch16-mcp -p 'test_*.py' -v
```

## 八个场景

| 场景 | 可观察结果 | 要理解的边界 |
| --- | --- | --- |
| `normal` | 发现版本和能力，读取当前员工的 IT-2048 | 发现不是授权；每次请求都携带版本和客户端能力 |
| `pagination` | 两次请求取得两个工具，第二次 cursor 是空字符串 | cursor 是不透明字符串，空字符串也可能有效；不能只用真值判断结束 |
| `incompatible_version` | JSON-RPC `-32022`，带 supported 与 requested | 版本不兼容必须显式处理，不能悄悄改用旧语义 |
| `identity_injection` | `isError: true`，handler 调用数为 0 | 模型不能通过 user_id 参数改变当前身份 |
| `resource_boundary` | 其他员工、其他租户和不存在的工单得到同一拒绝结果 | 参数里只传工单 ID，服务端仍要逐条检查归属，避免泄露存在性 |
| `error_layers` | 未安装工具返回 JSON-RPC error；无权或不存在的记录返回工具执行错误 | 传输成功、协议完成和业务成功要分别判断 |
| `read_retry` | 首次读取已经执行但回包丢失，第二次读取成功；两个 RPC ID | 受限读重试来自宿主固定策略，不来自工具自报的 readOnlyHint |
| `invalid_results` | 错误 RPC ID、超大返回、畸形 ticket、错配工单 ID、矛盾文本均被拒绝 | 接到 JSON 不代表可以进入模型上下文；还要验证结果与请求及各返回通道是否一致 |

`read_retry` 中 handler 执行两次是故意暴露的事实：第一次回包丢失不代表服务端没有执行。本实验的两个工具是无副作用的固定读取，所以宿主允许最多再试一次；这不证明工单提交也能照此重试。写入结果未知时的幂等、回执查询和核实流程，应结合第 14、26 章另行实现与验收。

## 代码怎样区分三类信任

1. `Client.rpc` 组装版本、客户端能力和软件信息，JSON 编码后交给 `InProcessTransport`。软件名称只用于说明和观察，不能证明用户是谁。
2. `InProcessTransport` 将 JSON 字节解码后交给 Server，并从独立的 `principal` 属性传入身份夹具。工具 arguments 和 `_meta.clientInfo` 都不能覆盖它。这个旁路只用于表现真实服务中的认证中间件边界，**没有实现认证**。
3. `Server` 每次读取 Principal，先检查工具权限，再检查参数，最后按 `tenant_id + user_id + ticket_id` 读取记录。即使工具已出现在目录里，调用时仍要重新检查。

测试会在同一个 Server、同一个 Transport 上更换 Principal，检查上一请求的身份不会遗留给下一请求。生产认证上下文应当逐请求不可变且并发隔离，不能使用这里可变的单线程夹具对象存放真实用户身份。

## 目录、缓存与结果校验

工具目录每页只返回一个条目，以便稳定观察分页。客户端区分“没有 nextCursor”和“nextCursor 是空字符串”，并限制最多 8 页，拒绝重复 cursor 与重复工具名。发现和目录结果带 `ttlMs`、`cacheScope`；本实验取 `ttlMs: 0` 并始终重新查询，没有实现缓存。

客户端先检查 32 KiB 回包预算、JSON-RPC envelope 与关联 ID，再检查明确的 `resultType: complete`，最后校验本实验的工具结果字段。返回的 `ticket.ticket_id` 必须等于本次请求的 `arguments.ticket_id`，避免把其他资源的有效对象当成本次结果；业务资源对应关系和 JSON-RPC 消息对应关系需要分别检查。

本实验另有固定合同：`content` 恰好包含一个 text，且 text 是 `structuredContent` 的 JSON 表示。客户端会解析后比较两者，拒绝额外文本、不可解析文本或矛盾内容，避免结构校验通过后把另一段不可信文本交给模型。这是本例的应用合同，**不是 MCP 要求所有工具结果都采取这种格式**。它不是通用内容处理器。该 fixture 固定为现代版本，缺少 `resultType` 会失败；跨旧版互操作客户端还需要按官方兼容规范处理旧结果。

手写参数校验只覆盖本例声明的两个简单工具，字符串长度按原始输入计算，另行拒绝全空白值；先裁掉空白再测长度会漏掉超长原文。它**不是 JSON Schema 2020-12 验证器**。示例没有宣称完整协议符合性；接入通用服务器时应采用支持目标协议版本的 SDK 和 Schema 验证器，并完成兼容测试。

## 实验未覆盖的生产能力

本实验不实现 HTTP、stdio、SSE、TLS、OAuth、凭据刷新、SDK 生命周期、旧版握手兼容、MRTR、订阅、Resources、Prompts、扩展、真实缓存、持久化审计、并发身份隔离、工单写入和跨进程恢复。它验证的是一个有意缩小的协议与业务边界。运行成功不能作为真实 MCP 服务上线或安全验收的证据。

真实接入时至少再验证：认证失败与权限吊销、HTTP header/body 元数据一致性、代理超时与 SSE、SDK/协议矩阵、跨租户资源访问、超限返回、目录缓存隔离，以及写入超时后的供应方回执核实。

协议依据：[Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)、[Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)、[Base protocol](https://modelcontextprotocol.io/specification/2026-07-28/basic)、[Versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)。
