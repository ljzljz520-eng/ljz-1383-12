'use strict';
const $ = s => document.querySelector(s);
const token = () => $('#token').value.trim();
function toast(msg, err) { const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? 'err' : ''); t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 2800); }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

async function api(path, method = 'GET', body) {
  const r = await fetch('/api/admin/' + path, {
    method, headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token() },
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r));
  return data;
}
let DB;
async function refresh() {
  DB = await api('db');
  fillSelects();
  renderNotes();
  renderConflicts();
  renderMergePane();
  renderTaxonomy();
  renderImports();
}
function liveWorks() { return DB.works.filter(w => !w.mergedInto); }
function nameOf(id) { const w = DB.works.find(x => x.id === id); return w ? `[${w.type}] ${w.title}` : id; }

function fillSelects() {
  const opts = liveWorks().map(w => `<option value="${w.id}">${esc(nameOf(w.id))}</option>`).join('');
  for (const id of ['e-work', 'ev-work', 'n-work', 'm-wrong', 'p-work']) $('#' + id).innerHTML = '<option value="">— 选择作品 —</option>' + opts;
  $('#m-correct').innerHTML = '<option value="">— 保留的作品 —</option>' + opts;
  $('#ev-work').onchange = () => {
    const eds = DB.editions.filter(e => e.workId === $('#ev-work').value);
    $('#ev-edition').innerHTML = '<option value="">版本（可略）</option>' + eds.map(e => `<option value="${e.id}">${esc(e.label || e.kind)}</option>`).join('');
  };
}

/* ---------- 录入 ---------- */
async function createWork() {
  try {
    let ids = {};
    if ($('#w-ids').value.trim()) ids = JSON.parse($('#w-ids').value);
    const r = await api('works', 'POST', {
      type: $('#w-type').value, title: $('#w-title').value, creators: $('#w-creators').value.split(';').map(s => s.trim()).filter(Boolean), externalIds: ids
    });
    toast(r.autoMatched ? `标识自动匹配到已有作品（${r.evidence.authority}）；未新建` : '作品已创建' + (r.sameTitleCandidates.length ? '；同名候选已进冲突队列' : ''));
    await refresh();
  } catch (e) { toast(e.message, true); }
}
async function addEdition() {
  try { await api('editions', 'POST', { workId: $('#e-work').value, label: $('#e-label').value, publisher: $('#e-publisher').value, year: Number($('#e-year').value) || null }); toast('版本已加'); await refresh(); }
  catch (e) { toast(e.message, true); }
}
async function addEvent() {
  try {
    await api('events', 'POST', { workId: $('#ev-work').value, editionId: $('#ev-edition').value || null, date: $('#ev-date').value, revisit: $('#ev-revisit').checked, rating: Number($('#ev-rating').value) || null, takeaway: $('#ev-takeaway').value });
    toast('事件已记录（重读是一次新事件）'); $('#ev-takeaway').value = ''; await refresh();
  } catch (e) { toast(e.message, true); }
}
async function createNote() {
  try { await api('notes', 'POST', { workId: $('#n-work').value, title: $('#n-title').value, body: $('#n-body').value }); toast('笔记已创建（私密）'); $('#n-body').value = ''; $('#n-title').value = ''; await refresh(); }
  catch (e) { toast(e.message, true); }
}

