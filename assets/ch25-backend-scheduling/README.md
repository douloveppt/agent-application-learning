# 第 25 章配图：任务后端与产物归属

本图是企业 Agent 应用的教学架构，描述一套可恢复调度的职责分工，不代表某个已部署系统。具体队列、数据库与对象存储可按容量和运维能力选型。

- `backend-control-data-flow.svg`：章节正文矢量配图，由交互 HTML 的原生 SVG 导出生成，跟随深浅色偏好。
- `backend-control-data-flow.html`：独立交互图，支持主题切换、缩放、搜索、关系追踪与导出。GitHub 中可下载后在浏览器打开。
- `backend-control-data-flow.json`：Archify architecture 源文件。

## 怎样读图

1. API 从受信身份取得租户与用户，检查会话归属和准入配额，将任务写入 SQL 后再确认接收。
2. SQL 保存 Session、Run 和产物元数据，作为任务状态的事实源。“待调度记录”箭头表示 Dispatcher 读取记录，不表示数据库自动推送。
3. Dispatcher 向有界队列投递 Run 引用。队列提供唤醒与解耦，丢失唤醒可由登记表扫描补发；重复唤醒由领取条件消化。
4. Worker 必须向 SQL 原子领取任务，带有效租约和 `lease_epoch` 续约与回写。数据库条件必须阻止失效 Worker 覆盖后来进度。
5. 每次 Attempt 使用独立临时工作区；正式文件经产物服务保存到对象存储。会话保存稳定产物引用，下载仍需服务端鉴权。

为保持主线清楚，图中省略了回传进度、产物元数据提交、定时触发、审计、扫描器与清理器等支路。正文会解释这些能力，以及 SQL 与对象存储跨系统提交的失败窗口。队列消费与租约本身不保证外部业务写入只发生一次；相关幂等和对账见第 26 章。

## 生成与校验

在已安装的 Archify 技能目录执行，路径替换为实际资产目录：

```bash
node bin/archify.mjs validate architecture <资产目录>/backend-control-data-flow.json --quality showcase --json
node bin/archify.mjs deliver architecture <资产目录>/backend-control-data-flow.json <资产目录>/backend-control-data-flow.html --quality showcase --json
node bin/archify.mjs visual-check <资产目录>/backend-control-data-flow.html --json
```

从交互 HTML 的“导出 → SVG”取得正文配图，再统一清理行尾空白；不改变图面、样式或主题。不要直接截取 HTML 中未经导出清理的 SVG，以免丢失主题样式或带入浏览器探索状态。

2026-11-03 验证记录：9/9 showcase 检查通过，组合错误 0、警告 0；1440×900、1600×1000、1920×1080、2048×1320 均无页面溢出。已查看最小尺寸浅色和最大尺寸深色截图，主路径、领取关系、临时工作区与正式产物的分工清晰，节点和结论卡无裁切。自动 visual-check 回执仍报告 `visualReview: pending`；人工查看结论单独记录于此。本机截图与诊断回执不作为发布资源。

| 文件 | SHA-256 | 字节数 |
| --- | --- | ---: |
| `backend-control-data-flow.json` | `43b5f44502131030c218534d9c8d4a06eed4acce5c2d10405e67076639aa71a2` | 4411 |
| `backend-control-data-flow.html` | `6296f00a9ad0c737963000fea18eba202a176712efad7ee9d981eb695398cab9` | 711061 |
| `backend-control-data-flow.svg` | `80a07f5e9ba1f5f6f447cdaffc81940e4235ce9dff0dbaefd134dd9b6fde3e8b` | 54590 |
