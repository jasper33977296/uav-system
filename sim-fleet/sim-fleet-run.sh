#!/bin/bash
# 多機模擬環境機隊起機（issue 013／multi-sim-env.md pivot A）。在 px4-gazebo-headless
# image 內跑：一個 gzserver＋N 台 PX4（-i 0..N-1 → MAV_SYS_ID 1..N），並改 px4-rc.mavlink
# 讓各實例 GCS mavlink 送到 fanout(14545)、offboard 送到 dead 埠（避開 backend 14540 撞號）。
# fanout（mav_fanout.py）另跑，合流到 backend/command。
set -e
N=${1:-3}
FANOUT_PORT=${FANOUT_PORT:-14545}
DEAD_PORT=14599
src=/root/Firmware
RC=$src/build/px4_sitl_default/etc/init.d-posix/px4-rc.mavlink

# 容器每次都是原廠 image（fresh），下面 sed 對原檔各套一次即可（無疊加問題）。

# 1. rcS：GCS → fanout（固定 -o、-t 127.0.0.1）；offboard → dead（避 14540 撞號）
sed -i "s#mavlink start -x -u \$udp_gcs_port_local -r 4000000#mavlink start -x -u \$udp_gcs_port_local -r 4000000 -t 127.0.0.1 -o ${FANOUT_PORT}#" "$RC"
sed -i "s#-o \$udp_offboard_port_remote#-o ${DEAD_PORT}#" "$RC"

# 1b. HIGHRES_IMU 補流：backend 現在吃 GCS(normal 模式)link，而 normal 預設串流
#     不含 HIGHRES_IMU（帶 HIGHRES_IMU 的是 onboard 模式的 offboard link，已被上面
#     導去 dead 埠）。少了它 → IMU 卡的 accel/gyro/mag/temp/氣壓共 12 欄全 null
#     （ATTITUDE 角速率、VIBRATION 走 normal 預設，仍有值）。在 GCS link 顯式加流。
sed -i "/mavlink start -x -u \$udp_gcs_port_local/a mavlink stream -r 20 -s HIGHRES_IMU -u \$udp_gcs_port_local" "$RC"

# 2. sitl_multiple_run **就地** patch 成 -i 0..N-1（原用 n+1→sysid 2..；改 n→sysid 1..）。
#    不可 cp 到 /tmp——腳本靠 BASH_SOURCE 相對算 src_path，搬走會算錯（→ /build 壞路徑）。
FLEET="$src/Tools/simulation/gazebo-classic/sitl_multiple_run.sh"
sed -i 's#spawn_model ${vehicle_model} $(($n + 1))#spawn_model ${vehicle_model} $n#' "$FLEET"

# 3. 顯示（gz model spawn 要 X）＋起機（從原位置跑，src_path 才對）
Xvfb :99 -screen 0 1600x1200x24 >/dev/null 2>&1 &
export DISPLAY=:99
cd "$src"
exec bash "$FLEET" -n "$N"
