# 第二十章：身份权限与多租户隔离实验

这个离线实验用 Python 标准库与 SQLite 演示：**身份由服务端确定，权限按当前状态判断，批准绑定具体动作，数据访问始终带租户与用户范围。** 不调用模型、不访问网络、不连接企业身份提供方，也不需要安装依赖。

所有数据均为合成夹具。两个租户为 `tenant-a`、`tenant-b`，三个用户为 Alice、Bob、Carol；Alice 同时是两个租户的成员。每次运行新建一个内存数据库，结束后销毁。

## 1. 运行与观察

需要 Python 3.11+，在仓库根目录执行：

```bash
# 14 个场景，每场景一行 JSON
python3.11 examples/ch20-identity/lab.py --scenario all

# 先批准、排队，然后撤销成员关系，观察执行时重新授权
python3.11 examples/ch20-identity/lab.py --scenario revoked_membership

# 单元与场景验收
python3.11 -m unittest discover -s examples/ch20-identity -p 'test_*.py' -v
```

例如，撤销权限后运行原任务会输出：

```json
{"scenario":"revoked_membership","status":"denied","reason":"membership_revoked","receipt_count":0}
```

字段顺序不影响含义。`receipt_count: 0` 与数据库中的工单仍为 `open` 一起证明这条执行路径没有完成业务写入。已经成功执行后尝试重用批准的场景保留原有一份回执。

| 场景 | 预期结果 | 要观察的边界 |
| --- | --- | --- |
| `success` | `closed`，一份回执 | 批准、排队、执行前复查、事务内写入与回执 |
| `tenant_spoof` | `server_scope_preserved` | 传入 Carol/tenant-b 声明，实际仍由 Alice/tenant-a 会话决定身份 |
| `cross_tenant_resource` | `denied` | 替换为另一个租户的资源 ID 也查不到记录 |
| `same_tenant_other_owner` | `denied` | 同租户成员也不能读取别人的私人工单 |
| `revoked_membership` | `denied` | 入队时有权限，执行时成员关系已撤销 |
| `role_downgrade` | `denied` | 上下文中旧角色不能保留已经撤销的写权限 |
| `forged_approval` | `denied` | 自造批准 ID 在入队前被拒绝，不创建任务 |
| `changed_args` | `denied` | 批准后改变关闭说明，入队前被拒绝 |
| `resource_changed` | `denied` | 资源版本变化使原批准失效 |
| `approval_replay` | `denied`，仍只有原回执 | 一次批准不能授权第二个业务操作 |
| `cache_acl_change` | `denied` | 已缓存的正文在 ACL 收紧后不能继续返回 |
| `memory_isolation` | `denied` | 已知另一用户 Memory ID 仍不能读取 |
| `retrieval_filter` | 只返回 `doc-alice`、`doc-team` | SQL 先按租户和 ACL 过滤，再把正文交给上层 |
| `idempotency_scope` | 两个租户各一份回执 | 同一用户跨租户使用同名资源 ID、相同幂等键，操作仍各自隔离 |

## 2. 从身份到执行的代码路径

`IdentityLab.authenticate()` 仅接受**服务端固定会话夹具**确定 actor 与当前租户，调用方附带的 `tenant_id`、`actor_id`、`role` 不参与授权。`RequestContext` 保存身份引用、请求 ID 和角色快照；`role_snapshot` 仅用于解释历史状态，不用于授予权限。

`_live()` 每次读取服务端会话、当前成员关系和策略。即使调用者在 Python 中改造了上下文，session 对应的 actor、tenant 绑定也必须相符。固定会话字符串公开在源码中，**它们只是测试入口，不能拿来实现真实登录**。

资源读取使用 `WHERE tenant = ? AND id = ?`；工单还需满足 owner 条件，关闭动作要求本人且当前角色明确为 `employee`。Alice 与 Bob 同属 tenant-a，并不代表两人的私人工单可以互读。`tenant_admin` 代表控制面管理角色，在本实验中不能绕过 owner，也不默认获得关闭本人工单的业务权限。跨租户和非本人资源统一返回 `resource_unavailable`，避免通过错误差异暴露资源存在性。

`approve()` 模拟可信界面已经取得一次明确确认，在服务端保存 actor、tenant、session、action、resource、参数摘要、资源版本、成员关系版本、策略版本及到期时间。它是**测试宿主夹具，不是模型可调用的工具**。生产系统需要真实审批交互、审批人的权限校验、CSRF/重放防护与持久化批准记录。

