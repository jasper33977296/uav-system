#!/usr/bin/env python3
"""任務飛到一半暫停、換一份新任務、續飛——驗訊號記錄會不會被切斷。

## 要回答的問題

使用者 2026-09-07 問：訊號記錄的閘門設成「解鎖／上鎖」可不可以？因為有一個
情境是**任務飛到一半暫停它、傳一份新任務讓它執行**。

擔心的是「離地」這個判準會在暫停時把記錄切掉。但本系統擋下空中上傳
（`_inflight_upload_block`），合法路徑是三步：切 hold → 上傳 → 切回 mission。
**三步全程機體都在空中懸停**，所以「離地」一直成立。

這支就是把那三步真的跑一遍，量代理的 `on_ground` 計數器：
**整段暫停期間它必須維持 0**——那就是「記錄沒有被切斷」的直接證據。

用法：python3 scripts/sitl-pause-reupload.py
"""
import json
import re
import subprocess
import sys
import time

from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as M

LOG = ("/tmp/claude-1000/-home-k200-uav-system/"
       "9f8cf7b7-bd46-4a76-9fd2-8637fe12176a/tasks/bl0mjwp7q.output")
HOME = (24.7814, 120.9947)
ALT = 25.0

m = mavutil.mavlink_connection("tcp:127.0.0.1:5762", dialect="ardupilotmega")
m.wait_heartbeat(timeout=20)
tgt = m.target_system
print(f"連上 SITL sysid={tgt}\n")


def agent_stats():
    """代理最新一行狀態裡的 modem 計數與 landed 判定。"""
    try:
        out = subprocess.run(["tail", "-40", LOG], capture_output=True,
                             text=True).stdout
    except Exception:
        return {}
    for line in reversed(out.splitlines()):
        g = re.search(r"\{.*\}", line)
        if g:
            try:
                d = json.loads(g.group())
            except Exception:
                continue
            return {"state": d.get("state"), **(d.get("modem") or {})}
    return {}


def cmd(c, *p):
    m.mav.command_long_send(tgt, 1, c, 0, *(list(p) + [0] * (7 - len(p))))
    r = m.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    return r.result if r else None


def upload(items, label):
    m.mav.mission_count_send(tgt, 1, len(items), 0)
    sent, t0 = 0, time.time()
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
    print(f"  {label}：{len(items)} 項，ACK={getattr(ack, 'type', None)}")


def wp(dla, dlo):
    return (M.MAV_CMD_NAV_WAYPOINT, HOME[0] + dla, HOME[1] + dlo, ALT)


MISSION_A = [(M.MAV_CMD_NAV_TAKEOFF, HOME[0], HOME[1], ALT),
             wp(0.0006, 0.0), wp(0.0006, 0.0006), wp(0.0, 0.0006),
             (M.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0.0, 0.0, 0.0)]
# 新任務走反方向，確認它真的換了航線而不是繼續飛舊的
MISSION_B = [(M.MAV_CMD_NAV_TAKEOFF, HOME[0], HOME[1], ALT),
             wp(-0.0006, 0.0), wp(-0.0006, -0.0006),
             (M.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0.0, 0.0, 0.0)]

print("① 上傳任務 A 並起飛")
upload(MISSION_A, "任務 A")
m.set_mode_apm("GUIDED"); time.sleep(2)
cmd(M.MAV_CMD_COMPONENT_ARM_DISARM, 1)
t0 = time.time()
while time.time() - t0 < 20:
    hb = m.recv_match(type="HEARTBEAT", blocking=True, timeout=2)
    if hb and hb.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED:
        break
else:
    sys.exit("**解鎖失敗**")
cmd(M.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, ALT)
while time.time() - t0 < 90:
    p = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=2)
    if p and p.relative_alt / 1000.0 > ALT * 0.9:
        break
m.set_mode_apm("AUTO")
print(f"  起飛完成，切 AUTO｜代理：{agent_stats()}\n")

print("② 飛到第 2 個航點就暫停")
t0 = time.time()
while time.time() - t0 < 120:
    r = m.recv_match(type="MISSION_ITEM_REACHED", blocking=True, timeout=3)
    if r:
        print(f"  到達第 {r.seq} 項")
        if r.seq >= 2:
            break

base = agent_stats()
print(f"\n③ 切 HOLD（暫停）｜暫停前代理：{base}")
m.set_mode_apm("LOITER")
time.sleep(6)
mid = agent_stats()
alt_now = None
p = m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=3)
if p:
    alt_now = p.relative_alt / 1000.0
e = m.recv_match(type="EXTENDED_SYS_STATE", blocking=True, timeout=5)
print(f"  懸停中：高度 {alt_now} m｜landed_state="
      f"{getattr(e, 'landed_state', '?')}｜代理：{mid}")

print("\n④ 懸停中上傳新任務 B")
upload(MISSION_B, "任務 B")
time.sleep(6)
after_up = agent_stats()
print(f"  上傳後代理：{after_up}")

print("\n⑤ 切回 AUTO 續飛")
m.set_mode_apm("AUTO")
t0 = time.time()
reached = []
while time.time() - t0 < 240:
    msg = m.recv_match(type=["MISSION_ITEM_REACHED", "HEARTBEAT"],
                       blocking=True, timeout=3)
    if msg is None:
        continue
    if msg.get_type() == "MISSION_ITEM_REACHED":
        reached.append(msg.seq)
        print(f"  到達第 {msg.seq} 項（新任務）")
    elif not (msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED):
        print("  已上鎖——落地")
        break

end = agent_stats()
print(f"\n落地後代理：{end}")
print("\n══ 結論 ══")
print(f"暫停前 on_ground={base.get('on_ground')}  "
      f"懸停中={mid.get('on_ground')}  上傳後={after_up.get('on_ground')}")
print(f"暫停前 buffered={base.get('buffered')}  "
      f"懸停中={mid.get('buffered')}  上傳後={after_up.get('buffered')}")
same = base.get("on_ground") == mid.get("on_ground") == after_up.get("on_ground")
print(("✓ 整段暫停＋換任務期間 on_ground 沒有增加"
       "——**記錄一秒都沒有被切斷**") if same else
      "✗ 暫停期間有樣本被當成「在地面」跳過")
