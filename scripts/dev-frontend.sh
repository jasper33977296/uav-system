#!/usr/bin/env bash
# 跑 Next.js dev server（連接埠由 setup.sh 寫進根目錄 .env，預設 33000）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/apps/frontend"
# shellcheck source=lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT/.env"

[[ -d node_modules ]] || die "找不到 apps/frontend/node_modules，請先執行 ./scripts/setup.sh"
[[ -f .env.local ]] || cp .env.local.example .env.local

have npm || { [[ -s "$HOME/.nvm/nvm.sh" ]] && . "$HOME/.nvm/nvm.sh"; }
have npm || die "找不到 npm。若用 nvm：source ~/.nvm/nvm.sh"

step "啟動 frontend → http://localhost:${FRONTEND_PORT}"
# -H 0.0.0.0：讓區網內其他機器能開頁面（API 位址由 signal.ts 從瀏覽器網址推導）
exec npm run dev -- --port "${FRONTEND_PORT}" -H "${FRONTEND_HOST:-0.0.0.0}"
