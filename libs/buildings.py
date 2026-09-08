"""建物輪廓與高度——**帶著出處，而且「猜的」不算數**。

輪廓來自 OSM（`scripts/fetch-buildings.py` 在有網路時抓進 `data/buildings/`，
現場離線只讀檔）。高度照 doc/field-3d-model-design.md §9-A 的三層退讓，
但第三層在這個系統裡的意思是**「有一棟樓，高度未知」**，不是一個數字：
猜 9 m 而實際 12 m 的樓，在傳播模型裡是誤差，在這裡是撞機。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

#: 每層樓的公尺數。**這是猜的**（OSM 沒有這個欄位），所以只用來往上取——
#: 障礙物寧可算高。3.0 是常見值，這裡加一層樓板厚度往上抓。
LEVEL_M = 3.5

#: OSM 輪廓的水平解析度：畫的是牆的位置，不是量出來的，公尺級。
OSM_HORIZ_RES_M = 3.0

#: 高度未知時的**假設高度**預設值（§9-A 第三層的 fallback）。
#: 這是規劃時的一個旋鈕，不是這些樓的高度——真正的答案要等光達實測。
#: 呼叫端不給 `assume_m` 就代表「不假設」，那時未知的樓仍然是 top=None。
ASSUMED_DEFAULT_M = 9.0

DATA_DIR = os.environ.get(
    "BUILDINGS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "buildings"))


@dataclass(frozen=True)
class Building:
    id: str                       # "way/12345"
    name: str | None
    kind: str                     # building=* 的值
    height_m: float | None        # None ＝ 高度未知，**不可用來放行**
    height_source: str            # osm:height／osm:levels／unknown
    ring: list[tuple[float, float]]          # [(lat, lon), ...] 外環
    bbox: tuple[float, float, float, float]  # (min_lat, min_lon, max_lat, max_lon)

    @property
    def known(self) -> bool:
        return self.height_m is not None


def resolve_height(tags: dict) -> tuple[float | None, str, int | None]:
    """§9-A 的三層。回傳 (公尺, 出處, 樓層數)。"""
    h = tags.get("height")
    if h is not None:
        try:
            return float(str(h).replace("m", "").strip()), "osm:height", None
        except ValueError:
            pass
    lv = tags.get("building:levels")
    if lv is not None:
        try:
            n = int(float(lv))
            if n > 0:
                return math.ceil(n * LEVEL_M), "osm:levels", n
        except ValueError:
            pass
    return None, "unknown", None


def _ring_ok(ring) -> bool:
    """§9-C：點數不足或自交就丟掉。沒有 PostGIS，修幾何比丟掉危險。"""
    if len(ring) < 4:
        return False
    n = len(ring)
    for i in range(n):
        a1, a2 = ring[i], ring[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            b1, b2 = ring[j], ring[(j + 1) % n]
            if _crosses(a1, a2, b1, b2):
                return False
    return True


def _side(p, q, r) -> float:
    return (q[1] - p[1]) * (r[0] - p[0]) - (q[0] - p[0]) * (r[1] - p[1])


def _crosses(a1, a2, b1, b2) -> bool:
    d1, d2 = _side(a1, a2, b1), _side(a1, a2, b2)
    d3, d4 = _side(b1, b2, a1), _side(b1, b2, a2)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


def _in_ring(lat: float, lon: float, ring) -> bool:
    """射線法（與 `plan_check._in_polygon` 同一個做法，邊界算在內）。"""
    inside = False
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            if lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


class Store:
    """`data/buildings/*.geojson` 讀進來的一份。找不到檔案就是空的。"""

    def __init__(self, path: str | None = None):
        self.dir = path or DATA_DIR
        self.items: list[Building] = []
        self.files: list[str] = []
        self._load()

    def _load(self) -> None:
        if not os.path.isdir(self.dir):
            return
        for fn in sorted(os.listdir(self.dir)):
            if not fn.endswith(".geojson"):
                continue
            try:
                with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                    fc = json.load(f)
            except (OSError, ValueError):
                continue
            self.files.append(fn)
            for ft in fc.get("features", []):
                b = _from_feature(ft)
                if b is not None:
                    self.items.append(b)

    @property
    def available(self) -> bool:
        return bool(self.items)

    def at(self, lat: float, lon: float) -> Building | None:
        """蓋住這一點的建物。重疊時取**已知高度中最高的**；
        全都未知就回其中一棟——未知本來就不比大小。"""
        hit = [b for b in self.items
               if b.bbox[0] <= lat <= b.bbox[2] and b.bbox[1] <= lon <= b.bbox[3]
               and _in_ring(lat, lon, b.ring)]
        if not hit:
            return None
        known = [b for b in hit if b.known]
        return max(known, key=lambda b: b.height_m) if known else hit[0]


def _from_feature(ft: dict) -> Building | None:
    g = ft.get("geometry") or {}
    if g.get("type") != "Polygon":
        return None
    coords = (g.get("coordinates") or [[]])[0]        # §9-D：只取外環
    ring = [(float(c[1]), float(c[0])) for c in coords if len(c) >= 2]
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return None
    p = ft.get("properties") or {}
    lats = [r[0] for r in ring]
    lons = [r[1] for r in ring]
    return Building(
        id=str(p.get("id") or ft.get("id") or "?"),
        name=p.get("name"),
        kind=p.get("kind") or "yes",
        height_m=p.get("height_m"),
        height_source=p.get("height_source") or "unknown",
        ring=ring,
        bbox=(min(lats), min(lons), max(lats), max(lons)),
    )


_shared: Store | None = None


def shared() -> Store:
    global _shared
    if _shared is None:
        _shared = Store()
    return _shared


def reload() -> Store:
    """抓完新資料之後叫一次；測試也用它換掉共用的那份。"""
    global _shared
    _shared = Store()
    return _shared
