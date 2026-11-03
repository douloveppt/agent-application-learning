# 第 29 章图解：预算准入与模型路由

本图表达教学控制链，节点分类用于区分职责，不代表已部署的生产组件。正文中的资源不足处理、费用核对和未知写入规则仍然适用。

- `model-routing-workflow.html`：自包含交互图，可下载后在浏览器打开，支持主题、缩放和导出。
- `model-routing-workflow.svg`：与交互图对应的静态主图，便于 Markdown 与 Obsidian 预览。
- `model-routing-workflow.json`：Archify 源规格；使用 `architecture` 图类型表达控制关系。

## 内容边界

主链为任务合同、能力与数据政策筛选、原子预算预留、模型执行、证据与质量检查、合格交付。只读质量不足时可受限升级，重新通过准入；未知写入先核实，不能通过换模型重放。预留与实际结算不同，未知费用保留待核对；资源不足时按合同部分交付、延期或明确拒绝。

## 验证

使用 Archify 2.16 完成 `validate` 与 `deliver`：9/9 showcase 检查通过，错误和警告均为 0。交互页在 1440×900、1600×1000、1920×1080、2048×1320 四种桌面尺寸下无水平或垂直溢出；最小、最大尺寸的浅色和深色截图均经过人工查看。静态 SVG 通过 XML 解析与浏览器渲染检查。

交付摘要：

```text
specification_sha256: 32510a867326679fb8e5a8bb86e7807604a5713741c733ad05de7d43e0264b32
html_sha256: 9bd341e484e4d66f48229dc9994cba5b008ec92ef605989009bc381c652885f3
```

使用已安装的 Archify 重建时，在技能目录执行下列命令，并将占位路径替换为实际路径；修改后需要重新生成静态 SVG 并复核。

```bash
node bin/archify.mjs validate architecture /path/to/model-routing-workflow.json --quality showcase --json
node bin/archify.mjs deliver architecture /path/to/model-routing-workflow.json /path/to/model-routing-workflow.html --quality showcase --json
node bin/archify.mjs visual-check /path/to/model-routing-workflow.html --json
```
