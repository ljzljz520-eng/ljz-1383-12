/**
 * imports.js — 离线导入（CSV）
 *
 * - 保存批次与每一条原始行（raw），供审计
 * - 行级幂等键 rowKey = sha1(clientId + batchRef + lineNo + 规范化字段)
 *   离线断网重传：同批同键直接跳过，不产生重复作品/事件
 * - 匹配：外部标识优先；同名无标识 → 进冲突队列，绝不自动合并
 * - 导入的笔记默认私密；即使行内带摘录也不自动公开
 * - 第三方元数据只作引用线索（clue），不视为转载授权
 */
'use strict';
const crypto = require('crypto');
const { load, mutate, uid } = require('./store');
const { findCandidates, normId } = require('./domain');

/** 极简 CSV 解析（支持引号包裹与转义双引号） */
function parseCsv(text) {
  const rows = [];
  let cur = [], field = '', inQ = false;
  const push = () => { cur.push(field); field = ''; };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQ) {
      if (ch === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
      else field += ch;
    } else if (ch === '"') inQ = true;
    else if (ch === ',') push();
    else if (ch === '\n') { push(); rows.push(cur); cur = []; }
    else if (ch === '\r') { /* skip */ }
    else field += ch;
  }
  if (field !== '' || cur.length) { push(); rows.push(cur); }
  return rows.filter(r => r.some(c => c.trim() !== ''));
}

function sha1(s) { return crypto.createHash('sha1').update(s).digest('hex'); }

/**
 * @param {object} opts
 *  csvText, clientId（设备标识）, batchRef（文件/批次标识，重传时保持不变）,
 *  sourceUrl（来源链接）, filename
 */
