"""植被（樹、行道樹、樹林）——**標到的位置，沒有高度，而且不是全部。**

使用者要在即時畫面上看到「可能的障礙物，譬如樹」（2026-10-13）。樹確實是
障礙物，但這份資料與建物有一個關鍵差別，而那個差別決定它能怎麼畫：

| | 輪廓 | 高度 |
|---|---|---|
| 建物 | 場地這塊 **59 棟全有** | 9 棟有、50 棟無 |
| 植被 | 場地這塊 **只有 5 棵樹、3 排行道樹、3 片林** | **一個都沒有** |

（2026-10-13 向 Overpass 實查同一個 bbox 的結果。）

所以這份資料回答得了「這裡標了一棵樹」，回答不了「這裡只有這些樹」。
**沒有標記的地方不等於淨空**——這句話要跟著資料一起上畫面，否則使用者會
把它當成樹的清單，而那是最糟的一種錯：看起來查過了。

高度：`natural=tree` 偶爾會有 `height`，有就帶出去、沒有就是 None。
**不給預設值**，理由同 `buildings.py`——猜的高度在這個系統裡是撞機。
"""
from __future__ import annotations

import json
import math
import os

import geo

DATA_DIR = os.environ.get(
    "VEGETATION_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "vegetation"))

#: OSM 標籤 → 這裡的分類。**只收這幾種**：其餘植被標籤（草地、花圃）
#: 不是障礙物，畫上去只會讓真正該看的東西更難找。
KINDS = {
    ("natural", "tree"): "tree",            # 單棵（點）
    ("natural", "tree_row"): "tree_row",    # 一排（線）
    ("natural", "wood"): "wood",            # 林（面）
    ("landuse", "forest"): "wood",
    ("natural", "scrub"): "scrub",          # 灌叢（面）——矮，但有
    ("landuse", "orchard"): "orchard",      # 果園（面）
}

#: 給畫面用的中文名。**在這裡只有一份**，前端不自己再翻一次
LABELS = {"tree": "樹", "tree_row": "行道樹", "wood": "樹林",
          "scrub": "灌叢", "orchard": "果園"}


def classify(tags: dict) -> str | None:
    for (k, v), kind in KINDS.items():
        if tags.get(k) == v:
            return kind
    return None


def _coords(geom: dict):
    """走過一個 geometry 的所有 (lat, lon)。型別不認得就回空。"""
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Point" and isinstance(c, list) and len(c) >= 2:
        return [(float(c[1]), float(c[0]))]
    if t == "LineString":
        return [(float(p[1]), float(p[0])) for p in c or [] if len(p) >= 2]
    if t == "Polygon":
        return [(float(p[1]), float(p[0])) for p in (c or [[]])[0] if len(p) >= 2]
    return []


def _in_ring(lat: float, lon: float, ring) -> bool:
    """射線法。與 `buildings._in_ring`／`plan_check._in_polygon` 同一個做法。"""
    inside = False
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            if lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0,
                                         ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _dist_to_geom(lat: float, lon: float, geom: dict) -> float:
    """這一點離這個幾何最近有多遠（公尺）。在面裡面就是 0。

    先換成以查詢點為原點的 ENU 再算平面距離——經緯度直接當平面會在緯度上
    失真（同 `buildings.path_dist_m`）。
    """
    pts = _coords(geom)
    if not pts:
        return float("inf")
    t = geom.get("type")
    if t == "Point":
        return geo.dist_m(lat, lon, pts[0][0], pts[0][1])
    if t == "Polygon" and _in_ring(lat, lon, pts):
        return 0.0
    enu = [geo.to_enu(la, lo, lat, lon) for la, lo in pts]
    if t == "Polygon":
        enu = enu + [enu[0]]              # 收口：最後一條邊也要量
    best = float("inf")
    for (ax, ay), (bx, by) in zip(enu, enu[1:]):
        best = min(best, _seg_dist(0.0, 0.0, ax, ay, bx, by))
    return best


class Store:
    """`data/vegetation/*.geojson` 讀進來的一份。找不到檔案就是空的。"""

    def __init__(self, path: str | None = None):
        self.dir = path or DATA_DIR
        #: (feature, (min_lat, min_lon, max_lat, max_lon))
        self.items: list[tuple[dict, tuple[float, float, float, float]]] = []
        self.files: list[str] = []
        #: 每個檔案宣告的涵蓋範圍（西,南,東,北）。**「那裡沒有樹」與「我們
        #: 沒抓過那一區」是兩件事**——同 buildings.Store.areas
        self.areas: list[tuple[float, float, float, float]] = []
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
            bb = fc.get("bbox")
            if isinstance(bb, list) and len(bb) == 4:
                self.areas.append(tuple(float(v) for v in bb))
            for ft in fc.get("features", []):
                pts = _coords(ft.get("geometry") or {})
                if not pts:
                    continue
                lats = [p[0] for p in pts]
                lons = [p[1] for p in pts]
                self.items.append(
                    (ft, (min(lats), min(lons), max(lats), max(lons))))

    @property
    def available(self) -> bool:
        return bool(self.items)

    def covers(self, bbox) -> bool:
        """有沒有**抓過**這個範圍（南,西,北,東）。不是「這裡有沒有樹」。"""
        want_s, want_w, want_n, want_e = bbox
        for west, south, east, north in self.areas:
            if not (east < want_w or west > want_e
                    or north < want_s or south > want_n):
                return True
        return False

    def near(self, lat: float, lon: float,
             radius_m: float = 300.0) -> list[tuple[dict, float]]:
        """這一點 `radius_m` 以內的植被，近的排前面。

        **距離量到幾何本身，不是外接框。** 一開始用外接框夾出最近點（快，
        而且足以篩掉遠的），但那個數字不能當答案：場地那片林的外接框罩住
        半個場地，查詢點明明在林子外，卻回「0 m」——讀起來就是「你在林子
        裡」。面用射線法判斷在不在裡面，不在就量到最近的一條邊；線量到最近
        的線段；點就是點。

        這個距離只用來排序與顯示遠近，**不拿來做放行判斷**——要判斷的前提
        是高度，而這裡的高度一個都沒有。
        """
        out = []
        for ft, bb in self.items:
            s, w, n, e = bb
            # 粗篩：外接框夾出最近點。框距已經超過半徑，真實距離只會更遠
            clat = min(max(lat, s), n)
            clon = min(max(lon, w), e)
            if geo.dist_m(lat, lon, clat, clon) > radius_m:
                continue
            d = _dist_to_geom(lat, lon, ft.get("geometry") or {})
            if d > radius_m:
                continue
            out.append((ft, round(d, 1)))
        return sorted(out, key=lambda x: x[1])


_shared: Store | None = None


def shared() -> Store:
    global _shared
    if _shared is None:
        _shared = Store()
    return _shared
