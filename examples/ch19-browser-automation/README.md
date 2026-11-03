# 第十九章：浏览器自动化与结果核实实验

这个实验启动一个仅监听 `127.0.0.1` 的假工单网站，再用 **真实 Chromium + Playwright** 填写、预览、提交和查询工单。没有模型调用，没有企业账户，也不连接真实工单系统。默认使用随机空闲端口；每个场景都有独立服务状态和 Browser Context，退出时关闭浏览器上下文与本次 HTTP 服务。

重点是观察三个边界：**当前界面是否仍对应原目标；批准是否仍覆盖这份内容；提交之后有哪些业务证据。** 浏览器完成点击不代表工单已创建，HTTP 响应丢失也不代表工单未创建。

## 1. 安装与运行

需要 Python 3.11+。在仓库根目录执行以下步骤。安装依赖和浏览器会访问 PyPI 与 Playwright 下载源；安装完成后，实验只访问本机练习站。

```bash
python3.11 -m venv examples/ch19-browser-automation/.venv
examples/ch19-browser-automation/.venv/bin/python -m pip install -r examples/ch19-browser-automation/requirements.txt
examples/ch19-browser-automation/.venv/bin/python -m playwright install chromium

# 十个场景，每个输出一行 JSON
examples/ch19-browser-automation/.venv/bin/python examples/ch19-browser-automation/lab.py --scenario all

# 显示真实浏览器，观察写后断开响应与只读核实
examples/ch19-browser-automation/.venv/bin/python examples/ch19-browser-automation/lab.py --scenario commit_then_disconnect --headed
```

Linux 如果缺少浏览器系统依赖，应根据 Playwright 官方安装说明准备对应环境。脚本不会自动安装操作系统软件。Windows 可使用 WSL 或把虚拟环境命令换成本机的 Python 路径。

依赖固定为本实验验证过的 Playwright 1.58.0，搭配该版本下载的 Chromium；这里没有宣称它是最新版本。升级时需要重跑浏览器验收。

## 2. 先读输出，再读实现

| 场景 | 最终状态 | `create_calls` | 工单数 | 观察重点 |
| --- | --- | ---: | ---: | --- |
| `success` | `succeeded` | 1 | 1 | 填写→核对预览→批准→提交→读回匹配回执 |
| `rerender` | `succeeded` | 1 | 1 | 原 DOM 节点确实被替换，语义 Locator 再次定位新节点 |
| `ambiguous_target` | `succeeded` | 1 | 1 | 两个同名按钮使宽泛定位报 strict 错误，限定预览对话框后再行动 |
| `stale_approval` | `approval_required` | 0 | 0 | 批准后改描述并生成新版本，旧批准不能覆盖新预览 |
| `expired_approval` | `approval_required` | 0 | 0 | 内容相同，但批准期限已过，禁止提交 |
| `commit_then_disconnect` | `succeeded` | 1 | 1 | 服务端先写入，再主动关闭 HTTP 连接；界面未知后查询原工单 |
| `unknown_no_receipt` | `unknown` | 1 | 0 | 派发后连接断开，查询无回执；保留未知，不第二次提交 |
| `login_expired` | `login_required` | 0 | 0 | 提交前刷新身份发现会话失效，停下来交还用户 |
| `injected_page` | `succeeded` | 1 | 1 | 页面恶意留言无法修改宿主固定的工单意图 |
| `identity_mismatch` | `identity_rejected` | 0 | 0 | 页面账号不等于宿主指定租户，生成草稿前即停止 |

成功输出包含 `ticket_id`；`events` 给出身份核对、预览核对、派发边界和回执核实等节点。`wrong_clicks` 必须始终为 `0`。`create_calls` 统计真正进入服务端创建路径的次数，不能仅靠最终工单只有一条来证明没有重放。

`unknown_no_receipt` 的测试夹具知道这次没有写入，但适配器没有利用这一内部事实。它只看见“已派发、未收到响应、查询为空”。真实系统的查询可能最终一致，空结果不能自动证明先前请求永远不会成功，因此仍保留 `unknown`。

## 3. 文件与信任边界

| 文件 | 职责 |
| --- | --- |
| `server.py` | 标准库 HTTP 服务、前端、内存工单数据、固定身份与批准夹具、故障注入 |
| `lab.py` | 可信任务上下文、预览检查、批准校验、真实浏览器交互、回执比对与场景运行 |
| `test_lab.py` | 无浏览器依赖的身份、批准和回执合同测试 |
| `test_browser.py` | 必须启动 Chromium 的真实浏览器验收，缺运行时会失败，不会静默跳过 |

