"""航點停留：規劃要停多久，**實際停了哪一段**（issues/062）。

使用者 2026-09-21 裁定：停留期間的樣本要標記。停 30 秒＝那個點上多出約 30 筆
樣本、沿航線里程全都一樣；不標的話，分析端分不出「機在這裡停了 30 秒」與
「這裡的樣本特別密」。**呈現要與事實貼齊**——所以這裡標的是**量到的**那一段，
不是規劃說的那一段。

**不能拿 `waypoint_reached` 當「到達」**（2026-09-22 用 09-21 兩趟實飛對出來的）：
ArduCopter 4.7 在這條航線上，那則事件是**停留結束**時才來，而且報的序號是停留點
**後面那一項**（改速度指令）——地速顯示 15:45:58～15:46:06 停著，事件在 15:46:06.96。
照「事件＝到達」去配，每一段都會標在停留之後、機已經在飛的那幾秒上。

所以：
* **是哪一點**：取機端序號不超過事件序號的**最後一個** `NAV_WAYPOINT`——事件報的
  是航點本身或它後面那一項，都對得上
* **是哪一段時間**：事件前後 `NEAR_S` 內、地速連續低於 `STOP_MS` 的那一段，往兩邊
  延伸到開始動為止。**不假設事件是起點還是終點**——換一個韌體語意可能相反
* 找不到停著的那段 → 照實說「規劃要停，但沒觀察到」
"""
from datetime import timedelta

#: 地速低於這個算「停著」。多旋翼定點時 GPS 地速在 0.0x～0.2 之間抖
STOP_MS = 0.3
#: 事件前後多遠內要找得到停著的樣本
NEAR_S = 3.0
_WAYPOINT = 16


def _cmd(w):
    return w.get("command") if w.get("command") is not None else (w.get("params") or {}).get("command")


def _p1(w):
    v = w.get("p1")
    if v is None:
        v = (w.get("params") or {}).get("p1")
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def planned(items):
    """規劃裡有停留的航點：[(我方 seq, 秒數)]。"""
    return [(w["seq"], _p1(w)) for w in items
            if _cmd(w) == _WAYPOINT and _p1(w) > 0]


def nav_for_event(items, wire_offset, ev_seq):
    """這則「到點」事件講的是我方哪一個航點：機端序號不超過事件序號的最後一個
    `NAV_WAYPOINT`。回我方 seq，對不上回 None。"""
    best = None
    for w in items:
        if _cmd(w) != _WAYPOINT:
            continue
        if w["seq"] + wire_offset <= ev_seq:
            if best is None or w["seq"] > best:
                best = w["seq"]
    return best


def stationary_window(speeds, t, near_s=NEAR_S, stop_ms=STOP_MS):
    """`speeds`：依時間排序的 [(時刻, 地速)]。回 `(起, 迄)` 或 None。

    從事件前後 `near_s` 內最靠近事件的那筆「停著」的樣本出發，往兩邊延伸到
    第一筆「在動」或沒有地速的樣本為止。**邊界的解析度就是遙測的間隔**（約 1 秒）。
    """
    lo, hi = t - timedelta(seconds=near_s), t + timedelta(seconds=near_s)
    cand = [i for i, (ts, v) in enumerate(speeds)
            if lo <= ts <= hi and v is not None and v < stop_ms]
    if not cand:
        return None
    i0 = min(cand, key=lambda i: abs((speeds[i][0] - t).total_seconds()))
    a = b = i0
    while a > 0 and speeds[a - 1][1] is not None and speeds[a - 1][1] < stop_ms:
        a -= 1
    while b < len(speeds) - 1 and speeds[b + 1][1] is not None and speeds[b + 1][1] < stop_ms:
        b += 1
    return speeds[a][0], speeds[b][0]


def measure(items, wire_offset, reached, speeds):
    """每一個規劃了停留的點，實際停了哪一段。

    `items`：這條航線的航點（`seq`／`command`／`p1` 或 `params`）。
    `wire_offset`：我方 seq → 機端序號要加多少（驅動的 `wire_seq(0)`）；
    None＝不認得自駕儀，對不上。
    `reached`：[(時刻, 機端序號)]，`waypoint_reached` 事件。
    `speeds`：[(時刻, 地速)]，依時間排序。

    回 [{seq, planned_s, observed, started_at, ended_at, seconds, note}]，
    同一點在一趟裡停了兩次（例如中斷後繼續）就有兩筆。
    """
    plan = planned(items)
    if not plan:
        return []
    out = []
    if wire_offset is None:
        return [{"seq": s, "planned_s": p, "observed": False,
                 "started_at": None, "ended_at": None, "seconds": None,
                 "note": "不認得這台的自駕儀，對不上機端的航點序號"} for s, p in plan]
    by_seq = {}
    for t, e in reached:
        n = nav_for_event(items, wire_offset, e)
        if n is not None:
            by_seq.setdefault(n, []).append(t)
    for s, p in plan:
        times = by_seq.get(s)
        if not times:
            out.append({"seq": s, "planned_s": p, "observed": False,
                        "started_at": None, "ended_at": None, "seconds": None,
                        "note": "規劃要停，但這一趟沒有收到飛到這一點的事件"})
            continue
        for t in times:
            win = stationary_window(speeds, t)
            if win is None:
                out.append({"seq": s, "planned_s": p, "observed": False,
                            "started_at": None, "ended_at": None, "seconds": None,
                            "note": f"有飛到這一點的事件，但前後 {NEAR_S:g} 秒內沒有"
                                    f"地速低於 {STOP_MS:g} m/s 的樣本——看不出有停"})
                continue
            a, b = win
            sec = round((b - a).total_seconds(), 1)
            out.append({"seq": s, "planned_s": p, "observed": True,
                        "started_at": a, "ended_at": b, "seconds": sec,
                        "note": None if sec >= 0.5 * p else
                        f"量到的停留（{sec:g} 秒）不到規劃的一半"})
    return out
