#!/usr/bin/env python3
"""一鍵起飛「先飛到任務起始點」的 SITL 驗證（2026-09-21，issues/056）。

## 要驗的事

ArduCopter 的 NAV_TAKEOFF 忽略經緯度，而 `mission_fly` 先離地再切 AUTO，
所以起飛位置離航線第一個點有距離時，機會**直接飛往第二個點**。修法是在
「離地」與「切 AUTO」之間插一段 GUIDED 飛到起始點。這支跑**產品端真的那一份
`mission_fly`**（不是重寫一份），只把 DB／機上守門／入列換成樁：

  A. 起始點 60 m 外（< 門檻）   → 不必確認，飛到起始點才切 AUTO
  B. 起始點 150 m 外，沒確認    → 409 far_start，**機沒有解鎖**
  C. 同 B，帶確認的距離         → 飛過去
  B'. 帶了過期的確認距離        → 仍擋（確認的是那個數字）
  D. 起始點就在腳下              → 不另外飛（transit.skipped）
  E. 途中有人切 hold             → 序列停手，不切任務、不搶回 GUIDED

## 為什麼不走 HTTP

SITL 沒有板號，在本系統的規則下恆為 unmanaged（見 `sitl-fly-mission.py`）。
這裡直接呼叫端點函式，入列與能力 gate 換成放行樁——被驗的是序列本身。

## 跑法

    docker run -d --name sitl-startleg --network host -e INSTANCE=3 \\
        -e LAT=24.7814 -e LON=120.9947 -e ALT=100 radarku/ardupilot-sitl:latest
    docker run --rm --network host -e ENABLE_COMMANDS=true \\
        -v $PWD/apps/command/app:/srv/app -v $PWD/libs:/srv/libs \\
        -v $PWD/scripts:/srv/scripts -v $PWD/sim-fleet:/srv/sim-fleet \\
        uav-system-uav-command python /srv/scripts/sitl-fly-to-start.py
"""
import asyncio
import os
import subprocess
import sys
import time

sys.path[:0] = ["/srv", "/srv/libs"]

from fastapi import HTTPException                      # noqa: E402
from pymavlink.dialects.v20 import ardupilotmega as M  # noqa: E402

import plan_check                                      # noqa: E402
from app import guard_client, main, mav                # noqa: E402

SITL_TCP = os.environ.get("SITL_TCP", "127.0.0.1:5790")
UDP_PORT = 14661
SYSID = 1
ALT = 12.0


# ── 樁：DB／守門／入列 ──────────────────────────────────────────────
class FakePool:
    """`mission_fly` 只讀三樣：current_plan_id、waypoints、plans.home。"""

    def __init__(self):
        self.rows, self.home = [], None

    async def fetch(self, q, *a):
        return self.rows

    async def fetchval(self, q, *a):
        if "current_plan_id" in q:
            return "p-test"
        if "home" in q:
            return self.home
        return None

    async def fetchrow(self, q, *a):
        return None

    async def execute(self, q, *a):
        return None


async def _noop(*a, **k):
    return None


async def _audit(sysid, action, params, result, detail=""):
    print(f"    audit {action}: {result}")


pool = FakePool()
main.pool = pool
main._audit = _audit
main._require_capability = _noop
guard_client.ask_guard = _noop
guard_client.show_on_live = _noop


