# 第十五章 Skills 可复用技能离线实验

这个实验用一个最小宿主模拟器说明：技能负责组织任务知识和步骤，宿主负责技能准入、身份、工具可用性、权限、完整性校验和执行。示例技能 `enterprise-it-triage` 只读取当前员工自己的工单，并生成内存中的工单草稿；它没有提交工单、修改账号或扩大访问范围的能力。

环境要求为 **Python 3.11+ 标准库**。实验没有网络、模型调用、API Key、真实企业系统或持久化写入。所有“技能选择”和模式候选都由场景显式传入，程序只演示确定性准入，不把规则匹配伪装成模型决策。

## 运行

在仓库根目录执行：

```bash
python3.11 examples/ch15-skills/lab.py catalog
python3.11 examples/ch15-skills/lab.py scenarios all
python3.11 -m unittest discover -s examples/ch15-skills -p 'test_*.py' -v
```

本实验的运行和测试不依赖 `skill-creator`。如果本机另外安装了该工具，可以把下面的占位路径替换为它的实际位置，额外检查技能结构：

```bash
python3.11 /path/to/skill-creator/scripts/quick_validate.py \
  examples/ch15-skills/skills/enterprise-it-triage
```

`catalog` 只输出名称和描述。此时即使技能目录不存在，目录发现仍可工作；真正准入后，宿主才校验包并选择性加载正文和当前模式的参考资料。

## 六个场景

| 场景 | 预期状态与结果 | 证明的边界 |
| --- | --- | --- |
| `normal` | `ok/draft_ready`，执行 2 次工具调用 | 只加载 `SKILL.md` 与 VPN 参考资料；只读本人数据并生成草稿 |
| `missing_input` | `needs_input/missing_required_input`，0 次调用 | 缺少现象时先补输入，不猜测、不执行 |
| `tool_unavailable` | `blocked/tool_unavailable`，0 次调用 | 技能写了工具名，也不能让未安装工具出现 |
| `ambiguous` | `clarification_required/ambiguous_mode`，0 次调用 | VPN 与邮箱两个模式同时成立时先澄清，不静默合并两套流程 |
| `tampered_resource` | `error/skill_integrity_failed`，0 次调用 | 参考资料内容偏离可信锁后，在进入上下文前拒绝 |
| `permission_boundary` | `blocked/tool_not_allowed`，0 次调用 | 候选动作增加 `ticket.submit` 也不能扩大技能或 Principal 的权限 |

`normal` 的结果中有三个可核查区域：

- `context` 记录宿主选为本轮上下文材料的文件和字节数；
- `integrity` 记录完整包中所有已核验资源、总字节数和内容摘要；
- `artifact.provenance` 记录技能版本、包摘要，以及固定 Handler 的宿主信任来源。

完整性校验为了计算摘要会读取所有列入锁的文件，但这不等于把所有内容加载进任务上下文。正常 VPN 场景会核验邮箱参考资料的摘要，却不会把邮箱指南加入 `context.loaded_files`。本实验不创建模型请求，固定 Handler 也不消费这些文本；`loaded_files` 是可核查的材料选择清单，不是模型已读取文本的证据。真实宿主还需完成模型上下文装配或提供受控读取接口。

## 信任边界怎样落到代码

`registry.json` 是本实验的可信宿主注册表。它保存用于廉价发现的名称与描述、版本、模式入口、工具需求、固定 Handler ID，以及技能包中每个文件的 SHA-256。宿主先检查文件清单和所有摘要，再从这份已核验的内存快照中加载 `SKILL.md` 和一个模式参考资料；多出的文件、缺失文件、符号链接或摘要变化都会失败。

注册表不是技能自己签发的权限证明。本实验把它与宿主代码一起分发，目的是呈现边界；生产系统还需要注册表签名或可信制品库、发布审批、回滚与吊销机制。固定 Handler 写在 `lab.py`，`HANDLER_TRUST` 明确它由宿主安装，不能由技能包替换。

Principal 由调用方创建，包含 `tenant_id`、`user_id` 和 `allowed_tools`。任务参数、技能正文和参考资料都不能覆盖这些值。`tickets.list_own` 不接收身份参数，过滤条件始终来自 Principal；测试数据同时包含其他员工和其他租户的工单，用来证明它们不会进入证据或草稿。

## 渐进加载与执行顺序

这个演示的顺序是：

1. 从可信注册表暴露名称与描述，形成廉价目录。
2. 接收场景明确给出的技能与模式候选；候选不是授权。
3. 检查歧义、必需输入、工具可用性和 Principal 权限。为演示依赖阻断，本实验把本人工单查询设为必需；正文生产示例将其设为可选，需按相应 `partial` 合同另外实现与验收。
4. 核验完整技能包，再只加载 `SKILL.md` 和当前模式参考资料。
5. 由宿主固定 Handler 调用两个受控工具，生成带来源信息的草稿。

正常路径不会执行技能目录中的任意代码。这个样例只含 Markdown 资源；若生产技能带有 `scripts/`，也应先锁定脚本摘要，再由受控运行器、明确参数和沙箱策略决定是否执行，不能因为脚本位于技能目录就直接运行。

## 能力边界

这个实验不是通用 Skills Runtime，也不解析 YAML frontmatter。`registry.json` 是教学用宿主设计；安装了 `skill-creator` 时，可以用它的 `quick_validate.py` 额外检查技能结构。包摘要、文件清单和输出信封能证明“加载了哪一版资源、走了哪个固定 Handler”，不能证明诊断内容在语义上正确；语义质量仍需要场景集、人工标注、回归评测和生产反馈。

本地示例会在解析前拒绝技能根路径各级符号链接，并从一次核验得到的内存快照加载上下文，但它不是抵御恶意并发文件系统修改的沙箱。生产环境应使用不可变制品或具备等价原子性保证的读取机制，并把脚本执行放入独立受限环境。

实验没有实现真实认证、RBAC/ABAC、密钥管理、签名制品、远程技能市场、模型路由、脚本沙箱、工单提交、审批、持久化、并发、跨进程恢复、审计平台或供应方回执。把这些能力接入生产前，应分别验证身份来源、最小权限、制品供应链、脚本执行环境和副作用一致性。
