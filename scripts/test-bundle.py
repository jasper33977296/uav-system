#!/usr/bin/env python3
"""場域資料包的回歸。

    DEM_DIR=data/dem BUILDINGS_DIR=data/buildings python3 scripts/test-bundle.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import bundle  # noqa: E402

fails = []


def ck(name, cond, got=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  ← {got}" if not cond else ""))
    if not cond:
        fails.append(name)


print("── 圖磚枚舉 ──")
bb = bundle.bbox_around(24.7734787, 121.045971, 800)
ck("bbox 是 (南,西,北,東)", bb[0] < bb[2] and bb[1] < bb[3], bb)
ck("半徑對得上（南北 ~1600 m）",
   abs((bb[2] - bb[0]) * 110574 - 1600) < 20, (bb[2] - bb[0]) * 110574)
t = bundle.tiles_for_bbox(bb, 14, 14)
ck("單一 zoom 枚舉出的張數 > 0", len(t) > 0, len(t))
ck("每張都在合法範圍", all(0 <= x < 2 ** z and 0 <= y < 2 ** z for z, x, y in t))
ck("沒有重複", len(set(t)) == len(t))
# 四倍律要在**張數夠多**的 zoom 上才看得到：z14 只有 2×2 張，
# 邊界的半格效應比本體還大（實測比值 1.5，那不是 bug 是尺度太小）
a17 = bundle.tiles_for_bbox(bb, 17, 17)
a18 = bundle.tiles_for_bbox(bb, 18, 18)
ck("zoom 加一，張數約四倍（z17→z18）",
   3.0 < len(a18) / len(a17) < 5.0, f"{len(a17)}→{len(a18)}")
ck("四角都被涵蓋", all(
    (14, *bundle.tile_xy(la, lo, 14)) in t
    for la, lo in ((bb[0], bb[1]), (bb[0], bb[3]), (bb[2], bb[1]), (bb[2], bb[3]))))

print("\n── manifest ──")
with tempfile.TemporaryDirectory() as d:
    tdir, odir = os.path.join(d, "t"), os.path.join(d, "o")
    for z, x, y in bundle.tiles_for_bbox(bb, 14, 14)[:3]:
        p = os.path.join(tdir, str(z), str(x))
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, f"{y}.png"), "wb") as f:
            f.write(b"x" * 100)
    os.makedirs(odir, exist_ok=True)
    blds = [{"known": True}, {"known": False}, {"known": False}]
    m = bundle.build("t", bb, tdir, odir, blds, (14, 14), (14, 14))
    L = m["layers"]
    ck("有數到放進去的那幾張", L["terrain"]["tiles"] == 3, L["terrain"]["tiles"])
    ck("缺的也數了", L["terrain"]["missing"] == len(t) - 3, L["terrain"]["missing"])
    ck("不完整就說不完整", not L["terrain"]["complete"])
    ck("正射一張都沒有", L["ortho"]["tiles"] == 0 and not L["ortho"]["complete"])
    ck("complete() 跟著 layer 走", not bundle.complete(m))
    ck("建物數對", L["buildings"]["count"] == 3 and L["buildings"]["measured"] == 1)
    ck("沒量過的比例算得出來", L["buildings"]["unmeasured_pct"] == 67,
       L["buildings"]["unmeasured_pct"])

    print("\n── caveat 是介面的一部分，不是文件 ──")
    for k in ("terrain", "ortho", "buildings"):
        ck(f"{k} 帶著 caveat", bool(L[k].get("caveat")))
    ck("地形的 caveat 說得出編碼", "terrarium" in L["terrain"]["caveat"])
    ck("地形的 caveat 說得出它畫不出樓",
       "畫不出" in L["terrain"]["caveat"])
    ck("建物的 caveat 說得出高度多半不是量的",
       "高度多半不是" in L["buildings"]["caveat"])
    ck("bbox 兩種順序都給（GeoJSON 慣例 ＋ 我方慣例）",
       m["bbox"][0] == bb[1] and m["bbox_latlon"][0] == bb[0])

    print("\n── 現場模式：只數不抓 ──")
    m2 = bundle.build("t", bb, tdir, odir, blds, (14, 14), (14, 14))
    ck("同樣的輸入給同樣的答案", m2["layers"] == L)
    ck("_files 列得出實際存在的那幾個",
       len(m["_files"]["terrain"]) == 3, m["_files"]["terrain"])

print("\n── .hgt 涵蓋率：出發前那一問（issues/047 §2）──")
import terrain  # noqa: E402
ck("一個小場域只要一塊", terrain.tiles_for_bbox(bb) == ["N24E121.hgt"],
   terrain.tiles_for_bbox(bb))
ck("跨經度邊界要兩塊",
   terrain.tiles_for_bbox((24.9, 120.98, 24.95, 121.02))
   == ["N24E120.hgt", "N24E121.hgt"])
ck("跨四塊就是四塊",
   len(terrain.tiles_for_bbox((23.98, 120.98, 24.02, 121.02))) == 4)
ck("南半球／西半球用 floor 不是 int",
   terrain.tiles_for_bbox((-0.5, -0.5, -0.4, -0.4)) == ["S01W001.hgt"],
   terrain.tiles_for_bbox((-0.5, -0.5, -0.4, -0.4)))
with tempfile.TemporaryDirectory() as d:
    c = terrain.coverage(bb, d)
    ck("空目錄＝全缺", not c["complete"] and c["missing"] == ["N24E121.hgt"], c)
    open(os.path.join(d, "N24E121.hgt"), "wb").write(b"x" * 10)
    c2 = terrain.coverage(bb, d)
    ck("放進去就算有", c2["complete"] and c2["have"] == ["N24E121.hgt"])
    ck("大小也數了", c2["bytes"] == 10, c2["bytes"])

print("\n── 不完整時要說得出下一步 ──")
m3 = bundle.build("t", bb, "/nope", "/nope", [], (14, 14), (14, 14),
                  dem_dir="/nope", fetched=False)
ck("complete() 把 dem 也算進去", not bundle.complete(m3))
hints = bundle.fix_hint(m3)
ck("缺 .hgt 要給 fetch-dem 的指令",
   any("fetch-dem.py" in h for h in hints), hints)
ck("沒抓過建物要說「不是那裡沒有樓」",
   any("不是「那裡沒有樓」" in h for h in hints), hints)
ck("每一條都是可以直接貼上去跑的",
   all("python3 scripts/" in h for h in hints), hints)
m4 = bundle.build("t", bb, "/nope", "/nope", [], (14, 14), (14, 14),
                  dem_dir="/nope", fetched=True)
ck("抓過但 0 棟就不再叫人去抓",
   not any("沒有抓過" in h for h in bundle.fix_hint(m4)))

print("\n── 上限 ──")
big = bundle.bbox_around(24.77, 121.04, 20000)
ck("大範圍高 zoom 會超過上限（腳本據此擋下）",
   len(bundle.tiles_for_bbox(big, 14, 19)) > bundle.MAX_TILES,
   len(bundle.tiles_for_bbox(big, 14, 19)))

print()
if fails:
    print(f"✗ {len(fails)} 項沒過：" + "、".join(fails))
    sys.exit(1)
print("全部通過")
