# 第十七章 Connectors 与业务集成离线实验

这个实验实现一个租户绑定的企业工单 Connector。它把可信请求上下文、模型参数、外部供应方回执与本地操作账本分开，重点观察一种常见事故：供应方已经创建工单，但网络超时使 Connector 没收到回执。此时结果是 `unknown`，恢复时只能用原 `operation_id` 查询，不能再次提交。

环境要求为 **Python 3.11+ 标准库**。实验不连接网络、模型或真实工单系统，不读取 API Key。`ledger.db` 与 `provider.db` 是两个独立 SQLite 数据库，模拟 Connector 与外部系统之间不存在跨库事务。

## 运行

在仓库根目录执行：

```bash
python3.11 examples/ch17-connectors/lab.py
python3.11 -m unittest discover -s examples/ch17-connectors -p 'test_*.py' -v
```

演示会输出四行 JSON：

| 场景 | 第一次调用 | 新进程恢复后 | create 次数 | lookup 次数 |
| --- | --- | --- | ---: | ---: |
| `success` | `succeeded` | 复用账本回执 | 1 | 0 |
| `business_reject` | `rejected` | 复用确定拒绝 | 1 | 0 |
| `commit_then_timeout` | `unknown` | 查询到原工单并完成 | 1 | 1 |
| `timeout_before_commit` | `unknown` | 仍然查不到，继续未知 | 1 | 1 |

最后一行有意保持 `unknown`。这个模拟查询没有承诺“查不到就代表绝对不存在”，所以 Connector 不能把空结果当成重新创建的许可。

## 信任边界

调用入口是：

```python
connector.submit(
    context=RequestContext("tenant-a", "employee-7", frozenset({CREATE_OWN})),
    operation_id="op-2026-0001",
    arguments={"title": "VPN 无法连接", "description": "证书已过期"},
)
```

`RequestContext`、`operation_id` 和租户绑定连接由宿主提供。模型参数只允许 `title` 与 `description`；`requester_user_id` 永远取自可信上下文。额外传入 requester、tenant 或 priority 都会在建立持久操作前被拒绝。Connector 先检查上下文租户是否等于连接租户，再检查 `tickets:create:own` 权限，因此跨租户上下文和缺少权限都不会进入派发阶段。

本实验把规范化后的业务负载绑定到 `operation_id`：标题与描述先裁掉首尾空白，随后与可信租户、可信用户一起计算 SHA-256。相同操作标识配不同摘要会得到 `OperationConflict`，不会覆盖旧操作或发起第二次调用。

## 状态与恢复协议

| 状态 | 含义 | 下次允许的动作 |
| --- | --- | --- |
| `prepared` | 本地已保存稳定意图，尚未越过派发边界 | 首次派发 |
| `dispatched` | 已提交派发意图，供应方结果还没落账 | 转成 `unknown` 并查询 |
| `succeeded` | 回执字段与原操作完全匹配 | 复用回执 |
| `rejected` | 收到与原操作匹配、且合同保证无业务副作用的明确拒绝 | 复用拒绝结果 |
| `unknown` | 超时、连接错误或回执校验失败 | 只按 `operation_id` 查询 |

`TicketConnector._dispatch()` 先把 `dispatched` 提交到 `ledger.db`，再调用供应方。成功与拒绝回执都必须匹配 `client_reference`、租户、requester 与参数摘要；收到 JSON 不等于业务已经确定。回执缺字段或关联字段错配都会进入 `unknown`。

拒绝只有在供应方合同明确支持时才是终态。本实验的可信拒绝除了稳定错误码，还必须包含 `effect: not_applied`，表示供应方保证这次操作没有产生业务副作用；普通 HTTP 4xx、网关错误、文本错误消息或缺少该证据的拒绝都不能推出“没有创建工单”。这些回执会进入 `unknown` 并走核实流程。该证据来自 Connector 与供应方之间的协议，不是模型参数，模型不需要也不能传入这些字段。

测试还直接构造了两个进程中断后的落盘边界。若账本已经是 `dispatched`、供应方尚未收到请求，恢复程序仍然只查询：空结果保持 `unknown`，create 次数为 0。若供应方已经提交、账本仍停在 `dispatched`，恢复程序按相同 `operation_id` 找到原工单并保存回执，create 次数始终为 1。两种情况在本地状态上完全相同，因此恢复代码不能利用测试夹具知道的真实中断位置来决定重放。

`FakeProvider` 把 create 与 lookup 调用分别记账。即使 `tickets.client_reference` 的唯一约束挡住重复数据，测试仍能发现重复 create 调用，避免把数据库去重误当成 Connector 没有重试。

## 测试覆盖

19 个测试覆盖：可信 requester 注入、额外身份参数拒绝、跨租户拒绝、权限拒绝、成功回执复用、同操作标识参数冲突、写入成功后超时、新 Python 进程核实原回执、两个 `dispatched` 崩溃边界、查询为空保持未知、具有无副作用证据的明确业务拒绝、拒绝证据缺失或关联字段错配、畸形回执恢复、业务 ID 与操作 ID 区分、规范化参数复用、参数合同校验，以及供应方的摘要或 requester 错配记录不能确认成功。

## 教学边界

这是受信任宿主驱动、固定单连接、固定供应方、单 Worker 的教学实验。它没有实现真实 OAuth、令牌刷新、Secret 管理、HTTP/TLS、Webhook、分页或增量同步，也没有实现多个 Worker 的租约、fencing、并发抢占和数据库迁移。账本只保存连接租户，没有绑定稳定的连接 ID、供应方账号或供应方实例；它不能证明同一租户更换连接或供应方之后仍可安全恢复。`RequestContext` 是测试夹具，不是认证机制；`FakeProvider` 的查询语义也不能代表真实供应商合同。

生产系统还需要验证供应商是否支持稳定的 client reference 或幂等键、查询是否强一致、空结果是否权威、回执保留时长、限流与退避、连接级和用户级权限、凭据吊销、审计脱敏、数据驻留，以及未知结果进入人工核实和后台对账的时限。本实验只证明：在列出的单 Worker 路径中，持久化操作标识与回执核实可以阻止未知写入被自动重放。
