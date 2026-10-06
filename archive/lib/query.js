/**
 * query.js — 访客侧查询、统计与统一可见规则
 * 搜索摘要 / 分享卡 / 导出 全部走 notePublicView，规则只有一处。
 */
'use strict';
const { load } = require('./store');
const { notePublicView } = require('./domain');

function liveWorks(db) { return db.works.filter(w => !w.mergedInto); }

/** 分页：以 (listedAt, id) 为游标，新增条目即时出现在末页之后，不漂移、不重不漏 */
function paginate(rows, { limit = 20, cursor } = {}) {
  const sorted = rows.slice().sort((a, b) =>
    (a.listedAt || a.createdAt) < (b.listedAt || b.createdAt) ? 1 :
    (a.listedAt || a.createdAt) > (b.listedAt || b.createdAt) ? -1 :
    (a.id < b.id ? 1 : -1));
  let start = 0;
  if (cursor) {
    const [ts, id] = cursor.split('|');
    const cmp = r => {
      const rts = r.listedAt || r.createdAt;
      if (rts !== ts) return rts < ts ? -1 : 1;
      return r.id < id ? -1 : r.id > id ? 1 : 0;
    };
    // 严格落在游标之后（更旧）的第一条；游标自身不重复返回
    start = sorted.findIndex(r => cmp(r) < 0);
    if (start === -1) start = sorted.length;
  }
  const slice = sorted.slice(start, start + Number(limit));
  const last = slice[slice.length - 1];
  return {
    items: slice,
    nextCursor: last ? `${last.listedAt || last.createdAt}|${last.id}` : null,
    total: sorted.length
  };
}

function publicEvents(db) {
  const works = new Map(liveWorks(db).filter(w => w.visible).map(w => [w.id, w]));
  return db.events.filter(ev => works.has(ev.workId)).map(ev => ({ ev, work: works.get(ev.workId) }));
}

/**
 * 访客查询：按年份 + 主题 + 类型 + 关键词
 * scope: work(默认,按作品计数) | event(按经历事件计数)
 */
function search(params = {}) {
  const db = load();
  const themeMap = new Map(db.themes.map(t => [t.id, t]));
  let rows = publicEvents(db);

  if (params.year) rows = rows.filter(({ ev }) => String(ev.year) === String(params.year));
  if (params.themeId) {
    rows = rows.filter(({ work }) => {
      const direct = work.themeIds.includes(params.themeId);
      const alias = (themeMap.get(params.themeId)?.aliases || []).some(a => work.themeIds.includes(a));
      return direct || alias;
    });
  }
  if (params.type) rows = rows.filter(({ work }) => work.type === params.type);
  if (params.q) {
    const q = params.q.toLowerCase();
    rows = rows.filter(({ ev, work }) =>
      work.title.toLowerCase().includes(q) ||
      (ev.takeaway || '').toLowerCase().includes(q) ||
      (work.creators || []).some(c => c.toLowerCase().includes(q)) ||
      // 只命中已发布快照；私密/待公开内容绝不进搜索摘要
      publishedNotes(db, work.id, ev.id).some(n =>
        (n.title + n.body + n.excerpt).toLowerCase().includes(q)));
  }

  const items = rows.map(({ ev, work }) => {
    const themeNames = work.themeIds.map(id => themeMap.get(id)?.name).filter(Boolean);
    return {
      eventId: ev.id, workId: work.id, type: work.type, title: work.title,
      creators: work.creators, date: ev.date, year: ev.year, revisit: ev.revisit,
      rating: ev.rating, takeaway: ev.takeaway,
      edition: db.editions.find(e => e.id === ev.editionId) || null,
      themes: themeNames,
      notes: publishedNotes(db, work.id, ev.id)
    };
  });

  const page = paginate(items.map(x => ({ ...x, listedAt: db.events.find(e => e.id === x.eventId).listedAt, id: x.eventId })),
    { limit: params.limit || 20, cursor: params.cursor });

  const stats = statsFor(rows.map(r => r.ev), rows.map(r => r.work));
  return { items: page.items, nextCursor: page.nextCursor, total: page.total, stats, scope: params.scope || 'work' };
}

function publishedNotes(db, workId, eventId) {
  return db.notes
    .filter(n => n.workId === workId && (!eventId || n.eventId === eventId || !n.eventId))
    .map(notePublicView)
    .filter(Boolean);
}

/**
 * 年度统计：口径必须显式
 *  - works  : 当年读过的"不同作品"数（重读不增加）
 *  - events : 当年"经历事件"数（重读计入）
 * 同时给 revisits，避免二者差不可解释
 */
function statsFor(events, works) {
  const workSet = new Set(events.map(e => e.workId));
  return {
    works: new Set(works ? works.map(w => w.id) : [...workSet]).size,
    events: events.length,
    revisits: events.filter(e => e.revisit).length,
    caliber: {
      works: '按作品计数：同一作品多次经历只算一次',
      events: '按事件计数：重读/重看/重听各算一次'
    }
  };
}

function yearlyStats(year) {
  const db = load();
  const rows = publicEvents(db).filter(({ ev }) => !year || String(ev.year) === String(year));
  const byYear = {};
  for (const { ev, work } of rows) {
    byYear[ev.year] = byYear[ev.year] || { events: [], works: [] };
    byYear[ev.year].events.push(ev);
    if (!byYear[ev.year].works.some(w => w.id === work.id)) byYear[ev.year].works.push(work);
  }
  const out = {};
  for (const [y, r] of Object.entries(byYear)) out[y] = statsFor(r.events, r.works);
  return year ? out[year] || { works: 0, events: 0, revisits: 0 } : out;
}

/** 分享卡：只含已发布内容 + 来源链接 */
function shareCard(noteId) {
  const db = load();
  const n = db.notes.find(x => x.id === noteId);
  const view = n && notePublicView(n);
  if (!view) return null;
  const work = liveWorks(db).find(w => w.id === n.workId);
  return {
    card: { title: view.title, excerpt: view.excerpt, workTitle: work?.title || '', publishedAt: view.publishedAt },
    sourceLinks: view.sourceLinks
  };
}

/** 导出：只导出作品可见且笔记已发布的内容；返回 CSV 文本 */
function exportCsv() {
  const db = load();
  const header = ['type', 'title', 'creators', 'date', 'revisit', 'themes', 'noteTitle', 'excerpt', 'sourceLinks'];
  const esc = v => `"${String(v ?? '').replace(/"/g, '""').replace(/\n/g, ' ')}"`;
  const themeMap = new Map(db.themes.map(t => [t.id, t.name]));
  const lines = [header.join(',')];
  for (const { ev, work } of publicEvents(db)) {
    const ns = publishedNotes(db, work.id, ev.id);
    const base = [work.type, work.title, (work.creators || []).join('; '), ev.date, ev.revisit ? 'yes' : '',
      work.themeIds.map(t => themeMap.get(t)).filter(Boolean).join('; ')];
    if (!ns.length) lines.push(base.concat(['', '', '']).map(esc).join(','));
    for (const n of ns) lines.push(base.concat([n.title, n.excerpt, (n.sourceLinks || []).join('; ')]).map(esc).join(','));
  }
  return lines.join('\n');
}

module.exports = { paginate, search, yearlyStats, shareCard, exportCsv, publishedNotes, statsFor, publicEvents, liveWorks };
