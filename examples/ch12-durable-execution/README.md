# 第十二章 状态 持久执行与恢复 离线实验

这个实验把一个“审批后提交工单”的任务拆成独立的 `runtime.db` 与 `provider.db`。前者代表 Agent 运行时，后者代表外部工单系统；两个数据库之间**没有事务**。实验使用真实子进程与 `os._exit(73)` 中断，观察数据库中已经提交的状态如何指导下一次恢复。

环境要求为 **Python 3.11+ 标准库**。没有网络、模型调用、API Key、真实企业系统或费用。默认演示只创建并清理程序自己的临时目录；不会删除用户指定目录。

## 运行

在仓库根目录执行：

```bash
python3.11 examples/ch12-durable-execution/lab.py
python3.11 -m unittest discover -s examples/ch12-durable-execution -p 'test_*.py' -v
```

演示输出四行 JSON；字段顺序由序列化决定，关键结果如下：

| 中断位置 `crash` | 重启前的持久状态 `persisted` | 恢复结果 `resumed` | 工单行数 | create 次数 | 查询次数 |
| --- | --- | --- | ---: | ---: | ---: |
| `waiting` 等待审批 | `WAITING_APPROVAL` | 收到审批后 `COMPLETED` | 1 | 1 | 0 |
| `prepared` 已有意图 尚未派发 | `PREPARED` | `COMPLETED` | 1 | 1 | 0 |
| `after_provider` 外部已成功 本地回执未保存 | `DISPATCHING` | `COMPLETED` | 1 | 1 | 1 |
| `dispatching` 已记派发意图 还没调用供应方 | `DISPATCHING` | `HUMAN_REQUIRED` | 0 | 0 | 1 |

每个子进程都必须以 `73` 退出，父进程再读取原来的文件数据库。第三行恢复时先持久化 `UNKNOWN`，随后只按原 `operation_key` 查询；读到成功回执后保存结果，**不会第二次调用 create**。

第四行特意展示保守恢复的代价：实验注入器知道外部调用尚未发生，恢复程序只看得到 `DISPATCHING`，无法凭这个状态证明“从未提交”。模拟供应方的查询不保证查不到就是不存在，因此状态保留为 `UNKNOWN`，Run 标记为 `HUMAN_REQUIRED`。再运行恢复仍然只查询，不会自行重放。

## 代码中的持久协议

1. `create_run()` 保存 `WAITING_APPROVAL`、待批准参数及规范化后的 `payload_hash`。
2. `approve()` 在一个本地事务中写入 inbox、创建 `PREPARED` action、将 Run 更新为 `READY`。稳定的 `operation_key` 同时绑定 Run 与批准的参数摘要。
3. `advance()` 先提交 `DISPATCHING`，再调用独立的模拟供应方。正常收到回执时，将回执、`SUCCEEDED` 和 `COMPLETED` 放入同一个运行时事务。
4. 进程重启后，`PREPARED` 可以首次派发；`DISPATCHING` 必须按不确定结果处理；`SUCCEEDED` 直接复用已保存的回执。
5. `UNKNOWN` 只能核实。找到相同操作及相同参数摘要的回执才完成；查不到或摘要冲突进入人工处理。

`provider.db` 分别记录 `tickets`、`create_calls`、`lookup_calls`。测试核对真实落盘的业务行数、调用次数、查询使用的操作标识与本地状态，因此供应方的唯一约束即便挡住重复插入，也无法掩盖重复 create 调用。

## 可调用接口

| 接口 | 作用 |
| --- | --- |
| `initialize(runtime_db, provider_db)` | 创建实验用表 |
| `create_run(runtime_db, run_id, payload)` | 创建待审批 Run，返回参数摘要 |
| `revise_payload(runtime_db, run_id, expected_version, payload)` | 只允许审批前修改；CAS 成功返回 `True` |
| `approve(runtime_db, run_id, event_id, approved_hash)` | 返回 `ACCEPTED`、`DUPLICATE`、`STALE_APPROVAL` 或 `ALREADY_APPROVED` |
| `advance(runtime_db, provider_db, run_id, crash_at=None)` | 推进或恢复同一 Run |
| `snapshot(runtime_db, run_id)` | 读取 Run 与 action 的持久状态 |
| `provider_stats(provider_db)` | 读取业务行数、create 调用数及查询数 |

`launch_worker()` 是测试使用的子进程入口。`crash_at` 可取 `waiting`、`prepared`、`dispatching`、`after_provider`。两个数据库路径由受信任的实验驱动传入，不是对外服务接口。

## 测试覆盖与边界

10 个测试覆盖：三个必要崩溃位置、无回执时保持未知、重复审批 inbox 去重、参数变化后的旧审批拒绝、CAS 旧版本拒绝、已确认成功复用、审批后参数冻结，以及供应方回执参数冲突。

这是**受信任输入、单 Worker、单步骤**实验。CAS 只在“审批前修改参数”接口演示，不能据此认为执行循环已经支持多个 Worker；`advance()` 没有租约、fencing、并发抢占或旧 Worker 隔离。审批只绑定内容摘要，没有绑定输入版本：内容从 A 改成 B 再改回 A 后，旧 A 摘要仍会匹配。审批事件也没有真实用户认证、授权、过期、撤销和签名验证；生产实现需要补齐版本、身份与有效期等条件。

实验没有实现持久队列、Outbox、后台对账服务、断电和磁盘损坏恢复、跨地区灾备、运行代码版本迁移、执行预算、取消与补偿。供应方仅为独立 SQLite 文件，不代表任何真实供应方的查询一致性或幂等合同。真正集成时，需要分别验证这些能力；这个实验只证明所列崩溃边界下，已保存的状态和外部回执如何约束恢复动作。
