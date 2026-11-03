# 第十八章 Sandbox 沙箱实验

这个实验分两层：默认运行的**离线控制面实验**说明“谁能创建什么任务，何时才可以交付产物”；单独的 **Docker 固定探针**用于在你明确选择的本地 Docker 环境检查一组容器配置与拒绝行为。两层证据分别记录。

默认实验只需要 **Python 3.11+ 标准库和 POSIX 文件接口**，可在 macOS 或 Linux 运行。它不会执行模型代码、启动容器、访问网络、读取凭据或修改现有仓库文件。临时目录由本次实验创建并自动回收。Windows 需要 WSL/Linux 环境。

## 1. 先运行离线控制面实验

在公开仓库根目录执行：

```bash
python3.11 examples/ch18-sandbox/lab.py
python3.11 -m unittest discover -s examples/ch18-sandbox -p 'test_*.py' -v
```

演示输出六行 JSON：

| 场景 | 最终状态 | 产物发布数 | 说明 |
| --- | --- | ---: | --- |
| `success` | `succeeded` | 1 | 执行结果、产物校验、停止和销毁均确认 |
| `failed` | `failed` | 0 | 即使目录中有文件，也不交付失败任务的文件 |
| `timeout` | `timed_out` | 0 | 超时后仍要求停止和销毁确认 |
| `cancel` | `cancelled` | 0 | 取消后仍要求停止和销毁确认 |
| `cleanup_unknown` | `cleanup_pending` | 0 | 程序正常结束不能覆盖清理结果未知 |
| `invalid_artifact` | `artifact_rejected` | 0 | 退出正常但产物合同不成立 |

`FakeRuntime` 是由测试注入结果的模拟器。`timed_out` 与 `cancelled` 是故障夹具，不是真实进程超时或信号终止；`stop_confirmed`、`destroy_confirmed` 也是模拟回执。运行这些测试，**不能宣称网络隔离、内存限制或逃逸防护已在操作系统上通过验证**。

## 2. 观察可信身份与固定配置

```python
context = Context("tenant-a", "employee-7", frozenset({PERMISSION}))
task = prepare(
    context,
    connection_tenant="tenant-a",
    arguments={"profile": "analysis-v1", "recipe": "vpn-log-summary"},
)
```

`Context` 模拟宿主认证后的上下文。任务 ID 由控制面生成，租户和员工取自上下文。模型只可选择已准入的 profile 与固定 recipe；传入 `tenant_id`、`task_id`、`network`、`mount` 或更大预算都会在派发前被拒绝。连接租户与上下文不一致、没有权限、其他员工访问该任务，也会被拒绝。

| `analysis-v1` 声明 | 值 | 离线实验中的含义 |
| --- | --- | --- |
| 网络 | `none` | 可信控制面要求；FakeRuntime 不创建网络 |
| 根文件系统 | 只读 | 可信控制面要求；FakeRuntime 不创建 mount |
| 用户与权限 | `65532:65532`、无 capabilities、禁止提权 | 可信控制面要求 |
| 资源 | 0.5 CPU、128 MiB 内存、32 PID、5 秒墙钟时间 | 预算合同；离线演示不真实施加这些限制 |
| 临时与产物预算 | 8 MiB tmpfs、4 KiB 单报告、最多 1 文件 | 产物字节数和固定文件清单由 collector 实际检查；tmpfs 仅为声明 |
| 宿主挂载 | 空 | 不允许模型指定宿主路径 |

本实验不接受任意源代码，也不展示如何把任意模型代码交给宿主 `exec` 或宿主 shell。

## 3. 观察停止 清理与产物发布

成功路径为：

```text
prepared → running → 确认执行结果 → 确认停止
         → 校验并暂存 report.json → 确认销毁 → succeeded 并发布
```

停止结果未知时不读取产物；销毁结果未知时丢弃暂存结果，保持 `cleanup_pending`。最终结果同时保留 `execution_outcome`，因此可以看到“计算正常结束，但资源清理未知”。对相同任务再次调用 `execute()` 会被拒绝，未知状态不会自动重跑。

