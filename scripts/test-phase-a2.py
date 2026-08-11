#!/usr/bin/env python3
"""Phase A.2 單元驗證：直接驅動 _vehicle_event（issue 014）。

繞開網路/解析器/註冊，只測產線處理邏輯：EVENT(410) 裸 frame → 解碼 →
severity 對映 → 折疊(count) → protocol(sev=8) 丟棄。stub 掉 db 與 broadcast，
斷言呼叫序列。跑在 backend 容器內：python3 test-phase-a2.py
"""
import asyncio
import struct
import sys

sys.path.insert(0, "/srv")
from pymavlink import mavutil
from pymavlink.mavutil import x25crc

from app import db, mavlink_rx
from app.state import LiveState
from app.ws import manager

M = mavutil.mavlink
inserts, bumps, broadcasts = [], [], []


async def fake_insert(drone_id, session_id, severity, type_, detail, source="system"):
    row = {"id": len(inserts) + 1, "time": "T", "severity": severity,
           "type": type_, "detail": detail, "source": source}
    inserts.append(row)
    return dict(row)


async def fake_bump(event_id, detail):
    bumps.append((event_id, detail))
    return {"id": event_id, "time": "T2", "detail": detail}


async def fake_broadcast(msg):
    broadcasts.append(msg)


def event_frame(event_id, sev_ext, args=b"", seq=0):
    payload = struct.pack("<II", event_id, 0) + struct.pack("<H", 0)
    payload += bytes([0, 0, sev_ext & 0x0F]) + (args + b"\x00" * 40)[:40]
    p = payload.rstrip(b"\x00") or b"\x00"
    core = bytes([len(p), 0, 0, seq, 1, 1, 0x9A, 0x01, 0x00]) + p
    c = x25crc(core); c.accumulate([160])
    return bytes([0xFD]) + core + bytes([c.crc & 0xFF, (c.crc >> 8) & 0xFF])


def parse(frame):
    p = M.MAVLink(None); p.robust_parsing = True
    return (p.parse_buffer(frame) or [])[0]


async def main():
    db.insert_event = fake_insert
    db.bump_event = fake_bump
    manager.broadcast = fake_broadcast

    rx = mavlink_rx.MavlinkRx()
    st = LiveState(drone_id="d1", drone_name="sim-uav-1")
    ent = {"state": st}

    # E1: id=1001 sev=4(warning) x3 → 1 insert + 2 bump（fold count 2,3）
    for _ in range(3):
        await rx._vehicle_event(ent, st, parse(event_frame(1001, 4, b"\x0a\x0b")))
    # E2: id=2002 sev=6(info) x1 → 新 insert
    await rx._vehicle_event(ent, st, parse(event_frame(2002, 6)))
    # E3: id=3003 sev=8(protocol) → 丟棄
    await rx._vehicle_event(ent, st, parse(event_frame(3003, 8, b"\xde\xad")))

    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    check("2 inserts (E1 首筆 + E2)", len(inserts) == 2)
    check("E1 insert severity=warning source=vehicle count=1",
          inserts[0]["severity"] == "warning" and inserts[0]["source"] == "vehicle"
          and inserts[0]["detail"]["count"] == 1 and inserts[0]["detail"]["event_id"] == 1001)
    check("E1 折疊 2 次 bump 到 count=3",
          len(bumps) == 2 and bumps[-1][1]["count"] == 3)
    check("E2 insert severity=info event_id=2002",
          inserts[1]["severity"] == "info" and inserts[1]["detail"]["event_id"] == 2002)
    check("E3 protocol(sev=8) 丟棄（無第 3 筆 insert）",
          all(i["detail"]["event_id"] != 3003 for i in inserts))
    check("廣播 fold 標記：折疊重播帶 fold=True",
          sum(1 for b in broadcasts if b.get("fold")) == 2)
    check("args 帶進 detail（供後續 metadata 升級）",
          inserts[0]["detail"].get("args") == "0a0b")
    print("RESULT:", "ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


asyncio.run(main())
