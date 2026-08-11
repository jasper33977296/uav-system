#!/usr/bin/env python3
"""多機模擬環境的 MAVLink 合流路由（issue 013／多機模擬環境；doc/multi-sim-env.md）。

⚠️ WIP 初稿（connect-model）——**下段需改成 listen-model**。第一次 bring-up 實測發現
PX4 GCS mavlink 不回連線者、而是送固定 remote（見 multi-sim-env.md「Bring-up 實測＋pivot」）。
Pivot A：edit_rcS 把各實例 GCS remote 重導到本程式監聽埠（14545）→ 本程式聽該埠收遙測、
複製轉發 backend（**forward-only 零回送**）＋command（雙向、指令按 sysid 路由回 18570+i）。
sysid 用 -i 0/1/2→1/2/3。下段重寫此檔為 listen-model。


PX4 SITL 多實例（sitl_multiple_run）各實例 GCS mavlink 綁 18570+i、等 GCS 連入；
本系統要單埠 demux（backend／command 各一埠、以 sysid 分）。此小程式＝地面合流：
  - instance 面（socket A）：1Hz 送 GCS 心跳到 127.0.0.1:18570+i（bootstrap＋keepalive，
    讓各實例開始並持續送遙測給我們），收各實例遙測。
  - sink 面（socket B）：把遙測原樣 fan-out 給 backend(14545)＋command(14555)；
    收 backend/command 回來的指令/查詢，**依 target_system 路由**回對應實例埠
    （unknown/broadcast→全送）。

mavlink-router 不在 image 也拉不到（registry denied），故自帶此輕量合流（可版控、
可控、零外部依賴）。sysid 天然唯一（實例 i→sysid i+1），撞號防線＋mode 去抖仍在 backend。
"""
import os
import select
import socket
import time

from pymavlink import mavutil

M = mavutil.mavlink
N = int(os.environ.get("FLEET_N", "3"))
GCS_BASE = int(os.environ.get("GCS_BASE_PORT", "18570"))   # 實例 GCS 埠 = base+i
HOST = os.environ.get("FLEET_HOST", "127.0.0.1")
BACKEND = (HOST, int(os.environ.get("BACKEND_PORT", "14545")))
COMMAND = (HOST, int(os.environ.get("COMMAND_PORT", "14555")))
ROUTER_SYSID = 255                       # 合流器自身 GCS 身分（bootstrap 心跳用）

inst_addrs = [(HOST, GCS_BASE + i) for i in range(N)]

# socket A：對實例（送心跳＋收遙測）
sa = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sa.bind(("0.0.0.0", 0))
# socket B：對 sink（送遙測給 backend/command＋收其指令）
sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sb.bind(("0.0.0.0", 0))

enc = M.MAVLink(None, srcSystem=ROUTER_SYSID, srcComponent=M.MAV_COMP_ID_MISSIONPLANNER)
parser = M.MAVLink(None)
parser.robust_parsing = True

sysid_port = {}     # sysid → 實例 GCS 埠（指令回程路由；從遙測學）
last_hb = 0.0
print(f"mav-fanout：{N} 實例 GCS {GCS_BASE}..{GCS_BASE+N-1} ↔ backend {BACKEND[1]}／command {COMMAND[1]}",
      flush=True)


def _bootstrap_heartbeat():
    """1Hz 送 GCS 心跳到每個實例埠：讓 normal-mode mavlink 認我方並持續送遙測。"""
    buf = enc.heartbeat_encode(M.MAV_TYPE_GCS, M.MAV_AUTOPILOT_INVALID, 0, 0, 0).pack(enc)
    enc.seq = (enc.seq + 1) % 256
    for addr in inst_addrs:
        try:
            sa.sendto(buf, addr)
        except OSError:
            pass


while True:
    now = time.monotonic()
    if now - last_hb >= 1.0:
        last_hb = now
        _bootstrap_heartbeat()
    r, _, _ = select.select([sa, sb], [], [], 0.2)
    for s in r:
        try:
            data, src = s.recvfrom(65535)
        except OSError:
            continue
        if s is sa:
            # 實例遙測 → 學 sysid→埠、原樣 fan-out 給 backend＋command
            if len(data) >= 6 and data[0] in (0xFD, 0xFE):
                sysid = data[5] if data[0] == 0xFD else data[3]
                if sysid and sysid != ROUTER_SYSID:
                    sysid_port[sysid] = src[1]      # src 埠＝該實例 GCS 埠
            try:
                sb.sendto(data, BACKEND)
                sb.sendto(data, COMMAND)
            except OSError:
                pass
        else:
            # backend/command 的指令/查詢 → 依 target_system 路由回實例
            targets = None
            try:
                for msg in (parser.parse_buffer(data) or []):
                    ts = getattr(msg, "target_system", 0)
                    if ts:
                        p = sysid_port.get(ts)
                        if p:
                            targets = [(HOST, p)]
                        break
            except Exception:
                parser = M.MAVLink(None); parser.robust_parsing = True
            # 無 target 或未知 → 廣播全實例（心跳等）
            for addr in (targets or inst_addrs):
                try:
                    sa.sendto(data, addr)
                except OSError:
                    pass
