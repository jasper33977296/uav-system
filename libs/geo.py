"""經緯度 ↔ 公尺。**Python 這一側唯一的換算處。**

原本住在 `plan_check`，但建物那一層也要用，而 `plan_check` 反過來 import
建物——放在那裡會變成迴圈，各自複製一份就會變成第三份
（`doc/field-3d-model-design.md` §9-G 早就標過這個風險）。
前端的 `lib/geo.ts` 用同一組值。
"""
import math

#: 一度緯度／赤道上一度經度的公尺數。**兩個不一樣**——111320 是後者，
#: 拿它乘緯度會高估 0.67%（400 m 上約 2.7 m）。
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON_EQ = 111320.0


def m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LON_EQ * math.cos(math.radians(lat))


def dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """兩點的水平距離（公尺）。"""
    dy = (lat2 - lat1) * M_PER_DEG_LAT
    dx = (lon2 - lon1) * m_per_deg_lon(lat1)
    return math.hypot(dx, dy)


def to_enu(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """以 (lat0, lon0) 為原點的平面座標（東, 北），公尺。

    只在**幾百公尺**的尺度上用——場域級的長寬計算夠了，跨縣市不行。
    """
    return ((lon - lon0) * m_per_deg_lon(lat0), (lat - lat0) * M_PER_DEG_LAT)
