# 第二章：HTTP、SQLite 与异步等待实验

这两个独立的小程序使用 **Python 3.11 或更高版本的标准库**，不调用模型或外部企业系统。HTTP 服务使用线程处理请求；`async_demo.py` 单独演示协程调度。完整讲解见[第二章](../../02%20%E8%BD%AF%E4%BB%B6%E4%B8%8E%E7%B3%BB%E7%BB%9F%E5%9F%BA%E7%A1%80.md)。

## 1. 启动本地工单服务

在仓库根目录执行：

```bash
cd examples/ch02-system-basics
python3.11 server.py --port 8765 --db var/tickets.sqlite3
```

服务固定监听 `127.0.0.1`。`var/` 是被 Git 忽略的本地实验数据目录。以下命令在另一个终端执行。

```bash
curl -i http://127.0.0.1:8765/health

curl -i http://127.0.0.1:8765/tickets \
  -H 'Content-Type: application/json' \
  --data '{"title":"VPN 无法连接","description":"macOS，连接超时","client_tag":"normal-01"}'

curl -sS 'http://127.0.0.1:8765/tickets?client_tag=normal-01'
```

创建响应为 `201`，JSON 包含 `request_id` 和 `ticket`。`X-Request-ID` 响应头与 JSON 中的值一致。工单字段包括 `ticket_id`、固定的 `owner_id: demo-user`、标题、描述、`client_tag`、状态 `open` 和 UTC 创建时间。

用响应中完整的 `ticket_id` 替换下面 URL 的最后一段，再查询同一业务对象：

```bash
curl -i 'http://127.0.0.1:8765/tickets/这里替换为完整ticket_id'
```

两次请求的 `request_id` 应不同，返回的 `ticket_id` 应相同。`client_tag` 只是便于查询实验记录的标签，不是身份或幂等键。

## 2. 验证错误与持久化

```bash
# JSON 语法错误：400，不新增记录
curl -i http://127.0.0.1:8765/tickets \
  -H 'Content-Type: application/json' --data '{'

# 字段不合法：422，不新增记录
curl -i http://127.0.0.1:8765/tickets \
  -H 'Content-Type: application/json' --data '{"title":"   "}'

# 媒体类型错误：415
curl -i http://127.0.0.1:8765/tickets \
  -H 'Content-Type: text/plain' --data '{}'
```

允许字段为 `title`、`description`、`client_tag`，均须为字符串；标题必填且不能全为空白。长度上限分别为 120、4,000、100 个 Python 字符；整个请求体上限为 16 KiB。未知字段（包括 `owner_id`）返回 `422`。缺少长度返回 `411`，请求体过大返回 `413`，未知对象或路径返回 `404`。

在服务终端按 `Ctrl+C`，然后用**同一个数据库路径**重新启动。先查 `/health`，再查原来的工单：进程内计数重新开始，数据库中的工单仍在。改用 `--db var/new.sqlite3` 启动可观察一个新数据库；不需要删除已有数据。

## 3. 验证“客户端超时，但写入已发生”

```bash
# 先提交数据库，再延迟 1,500ms 写响应；curl 等待 100ms 后超时
curl -i --max-time 0.1 \
  'http://127.0.0.1:8765/tickets?reply_delay_ms=1500' \
  -H 'Content-Type: application/json' \
  --data '{"title":"回执延迟实验","client_tag":"timeout-01"}'

curl -sS 'http://127.0.0.1:8765/tickets?client_tag=timeout-01'
```

查询可以看到已经创建的记录。检查当前数量后再决定下一步；重复执行同一个 POST 会再创建一条记录，本实验没有实现幂等。`reply_delay_ms` 只用于故障演练，允许 0–2,000 的整数。

日志只记录请求 ID、方法、路径、状态和响应写入情况，不输出完整请求正文。日志里记录的 `201` 不能证明客户端已经收到响应，业务是否持久化要看数据库。

## 4. 运行异步等待实验

```bash
python3.11 async_demo.py --mode all
# 也可单独选 sequential / concurrent / blocking / timeout / failure
```

输出含 `mode`、`status`、`elapsed_ms`、`results` 和 `events`：

| 模式 | 观察重点 |
| --- | --- |
| `sequential` | 两个仿真查询依次开始和完成 |
| `concurrent` | 等待相互交错；`gather` 返回结果仍按参数顺序排列 |
| `blocking` | 在协程中使用 `time.sleep`，事件循环被阻塞，两个查询又串行了 |
| `timeout` | `wait_for` 取消超时的本地仿真，并报告 `timeout` |
| `failure` | `TaskGroup` 中一个查询失败，另一个查询被取消，报告 `failed` |

时间数字用于辅助观察，不要求每次运行完全相同。这里的超时取消没有外部写入，不能据此推断取消 HTTP 请求会撤销工单。`gather` 的默认异常传播也不同于 `TaskGroup`，不要把示例中的兄弟取消行为套到所有并发调用上。

## 5. 运行可重复测试

在本目录执行：

```bash
python3.11 -m unittest -v test_lab
```

或从仓库根目录执行：

```bash
python3.11 -m unittest discover -s examples/ch02-system-basics -p 'test_*.py' -v
```

测试创建临时数据库并绑定临时本机端口，不占用演练服务的 `8765`，不修改 `var/` 中的记录。覆盖数据契约、请求与业务 ID、重启、并发、响应延迟以及协程取消。

## 教学边界

这是本机单用户实验：身份固定在服务端，不信任 `X-User` 请求头；没有登录认证、多租户、真实 Agent 或生产级服务配置。SQLite 连接在各请求线程内创建并显式关闭；`with connection` 管理事务，不能代替关闭连接。完整授权、持久任务与幂等方案在后续章节展开。
