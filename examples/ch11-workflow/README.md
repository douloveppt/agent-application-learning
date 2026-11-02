# 第十一章规划、路由与 Workflow 离线实验

本实验演示一条可检查的企业编排链：**模型只提出路由候选，程序完成准入；计划先经过静态校验，执行器再按依赖推进；失败、重试、部分结果和缓存复用都有明确语义。**

实验使用 Python 3.11+ 标准库，不调用网络、真实模型、数据库或企业系统，不需要 API Key，也没有写操作。完整讲解见[第十一章规划、路由与 Workflow](../../11%20%E8%A7%84%E5%88%92%E3%80%81%E8%B7%AF%E7%94%B1%E4%B8%8E%20Workflow.md)。

## 1. 运行实验

在仓库根目录执行：

```bash
python3.11 examples/ch11-workflow/lab.py
python3.11 -m unittest discover -s examples/ch11-workflow -p 'test_*.py' -v
```

主程序输出九个场景的最终状态。每次执行顺序和结果固定，便于逐项比较：

| 场景 | 结果 | 观察重点 |
| --- | --- | --- |
| `normal` | `completed / delivery_succeeded` | 知识查询与工单查询构成第一波，AND 汇合后才能生成交付结果 |
| `low_confidence` | `awaiting_input / confidence_below_threshold` | 低置信路由不能强行分派 |
| `missing_input` | `awaiting_input / required_input_missing` | `combined` 缺少工单号时，程序指出应补哪个字段 |
| `invalid_route` | `rejected / route_not_allowed` | allowlist 以外的路由在计划和工具执行前被拒绝 |
| `cyclic_plan` | `rejected / plan_contains_cycle` | 有环计划不会进入执行器 |
| `branch_failure` | `partial / delivery_not_completed` | 知识查询重试耗尽，独立工单分支仍成功；汇合节点被阻塞 |
| `retry_then_success` | `completed / delivery_succeeded` | 可重试读错误在节点上限内恢复 |
| `cache_reuse` | `completed / delivery_succeeded` | 第二次独立 Run 复用两项成功读结果，但仍重新检查 Scope |
| `input_changed` | `partial / knowledge_evidence_missing` | 客户端版本由 5.2 改成 5.3 后签名改变；新查询成功返回空集合，但没有证据时不能完成答复 |

`completed` 只表示本实验的交付节点成功生成了结构化结果。它不证明 VPN 已恢复，也不证明文字回答已经通过事实支持和业务效果评测。`partial` 会保留已成功的独立分支结果，但 `output` 仍为 `None`，因此不会把残缺结果伪装成完整交付。

## 2. 从路由候选到可执行计划

入口函数是 `run_workflow(scope, request, candidate)`：

```python
from lab import Scope, TaskRequest, route_candidate, run_workflow

result = run_workflow(
    Scope(tenant_id="demo-company", user_id="employee-a"),
    TaskRequest(
        issue="VPN E104",
        confirmed_os="Windows",
        confirmed_client_version="5.2",
        ticket_id="IT-100",
    ),
    route_candidate("combined", confidence=0.92),
)

assert result["status"] == "completed"
assert result["waves"] == [["knowledge", "ticket"], ["deliver"]]
```

这里有两类来源不同的数据：

- `Scope` 代表宿主服务注入的租户和用户身份。模型候选与用户正文都不能改写它。真实系统仍需从认证会话、委托关系和业务 ACL 得到这些值。
- `TaskRequest.confirmed_os` 与 `confirmed_client_version` 代表用户已经确认过的任务输入。它们比从自由文本中猜出的值可靠，但不是设备遥测或终端事实；若业务需要证明实际环境，还要接可信资产系统或设备探针。

模型候选只能包含 `route`、`confidence` 和 `rationale` 三个字段。`admit_route` 先严格检查结构，再应用程序规则：

| 路由 | 必要任务输入 | 参考计划 |
| --- | --- | --- |
| `knowledge` | 已确认操作系统、客户端版本 | 查知识 → 生成答复 |
| `ticket_status` | 工单号 | 查本人工单 → 生成答复 |
| `combined` | 上述全部字段 | 查知识 ╲<br>查本人工单 ╱ → AND 汇合 → 生成答复 |
| `clarify` | 无固定字段 | 暂停分派，向用户补问 |

候选结构非法或路由不在 allowlist 中会抛出 `CandidateError`。候选合法但输入不完整、置信度低于阈值或主动请求澄清时，返回 `clarify` 决策，不创建可执行计划。置信度只参与路由准入，不能授予权限。

## 3. 计划为何要在执行前校验

`build_plan` 生成教学用的确定性参考计划。每个 `Node` 都显式声明：

```text
id + capability + depends_on + inputs + max_attempts
```

`validate_plan(plan, request)` 在任何能力调用前检查：

1. 节点 ID 唯一，依赖存在，没有自依赖和环。
2. 能力位于 allowlist，并与当前路由匹配；交付节点只能是唯一的 `compose_answer`。
3. 请求字段和上游结果通过 `InputRef` 显式连接；知识查询和工单查询的每个输入槽固定绑定到对应请求字段，不能改接步骤结果或其他请求字段；交付节点引用某个上游时，必须声明对它的直接依赖。
4. 每个节点都能沿依赖关系到达交付节点，拒绝不会影响结果的游离步骤。
5. 节点数、单节点尝试次数和计划总尝试数都在硬上限内；只有只读能力允许设置重试。
6. 计划引用的请求字段确实存在。缺少输入应回到澄清，不让执行阶段猜测或用空值继续。

