# 自动同步说明

同步方向为 **本地 Obsidian 学习目录 → 本地发布仓库 → GitHub**。学习资料和样式从指定来源生成；仓库中的工具、测试和说明也可正常提交。

## 运行方式

同步脚本只依赖 Python 3.11 标准库，以及本机已经登录的 GitHub CLI 和 Git。认证使用系统中已有的 Git 凭据配置，不把 Token 写入仓库或配置文件。

本地配置放在仓库之外，例如：

```json
{
  "source_dir": "/path/to/Obsidian Vault/Agent 应用开发",
  "css_source": "/path/to/Obsidian Vault/.obsidian/snippets/agent-learning-entry-navigation.css",
  "repo_dir": "/path/to/agent-application-learning",
  "remote_url": "https://github.com/douloveppt/agent-application-learning.git",
  "branch": "main",
  "state_dir": "/path/to/local-sync-state",
  "poll_seconds": 3,
  "debounce_seconds": 15,
  "retry_seconds": 60
}
```

```bash
# 生成发布副本
python3 scripts/sync.py --config /path/to/config.json --export-only

# 同步一次，存在变更时提交并推送
python3 scripts/sync.py --config /path/to/config.json --once

# 持续观察保存后的变更
python3 scripts/sync.py --config /path/to/config.json --watch

# 查看最近的同步状态
python3 scripts/sync.py --config /path/to/config.json --status
```

## 保存和推送

工具每 3 秒检查内容变化，连续保存稳定 15 秒后合并为一次提交并推送。无变更时不创建提交。推送失败会保留本地提交，并在后台继续重试。

本机通过登录用户的 macOS LaunchAgent 运行同步程序：登录后自动启动，进程异常退出后重新启动。电脑需要处于开机、用户已登录且服务可运行的状态；联网后才会完成远端推送。

暂停与恢复服务：

```bash
# 暂停
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.douloveppt.agent-learning-sync.plist"

# 恢复
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.douloveppt.agent-learning-sync.plist"
```

配置、状态和日志位于本机的同步状态目录，不参与发布。状态记录最近的导出、提交、推送及失败原因。若 GitHub 上的修改导致历史分叉，程序保留现状，不会强制覆盖；先检查双方差异，处理后再恢复推送。

## 发布副本与原稿

- 只读取指定学习目录的 Markdown、Canvas 和单独指定的样式文件。
- 不读取整个个人知识库，不发布目录外的私人笔记与工作区设置。
- 公开文档使用 GitHub 可点击的相对链接，缺少正文标题时补齐标题；公开知识库配置关闭额外的文件标题展示。
- Canvas 中的目录前缀按仓库位置适配，Bases 与任务查询适配独立知识库。
- 本地原稿保留 Obsidian 的组织方式；发布转换不反向改写原稿。
- 删除只针对之前由同步工具管理的发布文件；源目录不可用时不会把整个发布目录清空。

以后对这套学习资料的保存会自动形成公开更新。同步工具和配置的维护方式与普通代码项目相同，修改后可用单次同步命令验证。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时目录和本地裸 Git 仓库，不访问 GitHub。
