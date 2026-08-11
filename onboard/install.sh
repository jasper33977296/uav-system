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

# ── preflight：把「裝好了卻收不到資料」的兩個常見原因提前抓出來 ──
# 末尾 || true：選填項（如 AT_PORT，範本預設是註解掉的）grep 無匹配時，
# set -e + pipefail 會直接中止整個安裝——沒有這道保險，用預設值的機器裝不起來。
envval() { grep -E "^$1=" "$DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r' || true; }
GROUND_API_V="$(envval GROUND_API)"

# 範本佔位符沒改＝設定錯誤（會通過上面的 ^GROUND_API=http 檢查，故單獨擋）
case "$GROUND_API_V" in
  *'<'*|*'>'*) echo "GROUND_API 還是範本佔位符（$GROUND_API_V）——請填實際地面站 IP"; exit 1 ;;
esac

# 地面站可達性：不可達只警告不中止（先裝機、之後才接上 5G 是合理流程）
if python3 -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$GROUND_API_V/healthz', timeout=4).read()
except Exception as e:
    sys.exit('{}: {}'.format(type(e).__name__, e))
" 2>/tmp/gs_err; then
  echo "[preflight] 地面站可達：$GROUND_API_V"
else
  echo "[preflight] ⚠ 連不到地面站 $GROUND_API_V（$(cat /tmp/gs_err)）"
  echo "            服務仍會安裝——樣本會存進緩衝，接通後自動補傳。"
  echo "            若非預期：查 5G 路由、地面站是否已 docker compose up。"
fi
rm -f /tmp/gs_err

# modem AT 埠：不存在只警告（modem 可能還沒列舉），並列出實際可用的埠
AT_PORT_V="$(envval AT_PORT)"; AT_PORT_V="${AT_PORT_V:-/dev/ttyUSB2}"
if [ -e "$AT_PORT_V" ]; then
  echo "[preflight] modem AT 埠存在：$AT_PORT_V"
else
  echo "[preflight] ⚠ AT_PORT 不存在：$AT_PORT_V"
  echo "            目前的 tty：$(ls /dev/ttyUSB* 2>/dev/null | tr '\n' ' ' || echo '（無）')"
  echo "            RB5 通常是 /dev/ttyUSB2；改 .env 的 AT_PORT 後重跑本腳本。"
  echo "            先用 python3 onboard_node.py --probe 確認哪個埠會回 AT 回應。"
fi

# 解析器自我測試不過就不該裝
python3 "$DIR/onboard_node.py" --selftest

# 依實際 clone 路徑生成服務檔
sed "s|/opt/uav-onboard|$DIR|g" "$DIR/uav-link-node.service" > "$UNIT"
systemctl daemon-reload
systemctl enable uav-link-node
systemctl restart uav-link-node    # 重跑本腳本＝更新 unit 後重啟（enable --now 不會）
sleep 2
systemctl --no-pager -l status uav-link-node | head -10

cat <<'EOF'

── 驗證 ──────────────────────────────────────
  機上：  journalctl -u uav-link-node -f      # 應每秒取樣；HTTP 錯誤會印原因
  地面站：python3 scripts/check-onboard.py    # 六項完整性驗收
EOF