产物只接受固定的 `report.json`，内容必须满足 `summary: string` 与 `error_count: nonnegative integer`，不接受额外字段。目录扫描最多读取文件数配额加一条便拒绝，避免无界枚举；collector 使用目录文件描述符与 `O_NOFOLLOW` 逐层打开路径，拒绝绝对路径、`..`、符号链接、硬链接、FIFO、超限字节与额外输出文件。JSON 深嵌套错误转为产物拒绝；未知采集异常记为 `artifact_collection_failed`，仍尝试销毁运行时。通过后返回字节数、SHA-256 和 `trust: untrusted_content`。内容仍可能错误或带有提示注入，不能据此获得执行权限。

这里的临时目录模拟“生产者停止后仍可读取的输出存储”。它没有 Docker tmpfs 的 mount 生命周期语义。真实 `/out` 如果使用容器 tmpfs，容器停止后内容通常不能继续按普通持久卷方式读取；生产系统应明确采用任务专属保留卷、快照，或受控流式暂存等策略，再定义冻结、校验、销毁和发布顺序。

43 项测试覆盖身份与租户、profile 越权、未知启动与禁止重放、失败/超时/取消不导出、停止和销毁未确认、采集与清理异常、产物 schema、大小、深嵌套 JSON、有界目录扫描、符号链接、硬链接、非普通文件和路径穿越。Docker CLI 与固定探针 mock 还覆盖完整回执、严格只读 errno、显式 context、远端拒绝、Healthcheck 关闭、身份或归属错配不删除，以及 DOWN 隧道接口、UP 非回环接口、未知 flags、接口数超限、IPv4/IPv6 非预期错误与 IPv6 明确不可用。测试不连接 Docker daemon；它们证明这些教学控制路径成立，没有证明真实多租户运行时安全。

## 4. 可选 Docker 固定探针

只有这一节会创建真实容器。需要已启动的 Docker daemon、Linux 容器能力，以及一个**已经位于所选本地 daemon、经过你检查且包含 `python3` 的 Linux 镜像**。必须显式传入 `--context NAME`，先核实这个 context 的 Unix socket 确实指向你准备用于实验的本地 daemon。脚本回读并输出其 endpoint，只接受 `unix://`，拒绝 SSH/TCP；每次调用均显式传入 context，不静默使用当前 default。Unix socket 仍可能由代理转发，路径检查不能代替你对目标环境的核实。

脚本要求不可变的本地 `sha256:<64位十六进制>` 镜像 ID，拒绝 tag，并使用 `--pull=never`；不会自动下载镜像。准备镜像属于独立步骤，下载会访问镜像源，应按你自己的环境策略进行。

先查看 context，核实确切目标，再查看该 daemon 的镜像并复制完整 ID。下面 `已核实的本地context` 与镜像 ID 都是需要替换的占位符：

```bash
docker context ls
docker context inspect 已核实的本地context
docker --context 已核实的本地context image ls --no-trunc
python3.11 examples/ch18-sandbox/docker_smoke.py --context 已核实的本地context --image sha256:替换为本地完整镜像ID
```

镜像中声明的匿名 `VOLUME` 会被探针拒绝，因为它扩大了实验存储范围。自定义 seccomp、AppArmor 或 SELinux 策略也可能影响结果，应检查实际报错，不要为通过演示关闭宿主防护。

探针不使用 shell、不挂载宿主目录、不传入宿主环境变量、不暴露端口。它设置 `--network=none`、只读 rootfs、非 root 用户、删除 capabilities、`no-new-privileges`、CPU/内存/PID/文件描述符上限，以及有容量上限的 `/tmp` 与 `/work` tmpfs。`--entrypoint=python3 -I -c <固定探针>` 覆盖镜像入口，`--no-healthcheck` 禁用镜像健康检查并回读核验，执行源码写死在脚本中。

| 证据 | 实际检查 |
| --- | --- |
| 创建后的配置检查 | 无 bind mount、仅声明的 tmpfs、网络 none、rootfs 只读、非 root 用户、0.5 CPU、128 MiB 内存、32 PID 和 `Healthcheck.Test=["NONE"]` |
| 容器内固定探针 | 完整的 9 项布尔结果：有效 UID、`NoNewPrivs`、`CapEff=0`、无 Docker socket、根目录写入返回 `EROFS`、`/work` 可写、无启用的非回环接口、IPv4 与 IPv6 文档示例地址的 UDP 连接被路由/权限明确拒绝，或 IPv6 地址族明确不可用 |
| 回收检查 | 核对创建回执中的完整 container ID、随机 name 与本次 owner label；清理前再次核对，再按精确 ID 删除容器及其匿名卷，查询确认该 ID 不存在 |

