# 成长站 · 阅读书影音档案

为成长站建设的「书 / 影 / 音」档案：Web 管理端录入作品、版本、多次阅读经历与个人收获；
访客页按年份与主题浏览、搜索；支持离线 CSV 导入、冲突人工审阅、错误合并拆分、
来源链接变更留痕、主题重命名、分页中稳定发布新条目。

**零外部依赖**：Python 3.10+ 标准库 + SQLite 自带 FTS5，完全离线运行；
不抓取任何第三方页面。

## 启动

```bash
cd archive
./run.sh                 # 首次自动写入演示数据
# 或手动：
python3 seed.py data/archive.db
ARCHIVE_ADMIN_TOKEN=你的令牌 python3 -m app.server --db data/archive.db --port 8000
```

- 访客页：http://localhost:8000/
- 管理端：http://localhost:8000/admin （令牌即 `ARCHIVE_ADMIN_TOKEN`，本地默认 `dev-token`）
- 导入文件：`sample-import.csv`

测试：

```bash
python3 -m unittest discover -s tests -v   # 25 个用例，全部覆盖验收场景
```

## 数据模型（为什么这样设计）

| 层 | 表 | 含义 |
|---|---|---|
| 作品 | `works` | 抽象作品，如《1984》《东邪西毒》《月之暗面》 |
| 标识 | `work_identifiers` | 作品级外部证据（goodgrid / imdb / mb_releasegroup / isbn_work…） |
| 版本 | `versions` + `version_identifiers` | 具体译本、版次、录音、发行（ISBN/UPC/EAN…） |
| 经历 | `events` + `event_topics` | 一次阅读/观看/聆听；**重读是新的一行事件** |
| 笔记 | `notes` + `note_published_snapshots` | 私密 / 待公开 / 已发布 / 已归档四态 |
| 导入 | `import_batches` / `import_rows` | 内容哈希幂等 + **每一行原文逐字保留** |
| 审阅 | `conflicts` | 标题相似 / 标识冲突 / 来源变更的人工队列 |
| 合并 | `merge_groups` / `merge_members` / `merge_log` | 合并来历完整可回放、可拆分 |
| 来源 | `sources` / `source_link_changes` | 链接变更历史 |
| 主题 | `topics` / `topic_renames` | slug 恒定，旧名可解析 |

### 「不能只凭标题自动合并」如何落地

匹配优先级（`app/identifiers.py`、`importer.match_row`）：

1. **作品级强标识精确一致** → 同一作品；同一命名空间指向多个作品 → `id_mismatch` 冲突；
2. **版本级标识（ISBN 等）一致** → 同一具体版本（并归属其作品）；不同 ISBN 即使标题完全相同也是不同版本；
3. **人工证据链接** → 只作为审阅线索，必须人工在冲突队列里确认并**填写理由**；
4. **仅标题相同**：
   - 同名且作者也相同 / 无作者信息 → `title_only` 冲突，绝不自动合并；
   - 同名但作者明显不同 → 直接作为不同作品（如同名电影）。

同一作品下新增「不同译本 / 不同录音」会生成**新的 version**，而不是覆盖或合并。

冲突审阅流程（管理端「冲突审阅」）：查看候选作品与原始行 →
**保持独立**（新建作品并回放该行落地事件与私密笔记）/
**确认同一作品**（选目标 + 填理由，写入 `merge_log` 合并来历）/
**取消**（原始行保留，状态 skipped）。

## 统计口径（页面明示）

- **事件数**：阅读 / 观看 / 聆听经历次数。重读、重看、重听各算一次，是新的经历，不是重复数据。
- **作品数**：去重后的作品数；同一作品的不同译本 / 版本只计一次；错误合并拆分后重新分开计数。
- 公开统计只含已发布笔记。访客「统计」页与 JSON 都带 `counting_basis` 说明。

## 统一可见性规则（一份规则，四处生效）

