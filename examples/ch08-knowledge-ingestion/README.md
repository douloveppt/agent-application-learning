# 第八章 知识加工与发布离线实验

本实验使用 Python 3.11+ 标准库和 SQLite 内存数据库，演示来源修订、带定位的分块、质量门禁、候选发布与删除栅栏。直接运行不会访问网络、企业系统或模型，也不需要 API Key。

在仓库根目录执行：

```bash
python3.11 examples/ch08-knowledge-ingestion/lab.py
python3.11 -m unittest discover -s examples/ch08-knowledge-ingestion -p 'test_*.py' -v
```

`lab.py` 输出一份 JSON；每次运行从空数据库开始，进程结束不保留数据。

## 逐步观察

1. `receive(scope, doc_id, revision, raw_content, source_uri)` 接收合成的单调来源版本；同版本同载荷返回 `replayed`，不同载荷拒绝。接收新版立即停用旧发布，采用严格新鲜策略。
2. `build(scope, doc_id, source_revision, pipeline_version, blocks, max_chars=120)` 接收已解析的 `{text, page, section}` 块，检查正文与定位，按字符切分，保留块序号和字符区间。全部写入同一个 SQLite 事务，候选尚不可检索。
3. `publish(scope, generation, expected_active_generation)` 在事务中复核当前来源修订、来源指纹、候选状态和发布指针。指针 CAS 防止持有旧快照的发布任务覆盖已经切换的版本。
4. `search(scope, literal)` 用字面匹配演示候选查找，并在同一条 SQL 中核对租户、当前来源修订和发布指针，返回含来源定位的片段。
5. `delete(scope, doc_id, expected_revision)` 递增版本并留下删除标记。旧 build、publish、receive 都不能让文档重新可用；本实验没有重新启用接口。

`pipeline_version` 必须包含会影响产物的配置变化。实验以同一个 generation 下的 manifest 指纹检测“未改版本却改变分块参数”等不一致；生产系统还需管理完整的解析器、规范化器、分块器和 Embedding 配置。

## 调用一个最小流程

在实验目录启动 Python 后，可运行下面的代码。`None` 表示可信调用方预期尚无当前制品；如果另一任务已切换指针，发布将报告冲突，而不是覆盖它。

```python
import sqlite3
from lab import KnowledgeStore, Scope

connection = sqlite3.connect(':memory:')
try:
    store, scope = KnowledgeStore(connection), Scope('demo-company')
    text = 'VPN 连接失败时，先检查客户端版本。'
    store.receive(scope, 'vpn-guide', 1, text, 'kb://it/vpn')
    blocks = [{'text': text, 'page': 2, 'section': 'VPN / 基础排查'}]
    generation = store.build(scope, 'vpn-guide', 1, 'parser1-chunker1', blocks)
    store.publish(scope, generation, expected_active_generation=None)
    assert store.search(scope, 'VPN')[0]['source_revision'] == 1
finally:
    connection.close()
```

`expected_active_generation` 由可信调用方提供。指针 CAS 可以拒绝持有旧快照的竞争发布，但不能阻止调用方主动读取新指针后，再将旧管道制品作为一次新的发布；允许发布哪个管道版本仍需受控发布政策。

## 演示输出

直接运行 `lab.py`，输出如下。`source_v2` 展示新版确实包含新增步骤；删除后的结果检查发生在旧发布再次被拒绝之后。

```json
{
  "first_source": {
    "doc_id": "vpn-guide",
    "source_revision": 1,
    "page": 2,
    "section": "VPN / 基础排查"
  },
  "receive_replay": "replayed",
  "hidden_while_building": true,
  "stale_publish_error": "stale_revision",
  "source_v2": {
    "source_revision": 2,
    "text": "VPN 连接失败时，请先检查客户端版本。仍失败时记录错误码并联系 IT。"
  },
  "deletion_revision": 3,
  "late_publish_error": "document_deleted",
  "visible_after_delete": false
}
```

## 验证边界

- `Scope` 来自可信合成调用者，只演示租户键约束；没有真实登录、ACL、委托、撤权或源系统权限校验。
- `blocks` 是手工合成的解析结果；代码没有验证它们与原始文件相符。原文内容哈希也不证明来源权威性。示例 URI 为合成定位符。
- 分块采用字符数，`max_chars=120` 是演示参数，不是 Token 预算；没有 PDF、OCR、布局、表格、重叠分块或语义质量测量。
- 索引用 SQLite 表表示，字面匹配不属于向量检索、BM25 或 RAG 效果评测。
- 版本是测试代码提供的单调整数；没有实现源游标、分页、增量扫描、消息队列、Outbox、真实异步回执或未知结果对账。
- SQLite 单连接事务及触发器故障实验不证明多进程并发、跨服务原子提交、崩溃恢复或真实向量库读写可见性。
- 删除只停止此实验的查询资格，旧候选与正文仍留在内存表中；没有物理擦除、备份处理或完整派生副本清理。
- 一个事务中的读取校验不保证结果返回后到模型外发之前不会撤权；生产系统需定义额外的外发边界。

这是生命周期控制的教学样例，不能直接作为企业知识平台部署。
