# 第 30 章实验：发布组合、灰度门禁与安全回退

这个实验用企业 VPN 知识与工单助手，演示一个小型发布控制器怎样管理 **不可变发布组合 → 小流量准入 → 观察判断 → 扩量或停止 → 兼容性回退**。全部输入都是合成数据，使用 Python 3.11+ 标准库；没有网络、容器、模型调用、真实工单或生产发布。

## 运行

在项目根目录执行：

```bash
python3.11 examples/ch30-llmops/lab.py demo
python3.11 -m unittest discover -s examples/ch30-llmops -p 'test_*.py' -v
```

命令只向标准输出写 JSON，不生成文件或修改环境。`demo` 成功退出码为 `0`；参数错误由 argparse 返回 `2`。JSON 中出现 `stop` 或 `rejected` 是预期的教学结果，不代表实验命令运行失败。

报告关键部分如下，完整 SHA-256 由程序计算：

```json
{
  "gates": [
    {"decision": "hold", "reason": "need at least 40 unique teaching run observations", "samples": 5},
    {"decision": "promote", "reason": "all teaching thresholds passed", "samples": 40},
    {"decision": "stop", "reason": "unauthorized access or unknown write observed", "samples": 1}
  ],
  "assertions": {
    "offline_failure_did_not_activate": true,
    "new_tasks_use_old_release": true,
    "old_task_keeps_candidate_release": true,
    "unknown_write_dispatched_once": true,
    "incompatible_rollback_did_not_advance_generation": true
  }
}
```

## 按什么顺序观察

1. `vpn-r1` 和 `vpn-r2` 各有一个 `Release`。Manifest 同时记录代码、模型、Prompt、工具 Schema、状态读写版本、索引和策略。字段按稳定 JSON 规则编码，再计算 SHA-256；改变 Prompt 就改变发布摘要。这里的 `image-sha256-r1` 等字符串是教学占位符，没有指向真实镜像或验证内容地址。
2. `stage()` 先拒绝离线质量失败的证据，随后以 1000 basis points，也就是 10% 的任务分桶准入 `vpn-r2`。本实验把离线证据对象当作可信输入；没有实现证据签名、CI 身份或防伪造验证。
3. `bucket(tenant, task_id)` 对规范 JSON 数组做 SHA-256，得到 0–9999 的稳定桶号。首次准入把 `release_digest` 记在任务上；同一租户、同一任务后续保持此版本。10% 是哈希空间比例，有限样本实际比例可能不同。
4. 只有 5 条观察时得到 `hold`；40 条合成成功观察满足教学阈值，得到 `promote`。这 40 条观察属于独立合成运行；它们不是该长任务的真实遥测，也不代表真实生产收益。
5. 推广后，一个已经固定在 `vpn-r2` 的任务派发工单写入，随后模拟丢失回执，操作结果保持 `unknown`。这发生在上述合成观察之后，不会被倒填为推广时已知的成功证据。
6. 旧控制器携带过时的 `generation=1` 尝试回退时被拒绝。当前控制器使用 `generation=2` 回退，新任务采用 `vpn-r1`；原任务恢复时仍使用 `vpn-r2`。再次派发同一个 `operation_id` 被拒绝，写入派发次数仍为 1。
7. 环境改成旧版本不能读取的 `state-v2` 后，简单回退被拒绝，提示需要恢复方案；控制器代次不变。独立的安全样本则表明：即使只有一条越权或未知写入证据，也会得到 `stop`。

## 门禁口径

以下阈值完全用于教学，不能直接照搬到生产：

| 条件 | 决策 |
| --- | --- |
| 任意越权或未知写入观察 | `stop`，不等待最低样本数 |
| 少于 40 个不重复 `run_id` | `hold` |
| 成功率低于 95%、最近秩 p95 大于 2500ms、平均成本大于 40000 micro-USD（0.04 USD） | `stop` |
| 满足以上全部条件 | `promote` |

观察都必须绑定当前候选的完整摘要；混入旧发布数据或重复 `run_id` 会拒绝评估。`p95` 采用最近秩定义：升序排列后取第 `ceil(N × 0.95)` 个值。成本用整数 micro-USD 表示，避免二进制浮点金额误差。

`stop` 只关闭候选的新任务准入，不会自动取消已固定候选的存量任务、撤销工具权限或补偿外部结果。真实系统发现越权时，还需要独立的运行时紧急政策阻止后续敏感操作，并核实已发生的效果。

实验没有置信区间、对照组、流量分层、等待观察时长、超时未完成样本、延迟到达事件或真实成本对账。生产系统要确定采样窗口、完整分母、最低样本数、业务风险和统计方法；不可只凭几十条成功运行宣称质量有保障。

## 主要接口

| 接口 | 教学职责 |
| --- | --- |
| `Release.digest` | 发布组合的规范 JSON 摘要 |
| `check_compatibility(release, resources)` | 检查当前状态格式、索引和工具版本是否仍受支持 |
| `Controller.stage(..., expected=...)` | 质量与兼容性检查通过后，按代次启动候选准入 |
| `Controller.admit(tenant, task_id)` | 首次选择并记录发布，同任务复用原决定 |
| `Controller.decide(observations, expected=...)` | 校验观察的摘要归属与唯一性，应用教学门禁 |
| `Controller.rollback(digest, expected=...)` | 兼容性允许时，仅切换新任务准入 |
| `Controller.resume(tenant, task_id)` | 恢复原任务固定版本，并检查其状态可读性 |
| `Controller.dispatch_write(..., operation_id, ...)` | 记录一次模拟派发，拒绝同操作直接重发 |

`Resources.state_format` 只是这个简化环境当前要求可读取的一种状态格式。真实系统可能同时存在多代状态、任务和消费者，需要完整兼容矩阵及迁移进度台账。改变 `resources` 不会迁移数据；本实验也没有恢复旧备份或回滚数据库。

## 测试与边界

19 项测试覆盖内容寻址、不可变对象、离线证据绑定、同名发布禁止覆盖、过时控制器、版本兼容、任务固定、租户键区分、样本不足、安全停止、质量/延迟/成本阈值、重复观察、未知写入保护和失败后的指针不变。

`generation` 在单进程中检查并修改，表达的是数据库 CAS 的合同；它没有跨进程锁、事务、故障持久化或领导者选举，不能承担真实多控制器并发。任务和操作日志也只保存在内存，重启就会丢失。

模拟工具没有真正执行外部工单写入，也没有查询权威回执。`unknown` 表示应用缺少完成证据；程序只证明回退没有自动重放同一操作。真实系统还需要持久操作台账、授权范围、业务幂等、只读核实和人工处置。固定发布版本不等于固定或延长用户权限；生产每次敏感操作仍要检查当前授权。

实验还没有实现集群部署、健康探针、密钥托管、镜像签名、供应链证明、数据库迁移、模型供应商漂移检测、备份恢复、真实流量停止或人工发布审批。它用于看清发布控制合同，不能直接当生产 LLMOps 平台使用。
