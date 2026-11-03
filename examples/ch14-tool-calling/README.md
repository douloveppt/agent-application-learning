# 第十四章 Tools 与 Function Calling 离线实验

这个实验实现一个最小的企业工单工具执行层。模型只能提交候选调用；受信任的应用负责解析、路由、身份、参数校验、审批、执行、回执和审计。外部写入结果不确定时，应用保留 `unknown`，再按服务端生成的 `operation_id` 查询，不把同一写操作重新发送一遍。

环境要求为 **Python 3.11+ 标准库**。没有网络、模型调用、API Key、真实企业系统或费用。演示使用临时目录中的两个 SQLite 数据库，结束后自动清理；测试也只写自己的临时目录。

## 运行

在仓库根目录执行：

```bash
python3.11 examples/ch14-tool-calling/lab.py scenarios all
python3.11 -m unittest discover -s examples/ch14-tool-calling -p 'test_*.py' -v
```

第一条命令输出五行 JSON，关键结果如下：

| `scenario` | 关键结果 | 说明 |
| --- | --- | --- |
| `approved_submit` | `status=ok`、`code=submitted` | 草稿经可信 UI 审批后提交，供应方只收到一次调用 |
| `invalid_and_overreach` | `invalid_json`、`invalid_arguments`、`tool_not_allowed` | 重复 JSON 键、身份字段注入和未注册工具分别被程序层拒绝 |
| `stale_and_expired_approval` | `approval_stale`、`approval_expired` | 内容修改或审批到期后都没有外部写入 |
| `duplicate_submit` | `submitted` → `already_submitted` | 重复请求复用已保存回执，`submit_calls=1` |
| `unknown_reconcile` | `outcome_unknown` → `reconciliation_required` → `reconciled` | 回执丢失后禁止盲目重放，查询一次确认供应方已经完成 |

可以单独运行某个场景，例如：

```bash
python3.11 examples/ch14-tool-calling/lab.py scenarios unknown_reconcile
```

## 一次调用怎样穿过执行边界

工具调用信封只有三个字段：

```json
{
  "call_id": "call-17",
  "name": "tickets.list_own",
  "arguments": {"limit": 20}
}
```

`ToolRuntime.execute_call()` 依次完成：

1. 按 UTF-8 字节数限制请求大小；JSON 重复键、`NaN`、`Infinity`、非对象顶层、过深嵌套、孤立 Unicode surrogate 和破损 JSON 都会被拒绝。
2. 对信封做精确字段检查，再用固定注册表和当前 `Principal.allowed_tools` 做双重工具白名单判断。
3. 按具体工具的参数合同检查字段、类型、枚举、长度和版本。多余字段同样失败，因此参数里增加 `tenant_id` 或 `user_id` 不能改变身份。
4. Handler 只能从受信任的 `Principal` 取得租户和用户。`tickets.list_own` 的 SQL 条件始终包含这两个维度。
5. 统一返回结果信封。列表过大时删除完整列表项；其他结果过大时保留身份、摘要和长度等边界信息。两种情况都标为 `partial/result_truncated`，不会从 JSON 字节中间截断。
6. 审计只记录调用 ID、工具名、结果分类、草稿 ID 和操作 ID，不保存标题、描述或整个参数正文。

每个结果都有相同的顶层字段：

| 字段 | 含义 |
| --- | --- |
| `call_id` / `tool` | 对应已解析的调用；解析前失败时可能为 `null` |
| `status` | `ok`、`partial`、`unknown` 或 `error` |
| `code` | 可供程序分支处理的稳定结果码 |
| `retryable` | 只说明当前错误是否适合按既定策略重试；未知写结果固定为 `false` |
| `truncated` | 结果是否因字节上限而裁剪 |
| `data` | 成功、部分成功或未知状态的结构化数据；业务拒绝时为 `null` |

`error` 不会被包装成 `ok`。`unknown` 也不是普通可重试错误：它表示外部副作用可能已经发生，下一步必须核实或转人工。

## 工具面与受信任接口

