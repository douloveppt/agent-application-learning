# 第十三章 Multi-Agent 协作离线实验

用三个确定性的只读“专家”模拟企业 IT 助手：`knowledge` 查知识库，`diagnosis` 读诊断，`policy` 查工单策略。可信父层签发任务合同、预留预算、接收并核验结果，最后决定是否可以**准备工单草稿**。实验不创建工单，也不把子 Agent 的成功当成整个业务已经完成。

环境要求为 **Python 3.11+ 标准库**。没有网络、真实模型、API Key、外部企业系统、文件数据库或费用。专家是异步函数与固定数据，`asyncio.gather` 仅展示独立子任务并发返回；它不能证明真实模型的质量、速度或成本收益。

## 运行

在仓库根目录执行：

```bash
python3.11 examples/ch13-multi-agent/lab.py
python3.11 -m unittest discover -s examples/ch13-multi-agent -p 'test_*.py' -v
```

演示输出六行 JSON：

| `scenario` | 父任务 `outcome` | 关键含义 |
| --- | --- | --- |
| `normal` | `READY_DRAFT` | 必需与可选任务均成功，事实完整且无冲突，可以准备草稿 |
| `optional_failure` | `READY_DRAFT_DEGRADED` | 可选知识库任务失败；必需诊断、策略仍满足合同，明确降级 |
| `required_failure` | `BLOCKED` | 必需策略任务失败，不能以其他专家的成功补齐草稿字段与升级规则依据 |
| `conflicting_evidence` | `NEEDS_REVIEW` | 两份来源对同一诊断快照的单值主报错给出不同值，程序保留分歧 |
| `cancelled` | `CANCELLED` | 所有结果迟于取消到达，只记审计，不推进 |
| `stale_plan` | `WAITING` | 结果属于旧计划，只记审计，等待新计划的任务合同 |

正常场景再次投递同一知识库结果，`duplicate_delivery` 为 `DUPLICATE`，已接受记录和计量账本不变。取消或旧计划场景中，重复投递仍按迟到或过期事件处理。所有场景的写权限申请均得到 `capability_denied`。

## 父层怎样控制协作

`TaskContract` 由受信任的实验驱动签发，子 Agent 只能返回候选结果。合同包括：

```json
{
  "tenant_id": "demo-company",
  "run_id": "vpn-001",
  "task_id": "diagnosis",
  "attempt_id": "diagnosis-a1",
  "agent_id": "diagnosis-agent",
  "plan_version": 1,
  "input_digest": "由规范化输入计算的 SHA-256",
  "required": true,
  "capabilities": ["read_diagnostics"],
  "allowed_claims": ["primary_error"],
  "token_limit": 200
}
```

`accept()` 对结果信封做精确字段与类型检查，再匹配身份、任务、尝试、计划和输入摘要。这里只匹配消息正文与已签发的合同，**不能认证发送者真实身份**；生产环境还需要独立验证连接、凭证与授权。`status`、`findings`、`usage_tokens` 是候选数据；不能通过新增 `approved`、更换租户或修改角色自行改变控制事实。

`allowed_claims` 由可信角色注册表派生：`knowledge` 只能产出 `recommended_step`，`diagnosis` 只能产出 `primary_error`，`policy` 只能产出 `ticket_allowed`。结果越出角色产物合同得到 `RESULT_SCOPE_REJECTED`；即使引用确实存在、同属本租户与 Run，也不能让知识库专家代替策略或诊断专家交付。证据表由可信驱动提供，父层继续逐项检查 `evidence_ref` 的租户、Run、声明与值。已存在任务结果不能被另一份不同内容覆盖。

`outcome()` 采用预先确定的汇合政策：`diagnosis` 和 `policy` 必需，`knowledge` 可选；必需分支失败可立即阻断，否则等待三个分支均有终态，再检查结果。结果须包含同一诊断快照的主报错 `primary_error` 与 `ticket_allowed=true`；**准备工单草稿允许根因未知**。正常主报错为 `tls_handshake_timeout`，冲突演示由诊断专家返回两份可分析资料中的不同值，另一值为 `connection_refused`；这不是对根因作判断。知识库的 `check_certificate_validity` 只是候选检查步骤，不声称证书已过期或必须更新。

冲突检查仅针对合同声明为单值的 `primary_error`、`ticket_allowed`。多个兼容的候选检查步骤可以并存，不能把所有多值发现都当成矛盾。此处策略事实只允许准备草稿，**不代表已取得用户审批或外部写权限**。知识库失败时没有手册证据，后续实现草稿生成器时必须省略依赖手册的具体检查建议，不能仅展示“降级”状态却继续使用缺失内容。

## 预算预留与实际消耗

父预算默认为 900 个教学 token。每个子任务派发前预留 200，正常实际计量为 100；失败任务为 40。三个正常子任务结算后 `spent=300`、`reserved=0`。拒绝新的超额派发不会改变已消耗费用。

计量通过独立的 `Budget.settle(attempt_id, trusted_usage)` 接口进入；模拟 harness 提供真实用量，结果信封的 `usage_tokens` 只供诊断，无法修改账本。无论结果被接受、过期、取消或校验失败，都需要结算真实消耗。同一计量回执重复送达不重复扣费，冲突回执报错；若服务实际超出子任务上限，仍如实记账并返回 `CAP_EXCEEDED`，而不是把超额成本丢弃。

这套内存账本只在单线程实验中用串行调用模拟原子预留与结算；没有验证真实并发争抢，没有实现服务端 token 硬上限或分布式原子预算，不能作为生产费用控制器直接使用。失败场景使用预先构造的失败结果，没有模拟超时控制或取消竞态。

## 测试与能力边界

10 个测试覆盖六种父任务结果、局部成功不能冒充完成、身份维度匹配、Schema 与证据拒绝、真实证据也不能跨角色产物合同、重复与冲突结果、取消和过期后仍计费、写权限拒绝、父预算预留、可信计量与超额如实记账。

实验的证据是结构化、作用域固定的可信 fixture；引用核验只证明声明与 fixture 一致，不能验证自然语言文档的真实性、语义蕴含或抗提示注入能力。没有真实身份认证、细粒度文档 ACL、签名消息、加密、输出字节上限、持久状态、跨进程并发、队列重投、超时与重试调度、输入纠正后重规划执行、外部写入、审批、回执对账、A2A 或 MCP。计划升级后停在 `WAITING`，必须由扩展后的运行时重新签发任务，实验不会自动重算。

学习时可先改 `required` 政策或证据冲突，再观察父任务结论如何变化；接入真实模型前，应把这些合同检查保留在模型输出之外的程序层。
