# 第 23 章配图 断线后的安全恢复时序

本图是企业 Agent 交互协议的教学设计，说明如何恢复观察中的任务。它不代表某个已部署系统。

- `streaming-recovery.svg`：正文嵌图，原生导出，跟随宿主深浅色偏好。
- `streaming-recovery.html`：可独立打开的交互图，支持搜索、缩放、主题切换与导出。
- `streaming-recovery.json`：Archify sequence 源文件。

## 阅读顺序

1. 浏览器向任务 API 请求快照，服务端验证当前权限。
2. 读取同一一致视图中的投影状态和 `last_seq = H`，浏览器整体替换本地投影。
3. 使用 `after = H` 重新订阅，服务端再次鉴权。
4. 服务端持续读取同一日志游标，以有序 SSE 传递历史及新提交事件，追平后继续实时跟随。

快照恢复只在本地投影缺失、游标失效或协议要求重新同步时执行；有效游标可直接续订。Worker 在浏览器断线期间仍可执行。取消走独立命令，必须等待后端确认，外部写入结果未知时先核对。

## 生成和校验

在已安装的 Archify 技能目录执行，下面的路径均相对于本资产目录：

```bash
node bin/archify.mjs validate sequence <资产目录>/streaming-recovery.json --quality showcase --json
node bin/archify.mjs deliver sequence <资产目录>/streaming-recovery.json <资产目录>/streaming-recovery.html --quality showcase --json
node bin/archify.mjs visual-check <资产目录>/streaming-recovery.html --json
```

从交互 HTML 的“导出 → SVG”获取正文矢量图。SVG 导出包含深浅色样式，不需要提取 HTML 内部节点。

2026-11-03 验证记录：9/9 showcase 检查通过，组合错误 0、警告 0；1440×900、1600×1000、1920×1080、2048×1320 均无页面溢出。已查看最小尺寸浅色和最大尺寸深色截图，文字、连线与结论卡均可读。自动 visual-check 回执本身只报告视觉审阅待处理，人工查看结论单独记录于此。本机诊断回执与截图不作为发布资源。

| 文件 | SHA-256 | 字节数 |
|---|---|---:|
| `streaming-recovery.json` | `1f8dacb1cdf4e029d179b20b2ecaee1241d58599ffc4ef3216a1363d2f377506` | 3196 |
| `streaming-recovery.html` | `24a028bdfa3c8cb5ce915d0a79b361d6ff091847a32758c17d755a4ddc158040` | 708832 |
| `streaming-recovery.svg` | `dc3bcdabed80bb8702ac7bc19a8ba551acdda55688a87947a048b838f2d30e48` | 52660 |
