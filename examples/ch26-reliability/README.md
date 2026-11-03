# 第 26 章实验：Outbox、幂等与未知结果核实

员工让企业 IT 助手创建一张 VPN 故障工单。运行时先在自己的数据库中保存“要创建哪张工单”，再把消息交给独立的工单系统。实验故意让工单系统提交成功后丢失响应，观察系统为什么必须保留原操作身份、进入 `UNKNOWN`，并通过查询原操作完成核实，而不能再次创建。

实验使用 **Python 3.11+ 标准库**与两个 SQLite 文件：

- `runtime.db` 保存 Operation、Outbox、派发状态和最终回执；
- `provider.db` 保存模拟工单与 Inbox 去重回执；
- 两个文件没有共同事务，故意呈现真实系统常见的跨边界问题。

不调用网络、模型或真实工单系统，不需要 API Key，也不产生费用。演示只使用并自动清理自己创建的临时目录。

## 运行与预期结果

在项目根目录执行：

```bash
python3.11 examples/ch26-reliability/lab.py
python3.11 -m unittest discover -s examples/ch26-reliability -p 'test_*.py' -v
```

演示输出中的随机稳定摘要可能不同，关键结果如下：

```text
1 operation + outbox: {'operations': 1, 'outbox': 1} created= True
2 lost receipt: local= DISPATCHING provider= {'tickets': 1, 'inbox': 1}
3 lookup recovery: result= SUCCEEDED attempts= 1 ticket= ticket_...
4 negative lookup: result= STILL_UNKNOWN state= UNKNOWN attempts= 1
```

13 项测试应全部通过。测试使用 `TemporaryDirectory` 隔离数据库，不依赖网络或实际等待。

## 1. 先看两个事务边界

```text
runtime.db                              provider.db
┌──────────────────────────┐           ┌──────────────────────────┐
│ Operation + Outbox       │──消息───→ │ Inbox + Ticket           │
│ 同一个本地事务提交       │           │ 同一个供应方事务提交     │
└──────────────────────────┘           └──────────────────────────┘
         │                                        │
         └──── 本地状态与外部工单之间没有共同事务 ┘
```

`RuntimeStore.prepare()` 在一个事务中插入 Operation 和 Outbox。测试在两次插入之间注入异常，确认两张表都没有残留。这样可以防止“业务意图保存了，但没有任何可投递消息”或“消息存在，却找不到对应业务意图”。

`ProviderStore.consume()` 在另一个事务中插入 Ticket 和 Inbox。若在 Ticket 插入后、Inbox 插入前失败，两者一起回滚。消息重复到达时先查 Inbox：

- 同一 `event_id`、同一消息直接复用原回执；正常的传输重试与重复投递保持这个 ID 不变；
- 同一 `event_id`、不同消息返回冲突；
- 若另一个逻辑事件错误地再次携带同一 `operation_key`，仍复用原 Ticket，只新增这个逻辑事件的 Inbox 回执。

这里有两层身份。`event_id` 标识一条逻辑消息，不标识每次网络发送；同一逻辑消息可以被传输多次。`operation_key` 标识一次业务动作，解决不同逻辑消息或不同传输尝试指向同一业务操作。只有消息去重而没有业务幂等，另一条事件再次指向同一操作时仍可能创建第二张工单。

## 2. 操作键必须绑定作用域与请求内容

实验中的操作作用域为：

```text
tenant_id + requester_id + action + client_key
```

运行时由这个作用域生成稳定 `operation_key`，并对规范化后的 `title` 与 `category` 计算 `payload_fp`。同一作用域下：

- 同键、同内容返回原 Operation；
- 同键、不同内容抛出 `Conflict`，不会悄悄覆盖；
- 不同员工或不同租户使用相同 `client_key`，得到独立 Operation；
- 明确换一个 `client_key` 才代表新的业务动作，并可创建新工单。

本实验把标题中的连续空白折叠为一个空格，并把分类转为小写，这是这个教学业务明确规定的“等价请求”。它不是通用哈希规则。真实系统要先定义字段级业务等价、默认值、集合顺序、时区、金额精度和 Schema 版本，再对稳定的规范形式计算摘要；不能随意改变规范化算法，也不能直接对不稳定的序列化文本做长期幂等判断。

`Scope` 代表服务端从可信认证上下文构造的身份。本实验为了聚焦一致性，演示使用固定 `TRUSTED_SCOPE`，测试由受信任代码构造少量作用域。**生产接口不能让客户端任意填写 `tenant_id`、`requester_id` 或 `action`。** 客户端只提供稳定请求键和允许的业务字段，服务端负责绑定真实身份、动作类型与授权结果。本例还固定为单一可信工单连接，因此没有 `connection_id`；连接器平台面对同租户多个工单实例或多套凭据时，操作作用域还要绑定服务端确认的 `connection_id` 或等价连接身份，不能让相同键跨连接碰撞。

## 3. 丢回执为什么会产生 UNKNOWN

正常派发依次经过：

```text
PREPARED / PENDING
        │ 持久化派发开始，attempts + 1
        ▼
DISPATCHING / SENDING
        │ 调用独立供应方
        ├─ 收到并保存匹配回执 ──→ SUCCEEDED / DELIVERED
        └─ 进程消失或响应丢失 ──→ 重启后 UNKNOWN / UNKNOWN
```

