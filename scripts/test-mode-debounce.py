#!/usr/bin/env python3
"""mode_change 防抖驗證（issue：撞號多來源 mode 翻打噴 15/秒 事故，2026-08-11）。

餵翻打的 custom_mode（HOLD↔RTL 交替）進 _handle，斷言**不噴** mode_change；
再餵穩定的新模式，斷言噴**恰一筆**。跑在 backend 容器內：python3 test-mode-debounce.py
"""
import asyncio
import sys

sys.path.insert(0, "/srv")
from pymavlink import mavutil

from app import db, mavlink_rx
from app.ws import manager

M = mavutil.mavlink
HOLD = (4 << 16) | (3 << 24)
RTL = (4 << 16) | (5 << 24)
MISSION = (4 << 16) | (4 << 24)
inserts = []


async def fake_insert(drone_id, session_id, severity, type_, detail, source="system"):
    inserts.append({"type": type_, "detail": detail})
    return {"id": len(inserts), "time": "T", "severity": severity, "type": type_,
            "detail": detail, "source": source}


async def fake_broadcast(msg):
    pass


async def fake_drone_for_sysid(sysid):
    return ("d5", "fake5")


def heartbeat(custom_mode):
    enc = M.MAVLink(None, srcSystem=5, srcComponent=1)
    fr = M.MAVLink_heartbeat_message(M.MAV_TYPE_QUADROTOR, M.MAV_AUTOPILOT_PX4,
                                     M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                                     custom_mode, M.MAV_STATE_STANDBY, 3).pack(enc)
    p = M.MAVLink(None); p.robust_parsing = True
    return (p.parse_buffer(fr) or [])[0]


async def main():
    db.insert_event = fake_insert
    db.drone_for_sysid = fake_drone_for_sysid
    manager.broadcast = fake_broadcast

    rx = mavlink_rx.MavlinkRx()
    addr = ("127.0.0.1", 6000)

    # 建檔＋定 HOLD（連 2 顆確立 flight_mode）
    await rx._handle(heartbeat(HOLD), addr)
    await rx._handle(heartbeat(HOLD), addr)
    base = len(inserts)

    # 翻打：HOLD↔RTL 交替 12 顆（模擬撞號多來源）
    for i in range(12):
        await rx._handle(heartbeat(RTL if i % 2 == 0 else HOLD), addr)
    flap_events = sum(1 for e in inserts[base:] if e["type"] == "mode_change")

    # 穩定換模式：MISSION 連 2 顆 → 應恰噴 1 筆
    mark = len(inserts)
    await rx._handle(heartbeat(MISSION), addr)
    await rx._handle(heartbeat(MISSION), addr)
    stable_events = sum(1 for e in inserts[mark:] if e["type"] == "mode_change")

    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")

    check("翻打 12 顆 → mode_change 不噴（防抖擋住）", flap_events == 0,
          f"(got {flap_events})")
    check("穩定換 MISSION → 恰噴 1 筆", stable_events == 1, f"(got {stable_events})")
    print("RESULT:", "ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


asyncio.run(main())
