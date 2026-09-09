"""場域資料包：**一份說得出自己涵蓋到哪、以及哪裡不能信的資料集。**

現場沒有網路，而地形圖磚、正射影像、建物輪廓全都要在有網路時先抓好。
這個模組把「哪些圖磚屬於這個場域」變成一個可以檢查、可以打包、可以
交給別的系統的東西。

**為什麼不只是一包檔案**（使用者 2026-09-09：要用 API 給其他系統即時呈現）：
拿到一堆 PNG 的人不知道那是 terrarium 編碼、不知道地面線畫不出任何一棟樓、
不知道八成建物的高度是猜的。**資料自己要說得出這些**，否則接收端會把
一份 30 m 格子的表面當成地形圖用。所以 manifest 裡每一種來源都帶
`caveat`，那不是文件，是介面的一部分。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

#: 一次最多允許幾張圖磚。**有上限而且要說**：z19 涵蓋 5 km² 就是幾萬張，
#: 那不是「慢一點」，是把上游打爛而且現場也塞不下。
MAX_TILES = 20000


def tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tiles_for_bbox(bbox: tuple[float, float, float, float],
                   z0: int, z1: int) -> list[tuple[int, int, int]]:
    """bbox ＝ (min_lat, min_lon, max_lat, max_lon)，回 [(z, x, y), ...]。"""
    out: list[tuple[int, int, int]] = []
    s, w, n, e = bbox
    for z in range(z0, z1 + 1):
        x0, y0 = tile_xy(n, w, z)          # 左上（北、西）
        x1, y1 = tile_xy(s, e, z)          # 右下（南、東）
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                out.append((z, x, y))
    return out


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, ...]:
    import geo
    d = radius_m / geo.M_PER_DEG_LAT
    dl = radius_m / geo.m_per_deg_lon(lat)
    return (round(lat - d, 6), round(lon - dl, 6),
            round(lat + d, 6), round(lon + dl, 6))


@dataclass
class Layer:
    """一種來源。`have`／`missing` 是**實際數過的**，不是估的。"""
    kind: str
    url: str
    zooms: tuple[int, int]
    caveat: str
    have: int = 0
    missing: int = 0
    bytes: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.missing == 0

    def to_json(self) -> dict:
        return {"kind": self.kind, "url": self.url,
                "zooms": list(self.zooms), "caveat": self.caveat,
                "tiles": self.have, "missing": self.missing, "bytes": self.bytes,
                "complete": self.complete}


TERRAIN_CAVEAT = ("SRTM 1 弧秒（水平約 30 m），terrarium 編碼"
                  "（h = R*256 + G + B/256 - 32768）。**它是被格子抹平的表面**："
                  "樹冠與屋頂混在裡面，但畫不出任何一棟樓。缺的圖磚是 404，"
                  "**不是一張全 0 的海平面**——後者看起來像個答案。")
ORTHO_CAVEAT = ("內政部國土測繪中心 PHOTO2 正射影像。只有影像，沒有高度。")
DEM_CAVEAT = ("SRTM 1 弧秒的 `.hgt` 原始檔（一塊 1°×1°、約 25 MB）。"
              "**地形圖磚是從它換算出來的**，所以缺一塊 `.hgt`，"
              "那一整片的地形圖磚就是 404、規劃頁的預檢會說「沒有檢查」"
              "——不會說「通過」，但也就等於沒有那道防線。")
BUILDINGS_CAVEAT = ("OSM 建物輪廓。**輪廓是量的（公尺級），高度多半不是**："
                    "`height_source` 為 `osm:height` 才是有人填的公尺數，"
                    "`osm:levels` 是樓層數 × 3.5 m 推算，`unknown` 是"
                    "沒有人量過——那一種不可以拿來放行。輪廓只取外環，"
                    "中庭當成實心。")


def scan(cache_dir: str, tiles: list[tuple[int, int, int]], ext: str) -> tuple[list[str], int, int]:
    """數這批圖磚在快取裡有幾張。回 (相對路徑, 有幾張, 幾位元組)。"""
    files, n, size = [], 0, 0
    for z, x, y in tiles:
        rel = os.path.join(str(z), str(x), f"{y}.{ext}")
        p = os.path.join(cache_dir, rel)
        if os.path.exists(p):
            files.append(rel)
            n += 1
            size += os.path.getsize(p)
    return files, n, size


def build(name: str, bbox, terrain_dir: str, ortho_dir: str,
          buildings, terrain_z=(10, 15), ortho_z=(14, 18),
          dem_dir: str | None = None, fetched: bool | None = None) -> dict:
    """組出 manifest。**只數現況，不抓東西**——抓是 `scripts/pack-field.py` 的事。

    分開的理由：這個函式要能在**現場**跑（沒有網路），用來回答
    「我手上這一份夠不夠飛這個場域」。會上網的東西不能長在這裡。
    """
    tt = tiles_for_bbox(bbox, *terrain_z)
    ot = tiles_for_bbox(bbox, *ortho_z)
    tf, tn, tb = scan(terrain_dir, tt, "png")
    of, on, ob = scan(ortho_dir, ot, "jpg")
    terr = Layer("terrain-rgb", "/api/terrain-rgb/{z}/{x}/{y}.png", terrain_z,
                 TERRAIN_CAVEAT, tn, len(tt) - tn, tb, tf)
    orth = Layer("ortho", "/api/ortho/{z}/{x}/{y}.jpg", ortho_z,
                 ORTHO_CAVEAT, on, len(ot) - on, ob, of)

    # `.hgt` 的涵蓋率**要在這裡問**，不是等到現場開規劃頁才發現缺一塊。
    # 地形圖磚是從它換算出來的，所以它才是根；上面那個 `terrain` 層數的是
    # 換算後的產物，缺了只會說「這一格沒有」，說不出「因為少了一個檔案」
    import terrain as _terrain
    dem = _terrain.coverage(bbox, dem_dir)
    if fetched is None:
        import buildings as _b
        fetched = _b.shared().covers(bbox)

    known = [b for b in buildings if b.get("known")]
    unknown = [b for b in buildings if not b.get("known")]
    return {
        "name": name,
        "bbox": [bbox[1], bbox[0], bbox[3], bbox[2]],   # GeoJSON 慣例：西南東北
        "bbox_latlon": list(bbox),
        "layers": {"dem": {"kind": "srtm-hgt", "caveat": DEM_CAVEAT,
                           "need": dem["need"], "have": len(dem["have"]),
                           "missing": dem["missing"], "bytes": dem["bytes"],
                           "complete": dem["complete"]},
                   "terrain": terr.to_json(), "ortho": orth.to_json(),
                   "buildings": {
                       "kind": "buildings", "provider": "OSM",
                       "url": f"/api/bundles/{name}/buildings.geojson",
                       "caveat": BUILDINGS_CAVEAT,
                       "count": len(buildings),
                       # **抓過沒有**，與「有幾棟」是兩個問題：0 棟可能是
                       # 那裡真的沒有樓，也可能是我們從來沒抓過那一區
                       "fetched": fetched,
                       "measured": len(known), "unmeasured": len(unknown),
                       # **這一欄是給接收端看的**：八成沒量過的資料集，
                       # 拿去做「即時呈現」時該畫成什麼樣子由它決定
                       "unmeasured_pct": (round(100 * len(unknown) / len(buildings))
                                          if buildings else None)}},
        "_files": {"terrain": tf, "ortho": of},
    }


def complete(man: dict) -> bool:
    return all(man["layers"][k].get("complete", True)
               for k in ("dem", "terrain", "ortho") if k in man["layers"])


def fix_hint(man: dict) -> list[str]:
    """缺東西時**把補救的指令直接寫出來**。

    「不完整」不是一個結論，是一件待辦——而待辦要說得出下一步是什麼。
    """
    out = []
    dem = man["layers"].get("dem") or {}
    if dem.get("missing"):
        s, w, n, e = man["bbox_latlon"]
        out.append(f"缺 {len(dem['missing'])} 塊 .hgt（{'、'.join(dem['missing'])}）："
                   f"python3 scripts/fetch-dem.py {s:.4f} {w:.4f} {n:.4f} {e:.4f}")
    b = man["layers"].get("buildings") or {}
    if not b.get("fetched"):
        s2, w2, n2, e2 = man["bbox_latlon"]
        out.append("**這一區的建物沒有抓過**（不是「那裡沒有樓」）："
                   f"python3 scripts/fetch-buildings.py "
                   f"{(s2 + n2) / 2:.4f} {(w2 + e2) / 2:.4f} --radius-m 1000")
    for k, label in (("terrain", "地形圖磚"), ("ortho", "正射影像")):
        L = man["layers"].get(k) or {}
        if L.get("missing"):
            out.append(f"缺 {L['missing']} 張{label}："
                       f"python3 scripts/pack-field.py --name {man['name']}"
                       f"（再跑一次；上游本來就沒有的那些會一直缺）")
    return out
