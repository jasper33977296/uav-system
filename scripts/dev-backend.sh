#!/usr/bin/env bash
# 在本機（非容器）跑 backend，帶 --reload 方便開發。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"
# shellcheck source=lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT/.env"

[[ -x .venv/bin/uvicorn ]] || die "找不到 backend/.venv，請先執行 ./scripts/setup.sh"

# DB 跑在容器裡、對外發佈到 $DB_PORT；backend 從 host 連過去
export DATABASE_URL="${DATABASE_URL:-postgresql://uav:uav@localhost:${DB_PORT}/uav}"
export MAVLINK_URL="${MAVLINK_URL:-udpin://0.0.0.0:14540}"
export LINK_SOURCE="${LINK_SOURCE:-simulated}"

ok "DATABASE_URL=$DATABASE_URL"
ok "MAVLINK_URL=$MAVLINK_URL"
step "啟動 backend → http://localhost:${BACKEND_PORT}"

exec .venv/bin/uvicorn app.main:app --reload --port "${BACKEND_PORT}"
