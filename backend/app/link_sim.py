"""模擬 5G 鏈路品質。

模型（刻意簡單，重點是讓「位置 → 訊號品質」的因果關係可控、可重現）：
  1. 對每個 gNB 以 log-distance path loss 估 RSRP，取最強者為服務 cell
     （服務 cell 改變即為 handover）。
  2. SINR 由 RSRP 線性映射，落在干擾區內時額外扣 severity_db（區緣有漸變）。
  3. RTT / jitter / 丟包率 / 吞吐量由 SINR 推導 — 這是研究上「RF 劣化
     如何反映到端到端品質」的模擬版本，真機階段換成實測值。

真機階段：新增 ModemLinkSource（AT command / QMI 讀 modem + ping 實測），
與本類別實作相同的 sample() 介面即可，其餘程式碼不動。
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


class SimulatedLinkSource:
    def __init__(self, cells: list[dict], zones: list[dict]):
        self.cells = cells
        self.zones = zones

    def sample(self, lat: float, lon: float, alt_rel: float | None) -> dict:
        # 1. 選最強 cell
        best, best_rsrp = None, -999.0
        for c in self.cells:
            d = _dist_m(lat, lon, c["lat"], c["lon"])
            rsrp = -55.0 - 25.0 * math.log10(max(d, 10.0) / 10.0) \
                   + (c.get("tx_power_dbm", 40.0) - 40.0) + random.gauss(0, 1.5)
            if rsrp > best_rsrp:
                best, best_rsrp = c, rsrp
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
            "cell_id": best["id"],
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
