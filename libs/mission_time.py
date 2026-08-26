"""航線預計時間：從航點幾何與 `.plan` 宣告的速度估算。

**這是估計值，而且要說得出估了什麼、沒估什麼。** 一個沒有標示前提的時間數字
會被當成承諾——使用者拿它安排電池、安排場地時段，而它其實不含風、不含加減速。

估算的組成：
  * 水平段：距離 ÷ 巡航速度（`.plan` 的 `cruiseSpeed`，可被 `DO_CHANGE_SPEED`
    中途改掉）
  * 垂直段：高度差 ÷ 懸停/爬升速度（`hoverSpeed`）
  * 起飛：從 0 爬到起飛高度
  * 停留：`NAV_WAYPOINT` 的 param1、`NAV_LOITER_TIME` 的 param1（秒）
  * 返航：最後一點回到 home 再下降（RTL／LAND 沒有座標，要靠 home）

水平與垂直**取大者不相加**：多旋翼是一邊平飛一邊爬升的，相加會高估。

**速度沒宣告就回 None，不給預設值**——與圍欄同一條原則：不知道就說不知道，
給一個猜的數字比空白危險，因為它看起來像是算出來的。
"""
import math

_NAV = {16, 17, 18, 19, 20, 21, 22, 82, 84, 85}
_TAKEOFF, _RTL, _LAND = 22, 20, 21
_LOITER_TIME = 19
_DO_CHANGE_SPEED = 178


def _cmd(w):
    if w.get("command") is not None:
        return int(w["command"])
    return {"takeoff": 22, "land": 21, "rtl": 20,
            "waypoint": 16}.get(w.get("action") or "waypoint")


def _dist_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def estimate(wps: list[dict], cruise: float | None, hover: float | None,
             home: list[float] | None = None) -> dict:
    """回傳 {seconds, assumptions, unknown}。算不出來時 seconds=None。"""
    unknown = []
    if not cruise or cruise <= 0:
        unknown.append("這份 .plan 沒宣告巡航速度（cruiseSpeed）")
    if not hover or hover <= 0:
        unknown.append("這份 .plan 沒宣告爬升速度（hoverSpeed）")
    if unknown:
        # **不給預設速度**：猜一個看起來合理的數字，使用者會拿它安排電池
        return {"seconds": None, "assumptions": [], "unknown": unknown}

    origin = ({"lat": home[0], "lon": home[1]}
              if home and len(home) >= 2 and (home[0] or home[1]) else None)
    speed = cruise
    total = 0.0
    hold_s = 0.0
    prev = None          # 上一個有座標的點 {lat, lon, alt}
    prev_alt = 0.0
    changed_speed = False
    saw_rtl = False

    for w in wps:
        c = _cmd(w)
        if c == _DO_CHANGE_SPEED:
            v = w.get("p2")
            if v and float(v) > 0:
                speed = float(v)
                changed_speed = True
            continue
        if c is not None and c not in _NAV:
            continue
        alt = float(w.get("alt") or 0)
        if c == _TAKEOFF:
            total += abs(alt - prev_alt) / hover
            prev_alt = alt
            if w.get("lat") and w.get("lon"):
                prev = {"lat": w["lat"], "lon": w["lon"]}
            elif origin:
                prev = origin
            continue
        if c in (_RTL, _LAND):
            saw_rtl = True
            if prev and origin:
                total += max(_dist_m(prev["lat"], prev["lon"],
                                     origin["lat"], origin["lon"]) / speed,
                             0.0)
            total += prev_alt / hover          # 降落
            prev_alt = 0.0
            continue
        if not (w.get("lat") and w.get("lon")):
            continue
        if prev:
            h = _dist_m(prev["lat"], prev["lon"], w["lat"], w["lon"]) / speed
            v = abs(alt - prev_alt) / hover
            total += max(h, v)                 # 一邊平飛一邊爬，取大者不相加
        prev = {"lat": w["lat"], "lon": w["lon"]}
        prev_alt = alt
        # 停留秒數：NAV_WAYPOINT 的 param1、NAV_LOITER_TIME 的 param1
        if c in (16, _LOITER_TIME):
            p1 = w.get("p1")
            if p1 and float(p1) > 0:
                hold_s += float(p1)

    assumptions = [
        f"巡航 {cruise:g} m/s、爬升/下降 {hover:g} m/s（來自 .plan）",
        "水平與垂直取大者不相加（多旋翼可同時平飛與爬升）",
        "**不含風、不含加減速、不含起飛前的解鎖與檢查時間**",
    ]
    if changed_speed:
        assumptions.append("航線中有 DO_CHANGE_SPEED，後段用改過的速度算")
    if hold_s:
        assumptions.append(f"含航點停留合計 {hold_s:.0f} 秒")
    if saw_rtl and not origin:
        assumptions.append(
            "**返航那一段沒算進去**：這份航線沒有 plannedHomePosition，"
            "不知道要飛回哪裡")
    return {"seconds": round(total + hold_s, 1), "assumptions": assumptions,
            "unknown": []}


def human(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}分{s:02d}秒" if m else f"{s}秒"
