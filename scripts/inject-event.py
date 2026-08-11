#!/usr/bin/env python3
"""MAVLink EVENT(410) 注入器——issue 014 Phase A.2 驗收（解碼／折疊／protocol 丟棄）。

以 sysid 2 註冊後送三組 EVENT 裸 frame（pymavlink 方言未定義 410，走手工解）：
  E1. id=1001 sev_ext=4(warning) 連送 3 次 → 折疊成 1 筆 count=3
  E2. id=2002 sev_ext=6(info)    送 1 次    → 1 筆 count=1
  E3. id=3003 sev_ext=8(protocol，非 MAV_SEVERITY) → 丟棄（不入流）

用法（backend 容器內）：python3 inject-event.py
"""
import socket
import struct
import time

from pymavlink.dialects.v20 import common as mav
from pymavlink.mavutil import x25crc

GS, PORT = "127.0.0.1", 14540
SYSID = 2
EVENT_CRC_EXTRA = 160          # 實算並對過真機 frame（issue 014 Phase A.2）
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
m = mav.MAVLink(None, srcSystem=SYSID, srcComponent=1)
_seq = 0


def event_frame(event_id: int, sev_ext: int, args: bytes = b"") -> bytes:
    """組一個 MAVLink2 EVENT(410) frame，**帶正確 X25 CRC**。收端解析器
    （robust_parsing、有狀態）會驗 CRC——dummy CRC 會被丟（實測踩過）。"""
    global _seq
    payload = struct.pack("<II", event_id, 0)      # id, event_time_boot_ms
    payload += struct.pack("<H", 0)                 # sequence
    payload += bytes([0, 0, sev_ext & 0x0F])        # dst_comp, dst_sys, log_levels
    payload += (args + b"\x00" * 40)[:40]           # arguments[40]
    p = payload.rstrip(b"\x00") or b"\x00"          # MAVLink2 尾零截斷
    core = bytes([len(p), 0, 0, _seq & 0xFF, SYSID, 1, 0x9A, 0x01, 0x00]) + p
    c = x25crc(core)
    c.accumulate([EVENT_CRC_EXTRA])
    crc = c.crc & 0xFFFF
    _seq = (_seq + 1) % 256
    return bytes([0xFD]) + core + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def hb():
    return mav.MAVLink_heartbeat_message(
        mav.MAV_TYPE_QUADROTOR, mav.MAV_AUTOPILOT_PX4, 0, 0,
        mav.MAV_STATE_STANDBY, 3)


for _ in range(3):
    sock.sendto(hb().pack(m), (GS, PORT))
    m.seq = (m.seq + 1) % 256
    time.sleep(0.3)
print("registered sysid 2")

for _ in range(3):                       # E1: 同 id x3 → 折疊
    sock.sendto(event_frame(1001, 4, b"\x0a\x0b"), (GS, PORT))
    time.sleep(0.15)
print("E1: id=1001 sev=4 x3 (expect fold count=3)")

sock.sendto(event_frame(2002, 6), (GS, PORT))      # E2: 單筆
time.sleep(0.15)
print("E2: id=2002 sev=6 x1")

sock.sendto(event_frame(3003, 8, b"\xde\xad"), (GS, PORT))   # E3: protocol → 丟
time.sleep(0.3)
print("E3: id=3003 sev=8 protocol (expect dropped)")
print("done")
