# 第 21 章实验：模型可以提议，执行器必须守住边界

这个实验把模型提议、可信身份、资源授权和真实写入拆开。攻击文档可能让模型提出危险动作；即使模型已经提出了这些动作，执行器仍应拒绝越权、未知工具和未获具体批准的写入。

**实验使用预录的候选调用，不调用模型，也不实现提示注入检测器。** 拒绝 `export.upload` 能证明它不在执行器的能力集合中，不能证明模型识别了攻击文档。全部工单、用户和内部 canary 都是合成数据。

## 运行

需要 Python 3.11 或更高版本，不安装第三方包、不访问网络、不读取系统凭证。

在仓库根目录运行：

```bash
python3.11 examples/ch21-guardrails/gate.py demo
python3.11 -m unittest discover -s examples/ch21-guardrails -v
```

也可以进入本目录运行：

```bash
python3.11 gate.py demo
python3.11 -m unittest -v
```

每次运行创建一个全新的 SQLite 内存数据库，退出即销毁。实验不会写入真实业务系统。

## 先读数据和工具合同

| 工单 | 租户 | 拥有者 | 允许谁读取或补充 |
| --- | --- | --- | --- |
| `A-100` | `tenant-a` | `alice` | 当前仍有效的 Alice |
| `A-200` | `tenant-a` | `bob` | 当前仍有效的 Bob |
| `B-100` | `tenant-b` | `carol` | 当前仍有效的 Carol |

原始工单带有 `secret_internal` 字段；读取工具固定返回 `id`、`title`、`status`、`version`。所有用户均无读取内部字段的工具能力。

模型只允许提出以下两种候选，所有未知字段均被拒绝：

```python
{
    "tool": "ticket.read",
    "arguments": {"ticket_id": "A-100"}
}

{
    "tool": "ticket.append",
    "arguments": {
        "ticket_id": "A-100",
        "body": "VPN reconnect still fails.",
        "expected_version": 1
    }
}
```

候选不能提供 `tenant_id`、`subject_id`、`approved`、`budget` 或任意 URL。工单 ID 使用本实验的固定格式，正文最多 500 个字符，版本要求真正的正整数；Python 的 `bool` 虽然是 `int` 的子类，在这里也不能冒充版本号。

`Context(tenant_id, subject_id, run_id)` 由可信宿主构造。这个对象本身没有认证能力；真实 HTTP 服务必须先完成认证、租户成员校验和任务绑定，不能直接用客户端字段来创建它。冻结 dataclass 只避免意外修改，不会让其内容自动可信。

## 观察演示结果

`demo` 输出 JSON；`outcomes` 中应出现以下结果。UUID 会因运行而变化。

| 场景 | 预期结果 | 要证明的约束 |
| --- | --- | --- |
| `normal_read` | `allowed` / `read_own_ticket` | 正常路径可用，输出字段受限 |
| `malicious_document_proposes_upload` | `denied` / `unknown_tool` | 文档不能给执行器增加上传能力 |
| `same_tenant_other_owner` | `denied` / `resource_not_allowed` | 同租户也不等于可以读同事工单 |
| `cross_tenant` | `denied` / `resource_not_allowed` | 替换资源 ID 不能跨租户 |
| `model_invents_authority` | `denied` / `invalid_fields` | 模型输出的身份和批准无效 |
| `append_without_approval` | `waiting_approval` / `approval_required` | 没有具体审批就没有写入 |
| `changed_body_after_approval` | `denied` / `approval_binding_changed` | 审批不能覆盖改过的内容 |
| `approved_exact_action` | `allowed` / `append_committed` | 只有准确获批并仍被授权的动作写入 |
| `replay_approval` | `denied` / `approval_consumed` | 一次审批不能写两次 |

末尾应有：

```json
{
  "committed_entries": 1,
  "audit_contains_canary": false
}
```

这两个字段分别检查“最终实际写入一次”和“审计未记录合成内部标记”。它们不代表通用外泄检测能力。

## 沿代码读一次真实执行链

