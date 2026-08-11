#!/usr/bin/env python3
"""STATUSTEXT 注入器——issue 014 Phase A 驗收（分段重組／重複折疊／source）。

以 sysid 2 先送一顆有效心跳讓 backend 認機，再送三組 STATUSTEXT：
  A. 單段短訊息（severity=4 warning）              → 1 筆事件、count=1
  B. 長訊息切三段（同 id、chunk_seq 0/1/2、末段<50）→ 重組成 1 筆完整整句
  C. 同一句連送 5 次（severity=6 info）            → 折疊成 1 筆 count=5

用法（backend 容器內，host 網路直達 RX 14540）：
    python3 inject-statustext.py
"""
import socket
import time

from pymavlink.dialects.v20 import common as mav

GS, PORT = "127.0.0.1", 14540
SYSID = 2
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
m = mav.MAVLink(None, srcSystem=SYSID, srcComponent=1)


def send(msg):
    sock.sendto(msg.pack(m), (GS, PORT))
    m.seq = (m.seq + 1) % 256


# 認機：自駕儀心跳（autopilot=PX4，非 INVALID/非 GCS）
def heartbeat():
    return mav.MAVLink_heartbeat_message(
        mav.MAV_TYPE_QUADROTOR, mav.MAV_AUTOPILOT_PX4, 0, 0,
        mav.MAV_STATE_STANDBY, 3)


for _ in range(3):
    send(heartbeat())
    time.sleep(0.3)
print("registered sysid 2")

# A. 單段短訊息
send(mav.MAVLink_statustext_message(4, b"Takeoff commanded", 0, 0))
print("A: single short warning sent")
time.sleep(0.3)

# B. 長訊息切段（>50 字）。手動切 50 一段、同 id、chunk_seq 遞增、末段 <50
long = ("Preflight Fail: Compass not calibrated, please complete "
        "onboard sensor calibration before arming the vehicle")
cid = 7
chunks = [long[i:i + 50] for i in range(0, len(long), 50)]
for seq, part in enumerate(chunks):
    send(mav.MAVLink_statustext_message(2, part.encode(), cid, seq))
    time.sleep(0.05)
print(f"B: long message in {len(chunks)} chunks sent (total {len(long)} chars)")
time.sleep(0.3)

# C. 同句連送 5 次 → 折疊 count=5
for _ in range(5):
    send(mav.MAVLink_statustext_message(6, b"GPS: 12 satellites", 0, 0))
    time.sleep(0.1)
print("C: same info line x5 sent (expect fold count=5)")
time.sleep(0.5)
print("done")
