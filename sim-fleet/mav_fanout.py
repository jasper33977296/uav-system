#!/usr/bin/env python3
"""多機模擬環境的 MAVLink 合流路由——**listen-model**（issue 013／multi-sim-env.md pivot A）。

PX4 SITL 多實例（sitl_multiple_run）各實例 GCS mavlink 經 edit_rcS 導到本程式的
IN 埠（14545）；本程式合流轉發給 backend(14540)＋command(14550)、並把指令按
target_system 路由回各實例。這樣 backend/command 都拿到全艦隊（單埠 demux），且不必改埠。

第一次 bring-up 實測：PX4 GCS mavlink 不回「連線者」而是送固定 remote，故用 listen-model
（我方聽艦隊送來的、非我方連上去）。sysid 天然唯一（-i 0/1/2 → 1/2/3）。

拓撲：
  各實例 GCS(18570+i, 送 14545) ──▶ [S_in 14545] ──┬─▶ backend 14540（**forward-only 零回送**，讀寫分離）
                                                     └─▶ command 14550（雙向；command 的指令回來→依 sysid 送回實例 18570+i）
  各實例 GCS 監聽 18570+i 收指令；本程式記 sysid→(addr) 從遙測學。
"""
import os
import select
import socket

from pymavlink import mavutil

M = mavutil.mavlink
HOST = os.environ.get("FLEET_HOST", "127.0.0.1")
IN_PORT = int(os.environ.get("FANOUT_IN_PORT", "14545"))       # 艦隊 GCS 送這裡
BACKEND = (HOST, int(os.environ.get("BACKEND_PORT", "14540"))) # forward-only
COMMAND = (HOST, int(os.environ.get("COMMAND_PORT", "14550"))) # 雙向
ROUTER_SYSID = 255

s_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)        # 收艦隊遙測＋回送指令給實例
s_in.bind(("0.0.0.0", IN_PORT))
s_be = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)        # → backend（只送，不讀＝forward-only）
s_cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # ↔ command（送遙測、收指令）
s_cmd.bind(("0.0.0.0", 0))

parser = M.MAVLink(None)
parser.robust_parsing = True
sysid_addr = {}     # sysid → 該實例來源位址（指令回程；從遙測學）
print(f"mav-fanout(listen)：艦隊→:{IN_PORT} ⇒ backend {BACKEND[1]}(fwd-only)／command {COMMAND[1]}",
      flush=True)


def _route_to_fleet(data):
    """command 的指令 → 依 target_system 送回對應實例（未知/廣播→全發已學到的）。"""
    dest = None
    try:
        for msg in (parser.parse_buffer(data) or []):
            ts = getattr(msg, "target_system", 0)
            if ts and ts in sysid_addr:
                dest = [sysid_addr[ts]]
            break
    except Exception:
        pass
    for addr in (dest or list(sysid_addr.values())):
        try:
            s_in.sendto(data, addr)
        except OSError:
            pass


while True:
    r, _, _ = select.select([s_in, s_cmd], [], [], 0.5)
    for s in r:
        try:
            data, src = s.recvfrom(65535)
        except OSError:
            continue
        if s is s_in:
            # 艦隊遙測：學 sysid→addr、原樣 fan-out 給 backend＋command
            if len(data) >= 6 and data[0] in (0xFD, 0xFE):
                sysid = data[5] if data[0] == 0xFD else data[3]
                if sysid and sysid != ROUTER_SYSID:
                    sysid_addr[sysid] = src           # 該實例 GCS 來源位址（回指令用）
            try:
                s_be.sendto(data, BACKEND)             # forward-only
                s_cmd.sendto(data, COMMAND)
            except OSError:
                pass
        else:
            # command 回來的指令/心跳 → 路由回艦隊
            _route_to_fleet(data)
