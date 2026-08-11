#!/bin/bash
# RB5（ModalAI m0052／PX4）機上 MAVLink 設定：把對外通道改成主動 unicast 打
# 地面站 14540（資料）/14541（指令），並加機內 127.0.0.1:14540（onboard node）。
# 見同目錄 README.md 與 issues/016。
#
# 安全設計：預設 dry-run（只印不改）；--apply 才寫入。寫入前備份、以 marker
# 區塊冪等插入（可重跑）。尚未在真機驗證——--apply 前請看預覽。
set -euo pipefail

PX4_START=/usr/bin/voxl-px4-start
MARK_BEGIN='# >>> uav-system mavlink（configure-mavlink.sh 管理，勿手改區塊內）>>>'
MARK_END='# <<< uav-system mavlink <<<'
MAX_INSTANCES=4            # PX4 mavlink 實例上限（超額行會啟動失敗）

GS_IP=""; SYSID=""; APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --gs-ip) GS_IP="$2"; shift 2;;
    --sysid) SYSID="$2"; shift 2;;
    --apply) APPLY=1; shift;;
    *) echo "未知參數：$1"; echo "用法：sudo $0 --gs-ip <地面站IP> [--sysid N] [--apply]"; exit 2;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "請用 sudo 執行"; exit 1; }
[ -n "$GS_IP" ] || { echo "缺 --gs-ip <地面站IP>（機上 unicast 目標）"; exit 2; }
[[ "$GS_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "--gs-ip 不是合法 IPv4：$GS_IP"; exit 2; }

# ── 世代偵測：只處理 m0052 代（有 voxl-px4-start）；server 代中止 ──
if [ ! -f "$PX4_START" ]; then
  echo "❌ 找不到 $PX4_START——這台可能是 voxl-mavlink-server 代。"
  echo "   server 代的 MAVLink 由平台層獨佔管理，不適用本腳本（見 issue 016，"
  echo "   路徑待上機檢查後另做）。請回報：ls /etc/modalai/ 與 voxl-inspect-services"
  exit 3
fi

# ── 實例數檢查：現有 mavlink start（含既有標記區塊內的）＋我方 3 條 ≤ 上限 ──
existing=$(grep -cE '^[[:space:]]*mavlink start' "$PX4_START" || true)
managed=$(awk "/$MARK_BEGIN/{f=1} f&&/mavlink start/{c++} /$MARK_END/{f=0} END{print c+0}" "$PX4_START")
outside=$((existing - managed))          # 非我方管理的現有實例
projected=$((outside + 3))
echo "現有 mavlink 實例：$existing（我方管理 $managed／其他 $outside）；套用後我方共 3 條 → 合計 $projected"
if [ "$projected" -gt "$MAX_INSTANCES" ]; then
  echo "❌ 合計 $projected 超過上限 $MAX_INSTANCES。請先在 $PX4_START 註解掉沒觀眾的既有實例"
  echo "   （QGC 已退場的話，餵 voxl-mavlink-server 的那組通常可停），再重跑。"
  exit 4
fi

# ── 組出要插入的 marker 區塊 ──
BLOCK=$(cat <<EOF
$MARK_BEGIN
$( [ -n "$SYSID" ] && echo "param set MAV_SYS_ID $SYSID          # 多機唯一（見 issue 016 sysid bug）" )
param set MAV_BROADCAST 0                # 5G 用 unicast，不廣播（廣播不過 5G；WiFi 下會關掉 QGC 自動連線）
mavlink start -x -u 14560 -o 14540 -t $GS_IP -m onboard -r 50000        # 資料 → 地面站（唯讀通道）
mavlink start -x -u 14561 -o 14541 -t $GS_IP -m minimal -r 20000       # 指令 → 地面站（雙向通道）
mavlink start -x -u 14562 -o 14540 -t 127.0.0.1 -m onboard -n lo -r 100000  # 機內 → onboard node 綁座標
$MARK_END
EOF
)

echo "── 將插入 $PX4_START 的區塊 ──"
echo "$BLOCK"
echo "────────────────────────────"

if [ "$APPLY" != 1 ]; then
  echo "（dry-run。確認無誤後加 --apply 真的寫入。）"
  exit 0
fi

# ── 寫入：備份 → 冪等替換/插入 marker 區塊 ──
BAK="$PX4_START.bak.$(date +%Y%m%d-%H%M%S 2>/dev/null || echo manual)"
cp "$PX4_START" "$BAK"
echo "已備份 → $BAK"

if grep -qF "$MARK_BEGIN" "$PX4_START"; then
  # 已有區塊：用 awk 換掉舊區塊
  awk -v blk="$BLOCK" "
    \$0 ~ /$MARK_BEGIN/ {print blk; skip=1; next}
    \$0 ~ /$MARK_END/ {skip=0; next}
    !skip {print}
  " "$PX4_START" > "$PX4_START.tmp" && mv "$PX4_START.tmp" "$PX4_START"
  echo "已更新既有 marker 區塊（冪等）。"
else
  # 尚無區塊：插在最後一個 mavlink start 之後（或檔尾）
  printf '\n%s\n' "$BLOCK" >> "$PX4_START"
  echo "已附加 marker 區塊至檔尾。"
fi

echo
echo "完成。接著："
echo "  sudo systemctl restart voxl-px4"
echo "  地面站驗證： curl :38000/healthz、curl :38001/healthz、python3 scripts/check-onboard.py"
echo "  回填備份： cp $BAK $PX4_START"
