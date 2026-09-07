#!/usr/bin/env python3
"""飛控換任務時會不會把索引歸零？（2026-09-07）

## 要回答的問題

使用者問：**飛控板本身支援「任務執行到一半換一份新任務」嗎，還是要我們
在系統上設計安全機制？**

判準只有一個：上傳新任務之後，`MISSION_CURRENT.seq` **會不會回到 0**。
* 會歸零 → 飛控自己處理好了，換任務是安全的原生操作
* 不歸零 → 飛機會用「舊任務碰巧進行到第幾點」去索引新任務，**那是未定義
  行為**，安全機制必須由我們做

地面上就測得出來：上傳 A、把目前項設成 2、上傳 B、讀 seq。不必真的飛。

用法：python3 scripts/sitl-mission-swap-probe.py
"""
import sys
import time

from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as M

HOME = (24.7814, 120.9947)
m = mavutil.mavlink_connection("tcp:127.0.0.1:5762", dialect="ardupilotmega")
m.wait_heartbeat(timeout=20)
tgt = m.target_system
print(f"連上 SITL sysid={tgt}")
# **ArduPilot 預設幾乎不送遙測**（015 實測：只有 HEARTBEAT／PARAM_VALUE／
# STATUSTEXT／TIMESYNC 四種）。不要求資料流就讀不到 MISSION_CURRENT——
# 第一版就是這樣讀回一串 None，看起來像「飛控不回答」而其實是我沒開口問
m.mav.request_data_stream_send(tgt, 1, M.MAV_DATA_STREAM_ALL, 4, 1)
time.sleep(1)


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
    print(f"  {label}：{len(items)} 項上傳完成（ACK={getattr(ack,'type',None)}）")


def seq_now(tag):
    """讀當下的 MISSION_CURRENT.seq（連讀幾則取最後一則，避開舊的快取）。"""
    got, t0 = None, time.time()
    m.mav.command_long_send(tgt, 1, 512, 0, 42, 0, 0, 0, 0, 0, 0)  # 主動要一則
    while time.time() - t0 < 8:
        r = m.recv_match(type="MISSION_CURRENT", blocking=True, timeout=2)
        if r:
            got = (r.seq, getattr(r, "total", None))
    print(f"  {tag}：MISSION_CURRENT.seq = {got[0] if got else '?'}"
          f"（total={got[1] if got else '?'}）")
    return got[0] if got else None


def wp(dla, dlo):
    return (M.MAV_CMD_NAV_WAYPOINT, HOME[0] + dla, HOME[1] + dlo, 25.0)


# A：5 項（0 起飛、1/2/3 航點、4 RTL）
A = [(M.MAV_CMD_NAV_TAKEOFF, HOME[0], HOME[1], 25.0),
     wp(0.0006, 0.0), wp(0.0006, 0.0006), wp(0.0, 0.0006),
     (M.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0.0, 0.0, 0.0)]
# B：4 項（0 起飛、1/2 航點、3 RTL）——**故意比 A 短**
B = [(M.MAV_CMD_NAV_TAKEOFF, HOME[0], HOME[1], 25.0),
     wp(-0.0006, 0.0), wp(-0.0006, -0.0006),
     (M.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0.0, 0.0, 0.0)]

print("\n① 上傳任務 A（5 項）")
upload(A, "任務 A")
seq_now("上傳後")

print("\n② 假裝已經飛到第 2 項")
m.mav.mission_set_current_send(tgt, 1, 2)
time.sleep(1)
before = seq_now("設定後")

print("\n③ 上傳任務 B（4 項）——這就是「執行到一半換任務」")
upload(B, "任務 B")
time.sleep(1)
after = seq_now("換任務後")

print("\n══ 結論 ══")
if after == 0:
    print("✓ 飛控**自己把索引歸零了**——換任務是安全的原生操作")
else:
    print(f"✗ **索引沒有歸零**：換任務前 seq={before}，換完 seq={after}")
    idx = after if after is not None else -1
    what = {0: "起飛", 1: "航點", 2: "航點", 3: "RTL"}.get(idx, "超出範圍")
    print(f"  新任務只有 {len(B)} 項，而它停在第 {idx} 項＝**{what}**")
    print("  → 飛機會用『舊任務碰巧飛到第幾點』去索引新任務。")
    print("  → 安全機制**必須由我們做**，飛控不會幫我們擋。")
