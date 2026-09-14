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
# 使用者裁定 2026-09-09：畫線時多一條飛回原點的線，看起來像自己畫錯了
ck("預設不回起飛點", d["land_at_home"] is False, d["land_at_home"])
ck("預設高度 3 m", d["height_m"] == 3.0, d["height_m"])
ck("預設高度剛好等於 LOW_ALT_M（刻意，見 §11）",
   pc.DEFAULT_POLICY_HEIGHT_M == pc.LOW_ALT_M)

print("\n── agl：逐點的 alt 應該各不相同 ──")
b = pc.build_plan(PTS, None, HOME, dem=dem)
wps = [w for w in b["waypoints"] if w["action"] == "waypoint"]
mine = [w for w in wps if not w.get("filled") and not w.get("approach")]
# 最後一個點成了降落點（沒有標降落點、也不回起飛點），所以它是 LAND 而
# 不是 waypoint——三個點都還在，只是最後那個換了身分
ck("我放的三個點都在（其餘是系統補的中繼點）",
   len(mine) == 2 and b["waypoints"][-1]["action"] == "land",
   f"{len(mine)} 個我放的／{len(wps)} 個總共")
ck("每個我放的點都認得出自己是第幾個（src_i）",
   [w.get("src_i") for w in mine] == [0, 1], [w.get("src_i") for w in mine])
ck("降落點也認得出來", b["waypoints"][-1].get("src_i") == 2,
   b["waypoints"][-1].get("src_i"))
ck("進場點是系統補的，不是我放的", any(w.get("approach") for w in wps),
   [w.get("approach") for w in wps])
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
                and not w.get("approach") and abs(w["lat"] - lat) < 1e-6)

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
bh = pc.build_plan(PTS, {"land_at_home": True}, HOME, dem=dem)
ck("要飛回起飛點時會補中繼點（回程那一段起伏最大）",
   any("中繼" in d["what"] for d in bh["decisions"]),
   [d["what"] for d in bh["decisions"]])
ck("decisions 裡有「退回」這件事",
   any("退回" in d["what"] or "退回" in d["value"] for d in b6["decisions"]),
   [d["what"] for d in b6["decisions"]])

print("\n── decisions：系統替你決定了什麼（§3 動作 3）──")
ws = {d["what"] for d in b["decisions"]}
for need in ("起飛高度", "改速度項的位置", "降落地點", "降落方式"):
    ck(f"有「{need}」", need in ws, sorted(ws))
# 「高度基準」那一條刪掉了（使用者 2026-09-09）：它是操作員在畫面上選的，
# 不是系統替他決定的——列在決策表裡等於把他自己的選擇當成系統的判斷
ck("「高度基準」不在決策表裡", "高度基準" not in ws, sorted(ws))
ck("每一條都說得出為什麼", all(d["why"] for d in b["decisions"]))
sp = next(d for d in b["decisions"] if d["what"] == "改速度項的位置")
ck("改速度項在第一個航點之前", "之前" in sp["value"], sp["value"])

print("\n── 起飛高度跟著政策 ──")
tk = b["waypoints"][0]
ck("離地 3 m 的航線從 3 m 起飛（不是 1.5）", tk["alt"] == 3.0, tk["alt"])
b7 = pc.build_plan(PTS, {"height_m": 0.8}, HOME, dem=dem)
ck("政策比最低起飛高度還低時，用最低那個",
   b7["waypoints"][0]["alt"] == pc.MIN_TAKEOFF_ALT_M, b7["waypoints"][0]["alt"])
b8 = pc.build_plan(PTS, {"takeoff_alt_m": 10.0}, HOME, dem=dem)
ck("操作員自己給了就用他的", b8["waypoints"][0]["alt"] == 10.0,
   b8["waypoints"][0]["alt"])
mn = min(l["agl_m"] for l in pc.check_waypoints(
    b["waypoints"], 1000, 120, home=[HOME["lat"], HOME["lon"]], dem=dem,
    wp_spd=1.0)["legs"] if l.get("agl_m") is not None)
ck("整條最低離地不再被起飛那一段拖下去", mn > 2.0, mn)
ck("整條每一段都貼著政策（含爬升與進場）", abs(mn - 3.0) < 0.05, mn)
ap = [w for w in b["waypoints"] if w.get("approach")]
ck("有降落前的進場點", len(ap) == 1, len(ap))
ck("decisions 說得出進場點是系統補的",
   any("進場" in d["what"] for d in b["decisions"]), [d["what"] for d in b["decisions"]])

