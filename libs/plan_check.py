"""任務幾何預檢：離線檢查路徑 vs 圍欄與方言規則，在上傳前抓出機端會拒絕的任務。

源自現場工具 check_plan.py（2026-08-10 整合進系統）。要點：

  1. **無座標項的 frame 是方言**：由 `libs/autopilot` 的驅動提供可接受值。
     不知道目標機種時只警告不擋——用猜的去否定一份可能合法的航線更糟。
  2. 圍欄**優先用 .plan 自帶的 geoFence**，沒帶才退回系統預設，而且報告
     要說出用的是哪一個（`fence_source`）。
  3. 高度上限永遠是系統設定：QGC 的 geoFence 只畫平面。
  4. 首導航項應為起飛（problem）；末項應為降落（warning——.plan 以 RTL
     結尾時 RTL 不入庫，屬正常）。

兩個消費端、兩種嚴格度：
  - POST /missions（匯入 .plan）：回報告**不擋存檔**——任務庫可放草稿
  - command 服務上傳到機：有 problem 直接 409——那才是安全門

**這裡是唯一一份實作。** 原本 backend 與 command 各有一份「同源副本」，
2026-08-26 發現它們早就漂移了：frame 檢查只存在於 backend 那份，於是
匯入時擋下來的東西，上傳到機時反而不擋。同源副本靠人記得同步是行不通的。
"""
import math

import autopilot as _autopilot


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
                    margin: float = 0.7, fence: dict | None = None,
                    autopilot: int | None = None,
                    home: list[float] | None = None) -> dict:
    """wps：本系統 waypoints 模型 [{seq, lat, lon, alt, action, command?}]。
    DO_* 設定類不計距離；回傳 {ok, problems, warnings, max_dist_m, ...}。"""
    problems: list[str] = []
    warnings: list[str] = []
    if not wps:
        return {"ok": False, "problems": ["沒有航點"], "warnings": [],
                "max_dist_m": 0.0, "max_alt_m": 0.0, "fence_source": "none"}

    # 無座標項（RTL、CONDITION_*、DO_*）的 frame **是方言，不是通則**。
    #
    # 2026-08-12 在 PX4 SITL 實測「RTL 配 frame 0/3/5/6 全拒、只有 2 過」，
    # 那條結果被當成通則寫死在這裡。2026-08-26 它擋下了一份**從 ArduPilot
    # 自己下載回來的**任務——ArduPilot 存 RTL 用 frame 0，下載時原樣回報，
    # 於是我們拿 PX4 的規則去否定 ArduPilot 自己的表示法。
    # **一家的實測結果被當成兩家的事實**，正是 issues/026 要收掉的洩漏。
    #
    # 現在規則由驅動提供；**不知道目標機種時只警告不擋**——兩家不同的事，
    # 在不知道是哪一家的情況下擋下來，等於用猜的去否定一份可能完全合法的航線。
    drv = _autopilot.get_driver(autopilot) if autopilot is not None else None
    allowed = drv.no_coord_frames if drv else None
    for w in wps:
        c, fr = _cmd(w), w.get("frame")
        if c is None or fr is None:
            continue
        if not (c == _RTL or c >= 112):
            continue
        if allowed is None:
            if int(fr) != 2:
                warnings.append(
                    f"seq {w.get('seq')}：command={c} 是無座標項、frame={fr}。"
                    "PX4 只吃 frame 2（其餘整包拒收），ArduPilot 兩者都吃——"
                    "**這份航線沒宣告目標機種，所以無法判定**")
        elif int(fr) not in allowed:
            problems.append(
                f"seq {w.get('seq')}：command={c} 是無座標項，frame 目前是 {fr}，"
                f"但 {_autopilot.autopilot_name(autopilot)} 只接受 "
                f"{sorted(allowed)}——機端會拒收整包任務")

    nav = [w for w in wps if _is_nav(w)]
    if not nav:
        problems.append("沒有任何導航項目")
    else:
        if _cmd(nav[0]) != _TAKEOFF:
            problems.append(f"第一個導航項不是起飛（command={_cmd(nav[0])}）")
        if _cmd(nav[-1]) not in (_RTL, _LAND):
            warnings.append(f"最後一個導航項不是返航/降落（command={_cmd(nav[-1])}）")

    # 距離的原點優先用 **.plan 的 plannedHomePosition**：起飛項在很多 .plan
    # 裡是 0,0（起飛只需要高度），拿「第一個有座標的航點」當原點會把整條
    # 航線的距離量錯一整段
    origin = ({"lat": home[0], "lon": home[1]} if home and len(home) >= 2
              and (home[0] or home[1]) else None)
    home = origin or next((w for w in nav if w.get("lat") and w.get("lon")), None)
    if home is None:
        return {"ok": False, "problems": problems + ["找不到帶座標的導航項"],
                "warnings": warnings, "max_dist_m": 0.0, "max_alt_m": 0.0,
                "fence_source": fence_src}

    # **圍欄優先用航線自己宣告的**（QGC .plan 的 geoFence）。系統預設值是
    # 「這套系統只在一個場地飛」才成立的假設，而測繪任務與定點巡檢的合理範圍
    # 可以差一個數量級。沒宣告才退回預設，而且訊息要說出用的是哪一個——
    # 使用者必須分得出「這份航線宣告了 N m 而你超出」與「這份沒宣告，
    # 我拿系統預設在量」，後者多半代表**預設值該改，不是航線該改**
    fence_src = "plan" if fence else "none"   # none＝這份沒宣告，系統也不替它設
    if fence:
        fp, fw = check_fence(nav, fence)
        problems += fp
        warnings += fw

    # **沒有系統預設圍欄**（使用者裁定 2026-08-26）。圍欄是每份航線自己的事，
    # 一個全域數字只對一個場地成立——而它會產生**看起來很具體的假錯誤**：
    # 「seq 6 離起飛點 54 m，超過圍欄半徑 50 m」讀起來像航線有問題，
    # 實際上那 50 是模擬環境留下來的值，跟使用者的場地毫無關係。
    #
    # 沒宣告圍欄時只報事實（最遠多少、最高多少），不判對錯——**我們不知道
    # 這個場地允許飛多遠，就不要假裝知道**。
    max_d, max_alt = 0.0, 0.0
    for w in nav:
        if w.get("lat") and w.get("lon"):
            max_d = max(max_d, _dist_m(home["lat"], home["lon"],
                                       w["lat"], w["lon"]))
        alt = w.get("alt")
        if alt is not None:
            max_alt = max(max_alt, float(alt))

    if not fence:
        warnings.append(
            f"這份航線沒有宣告圍欄（.plan 的 geoFence 是空的）——"
            f"最遠航點離起飛點 {max_d:.0f} m、最高 {max_alt:.0f} m，"
            "**系統不替你設一個範圍**，這條航線適不適合這個場地要你自己判斷。"
            "要讓系統幫你擋，在 QGC 的 Plan 頁畫一個 GeoFence 再存檔")
    return {"ok": not problems, "problems": problems, "warnings": warnings,
            "max_dist_m": round(max_d, 1), "max_alt_m": round(max_alt, 1),
            # **量測用的是哪一份圍欄，要跟著報告走**：同一句「超出圍欄」在
            # 兩種來源下的處置完全不同
            "fence_source": fence_src}


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


