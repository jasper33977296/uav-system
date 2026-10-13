#!/usr/bin/env python3
"""抓 OSM 植被（樹、行道樹、樹林）放進 `data/vegetation/`。

**佈署前的準備動作，不是執行期的功能**——現場離線，要在有網路時先抓好
（同 `scripts/fetch-buildings.py`、`scripts/fetch-dem.py`）。輸出不進 git。

    python3 scripts/fetch-vegetation.py 24.7734 121.0459              # 預設半徑 800 m
    python3 scripts/fetch-vegetation.py 24.76 121.03 24.79 121.06     # 明確的 bbox

**抓完要看最後那段統計再決定要不要開這個圖層。** OSM 的植被覆蓋率各地差
很多：場地這塊（2026-10-13 實查）只有 5 棵樹、3 排行道樹、3 片林，而現場
顯然不只這些。資料少不等於不能用，但畫面上必須講清楚「沒標到不等於淨空」，
否則使用者會把它當成樹的清單——詳見 `libs/vegetation.py` 的開頭。
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import vegetation  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "vegetation")
MIRRORS = ["https://overpass-api.de/api/interpreter",
           "https://overpass.kumi.systems/api/interpreter"]


def query(bbox) -> str:
    s, w, n, e = bbox
    box = f"({s},{w},{n},{e})"
    # 點（單棵樹）、線（行道樹）、面（林／灌叢／果園）都要。`out geom` 同
    # fetch-buildings：`out tags geom` 會讓 relation 的 members 整批消失
    sel = []
    for (k, v) in vegetation.KINDS:
        sel.append(f'node["{k}"="{v}"]{box};')
        sel.append(f'way["{k}"="{v}"]{box};')
        sel.append(f'relation["{k}"="{v}"]{box};')
    return f"[out:json][timeout:90];(" + "".join(sel) + ");out geom;"


def fetch(bbox) -> dict:
    """公開 Overpass 忙起來就回 504／「server too busy」，退避重試幾次通常就過。"""
    q = query(bbox).encode()
    last = None
    for attempt in range(5):
        for url in MIRRORS:
            try:
                req = urllib.request.Request(
                    url, data=q, headers={"User-Agent": "uav-gcs-fetch-vegetation"})
                with urllib.request.urlopen(req, timeout=200) as r:
                    raw = r.read()
                # 忙線時它回的是 HTTP 200 包著一頁 HTML 錯誤——**不檢查就會
                # 寫出一個空檔案，而空檔案與「這裡沒有樹」長得一模一樣**
                if not raw.lstrip().startswith(b"{"):
                    raise ValueError(raw[:200].decode(errors="replace").strip())
                return json.loads(raw)
            except Exception as e:                      # noqa: BLE001
                last = f"{url}：{type(e).__name__}: {e}"
                print(f"  {last}")
        if attempt < 4:
            time.sleep(15 * (attempt + 1))
    raise SystemExit(f"重試五輪都拿不到：{last}")


def geom_of(el: dict, kind: str) -> dict | None:
    """一個元素的 GeoJSON geometry。面的收口與型別都在這裡決定。"""
    t = el.get("type")
    if t == "node":
        if el.get("lat") is None:
            return None
        return {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
    if t == "way":
        g = [p for p in (el.get("geometry") or []) if p and "lat" in p]
        if len(g) < 2:
            return None
        co = [[p["lon"], p["lat"]] for p in g]
        # 行道樹是線；林／灌叢／果園是面（閉合的 way）
        if kind == "tree_row":
            return {"type": "LineString", "coordinates": co}
        if co[0] != co[-1]:
            co.append(co[0])
        if len(co) < 4:
            return None
        return {"type": "Polygon", "coordinates": [co]}
    # relation（multipolygon 的林）：每個 outer 成員各自成一個面
    rings = []
    for m in el.get("members") or []:
        if m.get("role") != "outer" or m.get("type") != "way":
            continue
        g = [p for p in (m.get("geometry") or []) if p and "lat" in p]
        if len(g) < 3:
            continue
        co = [[p["lon"], p["lat"]] for p in g]
        if co[0] != co[-1]:
            co.append(co[0])
        rings.append(co)
    if not rings:
        return None
    return {"type": "Polygon", "coordinates": [rings[0]]}


def height_of(tags: dict) -> float | None:
    """**有量就帶，沒量就 None。** 不從樹種、不從樹冠直徑推——那是猜的。"""
    h = tags.get("height")
    if h is None:
        return None
    try:
        return float(str(h).replace("m", "").strip())
    except ValueError:
        return None


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

    els = fetch(bbox).get("elements", [])
    print(f"Overpass 回 {len(els)} 個元素")

    feats, tally, with_h = [], {}, 0
    for el in els:
        tags = el.get("tags") or {}
        kind = vegetation.classify(tags)
        if kind is None:
            continue
        g = geom_of(el, kind)
        if g is None:
            continue
        h = height_of(tags)
        with_h += h is not None
        tally[kind] = tally.get(kind, 0) + 1
        feats.append({
            "type": "Feature",
            "properties": {
                "id": f"{el.get('type')}/{el.get('id')}",
                "name": tags.get("name"),
                "kind": kind,
                "label": vegetation.LABELS.get(kind, kind),
                # **跟建物同一個欄位名與同一個意思**：None ＝ 沒量過
                "height_m": h,
                "height_source": "osm:height" if h is not None else "unknown",
            },
            "geometry": g,
        })

    os.makedirs(OUT, exist_ok=True)
    name = a.name or ("%.4f_%.4f_%.4f_%.4f.geojson" % bbox)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection",
                   "bbox": [bbox[1], bbox[0], bbox[3], bbox[2]],
                   "features": feats}, f, ensure_ascii=False)

    print(f"寫出 {len(feats)} 筆 → {os.path.normpath(path)}"
          f"（{os.path.getsize(path) / 1024:.1f} KB）")
    for k, v in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"  {vegetation.LABELS.get(k, k)}（{k}）：{v}")
    print(f"\n**有高度的：{with_h} / {len(feats)}**"
          "——沒有高度的只能畫位置，不能說它會不會擋到航線。")
    if len(feats) < 50:
        print(f"**只有 {len(feats)} 筆。** OSM 的植被覆蓋率各地差很多，這個數字"
              "多半遠少於現場實際的樹。開這個圖層之前先確認畫面上有講"
              "「沒標到不等於淨空」，否則它會被當成樹的清單。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
