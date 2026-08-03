#!/usr/bin/env bash
# 啟動 TimescaleDB + PX4 SITL 容器，等到 DB 健康、schema 就緒。
#
# 用法：
#   ./scripts/dev-up.sh          DB + SITL
#   ./scripts/dev-up.sh db       只起 DB（不需要飛控模擬時）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT/.env"

require_docker

if [[ $# -gt 0 ]]; then SERVICES=("$@"); else SERVICES=(db sitl); fi

step "啟動容器：${SERVICES[*]}"
warn "首次執行要拉映像（TimescaleDB ~400MB、PX4 SITL ~2GB），請耐心等"
docker compose up -d "${SERVICES[@]}"

step "等待資料庫就緒（host port ${DB_PORT}）"
for i in $(seq 1 60); do
  if docker compose exec -T db pg_isready -U uav -q 2>/dev/null; then
    ok "資料庫已就緒"
    break
  fi
  [[ "$i" == 60 ]] && die "等待逾時，看 log：docker compose logs db"
  sleep 2
done

step "檢查 schema"
tables="$(docker compose exec -T db psql -U uav -d uav -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null || echo 0)"
if [[ "${tables//[!0-9]/}" -ge 8 ]]; then
  ok "已建立 $tables 張表"
  zones="$(docker compose exec -T db psql -U uav -d uav -tAc \
    "SELECT count(*) FROM interference_zones;" 2>/dev/null || echo 0)"
  cells="$(docker compose exec -T db psql -U uav -d uav -tAc \
    "SELECT count(*) FROM cells;" 2>/dev/null || echo 0)"
  ok "seed 場景：${cells//[!0-9]/} 個 gNB、${zones//[!0-9]/} 個干擾區"
else
  warn "只找到 $tables 張表。init SQL 只在資料卷第一次建立時執行——"
  warn "若之前起過失敗的 DB，清掉重來：docker compose down -v && ./scripts/dev-up.sh"
fi

printf '\n'
ok "psql 連線：psql postgresql://uav:uav@localhost:${DB_PORT}/uav"
ok "下一步：./scripts/dev-backend.sh"