# ── .plan 自帶的圍欄（QGC geoFence）────────────────────────────────────
#
# **圍欄是每份航線自己的事，不是系統的全域設定。** 原本只有 .env 的
# GEOFENCE_RADIUS_M 一個值：那是「這套系統只在一個場地飛」才成立的假設，
# 而測繪任務與定點巡檢的合理範圍可以差一個數量級。
# QGC 的 .plan 本來就帶 geoFence（圓形／多邊形、含納／排除），讀它就好。
#
# **沒帶就退回系統預設，而且要說出來**：使用者看到「超過圍欄半徑 50 m」時，
# 必須分得出「這份航線宣告了 50 m 而你超出」與「這份航線沒宣告，我拿系統
# 預設值在量」——後者多半代表預設值該改，不是航線該改。

def fence_from_plan(plan: dict) -> dict | None:
    """QGC `.plan` 的 geoFence → 本系統的形狀。沒有可用的圍欄回 None。

    只取**含納**（inclusion）的圓與多邊形——那是「只准在裡面飛」的邊界。
    排除區（exclusion）是另一回事（不准進去的區域），另外查。
    """
    gf = (plan or {}).get("geoFence") or {}
    inc_c, exc_c, inc_p, exc_p = [], [], [], []
    for c in gf.get("circles") or []:
        cir = c.get("circle") or {}
        ctr = cir.get("center") or []
        if len(ctr) < 2 or not cir.get("radius"):
            continue
        item = {"lat": ctr[0], "lon": ctr[1], "radius": float(cir["radius"])}
        (inc_c if c.get("inclusion", True) else exc_c).append(item)
    for p in gf.get("polygons") or []:
        pts = [(v[0], v[1]) for v in (p.get("polygon") or []) if len(v) >= 2]
        if len(pts) < 3:
            continue
        (inc_p if p.get("inclusion", True) else exc_p).append(pts)
    if not (inc_c or exc_c or inc_p or exc_p):
        return None
    return {"inclusion_circles": inc_c, "exclusion_circles": exc_c,
            "inclusion_polygons": inc_p, "exclusion_polygons": exc_p}


def _in_polygon(lat: float, lon: float, poly) -> bool:
    """射線法。邊界上的點視為在內（圍欄邊上不該因為浮點誤差被判出界）。"""
    inside = False
    n = len(poly)
    for i in range(n):
        y1, x1 = poly[i]
        y2, x2 = poly[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xc = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < xc:
                inside = not inside
    return inside


def check_fence(wps: list[dict], fence: dict) -> tuple[list[str], list[str]]:
    """航點對 .plan 自帶圍欄的檢查。回傳 (problems, warnings)。"""
    problems, warnings = [], []
    inc_c = fence.get("inclusion_circles") or []
    inc_p = fence.get("inclusion_polygons") or []
    exc_c = fence.get("exclusion_circles") or []
    exc_p = fence.get("exclusion_polygons") or []
    for w in wps:
        if not (w.get("lat") and w.get("lon")):
            continue
        seq, la, lo = w.get("seq"), w["lat"], w["lon"]
        # 含納圍欄：**任一個含納區包住就算在內**（QGC 允許多個含納區）
        if inc_c or inc_p:
            ok = any(_dist_m(c["lat"], c["lon"], la, lo) <= c["radius"]
                     for c in inc_c) or any(_in_polygon(la, lo, p) for p in inc_p)
            if not ok:
                near = min((_dist_m(c["lat"], c["lon"], la, lo) - c["radius"]
                            for c in inc_c), default=None)
                extra = f"（最近的含納圓還差 {near:.0f} m）" if near is not None else ""
                problems.append(
                    f"seq {seq} 在航線自帶的含納圍欄之外{extra}")
        for c in exc_c:
            if _dist_m(c["lat"], c["lon"], la, lo) <= c["radius"]:
                problems.append(f"seq {seq} 落在航線自帶的排除圓內")
        for p in exc_p:
            if _in_polygon(la, lo, p):
                problems.append(f"seq {seq} 落在航線自帶的排除多邊形內")
    return problems, warnings
