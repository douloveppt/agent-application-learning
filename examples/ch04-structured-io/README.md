# 第四章：结构化候选的分层校验实验

这个离线实验把一段“模型返回的 JSON”校验为 IT 工单**候选草稿**。它帮助你观察 JSON 解析、字段约束、跨字段规则和证据检查分别能拦住什么。完整讲解见[第四章：结构化输入输出](../../04%20%E7%BB%93%E6%9E%84%E5%8C%96%E8%BE%93%E5%85%A5%E8%BE%93%E5%87%BA.md)。

实验使用 **Python 3.11+、Pydantic v2 与 unittest**，已在 Python 3.11.15、Pydantic 2.13.3 验证。运行时不调用模型、网络或外部企业系统，也不写数据库。安装依赖需要访问你配置的 Python 包源。

## 1. 准备环境并运行

在仓库根目录执行。下面把虚拟环境放在仓库外，避免误提交依赖文件：

```bash
python3.11 -m venv /tmp/agent-ch04-venv
source /tmp/agent-ch04-venv/bin/activate
python -m pip install -r examples/ch04-structured-io/requirements.txt

python examples/ch04-structured-io/lab.py
python -m unittest discover -s examples/ch04-structured-io -p 'test_*.py' -v
```

`lab.py` 输出三个合成案例：

| 案例 | 预期结果 |
| --- | --- |
| `ready` | 返回字段完整、来源检查通过的候选；`next_step` 提示展示草稿并等待业务校验与授权 |
| `needs_clarification` | 使用未提供人数的独立用户原文，允许 `affected_users: null`；`next_step` 提示补问，`fields_to_clarify` 单独列出需补字段 |
| `extra_field` | 模型试图添加 `tenant_id`，被 `schema_invalid` 拒绝 |

这里的 `ready` 只表示“可交给下一道业务关卡”，不会创建工单，也不会证明工单已获批准。演示会打印固定的合成候选供观察；生产日志不应照抄这种完整正文输出方式。

## 2. 先看输入与输出契约

入口函数是：

```python
validate_candidate(result: ProviderResult, context: SourceContext) -> TicketCandidate
```

`ProviderResult` 是应用的模型适配器归一化后的结果；`SourceContext` 应由应用在认证、来源授权后构造。两者不能全部交给模型生成：

| 对象 | 字段 | 来源与作用 |
| --- | --- | --- |
| `ProviderResult` | `status`、`payload` | 适配器观察实际响应终态，再提供正文；`status` 不是正文里的自报状态 |
| `SourceContext` | `tenant_id`、`actor_id`、`sources` | 服务端提供当前身份及本次请求可使用的来源集合 |
| `EvidenceSource` | `tenant_id`、`text` | 可引用来源的归属与文本；来源 ID 是 `sources` 的键 |
| `TicketCandidate` | 见下表 | 模型生成、经过检查的候选数据 |

候选的每个键都必须存在；允许 `null` 的字段也不能直接省略：

| 字段 | 类型与约束 | 语义 |
| --- | --- | --- |
| `schema_version` | 固定字符串 `"1.0"` | 本实验契约版本 |
| `status` | `ready` / `needs_clarification` | 字段完整，或需要补问；两者都不代表已执行 |
| `title` | 字符串或 `null`；1–80 个 Python 字符，不能全为空白 | 拟定的工单标题 |
| `category` | `vpn` / `email` / `device` / `other`，或 `null` | 候选问题分类 |
| `affected_users` | 严格整数或 `null`；1–100,000 | 受影响人数；拒绝字符串、布尔值和浮点数 |
| `evidence` | 最多 8 项；每项为 `source_id`、`quote` | 引用允许使用的来源；`ready` 至少有一项 |
| `missing_fields` | `title` / `category` / `affected_users` 的不重复列表 | 必须精确对应值为 `null` 的业务字段 |

`source_id` 长度为 1–64 个 Python 字符，`quote` 为 1–1,000 个 Python 字符，均不能全为空白。所有候选对象与嵌套证据对象都拒绝额外字段。

## 3. 沿代码观察每一道关卡

阅读 [lab.py](./lab.py) 时，从 `validate_candidate` 入口向下追踪：

1. **响应终态**：只有 `completed` 可以继续。拒绝、截断、取消或错误，即使附带一份看起来完整的 JSON，也不能通过。
2. **解析与资源限制**：解析前检查正文最多 16 KiB UTF-8 字节；解析时拒绝重复键、`NaN`、`Infinity` 与浮点表示溢出，并捕获解码器的 `RecursionError`。解析完成后，迭代遍历结果，检查最多 16 层 JSON 嵌套与字符串 Unicode。16 层限制是在解析后执行，不能把它当成解析器内部的深度上限。JSON 前后添加解释或 Markdown 围栏也会解析失败。
3. **字段契约**：Pydantic 使用 `strict=True` 与 `extra="forbid"`，检查必填键、类型、枚举、长度、数值范围和额外字段。
4. **跨字段一致性**：`missing_fields` 与实际空值一致；`ready` 无缺失字段且有证据；`needs_clarification` 必须确实缺少业务信息。
5. **来源与引文**：来源必须在服务端允许集合内，租户必须一致，引用文字必须出现在该来源文本中。

通过后返回 `TicketCandidate`。**当前用户能否创建工单、支持队列是否开放、是否已有覆盖这些字段的授权，都不在这个函数中得到证明。** 这些检查应在接下来的业务关卡执行，并在真实写入边界重新核实需要保持的条件。

### 修改一个案例，观察失败位置

进入实验目录后可以运行以下代码：

