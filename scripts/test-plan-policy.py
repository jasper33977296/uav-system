#!/usr/bin/env python3
"""高度政策與例外的回歸（doc/route-planning-redesign.md §4、§5）。

    DEM_DIR=data/dem BUILDINGS_DIR=data/buildings python3 scripts/test-plan-policy.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import plan_check as pc  # noqa: E402
import terrain  # noqa: E402

fails = []


def ck(name, cond, got=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  ← {got}" if not cond else ""))
    if not cond:
        fails.append(name)


dem = terrain.shared()
HOME = {"lat": 24.7734787, "lon": 121.045971}
# 起飛點 123 m；往南 400 m 地面明顯不同，才看得出「逐點算」有沒有真的逐點
PTS = [{"lat": 24.7734787, "lon": 121.045971},
       {"lat": 24.7710, "lon": 121.0480},
       {"lat": 24.7700, "lon": 121.0505}]

print("── 預設政策 ──")
d = pc.default_policy()
ck("預設是離地面", d["mode"] == pc.POLICY_AGL, d["mode"])
ck("預設高度 3 m", d["height_m"] == 3.0, d["height_m"])
ck("預設高度剛好等於 LOW_ALT_M（刻意，見 §11）",
   pc.DEFAULT_POLICY_HEIGHT_M == pc.LOW_ALT_M)

print("\n── agl：逐點的 alt 應該各不相同 ──")
b = pc.build_plan(PTS, None, HOME, dem=dem)
wps = [w for w in b["waypoints"] if w["action"] == "waypoint"]
mine = [w for w in wps if not w.get("filled")]
ck("我放的三個點都在（其餘是系統補的中繼點）", len(mine) == 3,
   f"{len(mine)} 個我放的／{len(wps)} 個總共")
ck("有補中繼點——逐點貼地不等於整段貼地",
   len(wps) > len(mine), f"{len(wps)} vs {len(mine)}")
ck("全部 frame 3（飛控不必有地形圖庫）", all(w["frame"] == 3 for w in wps),
   [w["frame"] for w in wps])
alts = [w["alt"] for w in mine]
ck("alt 逐點不同（真的有用 DEM 算）", len(set(alts)) > 1, alts)
ha = terrain.surface(HOME["lat"], HOME["lon"], dem).ground
for w in mine:
    g = terrain.surface(w["lat"], w["lon"], dem).ground
    agl = ha + w["alt"] - g
    ck(f"  ({w['lat']:.4f}) 實際離地 {agl:.2f} m ≈ 3", abs(agl - 3.0) < 0.02, agl)

print("\n── home：所有點同一個數字（對照組）──")
b2 = pc.build_plan(PTS, {"mode": pc.POLICY_HOME, "height_m": 3.0}, HOME, dem=dem)
w2 = [w for w in b2["waypoints"] if w["action"] == "waypoint"]  # home 不補點
ck("alt 全部一樣", len({w["alt"] for w in w2}) == 1, [w["alt"] for w in w2])
ck("而且離地各不相同——這正是 09-07 的坑",
   len({round(ha + w["alt"] - terrain.surface(w["lat"], w["lon"], dem).ground, 1)
        for w in w2}) > 1)

print("\n── amsl ──")
b3 = pc.build_plan(PTS, {"mode": pc.POLICY_AMSL, "height_m": 150.0}, HOME, dem=dem)
w3 = [w for w in b3["waypoints"] if w["action"] == "waypoint"]
ck("frame 0、alt 就是那個海拔",
   all(w["frame"] == 0 and w["alt"] == 150.0 for w in w3),
   [(w["frame"], w["alt"]) for w in w3])

print("\n── 例外：改政策不動 manual 的點（§4）──")
pts_ex = [dict(PTS[0]), {**PTS[1], "h": 1.5, "alt_source": pc.ALT_FROM_MANUAL},
          dict(PTS[2])]
b4 = pc.build_plan(pts_ex, {"mode": pc.POLICY_AGL, "height_m": 3.0}, HOME, dem=dem)
b5 = pc.build_plan(pts_ex, {"mode": pc.POLICY_AGL, "height_m": 8.0}, HOME, dem=dem)
def at(built, lat):
    """按座標找我放的那個點——中繼點會讓索引跑掉。"""
    return next(w for w in built["waypoints"]
                if w["action"] == "waypoint" and not w.get("filled")
                and abs(w["lat"] - lat) < 1e-6)

e4, e5 = at(b4, PTS[1]["lat"]), at(b5, PTS[1]["lat"])
p4, p5 = at(b4, PTS[0]["lat"]), at(b5, PTS[0]["lat"])
ck("例外的那個點 h 沒被動到", e4["h"] == 1.5 and e5["h"] == 1.5,
   (e4["h"], e5["h"]))
ck("例外的 alt 兩份一樣", abs(e4["alt"] - e5["alt"]) < 0.001,
   (e4["alt"], e5["alt"]))
ck("跟著政策的點有跟著變", abs(p5["alt"] - p4["alt"] - 5.0) < 0.01,
   (p4["alt"], p5["alt"]))
ck("alt_source 留在航點上", e4["alt_source"] == pc.ALT_FROM_MANUAL)
ck("跟著政策的標成 policy", p4["alt_source"] == pc.ALT_FROM_POLICY)
ck("中繼點標得出來（不是操作員放的）",
   all(w.get("alt_source") == pc.ALT_FROM_POLICY
       for w in b4["waypoints"] if w.get("filled")))

print("\n── 沒有 DEM：退回離起飛點，而且要說出來 ──")
b6 = pc.build_plan(PTS, None, HOME, dem=None)
ck("退回時 alt 就是政策的數字",
   all(w["alt"] == 3.0 for w in b6["waypoints"] if w["action"] == "waypoint"))
ck("decisions 裡有「補了中繼航點」",
   any("中繼" in d["what"] for d in b["decisions"]),
   [d["what"] for d in b["decisions"]])
ck("decisions 裡有「退回」這件事",
   any("退回" in d["what"] or "退回" in d["value"] for d in b6["decisions"]),
   [d["what"] for d in b6["decisions"]])

print("\n── decisions：系統替你決定了什麼（§3 動作 3）──")
ws = {d["what"] for d in b["decisions"]}
for need in ("起飛高度", "改速度項的位置", "降落地點", "降落方式", "高度基準"):
    ck(f"有「{need}」", need in ws, sorted(ws))
ck("每一條都說得出為什麼", all(d["why"] for d in b["decisions"]))
sp = next(d for d in b["decisions"] if d["what"] == "改速度項的位置")
ck("改速度項在第一個航點之前", "之前" in sp["value"], sp["value"])

print("\n── 產出仍然是一份飛得起來的航線 ──")
ck("第一項是起飛", b["waypoints"][0]["action"] == "takeoff")
ck("第二項是改速度", b["waypoints"][1]["command"] == 178)
ck("最後一項是降落", b["waypoints"][-1]["action"] == "land")
chk = pc.check_waypoints(b["waypoints"], 1000, 120,
                         home=[HOME["lat"], HOME["lon"]], dem=dem, wp_spd=1.0)
ck("預設政策產生的航線不會穿地",
   not any("撞地" in p for p in chk["problems"]),
   [p[:80] for p in chk["problems"]])

print()
if fails:
    print(f"✗ {len(fails)} 項沒過：" + "、".join(fails))
    sys.exit(1)
print("全部通過")