| 接口 | 参数 | 用途 |
| --- | --- | --- |
| `tickets.list_own` | `limit` | 只读当前 Principal 自己的工单 |
| `tickets.preview` | `title`、`description`、`priority` | 创建本地草稿和内容摘要，不提交外部系统 |
| `tickets.revise` | 草稿 ID、期望版本和完整新内容 | 用 CAS 式版本检查修改草稿，并要求重新审批 |
| `tickets.submit` | 草稿 ID、审批 ID | 校验审批后尝试一次外部提交 |
| `tickets.reconcile` | 草稿 ID、审批 ID | 按已保存的操作 ID 查询未知写结果 |
| `approve_from_ui(...)` | 草稿、版本、摘要、动作、过期时间 | 模拟可信 UI 的审批事件；**不在模型工具注册表中** |
| `provider_submit(...)` | 服务端操作 ID、Principal、负载、摘要 | 模拟供应方本地幂等事务，供运行时和测试使用 |

模型参数中没有 `operation_id`。可信应用在批准指定版本和内容时，根据租户、用户、草稿、版本、内容摘要与动作生成稳定操作 ID，再与审批一起持久化。用户重新发送同一工具调用时，运行时读取原操作和回执，不能让模型随意换一个 ID 绕过幂等。

## 审批绑定了什么

`approve_from_ui()` 只接受 `tickets.submit`，并把一次决定绑定到：

- 当前 Principal 的 `tenant_id` 与 `user_id`；
- `draft_id`、`draft_version` 和规范化内容的 SHA-256；
- 具体动作 `tickets.submit`；
- 绝对过期时间 `expires_at`。

提交时会重新读取数据库中的草稿与审批，逐项比较这些条件。审批后改标题、描述或优先级会增加草稿版本并改变摘要，旧审批返回 `approval_stale`；当前时间达到过期时间后返回 `approval_expired`。另一个用户看不到该草稿，也找不到该审批。

审批 ID 只是这个实验中的确定性引用，不是数字签名或真实登录凭据。生产系统必须从已认证的 UI 会话接收审批事件，并独立校验用户是否仍有权批准该资源与动作。

## 幂等、未知结果与对账

`runtime.db` 保存草稿、审批、动作和最小审计；`provider.db` 保存模拟工单、提交调用和查询调用。两个数据库之间没有跨库事务。

正常提交的顺序是：

1. 运行时先在本地事务中创建 `DISPATCHING` 动作，保存稳定 `operation_id` 与 `payload_hash`。
2. 模拟供应方在自己的 `BEGIN IMMEDIATE` 事务内检查 `operation_id`。同 ID、同摘要返回原回执；同 ID、不同摘要明确报 `operation_payload_conflict`。
3. 收到回执后，运行时把动作更新为 `SUCCEEDED` 并保存回执。之后重复提交直接返回 `already_submitted`。

`unknown_reconcile` 场景在第二步提交成功后丢弃响应。运行时只能知道动作结果不确定，因此写入 `UNKNOWN`。再次调用 `tickets.submit` 返回 `reconciliation_required`，不会第二次执行 `provider_submit()`。`tickets.reconcile` 用原 `operation_id` 查询，且只有回执中的 `payload_hash` 匹配才保存为成功。

审批过期会阻止新的外部写入，但不会阻止已经派发动作的只读对账。测试把时钟推进到审批过期之后，仍能按原操作 ID 修复丢失回执；这不会扩大原授权的动作范围。

这个设计也保留“查不到”的不确定性：演示供应方查询返回空时，运行时仍返回 `unknown/provider_receipt_not_found`。查询为空不自动证明写操作从未发生。

## 测试覆盖与能力边界

10 个测试覆盖：严格 JSON 边界（包括过深嵌套与非法 Unicode）、工具和 Principal 双白名单、身份参数注入拒绝、本人数据隔离、审批的身份／动作／版本／摘要／过期约束、服务端稳定操作 ID、成功回执复用、丢回执后的禁止重放与对账、供应方幂等冲突、结果结构化裁剪、业务错误状态，以及审计不记录工单正文。

这是一个单进程教学实验。SQLite 的 `BEGIN IMMEDIATE` 和唯一键只证明本地模拟中的原子检查与写入，不能替代真实供应方的幂等合同，也不能证明跨服务 exactly-once。实验没有实现真实认证、签名审批、撤销、角色与资源级授权、密钥管理、Outbox、消息队列、租约与 fencing、多 Worker 抢占、崩溃恢复进程、后台对账任务、限流、分布式追踪或日志脱敏平台。

`lose_next_receipt` 是受信任测试驱动里的故障注入，不是模型参数。模拟供应方查询是立即一致的，真实系统可能延迟可见或永久无法查询；这时应保持未知、按预算有限轮询或转人工。接入真实工具前，还要按其实际 API 契约验证超时、重复请求、冲突、回执保留期和取消语义。
