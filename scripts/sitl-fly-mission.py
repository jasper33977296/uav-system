#!/usr/bin/env python3
"""用 SITL 真的飛一趟任務，驗地面站有沒有把過程記下來（2026-09-07）。

## 為什麼指令是直接下給 SITL，不經過本系統的 command 服務

**模擬器在本系統的規則下永遠不可能被指揮。** 入列要求板子身分
（issues/038），而 ArduPilot SITL 的 `AUTOPILOT_VERSION` 回的是
`uid=0`、`uid2` 全空——沒有真板子就沒有板號，所以它恆為 `unmanaged`。

但這不影響要驗的事：**紀錄路徑不經過 command 服務**。飛控送出的訊息由
機上代理橋接到 backend，backend 落盤——任務是誰下的都一樣。直接下給 SITL
反而更嚴格：它證明「紀錄不依賴我們自己有沒有記過那道指令」。

（指令留痕那半另有 `test-command-session-link.py` 真打 HTTP 端點驗過。）

用法：python3 scripts/sitl-fly-mission.py [--port 5762]
"""
import argparse
import time

from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as M

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=5762)
ap.add_argument("--alt", type=float, default=20.0)
ap.add_argument("--timeout", type=float, default=300.0)
a = ap.parse_args()

HOME = (24.7814, 120.9947)
# 邊長約 60 m 的小三角：夠短，跑得完；夠長，看得出航點切換
LEGS = [(0.00055, 0.0), (0.00055, 0.00055), (0.0, 0.00055)]

m = mavutil.mavlink_connection(f"tcp:127.0.0.1:{a.port}", dialect="ardupilotmega")
m.wait_heartbeat(timeout=20)
tgt = m.target_system
print(f"連上 SITL sysid={tgt}")


def cmd(c, *p):
    m.mav.command_long_send(tgt, 1, c, 0, *(list(p) + [0] * (7 - len(p))))
    r = m.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    return r.result if r else None


# ── 上傳任務 ────────────────────────────────────────────────────────
items = [(M.MAV_CMD_NAV_TAKEOFF, HOME[0], HOME[1], a.alt)]
items += [(M.MAV_CMD_NAV_WAYPOINT, HOME[0] + dla, HOME[1] + dlo, a.alt)
          for dla, dlo in LEGS]
items.append((M.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0.0, 0.0, 0.0))

m.mav.mission_count_send(tgt, 1, len(items), 0)
sent = 0
t0 = time.time()
while sent < len(items) and time.time() - t0 < 30:
    req = m.recv_match(type=["MISSION_REQUEST", "MISSION_REQUEST_INT"],
                       blocking=True, timeout=5)
    if not req:
        continue
    c, la, lo, al = items[req.seq]
    m.mav.mission_item_int_send(
        tgt, 1, req.seq, M.MAV_FRAME_GLOBAL_RELATIVE_ALT, c,
        0, 1, 0, 0, 0, 0, int(la * 1e7), int(lo * 1e7), al, 0)
    sent = max(sent, req.seq + 1)
ack = m.recv_match(type="MISSION_ACK", blocking=True, timeout=10)
print(f"任務上傳：{len(items)} 項，ACK={getattr(ack, 'type', None)}")

# ── 解鎖起飛 ────────────────────────────────────────────────────────
m.set_mode_apm("GUIDED")
time.sleep(2)
print("ARM →", cmd(M.MAV_CMD_COMPONENT_ARM_DISARM, 1))
t0 = time.time()
while time.time() - t0 < 20:
    hb = m.recv_match(type="HEARTBEAT", blocking=True, timeout=2)
    if hb and hb.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED:
        print("已解鎖"); break
else:
    raise SystemExit("**解鎖失敗**——後面不用跑了")

print("TAKEOFF →", cmd(M.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, a.alt))
t0 = time.time()
while time.time() - t0 < 60:
    p = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=2)
    if p and p.relative_alt / 1000.0 > a.alt * 0.9:
        print(f"到達 {p.relative_alt / 1000.0:.1f} m"); break

# ── 切 AUTO，讓它自己飛 ─────────────────────────────────────────────
m.set_mode_apm("AUTO")
print("已切 AUTO，開始飛任務\n")

t0 = time.time()
seq = state = None
reached = []
while time.time() - t0 < a.timeout:
    msg = m.recv_match(type=["MISSION_CURRENT", "MISSION_ITEM_REACHED",
                             "HEARTBEAT"], blocking=True, timeout=3)
    if msg is None:
        continue
    t = msg.get_type()
    if t == "MISSION_ITEM_REACHED":
        reached.append(msg.seq)
        print(f"  [{time.time() - t0:5.1f}s] 到達第 {msg.seq} 項")
    elif t == "MISSION_CURRENT":
        s2 = getattr(msg, "mission_state", None)
        if msg.seq != seq or s2 != state:
            print(f"  [{time.time() - t0:5.1f}s] seq={msg.seq} state={s2}")
            seq, state = msg.seq, s2
    elif t == "HEARTBEAT":
        if seq is not None and not (msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED):
            print(f"  [{time.time() - t0:5.1f}s] 已上鎖——落地了")
            break

print(f"\n飛完：到達過 {reached}，最後 seq={seq} state={state}")
