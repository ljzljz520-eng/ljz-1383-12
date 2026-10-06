/**
 * domain.js — 档案核心领域规则
 *
 * 1. 匹配：外部标识优先，绝不只凭标题自动合并
 * 2. 合并 / 错误合并后拆分（有快照、可回溯）
 * 3. 笔记三态：private / pending / published，发布快照不可变
 * 4. 统计：明确区分 "作品数" 与 "事件数"
 * 5. 主题重命名 id 不变；来源链接变更保留历史
 */
'use strict';
const { load, mutate, uid } = require('./store');

/* ---------------- 外部标识 ---------------- */

const ID_AUTHORITIES = {
  book: ['isbn13', 'isbn10', 'oclc', 'goodreads', 'douban'],
  movie: ['imdb', 'tmdb', 'douban'],
  music: ['upc', 'isrc', 'musicbrainz', 'spotify', 'douban']
};

/** 规范化外部标识：isbn 去连字符并统一为 13 位字符串（此处不做校验位换算，只比较规范值） */
function normId(authority, value) {
  if (value == null) return null;
  let v = String(value).trim().toLowerCase();
  if (!v) return null;
  if (authority === 'isbn13' || authority === 'isbn10') v = v.replace(/-/g, '');
  if (authority === 'imdb' && !v.startsWith('tt')) v = 'tt' + v;
  return v;
}

function sameIds(a = {}, b = {}) {
  for (const [k, v] of Object.entries(a)) {
    const nv = normId(k, v);
    if (nv && b[k] != null && normId(k, b[k]) === nv) return { authority: k, value: nv };
  }
  return null;
}