print("\n── 產出仍然是一份飛得起來的航線 ──")
ck("第一項是起飛", b["waypoints"][0]["action"] == "takeoff")
ck("第二項是改速度", b["waypoints"][1]["command"] == 178)
ck("最後一項是降落", b["waypoints"][-1]["action"] == "land")
chk = pc.check_waypoints(b["waypoints"], 1000, 120,
                         home=[HOME["lat"], HOME["lon"]], dem=dem, wp_spd=1.0)
ck("預設政策產生的航線不會穿地",
   not any("撞地" in p for p in chk["problems"]),
   [p[:80] for p in chk["problems"]])

print("\n── 發現變成選擇（§6）──")
# 刻意做一條會撞的：政策壓到 0.5 m
bad = pc.build_plan(PTS, {"height_m": 0.5}, HOME, dem=dem)
cb = pc.check_waypoints(bad["waypoints"], 1000, 120,
                        home=[HOME["lat"], HOME["lon"]], dem=dem, wp_spd=3.0)
worst = min(l["agl_m"] for l in cb["legs"] if l["agl_m"] is not None)
ck("先做出一條餘裕不足的", worst < pc.MIN_CLEARANCE_M, worst)

r = pc.resolve("raise_all", cb)
ck("raise_all 回政策改動", r["kind"] == "policy" and r["delta_m"] > 0, r)
pol2, _ = pc.apply_resolution(r, {"height_m": 0.5}, PTS, bad["waypoints"])
b9 = pc.build_plan(PTS, {"height_m": pol2["height_m"]}, HOME, dem=dem)
c9 = pc.check_waypoints(b9["waypoints"], 1000, 120,
                        home=[HOME["lat"], HOME["lon"]], dem=dem, wp_spd=3.0)
w9 = min(l["agl_m"] for l in c9["legs"] if l["agl_m"] is not None)
ck("套用之後真的夠了（而且有留餘裕）",
   w9 >= pc.MIN_CLEARANCE_M + pc.RESOLVE_MARGIN_M - 0.05, w9)

lg = min((l for l in cb["legs"] if l["agl_m"] is not None), key=lambda l: l["agl_m"])
r2 = pc.resolve("raise_leg", cb, lg["from"])
ck("raise_leg 回那一段的兩個航點",
   r2["kind"] == "leg" and set(r2["seqs"]) == {lg["from"], lg["to"]}, r2)
pol3, pts3 = pc.apply_resolution(r2, {"height_m": 0.5}, PTS, bad["waypoints"])
ck("政策沒被動到", pol3["height_m"] == 0.5, pol3)
ck("被抬高的點變成例外",
   any(q.get("alt_source") == pc.ALT_FROM_MANUAL for q in pts3), pts3)

r3 = pc.resolve("slow_all", cb)
ck("slow_all 降到門檻的速度", r3.get("speed_ms") == pc.LOW_SPEED_MS, r3)
r4 = pc.resolve("slow_leg", cb, lg["from"])
# 政策 3 m/s，把其中一段降到 1——這才是「例外」
pol4 = {**pc.default_policy(), "speed_ms": 3.0}
_, pts4 = pc.apply_resolution(r4, pol4, PTS, bad["waypoints"])
b10 = pc.build_plan(pts4, pol4, HOME, dem=dem)
dcs = [w for w in b10["waypoints"] if w.get("command") == 178]
ck("速度例外會多插一個改速度項（擺在那個航點之前）", len(dcs) >= 2, len(dcs))

ck("已經夠高時不給沒用的建議",
   pc.resolve("raise_all", pc.check_waypoints(
       b["waypoints"], 1000, 120, home=[HOME["lat"], HOME["lon"]],
       dem=dem, wp_spd=1.0))["kind"] == "none")
ck("不認得的動作要說出來", pc.resolve("nope", cb)["kind"] == "none")
ck("每一種回覆都帶一句話", all(
   pc.resolve(a, cb, lg["from"]).get("note")
   for a in ("raise_all", "raise_leg", "slow_all", "slow_leg", "assume",
             "ack", "nope")))