/* ---------- 笔记三态 ---------- */
function renderNotes() {
  $('#notes-body').innerHTML = DB.notes.map(n => {
    const candidates = n.current.excerpts || [];
    return `<tr>
      <td><strong>${esc(n.current.title)}</strong><div class="meta">${esc(nameOf(n.workId))}</div></td>
      <td><span class="badge ${n.visibility}">${({ private: '私密', pending: '待公开', published: '已发布' })[n.visibility]}</span>
        ${n.publishedVersion ? `<div class="meta">v${n.publishedVersion.version} · ${n.publishedVersion.publishedAt.slice(0, 10)}</div>` : ''}</td>
      <td class="row">
        ${n.visibility === 'private' ? `<button class="mini" onclick="editNote('${n.id}')">编辑草稿</button><button class="mini ghost" onclick="act('notes/submit',{id:'${n.id}'})">提交待公开</button>` : ''}
        ${n.visibility === 'pending' ? `<button class="mini" onclick="openPublish('${n.id}')">审阅并发布</button><button class="mini ghost" onclick="act('notes/draft',{id:'${n.id}',patch:{}})">留在待公开</button>` : ''}
        ${n.visibility === 'published' ? `<button class="mini danger" onclick="act('notes/unpublish',{id:'${n.id}'})">撤回为私密</button>` : ''}
      </td>
      <td class="meta">${candidates.length ? candidates.map(esc).join('<br>') : '（草稿中无摘录）'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="4" class="muted">暂无笔记</td></tr>';
}
async function editNote(id) {
  const n = DB.notes.find(x => x.id === id);
  openDialog('编辑草稿', `
    <label>标题</label><input id="d-title" value="${esc(n.current.title)}" style="width:100%">
    <label>正文</label><textarea id="d-body">${esc(n.current.body)}</textarea>
    <label>候选摘录（每行一条；不会自动公开）</label><textarea id="d-excerpts">${esc((n.current.excerpts || []).join('\n'))}</textarea>
    <div class="row" style="margin-top:.8rem"><button onclick="saveDraft('${id}')">保存草稿</button><button class="ghost" onclick="closeDialog()">取消</button></div>`);
}
async function saveDraft(id) {
  await api('notes/draft', 'POST', { id, patch: { title: $('#d-title').value, body: $('#d-body').value, excerpts: $('#d-excerpts').value.split('\n').map(s => s.trim()).filter(Boolean) } });
  closeDialog(); toast('草稿已保存'); await refresh();
}
function openPublish(id) {
  const n = DB.notes.find(x => x.id === id);
  const items = (n.current.excerpts || []);
  openDialog('审阅并发布', `
    <p>将对当前草稿生成不可变<strong>发布版本</strong>。请逐条勾选可以公开的摘录：</p>
    <div class="meta" style="margin:.5rem 0">${esc(n.current.title)}</div>
    ${items.length ? items.map((x, i) => `<label style="color:var(--text-white)"><input type="checkbox" class="d-ex" value="${i}"> 「${esc(x)}」</label>`).join('') : '<p class="muted">无可勾选摘录（将只发布正文）</p>'}
    <label>来源链接（每行一个）</label><textarea id="d-links">${esc((n.publishedVersion?.sourceLinks || []).join('\n'))}</textarea>
    <div class="row" style="margin-top:.8rem"><button onclick="doPublish('${id}')">确认发布</button><button class="ghost" onclick="closeDialog()">取消</button></div>`);
}
async function doPublish(id) {
  const n = DB.notes.find(x => x.id === id);
  const picked = [...document.querySelectorAll('.d-ex:checked')].map(cb => n.current.excerpts[Number(cb.value)]);
  await api('notes/publish', 'POST', { id, excerpts: picked, sourceLinks: $('#d-links').value.split('\n').map(s => s.trim()).filter(Boolean) });
  closeDialog(); toast('已生成发布版本'); await refresh();
}
async function act(path, body) { try { await api(path, 'POST', body); toast('已更新'); await refresh(); } catch (e) { toast(e.message, true); } }

/* ---------- 冲突 ---------- */
async function renderConflicts() {
  const cs = await api('conflicts?status=open');
  $('#conflicts').innerHTML = cs.map(c => `
    <div class="card" style="margin-bottom:.7rem">
      <span class="badge ${c.status}">${c.kind}</span>
      <strong> ${esc(nameOf(c.workId))}</strong>
      <div class="meta">${esc(c.evidence?.reason || '')}
        ${c.evidence?.authority ? `<div>标识冲突 authority=${esc(c.evidence.authority)}: 保留 ${esc(c.evidence.kept)} vs ${esc(c.evidence.dropped)}</div>` : ''}</div>
      <div class="meta">候选：${c.candidates.map(w => `<label style="color:var(--text-white);display:inline"><input type="radio" name="t-${c.id}" value="${w.id}"> ${esc(w.title)} <span class="evidence">${esc(JSON.stringify(w.externalIds))}</span></label>`).join(' &nbsp; ') || '无'}</div>
      <div class="row">
        <button class="mini" onclick="resolveC('${c.id}','merge')">确认同一作品 → 合并到所选</button>
        <button class="mini ghost" onclick="resolveC('${c.id}','keep')">是不同作品，保留各自独立</button>
      </div>
      <input id="reason-${c.id}" placeholder="裁决理由/人工证据说明" style="width:100%;margin-top:.4rem">
    </div>`).join('') || '<p class="muted">没有待审冲突。</p>';
}
async function resolveC(id, action) {
  const target = document.querySelector(`input[name="t-${id}"]:checked`)?.value;
  const reason = $('#reason-' + id).value;
  if (action === 'merge' && !target) return toast('请先选择合并目标', true);
  try { await api('conflicts/resolve', 'POST', { conflictId: id, action, targetId: target, reason, evidence: action === 'merge' ? { manual: true, via: 'conflict-review', note: reason } : null }); toast('裁决已记录'); await refresh(); }
  catch (e) { toast(e.message, true); }
}

/* ---------- 合并 / 拆分 ---------- */
function renderMergePane() {
  $('#p-work').innerHTML = '<option value="">— 选择作品 —</option>' + DB.works.map(w => `<option value="${w.id}">${esc(nameOf(w.id))}${w.mergedInto ? ' (已并入)' : ''}</option>`).join('');
}
async function merge() {
  try {
    let evidence = {};
    if ($('#m-evidence').value.trim()) evidence = JSON.parse($('#m-evidence').value);
    else evidence = { manual: true };
    await api('merge', 'POST', { correctId: $('#m-correct').value, wrongId: $('#m-wrong').value, evidence, reason: $('#m-reason').value });
    toast('已合并并记录审计'); await refresh();
  } catch (e) { toast(e.message, true); }
}
async function provenance() {
  const id = $('#p-work').value;
  if (!id) return;
  const log = await api('provenance?workId=' + id);
  $('#prov').innerHTML = log.length ? `<table><tr><th>动作</th><th>对象</th><th>证据</th><th>时间</th><th></th></tr>` +
    log.map(m => `<tr><td>${m.action === 'merge' ? '合并' : '拆分'}</td><td class="meta">${esc(nameOf(m.withWork))}</td>
      <td class="evidence">${esc(JSON.stringify(m.evidence || {}))}</td><td class="meta">${m.at.slice(0,16).replace('T',' ')}</td>
      <td>${m.action === 'merge' ? `<button class="mini danger" onclick="splitIt('${m.id}')">拆分撤销</button>` : `<span class="meta">撤销自 ${m.undoOf.slice(0,10)}</span>`}</td></tr>`).join('') + '</table>'
    : '<p class="muted">该作品无合并历史。</p>';
}
async function splitIt(mergeId) {
  if (!confirm('按合并时快照拆分归还？合并后新增的内容保留在当前作品上。')) return;
  try { await api('split', 'POST', { mergeId, reason: '人工发现错误合并' }); toast('已拆分'); await refresh(); provenance(); }
  catch (e) { toast(e.message, true); }
}

/* ---------- 导入 ---------- */
async function doImport() {
  const f = $('#imp-file').files[0];
  if (!f) return toast('请选择 CSV 文件', true);
  const csvText = await f.text();
  try {
    const r = await api('imports', 'POST', { csvText, clientId: $('#imp-client').value, batchRef: $('#imp-batch').value, sourceUrl: $('#imp-source').value, filename: f.name });
    $('#imp-result').textContent = `批次 ${r.batchId}\n收到 ${r.received} 行 / 新增 ${r.inserted} / 跳过重复 ${r.skipped} / 产生冲突 ${r.conflicts} / 标识自动匹配 ${r.autoMatched}`;
    toast('导入完成'); await refresh();
  } catch (e) { toast(e.message, true); }
}
function renderImports() {
  $('#imp-batches').innerHTML = DB.imports.map(b => `<div>${esc(b.filename)} · ${esc(b.clientId)}/${esc(b.batchRef)} · ${b.status} · 原始行 ${b.rawLines.length} · ${b.uploadedAt.slice(0,16).replace('T',' ')}</div>`).join('') || '无批次';
}

/* ---------- 主题 / 来源 ---------- */
function renderTaxonomy() {
  $('#themes').innerHTML = DB.themes.map(t => `
    <div class="row" style="margin-bottom:.4rem">
      <input value="${esc(t.name)}" id="tn-${t.id}" size="16">
      <span class="meta">曾用名: ${(t.aliases.length ? t.aliases : ['—']).map(esc).join('、')}</span>
      <button class="mini" onclick="renameT('${t.id}')">重命名(id不变)</button>
    </div>`).join('') || '<p class="muted">暂无主题（导入时会自动建）。</p>';
  $('#sources').innerHTML = DB.sources.map(s => `
    <div style="margin-bottom:.6rem">
      <div class="pill-src">引用线索（非转载授权） · ${esc(s.label || '')}</div>
      <input value="${esc(s.url)}" id="su-${s.id}" size="50">
      <button class="mini" onclick="changeSrc('${s.id}')">变更链接</button>
      <div class="meta">${(s.history.length ? s.history.map(h => `旧: ${esc(h.url)} @ ${h.changedAt.slice(0,10)}`).join('；') : '无变更历史')}</div>
    </div>`).join('') || '<p class="muted">暂无来源。</p>';
}
async function renameT(id) { try { await api('themes/rename', 'POST', { id, name: $('#tn-' + id).value }); toast('已重命名'); await refresh(); } catch (e) { toast(e.message, true); } }
async function changeSrc(id) { try { await api('sources/change', 'POST', { id, url: $('#su-' + id).value }); toast('链接已变更，旧值入历史'); await refresh(); } catch (e) { toast(e.message, true); } }

/* ---------- 对话框 / Tab ---------- */
function openDialog(title, html) { $('#dlg').innerHTML = `<dialog open><h3>${esc(title)}</h3>${html}</dialog>`; }
function closeDialog() { $('#dlg').innerHTML = ''; }
window.closeDialog = closeDialog;
$('#tabs').addEventListener('click', e => {
  if (!e.target.dataset.tab) return;
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b === e.target));
  document.querySelectorAll('section[id^="tab-"]').forEach(s => s.hidden = s.id !== 'tab-' + e.target.dataset.tab);
});
Object.assign(window, { createWork, addEdition, addEvent, createNote, editNote, saveDraft, openPublish, doPublish, act,
  resolveC, merge, provenance, splitIt, doImport, renameT, changeSrc });
refresh().catch(e => toast(e.message + '（检查令牌）', true));