1. `_reserve_attempt()` 从服务端 `runs` 表原子扣除尝试预算，候选被拒绝也计入预算。每个 Run 最多 12 次；模型不能通过改参数重置它。
2. `validate()` 校验工具名、准确字段集合、类型和长度，并复制候选快照。JSON Schema 合法与资源授权是两个步骤。
3. `_authorize()` 在本地事务中读取当前策略、成员状态、工单拥有者、租户、业务状态和资源版本。未找到与无权限统一返回 `resource_not_allowed`。
4. 读取走固定字段投影；补充说明在无审批时进入等待，产生不了成功回执。
5. `approve()` 模拟可信审批服务已经展示准确动作并收到合法用户同意。它不在模型可调用的工具目录中；实验直接调用此函数，不模拟真实审批 UI。
6. `_check_approval()` 检查审批绑定的主体、租户、任务 `run_id`、动作、资源、候选摘要、资源版本、策略版本、期限和消费状态。同一位用户的另一个任务也不能消费这份批准。批准不能代替当前授权。
7. 审批消费、`entries` 模拟业务写入、工单版本更新和成功审计在同一个 SQLite 事务内提交。模拟写入异常时全部回滚；预算消耗保留。

对审批内容使用固定 JSON 序列化后计算 SHA-256，目的是比较本实验中的准确候选。摘要不是签名，也不能证明批准来自谁；批准事件的可信性依赖审批服务及其受控数据库。

审计只记录任务、主体、租户、已验证的工具与资源 ID、决策原因、策略版本和操作 ID。正文、原始恶意文本、审批凭据和内部字段均不写入安全审计。`entries.body` 是本地模拟业务数据，和审计分开存放。

## 故障注入与测试

测试覆盖正常读取和具体审批写入，以及横向越权、跨租户、未知工具、身份覆盖、严格类型、正文长度、过期与重放、跨用户和同主体跨任务审批、撤权、资源版本变化、策略版本变化、策略不可用、工单关闭、预算耗尽和日志字段。

`test_business_failure_rolls_back_consumption_and_write` 通过 SQLite trigger 在写入时注入异常。它检查审批仍未消费、业务行没有增加、工单版本未变化，并且没有伪造 `append_committed` 成功审计。

可以继续做三组观察：

- 在发放审批之后撤销 Alice 的成员资格；即使审批仍在有效期内，也应拒绝写入。
- 在发放审批之后修改正文、工单版本或策略版本；系统应要求重新经过相应准入和审批，而非自动沿用。
- 连续提交 12 次破损候选，第 13 次即使是合法读取也应得到 `budget_exhausted`。生产服务还需在入口限制请求速率与并发，防止审计、队列和无效请求本身耗尽资源。

## 实验范围与生产补齐项

| 实验实现 | 生产还需要 |
| --- | --- |
| 可信宿主直接构造 Context | 真实认证、可验证的服务调用、任务委托与租户绑定 |
| 内存 SQLite、单连接本地事务 | 持久数据库、并发控制、恢复与撤权传播；重启不重置预算 |
| 本地模拟写与审批消费原子提交 | 外部派发账本、稳定操作身份、幂等合同、未知结果核实 |
| 工具/字段允许列表、当前授权 | 下游资源服务的独立校验、网络与文件系统隔离 |
| 固定输出字段投影 | 正文、引用、附件、URL、渲染链路与流式内容的检查 |
| 合成 canary 检查、最小审计 | 经校准的 DLP、脱敏、审计访问控制、保留与完整性保护 |
| 服务端固定尝试预算 | 持久 token/费用/并发/时间/外发预算及多 Agent 汇总 |
| 预录攻击候选 | 模型级攻击评测、正常任务误拒绝评测和真实供应商适配 |

本实验没有 HTTP 请求解析、SSRF 防护、命令执行、容器、OAuth、真实密钥、真实模型、通用 DLP、完整红队体系或分布式执行。字段投影也不会检查字符串内部是否含秘密；例如 `title` 的内容仍然需要生产输出策略。

**本地事务的原子性不能推导出外部 exactly-once。** 真实工单系统可能已经写入而本服务尚未取得回执；这种状态必须保留并核实，不能因审批已消费、超时或进程重启就自动再发一次。
