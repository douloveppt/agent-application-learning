# 第 24 章配图 语音打断与业务回执

本图是企业实时语音会话的教学设计，说明用户抢话时媒体输出和业务操作如何分别收敛。它不代表某个已部署系统，也不说明任何供应商自动提供整套能力。

- `interruption-sequence.svg`：正文静态配图，通过交互 HTML 的原生 SVG 导出生成，支持深浅色偏好。
- `interruption-sequence.html`：独立交互图，支持主题切换、缩放、搜索与导出。GitHub 阅读时可下载后在浏览器打开。
- `interruption-sequence.json`：Archify sequence 源文件。

## 阅读顺序

1. 已批准的工单写入获得独立 `operation_id` 并派发，语音旧回复 `r7` 同时流式播放。
2. 用户抢话时，设备先停止本地播放、清空缓冲，并通知协调器；通知应携带回复身份与已播放位置。
3. 协调器失效旧回复，语音适配器取消生成，并按供应商支持的机制对齐会话历史，避免把未播放内容当成用户已听到的内容。
4. 新轮次 `t8` 开始后，旧回复迟到音频不能重新进入播放队列；下一次依赖历史的生成，应先完成旧条目对齐或重建上下文。
5. 已派发写入按 `operation_id` 核实并持久记录业务回执，界面据此展示真实状态。停止播放不会撤销写入，也不能成为重复派发的理由。

图中“历史对齐”包括明确回复／内容项身份和已播放边界；具体截断、缓冲清理与确认事件应由供应商适配器转换，不应直接照搬本图的教学字段。实际系统还应验证连接代次、用户权限、重复／乱序帧、有界缓冲与硬件回声处理。

## 生成与校验

在已安装的 Archify 技能目录执行，资产路径按实际位置替换：

```bash
node bin/archify.mjs validate sequence <资产目录>/interruption-sequence.json --quality showcase --json
node bin/archify.mjs deliver sequence <资产目录>/interruption-sequence.json <资产目录>/interruption-sequence.html --quality showcase --json
node bin/archify.mjs visual-check <资产目录>/interruption-sequence.html --json
```

从交互 HTML 的“导出 → SVG”获取正文矢量图；发布副本仅清理行尾空白，不改变图面样式。

2026-11-03 验证记录：9/9 showcase 检查通过，组合错误 0、警告 0；1440×900、1600×1000、1920×1080、2048×1320 均无页面溢出。已查看最小尺寸浅色和最大尺寸深色截图，参与方、消息标签、分段和结论卡均可读。自动 visual-check 回执只报告视觉审阅待处理，人工查看结论单独记录于此。本机截图与诊断回执不作为发布资源。

| 文件 | SHA-256 | 字节数 |
| --- | --- | ---: |
| `interruption-sequence.json` | `2adf302dfc8ea474e3bab28e8b49727b37b069cc595e78d5d07a2f60a101b452` | 3279 |
| `interruption-sequence.html` | `e07cefaa05493560f82c9cae5b2fef74a6b45e8ffba5d6c09282df1a032f11dd` | 709347 |
| `interruption-sequence.svg` | `9de95a8de2fb7f48c3e21d688d0887833301272fdab3d737e9d8a9063002d494` | 53453 |