print("\n── 簽核的指紋（§7）──")
h1 = pc.waypoints_hash(b["waypoints"])
ck("同一份算出同一個", h1 == pc.waypoints_hash(b["waypoints"]))
moved = [dict(w) for w in b["waypoints"]]
moved[2]["alt"] = (moved[2]["alt"] or 0) + 0.1
ck("動一個高度就變", pc.waypoints_hash(moved) != h1)
moved2 = [dict(w) for w in b["waypoints"]]
moved2[2]["lat"] = moved2[2]["lat"] + 1e-5
ck("動一個位置就變", pc.waypoints_hash(moved2) != h1)
shuffled = list(reversed([dict(w) for w in b["waypoints"]]))
ck("順序不影響（照 seq 排）", pc.waypoints_hash(shuffled) == h1)

print("\n── 失效處置：返航也在同一片地形上（C7）──")
# 起飛點 123 m、航點 111 m，中間一道 167 m 的稜線
H2 = {"lat": 24.7734787, "lon": 121.045971}
P2 = [{"lat": 24.7684787, "lon": 121.037971}]
b11 = pc.build_plan(P2, None, H2, dem=dem)

c_no = pc.check_waypoints(b11["waypoints"], 5000, 300,
                          home=[H2["lat"], H2["lon"]], dem=dem, wp_spd=2.0)
ck("沒讀到 RTL_ALT_M 就不判（**不是當成安全**）",
   c_no.get("terrain_rtl") is None
   and not any("返航" in x for x in c_no["problems"]), c_no.get("terrain_rtl"))

c5 = pc.check_waypoints(b11["waypoints"], 5000, 300,
                        home=[H2["lat"], H2["lon"]], dem=dem, wp_spd=2.0,
                        rtl_alt_m=5.0)
ck("RTL 5 m 飛越 44 m 稜線 → 判定會撞",
   c5["terrain_rtl"]["min_agl_m"] < 0, c5["terrain_rtl"])
ck("而且說得出是哪一段、以及那是機上參數不是航線",
   any("返航會撞地" in x and "RTL_ALT_M" in x and "改航線不會讓返航變安全" in x
       for x in c5["problems"]),
   [x[:60] for x in c5["problems"]])

need = 5.0 - c5["terrain_rtl"]["min_agl_m"] + pc.MIN_CLEARANCE_M
c_hi = pc.check_waypoints(b11["waypoints"], 5000, 300,
                          home=[H2["lat"], H2["lon"]], dem=dem, wp_spd=2.0,
                          rtl_alt_m=need)
ck(f"照訊息說的調到 {need:.0f} m 就過得去",
   c_hi["terrain_rtl"]["min_agl_m"] >= pc.MIN_CLEARANCE_M - 0.05,
   c_hi["terrain_rtl"])

pr11 = pc.route_profile(P2 and b11["waypoints"], H2, dem=dem, rtl_alt_m=5.0)
rp = [x for x in pr11["points"] if x.get("rtl_agl") is not None]
ck("剖面每個點都帶返航的離地", len(rp) > 5, len(rp))
ck("有一段是紅的（返航會撞）", any(x["rtl_agl"] < 0 for x in rp))
# RTL_ALT 是**下限**不是目標：飛機比它高的時候維持現高
flat = pr11["home_amsl_m"] + 5
ck("谷地裡的返航高度就是 home ＋ RTL_ALT",
   abs(min(x["rtl_amsl"] for x in rp) - flat) < 0.6,
   (min(x["rtl_amsl"] for x in rp), flat))
ck("稜線上的返航高度跟著航線（RTL_ALT 是下限不是目標）",
   max(x["rtl_amsl"] for x in rp) > flat + 20,
   (max(x["rtl_amsl"] for x in rp), flat))
pr_no = pc.route_profile(b11["waypoints"], H2, dem=dem)
ck("沒給返航高度就不畫那一層",
   all("rtl_agl" not in x for x in pr_no["points"]))

print("\n── 剖面點要說得出「這是什麼」（使用者 2026-09-09）──")
pr0 = pc.route_profile(b["waypoints"], HOME, dem=dem)
named = [x for x in pr0["points"] if x.get("seq") is not None]
ck("每個航點都有 kind", all(x.get("kind") for x in named),
   [x.get("kind") for x in named])
ck("第一個是 takeoff", named[0]["kind"] == "takeoff", named[0].get("kind"))
ck("最後一個是 land", named[-1]["kind"] == "land", named[-1].get("kind"))
ck("中間是 wp", all(x["kind"] == "wp" for x in named[1:-1]),
   [x["kind"] for x in named[1:-1]])
ck("系統補的點標成 auto（中繼點、進場點）",
   any(x.get("auto") for x in named), [x.get("auto") for x in named])
