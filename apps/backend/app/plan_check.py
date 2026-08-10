"""任務幾何預檢：離線檢查路徑 vs Geofence，在上傳前抓出 PX4 會拒絕的任務。

源自現場工具 check_plan.py（2026-08-10 整合進系統）。要點：

  1. 各航點離起飛點距離 vs 圍欄半徑——含餘裕警告：**實際的 Home 是
     「無人機擺放位置」，不是圖上的起飛點**，貼著圍欄邊的路徑只要
     擺放偏一點就會觸圈，故 >70% 半徑即警告
  2. 高度 vs 圍欄高度上限
  3. 首導航項應為起飛（problem）；末項應為降落（warning——.plan 以
     RTL 結尾時 RTL 不入庫，屬正常）

兩個消費端、兩種嚴格度：
  - POST /missions（匯入 .plan）：回報告**不擋存檔**——任務庫可放草稿
  - command 服務上傳到機：有 problem 直接 409——那才是安全門

（command 服務有同源副本 apps/command/app/plan_check.py，改動要同步；
 圍欄值兩服務共用 .env 的 GEOFENCE_*，與機上 QGC 設定保持一致。）
"""
import math


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 111320.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def check_waypoints(wps: list[dict], fence_r: float, fence_alt: float,
                    margin: float = 0.7) -> dict:
    """wps：本系統 waypoints 模型 [{seq, lat, lon, alt, action}]（帶座標導航項）。
    回傳 {ok, problems, warnings, max_dist_m, fence_r, fence_alt}。"""
    problems: list[str] = []
    warnings: list[str] = []
    if not wps:
        return {"ok": False, "problems": ["沒有航點"], "warnings": [],
                "max_dist_m": 0.0, "fence_r": fence_r, "fence_alt": fence_alt}

    if (wps[0].get("action") or "waypoint") != "takeoff":
        problems.append(f"第一個導航項不是起飛（action={wps[0].get('action')}）")
    last = wps[-1].get("action") or "waypoint"
    if last != "land":
        warnings.append(f"最後一項不是降落（action={last}）——"
                        "若 .plan 以 RTL 結尾屬正常（RTL 不入庫）")

    home = next((w for w in wps if w.get("lat") and w.get("lon")), None)
    if home is None:
        return {"ok": False, "problems": ["找不到帶座標的航點"], "warnings": warnings,
                "max_dist_m": 0.0, "fence_r": fence_r, "fence_alt": fence_alt}

    max_d = 0.0
    for w in wps:
        seq = w.get("seq")
        if w.get("lat") and w.get("lon"):
            d = _dist_m(home["lat"], home["lon"], w["lat"], w["lon"])
            max_d = max(max_d, d)
            if d > fence_r:
                problems.append(
                    f"seq {seq} 離起飛點 {d:.0f} m，超過圍欄半徑 {fence_r:.0f} m")
            elif d > fence_r * margin:
                warnings.append(
                    f"seq {seq} 距離 {d:.0f} m 貼近圍欄（>{fence_r * margin:.0f} m）"
                    "——實際 Home 是擺放位置，請把無人機擺在起飛點 10 m 內")
        alt = w.get("alt")
        if alt is not None and float(alt) > fence_alt:
            problems.append(
                f"seq {seq} 高度 {float(alt):.0f} m 超過上限 {fence_alt:.0f} m")

    return {"ok": not problems, "problems": problems, "warnings": warnings,
            "max_dist_m": round(max_d, 1), "fence_r": fence_r, "fence_alt": fence_alt}
