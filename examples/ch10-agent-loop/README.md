# 第十章 Agent 执行循环离线实验

本实验演示：**模型提出候选，程序决定是否接受、是否执行、是否继续以及怎样结束。** 使用 Python 3.11+ 标准库，不调用网络、模型、数据库或企业系统，不需要 API Key。

企业 IT 助手的固定教学任务是：找到适用于 Windows 5.2 / VPN E104 的知识依据，查询本人工单 IT-100，然后返回建议、证据编号和观察到的工单状态。只有两个合成只读工具：`search_knowledge` 与 `get_my_ticket`。

完整讲解见[第十章 Agent 执行循环与运行机制](../../10%20Agent%20%E6%89%A7%E8%A1%8C%E5%BE%AA%E7%8E%AF%E4%B8%8E%E8%BF%90%E8%A1%8C%E6%9C%BA%E5%88%B6.md)。

## 1. 运行与观察

在仓库根目录执行：

```bash
python3.11 examples/ch10-agent-loop/lab.py
python3.11 -m unittest discover -s examples/ch10-agent-loop -p 'test_*.py' -v
```

命令输出十个场景的状态、原因、模型回合数、工具派发数，以及正常运行的事件序列。

| 场景 | 状态与原因 | 模型回合／工具派发 | 观察重点 |
| --- | --- | --- | --- |
| `normal` | `completed / structured_contract_satisfied` | 3／2 | 两次工具观察之后才能通过结构化完成检查 |
| `early_final_repaired` | `completed / structured_contract_satisfied` | 4／2 | 提前完成被拒，拒绝也消耗一次模型回合 |
| `unsupported_final` | `stopped / rejection_limit` | 2／0 | 连续提出无依据的完成候选会停止 |
| `repeated_call` | `stopped / repeated_observation_without_progress` | 3／3 | 首次得到结果后，又两次得到相同动作与结果指纹 |
| `batch_budget` | `stopped / tool_batch_exceeds_remaining_budget` | 1／0 | 剩余配额只有一次，两次调用的批次整体不执行 |
| `denied_ticket` | `stopped / rejection_limit` | 3／1 | 查询他人工单得到通用拒绝观察，不能据此宣称完成 |
| `read_timeout` | `stopped / rejection_limit` | 4／2 | 搜索超时产生错误观察，缺少知识证据的完成候选被拒 |
| `cancel_boundary` | `cancelled / cancel_requested` | 1／1 | 完成第一次工具调用后，在下一安全边界停止 |
| `model_budget` | `stopped / model_turn_limit` | 1／1 | 尚可调用工具，也不能突破模型回合上限 |
| `untrusted_text` | `completed / structured_contract_satisfied` | 3／2 | 资料中的指令文字不会成为服务端身份或工具权限 |

`completed` 仅表示本实验的结构化契约满足。它不表示 VPN 已恢复，也不表示自然语言答案已经通过事实与语义评测。

## 2. 跟踪一次受控闭环

在实验目录启动 Python，再运行：

```python
from lab import Runtime, Scope, ScriptedModel, normal_script

runtime = Runtime(Scope("demo-company", "employee-a"), ScriptedModel(normal_script()))
result = runtime.run()
assert result["status"] == "completed"
assert result["model_turns"] == 3
assert result["tool_calls"] == 2
assert [o["call_id"] for o in result["observations"]] == ["c1", "c2"]
print(result["events"])
```

逐步对照 [lab.py](./lab.py)：

1. `Runtime` 从可信应用代码接收 `Scope`、`Task` 和 `Limits`。模型不能在工具参数中提供 `tenant_id` 或 `user_id`；额外字段会被拒绝。
2. 每轮先检查取消和模型回合预算，再向 `ScriptedModel.next(observations, feedback)` 请求候选。`feedback` 是程序形成的拒绝原因；`observations` 是已执行工具的观察。这里的模型按预先编写的脚本返回候选，不进行推理，也不验证模型会不会利用反馈改善。
3. `validate_candidate` 严格检查候选类型、精确字段集合、字符串、工具名称和调用 ID。一个批次中的所有调用先完成检查，再判断整批是否放得进剩余工具配额。无效批次和超额批次都不会先执行其中一部分。
4. 工具真正执行时仍检查可信租户与所有权。语法合格的 `get_my_ticket(IT-200)` 会得到 `not_found_or_forbidden`；参数校验本身不会赋予业务权限。
5. 每个已派发调用产生带 `call_id` 的观察。连续出现相同动作与结果指纹时，更新无进展计数；首次出现新指纹会清零。该启发式不能判断语义进展，也不能阻止不断改变参数的循环，因此仍需要硬预算。
6. `verify_final` 核对：引用 ID 来自本次已观察且适用于目标主题的证据，目标工单为 IT-100，候选状态与查询观察一致。完成候选不合格时产生反馈，达到拒绝上限才停止；合格时返回结构化结果。

