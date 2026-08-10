#!/bin/bash
# 機上一鍵安裝：把 onboard node 裝成開機自啟的 systemd 服務。
#
# clone 在哪個路徑都行（家目錄、/opt 皆可）——服務檔的路徑由本腳本
# 依實際位置生成。手動跑 python3 是測試手段，不是部署形態：
# 機上每個會動的元件都必須開機自啟、掛掉自復（2026-08-10 教訓：
# 機身重啟後手動程序全滅，5G 量測斷了一小時才發現）。
#
# 用法：sudo ./install.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=/etc/systemd/system/uav-link-node.service

[ "$(id -u)" = 0 ] || { echo "請用 sudo 執行：sudo ./install.sh"; exit 1; }
command -v python3 >/dev/null || { echo "找不到 python3"; exit 1; }

# .env：GROUND_API 是唯一必填
if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo "已建立 $DIR/.env —— 請編輯其中的 GROUND_API（地面站位址）後重跑本腳本"
  exit 1
fi
if ! grep -Eq '^GROUND_API=http' "$DIR/.env"; then
  echo "$DIR/.env 的 GROUND_API 尚未設定（如 GROUND_API=http://10.141.2.32:38000）"
  exit 1
fi

# 解析器自我測試不過就不該裝
python3 "$DIR/onboard_node.py" --selftest

# 依實際 clone 路徑生成服務檔
sed "s|/opt/uav-onboard|$DIR|g" "$DIR/uav-link-node.service" > "$UNIT"
systemctl daemon-reload
systemctl enable --now uav-link-node
sleep 2
systemctl --no-pager -l status uav-link-node | head -10

cat <<'EOF'

── 驗證 ──────────────────────────────────────
  機上：  journalctl -u uav-link-node -f      # 應每秒取樣；HTTP 錯誤會印原因
  地面站：python3 scripts/check-onboard.py    # 六項完整性驗收
EOF
