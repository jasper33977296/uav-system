#!/usr/bin/env python3
"""把一個場域打包成離線可用的一份（DEM ＋ 正射 ＋ 建物）。

**佈署前、有網路的時候跑。** 現場沒有網路，而地形圖磚與正射影像都是
上游來的——沒先抓好，到了現場規劃頁就是一片空白。

    python3 scripts/pack-field.py --name itri --lat 24.7734787 --lon 121.045971
    python3 scripts/pack-field.py --name itri --radius-m 1500 --ortho-z 14 19

抓圖磚是走**本機後端的端點**而不是直連上游：那兩個端點已經知道上游在哪、
怎麼從 `.hgt` 換算、抓到之後要寫進哪個快取——在這裡再寫一次就會有第二份
會漂的規則。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import bundle  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "bundles")
API = os.environ.get("PACK_API", "http://localhost:38000/api")


def warm(kind: str, tiles, ext: str) -> tuple[int, int]:
    """逐張打端點，讓它把上游的東西抓進快取。回 (成功, 沒有)。"""
    ok = miss = 0
    for i, (z, x, y) in enumerate(tiles):
        url = f"{API}/{kind}/{z}/{x}/{y}.{ext}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                r.read()
            ok += 1
        except urllib.error.HTTPError as e:
            # **404 是正常的**：這一格上游本來就沒有（海上、範圍外）
            miss += 1
            if e.code != 404:
                print(f"  {url} → HTTP {e.code}")
        except Exception as e:                          # noqa: BLE001
            miss += 1
            print(f"  {url} → {type(e).__name__}")
        if (i + 1) % 200 == 0:
            print(f"  …{i + 1}/{len(tiles)}（有 {ok}、沒有 {miss}）")
            time.sleep(0.2)
    return ok, miss


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lat", type=float, default=24.7734787)
    ap.add_argument("--lon", type=float, default=121.045971)
    ap.add_argument("--radius-m", type=float, default=1200.0)
    ap.add_argument("--terrain-z", type=int, nargs=2, default=[10, 15])
    ap.add_argument("--ortho-z", type=int, nargs=2, default=[14, 18])
    ap.add_argument("--no-fetch", action="store_true",
                    help="只數現況、不上網（現場用這個檢查手上這份夠不夠）")
    a = ap.parse_args()

    bbox = bundle.bbox_around(a.lat, a.lon, a.radius_m)
    tt = bundle.tiles_for_bbox(bbox, *a.terrain_z)
    ot = bundle.tiles_for_bbox(bbox, *a.ortho_z)
    print(f"場域 {a.name}｜半徑 {a.radius_m:g} m｜bbox {bbox}")
    print(f"  地形 z{a.terrain_z[0]}-{a.terrain_z[1]}：{len(tt)} 張"
          f"　正射 z{a.ortho_z[0]}-{a.ortho_z[1]}：{len(ot)} 張")
    if len(tt) + len(ot) > bundle.MAX_TILES:
        raise SystemExit(f"共 {len(tt) + len(ot)} 張，超過上限 {bundle.MAX_TILES}"
                         "——把半徑或 zoom 上限調小")

    if not a.no_fetch:
        print("抓地形圖磚…")
        warm("terrain-rgb", tt, "png")
        print("抓正射影像…")
        warm("ortho", ot, "jpg")

    # 建物走 near_path 那條路的姊妹：整個 bbox
    import buildings as B
    st = B.reload()
    feats = []
    for b in st.items:
        if (b.bbox[2] < bbox[0] or b.bbox[0] > bbox[2]
                or b.bbox[3] < bbox[1] or b.bbox[1] > bbox[3]):
            continue
        dm = B.dims(b)
        feats.append({
            "type": "Feature",
            "properties": {"id": b.id, "name": b.name, "kind": b.kind,
                           "height_m": b.height_m,
                           "height_source": b.height_source,
                           "known": b.known, **dm},
            "geometry": {"type": "Polygon", "coordinates": [
                [[lo, la] for la, lo in b.ring] + [[b.ring[0][1], b.ring[0][0]]]]}})

    d = os.path.join(OUT, a.name)
    os.makedirs(d, exist_ok=True)
    man = bundle.build(a.name, bbox,
                       os.path.join(os.path.dirname(__file__), "..",
                                    "data", "terrain-tiles"),
                       os.path.join(os.path.dirname(__file__), "..",
                                    "data", "ortho"),
                       [f["properties"] for f in feats],
                       tuple(a.terrain_z), tuple(a.ortho_z))
    man["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    man["centre"] = {"lat": a.lat, "lon": a.lon, "radius_m": a.radius_m}
    with open(os.path.join(d, "buildings.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f,
                  ensure_ascii=False)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)

    L = man["layers"]
    print(f"\n寫出 {os.path.normpath(d)}")
    for k in ("terrain", "ortho"):
        x = L[k]
        print(f"  {k:<8} {x['tiles']} 張 / {x['bytes'] / 1e6:.1f} MB"
              f"　缺 {x['missing']}" + ("" if x["complete"] else "  ← 不完整"))
    b = L["buildings"]
    print(f"  buildings {b['count']} 棟，其中 **{b['unmeasured']} 棟沒量過高度**"
          f"（{b['unmeasured_pct']}%）")
    if not bundle.complete(man):
        print("\n**這一份不完整。** 缺的那些格子在現場會是空白——"
              "上游本來就沒有（海上／範圍外）就沒關係，其餘要回頭再抓一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
