#!/usr/bin/env python3
"""sysid 撞號時，新來的機不得繼承舊記錄（issues/038 的比對半邊）。

**2026-09-01 實際發生**：PX4 SITL 用 sysid 1 連上，被認領進一台 ArduPilot
真機的記錄，PX4 的 `vehicle_event` 寫進了那台真機的事件流——而全程只有一行
`log.info`。事後查出**同樣的事 08-24 也發生過一次**，總共 46 筆假事件躺在
真機的記錄裡沒有人發現。遙測沒被污染純粹是因為 issues/004 的修法
（未 armed 不入庫），不是因為有人擋住。

兩個訊號強度不同，處置也不同——本測試把這個差別釘住：
  * **廠牌不合 → 硬擋**（一台機不會重開機之後從 ArduPilot 變成 PX4）
  * **board_uid 不合 → 只示警、不擋、但也不覆蓋**（uid2 跨韌體升級的穩定性
    還沒實測，硬擋會製造假警報；而覆蓋會把唯一的期望值抹掉）

不需要飛機也不需要 SITL：直接對守門餵狀態。

跑法（**要在 backend 容器內**，asyncpg 只裝在那裡）：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-identity-guard.py
"""
import asyncio
import sys

from app import db, mavlink_rx                    # noqa: E402
from app.state import LiveState                   # noqa: E402

ok = True
events = []


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


async def fake_insert_event(drone_id, session_id, severity, typ, detail):
    events.append((severity, typ, detail))
    return {"id": 1, "type": typ, "severity": severity, "detail": detail}


class _NoBroadcast:
    async def broadcast(self, _msg):
        pass


async def main():
    db.insert_event = fake_insert_event
    mavlink_rx.manager = _NoBroadcast()
    rx = mavlink_rx.MavlinkRx.__new__(mavlink_rx.MavlinkRx)

    print("── 1. 廠牌相符：照常記錄 ──────────────────────────────")
    st = LiveState(drone_id="d1", drone_name="真機")
    st.sysid, st.expect_autopilot = 1, 3          # 記錄是 ArduPilot
    r = await rx._identity_guard(st, autopilot=3)
    chk("同一家 → 放行", r is True and st.identity_ok and not events)

    print("\n── 2. 廠牌不合：硬擋，而且資料停止記入 ────────────────")
    st2 = LiveState(drone_id="d1", drone_name="真機")
    st2.sysid, st2.expect_autopilot = 1, 3
    r = await rx._identity_guard(st2, autopilot=12)   # PX4 接進 ArduPilot 的記錄
    chk("不同家 → 擋下", r is False)
    chk("**identity_ok 轉成 False**（資料不再記在這筆記錄名下）",
        st2.identity_ok is False)
    chk("事件是 critical 且說得出兩邊各是誰",
        events and events[-1][0] == "critical"
        and events[-1][1] == "identity_mismatch"
        and "ardupilot" in events[-1][2]["reason"]
        and "px4" in events[-1][2]["reason"],
        events[-1][2]["reason"] if events else "")

    n = len(events)
    await rx._identity_guard(st2, autopilot=12)
    chk("**只在轉態那一次報**（否則每拍一則會淹掉事件流）", len(events) == n)

    print("\n── 3. 第一次認得：記下來當期望值，不當成不符 ──────────")
    calls = []

    async def fake_set_autopilot(did, raw):
        calls.append((did, raw))

    db.set_autopilot = fake_set_autopilot
    st3 = LiveState(drone_id="d2", drone_name="新機")
    st3.sysid = 5
    r = await rx._identity_guard(st3, autopilot=12)
    chk("期望值是 NULL → 放行並學起來",
        r is True and st3.expect_autopilot == 12 and calls == [("d2", 12)])

    print("\n── 4. board_uid 不合：示警、不擋、**不覆蓋** ───────────")
    st4 = LiveState(drone_id="d1", drone_name="真機")
    st4.sysid, st4.expect_board_uid = 1, "2a00aaaa"
    before = len(events)
    r = await rx._identity_guard(st4, board_uid="ffff0000")
    chk("回 False → 呼叫端因此不會覆蓋 DB 的期望值", r is False)
    chk("**但沒有硬擋**（identity_ok 仍為 True）", st4.identity_ok is True)
    chk("事件是 warn 不是 critical（強度與訊號可信度相稱）",
        len(events) == before + 1 and events[-1][0] == "warn"
        and events[-1][1] == "board_uid_changed", events[-1][:2])

    print("\n── 5. 反向驗證：uid 相同不該報 ────────────────────────")
    before = len(events)
    st5 = LiveState(drone_id="d1")
    st5.sysid, st5.expect_board_uid = 1, "2a00aaaa"
    r = await rx._identity_guard(st5, board_uid="2a00aaaa")
    chk("uid 一致 → 放行且無事件", r is True and len(events) == before)

    print("\n" + ("全部通過" if ok else "**有未通過項目**"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
