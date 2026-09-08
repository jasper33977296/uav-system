#!/usr/bin/env python3
"""抓 OSM 建物輪廓放進 `data/buildings/`（doc/field-3d-model-design.md §7-3）。

**佈署前的準備動作，不是執行期的功能**——現場離線，要在有網路的時候先抓好
（同 `scripts/fetch-dem.py`）。輸出**不進 git**。

    python3 scripts/fetch-buildings.py 24.7734 121.0459            # 預設半徑 800 m
    python3 scripts/fetch-buildings.py 24.7734 121.0459 --radius-m 1500
    python3 scripts/fetch-buildings.py 24.76 121.03 24.79 121.06   # 明確的 bbox

高度照 §9-A 三層退讓；**第三層不給數字**，只記「有一棟樓、高度未知」。
自交或點數不足的輪廓直接丟掉並印出是哪一棟（§9-C）。
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import buildings  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "buildings")
MIRRORS = ["https://overpass-api.de/api/interpreter",
           "https://overpass.kumi.systems/api/interpreter"]


def query(bbox) -> str:
    s, w, n, e = bbox
    # §9-B：body 級 `out geom`（`out tags geom` 會讓 relation 的 members 整批消失），
    # 而且 way ＋ multipolygon relation 都要抓
    return (f"[out:json][timeout:90];("
            f'way["building"]({s},{w},{n},{e});'
            f'relation["building"]({s},{w},{n},{e});'
            f");out geom;")


def fetch(bbox) -> dict:
    """公開 Overpass 忙起來就回 504，退避重試幾次通常就過了。"""
    q = query(bbox).encode()
    last = None
    for attempt in range(4):
        for url in MIRRORS:
            try:
                req = urllib.request.Request(
                    url, data=q, headers={"User-Agent": "uav-gcs-fetch-buildings"})
                with urllib.request.urlopen(req, timeout=200) as r:
                    return json.load(r)
            except Exception as e:                      # noqa: BLE001
                last = f"{url}：{type(e).__name__}: {e}"
                print(f"  {last}")
        if attempt < 3:
            time.sleep(15 * (attempt + 1))
    raise SystemExit(f"重試四輪都拿不到：{last}")


def rings_of(el: dict):
    """一個元素的外環們。way 一個；relation 取 outer 成員（§9-D：忽略內庭）。"""
    if el.get("type") == "way":
        g = el.get("geometry") or []
        return [[(p["lat"], p["lon"]) for p in g if "lat" in p]]
    out = []
    for m in el.get("members") or []:
        if m.get("role") != "outer" or m.get("type") != "way":
            continue
        g = m.get("geometry") or []
        if g:
            out.append([(p["lat"], p["lon"]) for p in g if "lat" in p])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("coords", type=float, nargs="+",
                    help="lat lon（配 --radius-m）或 min_lat min_lon max_lat max_lon")
    ap.add_argument("--radius-m", type=float, default=800.0)
    ap.add_argument("--name", help="輸出檔名（預設由 bbox 產生）")
    a = ap.parse_args()

    if len(a.coords) == 2:
        lat, lon = a.coords
        d = a.radius_m / 110574.0
        dl = d / math.cos(math.radians(lat))
        bbox = (lat - d, lon - dl, lat + d, lon + dl)
    elif len(a.coords) == 4:
        bbox = tuple(a.coords)
    else:
        raise SystemExit("要 2 個或 4 個座標")
    bbox = tuple(round(v, 6) for v in bbox)
    print(f"bbox {bbox}")

    js = fetch(bbox)
    els = js.get("elements", [])
    print(f"Overpass 回 {len(els)} 個元素")

    feats, dropped, tally = [], [], {}
    for el in els:
        tags = el.get("tags") or {}
        h, src, levels = buildings.resolve_height(tags)
        tally[src] = tally.get(src, 0) + 1
        eid = f"{el.get('type')}/{el.get('id')}"
        for k, ring in enumerate(rings_of(el)):
            if ring and ring[0] == ring[-1]:
                ring = ring[:-1]
            if not buildings._ring_ok(ring):
                dropped.append(f"{eid}#{k}（{len(ring)} 點）")
                continue
            feats.append({
                "type": "Feature",
                "properties": {
                    "id": eid if k == 0 else f"{eid}#{k}",
                    "name": tags.get("name"),
                    "kind": tags.get("building") or "yes",
                    "height_m": h, "height_source": src, "levels": levels,
                },
                "geometry": {"type": "Polygon", "coordinates": [
                    [[lo, la] for la, lo in ring] + [[ring[0][1], ring[0][0]]]]},
            })

    os.makedirs(OUT, exist_ok=True)
    name = a.name or ("%.4f_%.4f_%.4f_%.4f.geojson" % bbox)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection",
                   "bbox": [bbox[1], bbox[0], bbox[3], bbox[2]],
                   "features": feats}, f, ensure_ascii=False)

    print(f"寫出 {len(feats)} 棟 → {os.path.normpath(path)}"
          f"（{os.path.getsize(path) / 1024:.0f} KB）")
    for k in ("osm:height", "osm:levels", "unknown"):
        print(f"  {k}：{tally.get(k, 0)}")
    if dropped:
        print(f"  丟掉 {len(dropped)} 個輪廓（§9-C）：{', '.join(dropped[:10])}")
    if tally.get("unknown"):
        print(f"\n**{tally['unknown']} 棟高度未知**——這些在剖面圖上是開口向上的"
              f"柱子，檢查算「不知道」而不是一個數字。要放行就得實地量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
