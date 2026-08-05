#!/usr/bin/env bash
# UAV System — 開發環境一鍵安裝
#
# 可重複執行（idempotent）：已安裝的步驟會跳過。
# 用法：
#   ./scripts/setup.sh                 全部安裝
#   ./scripts/setup.sh --skip-docker   跳過 Docker（已裝過時）
#   ./scripts/setup.sh --help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib.sh
source "$ROOT/scripts/lib.sh"

SKIP_DOCKER=0 SKIP_BACKEND=0 SKIP_FRONTEND=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-docker)   SKIP_DOCKER=1 ;;
    --skip-backend)  SKIP_BACKEND=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) die "未知參數：$1（用 --help 看用法）" ;;
  esac
  shift
done

printf '\033[1mUAV System 開發環境安裝\033[0m  (%s)\n' "$ROOT"

# ============================================================
step "1/5 檢查前置工具"
# ============================================================
[[ "$(uname -s)" == "Linux" ]] || die "此腳本只針對 Linux；目前是 $(uname -s)"
need_cmd curl; need_cmd sudo; need_cmd ss
ok "curl / sudo / ss"

if ! have uv; then
  die "找不到 uv（backend 用它建 venv）。安裝：curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

if ! have npm; then
  # 非互動 shell 可能沒載入 nvm，補一次
  [[ -s "$HOME/.nvm/nvm.sh" ]] && { . "$HOME/.nvm/nvm.sh"; } || true
fi
have npm || die "找不到 npm。若用 nvm，先執行：source ~/.nvm/nvm.sh"
ok "node $(node --version) / npm $(npm --version)"

# ============================================================
step "2/5 挑選不衝突的連接埠"
# ============================================================
# 專案慣例：所有自家服務一律用 30000 以上的 port，
# 避開系統服務與本機其他專案（5432 的 PostgreSQL、3000 的 next-server）。
DB_PORT="$(pick_free_port 35432 35433 35434 35435)"
BACKEND_PORT="$(pick_free_port 38000 38001 38002 38003)"
FRONTEND_PORT="$(pick_free_port 33000 33001 33002 33003)"

report_port "TimescaleDB" 35432 "$DB_PORT"
report_port "Backend    " 38000 "$BACKEND_PORT"
report_port "Frontend   " 33000 "$FRONTEND_PORT"

# MAVLink UDP 14540/14550 是 PX4 與 QGroundControl 的固定慣例，
# 寫死在 SITL 映像裡，不隨上面的規則改動。
for p in 14540 14550; do
  udp_busy "$p" && warn "UDP $p 已被佔用（MAVLink 可能收不到資料）" || true
done

write_env_file "$ROOT/.env" \
  "DB_PORT=$DB_PORT" \
  "BACKEND_PORT=$BACKEND_PORT" \
  "FRONTEND_PORT=$FRONTEND_PORT"
# 部署設定：缺哪個補哪個（不覆蓋既有值），完整說明見 .env.example
for kv in "LINK_SOURCE=simulated" \
          "MAVLINK_URL=udpin://0.0.0.0:14540" \
          "SITL_BACKEND_HOST=127.0.0.1" "SITL_QGC_HOST=127.0.0.1"; do
  grep -q "^${kv%%=*}=" "$ROOT/.env" || echo "$kv" >> "$ROOT/.env"
done
ok "已寫入 $ROOT/.env（docker compose 與 dev 腳本共用；部署設定見 .env.example）"

# ============================================================
step "3/5 Docker Engine"
# ============================================================
if [[ "$SKIP_DOCKER" == 1 ]]; then
  warn "依參數跳過"
elif have docker && docker compose version >/dev/null 2>&1; then
  ok "已安裝：$(docker --version | cut -d, -f1)"
else
  bash "$ROOT/scripts/install-docker.sh"
fi

# ============================================================
step "4/5 Backend（uv venv + 套件）"
# ============================================================
if [[ "$SKIP_BACKEND" == 1 ]]; then
  warn "依參數跳過"
else
  [[ -d apps/backend/.venv ]] || uv venv apps/backend/.venv --python 3.12
  uv pip install -r apps/backend/requirements.txt --python apps/backend/.venv/bin/python -q
  ok "apps/backend/.venv 就緒（$(apps/backend/.venv/bin/python --version)）"
  if apps/backend/.venv/bin/python -c "import mavsdk, fastapi, asyncpg" 2>/dev/null; then
    ok "mavsdk / fastapi / asyncpg 匯入正常"
  else
    die "套件匯入失敗，請檢查上方 uv pip install 的輸出"
  fi
fi

# ============================================================
step "5/5 Frontend（npm 套件 + .env.local）"
# ============================================================
if [[ "$SKIP_FRONTEND" == 1 ]]; then
  warn "依參數跳過"
else
  ( cd frontend && npm install --no-fund --no-audit )
  # 直接帶入實際的 backend port，不只是複製範例檔
  write_env_file apps/frontend/.env.local \
    "NEXT_PUBLIC_API_URL=http://localhost:${BACKEND_PORT}" \
    "NEXT_PUBLIC_WS_URL=ws://localhost:${BACKEND_PORT}/ws/telemetry"
  ok "apps/frontend/.env.local 指向 backend :${BACKEND_PORT}"
fi

# ============================================================
printf '\n\033[1m✓ 安裝完成\033[0m\n\n'
if have docker && ! docker info >/dev/null 2>&1; then
  warn "docker 群組尚未生效——請重新登入，或在此終端執行： newgrp docker"
  printf '\n'
fi
cat <<EOS
接下來（三個終端）：

  1. 起 DB + PX4 SITL 容器（首次會拉 ~2GB 映像）
       ./scripts/dev-up.sh

  2. 起 backend
       ./scripts/dev-backend.sh          → http://localhost:$BACKEND_PORT/healthz

  3. 起 frontend
       ./scripts/dev-frontend.sh         → http://localhost:$FRONTEND_PORT

停掉容器：docker compose down（加 -v 連資料庫資料一起清掉）
EOS