```bash
cd examples/ch04-structured-io
python - <<'PY'
import json
from lab import (
    GateError, ProviderResult, demo_context, demo_payload, validate_candidate,
    demo_clarification_context, demo_clarification_payload,
)

data = demo_payload()
data["affected_users"] = "3"  # 字符串不是整数；不要自动猜测并转换

try:
    validate_candidate(ProviderResult("completed", json.dumps(data)), demo_context())
except GateError as error:
    print(error.code, error.path, error.retryable)

# 另一个独立输入只说 VPN 连不上，没有提供受影响人数。
draft = validate_candidate(
    ProviderResult("completed", json.dumps(demo_clarification_payload())),
    demo_clarification_context(),
)
print(draft.status, draft.affected_users, draft.missing_fields)
PY
```

第一行预期输出 `schema_invalid $.affected_users True`；第二行输出 `needs_clarification None ['affected_users']`。后者对应独立的用户原文“VPN 连不上，请帮我准备一个 IT 工单。”，没有人数信息，因此用 `null` 与 `missing_fields` 表达需要补问。

## 4. 稳定错误与恢复方式

`GateError` 只公开 `code`、`path`、`retryable`，不会将非法字段值拼入错误消息。未知字段名也会被替换成 `<unknown>`。实验没有自动重试循环。

| 错误代码 | 含义 | 调用方下一步 |
| --- | --- | --- |
| `response_incomplete` | 模型响应未完整完成 | 在预算允许时有限重生成候选 |
| `model_refused` / `response_cancelled` | 模型拒绝或操作取消 | 结束当前分支或交给用户处理 |
| `provider_error` / `response_status_invalid` | 适配器报告错误或未知终态 | 按实际提供方错误及适配器情况分类，不盲目重试 |
| `missing_payload` | 完成响应没有可处理的正文 | 检查适配器与响应类型 |
| `payload_too_large` | 正文超过教学上限 | 缩小输出契约或调整经评估的预算 |
| `invalid_json` / `duplicate_key` / `non_finite_number` / `json_too_deep` / `invalid_unicode` | 解析或数据表示不被接受 | 有限重生成候选；不靠字符串补丁伪造合法数据 |
| `schema_invalid` | 必填、类型、枚举、范围或字段白名单失败 | 按稳定路径定位；先排查契约版本与配置，再考虑重生成 |
| `missing_fields_mismatch` / `ready_has_missing_fields` / `ready_requires_evidence` / `clarification_requires_missing_fields` | 候选状态与字段互相矛盾 | 修复候选；真实缺信息时转入澄清 |
| `evidence_not_allowed` | 来源不存在于允许集合，或租户不一致 | 停止该候选，核查来源范围与授权；不会建议放宽访问 |
| `evidence_quote_mismatch` | 引文未出现在给定来源中 | 核查来源或有限重生成候选 |

`retryable=True` 仅表示**可能适合重生成候选**；还需调用方检查总次数、总时限和错误原因。它不表示自动重试、不表示重新发送外部写操作，也不允许绕过拒绝或授权失败。

## 5. 导出 Schema，并理解其边界

在仓库根目录运行：

```bash
python examples/ch04-structured-io/lab.py --schema
```

该命令从 Pydantic 类型生成本地 JSON Schema，包含必填键、额外字段限制、nullable、长度和数值边界等声明。它**不完整表达** Python 中的非空白检查、跨字段状态规则或来源校验。把模型输出约束到 Schema 后，这些应用检查仍需执行。

提供方支持的 JSON Schema 通常有各自的子集与限制。导出的 Schema 没有在本实验中向任何提供方提交；接入时应由适配器核对并转换，应用侧保留完整契约。JSON Schema 的 `integer` 按数值语义描述整数；本实验 `json.loads` 后的严格 Pydantic 校验还会拒绝 `3.0`，不能假设所有校验器对这种输入完全一致。

## 6. 测试能证明什么

[test_lab.py](./test_lab.py) 包含 34 个测试方法，覆盖正常候选、nullable 与缺失键、严格类型与边界、额外字段、跨字段矛盾、伪造引文、跨租户来源、重复键、非有限数、截断、响应终态、字节与深度限制，以及公开错误中的数据泄漏边界。

这些测试验证程序是否按契约拦截输入，**没有评测模型实际提取质量**。两个单独的能力边界测试展示了预期局限：原文已说“影响 3 位同事”，模型却输出 `null`，基础契约检查仍可能通过；`test_quote_presence_does_not_prove_affected_users` 则保留这段真实引文，将人数填为 `300`，同样可以通过。前者说明校验器无法证明模型已提取所有可用信息，后者说明引文出现并不证明它支持所填字段。实际项目还需用业务样本评测提取准确率、关键字段证据、澄清是否合理，以及人工确认后的最终业务结果。

## 教学边界与继续阅读

16 KiB 正文、16 层 JSON 嵌套、8 项证据都是本实验的教学上限，实际应用应依据任务、攻击面和容量评估设定。它们不能代替 API 网关的读取限制、超时、并发与租户资源配额。

`SourceContext` 只是表达信任边界的数据对象，构造这个对象并不会完成认证或授权。实验仅检查当前传入来源的租户和引文，未实现会话、来源过期、按用户细分的访问控制、业务状态校验、审批绑定、外部写入、幂等或真实审计系统。相关工程职责见[工具与 Function Calling](../../14%20Tools%20%E4%B8%8E%20Function%20Calling.md)、[身份、权限与多租户](../../20%20%E8%BA%AB%E4%BB%BD%E3%80%81%E6%9D%83%E9%99%90%E4%B8%8E%E5%A4%9A%E7%A7%9F%E6%88%B7.md)和[可靠性与副作用一致性](../../26%20%E5%8F%AF%E9%9D%A0%E6%80%A7%E4%B8%8E%E5%89%AF%E4%BD%9C%E7%94%A8%E4%B8%80%E8%87%B4%E6%80%A7.md)。
