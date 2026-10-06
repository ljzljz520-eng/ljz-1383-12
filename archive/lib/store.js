/**
 * store.js — JSON 文件持久层
 *
 * 保存档案全部实体：作品 / 版本 / 经历事件 / 笔记版本 /
 * 主题 / 来源 / 导入批次(含原始行) / 冲突队列 / 合并审计。
 * 写入采用 "临时文件 + rename" 原子替换，防止离线重传时写坏数据。
 */
'use strict';
const fs = require('fs');
const path = require('path');

const DB_PATH = process.env.ARCHIVE_DB || path.join(__dirname, '..', 'data', 'archive.json');

const DEFAULT_DB = {
  meta: { version: 1, createdAt: null },
  works: [],          // 作品（书/电影/音乐……）
  editions: [],       // 版本/译本/录音版
  events: [],         // 阅读/观看/聆听事件（重读=新事件）
  notes: [],          // 个人笔记（版本链 + 可见状态）
  themes: [],         // 主题（可重命名，id 不变）
  sources: [],        // 来源链接（可变更，保留历史）
  imports: [],        // 导入批次 + 原始行 + 行级幂等键
  conflicts: [],      // 匹配冲突审阅队列
  mergeLog: []        // 合并/拆分审计
};

let cache = null;
let writeChain = Promise.resolve();

function load() {
  if (cache) return cache;
  try {
    cache = JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
  } catch (e) {
    if (e.code === 'ENOENT') {
      cache = JSON.parse(JSON.stringify(DEFAULT_DB));
      cache.meta.createdAt = new Date().toISOString();
      persist();
    } else throw e;
  }
  for (const k of Object.keys(DEFAULT_DB)) if (!cache[k]) cache[k] = JSON.parse(JSON.stringify(DEFAULT_DB[k]));
  return cache;
}

function persist() {
  const tmp = DB_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(cache, null, 2), 'utf8');
  fs.renameSync(tmp, DB_PATH);
}

/** 串行化写入，保证并发导入也不会丢数据 */
function mutate(fn) {
  writeChain = writeChain.then(async () => {
    const db = load();
    const r = await fn(db);
    persist();
    return r;
  });
  return writeChain;
}

function uid(prefix) {
  return prefix + '_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

module.exports = { load, mutate, uid, DB_PATH };