静态校验解决的是“这个计划能否安全进入调度”，不证明计划能完成用户目标。真实系统还需要按租户、工具版本、费用、期限、数据分类和业务政策检查计划。

## 4. 就绪波次与 AND 依赖

`Executor` 每轮先找出所有依赖均成功的节点，把它们记录为一个“就绪波次”，然后按计划中的稳定顺序逐个执行：

```text
波次 1：knowledge    ticket
            \        /
             \      /
波次 2：      deliver
```

波次表达“这些节点此时都具备执行条件”，方便学习 DAG 调度和并行机会。实验仍是单线程串行执行，**没有实现真实并行、线程安全、背压、并发额度或完成顺序竞态**。

依赖采用 AND 语义：`deliver` 的两个上游都成功或命中缓存后才就绪。若知识查询失败，工单查询作为同一波中的独立节点仍会执行；随后 `deliver` 因依赖失败被标记为 `blocked`。失败只沿依赖边传播，不会无条件终止整张图。

只读节点会在自己的 `max_attempts` 内处理显式的 `RetryableReadError`。重试耗尽后保留 `error / read_timeout`，不会改写成“没有查到数据”。“查询失败”“查询成功但结果为空”“无权或不存在”是三类不同观察。

每个能力返回 `status=ok` 后仍要通过自己的结果契约：知识结果必须包含文档列表及完整的证据字段；工单结果的编号必须与请求一致，状态必须属于教学枚举；交付结果必须包含当前路由要求的字段，并与上游结果对应。缺字段、类型错误、错误工单号或伪造证据编号统一成为 `invalid_backend_result`，不会作为成功结果继续下传。

空文档列表是合法的查询观察，因此知识节点仍为 `succeeded`，也可以按当前数据版本缓存。但 `compose_answer` 需要知识证据时会返回 `knowledge_evidence_missing`，最终 Run 为 `partial` 且没有完整 `output`。这区分了“查询技术上成功”和“任务已有足够材料完成”。

## 5. 成功读缓存的边界

`ReadCache` 只保存通过能力级结果契约的成功只读结果；写入和命中读取都会执行同一校验。非法写入被拒绝，旧格式或损坏条目会被丢弃并按未命中重新查询。签名覆盖：

```text
data_revision + policy_revision + tenant_id + user_id + capability + resolved_inputs
```

因此，任务输入或 Scope 变化会产生新签名。缓存命中前，`SyntheticBackend.admit` 仍会重新检查当前 Scope；缓存不能绕过本次运行的准入。

本实验的 `data_revision=fixture-data-v1` 和 `policy_revision=fixture-policy-v1` 在一个缓存实例的生命周期内固定，合成资料也不会变化。这只是可复现夹具。生产缓存还要处理 TTL、源数据版本推进、权限撤销、政策更新、删除传播、容量限制和多实例一致性；不能把“参数相同”理解为业务事实永远不变。

下面的代码展示跨两个独立 Run 的复用。第二次 Run 仍重新经过路由和执行准入：

```python
from lab import ReadCache, Scope, SyntheticBackend, TaskRequest, route_candidate, run_workflow

cache = ReadCache()
backend = SyntheticBackend()
scope = Scope()
request = TaskRequest()
candidate = route_candidate("combined")

first = run_workflow(scope, request, candidate, backend=backend, cache=cache)
second = run_workflow(scope, request, candidate, backend=backend, cache=cache)

assert first["status"] == second["status"] == "completed"
assert second["steps"]["knowledge"]["status"] == "reused"
assert second["steps"]["ticket"]["status"] == "reused"
assert cache.hits == 2
```

## 6. 测试覆盖与生产边界

`test_lab.py` 的 34 个测试覆盖：路由 schema 与 allowlist、低置信澄清、缺输入澄清、计划 ID 和依赖、环检测、能力范围、节点与尝试预算、读取槽位来源绑定、直接数据依赖、交付输入完整性及来源能力、游离节点、请求字段缺失、正常两波执行、AND 汇合、独立分支继续、有限读重试、空证据交付门禁、知识／工单／交付结果契约、非法后端结果、成功读缓存、缓存污染拒绝与旧格式淘汰、缓存前重新准入、Scope 隔离、输入变化失效、数据与策略版本签名、失败不缓存和结果确定性。

这个实验没有实现以下生产能力：

- 没有真实 LLM、Prompt、结构化输出 SDK 或模型效果评测；路由候选由代码直接构造。
- 没有真实认证、ACL、委托、数据库行级隔离或并发撤权；`Scope` 和权限表都是测试夹具。
- 没有真实网络截止时间、异步任务、并行执行、队列、Worker 租约、持久状态、暂停恢复或跨进程事件。
- 没有写操作、审批、幂等键、未知写结果、对账或补偿；只读重试语义不能直接用于业务写入。
- 没有动态重规划。每个独立 Run 都从路由准入重新开始；输入变化只用于证明缓存签名失效，不代表生产重规划已经实现。
- 没有 OR 汇合、动态 fan-out、循环子图、子工作流或分布式调度。需要这些能力时，应先定义清楚汇合、失败和完成语义，再扩展计划模型。
- `compose_answer` 会检查结构化证据编号和工单字段是否来自上游，但不检查自然语言中的每项主张是否真的受到证据语义支持。

实验的价值在于让关键边界可以运行和反驳：模型候选不等于路由决定，计划不等于可执行计划，节点成功不等于 Workflow 完成，部分结果不等于完整交付，缓存命中也不等于跳过本次授权。
