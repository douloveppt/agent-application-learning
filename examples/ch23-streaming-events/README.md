# 第 23 章实验：事件回放与客户端状态恢复

这个离线实验用企业 VPN 工单助手的简化事件流，解释为什么文本输出、任务状态、取消请求和业务回执要分开处理。它只使用 Python 3.11 标准库，在内存中模拟事件日志与客户端投影，不调用模型，不访问网络，不创建真实工单。

## 运行

在本目录执行：

```bash
python3.11 streaming_lab.py
python3.11 -m unittest -v
```

查看逐行 JSON 事件：

```bash
python3.11 streaming_lab.py --events
```

预期看到六个场景：正常完成；重复文本增量被忽略；事件缺口触发回放；游标过期后安装快照并衔接后续事件；迟到文本不修改终态；取消与外部写入竞争时先核对回执。

```text
1 正常：succeeded，last_seq=8，文本=已找到 VPN 排查指引。
2 重复：seq=5 -> duplicate，文本='已找到 '
3 断线：收到 seq=6 -> needs_replay，从 cursor=3 回放；最终 succeeded
4 过期：cursor=2 -> snapshot H=6 + events>H；文本=已找到 VPN 排查指引。
5 终态：迟到文本 -> late_ignored，状态仍是 succeeded
6 取消竞态：cancel_status=requested，状态=reconciling；只查询回执，不重发写入
  核对后：succeeded，receipt_id=ticket-8042，cancel_status=too_late
```

## 从事件到界面

`EventLog` 是单个已授权公开流的内存模型。`append()` 分配从 1 开始的连续 `seq`；`replay(after)` 只读取事件，不会再次执行任务。每条事件有 `run_id`、`seq`、`event_id`、`type`、`schema_version`、`payload`。

`Projection` 是客户端 reducer。`view.status` 表示任务业务状态；`sync_required` 表示客户端是否需要恢复；`cancel_status` 表示取消的处理结果。界面不根据一段生成文字把步骤标为完成。示例中只有带 `evidence_id` 的 `step.completed` 会完成相应步骤。

| 输入情况 | reducer 的行为 | 游标变化 |
|---|---|---|
| 下一条合法事件 | 更新投影 | 前进到此事件 |
| 已处理事件重复到达且窗口内指纹一致 | 返回 `duplicate`，不再追加文本 | 不变 |
| 收到的 `seq` 大于期望值 | 返回 `needs_replay`，保留现有投影 | 不变 |
| 不兼容版本、错误流身份、坏 payload、冲突事件 | 返回 `needs_snapshot`，停止推进 | 不变 |
| 终态后结构合法且连续的迟到文本或步骤事件 | 返回 `late_ignored`，保持终态结果并记录诊断 | 前进，避免反复消费 |
| 游标早于日志保留水位 | `CursorExpired`，获取快照 | 安装快照时前进到 `H` |

快照中的 `last_seq=H` 必须与快照状态对应。在本实验中，先用权威投影消费到 `H`，再在单线程中同时复制状态和水位。恢复时先安装快照，再消费 `seq > H`，不能先订阅一条随时变化的 live 流再随意覆盖快照。

`text.snapshot` 是文本消息的完整替换事件；`Projection.snapshot()` 是整个任务投影及游标的一致快照，两者含义不同。

处理顺序是先验证字段与各事件类型的 payload 结构，再检查重复和序号连续性，最后执行状态转换或终态保护。终态保护只忽略业务变化，不能把缺少 `delta`、无效步骤标识或缺少完整文本的事件视为已消费。

客户端保留最近 128 个已消费事件的 SHA-256 指纹。实验定义的规范 JSON 使用排序后的对象键、固定分隔符和 UTF-8 编码，指纹覆盖 `run_id`、`seq`、`event_id`、`type`、`schema_version`、`payload`。同一 `seq` 和 `event_id` 若内容变化，也会触发 `needs_snapshot`；对象键的排列顺序不影响指纹。非法 JSON 值、非字符串对象键、NaN、Infinity、循环引用和超过 32 层的嵌套会被受控拒绝。

窗口之外的旧事件以及安装快照前的事件没有本地指纹可比，结构验证通过后可作为旧重复事件忽略，其内容一致性依赖服务端不可变日志。本地有界去重窗口不能证明任意历史事件未被篡改。

## 取消为什么不能立即显示成功

`cancel.requested` 只把 `cancel_status` 改为 `requested`。若工单提交已发出但结果尚不明确，`run.status_changed(status="reconciling")` 将任务置为 `reconciling`；此时直接消费 `run.completed(status="cancelled")` 或 `run.completed(status="failed")` 都会被拒绝。

示例随后收到 `operation.reconciled(receipt_id="ticket-8042")`，确认工单已经提交，再以 `run.completed` 完成任务；原来的取消请求显示 `too_late`。恢复连接与核对回执都不重发提交动作。另一条测试覆盖没有未知结果时的取消确认。

## 测试验证的契约

十四项测试覆盖重复增量、缺口补发、生成文字不能证明完成、快照水位衔接、未知版本阻断、错误流身份和冲突事件、终态保护、终态坏 payload、相同标识下的内容冲突、非法 JSON 值、有界指纹与规范键序、未知写入的取消竞争、安全取消确认，以及快照身份与游标不能倒退。

可以自行改变 `sample_log()` 的文本，把“工单提交成功”放进 `text.delta`：它仍然不能把 `run.status` 改成 `succeeded`，也不会产生工单回执。

## 生产系统还需要什么

这里没有实现 HTTP/SSE、SSE 帧解析、认证与租户隔离、数据库持久化、事务 Outbox、跨进程排他写入、worker fencing、审批授权、服务端命令幂等、快照签发和权限失效、订阅背压与限流。`EventLog` 进程退出即丢失数据；指纹窗口虽有上限，文本、事件列表和诊断列表仍是未限长的教学数据结构。生产中应由持久化日志、带权限检查的快照接口、受控缓冲与游标存储承担这些职责。

本实验的连续 `seq` 只适用于一个完整的已授权公开流。内部事件先按可见性投影，再为公开流分配序号；若服务端中途过滤事件，客户端就不能把每个序号缺口都当作丢包。审批处理在这里只演示界面状态，不能作为真实审批安全实现。

不兼容版本触发恢复流程并不意味着随便获取一次快照就能解决：必须取得客户端能够理解的快照版本，或者升级客户端。快照也不能替代重新鉴权。
