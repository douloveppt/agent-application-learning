# 协作与发布说明

本项目的约定是：**Codex 每次完成用户要求的一轮修改并验证后，提交并推送到 GitHub。** 这项发布工作由 Codex 在任务收尾时执行。

## 内容来源

本地 Obsidian 学习目录保存原稿，公开仓库保存适配 GitHub 的发布副本。同步工具仅在明确调用时执行一次，不包含文件监听或后台运行模式。

本地配置保存在仓库外，包含 `source_dir`、`css_source`、`repo_dir`、`remote_url`、`branch` 和 `state_dir`。认证沿用本机 Git／GitHub CLI 配置；不把凭据写入项目。

## 一轮修改如何发布

1. 检查已有改动，在原稿中完成用户要求的修改。
2. 生成公开副本，检查 README、目录、章节链接和实际涉及的文件。
3. 完成相关验证后，明确暂存本轮文件、创建提交并推送 `origin main`。
4. 核对远端提交，向用户报告本轮更新结果。

同步工具依赖 Python 3.11 标准库和 Git，支持以下单次操作：

```bash
# 仅生成公开副本，供检查与普通 Git 提交使用
python3.11 scripts/sync.py --config /path/to/local-config.json --export-only

# 已有学习资料的单次导出、提交和推送
python3.11 scripts/sync.py --config /path/to/local-config.json --once

# 查看上一次发布状态
python3.11 scripts/sync.py --config /path/to/local-config.json --status
```

新增工具、测试或其他非原稿文件时，按普通 Git 流程明确提交这些文件。推送失败会保留本地提交，下次调用可继续推送；远端分叉需要先检查差异，不强制覆盖。

## 公开副本的处理

- 只读取指定目录的 Markdown、Canvas 和指定样式文件。
- 不读取整个个人知识库，不发布目录外的私人笔记。
- 转换为 GitHub 可点击的相对链接，补齐公开文档标题，并适配 Canvas 与 Bases 查询。
- 本地原稿保持原有组织方式，发布转换不会反向改写原稿。
- 配置、状态和日志保存在本机，文件删除仅限已管理的发布文件。

## 验证

```bash
python3.11 -m unittest discover -s tests -v
```

测试使用临时目录和本地裸 Git 仓库，不访问 GitHub。完整协作约定见 [AGENTS.md](./AGENTS.md)。
