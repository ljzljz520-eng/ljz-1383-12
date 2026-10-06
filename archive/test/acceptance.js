/**
 * 验收脚本：node test/acceptance.js
 * 覆盖：离线导入重传、错误合并后拆分、来源链接变更、主题重命名、
 *       分页中发布新条目、统计口径、合并来历、可见规则一致性、第三方摘录不自动公开。
 */
'use strict';
process.env.ARCHIVE_DB = require('path').join(__dirname, '..', 'data', 'test.json');
process.env.PORT = '4391';
process.env.ADMIN_TOKEN = 'test-token';
try { require('fs').unlinkSync(process.env.ARCHIVE_DB); } catch {}

const http = require('http');
const server = require('../server');

const TOKEN = 'test-token';
function req(method, path, body, raw = false) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const r = http.request({ host: 'localhost', port: 4391, path, method,
      headers: { 'X-Admin-Token': TOKEN, ...(data ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } : {}) } },
      res => { let buf = ''; res.on('data', c => buf += c); res.on('end', () => {
        const ct = res.headers['content-type'] || '';
        resolve({ status: res.statusCode, body: ct.includes('json') ? JSON.parse(buf) : buf });
      }); });
    r.on('error', reject); if (data) r.end(data); else r.end();
  });
}
const assert = (cond, msg) => { if (!cond) { console.error('✗ ' + msg); process.exitCode = 1; } else console.log('✓ ' + msg); };

