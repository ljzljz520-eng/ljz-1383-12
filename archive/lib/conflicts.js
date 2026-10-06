/**
 * conflicts.js — 冲突审阅流程
 *
 * 系统不会自动合并标题同名作品；所有不确定匹配进入队列，由人工裁决：
 *   merge   确认同一作品 → 记录证据后合并
 *   keep    确认不同作品（如同名不同片、不同译本属于不同作品）→ 关闭冲突
 *   new     标记为独立作品
 * 裁决结果全部留痕，可在作品页查看"合并来历"。
 */
'use strict';
const { load, mutate, uid } = require('./store');
const { mergeWorks } = require('./domain');

function listConflicts({ status = 'open' } = {}) {
  const db = load();
  return db.conflicts
    .filter(c => !status || c.status === status)
    .map(c => ({
      ...c,
      work: db.works.find(w => w.id === c.workId) || null,
      candidates: c.candidateIds.map(id => db.works.find(w => w.id === id)).filter(Boolean)
    }));
}

async function resolveConflict(conflictId, { action, evidence, reason = '', targetId }) {
  const db0 = load();
  const c = db0.conflicts.find(x => x.id === conflictId);
  if (!c) throw Object.assign(new Error('冲突不存在'), { status: 404 });
  if (c.status !== 'open') throw Object.assign(new Error('冲突已裁决'), { status: 400 });

  let mergeRecord = null;
  if (action === 'merge') {
    const correctId = targetId || c.candidateIds[0];
    if (!correctId) throw Object.assign(new Error('缺少合并目标'), { status: 400 });
    // 人工证据必须写明依据（外部标识优先；标题同名不算充分证据）
    mergeRecord = await mergeWorks({
      correctId, wrongId: c.workId,
      evidence: evidence || { manual: true, note: reason }, reason
    });
  }

  return mutate(async db => {
    const cc = db.conflicts.find(x => x.id === conflictId);
    cc.status = 'resolved';
    cc.resolvedAt = new Date().toISOString();
    cc.resolution = { action, evidence: evidence || null, reason, mergeId: mergeRecord?.id || null };
    return cc;
  });
}

/** 合并来历：作品页展示一条作品身上发生过的全部合并/拆分 */
function provenance(workId) {
  const db = load();
  return db.mergeLog
    .filter(m => m.correctId === workId || m.wrongId === workId)
    .map(m => ({
      id: m.id, action: m.action, at: m.at,
      withWork: m.correctId === workId ? m.wrongId : m.correctId,
      evidence: m.evidence || null, reason: m.reason || '',
      undoOf: m.undoOf || null
    }));
}

module.exports = { listConflicts, resolveConflict, provenance };
