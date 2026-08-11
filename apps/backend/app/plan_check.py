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


# 導航類指令（有實際飛行位置）；DO_*（如 178 改速度）是設定類，
# 可出現在任何位置、不計距離——與現場工具 check_plan.py 的語意一致
NAV_CMDS = {16, 17, 18, 19, 20, 21, 22}
_TAKEOFF, _RTL, _LAND = 22, 20, 21


def _cmd(w: dict) -> int | None:
    """航點的 MAV_CMD：新資料帶原始 command；舊資料從 action 推回。"""
    if w.get("command") is not None:
        return int(w["command"])
    return {"takeoff": 22, "land": 21, "rtl": 20,
            "waypoint": 16}.get(w.get("action") or "waypoint")


def _is_nav(w: dict) -> bool:
    c = _cmd(w)
    return c is None or c in NAV_CMDS


def check_waypoints(wps: list[dict], fence_r: float, fence_alt: float,
                    margin: float = 0.7) -> dict:
    """wps：本系統 waypoints 模型 [{seq, lat, lon, alt, action, command?}]。
    DO_* 設定類不計距離；回傳 {ok, problems, warnings, max_dist_m, ...}。"""
    problems: list[str] = []
    warnings: list[str] = []
    if not wps:
        return {"ok": False, "problems": ["沒有航點"], "warnings": [],
                "max_dist_m": 0.0, "fence_r": fence_r, "fence_alt": fence_alt}

    nav = [w for w in wps if _is_nav(w)]
    if not nav:
        problems.append("沒有任何導航項目")
    else:
        if _cmd(nav[0]) != _TAKEOFF:
            problems.append(f"第一個導航項不是起飛（command={_cmd(nav[0])}）")
        if _cmd(nav[-1]) not in (_RTL, _LAND):
            warnings.append(f"最後一個導航項不是返航/降落（command={_cmd(nav[-1])}）")

    home = next((w for w in nav if w.get("lat") and w.get("lon")), None)
    if home is None:
        return {"ok": False, "problems": problems + ["找不到帶座標的導航項"],
                "warnings": warnings, "max_dist_m": 0.0,
                "fence_r": fence_r, "fence_alt": fence_alt}

    max_d = 0.0
    for w in nav:
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


def check_group(paths: list[dict], vsep_m: float, lsep_m: float) -> dict:
    """群組跨路徑互檢（issue 013-A）：N 條同時飛的路徑要分離足夠。
    paths：[{label, waypoints:[{lat,lon,alt}]}]。兩條路徑若在某處**橫向 < lsep
    且垂直 < vsep**＝衝突（都靠太近才危險，分層或分離任一夠即安全）。
    unified 高度分層下垂直本就 ≥ vsep，天然通過；separate 才真的互檢。"""
    conflicts = []
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            a, b = paths[i], paths[j]
            wa = [w for w in a.get("waypoints", []) if w.get("lat") and w.get("lon")]
            wb = [w for w in b.get("waypoints", []) if w.get("lat") and w.get("lon")]
            hit = None
            for pa in wa:
                for pb in wb:
                    dh = _dist_m(pa["lat"], pa["lon"], pb["lat"], pb["lon"])
                    dv = abs((pa.get("alt") or 0) - (pb.get("alt") or 0))
                    if dh < lsep_m and dv < vsep_m:
                        hit = (round(dh), round(dv))
                        break
                if hit:
                    break
            if hit:
                conflicts.append({
                    "a": a.get("label"), "b": b.get("label"),
                    "why": f"最近處 橫向 {hit[0]}m／垂直 {hit[1]}m"
                           f"（門檻 橫向 {lsep_m:.0f}m 或 垂直 {vsep_m:.0f}m）"})
    return {"ok": not conflicts, "conflicts": conflicts}