# ── 連 SITL ───────────────────────────────────────────────────────
bridge = subprocess.Popen(
    [sys.executable, "/srv/sim-fleet/ardupilot_bridge.py"],
    env={**os.environ, "ARDU_TCP": SITL_TCP, "FANOUT": f"127.0.0.1:{UDP_PORT}"},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
router = mav.MavRouter(f"udpin://0.0.0.0:{UDP_PORT}")
router.start()
main.router = router

fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def d():
    return router.drones.get(SYSID) or {}


def wait(pred, timeout, what):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if pred():
            return True
        time.sleep(0.5)
    print(f"    （{timeout:.0f}s 內等不到：{what}）")
    return False


def offset(lat, lon, north_m, east_m):
    import math
    return (lat + north_m / plan_check.M_PER_DEG_LAT,
            lon + east_m / (plan_check.M_PER_DEG_LON_EQ * math.cos(math.radians(lat))))


def mission(start, wp2):
    """起飛項（帶起始點座標）→ 第二個點 → RTL。回 (DB rows, 上傳 items)。"""
    rows = [
        {"seq": 0, "lat": start[0], "lon": start[1], "alt": ALT, "action": "takeoff",
         "params": {"command": 22, "frame": 3}},
        {"seq": 1, "lat": wp2[0], "lon": wp2[1], "alt": ALT, "action": "waypoint",
         "params": {"command": 16, "frame": 3}},
        {"seq": 2, "lat": 0, "lon": 0, "alt": 0, "action": "rtl",
         "params": {"command": 20, "frame": 2}},
    ]
    items = [{"seq": r["seq"], "frame": r["params"]["frame"],
              "command": r["params"]["command"], "p1": 0, "p2": 0, "p3": 0, "p4": 0,
              "x": int(r["lat"] * 1e7), "y": int(r["lon"] * 1e7), "z": float(r["alt"])}
             for r in rows]
    return rows, items


def land_and_wait():
    router.submit(mav.job_set_mode, SYSID, "land")
    wait(lambda: not d().get("armed"), 120, "落地上鎖")


async def fly(label, north_m, accept=None, expect=None):
    """起始點放在目前位置正北 north_m 公尺，第二個點再往東 60 m。"""
    print(f"\n── {label}")
    here = (d()["lat"], d()["lon"])
    start = offset(*here, north_m, 0)
    wp2 = offset(*start, 0, 60)
    pool.rows, items = mission(start, wp2)
    router.submit(mav.job_upload_mission, SYSID, items)
    try:
        res = await main.mission_fly(SYSID, main.FlyIn(accept_start_distance_m=accept))
    except HTTPException as e:
        det = e.detail if isinstance(e.detail, dict) else {"msg": e.detail}
        print(f"    HTTP {e.status_code} {det.get('code')}：{det.get('msg')}")
        return {"error": e.status_code, **det}, start, wp2
    tr = res["steps"].get("transit", {})
    print(f"    transit：{ {k: tr.get(k) for k in ('arrived', 'skipped', 'from_m', 'distance_m', 'seconds')} }")
    return res, start, wp2


def dist_to(p):
    return plan_check.dist_m(d()["lat"], d()["lon"], *p)


async def run():
    print("等 SITL 心跳與 EKF…")
    wait(lambda: d().get("custom_mode") is not None, 60, "心跳")
    # 串流在正式環境是 backend 要的（`mavlink_rx`），這裡沒有 backend，自己要
    router.submit(lambda r, s: r._sendto(s, lambda m: m.request_data_stream_encode(
        s, 1, M.MAV_DATA_STREAM_ALL, 4, 1)), SYSID)
    wait(lambda: d().get("lat") is not None, 120, "位置")
    time.sleep(25)                                 # EKF 收斂（GUIDED arm 需要）

    # A：60 m，不必確認；切 AUTO 的那一刻機必須在起始點
    res, start, wp2 = await fly("A. 起始點 60 m 外（< 門檻）", 60)
    tr = res.get("steps", {}).get("transit", {}) if "error" not in res else {}
    check(tr.get("arrived") is True, "A：transit 回報到位")
    check("error" not in res and dist_to(start) < 6.0,
          f"A：切 AUTO 當下離起始點 {dist_to(start):.1f} m（到位才切）")
    check(wait(lambda: dist_to(wp2) < 5, 90, "飛到第二點"), "A：接著飛往第二個點")
    land_and_wait()

    # B：150 m，沒確認 → 擋下、沒解鎖
    res, *_ = await fly("B. 起始點 150 m 外，沒確認", 150)
    check(res.get("error") == 409 and res.get("code") == "far_start", "B：409 far_start")
    check(abs((res.get("distance_m") or 0) - 150) < 10,
          f"B：回報距離 {res.get('distance_m')} m")
    time.sleep(2)
    check(not d().get("armed"), "B：機沒有解鎖")

    # B'：確認的距離比實際少很多 → 仍要重看
    res, *_ = await fly("B'. 帶了過期的確認（50 m）", 150, accept=50)
    check(res.get("code") == "far_start", "B'：確認的數字對不上 → 仍擋")

    # C：帶確認
    res, start, wp2 = await fly("C. 起始點 150 m 外，帶確認", 150, accept=150)
    tr = res.get("steps", {}).get("transit", {}) if "error" not in res else {}
    check(tr.get("arrived") is True and tr.get("from_m", 0) > 100,
          "C：確認後飛過去並到位")
    check(wait(lambda: dist_to(wp2) < 5, 90, "飛到第二點"), "C：接著飛往第二個點")
    land_and_wait()

    # E：途中有人接手（切 LOITER）→ 序列停手，不切任務、也不把它搶回 GUIDED
    print("\n── E. 飛往起始點途中有人切 hold")
    here = (d()["lat"], d()["lon"])
    start = offset(*here, 90, 0)
    pool.rows, items = mission(start, offset(*start, 0, 60))
    router.submit(mav.job_upload_mission, SYSID, items)
    task = asyncio.create_task(main.mission_fly(SYSID, main.FlyIn()))
    await asyncio.to_thread(wait, lambda: dist_to(here) > 20, 90, "離開起飛點 20 m")
    await asyncio.to_thread(router.submit, mav.job_set_mode, SYSID, "hold")
    try:
        await task
        check(False, "E：應該停手卻回報成功")
    except HTTPException as e:
        print(f"    HTTP {e.status_code} {e.detail.get('code')}：{e.detail.get('msg')}")
        check(e.detail.get("code") == "start_not_reached" and "接手" in e.detail["msg"],
              "E：回報有人接手、未啟動任務")
    time.sleep(6)                                  # 超過一個重送週期
    drv = mav.dialect(router, SYSID)["driver"]
    check(drv.decode_verb(d()["custom_mode"]) == "hold",
          f"E：模式維持 {drv.decode_mode(d()['custom_mode'])}（沒被搶回 GUIDED、沒切 AUTO）")
    land_and_wait()

    # D：起始點就在腳下
    res, *_ = await fly("D. 起始點就在腳下", 0)
    tr = res.get("steps", {}).get("transit", {}) if "error" not in res else {}
    check(bool(tr.get("skipped")), f"D：不另外飛（{tr.get('skipped')}）")
    land_and_wait()


try:
    asyncio.run(run())
finally:
    bridge.terminate()
print(f"\n{'全部通過' if not fails else f'{len(fails)} 項失敗'}")
sys.exit(1 if fails else 0)