`dispatch(..., crash_at="after_provider_commit")` 让供应方的 Inbox 和 Ticket 已经提交，然后抛出 `SimulatedCrash`，本地来不及保存回执。这个异常是教学故障注入，不会真的杀死 Python 进程；测试随后丢弃对象、重新打开两个数据库，模拟进程重启只依赖持久数据。

重启时只能看到本地 `DISPATCHING`。它既不能证明供应方已执行，也不能证明未执行，所以 `reconcile()` 先把本地状态持久化为 `UNKNOWN`，再使用**原 `operation_key` 只读查询**供应方。查询得到工单后，还要逐项核对：

- `operation_key`；
- `tenant_id` 与 `requester_id`；
- `action`；
- `payload_fp`；
- 供应方业务状态。

全部匹配才保存回执并转为 `SUCCEEDED`。测试还注入了“同操作键但租户不匹配”的供应方记录：业务确定性仍然是 `UNKNOWN`，同时设置独立的 `review_required=1` 控制标记，并向调用方返回 `MANUAL_REVIEW`。人工处理只是下一步控制方式，不能覆盖“外部动作结果仍未知”这个业务事实；重复核实时会根据控制标记继续返回人工处理，不能把别人的工单当成本次成功。

## 4. 对账只查询，不把查询变成第二次写入

`ProviderStore.lookup()` 使用 SQLite `mode=ro` 打开供应方数据库。核实过程对供应方只执行 `SELECT`；本地可以保存 `UNKNOWN`、回执或人工处理状态，但不会调用 `consume()`、创建 Ticket 或增加 Outbox 派发次数。

实验也注入“本地已记录派发开始，但供应方没有 Ticket”的场景。供应方没有承诺“立刻查不到就永远不存在”，因此查询为空只返回 `STILL_UNKNOWN`：

```text
UNKNOWN ──查到且完全匹配──→ SUCCEEDED
   │
   ├─暂时查不到───────────→ UNKNOWN（稍后继续核实或转人工）
   │
   └─查到但身份/内容冲突──→ UNKNOWN + review_required=1
```

连续核实三次，`attempts` 仍为 1，Ticket 和 Inbox 仍为 0。查询为空不会自动转 `FAILED`，也不会把 Outbox 改回 `PENDING`。只有供应方合同明确保证查询强一致、查询范围完整、“不存在”结论具有权威性，**并且原请求不会在查询之后排队落地或稍后执行**时，系统才能按该合同设计其他分支；不能从普通最终一致查询自行推导。

## 5. 代码入口

| 入口 | 作用 | 关键约束 |
| --- | --- | --- |
| `RuntimeStore.prepare()` | 创建或复用 Operation | Operation 与 Outbox 同事务；同键异参冲突 |
| `RuntimeStore.begin_dispatch()` | 记录派发边界 | 只允许 `PREPARED/PENDING`；先提交状态再出站 |
| `ProviderStore.consume()` | 消费 Outbox 消息 | Inbox 与 Ticket 同事务；事件和业务操作两层去重 |
| `dispatch()` | 执行一次真实派发尝试 | 状态一旦含糊，不能再次调用本入口 |
| `ProviderStore.lookup()` | 按操作键查询供应方 | 只读连接，不创建或修改供应方数据 |
| `reconcile()` | 恢复含糊派发 | 只查询原操作；验证身份与参数后保存回执 |
| `snapshot()` / `counts()` | 观察实验状态 | 均以只读连接访问数据库 |

## 6. 测试覆盖

测试覆盖以下 13 个场景：

1. Operation 与 Outbox 在故障下注入后共同回滚；
2. 同作用域同键同规范化内容复用，同键异参冲突；
3. 不同员工、不同租户的同名键互相独立；
4. Outbox 消息绑定操作、身份和参数摘要；
5. 供应方 Ticket 与 Inbox 共同回滚；
6. 相同事件重复投递只保留一条 Inbox 和一张工单；
7. 另一条逻辑事件重复同一业务操作时仍复用工单；
8. 事件 ID 被不同内容复用时拒绝；
9. 外部成功但回执丢失后，重开数据库并通过只读查询恢复；
10. 阴性查询持续保留 `UNKNOWN`，不重发；
11. 新请求键创建真实的新业务操作；
12. 核实结果的租户不匹配时保持 `UNKNOWN`，同时设置人工处理标记；
13. 成功回执重新打开数据库后仍可读取。

## 7. 教学边界

这个实验只证明两个单机 SQLite 事务边界下的协议行为，不代表完整生产实现。它没有实现：

- HTTP、认证、授权、真实多租户接入与凭据管理；
- 多 Worker 争抢、租约、fencing、心跳、旧 Worker 隔离；
- 真实消息队列的 ACK、可见性超时、分区顺序、投递延迟与积压治理；
- Outbox 后台扫描器、指数退避、抖动、重试预算、熔断、限流和死信队列；
- 供应方真实幂等协议、查询一致性、分页、限流、回执签名与保留期；
- Saga、补偿审批、人工工作台、告警、审计、指标与对账批次；
- 数据迁移、备份恢复、跨地区灾备、加密、删除和合规保留。

SQLite 会串行化写事务，两个数据库也位于同一台机器；这不能证明分布式吞吐量或高可用。`SimulatedCrash` 发生在代码明确选择的已提交边界，不覆盖内核、磁盘、文件系统或断电故障。生产系统仍需结合真实数据库、队列和供应方合同做故障注入与恢复演练。