`queue()` 在事务内检查当前身份、owner 和业务权限，并调用不消费批准的 `_validate_approval()` 校验存在性、完整绑定、成员/策略/资源版本、期限与未消费状态；校验通过才保存原始身份引用与不可变的序列化提议。伪造、跨范围、过期、篡改内容或版本过时的批准不会产生任务。

`run_job()` 模拟内部 Worker，不从当前操作员或其他租户借用身份。`execute_close()` 在事务内再次复查当前成员关系、当前策略、资源 owner 和同一套批准规则，然后原子消费批准、条件更新工单、保存回执和允许审计。入队时的合法性不保留到执行时：撤销、角色变化、策略切换、资源变更或批准过期仍会阻止写入。

## 3. 三条容易遗漏的数据路径

**检索。** `retrieve()` 在 SQL 中同时约束租户和文档可见性，未获准的正文不会先返回给模型再被删掉。这里用子串查询代替向量检索，验证的是权限过滤的入口位置，不是检索质量。

**Memory。** `read_memory()` 的复合范围是 `(tenant, actor, memory_id)`。同名 `memory-self` 在不同租户、不同用户下是独立记录。这个实验只读已有 Memory；真实写入、合并、摘要和删除也必须采用相同范围。

**缓存。** `cached_document()` 的键含 `(tenant, actor, document_id)`，每次命中前仍查询当前 ACL；ACL 版本变化时旧内容不能复用。例子中的内容不可变，`acl_revision` 只表达 ACL 变化；生产缓存还需绑定正文版本、策略版本、连接器身份、模型与配置等实际依赖。单靠更长的缓存键不能撤回已经交付给用户的内容。

## 4. 批准、幂等与事务各解决什么

批准回答“这次动作是否经过确认”。幂等键回答“同一业务操作是否已经执行”。两者不能替代彼此。

回执主键为 `(tenant, actor, operation_key)`，合同摘要绑定 action、resource 与参数。相同键与相同合同重试时返回原回执，`replayed_receipt: true`；相同键却改变参数会返回 `idempotency_conflict`。已经提交的操作不因批准后来过期而重写一次，但读取原回执仍重新校验当前权限。重新使用同一批准配另一操作键会被 `approval_consumed` 拒绝。

本例把成员关系、策略、资源、批准、业务结果和回执放在同一 SQLite 数据库。`BEGIN IMMEDIATE`、版本条件写和唯一键使这些状态在本地事务中一起提交或一起回滚。失败的参数篡改不会消耗原批准。允许审计与业务写入同事务保存，事务内业务拒绝的审计在回滚后保存，不记录原始说明、文档正文、会话句柄或批准句柄。审计身份由服务端会话绑定派生；上下文绑定不匹配时记录空身份，避免伪造 actor 污染记录。生产系统还需将认证入口、参数校验和检索前拒绝统一接入审计。

这份实验没有外部业务系统，不产生跨系统“结果未知”。生产连接器应结合第 26 章的幂等合同与对账流程处理派发后超时，不能推断本地回滚就撤销了远端副作用。

## 5. 文件与验证范围

| 文件 | 内容 |
| --- | --- |
| `lab.py` | 固定会话、当前身份复查、owner 授权、数据范围、批准、队列、幂等回执和 14 个场景 |
| `test_lab.py` | 41 项测试，涵盖跨租户、同租户越权、管理角色与业务权限分离、上下文伪造与审计防污染、入队批准校验、撤权、角色降级、策略/资源/成员版本、批准到期与复用、事务故障回滚、检索/Memory/缓存隔离及审计字段 |

这是单进程、单连接、同步执行的教学模型。真实 SSO、OIDC/OAuth、JWT 验签、MFA、SCIM、PDP/PEP 服务、高并发、多 Worker、连接池、数据库 RLS、独立 Secret Manager、数据加密、分布式撤权传播、委托令牌及服务身份均未实现。实验也没有证明模型抵抗提示注入的能力。

生产系统中的权限读取与业务写入可能分属不同服务，无法直接使用这个单库事务。需要明确授权决策的一致性、撤权时效、执行租约和资源端条件写语义，再分别验证。审计表也只是本地教学记录，不具备防篡改存储、租户自助查询权限、保留策略和安全运营能力。

建议继续练习：给 Memory 增加同范围写入与删除；把缓存访问者变为组成员并测试退组；把审批改为第二名有资格的用户确认；最后接入真实身份提供方，验证会话吊销、组同步延迟和 Worker 身份。
