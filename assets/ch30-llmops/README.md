# 第 30 章图解：企业 Agent 的发布与恢复路径

本图表达企业 Agent 的发布控制流程。使用 Archify 的 `architecture` 图类型横向展示步骤及恢复分支；图标分类用于区分教学职责，不代表已经部署的生产组件。

- `release-lifecycle.html`：自包含交互图，可下载后在浏览器打开，支持主题、缩放和导出。
- `release-lifecycle.svg`：通过交互页原生 SVG 导出、仅清理行尾空白得到的静态主图，便于 Markdown 与 Obsidian 预览。
- `release-lifecycle.json`：可复现图形布局与语义关系的源规格。

## 内容边界

主链为确定候选变更组合、构建与封存制品、兼容性和离线评测门禁、测试环境演练、小流量灰度、生产观察、全量激活。构建获得实际制品内容摘要并完成安全验证后，才封存完整的 ReleaseManifest。观察发现异常时，停止向候选版本分配新流量，隔离影响并核实；兼容性确认后选择回退或前滚。

版本切换默认只影响新任务，进行中任务固定原版本。状态、数据库和索引的兼容性需要单独验证；外部业务副作用需要核实，未知写入不能因应用回滚而自动重发。图中没有设定通用灰度比例、观察时长或放量阈值，具体条件由正文中的任务风险与发布合同决定。

## 验证

使用 Archify 2.16 完成 `validate` 与 `deliver`：9/9 showcase 检查通过，错误和警告均为 0。交互页在 1440×900、1600×1000、1920×1080、2048×1320 四种桌面尺寸下均无水平或垂直溢出；对最小、最大尺寸的浅色与深色截图完成逐图查看。静态 SVG 通过 XML 解析、浏览器渲染与图像查看；不包含脚本或本机绝对路径。

交付摘要：

```text
specification_sha256: 70ff6991bcc64f29ae60b2ff007121fd59b46f344d49110481ff4e524854d023
html_sha256: ac96cea19b005c32f0f880f54c736179b5dd82d1ea33e1b06a9206e5f1038787
validation: 9/9 showcase, 0 errors, 0 warnings
visual_review: passed
```

使用已安装的 Archify 重建时，在技能目录执行下列命令，将占位路径替换为实际路径；修改后需要重新导出静态 SVG 并复核。

```bash
node bin/archify.mjs validate architecture /path/to/release-lifecycle.json --quality showcase --json
node bin/archify.mjs deliver architecture /path/to/release-lifecycle.json /path/to/release-lifecycle.html --quality showcase --json
node bin/archify.mjs visual-check /path/to/release-lifecycle.html --json
```

静态图导出：在可信交互页的“导出”菜单选择 SVG，保留完整主图；不要将截图、放大后的局部视图或浏览器运行状态当作源图。
