#!/bin/bash
# 多機 SITL 機隊控制（issue 013／multi-sim-env.md pivot A）。三個角色：
#   1) uav-sitl-fleet：一個 gzserver＋N 台 PX4（-i 0..N-1 → MAV_SYS_ID 1..N），
#      各實例 GCS mavlink 導到 fanout 埠 14545（見 sim-fleet-run.sh）。
#   2) uav-sim-fanout：listen-model 合流路由，把艦隊遙測 fan-out 給
#      backend(14540)＋command(14550)，並把 command 指令依 sysid 路由回各實例
#      （見 mav_fanout.py）。用 backend image（含 pymavlink），獨立容器＝
#      不受 backend --reload 重啟影響。
#   3) backend/command/db/frontend：既有 compose 堆疊，不需改埠。
#
# 用法：./fleet.sh up [N] | down | status  （N 預設 3，範圍 1–3）
set -e
cd "$(dirname "$0")/.."                       # repo 根
N=${2:-3}
SITL_IMG=jonasvautherin/px4-gazebo-headless:1.14.3
FANOUT_IMG=${FANOUT_IMG:-uav-system-uav-backend}   # 借 backend image（有 pymavlink）

up() {
  # 單機 sim 與機隊互斥（都想佔 14540/14545 一帶）——先停單機。
  docker stop uav-sitl >/dev/null 2>&1 || true
  echo "[1/2] 起 SITL 機隊（$N 台，sysid 1..$N）…"
  docker rm -f uav-sitl-fleet >/dev/null 2>&1 || true
  docker run -d --name uav-sitl-fleet --network host \
    -v "$PWD/sim-fleet:/sim-fleet:ro" \
    --entrypoint bash "$SITL_IMG" /sim-fleet/sim-fleet-run.sh "$N" >/dev/null
  echo "[2/2] 起 fanout 合流路由（14545 ⇒ backend 14540＋command 14550）…"
  docker rm -f uav-sim-fanout >/dev/null 2>&1 || true
  docker run -d --name uav-sim-fanout --network host --restart unless-stopped \
    -v "$PWD/sim-fleet:/sim-fleet:ro" \
    -e FANOUT_IN_PORT=14545 -e BACKEND_PORT=14540 -e COMMAND_PORT=14550 \
    --entrypoint python3 "$FANOUT_IMG" /sim-fleet/mav_fanout.py >/dev/null
  echo "機隊起機中——PX4 開機＋GPS 定位約需 20–40s。之後跑：./fleet.sh status"
}

down() {
  echo "停機隊＋fanout…"
  docker rm -f uav-sitl-fleet uav-sim-fanout >/dev/null 2>&1 || true
  echo "已停。（單機 sim uav-sitl 若要用，docker start uav-sitl）"
}

status() {
  echo "== 容器 =="
  docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'uav-sitl-fleet|uav-sim-fanout' || echo "  機隊未運行"
  echo "== command 可控清單（sysid／age）=="
  curl -s http://localhost:38001/healthz 2>/dev/null \
    | python3 -c "import sys,json;d=json.load(sys.stdin);[print(f'  sysid {k}  age {v[\"age_s\"]}s  armed {v[\"armed\"]}') for k,v in sorted(d.get('drones',{}).items())] or print('  （command 尚未收到艦隊心跳）')" 2>/dev/null \
    || echo "  command 服務無回應"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "用法：$0 up [N] | down | status"; exit 1 ;;
esac
