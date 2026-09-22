#!/usr/bin/env python3
"""航點停留秒數（issues/062）：規劃端寫得出來、存得進去、看得出來。

在後端容器裡跑（要 app 套件與 DEM）：
    docker cp scripts/test-waypoint-hold.py uav-backend:/tmp/ && \\
    docker exec -w /srv uav-backend python /tmp/test-waypoint-hold.py
"""
import sys

sys.path.insert(0, "/srv")
sys.path.insert(0, "/srv/libs")
import mission_time  # noqa: E402
import plan_check  # noqa: E402
import terrain  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.api import PlanOverride, _apply_overrides  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


HOME = {"lat": 24.7814, "lon": 121.0947}
PTS = [{"lat": 24.7816, "lon": 121.0947, "kind": "wp"},
       {"lat": 24.7818, "lon": 121.0950, "kind": "wp", "hold_s": 30},
       {"lat": 24.7820, "lon": 121.0947, "kind": "wp"}]
dem = terrain.shared()

print("── 從零畫的航線 ─────────────────────────────────────────")
b = plan_check.build_plan(PTS, None, HOME, dem=dem)
wps = b["waypoints"]
nav = [w for w in wps if w.get("command") == 16]
held = [w for w in nav if w.get("p1")]
chk("停留寫進那個航點的 param1", len(held) == 1 and held[0]["p1"] == 30.0,
    [(w.get("src_i"), w.get("p1")) for w in nav])
chk("而且是操作員指定的那一點", held and held[0].get("src_i") == 1)
chk("其他航點沒有停留", all(not w.get("p1") for w in nav if w.get("src_i") != 1))
chk("起飛項的 param1 沒被動到", all(w.get("p1") != 30.0 for w in wps if w.get("command") == 22))
chk("hold_of 讀得回來", plan_check.hold_of(held[0]) == 30.0 if held else False)

print("\n── 飛行時間估算算得進去 ─────────────────────────────────")
t0 = mission_time.estimate(plan_check.build_plan(
    [dict(p, hold_s=None) for p in PTS], None, HOME, dem=dem)["waypoints"], 5, 2,
    [HOME["lat"], HOME["lon"]])
t1 = mission_time.estimate(wps, 5, 2, [HOME["lat"], HOME["lon"]])
chk("多停 30 秒＝估算多 30 秒", t0["seconds"] is not None
    and abs(t1["seconds"] - t0["seconds"] - 30) < 0.5, (t0["seconds"], t1["seconds"]))
chk("而且說出來", any("停留" in a for a in t1["assumptions"]), t1["assumptions"])

print("\n── 剖面看得出來 ─────────────────────────────────────────")
prof = plan_check.route_profile(wps, HOME, dem=dem)
if not prof["points"]:
    print("skip：沒有 DEM，剖面是空的。**skip ≠ pass**")
    ok = False
else:
    hp = [p for p in prof["points"] if p.get("hold_s")]
    chk("剖面上那一點帶 hold_s", len(hp) == 1 and hp[0]["hold_s"] == 30.0,
        [(p.get("seq"), p.get("hold_s")) for p in hp])
    chk("中間的取樣點不帶（只有航點本身）", all(p.get("seq") is not None for p in hp))

print("\n── 既有航線的逐點覆寫 ───────────────────────────────────")
old = [{"seq": 0, "command": 22, "p1": 0}, {"seq": 1, "command": 16, "lat": 1, "lon": 1},
       {"seq": 2, "command": 16, "lat": 2, "lon": 2, "p1": 12.0}, {"seq": 3, "command": 21}]
out = _apply_overrides(old, [PlanOverride(seq=1, hold=20)])
w1 = [w for w in out if w["seq"] == 1][0]
chk("覆寫寫進 p1", w1["p1"] == 20.0 and w1["params"]["p1"] == 20.0)
chk("匯入時就有的停留沒被動到", [w for w in out if w["seq"] == 2][0]["p1"] == 12.0)
out = _apply_overrides(old, [PlanOverride(seq=2, hold=0)])
chk("設成 0＝取消停留", [w for w in out if w["seq"] == 2][0]["p1"] == 0.0)
for seq, what in ((0, "起飛"), (3, "降落")):
    try:
        _apply_overrides(old, [PlanOverride(seq=seq, hold=5)])
        chk(f"{what}項設停留要被擋", False)
    except HTTPException as e:
        chk(f"{what}項設停留要被擋（param1 意思不同）", e.status_code == 422, e.detail)
try:
    PlanOverride(seq=1, hold=99999)
    chk("超過上限要被擋", False)
except Exception:
    chk("超過上限要被擋", True, f"上限 {plan_check.HOLD_MAX_S:.0f} s")
try:
    PlanOverride(seq=1, hold=-1)
    chk("負數要被擋", False)
except Exception:
    chk("負數要被擋", True)

print("\n" + ("✓ 全部通過" if ok else "✗ 有失敗"))
sys.exit(0 if ok else 1)
