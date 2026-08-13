"""模擬 5G 鏈路品質。**這是開發鷹架，不是產品的一部分。**

**目的：確保本系統的功能正常。** SITL 沒有真的 5G modem，需要有東西餵進管線，
才能驗證取樣 → live state → 事件 → 入庫 → WebSocket → 前端這條路徑真的會動。
判準是「有沒有走到與真機相同的程式路徑」，不是「數值像不像真的」。

本系統的職責是控制資料蒐集與記錄事實，不做通道模擬。因此這裡**不追求擬真**：
夠合理讓開發看得出因果、不產生會誤導開發者的假象即可。
不要在這裡投資 3GPP 等級的建模（A3 offset、time-to-trigger、L3 濾波那一套）。

模型（刻意簡單，重點是讓「位置 → 訊號品質」的因果關係可控、可重現）：
  1. 對每個 gNB 以 log-distance path loss 估 RSRP，取最強者為服務 cell。
     現任服務 cell 有 handover_margin_db 的優勢，避免雜訊造成每秒抖動。
  2. SINR 由 RSRP 線性映射，落在干擾區內時額外扣 severity_db（區緣有漸變）。
  3. RTT / jitter / 丟包率 / 吞吐量由 SINR 推導 — 真機階段換成實測值。

只輸出 `pci` 不輸出 `cell_id`：`cell_id` 的語意是 modem 回報的全域 cell 識別碼
（NCI/CGI），模擬器產生不出有意義的值，留 NULL 才是誠實的。模擬階段用 pci
就足以識別服務 cell。

真機階段：改用 ModemLinkSource（modem 讀 RF 指標 + ping 實測 RTT/丟包）。
注意介面可能要從 pull（sample()）改成 push——機上跑 ROS，量測的自然採集點在
機上，見 doc/architecture.md 的「5G 量測資料如何從機上回到地面站」。
"""
import math
import random


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# 模擬場景（開發鷹架自帶）：兩個 gNB＋一個強干擾源。
# 原為 cells / interference_zones 兩張表，2026-08-10 拆除——場景是模擬器的
# 內部細節，不佔正式 schema；真機階段的「已知干擾源」由實測資料自己揭露
# （樣本帶 cell_id/PCI 與座標，事後可歸因，不需要預先標注表）。
# **場景座標必須跟著機隊出生點走**（2026-08-13 從蘇黎世搬到臺北大稻埕）。
# 機在台北、干擾區留在蘇黎世的話，模擬 5G 會永遠平坦——SINR 不會掉、
# `in_interference_zone` 永遠 false，**整個研究情境靜默失效**：畫面一切正常，
# 只是那條「飛進干擾區訊號變差」的曲線再也不會出現。
#
# 搬遷時保持**相對幾何**（相對 home 的距離與方位不變），而不是平移經緯度差值
# ——經度一度的長度隨緯度變（蘇黎世 47.4° vs 臺北 25.06°，差約 25%），
# 直接搬差值會把場景橫向壓扁 25%。換算成公尺再套用才對：
#
#   sim-gnb-1     93.9m @208.5°   sim-gnb-2    444.3m @ 35.3°
#   sim-jammer-A 195.7m @  0.1°（半徑 120m，涵蓋 home 正北 76~316m）
#
# 出生點若再變（`FLEET_HOME`），這裡要一起換算——**兩者是同一個場景的兩半**。
_HOME = (25.0554, 121.5065)          # 與 sim-fleet/fleet.sh 的 FLEET_HOME 對齊

DEFAULT_CELLS = [
    {"name": "sim-gnb-1", "lat": 25.054658, "lon": 121.506056, "pci": 101, "band": "n78"},
    {"name": "sim-gnb-2", "lat": 25.058658, "lon": 121.509045, "pci": 205, "band": "n78"},
]
DEFAULT_ZONES = [
    {"name": "sim-jammer-A", "center_lat": 25.057158, "center_lon": 121.506504,
     "radius_m": 120.0, "severity_db": 25.0},   # 區內 SINR 最深 -25dB
]


class SimulatedLinkSource:
    def __init__(self, cells: list[dict] | None = None,
                 zones: list[dict] | None = None,
                 handover_margin_db: float = 6.0):
        self.cells = cells if cells is not None else DEFAULT_CELLS
        self.zones = zones if zones is not None else DEFAULT_ZONES
        self.handover_margin_db = handover_margin_db
        self._serving_pci: int | None = None

    def sample(self, lat: float, lon: float, alt_rel: float | None) -> dict:
        # 1. 量各 cell 的 RSRP
        measured = []
        for c in self.cells:
            d = _dist_m(lat, lon, c["lat"], c["lon"])
            rsrp = -55.0 - 25.0 * math.log10(max(d, 10.0) / 10.0) \
                   + (c.get("tx_power_dbm", 40.0) - 40.0) + random.gauss(0, 1.5)
            measured.append((c, rsrp))
        best, best_rsrp = max(measured, key=lambda m: m[1])

        # 2. 換手邊際：現任服務 cell 有優勢，候選要強過 margin 才換。
        #    沒有這道門檻的話，兩個 cell 距離相近時（RSRP 只差 ~1dB）純靠 ±1.5dB
        #    的雜訊就會每秒來回換手，產生大量不對應任何真實現象的 handover 事件。
        #    真機階段 PCI 由 modem 直接回報，這段不適用。
        serving = next((m for m in measured if m[0]["pci"] == self._serving_pci), None)
        if serving is not None and best_rsrp < serving[1] + self.handover_margin_db:
            best, best_rsrp = serving
        self._serving_pci = best["pci"]

        rsrp = _clamp(best_rsrp, -140.0, -50.0)

        # 2. RSRP → SINR，再套用干擾區衰減
        sinr = (rsrp + 105.0) * 0.45 + random.gauss(0, 0.8)
        in_zone = False
        for z in self.zones:
            d = _dist_m(lat, lon, z["center_lat"], z["center_lon"])
            if d < z["radius_m"]:
                in_zone = True
                edge = _clamp((z["radius_m"] - d) / (z["radius_m"] * 0.3), 0.0, 1.0)
                sinr -= z["severity_db"] * edge
        sinr = _clamp(sinr, -15.0, 35.0)

        # 3. SINR → 端到端品質
        rtt = 18.0 + max(0.0, 15.0 - sinr) * 6.0 + random.expovariate(1 / 3.0)
        jitter = 2.0 + max(0.0, 10.0 - sinr) * 1.5 + random.expovariate(1 / 1.0)
        loss = _clamp((3.0 - sinr) * 4.0, 0.0, 90.0) if sinr < 3.0 else _clamp(random.gauss(0.1, 0.1), 0.0, 1.0)
        thr_down = _clamp(sinr, 0.0, 30.0) / 30.0 * 400_000.0 * random.uniform(0.85, 1.0)

        return {
            "rsrp": round(rsrp, 1),
            "rsrq": round(_clamp(-8.0 - (30.0 - sinr) * 0.25, -20.0, -5.0), 1),
            "sinr": round(sinr, 1),
            "cqi": int(_clamp(round((sinr + 10.0) / 3.0), 1, 15)),
            "pci": best["pci"],
            "band": best.get("band", "n78"),
            "nr_mode": "SA",
            "rtt_ms": round(rtt, 1),
            "jitter_ms": round(jitter, 1),
            "packet_loss_pct": round(loss, 2),
            "throughput_up_kbps": round(thr_down / 4.0),
            "throughput_down_kbps": round(thr_down),
            "in_interference_zone": in_zone,
            "source": "simulated",
        }
