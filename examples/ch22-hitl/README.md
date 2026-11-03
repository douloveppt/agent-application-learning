# 第 22 章实验：把人的决定变成可靠的执行条件

员工 Alice 准备给自己的 VPN 工单 `A-100` 补充说明。系统保存一份具体草稿，等待 Alice 决定，再把获批内容提交到模拟工单系统。重点观察：**批准是一项授权事实，工单是否完成还需要独立的业务回执。**

全部数据均为合成数据。程序不访问网络，不调用模型，不读取系统凭据，也不连接真实业务系统。需要 Python 3.11 或更高版本，仅使用标准库。

## 运行与预期

在仓库根目录运行：

```bash
python3.11 examples/ch22-hitl/hitl.py demo
python3.11 -m unittest discover -s examples/ch22-hitl -v
```

每次 demo 和测试使用新的临时目录，目录内包含两份 SQLite 文件：

| 文件 | 谁拥有 | 保存什么 |
| --- | --- | --- |
| `approvals.sqlite` | 审批服务 | 草稿版本、审批决定、成员与策略、派发意图、操作状态、最小审计 |
| `tickets.sqlite` | 模拟下游工单系统 | 工单版本、实际补充内容、按 operation ID 查询的业务回执、调用次数 |

两份数据库不能一起原子提交。演示中重新构造服务对象，继续读取原文件，模拟服务重启后的恢复；这不是杀进程或断电测试。演示结束自动清理临时目录。

`demo` 输出的 `outcomes` 应包括：

| 字段 | 预期 | 观察重点 |
| --- | --- | --- |
| `human_rejected` | `rejected` | 人拒绝后审批结束 |
| `rejected_write` | `not_approved` | 拒绝没有造成工单写入 |
| `edited_new_revision` | `2` | 修改正文产生新 revision |
| `old_tab_approval` | `stale_or_not_pending` | 旧标签页不能批准已被替换的版本 |
| `approved_but_not_completed` | `approved`，两项计数均为 0 | 批准不会提前执行业务写入 |
| `external_success_receipt_lost` | `unknown` | 下游已成功，本地没有收到确定回执 |
| `resume_does_not_resend` | `unknown` | 恢复不会再提交一次 |
| `read_only_reconciliation` | `succeeded` | 使用原 operation ID 查询下游回执 |
| `repeat_execution_reuses_receipt` | `true` | 再次执行返回原回执 |

最终必须为：

```json
{
  "external_calls": 1,
  "committed_entries": 1
}
```

`external_calls` 检查真实调用模拟下游 `append()` 的次数；`committed_entries` 检查实际写入行数。单看“没有重复行”不能证明恢复过程没有重复派发，因此需要同时观察这两个数。

## 先读状态和对象

`approval_id` 标识一项审批，`revision` 标识具体内容版本，`version` 是这一行的并发控制版本。三者不能混用。例如正文没变，但审批从 pending 变成 approved，行的 version 仍会增加。

```text
draft → pending → approved
                → rejected
                → expired

draft / pending / approved → superseded → 新 revision 的 draft
draft / pending / approved → cancelled（前提是尚未取得派发资格）
approved → expired（执行前发现审批期限已到）
```

实际操作单独保存：

```text
reserved → succeeded
         → failed（下游明确拒绝，确定没有写入）
         → unknown → 只读核实 → succeeded 或继续 unknown
```

审批消费通过 `consumed_operation` 记录，审批行仍保留 `approved`，以区分“人的决定”和“业务是否完成”。消费后不允许编辑或取消；取消接口不会声称已经撤回下游写入。

## 沿代码阅读一次执行链