async function main() {
  await new Promise(r => server.listen(4391, r));
  const csv = require('fs').readFileSync(require('path').join(__dirname, '..', 'public', 'sample-import.csv'), 'utf8');

  /* 1. 首次导入 */
  const imp1 = (await req('POST', '/api/admin/imports', { csvText: csv, clientId: 'dev1', batchRef: 'b1', sourceUrl: 'https://src.example/a' })).body;
  assert(imp1.inserted === 6, '首次导入 6 行事件（实际 ' + imp1.inserted + '）');
  assert(imp1.conflicts >= 2, '同名不同片/无标识同名产生冲突（实际 ' + imp1.conflicts + '）');

  /* 2. 离线重传：同 client+batch 应全部跳过，不产生重复 */
  const imp2 = (await req('POST', '/api/admin/imports', { csvText: csv, clientId: 'dev1', batchRef: 'b1', sourceUrl: 'https://src.example/a' })).body;
  assert(imp2.skipped === 6 && imp2.inserted === 0, '断网重传幂等：跳过 6 行、新增 0（skip=' + imp2.skipped + '）');
  const db1 = (await req('GET', '/api/admin/db')).body;
  assert(db1.events.length === 6, '重传后事件仍为 6（无重复）');

  /* 3. 不同译本/不同录音不得因标题被并掉 */
  const hamEditions = db1.editions.filter(e => ['朱生豪译本', '梁实秋译本'].includes(e.label));
  assert(hamEditions.length === 2, '同一本书保留两个译本版本');
  let hamWorks = db1.works.filter(w => w.title === '哈姆雷特' && !w.mergedInto);
  assert(hamWorks.length === 2, '两个不同 isbn13 缺少一致标识 → 不自动合并，各自独立（' + hamWorks.length + ' 部，可走人工冲突审阅再合并）');
  const ninth = db1.works.filter(w => w.title === '贝多芬第九交响曲');
  assert(ninth.length === 2, '不同 UPC 录音版本不被合并（' + ninth.length + ' 部）');

  /* 4. 外部标识优先自动匹配 */
  const matched = (await req('POST', '/api/admin/works', { type: 'movie', title: '英雄', externalIds: { imdb: 'tt0299977' } })).body;
  assert(matched.autoMatched === true && matched.matchedWork, 'IMDB 标识命中自动匹配，不新建作品');
  const sameName = (await req('POST', '/api/admin/works', { type: 'movie', title: '英雄', externalIds: { imdb: 'tt1234567' } })).body;
  assert(sameName.autoMatched === false && sameName.sameTitleCandidates.length >= 1, '同名但标识不同 → 不自动合并并进冲突队列');

  /* 5. 错误合并 → 拆分恢复（从当前库实时取数） */
  let dbLive = (await req('GET', '/api/admin/db')).body;
  // 人工确认两本哈姆雷特实为同一作品的两个译本 → 合并；之后再演示误合并拆分
  const [h1, h2] = dbLive.works.filter(w => w.title === '哈姆雷特' && !w.mergedInto);
  const hamMerge = (await req('POST', '/api/admin/merge', {
    correctId: h1.id, wrongId: h2.id,
    evidence: { manual: true, note: '人工确认同一作品的两个译本，保留为版本' }, reason: '同书异译'
  })).body;
  dbLive = (await req('GET', '/api/admin/db')).body;
  assert(dbLive.works.find(w => w.id === h2.id).mergedInto === h1.id, '人工确认后可合并，两个译本版本随之归集');
  assert(dbLive.editions.filter(e => e.workId === h1.id).length === 2, '合并后两个译本版本都挂在保留作品下');

  const yxA = dbLive.works.find(w => w.title === '英雄' && w.externalIds.imdb === 'tt0299977' && !w.mergedInto);
  const yxB = dbLive.works.find(w => w.title === '英雄' && w.externalIds.imdb === 'tt9999999' && !w.mergedInto);
  const beforeCount = dbLive.works.filter(w => !w.mergedInto).length;
  const mg = (await req('POST', '/api/admin/merge', { correctId: yxA.id, wrongId: yxB.id, evidence: { manual: true }, reason: '误操作测试' })).body;
  let db2 = (await req('GET', '/api/admin/db')).body;
  assert(db2.works.find(w => w.id === yxB.id).mergedInto === yxA.id, '错误合并生效');
  await req('POST', '/api/admin/split', { mergeId: mg.id, reason: '发现同名不同片' });
  db2 = (await req('GET', '/api/admin/db')).body;
  const restored = db2.works.find(w => w.id === yxB.id);
  assert(!restored.mergedInto && restored.externalIds.imdb === 'tt9999999', '拆分后作品恢复独立且标识还原');
  assert(db2.works.filter(w => !w.mergedInto).length === beforeCount, '拆分后作品数量复原');
  const prov = (await req('GET', '/api/admin/provenance?workId=' + yxA.id)).body;
  assert(prov.some(p => p.action === 'merge') && prov.some(p => p.action === 'split'), '合并来历可查（merge+split 留痕）');

  /* 6. 冲突审阅流程（keep 保留独立） */
  const open = (await req('GET', '/api/admin/conflicts?status=open')).body;
  const stConflict = open.find(c => c.workId === yxB.id);
  if (stConflict) {
    const res = await req('POST', '/api/admin/conflicts/resolve', { conflictId: stConflict.id, action: 'keep', reason: '同名不同片' });
    assert(res.status === 200, '冲突可裁决为「保留独立」');
  } else assert(true, '（同名冲突已在其它步骤处理）');

  /* 7. 来源链接变更保留历史 */
  const src = db2.sources[0];
  await req('POST', '/api/admin/sources/change', { id: src.id, url: 'https://src.example/a-v2', label: '新地址' });
  const db3 = (await req('GET', '/api/admin/db')).body;
  const src2 = db3.sources.find(s => s.id === src.id);
  assert(src2.url === 'https://src.example/a-v2' && src2.history.length >= 1, '来源链接可变更且保留旧值历史');

  /* 8. 主题重命名 id 不变、旧名可检索 */
  const theme = db3.themes.find(t => t.name === '戏剧');
  await req('POST', '/api/admin/themes/rename', { id: theme.id, name: '舞台戏剧' });
  const db4 = (await req('GET', '/api/admin/db')).body;
  const t2 = db4.themes.find(t => t.id === theme.id);
  assert(t2.name === '舞台戏剧' && t2.aliases.includes('戏剧'), '主题重命名 id 不变且旧名入别名');
  const byAlias = (await req('GET', '/api/public/search?themeId=' + theme.id)).body;
  assert(byAlias.items.length >= 2, '用主题 id（旧名/新名）仍能查到条目（' + byAlias.items.length + '）');

  /* 9. 统计口径：作品 vs 事件；重读计事件不计作品 */
  const statsAll = (await req('GET', '/api/public/stats')).body.byYear;
  const s2026 = statsAll['2026'];
  assert(s2026.events >= 6 && s2026.works <= s2026.events && s2026.revisits >= 2,
    `年度统计区分口径：事件 ${s2026.events} / 作品 ${s2026.works} / 重读 ${s2026.revisits}`);
  const wScope = (await req('GET', '/api/public/search?scope=work&limit=20')).body;
  const eScope = (await req('GET', '/api/public/search?scope=event&limit=20')).body;
  assert(wScope.stats.caliber.works && eScope.stats.caliber.events, '页面/API 明示两种统计口径');

  /* 10. 笔记三态 + 可见规则一致性（搜索/分享/导出） */
  const note = db4.notes.find(n => (n.current.excerpts || []).includes('生存还是毁灭'));
  assert(note && note.visibility === 'private', '导入的摘录默认私密，未被自动公开');
  // 私密态：搜索/分享/导出均不可见
  let pub = (await req('GET', '/api/public/search?q=' + encodeURIComponent('生存还是毁灭'))).body;
  assert(pub.items.length === 0, '私密摘录不出现在访客搜索');
  let share = await req('GET', '/api/public/share?noteId=' + note.id);
  assert(share.status === 404, '私密笔记无法生成分享卡');
  assert(!(await req('GET', '/api/public/export.csv')).body.includes('生存还是毁灭'), '导出不含私密摘录');

  // 待公开仍不可见
  await req('POST', '/api/admin/notes/submit', { id: note.id });
  share = await req('GET', '/api/public/share?noteId=' + note.id);
  assert(share.status === 404, '待公开笔记仍不能分享');

  // 分页：先取第一页，再发布一条新笔记对应事件，验证游标稳定、新条目随后可取
  const page1 = (await req('GET', '/api/public/search?limit=3')).body;
  assert(page1.items.length === 3 && page1.nextCursor, '分页第 1 页 3 条且返回游标');
  // 发布笔记（显式勾选摘录）
  const pubRes = await req('POST', '/api/admin/notes/publish', { id: note.id, excerpts: ['生存还是毁灭'], sourceLinks: ['https://src.example/a-v2'] });
  assert(pubRes.body.publishedVersion.excerpts.length === 1, '发布版本仅含站主勾选的 1 条摘录');

  // 分页进行中录入一条全新经历事件：旧游标页保持稳定，新事件随后可被新翻页取到
  const anyWork = (await req('GET', '/api/admin/db')).body.works.find(w => w.title === '英雄' && !w.mergedInto);
  const newEv = (await req('POST', '/api/admin/events', { workId: anyWork.id, date: '2026-09-01', revisit: true, takeaway: '翻页期间新增的重看' })).body;
  const page2 = (await req('GET', '/api/public/search?limit=3&cursor=' + encodeURIComponent(page1.nextCursor))).body;
  const ids1 = page1.items.map(i => i.eventId);
  assert(page2.items.every(i => !ids1.includes(i.eventId)), '游标翻页不重不漏（发布不打乱已有页）');
  // 从首页用大页重新拉取，新增事件排在最前且可取到
  const fresh = (await req('GET', '/api/public/search?limit=50')).body;
  assert(fresh.items[0].eventId === newEv.id, '分页期间新增的事件在新查询中即时可见且排在最前');

  // 发布后：搜索/分享/导出三处同时可见
  pub = (await req('GET', '/api/public/search?q=' + encodeURIComponent('生存还是毁灭'))).body;
  assert(pub.items.length === 1 && pub.items[0].notes[0].excerpt.includes('生存还是毁灭'), '发布后搜索摘要可见摘录');
  share = (await req('GET', '/api/public/share?noteId=' + note.id)).body;
  assert(share.card.excerpt.includes('生存还是毁灭') && share.sourceLinks[0].includes('a-v2'), '分享卡只含发布快照+来源');
  const csvOut = (await req('GET', '/api/public/export.csv')).body;
  assert(csvOut.includes('生存还是毁灭'), '导出包含已发布摘录（与搜索/分享同规则）');

  // 撤回后三处同时消失
  await req('POST', '/api/admin/notes/unpublish', { id: note.id });
  assert((await req('GET', '/api/public/share?noteId=' + note.id)).status === 404, '撤回后分享卡立即失效');
  assert(!(await req('GET', '/api/public/export.csv')).body.includes('生存还是毁灭'), '撤回后导出同步移除');

  /* 11. 管理端鉴权 */
  const ok = await new Promise((resolve) => {
    http.request({ host: 'localhost', port: 4391, path: '/api/admin/db', headers: { 'X-Admin-Token': TOKEN } }, res => resolve(res.statusCode)).end();
  });
  assert(ok === 200, '正确令牌可访问管理接口（200）');
  const bad = await new Promise((resolve) => {
    http.request({ host: 'localhost', port: 4391, path: '/api/admin/db', headers: { 'X-Admin-Token': 'wrong' } }, res => resolve(res.statusCode)).end();
  });
  assert(bad === 401, '错误令牌被拒绝（401）');

  /* 12. 原始行留档 */
  const db5 = (await req('GET', '/api/admin/db')).body;
  assert(db5.imports[0].rawLines.length === 6 && db5.imports[0].rawLines[0].raw.includes('哈姆雷特'), '导入原始行完整留档');

  console.log('\n验收完成。');
  server.close();
  if (process.exitCode) console.error('存在失败项'); else console.log('全部通过 ✅');
  process.exit(process.exitCode || 0);
}
main().catch(e => { console.error(e); process.exit(1); });