网络接口由 `socket.if_nameindex()` 枚举，再从 sysfs flags 检查 `IFF_UP=0x1` 与 `IFF_LOOPBACK=0x8`。`no_active_nonloopback` 要求没有同时满足“UP 且非 LOOPBACK”的接口；某些 Linux 内核会创建处于 DOWN 的 fallback 隧道接口，它们的名称不决定验收结果。接口数超过 16、flags 读取失败或信息不完整都保守失败。检查失败时只输出有限接口名、flags 与错误类型，不输出地址或凭据。

IPv4 `192.0.2.1` 与 IPv6 `2001:db8::1` 都是文档示例地址。UDP `connect()` 只检查路由，不发送业务数据；只有 `ENETUNREACH`、`EHOSTUNREACH`、`EACCES` 或 `EPERM` 才作为明确拒绝，其他异常不会被当成隔离通过。IPv6 socket 创建返回 `EAFNOSUPPORT` 或 `EPROTONOSUPPORT` 时，明确记录 `address_family_unavailable`；这代表 IPv6 在此环境不可用，不能表述为已验证 IPv6 路由机制。相关状态与 errno 通过有限 stderr 诊断保留。

探针没有运行 fork bomb、内存耗尽或 CPU 压测；它读取配置，**没有证明 cgroup 对所有压力场景的实际强制效果**。它也没有验证自定义 seccomp、DNS/代理白名单、挂载越界、内核逃逸或跨租户隔离。

固定探针通过会先输出所选 context 与 endpoint、`configuration` 与 `probes`，再输出 `cleanup_confirmed: true` 和 `status: probe_passed`。退出码 `0` 表示列出的固定探针与回收检查通过；`1` 表示探针或环境失败；`2` 表示清理未确认。创建请求本身未收到成功回执时，一次空查询也不能排除延迟创建，脚本返回 `cleanup_pending`，不按名称删除归属未知资源。清理失败时，输出包含本次名称、已知的 container ID 和 owner label，可在 daemon 恢复后对照核实；不要运行全局 `docker prune`。

`/work` 只是本次探针的临时空间，测试结果通过有界的固定 stdout 返回。脚本不声称在容器停止后导出了 tmpfs 产物。探针也不支持在任意不可信 payload 下安全采集无限输出；真正的执行服务需要独立的有界日志流与磁盘配额。

### 本次验证记录

**2026-11-03**：43 项离线及 mock 测试通过；另在 Docker Desktop 的 LinuxKit 7.0.14、runc、arm64 环境，以本地已有的 Python 3.11 slim 不可变镜像运行固定探针。9 项创建配置检查和 9 项容器内行为检查均通过，IPv6 连接返回 `ENETUNREACH`，本次容器已按核验后的精确 ID 删除并确认清理。运行未下载镜像、未挂载宿主目录。

这条记录只对应上述环境、镜像与固定检查范围，不代表已完成恶意代码对抗、所有 cgroup 压力场景、跨租户隔离或内核逃逸验收。换用镜像、运行时或宿主策略后，应重新收集对应证据。

## 5. 生产实现还需要补齐什么

这是单进程、单 Worker、内存状态的教学控制面。`Context` 不是认证实现，任务状态没有持久化，也没有实际的租约、fencing、后台 reaper、配额服务、Secret broker、策略签名或 artifact 对象存储。它不支持进程崩溃后的回收恢复，也不支持重新核实 `cleanup_pending`。正式系统需要把稳定 runtime ID、任务所有权、授权版本、期限与清理回执持久化，由恢复 Worker 查询同一运行实例并完成回收，不能直接创建新任务替代未知旧实例。

collector 假定生产者已经停止，输出根目录由可信控制面建立、祖先目录受控；没有实现任意活跃文件系统上的对抗性快照、压缩包解包、恶意 PDF/HTML 扫描或多文件事务发布。真实服务还需要文件系统与对象存储的配额、类型验证、恶意内容扫描，以及按租户授权的下载与保留期限。

普通容器仍依赖宿主内核。这个实验不替你选择 gVisor、Kata 或微虚拟机，也不能证明可安全承载互不信任租户的任意恶意代码。隔离等级应按第十八章的威胁模型和真实运行时验收决定。
