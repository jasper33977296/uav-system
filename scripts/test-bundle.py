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
