#!/usr/bin/env bash
# 各腳本共用的小工具。以 source 使用，不直接執行。

_c_ok=$'\033[32m'; _c_warn=$'\033[33m'; _c_err=$'\033[31m'; _c_off=$'\033[0m'

step() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$_c_ok"   "$_c_off" "$*"; }
warn() { printf '  %s!%s %s\n' "$_c_warn" "$_c_off" "$*"; }
die()  { printf '  %s✗%s %s\n' "$_c_err"  "$_c_off" "$*" >&2; exit 1; }

have()     { command -v "$1" >/dev/null 2>&1; }
need_cmd() { have "$1" || die "找不到指令：$1"; }

# TCP / UDP 連接埠是否已被監聽
port_busy() { ss -tln 2>/dev/null | grep -qE "[:.]$1[[:space:]]"; }
udp_busy()  { ss -uln 2>/dev/null | grep -qE "[:.]$1[[:space:]]"; }

# 從候選清單挑第一個沒被佔用的 port；全都佔用就回傳最後一個
pick_free_port() {
  local p last=""
  for p in "$@"; do
    last="$p"
    port_busy "$p" || { printf '%s' "$p"; return 0; }
  done
  printf '%s' "$last"
}

# 回報某服務最後用到的 port（偏好值被佔用時提示改用了什麼）
report_port() {
  local name="$1" want="$2" got="$3"
  if [[ "$want" == "$got" ]]; then
    ok "$name → $got"
  else
    warn "$name 偏好的 $want 已被佔用 → 改用 $got"
  fi
}

# 寫設定檔，逐行覆寫同名的 KEY=，其餘保留（不會蓋掉使用者自己加的變數）
write_env_file() {
  local file="$1"; shift
  local line key tmp
  tmp="$(mktemp)"
  [[ -f "$file" ]] && cat "$file" >"$tmp"
  for line in "$@"; do
    key="${line%%=*}"
    if grep -qE "^${key}=" "$tmp" 2>/dev/null; then
      sed -i "s|^${key}=.*|${line}|" "$tmp"
    else
      printf '%s\n' "$line" >>"$tmp"
    fi
  done
  mv "$tmp" "$file"
}

# 讀根目錄 .env（dev 腳本用）
load_env() {
  local file="${1:-}"
  [[ -f "$file" ]] || die "找不到 $file，請先執行 ./scripts/setup.sh"
  set -a
  # shellcheck disable=SC1090
  source "$file"
  set +a
}

# docker 群組未生效時給出可執行的指示
require_docker() {
  have docker || die "找不到 docker，請先執行 ./scripts/setup.sh"
  docker info >/dev/null 2>&1 && return 0
  die "無法連上 docker daemon。若剛安裝完，請重新登入或先執行： newgrp docker"
}
