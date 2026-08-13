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
ARDU_IMG=${ARDU_IMG:-radarku/ardupilot-sitl:latest}
FANOUT_IMG=${FANOUT_IMG:-uav-system-uav-backend}   # 借 backend image（有 pymavlink）

# 出生點（lat,lon,alt）。**預設台灣**——映像的原廠預設是蘇黎世（PX4）與波士頓
# （ArduPilot），那是 image 作者的選擇不是我們的。實際場域在台灣，而 NLSC 正射
# 影像圖資**只涵蓋台灣**（境外回空白磚），出生點在境外＝底圖永遠是空的。
#
# 預設點：臺北大稻埕碼頭一帶（淡水河岸＋東側密集街廓）——河岸與街廓交界比空曠
# 郊區更能看出正射影像的效果。要換地點覆寫 FLEET_HOME 即可。
FLEET_HOME=${FLEET_HOME:-25.0554,121.5065,5}
HOME_LAT=${FLEET_HOME%%,*}
HOME_REST=${FLEET_HOME#*,}
HOME_LON=${HOME_REST%%,*}
HOME_ALT=${HOME_REST#*,}

up() {
  # 單機 sim 與機隊互斥（都想佔 14540/14545 一帶）——先停單機。
  docker stop uav-sitl >/dev/null 2>&1 || true
  echo "[1/2] 起 SITL 機隊（$N 台，sysid 1..$N）…"
  docker rm -f uav-sitl-fleet >/dev/null 2>&1 || true
  docker run -d --name uav-sitl-fleet --network host \
    -e PX4_HOME_LAT="$HOME_LAT" -e PX4_HOME_LON="$HOME_LON" -e PX4_HOME_ALT="$HOME_ALT" \
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

ardu() {
  # ArduPilot SITL（sysid 10）＋ TCP↔UDP 橋接。原本兩者都是臨時 docker run 起的、
  # 出生點寫死——納進腳本才能與 PX4 機隊共用同一個 FLEET_HOME。
  #
  # **出生點要用環境變數 LAT/LON/ALT/DIR，不是 --custom-location**：這個 image 的
  # 啟動包裝（RiTW）會用那四個環境變數自己組出 `--home`，把命令列上的
  # --custom-location 蓋掉。實測傳了 --custom-location 仍以映像預設的波士頓起機。
  echo "起 ArduPilot SITL（sysid 10，home $FLEET_HOME）…"
  docker rm -f uav-sitl-ardu >/dev/null 2>&1 || true
  # **必須 --entrypoint sh**：這個 image 自帶 entrypoint（會用 LAT/LON/ALT/DIR
  # 環境變數自己跑一次 sim_vehicle.py）。不覆寫的話，我們的指令會被當成多餘
  # 參數吞掉——實測後果是 `SYSID_THISMAV 10` 從沒執行，ArduPilot 以預設 sysid 1
  # 起機，**與 PX4 一號機撞號**（backend 記到「來源位址改變（撞號？）」）。
  #
  # 出生點兩條路都設：環境變數 LAT/LON/ALT（image 包裝用的）＋ --custom-location
  # （我們自己這行用的）。只設命令列會被 image 包裝蓋掉，只設環境變數則在覆寫
  # entrypoint 後沒人讀——兩個都給才不必記住現在走的是哪條。
  docker run -d --name uav-sitl-ardu --network host \
    -e LAT="$HOME_LAT" -e LON="$HOME_LON" -e ALT="$HOME_ALT" -e DIR=0 \
    --entrypoint sh "$ARDU_IMG" -c "
    printf '\nSYSID_THISMAV 10\n' >> /ardupilot/Tools/autotest/default_params/copter.parm &&
    /ardupilot/Tools/autotest/sim_vehicle.py --vehicle ArduCopter -I0 \
      --custom-location=$HOME_LAT,$HOME_LON,$HOME_ALT,0 \
      -w --frame quad --no-rebuild --no-mavproxy --speedup 1" >/dev/null

  # 橋接：ArduPilot 只開 TCP 5760，機隊 fanout 收 UDP 14545。**必須在 SITL 之後
  # 重起**——它是對舊容器的 TCP 連線，SITL 換了容器它不會自己接回去
  # （症狀：sysid 10 的 age 一路長大，看起來像機掛了）。
  echo "起 ArduPilot 橋接（tcp 5760 ⇒ fanout 14545）…"
  docker rm -f uav-ardu-bridge >/dev/null 2>&1 || true
  docker run -d --name uav-ardu-bridge --network host --restart unless-stopped \
    -v "$PWD/sim-fleet:/sim-fleet:ro" \
    --entrypoint python3 "$FANOUT_IMG" /sim-fleet/ardupilot_bridge.py >/dev/null
  echo "  （-w＝抹除既有參數重來，所以 SYSID_MYGCS 會回原廠 255；"
  echo "    要驗搖桿需另設 254，見 issue 015）"
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
  ardu) ardu ;;
  down) down ;;
  status) status ;;
  *) echo "用法：$0 up [N] | ardu | down | status"
     echo "      出生點：FLEET_HOME=lat,lon,alt（預設 $FLEET_HOME）"; exit 1 ;;
esac
