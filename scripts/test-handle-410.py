#!/usr/bin/env python3
"""驗證 _handle 的 sysid 補回修正（issue 014 Phase A.2 bug）。

pymavlink 對未知 msgid 不填 header → get_srcSystem()=0 → _handle 的
「if not sysid: return」把 EVENT 410 全吞掉（真機 0 筆的元凶）。此測試餵一顆
**真機 tlog 的 410 frame**（sysid 1、get_srcSystem()=0）進完整 _handle，斷言
它走到 vehicle_event insert。跑在 backend 容器內：python3 test-handle-410.py
"""
import asyncio
import sys
import time

sys.path.insert(0, "/srv")
from pymavlink import mavutil

from app import db, mavlink_rx
from app.state import LiveState
from app.ws import manager

M = mavutil.mavlink
inserts, broadcasts = [], []


async def fake_insert(drone_id, session_id, severity, type_, detail, source="system"):
    row = {"id": len(inserts) + 1, "time": "T", "severity": severity,
           "type": type_, "detail": detail, "source": source}
    inserts.append(row)
    return dict(row)


async def fake_broadcast(msg):
    broadcasts.append(msg)


async def main():
    db.insert_event = fake_insert
    manager.broadcast = fake_broadcast

    rx = mavlink_rx.MavlinkRx()
    st = LiveState(drone_id="d1", drone_name="sim-uav-1")
    addr = ("127.0.0.1", 14550)
    # 預先註冊 sysid 1（模擬心跳已建檔）
    rx.sysids[1] = {"drone_id": "d1", "state": st, "addr": addr,
                    "seen": time.monotonic()}

    # 真機 tlog 的 EVENT(410) frame（sysid 1、sev_ext=6 info）
    real = bytes.fromhex(
        "fd1400000201019a0100ef5b1201f0020000f6ff000066ea07080b032520b1fc")
    p = M.MAVLink(None); p.robust_parsing = True
    msg = (p.parse_buffer(real) or [])[0]

    print("  frame type:", msg.get_type(), "| get_srcSystem():", msg.get_srcSystem(),
          "(0 = 未填，正是 bug 觸發點)")
    await rx._handle(msg, addr)

    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    check("_handle 走到 vehicle_event insert（修正前這裡是 0）", len(inserts) == 1)
    if inserts:
        ev = inserts[0]
        check("type=vehicle_event source=vehicle", ev["type"] == "vehicle_event"
              and ev["source"] == "vehicle")
        check("severity=info（log_levels 外層=6）", ev["severity"] == "info")
        check("detail 帶 event_id", "event_id" in ev["detail"])
        check("有廣播出去（前端 WS 收得到）", len(broadcasts) == 1)
    print("RESULT:", "ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


asyncio.run(main())
