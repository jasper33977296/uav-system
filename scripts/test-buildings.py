#!/usr/bin/env python3
"""建物層的回歸（doc/field-3d-model-design.md §7-3、§9-A/C）。

    DEM_DIR=data/dem python3 scripts/test-buildings.py

要有 `data/buildings/*.geojson`（`scripts/fetch-buildings.py` 先抓）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import buildings  # noqa: E402
import plan_check  # noqa: E402
import terrain  # noqa: E402

fails = []


def ck(name, cond, got=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  ← {got}" if not cond else ""))
    if not cond:
        fails.append(name)


def poly(ring, **props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Polygon",
                         "coordinates": [[[lo, la] for la, lo in ring]
                                         + [[ring[0][1], ring[0][0]]]]}}


print("── 三層高度退讓（§9-A）──")
ck("height 直接用", buildings.resolve_height({"height": "12.5"}) == (12.5, "osm:height", None))
ck("height 帶單位", buildings.resolve_height({"height": "9 m"})[0] == 9.0)
h, src, lv = buildings.resolve_height({"building:levels": "12"})
ck("樓層往上取", (h, src, lv) == (42, "osm:levels", 12), f"{h} {src} {lv}")
ck("兩個都沒有＝unknown，且不給數字",
   buildings.resolve_height({"building": "yes"}) == (None, "unknown", None))
ck("height 壞掉就退到樓層",
   buildings.resolve_height({"height": "約十層", "building:levels": "2"})[1] == "osm:levels")

print("\n── 輪廓（§9-C）──")
sq = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
ck("正常方形留下", buildings._ring_ok(sq))
ck("點數不足丟掉", not buildings._ring_ok([(0, 0), (0, 1), (1, 1)]))
ck("自交丟掉（蝴蝶結）", not buildings._ring_ok([(0, 0), (1, 1), (0, 1), (1, 0)]))

print("\n── 空間查詢與未知語意 ──")
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "t.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": [
            poly(sq, id="way/1", name="有量過的", kind="commercial",
                 height_m=20.0, height_source="osm:levels"),
            poly([(0.0, 2.0), (0.0, 3.0), (1.0, 3.0), (1.0, 2.0)],
                 id="way/2", name="沒量過的", kind="yes",
                 height_m=None, height_source="unknown"),
        ]}, f)
    st = buildings.Store(d)
    ck("讀進兩棟", len(st.items) == 2, len(st.items))
    ck("點在裡面找得到", (st.at(0.5, 0.5) or {}) and st.at(0.5, 0.5).name == "有量過的")
    ck("點在外面是 None", st.at(0.5, 1.5) is None)
    b2 = st.at(0.5, 2.5)
    ck("未知高度的樓仍然找得到", b2 is not None and b2.name == "沒量過的")
    ck("未知高度不是一個數字", b2 is not None and b2.height_m is None and not b2.known)

print("\n── surface()：屋頂 vs 地面 ──")
store = buildings.shared()
if not store.available:
    print("  data/buildings/ 是空的——先跑 scripts/fetch-buildings.py")
    sys.exit(1)
dem = terrain.shared()
if not dem.available:
    print("  沒有 DEM（DEM_DIR=data/dem）")
    sys.exit(1)

known = [b for b in store.items if b.known]
unknown = [b for b in store.items if not b.known]
ck("場域有量得出高度的樓", len(known) > 0, len(known))
ck("場域有高度未知的樓", len(unknown) > 0, len(unknown))


def centre(b):
    return (sum(p[0] for p in b.ring) / len(b.ring),
            sum(p[1] for p in b.ring) / len(b.ring))


la, lo = centre(known[0])
s = terrain.surface(la, lo, dem)
ck("有高度：top 高過 ground", s.top is not None and s.ground is not None
   and abs((s.top - s.ground) - known[0].height_m) < 0.01, f"{s}")
ck("有高度：出處跟著出來", s.source == known[0].height_source and s.kind == "building",
   f"{s.source}/{s.kind}")
ck("有高度：水平解析度是輪廓級不是 SRTM 級", s.horiz_res_m < 30.0, s.horiz_res_m)

la, lo = centre(unknown[0])
s = terrain.surface(la, lo, dem)
ck("未知高度：ground 照樣有", s.ground is not None, f"{s}")
ck("未知高度：top 是 None（不是地面高度）", s.top is None, f"{s.top}")
ck("未知高度：kind 說得出是建物", s.kind == "building", s.kind)

print("\n── check_terrain：不猜、不放行 ──")
b = max(known, key=lambda x: x.height_m)
bl, bo = centre(b)
home = {"lat": bl + 0.004, "lon": bo}
gnd = terrain.surface(bl, bo, dem).ground
wps = [
    {"seq": 0, "cmd": 22, "frame": 3, "lat": home["lat"], "lon": home["lon"], "alt": 10},
    {"seq": 1, "cmd": 16, "frame": 3, "lat": bl, "lon": bo, "alt": 10},
]
r = plan_check.check_terrain(wps, home=home, dem=dem)
hit = [p for p in r["problems"] + r["warnings"] if "撞" in p or "離地" in p]
ck(f"10 m 飛過 {b.name}（{b.height_m:.0f} m）要被擋下", bool(r["problems"]),
   json.dumps(r["problems"] + r["warnings"], ensure_ascii=False)[:200])

u = unknown[0]
ul, uo = centre(u)
home2 = {"lat": ul + 0.004, "lon": uo}
wps2 = [
    {"seq": 0, "cmd": 22, "frame": 3, "lat": home2["lat"], "lon": home2["lon"], "alt": 30},
    {"seq": 1, "cmd": 16, "frame": 3, "lat": ul, "lon": uo, "alt": 30},
]
r2 = plan_check.check_terrain(wps2, home=home2, dem=dem)
blind = [p for p in r2["problems"] if "沒有量過" in p]
ck("飛過高度未知的樓 → 擋下並說是哪一棟", bool(blind),
   json.dumps(r2["problems"], ensure_ascii=False)[:200])
ck("未知的那些樓列得出來", bool(r2.get("terrain_blind")), r2.get("terrain_blind"))

print("\n── route_profile：第三條線 ──")
pr = plan_check.route_profile(wps2, home=home2, dem=dem)
pts = pr["points"]
ck("剖面有點", len(pts) > 1, len(pts))
ck("每一點都有 ground 與 top 兩個欄位",
   all("ground" in p and "top" in p for p in pts))
col = [p for p in pts if p.get("obst") == "unknown"]
ck("經過未知建物的點標成 unknown（開口向上的柱子）", bool(col), len(col))
ck("那些點的 top 是 None", all(p["top"] is None for p in col))
ck("那些點說得出是哪一棟", all(p.get("obst_name") for p in col))

pr1 = plan_check.route_profile(wps, home=home, dem=dem)
roof = [p for p in pr1["points"] if p.get("obst") == "building"]
ck("經過有高度的樓：top 高過 ground",
   bool(roof) and all(p["top"] > p["ground"] for p in roof), len(roof))

print("\n── 假設高度（使用者可調的旋鈕）──")
ck("不給 assume_m 時 top 仍然是 None",
   terrain.surface(ul, uo, dem).top is None)
s9 = terrain.surface(ul, uo, dem, assume_m=9)
ck("給了就有 top", s9.top is not None and abs(s9.top - (s9.ground + 9)) < 0.01, f"{s9}")
ck("出處變成 assumed（分得出估的與量的）", s9.source == "assumed", s9.source)
ck("改數字 top 跟著變",
   abs(terrain.surface(ul, uo, dem, assume_m=20).top - (s9.top + 11)) < 0.01)
ck("有量過的樓不受旋鈕影響",
   terrain.surface(*centre(known[0]), dem, assume_m=99).source == known[0].height_source)

r3 = plan_check.check_terrain(wps2, home=home2, dem=dem, assume_m=9)
ck("有假設值時不再是 problem",
   not any("沒有量過" in p for p in r3["problems"]),
   json.dumps(r3["problems"], ensure_ascii=False)[:160])
ck("但一定有一句 warning 說是用假設值算的",
   any("假設高度 9 m" in w for w in r3["warnings"]),
   json.dumps(r3["warnings"], ensure_ascii=False)[:160])
ck("報告帶著 assumed_m 出去", r3.get("assumed_m") == 9, r3.get("assumed_m"))

# 高得一定會撞：撞的那句要說「照假設高度算」，而不是「這一段會撞地」
r4 = plan_check.check_terrain(wps2, home=home2, dem=dem, assume_m=60)
ck("估出來的撞是 warning 不是 problem",
   any("照假設高度算" in w for w in r4["warnings"]) and not r4["problems"],
   json.dumps(r4["problems"] + r4["warnings"], ensure_ascii=False)[:200])

pr9 = plan_check.route_profile(wps2, home=home2, dem=dem, assume_m=9)
asum = [x for x in pr9["points"] if x.get("obst") == "assumed"]
ck("剖面把假設的標成 assumed（畫面要畫成虛線，不是實心）", bool(asum), len(asum))
ck("假設的點 top 有數字", all(x["top"] is not None for x in asum))

lp = plan_check.check_waypoints(
    wps2, 1000, 120, home=[home2["lat"], home2["lon"]], dem=dem, assume_m=9)
ck("check_waypoints 也把 assumed_m 與名單帶出來",
   lp.get("assumed_m") == 9 and bool(lp.get("terrain_blind")),
   f"{lp.get('assumed_m')} {lp.get('terrain_blind')}")
ck("limits 帶著旋鈕的預設值（前端不抄第二份）",
   lp["limits"].get("assumed_default_m") == buildings.ASSUMED_DEFAULT_M,
   lp["limits"].get("assumed_default_m"))

print()
if fails:
    print(f"✗ {len(fails)} 項沒過：" + "、".join(fails))
    sys.exit(1)
print("全部通過")