1. `Context(tenant, subject, run)` 由可信宿主提供。Alice 属于 `tenant-a`，拥有 `A-100`；Bob 同租户但不是工单拥有者，Carol 属于另一个租户。两人都不能代替 Alice 批准。本实验没有登录认证；直接从客户端 JSON 构造 Context 并不安全。
2. `create(ALICE, body)` 建立 `draft`，冻结动作 `ticket.append`、正文、工单 ID 与版本、策略版本、主体、租户和 run。`submit()` 把准确版本变为 `pending`。`view()` 可供可信 UI 展示具体内容。
3. `decide(approval_id, revision, expected_version, decision_id, decision, ctx)` 接收 `approve` 或 `reject`。服务端读取当前身份和准确审批版本，在事务内用 version 条件更新。同一租户的相同 `decision_id` 和相同请求返回原响应；同一 ID 改变请求含义会被拒绝，不同租户的相同 ID 互不冲突。重复响应表示曾经接受了这项决定，不表示审批当前仍然有效。
4. `edit()` 将旧 revision 标记为 `superseded`，另建 draft。即使旧版本已经批准，新正文也要重新提交和批准。实验保留原期限，不因编辑自动延长。
5. `execute()` 再读当前成员资格、工单归属与版本、策略版本、有效期和内容摘要。检查通过后，在同一个本地事务中写入稳定 `operation_id` 的 `reserved` 记录、消费审批并写入派发审计，然后才离开事务调用下游。
6. 下游 `append()` 也检查工单版本，防止本地检查之后工单被其他人改写。下游以 operation ID 唯一约束和准确内容摘要保存回执。本实验的恢复策略仍然不重发，而是查询原 operation ID。
7. 首次执行和恢复核实使用同一个回执校验入口：核对操作 ID、准确工单 ID、允许的状态与字段集合，以及非空业务条目 ID。成功回执不能夹带拒绝原因。只有本模拟下游明确保证未写入的 `resource_changed` 拒绝格式才能成为确定失败，任意错误或内容冲突均保留 unknown。`fault="lost_receipt"` 模拟下游已经提交而本地回执丢失；再次 `execute()` 发现已有派发意图，返回 `unknown`。`reconcile()` 调用只读 `lookup()`，验证原回执后保存成功。
8. `fault="before_dispatch"` 模拟保存意图后、发送前退出。恢复无法仅从本地意图证明实际未发送，因此查询不到回执时仍保留 unknown。这个保守示例故意没有自动恢复发送路径；真实系统应依据下游提供的确定性合同设计核实和人工处理。

`execute()` 首次派发后，重复执行只返回原回执或要求核实，不能产生第二个 operation ID。同一个 run 有 `reserved/unknown` 操作时，也不能通过新建另一份审批或执行预先批准的另一份审批绕过核实。跨 run 的业务去重需要生产系统使用更稳定的业务意图身份；本实验没有实现。

内容摘要使用固定 JSON 序列化后计算 SHA-256，仅用于比较本实验中的准确快照。它不是签名，不证明是谁批准，也不防止拥有数据库写权限的攻击者同时篡改内容和摘要。

## 测试验证什么

测试覆盖草稿、拒绝、修改与旧标签页、同一决定重放和含义冲突、租户内决定 ID 作用域、过期、撤权、工单和策略变化、取消边界、重复执行、恢复核实、未发送意图的保守处理、同一 run 绕过防护、最小审计和参数校验。首次执行回执破损或相互矛盾时，不会把真实业务成功误报成成功或确定失败；必须等待独立核实。

并发测试使用线程、不同数据库连接和屏障，验证两类争用：

- 两个不同决定同时提交时只有一个胜出；同一决定同时重试返回相同响应。
- 第一个执行者已经取得派发资格但尚未提交时，第二个执行者只能看到 unknown，不会产生另一次下游调用。

另一个测试在本地检查后、下游写入前修改工单版本，验证下游前置条件能够阻止过时写入。它只覆盖工单版本竞争，不能证明跨系统撤权立即生效。

审计仅记录事件类型、审批 ID、revision、主体和 operation ID，不记录正文。审批快照和工单正文属于业务存储，生产仍需加密、访问控制、保留期和删除规则。

## 进入生产前还需补齐

| 实验边界 | 生产需要 |
| --- | --- |
| 可信宿主直接构造 Context | 真实 SSO/认证、请求防重放、CSRF 防护、可信服务身份、任务绑定与租户范围 |
| Alice 确认自己的具体写入 | 审批人策略、委派、双人批准、职责分离与审批权限复核 |
| 固定 run 与单一工单 | 稳定业务意图、多个任务与对象、跨 run 去重、批量审批边界 |
| 两份本地 SQLite 文件 | 生产数据库、迁移、备份、灾难恢复、队列、可靠通知与持久计时器 |
| 本地事务决定谁先取得派发资格 | 取消与撤权的明确生效点；更严格场景需要下游授权或版本前置条件 |
| 成功后按 operation ID 查回执 | 真实供应商的幂等、可核实性、回执完整性和数据可见性合同 |
| 对 unknown 保守阻断 | 支持人员处理入口、受控核实身份、超时升级与人工记录；不能换 ID 盲重试 |
| 预录的故障注入与对象重建 | 真实进程崩溃、网络丢包、数据库故障、供应商沙箱与端到端验收 |

本实验没有 HTTP、模型、Web UI、审批通知、双人批准、人员接管、Outbox、租约、fencing token 或真实外部 exactly-once 保证。直接在 Python 中调用 `decide()` 只是模拟可信审批入口；不能据此证明客户端消息真实可信。