ck("操作員放的點不是 auto",
   not any(x.get("auto") for x in named if x["kind"] == "takeoff"))

print("\n── 圍欄（使用者裁定 2026-09-09：圓形＋多邊形，只做規劃端）──")
fc = pc.fence_circle(HOME, 80, 30)
far = [{"seq": 0, "lat": HOME["lat"], "lon": HOME["lon"], "alt": 5,
        "frame": 3, "command": 16, "action": "takeoff"},
       {"seq": 1, "lat": HOME["lat"] + 0.002, "lon": HOME["lon"], "alt": 5,
        "frame": 3, "command": 16, "action": "waypoint"},
       {"seq": 2, "lat": HOME["lat"], "lon": HOME["lon"], "alt": 45,
        "frame": 3, "command": 16, "action": "waypoint"}]
fp, fw = pc.check_fence(far, fc)
ck("飛出圓形圍欄要報", any("圍欄之外" in x for x in fp), fp)
ck("超過高度上限要報", any("高度上限" in x for x in fp), fp)
ck("在圈內又不超高的不報",
   not pc.check_fence(far[:1], fc)[0], pc.check_fence(far[:1], fc)[0])

# frame 10 的高度是離**地面**的，不是離起飛點——比不了就說比不了，不猜
terr = [{"seq": 0, "lat": HOME["lat"], "lon": HOME["lon"], "alt": 5,
         "frame": 10, "command": 16, "action": "waypoint"}]
ck("地形跟隨的高度比不了上限，要說出來",
   any("比不了" in x for x in pc.check_fence(terr, fc)[1]),
   pc.check_fence(terr, fc))

poly = pc.fence_polygon([(HOME["lat"] - 0.001, HOME["lon"] - 0.001),
                         (HOME["lat"] - 0.001, HOME["lon"] + 0.001),
                         (HOME["lat"] + 0.001, HOME["lon"])], 30)
ck("多邊形圍欄擋得住外面的點",
   any("圍欄之外" in x for x in pc.check_fence(far, poly)[0]),
   pc.check_fence(far, poly)[0])
ck("少於三點就不是多邊形", pc.fence_polygon([(1, 2), (3, 4)]) == {})
ck("沒有高度上限就不判高度",
   not pc.check_fence(far, pc.fence_circle(HOME, 500))[0],
   pc.check_fence(far, pc.fence_circle(HOME, 500))[0])

print()
print("\n── 審查指紋也綁圍欄 ──")
circ = pc.fence_circle(HOME, 120, 30)
ck("沒有圍欄是 None（舊審查照樣算數）",
   pc.fence_hash(None) is None and pc.fence_hash({}) is None)
ck("同一個圍欄 JSON 來回一次不變",
   pc.fence_hash(circ) == pc.fence_hash(__import__("json").loads(
       __import__("json").dumps(circ))))
ck("半徑一改就變",
   pc.fence_hash(circ) != pc.fence_hash(pc.fence_circle(HOME, 121, 30)))

print("\n── 圍欄多邊形交叉（使用者 2026-09-11：頂點可以拖，就拖得出蝴蝶結）──")
sq = [(HOME["lat"] + 0.0005, HOME["lon"] - 0.0005), (HOME["lat"] + 0.0005, HOME["lon"] + 0.0005),
      (HOME["lat"] - 0.0005, HOME["lon"] + 0.0005), (HOME["lat"] - 0.0005, HOME["lon"] - 0.0005)]
bow = [sq[0], sq[2], sq[1], sq[3]]
ck("正方形不算交叉", not pc.fence_crossing(sq))
ck("蝴蝶結算交叉", pc.fence_crossing(bow))
ck("三角形不會交叉", not pc.fence_crossing(sq[:3]))
ck("凹的形狀不算交叉（場地不是凸的）",
   not pc.fence_crossing([sq[0], sq[1], (HOME["lat"], HOME["lon"]), sq[2], sq[3]]))
wp0 = [{"seq": 0, "lat": HOME["lat"], "lon": HOME["lon"], "alt": 5, "frame": 3,
        "command": 16, "action": "waypoint"}]
ck("規劃頁報交叉", pc.FENCE_CROSSING_MSG in pc.check_fence(wp0, pc.fence_polygon(bow, 30))[0])

if fails:
    print(f"✗ {len(fails)} 項沒過：" + "、".join(fails))
    sys.exit(1)
print("全部通過")