`visibility ∈ private / pending / published / archived`。
**列表、搜索摘要、分享卡 `/share/<id>`、导出** 全部只输出 `published`：

- 私密 / 待公开内容不会出现在访客页、搜索结果、分享卡（404）、公开导出；
- 导入产生的摘录一律 `private` 且 `from_import=1`；**站主不勾选发布，就永远不会自动公开**；
- 第三方**摘录文本不参与公开搜索索引**（只索引站主正文与标题），避免被动转载；
- 发布产生快照；归档后从所有公开表面撤下；管理端搜索与全量导出需令牌，可见全部。

## 分页中发布新条目

访客动态使用 keyset（seek）分页：按 `(published_at DESC, id DESC)`，游标为不透明 base64。
翻页期间发布的新条目只会出现在最新一页，不会插进读者正在浏览的位置，无重复无跳条
（测试 `test_keyset_pagination_stable_while_publishing`）。

## 验收场景对照

| 验收项 | 入口 | 测试 |
|---|---|---|
| 离线导入重传 | 内容 SHA-256 相同 → 返回同一批次 `idempotent:true`，不写任何数据 | `test_reimport_same_bytes_is_idempotent`、HTTP `test_02` |
| 错误合并后拆分 | 管理端「合并/拆分」→ 拆分恢复作品；可再重新归属版本/事件/笔记 | `test_wrong_merge_split_restores_work`、HTTP `test_09` |
| 来源链接变更 | 「来源」面板改 URL，写入 `source_link_changes` | `test_source_url_change_history`、HTTP `test_08` |
| 主题重命名 | slug 不变，旧名经 `topic_renames` 仍可解析，页面显示曾用名 | `test_topic_rename_keeps_links`、HTTP `test_07` |
| 分页中发布新条目 | 游标翻页稳定 | `test_keyset_pagination_stable_while_publishing`、HTTP `test_06` |
| 合并来历展示 | 作品页「合并来历」（成员、理由、证据、merge/split 日志） | HTTP `test_11` |
| 同名不自动合并 | 不同 ISBN 的 1984 译本靠共同作品 ID 关联为两版本；无证据同名进冲突队列 | `test_same_title_different_isbn_not_merged`、`test_title_only_creates_conflict` |
| 重读计数 | 同作品两条事件；年度 events/works 分列 | `test_reread_is_new_event_and_stats_basis` |

## CSV 格式（utf-8，首行表头）

`title, kind, event_kind, event_date` 必填；可选：
`creator, origin_year, external_ids, version_label, publisher, medium,
version_year, source, source_url, topics(| 分隔), rating, personal_gain,
excerpt, excerpt_from, note, evidence_url`

`external_ids` 形如 `goodgrid:12000;isbn:978-7-5327-7753-2; imdb:tt0109688`
（分号分隔；命名空间不区分大小写，ISBN10 自动归一到 ISBN13，IMDB 补齐 `tt`+7 位）。

## 主要 HTTP 接口

- 公开：`GET /api/public/feed|years|years/{y}|topics|topics/{slug}|search?q=|stats|works/{id}|export[?format=markdown]`，`GET /share/{note_id}`
- 管理（`X-Admin-Token`）：作品 / 版本 / 事件 / 主题 / 笔记 CRUD，
  `/api/admin/match-check`、`/import`、`/conflicts`、`/conflicts/{id}/resolve`、
  `/merge`、`/merge-groups/{id}/split`、`/reassign/{version,event,note}`、
  `/sources`、`/sources/{id}/url`、`/stats`、`/export`

## 合规边界（已内置）

1. 第三方页面 / ISBN / 条目**仅作引用线索**，系统不执行任何抓取；`evidence_url` 与
   `source_url` 只保存链接，不代表授权。
2. 抓取或导入成功 ≠ 内容转载授权：第三方摘录默认私密、不进公开搜索，是否公开由站主逐条勾选。
3. 所有自动决策可追溯到外部标识或人工理由；人工合并必须填理由，错误可拆分且来历完整。
