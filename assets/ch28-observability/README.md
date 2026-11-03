# 第 28 章：从异常信号到可核实的诊断

这份图示是企业 Agent 的教学排障流程，不代表任何生产系统已经接入或通过验收。

- [交互 HTML](./incident-diagnosis-workflow.html)：下载后在浏览器打开，支持主题、缩放、搜索与导出。
- [静态 SVG](./incident-diagnosis-workflow.svg)：双主题矢量预览，供章节正文使用。
- [Archify JSON](./incident-diagnosis-workflow.json)：可复查的流程源规范。

先确认影响范围与时间窗，再关联 Run、Attempt 和 Trace；沿执行时间线定位异常，涉及外部写入时核实原操作与回执，最后记录已证实的事实和证据缺口，并形成修复与回归。图中下排从右向左阅读。没有 Trace 或收到超时，都不能证明外部没有执行；后续动作继续遵守权限与审批。

## 生成与验证

使用 Archify 2.16 `workflow` 渲染器，`showcase` 质量配置。`deliver` 校验冻结的 JSON 后原子交付 HTML；SVG 由同一 HTML 的标准导出器生成，收据 `canonical=true`。SVG 仅清除了行尾空白，未改图形或文字。

- 几何与产物：9/9 检查通过，composition 0 errors、0 warnings。
- 桌面容纳：1440×900、1600×1000、1920×1080、2048×1320 均无横向或纵向溢出。
- 视觉复核：已实际检查最小与最大桌面截图的浅色、深色主题，节点、连线、标签清晰，无相交遮挡。
- 修正轮数：1 轮，修正边界节点与主路径声明；下排保留真实方向。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| JSON | 2690 | `1f88a0dde7cce25ea9cb320c8c80625338262c4e29cc9fb18b91827a9ddf2e0f` |
| HTML | 705918 | `7b7e8ed4f1c5b9853824accd386b7d3dc8a15659a22651b6eb78d1f952ec1356` |
| SVG | 50102 | `2a316dfb9a230617a221371a3bf999b0ab346016eb4f229f23a6748578e75958` |

自动截图收据的 `visualReview` 固定为 `pending`，上述视觉结论来自实际图像检查。含本机路径的截图收据与临时浏览器文件只保存在本地，不进入公开仓库。
