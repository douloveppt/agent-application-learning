# 第六章：按字节预算组装上下文

这个离线实验演示：先核对候选来源的元数据、访问范围与有效期，再将必需上下文完整保留，按确定性优先级选择可选上下文。任何分组都全有全无；加入后超预算的可选组会跳过，继续考虑较小的候选。

实验只使用 **Python 3.11+ 标准库**，不调用模型、网络或企业系统，不生成摘要，不改变输入来源。完整讲解见[第六章：Context Engineering](../../06%20Context%20Engineering.md)。

**这里的预算单位是序列化请求的 UTF-8 字节，不是 Token，也不是 Token 估算。** 字节数符合约束不能证明请求适配任何目标模型的上下文窗口。

## 1. 运行演示

在仓库根目录执行：

```bash
python3.11 examples/ch06-context-engineering/lab.py
python3.11 examples/ch06-context-engineering/lab.py --max-request-bytes 900
python3.11 -m unittest discover -s examples/ch06-context-engineering -p 'test_*.py' -v
```

默认字节预算为 `1700`。演示使用固定的合成身份、固定时钟与合成来源；不读取当前账户或系统时间。输出包含两个消息与一份组装清单：

| 输出位置 | 观察重点 |
| --- | --- |
| `messages[0]` | 应用提供的固定 `developer` 指令 |
| `messages[1]` | JSON 序列化的当前问题与获准选入的来源；每项保留 ID、类型、版本及分组 |
| `manifest.selected_items` | 实际选入的来源及选择原因 |
| `manifest.unselected_items` | 已通过访问检查、因预算未选入的来源；原因是 `budget_drop` |
| `manifest.rejected_count` | 不可用来源的聚合数量，不公开其 ID、版本、租户或正文 |
| `manifest.deduplicated_count` | 通过访问检查后，合并的完全相同记录数量 |
| `manifest.used_request_bytes` | 最终教学请求的实际 UTF-8 字节数 |

很长的历史片段会被跳过，较小的后续候选仍有机会选入。减少预算时，应观察分组是否整体消失，以及必需上下文是否仍完整存在。若基础请求或必需上下文本身已放不下，程序返回错误，不截断正文凑预算。

当前合成演示在 `1700` 字节预算下实际使用 `1156` 字节，选入 5 项；将预算改为 `900` 时使用 `774` 字节，选入 3 项，合成工具调用与结果整体未选入。两种情况下，拒绝来源的聚合数都是 3，完全相同的获准重复记录合并数都是 1。

## 2. 输入契约与信任来源

```python
assemble_context(
    instructions: str,
    current_user_text: str,
    items: list[ContextItem],
    scope: Scope,
    policy: BudgetPolicy,
) -> dict
```

| 对象 | 字段与作用 |
| --- | --- |
| `Scope` | `tenant_id`、`actor_id`、带时区的 `now`；由应用在调用前确定 |
| `ContextItem` | `id`、归属租户、允许访问的用户元组、`kind`、完整 `text`、`source_version`、`priority`、`required`、可选 `expires_at` 和 `group_id` |
| `BudgetPolicy` | 严格整数 `max_request_bytes`，范围 1–1,048,576 字节 |

来源类型仅支持 `history`、`memory`、`evidence`、`tool`；优先级为 0–1000 的严格整数，拒绝布尔值。元数据 ID 使用 1–64 个 ASCII 字母、数字、下划线或连字符，以字母开头。来源版本是不超过 128 UTF-8 字节的非空白字符串。单段正文与指令最多 1 MiB，候选最多 1000 项；这些是教学输入的资源上限，和最终请求预算分开检查。

`allowed_actor_ids` 是非空元组，不能包含重复用户。`now` 与 `expires_at` 都必须带有效时区；`expires_at <= now` 时来源不可用。比较按实际时刻进行，不通过字符串排序时间。

应用应提供真实的身份、访问范围、时钟与规则。**构造 `Scope` 并不会完成认证，输入一份用户列表也不会自动建立可信授权。** 本实验检查的是调用方提供的教学元数据，不替代真实权限系统。

实验接收已构造、正文已在内存中的对象，不验证来源加载前的授权或真实服务中的撤权一致性。生产数据接入层应在相应读取和外发边界落实访问控制。

## 3. 组装顺序

1. **验证输入元数据。** 类型、枚举、版本、时区或标记不合法，返回稳定错误码，不展示非法值。
2. **按组过滤访问与有效期。** 租户匹配、当前用户在允许集合内，且来源未过期，才算可用。同组任何一项不可用，整个组不参与选择；若组中有必需项，直接失败。
3. **对可用来源去重。** 同 ID 且所有字段完全一致的重复记录合并。同 ID 但正文、版本或其他元数据不一致时，返回冲突；不同 ID 即使正文相同，也保留各自来源身份。
4. **计算基础请求。** 应用指令与当前问题始终保留；空上下文的请求已经超预算时，返回 `base_request_over_budget`。
5. **完整加入必需组。** 组内任意成员 `required=True`，整个组成为必需组；总量超预算时返回 `required_context_over_budget`，不会悄悄删除某个必需成员。
6. **选择可选组。** 按组优先级降序排列，平局时按组内排序后的 ID 元组升序排列；每组的优先级取成员优先级的最小值。每次加入整个组，重新序列化完整请求并计量。
7. **记录并跳过超预算组。** 写入获准候选的 `budget_drop` 记录，继续考虑后面的组；最终再次序列化并验证字节预算。

