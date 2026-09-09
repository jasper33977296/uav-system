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

import geo

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


def _hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """凸包（Andrew monotone chain）。長寬只看外緣，凹進去的部分不影響。"""
    p = sorted(set(pts))
    if len(p) < 3:
        return p

    def half(seq):
        out: list[tuple[float, float]] = []
        for q in seq:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (q[1] - y1) - (y2 - y1) * (q[0] - x1) > 0:
                    break
                out.pop()
            out.append(q)
        return out[:-1]

    return half(p) + half(reversed(p))


def dims(b: "Building") -> dict:
    """一棟樓的長、寬、面積、朝向。

    **長寬來自輪廓的最小面積外接矩形**（旋轉卡尺），不是南北向的包圍盒
    ——一棟斜的樓照軸向去量會兩邊都偏大。輪廓是量出來的（OSM 足跡，
    公尺級），所以長寬跟高度**不是同一種東西**：高度多半是推算或假設的，
    畫面上要分開講。
    """
    lat0 = sum(p[0] for p in b.ring) / len(b.ring)
    lon0 = sum(p[1] for p in b.ring) / len(b.ring)
    pts = [geo.to_enu(la, lo, lat0, lon0) for la, lo in b.ring]
    hull = _hull(pts)
    if len(hull) < 3:
        return {"length_m": None, "width_m": None, "area_m2": None,
                "bearing_deg": None}
    best = None
    n = len(hull)
    for i in range(n):
        (x1, y1), (x2, y2) = hull[i], hull[(i + 1) % n]
        ex, ey = x2 - x1, y2 - y1
        L = math.hypot(ex, ey)
        if L < 1e-9:
            continue
        ux, uy = ex / L, ey / L
        us = [q[0] * ux + q[1] * uy for q in hull]
        vs = [-q[0] * uy + q[1] * ux for q in hull]
        w, h = max(us) - min(us), max(vs) - min(vs)
        if best is None or w * h < best[0]:
            best = (w * h, max(w, h), min(w, h), math.degrees(math.atan2(ux, uy)))
    area = abs(sum(hull[i][0] * hull[(i + 1) % n][1] - hull[(i + 1) % n][0] * hull[i][1]
                   for i in range(n))) / 2
    return {"length_m": round(best[1], 1), "width_m": round(best[2], 1),
            "area_m2": round(area), "bearing_deg": round(best[3] % 180, 1)}


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def path_dist_m(b: "Building", path: list[tuple[float, float]]) -> float:
    """這棟樓的輪廓離這條折線最近有多遠（公尺）。線在樓裡面就是 0。"""
    if len(path) < 1:
        return float("inf")
    lat0, lon0 = path[0]
    ring = [geo.to_enu(la, lo, lat0, lon0) for la, lo in b.ring]
    line = [geo.to_enu(la, lo, lat0, lon0) for la, lo in path]
    if any(_in_ring(la, lo, b.ring) for la, lo in path):
        return 0.0
    best = float("inf")
    segs = list(zip(line, line[1:])) or [(line[0], line[0])]
    for (px, py) in ring:
        for (ax, ay), (bx, by) in segs:
            best = min(best, _seg_dist(px, py, ax, ay, bx, by))
    # 線可能整條在樓外但穿過某一條邊——把輪廓的邊也當線段量一次
    for (px, py) in line:
        for i in range(len(ring)):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % len(ring)]
            best = min(best, _seg_dist(px, py, ax, ay, bx, by))
    return best


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

    def near_path(self, path: list[tuple[float, float]],
                  buffer_m: float = 30.0) -> list[tuple["Building", float]]:
        """離這條航線 `buffer_m` 以內的建物，近的排前面。

        **範圍跟著航線走，不是一個固定方框**（使用者裁定 2026-09-09）：
        方框會把使用者根本不會飛過去的整排樓也建出來，而真正要看的是
        「我這條線旁邊有什麼」。
        """
        if not path:
            return []
        lats = [p[0] for p in path]
        lons = [p[1] for p in path]
        pad = buffer_m / geo.M_PER_DEG_LAT * 1.5
        padl = pad / max(0.1, math.cos(math.radians(lats[0])))
        out = []
        for b in self.items:
            if (b.bbox[2] < min(lats) - pad or b.bbox[0] > max(lats) + pad
                    or b.bbox[3] < min(lons) - padl or b.bbox[1] > max(lons) + padl):
                continue
            d = path_dist_m(b, path)
            if d <= buffer_m:
                out.append((b, round(d, 1)))
        return sorted(out, key=lambda x: x[1])

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
