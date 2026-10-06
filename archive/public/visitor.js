'use strict';
const $ = s => document.querySelector(s);
let cursor = null, currentParams = '';
function toast(msg, err) { const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 2600); }

async function loadThemes() {
  const ts = await (await fetch('/api/public/themes')).json();
  $('#theme').innerHTML = '<option value="">全部主题</option>' + ts.map(t => `<option value="${t.id}">${t.name}</option>`).join('');
}
async function loadYears() {
  const s = await (await fetch('/api/public/stats')).json();
  const years = Object.keys(s.byYear).sort().reverse();
  $('#year').innerHTML = '<option value="">全部年份</option>' + years.map(y => `<option>${y}</option>`).join('');
}

async function fetchPage(reset) {
  if (reset) cursor = null;
  const p = new URLSearchParams({
    q: $('#q').value.trim(), year: $('#year').value, themeId: $('#theme').value,
    type: $('#type').value, scope: $('#scope').value, limit: '6', ...(cursor ? { cursor } : {})
  });
  currentParams = p.toString();
  const data = await (await fetch('/api/public/search?' + p)).json();
  cursor = data.nextCursor;
  renderStats(data);
  const grid = $('#grid');
  if (reset) grid.innerHTML = '';
  if (!data.items.length && reset) grid.innerHTML = '<p class="muted">没有符合条件的记录（私密/待公开笔记不会出现）。</p>';
  for (const it of data.items) grid.insertAdjacentHTML('beforeend', card(it));
  $('#total').textContent = `共 ${data.total} 个事件`;
  $('#more').style.display = cursor ? 'inline-block' : 'none';
}

function renderStats(data) {
  const scope = data.scope;
  const main = scope === 'work' ? data.stats.works : data.stats.events;
  $('#stats').innerHTML = `
    <div><b>${main}</b><div class="muted">${scope === 'work' ? '作品' : '事件'}数（当前筛选）</div></div>
    <div><b>${data.stats.events}</b><div class="muted">事件总数</div></div>
    <div><b>${data.stats.revisits}</b><div class="muted">其中重读</div></div>
    <div><b>${data.stats.works}</b><div class="muted">涉及作品</div></div>
    <div class="caliber">口径说明：${data.stats.caliber[scope === 'work' ? 'works' : 'events']}<br>
    另一口径（按${scope === 'work' ? '事件' : '作品'}）：${scope === 'work' ? data.stats.events + ' 次经历' : data.stats.works + ' 部作品'}</div>`;
}

function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function card(it) {
  const typeName = { book: '书', movie: '影', music: '音' }[it.type] || it.type;
  const notes = it.notes.map(n => `
    <div class="note"><strong>${esc(n.title)}</strong>
      ${n.excerpt ? `<div class="excerpt">「${esc(n.excerpt)}」</div>` : ''}
      <div class="meta">发布于 ${n.publishedAt.slice(0, 10)}
        <button class="mini ghost" onclick="share('${n.noteId}')">分享卡</button></div>
    </div>`).join('');
  return `<div class="card">
    <h3>[${typeName}] ${esc(it.title)}</h3>
    <div class="meta">${esc((it.creators || []).join(' / '))} · ${it.date}${it.revisit ? ' · <span class="tag revisit">重读</span>' : ''}</div>
    ${it.edition ? `<div class="meta">版本：${esc(it.edition.label || it.edition.kind)}${it.edition.publisher ? ' / ' + esc(it.edition.publisher) : ''}</div>` : ''}
    <div>${(it.themes || []).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>
    ${it.takeaway ? `<p style="margin-top:.5rem">${esc(it.takeaway)}</p>` : ''}
    ${notes}
  </div>`;
}
async function share(noteId) {
  const r = await fetch('/api/public/share?noteId=' + noteId);
  if (!r.ok) return toast('该笔记不可分享', true);
  const { card, sourceLinks } = await r.json();
  const text = `${card.workTitle}\n${card.title}\n${card.excerpt || ''}\n来源：${(sourceLinks || []).join('；') || '（无）'}\n发布于 ${card.publishedAt.slice(0,10)}`;
  try { await navigator.clipboard.writeText(text); toast('分享卡内容已复制（仅含已发布内容与来源）'); }
  catch { toast(text); }
}
window.share = share;
$('#go').onclick = () => fetchPage(true);
$('#more').onclick = () => fetchPage(false);
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') fetchPage(true); });
$('#scope').onchange = () => fetchPage(true);
loadThemes().then(loadYears).then(() => fetchPage(true));