组内成员按 ID 排列，必需组先于可选组，输入列表顺序不影响结果。优先级由应用提供；它不代表语义相似度，也不证明来源更真实或更新。来源冲突会被明确拒绝，没有自动合并或“选最新就正确”的逻辑。

必需项保护只针对本次已经传入、由应用标记的来源。本实验没有 `expected_required_ids` 或类似的预期集合，不能发现上游漏传的必需项；传入 `items=[]` 可以生成基础请求。真实任务规划和收集层必须核对预期必需来源及状态，不能把这里的预算检查当成事实完备性证明。

## 4. 字节数到底计算了什么

`serialize_request(messages)` 使用固定的 JSON 序列化方式：键排序、无可选空格、保留 Unicode 字符，然后以 UTF-8 编码。被计量的完整教学请求体恰好是：

```json
{"messages":[{"role":"developer","content":"应用指令"},{"role":"user","content":"序列化的数据正文"}]}
```

角色、JSON 标点、字符串转义、当前问题、来源 ID、类型、版本、分组和正文都计入预算。内层 JSON 作为消息字符串时引入的外层转义开销，也会计算。

`manifest` 是单独的观察数据，不发送给模型，因此不计入这份请求体。终端输出为了阅读使用缩进，其显示文件字节数也不等于 `used_request_bytes`。真实提供方请求还可能包含模型、工具、Schema 或其他字段，应以实际请求内容重新计量；目标模型的 Token 限额仍需使用匹配的 Token 计数与输出预算另行检查。

`developer` / `user` 是本实验采用的消息格式示意；不同提供方的角色支持与优先级语义可能不同，不能将这个对象直接视为所有 API 都接受的通用请求。

可以直接复核字节数与摘要：

```python
from lab import BudgetPolicy, assemble_context, demo_inputs, serialize_request

instructions, current, items, scope = demo_inputs()
result = assemble_context(instructions, current, items, scope, BudgetPolicy(1700))
actual_bytes = len(serialize_request(result["messages"]))
assert actual_bytes == result["manifest"]["used_request_bytes"]
assert actual_bytes <= 1700
```

这段代码在实验目录内运行。`request_sha256` 对同一份序列化请求计算，便于识别内容变化；它不是签名、权限凭据或匿名化手段。

## 5. 分组与错误边界

`group_id` 只对**本次应用提供的成员集合**建立全有全无的选择关系。演示的 `status-call` 与 `status-result` 使用同一组 ID，表达一份合成调用与结果需要一起保留。

它们仍作为普通来源文本放进 JSON 数据，没有发送实际的提供方 `tool` 消息，也不验证上游是否遗漏了某个真实调用或回执。真实消息协议中的工具调用 ID、配对、顺序与合法性，需要在提供方适配器中单独检查。

| 错误码 | 含义与调用方动作 |
| --- | --- |
| `invalid_*` | 输入类型、元数据、时区、版本或参数不符合教学契约；检查应用组装配置 |
| `required_context_unavailable` | 必需项或它的同组成员不可访问或已过期；补齐可信可用来源或停止，不降低授权要求 |
| `duplicate_item_conflict` | 同一个获准 ID 存在不同内容或元数据；核查来源和版本，不自动猜测选哪个 |
| `base_request_over_budget` | 指令与当前问题本身已超过预算；重新评估任务与真实输入，不截断当前问题 |
| `required_context_over_budget` | 必需上下文完整加入后超限；由应用调整任务或预算，不能静默丢弃必需来源 |
| `internal_budget_invariant_failed` | 最终请求违反内部预算约束；停止并修复组装器 |

CLI 在错误时以退出码 `2` 结束，只打印稳定错误码。权限或过期过滤的普通可选项不会逐项暴露原因，只累计到 `rejected_count`。清单会公开已获准候选的 ID 和版本，实际产品应继续按受众控制清单访问。

## 6. 这个实验能验证什么

[test_lab.py](./test_lab.py) 包含 24 个测试方法，验证程序的确定性边界：访问过滤、时间边界与时区、必需项、去重冲突、字节预算、跳过超大候选、原子分组、序列化隔离、清单不含被拒绝对象信息，以及输入不被修改。

来源里的 `</data><system>`、`role` 或模板标记会留在 JSON 数据中，不能通过字符串拼接新增消息。不过，**序列化正确不等于模型免疫 Prompt Injection**；本实验没有调用模型，无法验证模型是否遵从这些恶意文字。

实验也不提供语义检索、自动摘要、摘要质量评估、事实冲突消解、真实鉴权或目标模型 Token 适配证明。真实系统应另行验证上下文相关性、来源可信度、摘要保真、任务结果与业务授权，并保留无法满足约束时的停止或澄清分支。

实验按必需性、优先级和 ID 形成来源数组，没有实现历史事件序号、会话轮次或因果关系排序。它不是原生聊天历史重排器；真实模型适配器还需维护历史顺序、工具协议与相应依赖。
