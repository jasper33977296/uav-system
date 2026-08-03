#!/usr/bin/env bash
# 安裝 Docker Engine + compose plugin（Ubuntu，官方 apt repo）
#
# 需要 sudo。安裝後會把目前使用者加入 docker 群組，
# 該變更要重新登入（或 newgrp docker）才生效。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "$ROOT/scripts/lib.sh"

if have docker && docker compose version >/dev/null 2>&1; then
  ok "Docker 已安裝：$(docker --version | cut -d, -f1)"
  exit 0
fi

. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || warn "此腳本針對 Ubuntu 撰寫，目前是 ${ID:-unknown}，可能需要調整"

ok "將安裝 Docker Engine（來源：download.docker.com，需 sudo）"

# 1. 官方 GPG key
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# 2. apt repo
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

# 3. 安裝
sudo apt-get update -qq
sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 4. 啟用服務
sudo systemctl enable --now docker

# 5. 免 sudo 使用 docker
if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  ok "$USER 已在 docker 群組"
else
  sudo usermod -aG docker "$USER"
  warn "已把 $USER 加入 docker 群組 —— 需重新登入或執行 newgrp docker 才生效"
fi

ok "$(docker --version | cut -d, -f1) / $(docker compose version | head -1)"
