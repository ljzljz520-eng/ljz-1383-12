/**
 * server.js — 零依赖 HTTP 服务
 *   公开：/archive/ 访客页；/api/public/* 查询/统计/分享卡/导出
 *   管理：/archive/admin.html；/api/admin/*（X-Admin-Token 校验，默认 dev-token）
 */
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const { load, mutate, uid } = require('./lib/store');
const D = require('./lib/domain');
const Q = require('./lib/query');
const I = require('./lib/imports');
const C = require('./lib/conflicts');

const PORT = process.env.PORT || 4173;
const ADMIN_TOKEN = process.env.ADMIN_TOKEN || 'dev-token';
const PUB = path.join(__dirname, 'public');

function send(res, code, obj, headers = {}) {
  const body = typeof obj === 'string' ? obj : JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', ...headers });
  res.end(body);
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let buf = '';
    req.on('data', c => { buf += c; if (buf.length > 8e6) reject(Object.assign(new Error('body too large'), { status: 413 })); });
    req.on('end', () => { try { resolve(buf ? JSON.parse(buf) : {}); } catch { reject(Object.assign(new Error('JSON 解析失败'), { status: 400 })); } });
  });
}
function adminOnly(req, res) {
  if (req.headers['x-admin-token'] !== ADMIN_TOKEN) { send(res, 401, { error: '管理端令牌无效' }); return false; }
  return true;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const p = url.searchParams;
  try {
    /* ---------- 静态页 ---------- */
    if (req.method === 'GET' && (url.pathname === '/archive' || url.pathname === '/archive/')) {
      return fs.createReadStream(path.join(PUB, 'index.html')).pipe(res);
    }
    if (req.method === 'GET' && url.pathname.startsWith('/archive/')) {
      const name = path.basename(url.pathname);
      if (/^[\w.-]+\.(html|js|css|csv)$/.test(name)) {
        const file = path.join(PUB, name);
        if (fs.existsSync(file)) {
          const ct = name.endsWith('.js') ? 'text/javascript; charset=utf-8'
            : name.endsWith('.css') ? 'text/css; charset=utf-8'
            : name.endsWith('.csv') ? 'text/csv; charset=utf-8' : 'text/html; charset=utf-8';
          res.writeHead(200, { 'Content-Type': ct });
          return fs.createReadStream(file).pipe(res);
        }
      }
      return send(res, 404, { error: '页面不存在' });
    }

    /* ---------- 公开 API ---------- */
    if (url.pathname === '/api/public/themes' && req.method === 'GET') {
      return send(res, 200, load().themes.map(t => ({ id: t.id, name: t.name })));
    }
    if (url.pathname === '/api/public/search' && req.method === 'GET') {
      return send(res, 200, Q.search({
        year: p.get('year'), themeId: p.get('themeId'), type: p.get('type'),
        q: p.get('q'), scope: p.get('scope') || 'work',
        limit: p.get('limit'), cursor: p.get('cursor')
      }));
    }
    if (url.pathname === '/api/public/stats' && req.method === 'GET') {
      return send(res, 200, { year: p.get('year') || null, byYear: Q.yearlyStats(p.get('year')) });
    }
    if (url.pathname === '/api/public/share' && req.method === 'GET') {
      const card = Q.shareCard(p.get('noteId'));
      return card ? send(res, 200, card) : send(res, 404, { error: '不可分享：笔记未发布' });
    }
    if (url.pathname === '/api/public/export.csv' && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="archive-export.csv"' });
      return res.end(Q.exportCsv());
    }

    /* ---------- 管理 API ---------- */
    if (url.pathname.startsWith('/api/admin/')) {
      if (!adminOnly(req, res)) return;
      const body = req.method === 'GET' ? {} : await readBody(req);
      const sub = url.pathname.replace('/api/admin/', '');

      if (sub === 'db' && req.method === 'GET') return send(res, 200, load());

      if (sub === 'works' && req.method === 'POST') return send(res, 201, await D.createWork(body));
      if (sub === 'editions' && req.method === 'POST') return send(res, 201, await D.addEdition(body));
      if (sub === 'events' && req.method === 'POST') return send(res, 201, await D.addEvent(body));

      if (sub === 'notes' && req.method === 'POST') return send(res, 201, await D.createNote(body));
      if (sub === 'notes/draft' && req.method === 'POST') return send(res, 200, await D.saveDraft(body.id, body.patch || {}));
      if (sub === 'notes/submit' && req.method === 'POST') return send(res, 200, await D.submitForReview(body.id));
      if (sub === 'notes/publish' && req.method === 'POST')
        return send(res, 200, await D.publishNote(body.id, { excerpts: body.excerpts, sourceLinks: body.sourceLinks }));
      if (sub === 'notes/unpublish' && req.method === 'POST') return send(res, 200, await D.unpublishNote(body.id));

      if (sub === 'themes' && req.method === 'POST') {
        const t = await mutate(async db => {
          const t = { id: uid('t'), name: body.name, aliases: [], createdAt: new Date().toISOString() };
          db.themes.push(t); return t;
        });
        return send(res, 201, t);
      }
      if (sub === 'themes/rename' && req.method === 'POST') return send(res, 200, await I.renameTheme(body.id, body.name));

      if (sub === 'sources/change' && req.method === 'POST') return send(res, 200, await I.changeSource(body.id, { url: body.url, label: body.label }));

      if (sub === 'merge' && req.method === 'POST')
        return send(res, 200, await D.mergeWorks({ correctId: body.correctId, wrongId: body.wrongId, evidence: body.evidence, reason: body.reason }));
      if (sub === 'split' && req.method === 'POST') return send(res, 200, await D.splitWorks(body.mergeId, { reason: body.reason }));

      if (sub === 'conflicts' && req.method === 'GET') return send(res, 200, C.listConflicts({ status: p.get('status') || 'open' }));
      if (sub === 'conflicts/resolve' && req.method === 'POST')
        return send(res, 200, await C.resolveConflict(body.conflictId,
          { action: body.action, evidence: body.evidence, reason: body.reason, targetId: body.targetId }));

      if (sub === 'provenance' && req.method === 'GET') return send(res, 200, C.provenance(p.get('workId')));

      if (sub === 'imports' && req.method === 'POST') {
        const r = await I.importCsv({
          csvText: body.csvText, clientId: body.clientId, batchRef: body.batchRef,
          sourceUrl: body.sourceUrl, filename: body.filename || 'import.csv'
        });
        return send(res, 201, r);
      }
      if (sub === 'imports' && req.method === 'GET') return send(res, 200, load().imports);

      return send(res, 404, { error: '未知管理接口' });
    }

    send(res, 404, { error: 'not found' });
  } catch (e) {
    send(res, e.status || 500, { error: e.message || 'server error' });
  }
});

if (require.main === module) {
  server.listen(PORT, () => console.log(`archive server: http://localhost:${PORT}/archive/  (admin token: ${ADMIN_TOKEN})`));
}
module.exports = server;