工具调用次数在派发前增加，失败与超时同样消耗次数。模型回合数包含工具提议、被拒候选和最终完成候选；二者不能混成一个步数。

## 3. 改动三个条件，观察边界

### 批量调用不会绕过剩余配额

```python
from lab import Limits, batch, tool_call

candidate = batch(
    tool_call("a", "search_knowledge", query="VPN E104 Windows 5.2"),
    tool_call("b", "get_my_ticket", ticket_id="IT-100"),
)
result = Runtime(Scope(), ScriptedModel([candidate]),
                 limits=Limits(max_tool_calls=1)).run()
assert result["reason"] == "tool_batch_exceeds_remaining_budget"
assert result["tool_calls"] == 0
```

这里采用整批准入策略，便于解释配额与部分执行。真实项目也可以定义部分准入，但必须返回逐调用的准确结果，并处理依赖关系。

### 取消会记录尚未派发的调用

将上例额度改为 2，并增加 `cancel_after_tool_calls=1`。第一个工具执行后，运行在第二个调用派发前结束。返回观察中，第二个调用仍按原 `call_id` 记录 `status=not_executed`，工具计数为 1。这样可以区分“没有执行”和“执行失败”。

这是进程内的确定性安全边界夹具，**没有真实异步取消、远端请求中断或取消竞态**。它也没有使用任何供应商的原生工具消息协议；真正接入模型时，适配器应按所选接口组织调用／结果配对。

### 完成检查有明确能力边界

```python
from lab import final

script = [*normal_script()[:2], final(answer="月球是奶酪。")]
assert Runtime(Scope(), ScriptedModel(script)).run()["status"] == "completed"
```

这个刻意保留的反例说明：证据编号存在、工单状态匹配，仍不能证明文字受到证据支持。上线前还需要检查重要结论、适用条件、引用支持，以及真实模型的任务效果。

## 4. 测试覆盖与生产边界

[test_lab.py](./test_lab.py) 的 19 个测试验证正常完成、提前完成修复、拒绝上限、非法候选不派发、整批先校验、批次配额、模型预算、调用 ID 去重、所有权拒绝、缺失与无权的统一错误、只读超时、重复无进展、变化结果、取消边界、未执行调用配对、最终结构化事实、语义检查缺口，以及资料正文不能覆盖服务端身份。

实验没有实现以下能力，不能据此宣称已完成企业生产验证：

- `Scope` 和权限是可信测试夹具，没有真实认证、委托、ACL 查询、数据库隔离或并发撤权。
- 两个工具全部只读、串行、在内存中执行。超时通过显式抛出 `TimeoutError` 注入，不是网络截止时间；只读超时的处理不能套用到结果未知的写操作。
- 没有真实模型、上下文窗口、Token／费用计量、墙钟截止时间、流式增量解析、模型切换或调用重试。供应商异常和未预期执行异常也没有通用恢复机制。
- 没有持久化、检查点、租约、崩溃恢复、审批、幂等写入和对账。`Runtime` 实例只允许 `run()` 一次；重新创建实例不代表从旧进度恢复。
- `untrusted_text` 证明的是执行端权限不会从正文取得，脚本模型不会受提示注入影响，因此这个场景不能评测真实模型的注入抵抗能力。
- 指纹用来演示重复观察检测；结果文本略有变化可能被当成新观察。生产中需要结合业务版本、目标进度、错误类别和硬预算设计停止策略。
- 内存事件没有真实 Trace 后端、隐私分级、持久审计或生产指标。示例中的数字是可复现教学参数，不是 SLO 或推荐额度。
