# 第 28 章实验：从一次运行到可核实诊断

这个离线实验用五条确定性 VPN 报修运行，演示怎样把运行记录、Trace 与权威业务回执关联起来，再依据证据判断“已完成、变慢、回执丢失或结果未知”。它使用 **Python 3.11+ 标准库**，不调用模型、网络、第三方 SDK 或真实工单系统。

实验是教学事件模型，不是 OpenTelemetry SDK、Collector 或生产监控平台的替代实现。所有耗时和 ID 都是合成数据；ID 没有从用户内容派生，也不代表匿名化处理。

## 运行

在仓库根目录执行：

```bash
python3.11 examples/ch28-observability/lab.py --all
python3.11 examples/ch28-observability/lab.py --scenario lost-receipt
python3.11 examples/ch28-observability/lab.py --scenario telemetry-drop --output-dir /tmp/ch28-report
python3.11 -m unittest discover -s examples/ch28-observability -p 'test_*.py' -v
```

默认报告写入本目录的 `var/`，该目录已忽略。`--all` 生成 `observability-report.json`；单场景生成 `<scenario>.json`。同名文件会被本次运行覆盖，要保留多轮结果应使用不同输出目录。

预期摘要类似：

```text
run-01 success: completed_verified; duration=720ms; trace=exported
run-02 slow: completed_verified; duration=2300ms; trace=exported
run-03 lost-receipt: completed_after_read_only_reconciliation; duration=1400ms; trace=exported
run-04 missing-trace: completed_verified; duration=810ms; trace=not_sampled
run-05 telemetry-drop: unknown; duration=950ms; trace=dropped
metrics: runs=5; unknown=1; trace_exports=3; ...
```

退出码：`0` 表示报告已完整生成，包含 `unknown` 教学场景也仍是成功运行；`2` 表示 fixture、证据、SQLite 或报告写入失败。参数错误也由命令行解析器返回 `2`。这不是发布门禁，因此没有用退出码 `1` 表示业务结果未知。

## 五个场景分别证明什么

| 场景 | 关键证据 | 诊断 |
| --- | --- | --- |
| `success` | 运行终态与权威工单回执一致 | 已核实完成 |
| `slow` | 全量运行耗时 2300ms；显式排队 500ms；检索子 Span 1000ms | 完成，但排队和检索是主要耗时点 |
| `lost-receipt` | 受控写工具先提交 SQLite，再抛出丢回执异常；恢复过程按同一 `operation_id` 只读查到工单 | 核实完成，全程只有一次写入派发、零次重写 |
| `missing-trace` | Trace 未采样；全量运行记录与权威回执仍在 | 业务完成可证明，同时记录 Trace 缺口 |
| `telemetry-drop` | Trace 丢失、运行保持 `UNKNOWN`、阴性查询不具终局性 | 证据不足，继续只读核实或人工复核，不重新写入 |

诊断函数只读取证据，不依赖场景名称。测试会改写名称再重新诊断，避免把 fixture 标签当作结论。

`lost-receipt` 不是一组预填的计数。`ControlledTicketTools.create_ticket()` 先记录写调用，把权威回执提交到 SQLite，然后抛出 `LostReceipt`；恢复代码捕获异常并调用 `lookup_by_operation_id()`，后者记录只读调用和查询结果。诊断时会从 `operation_journal.events` 重新统计同一操作的写入派发、重试和只读核实次数，再与 `run_record` 摘要交叉检查。第二次写入、摘要计数不一致，或把核实方法改成写操作，都会得到 `evidence_conflict`，不能得到安全恢复结论。

## 报告中的身份与证据

每条样本都包含版本 `manifest`，以及以下关联字段：

- `run_id`：一次端到端运行；
- `attempt_id`：这次运行中的执行尝试；
- `trace_id`、`root_span_id`：合法长度的合成追踪身份；
- `operation_id`：工单写入及后续核实共同使用的业务操作身份；
- `verified_ticket_id`：只有权威业务系统确认后才写入运行终态；
- `tenant_ref`、`requester_ref`、`approved_revision`：固定教学作用域与获批版本，不含真实个人或租户数据。

`evidence.run_record` 模拟全量运行台账，`evidence.operation_journal` 保存受控工具调用，`evidence.telemetry` 保存采样 Trace，SQLite `ReceiptStore` 模拟与 Trace 分离的权威业务回执。成功声明必须同时满足：回执来自固定教学权威 `teaching-ticket-system`，操作、工单、作用域和获批版本一致，调用摘要与事件明细一致。Trace 适合解释过程，不能单独证明外部副作用已完成。

## 性能和指标口径

汇总指标从五条全量运行记录计算，分母始终是 `runs_total=5`。Trace 只有三条成功导出，但未采样和导出丢失的运行不会从成功率、未知率或延迟分母中消失。

实验报告 `root_span_elapsed_ms`，表示根 Span 的墙钟经过时间，并把根 Span 之外的排队时间作为显式阶段记录。它不是通用的关键路径算法：并发分支、Span Link、断裂 Trace 与时钟偏差都需要更完整的拓扑和时间处理。父 Span 包含子 Span，直接把父子耗时相加会重复计算；实验特意用测试阻止这种算法。这里的 2000ms 慢阈值和全部耗时只用于教学，不能照搬成生产 SLO。

## 隐私边界

采集器使用字段白名单，只保留分类、语言、批准标记和允许的工具名。它不会递归复制请求，也不记录原始 Prompt、消息内容、工具参数、令牌或密码。fixture 内置敏感哨兵，测试还会把哨兵放进多层对象，验证最终报告不包含这些内容。

白名单只能降低误采集风险，不能取代数据分类、访问控制、保留期、删除流程和独立审计。生产中还要验证 SDK 自动采集、异常对象、代理日志、导出器失败以及观测后端本身的权限边界。

## 实验边界

实验能验证证据关联规则、同操作调用计数、只读核实约束、权威来源和作用域绑定、未知结果保护、采样分母、基本耗时分析和内容白名单。测试会破坏回执权威、教学租户、教学请求者、获批修订、写入次数、摘要计数和核实方法，确保这些错配不能得到成功结论。

它没有实现跨进程上下文传播、异步 Span Link、尾采样、Collector 背压、多租户隔离、时钟偏差、真实存储保留、告警、SLO 或审计防篡改。SQLite、操作日志与 Trace 都在单进程内生成，没有独立信任根。`telemetry.complete` 和操作日志版本只是 fixture 字段，方便教学规则判断，不是防删除、防伪造或完整性证明；生产系统需要受控采集、独立存储和完整性校验。

汇总中的 `latency_ms` 描述这五条 fixture 截至本次观察点的模拟运行耗时。`UNKNOWN` 样本尚未取得最终业务结果，其耗时到观察截止为止；这里的 p50 / p95 不是最终业务完成延迟的估计，也不能代表生产分布。
