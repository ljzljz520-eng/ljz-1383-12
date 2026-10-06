# 成长站 · 阅读书影音档案

记录「作品—版本—多次经历」三层档案，支持访客按年份/主题查询、管理端录入与离线导入、
外部标识匹配与人工冲突审阅、笔记三态发布，以及口径清晰的年度统计。

## 运行

```bash
cd archive
npm start                 # 默认 http://localhost:4173/archive/
# 可选环境变量
PORT=4173 ADMIN_TOKEN=你的令牌 ARCHIVE_DB=./data/archive.json node server.js
```

- 访客页： http://localhost:4173/archive/
- 管理端： http://localhost:4173/archive/admin.html （默认令牌 `dev-token`，在页首填入；生产务必用 `ADMIN_TOKEN` 覆盖）
- 样例导入文件：管理端「离线导入」可下载 `sample-import.csv`

## 验收

```bash
npm test                  # node test/acceptance.js，36 项断言
```

覆盖：离线重传幂等、错误合并后拆分、来源链接变更留痕、主题重命名、
分页过程中发布/新增条目、作品/事件两种统计口径、合并来历，以及
搜索摘要/分享卡/导出三处共用的笔记可见规则。

## 数据模型（`data/archive.json`）

- `works` 作品（书/影/音；externalIds；mergedInto 合并指针）
- `editions` 版本：译本、印刷版、录音版、剪辑版……
- `events` 经历事件：每次读/看/听一条，重读 `revisit:true`
- `notes` 笔记：`visibility=private|pending|published` + 不可变 `publishedVersion`
- `themes` 主题（重命名后旧名进 `aliases`）
- `sources` 来源（`kind:'clue'` 引用线索；变更保留 `history`）
- `imports` 导入批次：原始行 `rawLines` + 行级幂等键
- `conflicts` 冲突审阅队列；`mergeLog` 合并/拆分审计（含快照与迁移清单）

## 规则速览

1. **只凭标题绝不自动合并**。外部标识（ISBN/IMDB/UPC/ISRC/MusicBrainz/豆瓣/Goodreads…）一致才自动归属；
   同名缺证据 → 冲突队列 → 人工 `merge`（带证据）或 `keep`（独立）。
2. **重读 = 新事件**，统计中「作品数」与「事件数」分别给出并标注口径。
3. **一条可见性规则**：`notePublicView` 是搜索/分享卡/导出的唯一出口；
   导入内容默认私密，发布时逐条勾选摘录，第三方来源仅标注为引用线索，不构成转载授权。

详见 [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) 的逐条需求对照。

## 目录

```
archive/
  server.js            零依赖 HTTP 服务（静态页 + 公开/管理 API）
  lib/store.js         JSON 持久层（原子写、串行化）
  lib/domain.js        匹配、作品/版本/事件、合并拆分、笔记三态
  lib/imports.js       CSV 离线导入（幂等、原始行留档）、主题/来源维护
  lib/conflicts.js     冲突审阅与合并来历
  lib/query.js         访客查询、游标分页、双口径统计、分享卡、导出
  public/              访客页 index.html / 管理端 admin.html / 样例 CSV
  test/acceptance.js   验收脚本
  docs/REQUIREMENTS.md 需求对照
```