宿主预先给定 `Context(tenant="tenant-a", actor="employee-7")`，并为本次操作生成唯一 `client_reference`。网站显示的身份必须与它相符。脚本使用 `get_by_label()` 填写字段，使用 `get_by_role("dialog", name="提交预览")` 限定提交按钮，未使用 `.first`、坐标猜测或 `force=True` 绕过歧义。

预览包含 tenant、actor、action、client reference、草稿版本、标题和描述。宿主核对完整预览后，`State.approve()` 模拟已经获得的一次明确授权，产生内容摘要和到期时间。临近点击时，`validate_approval()` 重新检查当前界面中的预览与批准是否相符；站点也对当前草稿做一次校验，展示版本检查应尽可能接近业务写入。

批准函数是**测试宿主夹具**，不是用户审批产品，也不是模型可以调用的工具。摘要仅检测内容变化，没有签名、不可伪造能力、持久化审批记录或分布式时钟；不能拿此实现保护真实权限。真实旧系统未必支持版本检查，本实验的服务端检查能力不能直接假定它也具备。

`events.append("dispatched")` 在浏览器点击之前发生。越过这个边界后，本脚本只提供一次提交，后面只能点击“核实结果”。回执必须同时匹配原 tenant、actor、action、reference、草稿版本、标题和描述，并含业务工单 ID，才能交付成功。

## 4. 测试

```bash
# 9 项合同测试：只需要 Python 标准库
python3.11 -m unittest discover -s examples/ch19-browser-automation -p 'test_lab.py' -v

# 全部 19 项测试：包括 10 项真实 Chromium 验收
examples/ch19-browser-automation/.venv/bin/python -m unittest discover -s examples/ch19-browser-automation -p 'test_*.py' -v
```

真实测试检查旧 DOM 节点确实 detach、宽泛同名定位确实出现 strict 错误、错误按钮未被点击、过期批准和身份错误没有进入创建路径，以及丢响应后的创建调用次数仍为一。回执合同测试逐项改变关联字段，验证“内容看起来接近”不能通过结果核实。

**2026-11-03 本次验证记录**：Python 3.11、macOS arm64、Playwright 1.58.0 与 Chromium 145.0.7632.6；9 项合同测试与 10 项真实浏览器验收共 19 项全部通过。这条环境信息不代表生产兼容性承诺。

## 5. 可选 Trace

```bash
examples/ch19-browser-automation/.venv/bin/python examples/ch19-browser-automation/lab.py --scenario rerender --trace-dir examples/ch19-browser-automation/traces
examples/ch19-browser-automation/.venv/bin/python -m playwright show-trace examples/ch19-browser-automation/traces/rerender.zip
```

Trace 包含页面快照、截图和源码，默认关闭。虚拟环境与默认 `traces/` 均已忽略；不要把真实业务页面、认证状态或包含隐私的 Trace 放进公开仓库。此实验所有页面内容都是合成数据。

## 6. 这个实验能够证明什么

它验证的是确定性适配器的十条控制路径。页面里的恶意文字不会被执行，因为宿主从未把它当指令；实验**没有调用 LLM，因此不能证明真实模型抵抗提示注入的能力**。正式的 Computer Use Agent 还需要单独评测模型提议、工具门禁、域名跳转与数据外发路径。

固定账号、随机 Cookie 和会话开关仅模拟登录，不具备真实 SSO、MFA、RBAC、多租户认证或会话吊销实现。每场景 Browser Context 隔离 Cookie，并不等于浏览器进程、宿主文件或网络的安全沙箱。Playwright route 只是本实验的访问约束，不是生产出口防火墙。

操作记录、批准、草稿和工单都保存在单进程内存；本实验不支持进程崩溃恢复、多 Worker、租约、fencing、跨系统事务，也没有验证真实旧系统中的条件写入、幂等合同或最终一致查询。提交后浏览器自身崩溃、用户在另一窗口修改数据、SSO 二次跳转、文件上传下载、桌面焦点与坐标交互，需要分别设计和验收。

继续练习时，先增加“回执字段错配”“业务版本在点击前改变”“查询长时间无结果”的用例，再接入持久操作账本和人工核实队列。不要把 `unknown` 简单改成 `failed` 或自动重新点击。

官方参考：[Locators](https://playwright.dev/python/docs/locators)、[Auto-waiting](https://playwright.dev/python/docs/actionability)、[Network](https://playwright.dev/python/docs/network)、[Trace Viewer](https://playwright.dev/python/docs/trace-viewer-intro)。
