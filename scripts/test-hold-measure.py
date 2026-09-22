#!/usr/bin/env python3
"""停留是量出來的（issues/062）：`holds.measure` 對合成資料與 09-21 實飛。

在後端容器裡跑：
    docker cp scripts/test-hold-measure.py uav-backend:/tmp/ && \\
    docker exec -w /srv uav-backend python /tmp/test-hold-measure.py
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/srv")
from app import holds as H  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


T = datetime(2026, 9, 21, 7, 45, tzinfo=timezone.utc)
S = lambda x: T + timedelta(seconds=x)  # noqa: E731
# 航線形狀照 260921-straight-hold-v2：起飛、改速、停 10、改速、停 20、改速、停 10、改速、降落
ITEMS = [{"seq": 0, "command": 22}, {"seq": 1, "command": 178},
         {"seq": 2, "command": 16, "p1": 10}, {"seq": 3, "command": 178},
         {"seq": 4, "command": 16, "p1": 20}, {"seq": 5, "command": 178},
         {"seq": 6, "command": 16, "p1": 10}, {"seq": 7, "command": 178},
         {"seq": 8, "command": 21}]


def speeds(stops):
    """每秒一筆；stops 內的秒數地速 0.05，其餘 1.0"""
    return [(S(i), 0.05 if any(a <= i <= b for a, b in stops) else 1.0) for i in range(0, 120)]


print("── 是哪一點 ─────────────────────────────────────────────")
chk("事件報的是停留點**後面那一項**（ArduCopter 實測）→ 對到停留點",
    H.nav_for_event(ITEMS, 1, 4) == 2, "wire 4＝我方 3（改速）→ 最後一個航點是 2")
chk("事件報的是航點本身 → 也對得上", H.nav_for_event(ITEMS, 1, 3) == 2)
chk("PX4（不偏移）", H.nav_for_event(ITEMS, 0, 4) == 4)
chk("起飛之後那一則不對到任何航點", H.nav_for_event(ITEMS, 1, 2) is None)

print("\n── 是哪一段時間 ─────────────────────────────────────────")
sp = speeds([(20, 30), (40, 60)])
m = H.measure(ITEMS, 1, [(S(30.9), 4), (S(60.8), 6)], sp)
got = {h["seq"]: h for h in m}
chk("**事件在停留結束時**（ArduCopter）→ 往前找到整段",
    got[2]["observed"] and got[2]["started_at"] == S(20) and got[2]["ended_at"] == S(30),
    (got[2]["started_at"], got[2]["ended_at"]))
chk("量到的秒數", got[2]["seconds"] == 10.0 and got[4]["seconds"] == 20.0)
m = H.measure(ITEMS, 1, [(S(20.2), 3)], speeds([(20, 30)]))
chk("**事件在停留開始時**（另一種韌體語意）→ 往後找到整段",
    m[0]["observed"] and m[0]["ended_at"] == S(30) and m[0]["started_at"] == S(20))
m = H.measure(ITEMS, 1, [(S(50), 4)], speeds([]))
h2 = [h for h in m if h["seq"] == 2][0]
chk("有事件但沒停下來 → 照實說看不出有停", not h2["observed"] and "看不出有停" in h2["note"])
chk("沒收到事件的點 → 說沒收到", any(not h["observed"] and "沒有收到" in h["note"]
                                   for h in m if h["seq"] == 6))
m = H.measure(ITEMS, None, [(S(30), 4)], sp)
chk("**不認得自駕儀 → 不猜偏移**", all(not h["observed"] and "不認得" in h["note"] for h in m))
m = H.measure(ITEMS, 1, [(S(30.9), 4)], speeds([(27, 30)]))
chk("停得比規劃短一半以上要說", m[0]["observed"] and "不到規劃的一半" in (m[0]["note"] or ""))
chk("沒有規劃停留的航線 → 什麼都不回", H.measure([{"seq": 0, "command": 16}], 1, [], sp) == [])


async def real():
    from app import db, ext_history as X
    await db.init_pool()
    mid = await db.pool.fetchval(
        "SELECT mission_id::text FROM flight_sessions WHERE id = 'b5d169f3-7e59-40b7-aea4-7e20ad7e4a9b'")
    out = await X.mission_signal(mid)
    ses = [s for d in out["drones"] for s in d["sessions"]
           if s["session_id"] == "b5d169f3-7e59-40b7-aea4-7e20ad7e4a9b"][0]
    hs = {h["seq"]: h for h in ses["holds"]}
    print(f"\n── 09-21 實飛（{ses['plan_name']}）─────────────────────")
    for s, h in sorted(hs.items()):
        print(f"   seq {s}：規劃 {h['planned_s']:g} s → 量到 {h['seconds']} s，"
              f"{h['started_at'][11:19]}～{(h['ended_at'] or '')[11:19]}，{h['samples']} 筆")
    chk("三個停留點都量到", sorted(hs) == [2, 4, 6] and all(h["observed"] for h in hs.values()))
    chk("量到的秒數與規劃差不到 3 秒",
        all(abs(h["seconds"] - h["planned_s"]) < 3 for h in hs.values()),
        {s: h["seconds"] for s, h in hs.items()})
    n = sum(1 for x in ses["samples"] if x["hold_seq"] is not None)
    chk("樣本上的 hold_seq 與 holds 的筆數一致", n == sum(h["samples"] for h in hs.values()), n)
    chk("停留期間約每秒一筆", all(h["samples"] >= h["seconds"] * 0.7 for h in hs.values()),
        {s: h["samples"] for s, h in hs.items()})
    chk("method 說得出門檻", out["method"]["hold_stop_ms"] == H.STOP_MS)


asyncio.run(real())
print("\n" + ("✓ 全部通過" if ok else "✗ 有失敗"))
sys.exit(0 if ok else 1)