function titleKey(s) {
  return String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

/**
 * 在已有作品中找候选。
 * 返回 { strong: work|null(带匹配证据), weak: [works] }
 * strong = 同类型 + 外部标识命中；weak = 同类型 + 同名（仅供人工参考，绝不自动合并）
 */
function findCandidates(db, { type, title, externalIds = {} }) {
  const normInput = {};
  for (const [k, v] of Object.entries(externalIds)) {
    const nv = normId(k, v);
    if (nv) normInput[k] = nv;
  }
  let strong = null;
  let strongEvidence = null;
  const weak = [];
  for (const w of db.works) {
    if (w.type !== type) continue;
    if (Object.keys(normInput).length) {
      const hit = sameIds(normInput, w.externalIds || {});
      if (hit) { strong = w; strongEvidence = hit; continue; }
    }
    if (titleKey(w.title) && titleKey(w.title) === titleKey(title)) weak.push(w);
  }
  return { strong, strongEvidence, weak };
}

/* ---------------- 作品 / 版本 / 事件 ---------------- */

function createWork(data) {
  return mutate(async db => {
    const { strong, strongEvidence, weak } = findCandidates(db, data);
    // 外部标识命中 → 直接归属已有作品，绝不重复新建
    if (strong) {
      if (strong.mergedInto) {
        db.conflicts.push({
          id: uid('c'), status: 'open', kind: 'merged-target',
          workId: strong.mergedInto, candidateIds: [strong.id],
          evidence: { reason: '标识命中的作品处于已合并状态', matched: strongEvidence },
          createdAt: new Date().toISOString(), resolvedAt: null, resolution: null
        });
      }
      return { work: strong, autoMatched: true, matchedWork: strong.id, evidence: strongEvidence, sameTitleCandidates: [] };
    }
    const work = {
      id: uid('w'),
      type: data.type,
      title: data.title.trim(),
      creators: data.creators || [],          // 作者/导演/艺术家
      externalIds: data.externalIds || {},
      themeIds: data.themeIds || [],
      visible: data.visible !== false,        // 作品默认可见；笔记默认不可见
      createdAt: new Date().toISOString(),
      mergedInto: null,
      mergeHistory: []
    };
    db.works.push(work);
    // 同名但无标识一致 → 进冲突队列，等待人工裁决，绝不自动合并
    if (weak.length) {
      db.conflicts.push({
        id: uid('c'), status: 'open', kind: 'same-title',
        workId: work.id, candidateIds: weak.map(w => w.id),
        evidence: { reason: '标题同名，但缺少一致的外部标识', title: work.title, type: work.type },
        createdAt: new Date().toISOString(), resolvedAt: null, resolution: null
      });
    }
    return { work, autoMatched: false, matchedWork: null, evidence: null, sameTitleCandidates: weak.map(w => w.id) };
  });
}

function addEdition(data) {
  return mutate(async db => {
    const work = db.works.find(w => w.id === data.workId && !w.mergedInto);
    if (!work) throw Object.assign(new Error('作品不存在或已被合并'), { status: 400 });
    const edition = {
      id: uid('e'), workId: work.id,
      kind: data.kind || 'translation',      // translation(译本)/print/recording(录音)/cut(剪辑版)…
      label: data.label || '',               // 例如：朱生豪译本 / 1994 原声录音 / 4K 修复版
      publisher: data.publisher || '',
      year: data.year || null,
      externalIds: data.externalIds || {},
      createdAt: new Date().toISOString()
    };
    db.editions.push(edition);
    return edition;
  });
}

/** 新增一次经历事件。重读/重看/重听 = 新事件，绝不复用旧记录 */
function addEvent(data) {
  return mutate(async db => {
    const work = db.works.find(w => w.id === data.workId && !w.mergedInto);
    if (!work) throw Object.assign(new Error('作品不存在或已被合并'), { status: 400 });
    if (data.editionId) {
      const ed = db.editions.find(e => e.id === data.editionId && e.workId === work.id);
      if (!ed) throw Object.assign(new Error('版本不属于该作品'), { status: 400 });
    }
    const ev = {
      id: uid('ev'), workId: work.id, editionId: data.editionId || null,
      date: data.date,                        // 经历日期 YYYY-MM-DD
      year: Number(String(data.date).slice(0, 4)),
      revisit: !!data.revisit,                // 是否重读
      rating: data.rating ?? null,
      takeaway: data.takeaway || '',          // 个人收获
      sourceId: data.sourceId || null,
      importRowId: data.importRowId || null,
      listedAt: new Date().toISOString(),     // 分页游标
      createdAt: new Date().toISOString()
    };
    db.events.push(ev);
    return ev;
  });
}

/* ---------------- 合并 / 拆分 ---------------- */

/**
 * 人工合并：把 wrongId 并入 correctId。
 * 外部标识优先的证据必须由调用方给出；标题同名本身不构成自动合并理由。
 */
function mergeWorks({ correctId, wrongId, evidence, reason }) {
  return mutate(async db => {
    const correct = db.works.find(w => w.id === correctId);
    const wrong = db.works.find(w => w.id === wrongId);
    if (!correct || !wrong) throw Object.assign(new Error('作品不存在'), { status: 404 });
    if (correctId === wrongId) throw Object.assign(new Error('不能与自身合并'), { status: 400 });
    if (correct.mergedInto || wrong.mergedInto) throw Object.assign(new Error('请先拆分嵌套合并'), { status: 400 });

    const snapshot = JSON.parse(JSON.stringify(wrong));
    // 记录迁移集合（拆分时按此归还；合并之后新增的子实体不会被误带走）
    const migratedIds = {
      editions: db.editions.filter(e => e.workId === wrongId).map(e => e.id),
      events: db.events.filter(ev => ev.workId === wrongId).map(ev => ev.id),
      notes: db.notes.filter(n => n.workId === wrongId).map(n => n.id)
    };
    // 迁移子实体
    for (const e of db.editions) if (e.workId === wrongId) e.workId = correctId;
    for (const ev of db.events) if (ev.workId === wrongId) ev.workId = correctId;
    for (const n of db.notes) if (n.workId === wrongId) n.workId = correctId;
    // 合并外部标识（不覆盖已有值；冲突值进审阅）
    for (const [k, v] of Object.entries(wrong.externalIds || {})) {
      if (!correct.externalIds[k]) correct.externalIds[k] = v;
      else if (normId(k, correct.externalIds[k]) !== normId(k, v)) {
        db.conflicts.push({
          id: uid('c'), status: 'open', kind: 'id-clash',
          workId: correctId, candidateIds: [wrongId],
          evidence: { authority: k, kept: correct.externalIds[k], dropped: v },
          createdAt: new Date().toISOString(), resolvedAt: null, resolution: null
        });
      }
    }
    for (const t of wrong.themeIds) if (!correct.themeIds.includes(t)) correct.themeIds.push(t);
    wrong.mergedInto = correctId;
    const rec = {
      id: uid('m'), action: 'merge', at: new Date().toISOString(),
      correctId, wrongId, evidence: evidence || null, reason: reason || '',
      snapshot, migratedIds
    };
    db.mergeLog.push(rec);
    correct.mergeHistory.push(rec.id);
    return rec;
  });
}

/** 错误合并后拆分：用快照恢复被吸收的作品，事件/版本/笔记按快照归还 */
function splitWorks(mergeId, { reason }) {
  return mutate(async db => {
    const rec = db.mergeLog.find(m => m.id === mergeId && m.action === 'merge');
    if (!rec) throw Object.assign(new Error('合并记录不存在'), { status: 404 });
    const wrong = db.works.find(w => w.id === rec.wrongId);
    if (!wrong || wrong.mergedInto !== rec.correctId) throw Object.assign(new Error('当前不可拆分（可能已再次合并）'), { status: 400 });

    // 按合并时记录的迁移集合归还（合并后才新增的子实体保留在 correct 上）
    const ids = rec.migratedIds || { editions: [], events: [], notes: [] };
    for (const e of db.editions) if (ids.editions.includes(e.id)) e.workId = wrong.id;
    for (const ev of db.events) if (ids.events.includes(ev.id)) ev.workId = wrong.id;
    for (const n of db.notes) if (ids.notes.includes(n.id)) n.workId = wrong.id;
    delete wrong.mergedInto;
    wrong.externalIds = rec.snapshot.externalIds || {};
    wrong.themeIds = rec.snapshot.themeIds || [];
    wrong.title = rec.snapshot.title;
    const split = {
      id: uid('m'), action: 'split', at: new Date().toISOString(),
      undoOf: mergeId, correctId: rec.correctId, wrongId: rec.wrongId,
      reason: reason || '', restoredFromSnapshot: true
    };
    db.mergeLog.push(split);
    return split;
  });
}

/* ---------------- 笔记三态 ---------------- */

/**
 * 可见规则（唯一出口，搜索摘要/分享卡/导出共用）：
 *   private  → 仅管理端可见
 *   pending  → 仅管理端可见（待公开）
 *   published→ 公开，且对外只展示发布快照 publishedVersion
 */
function notePublicView(note) {
  if (note.visibility !== 'published' || !note.publishedVersion) return null;
  return {
    noteId: note.id, workId: note.workId, eventId: note.eventId || null,
    title: note.publishedVersion.title,
    excerpt: note.publishedVersion.excerpt,   // 仅站主明确勾选的摘录
    body: note.publishedVersion.body,
    publishedAt: note.publishedVersion.publishedAt,
    sourceLinks: note.publishedVersion.sourceLinks || []
  };
}

function createNote(data) {
  return mutate(async db => {
    const work = db.works.find(w => w.id === data.workId && !w.mergedInto);
    if (!work) throw Object.assign(new Error('作品不存在'), { status: 400 });
    const note = {
      id: uid('n'), workId: work.id, eventId: data.eventId || null,
      visibility: 'private',   // 新建一律私密，绝不自动公开
      current: { title: data.title || '', body: data.body || '', excerpts: [], editedAt: new Date().toISOString() },
      publishedVersion: null,
      createdAt: new Date().toISOString()
    };
    db.notes.push(note);
    return note;
  });
}

/** 保存草稿；不影响已发布版本 */
function saveDraft(id, patch) {
  return mutate(async db => {
    const n = db.notes.find(x => x.id === id);
    if (!n) throw Object.assign(new Error('笔记不存在'), { status: 404 });
    n.current = { ...n.current, ...patch, editedAt: new Date().toISOString() };
    return n;
  });
}

/** 私密 → 待公开（站主审阅中） */
function submitForReview(id) {
  return mutate(async db => {
    const n = db.notes.find(x => x.id === id);
    if (!n) throw Object.assign(new Error('笔记不存在'), { status: 404 });
    if (n.visibility === 'published') throw Object.assign(new Error('请先撤回发布再修改状态'), { status: 400 });
    n.visibility = 'pending';
    return n;
  });
}

/**
 * 发布：对当前内容生成不可变快照。
 * excerpts 必须是站主显式勾选的摘录；第三方内容即使抓取成功也不自动进入发布版。
 */
function publishNote(id, { excerpts = [], sourceLinks = [] } = {}) {
  return mutate(async db => {
    const n = db.notes.find(x => x.id === id);
    if (!n) throw Object.assign(new Error('笔记不存在'), { status: 404 });
    n.publishedVersion = {
      version: (n.publishedVersion?.version || 0) + 1,
      title: n.current.title,
      body: n.current.body,
      excerpt: (excerpts || []).filter(Boolean).join('\n---\n'),
      excerpts: excerpts || [],
      sourceLinks: sourceLinks || [],
      publishedAt: new Date().toISOString()
    };
    n.visibility = 'published';
    return n;
  });
}

function unpublishNote(id) {
  return mutate(async db => {
    const n = db.notes.find(x => x.id === id);
    if (!n) throw Object.assign(new Error('笔记不存在'), { status: 404 });
    n.visibility = 'private';   // 保留 publishedVersion 快照，但对外不可见
    return n;
  });
}

module.exports = {
  ID_AUTHORITIES, normId, sameIds, titleKey, findCandidates,
  createWork, addEdition, addEvent,
  mergeWorks, splitWorks,
  createNote, saveDraft, submitForReview, publishNote, unpublishNote, notePublicView
};