async function importCsv({ csvText, clientId = 'anon', batchRef, sourceUrl = '', filename = 'import.csv' }) {
  const rows = parseCsv(csvText);
  if (!rows.length) throw Object.assign(new Error('CSV 为空'), { status: 400 });
  const header = rows[0].map(h => h.trim());
  const need = ['type', 'title', 'date'];
  for (const n of need) if (!header.includes(n)) throw Object.assign(new Error(`缺少列: ${n}`), { status: 400 });

  // 幂等：批次内同键行只处理一次（离线重传安全）
  const keyOf = (lineNo, rec) => sha1([clientId, batchRef || filename, lineNo,
    rec.type, rec.title, rec.date, rec.isbn13 || rec.imdb || rec.upc || '',
    rec.editionLabel || '', rec.translator || ''].join('|'));

  const result = { batchId: null, received: rows.length - 1, inserted: 0, skipped: 0, conflicts: 0, autoMatched: 0, rows: [] };

  await mutate(async db => {
    let batch = db.imports.find(b => b.clientId === clientId && b.batchRef === (batchRef || filename));
    if (!batch) {
      batch = {
        id: uid('imp'), clientId, batchRef: batchRef || filename, filename,
        sourceUrl, status: 'open',
        uploadedAt: new Date().toISOString(), completedAt: null,
        rowKeys: {}, rawLines: []
      };
      db.imports.push(batch);
    }
    result.batchId = batch.id;

    for (let i = 1; i < rows.length; i++) {
      const rec = {};
      header.forEach((h, idx) => { rec[h] = (rows[i][idx] || '').trim(); });
      const lineNo = i + 1;
      const key = keyOf(lineNo, rec);
      // 完整保存原始行（审计/回溯）
      if (!batch.rawLines.some(r => r.lineNo === lineNo)) {
        batch.rawLines.push({ lineNo, raw: rows[i].join(','), storedAt: new Date().toISOString() });
      }
      if (batch.rowKeys[key]) { result.skipped++; result.rows.push({ lineNo, status: 'duplicate' }); continue; }
      batch.rowKeys[key] = true;

      const externalIds = {};
      for (const a of ['isbn13', 'isbn10', 'imdb', 'tmdb', 'upc', 'musicbrainz', 'douban', 'goodreads', 'spotify', 'oclc', 'isrc']) {
        if (rec[a]) externalIds[a] = normId(a, rec[a]);
      }
      const { strong, strongEvidence, weak } = findCandidates(db, { type: rec.type, title: rec.title, externalIds });

      let work;
      if (strong) {
        work = strong;
        result.autoMatched++;
      } else {
        work = {
          id: uid('w'), type: rec.type, title: rec.title,
          creators: rec.creators ? rec.creators.split(';').map(s => s.trim()).filter(Boolean) : [],
          externalIds, themeIds: rec.themes ? await resolveThemes(db, rec.themes) : [],
          visible: true, mergedInto: null, mergeHistory: [],
          createdAt: new Date().toISOString()
        };
        db.works.push(work);
        if (weak.length) {
          db.conflicts.push({
            id: uid('c'), status: 'open', kind: 'same-title',
            workId: work.id, candidateIds: weak.map(w => w.id),
            evidence: { reason: '导入：标题同名但外部标识不一致或缺失', title: rec.title, type: rec.type, lineNo },
            createdAt: new Date().toISOString(), resolvedAt: null, resolution: null
          });
          result.conflicts++;
        }
      }

      // 版本（不同译本/录音版各建一条，绝不因同名被并掉）
      let editionId = null;
      if (rec.editionLabel || rec.translator || rec.publisher) {
        const ed = {
          id: uid('e'), workId: work.id,
          kind: rec.editionKind || (rec.type === 'music' ? 'recording' : 'translation'),
          label: rec.editionLabel || (rec.translator ? rec.translator + ' 译' : ''),
          publisher: rec.publisher || '', year: rec.editionYear ? Number(rec.editionYear) : null,
          externalIds: {}, createdAt: new Date().toISOString()
        };
        db.editions.push(ed);
        editionId = ed.id;
      }

      const rowRef = { id: uid('ir'), batchId: batch.id, lineNo };
      const ev = {
        id: uid('ev'), workId: work.id, editionId,
        date: rec.date, year: Number(String(rec.date).slice(0, 4)),
        revisit: /^(1|true|yes|重读|重看|重听)$/i.test(rec.revisit || ''),
        rating: rec.rating ? Number(rec.rating) : null,
        takeaway: rec.takeaway || '',
        sourceId: null, importRowId: rowRef.id,
        listedAt: new Date().toISOString(), createdAt: new Date().toISOString()
      };
      db.events.push(ev);

      // 来源链接（第三方信息仅作引用线索登记）
      if (rec.sourceUrl || sourceUrl) {
        let src = db.sources.find(s => s.url === (rec.sourceUrl || sourceUrl));
        if (!src) {
          src = { id: uid('s'), url: rec.sourceUrl || sourceUrl, label: rec.sourceLabel || batch.filename,
                  kind: 'clue', // clue=引用线索，非内容授权
                  history: [], createdAt: new Date().toISOString() };
          db.sources.push(src);
        }
        ev.sourceId = src.id;
      }

      // 笔记默认私密：摘录文本即使存在也不会被自动公开
      if (rec.note || rec.excerpt) {
        db.notes.push({
          id: uid('n'), workId: work.id, eventId: ev.id, visibility: 'private',
          current: { title: rec.noteTitle || rec.title, body: rec.note || '',
                     excerpts: rec.excerpt ? [rec.excerpt] : [], editedAt: new Date().toISOString() },
          publishedVersion: null, createdAt: new Date().toISOString()
        });
      }

      result.inserted++;
      result.rows.push({ lineNo, status: 'inserted', workId: work.id, eventId: ev.id,
                         matched: !!strong, evidence: strongEvidence });
    }
    batch.status = 'completed';
    batch.completedAt = new Date().toISOString();
  });
  return result;
}

async function resolveThemes(db, list) {
  const names = list.split(';').map(s => s.trim()).filter(Boolean);
  const ids = [];
  for (const name of names) {
    let t = db.themes.find(x => x.name === name);
    if (!t) { t = { id: uid('t'), name, aliases: [], createdAt: new Date().toISOString() }; db.themes.push(t); }
    ids.push(t.id);
  }
  return ids;
}

/** 来源链接变更：保留历史，不动导入原始行 */
function changeSource(sourceId, { url, label }) {
  return mutate(async db => {
    const s = db.sources.find(x => x.id === sourceId);
    if (!s) throw Object.assign(new Error('来源不存在'), { status: 404 });
    s.history.push({ url: s.url, label: s.label, changedAt: new Date().toISOString() });
    if (url) s.url = url;
    if (label != null) s.label = label;
    return s;
  });
}

/** 主题重命名：id 不变，旧名进 aliases，访客按新旧名都能查到 */
function renameTheme(themeId, newName) {
  return mutate(async db => {
    const t = db.themes.find(x => x.id === themeId);
    if (!t) throw Object.assign(new Error('主题不存在'), { status: 404 });
    if (t.name !== newName) {
      if (!t.aliases.includes(t.name)) t.aliases.push(t.name);
      t.name = newName;
    }
    return t;
  });
}

module.exports = { parseCsv, importCsv, changeSource, renameTheme };
