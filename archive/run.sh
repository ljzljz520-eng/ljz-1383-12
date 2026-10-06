#!/usr/bin/env bash
# 成长站 · 书影音档案 — 一键本地启动（仅需 Python 3.10+，零外部依赖，可完全离线）
set -e
cd "$(dirname "$0")"
export ARCHIVE_ADMIN_TOKEN="${ARCHIVE_ADMIN_TOKEN:-dev-token}"
export ARCHIVE_DB="${ARCHIVE_DB:-data/archive.db}"
PORT="${PORT:-8000}"

if [ ! -s "$ARCHIVE_DB" ]; then
  echo "首次启动：写入演示数据（可用 seed.py 另行重置）"
  python3 seed.py "$ARCHIVE_DB"
fi
echo "访客页:  http://localhost:${PORT}/"
echo "管理端:  http://localhost:${PORT}/admin   令牌: ${ARCHIVE_ADMIN_TOKEN}"
exec python3 -m app.server --db "$ARCHIVE_DB" --port "${PORT}" --host 0.0.0.0 \
  --token "${ARCHIVE_ADMIN_TOKEN}"
