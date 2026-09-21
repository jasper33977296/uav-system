#!/usr/bin/env python3
"""`mission/start` 的「重新 vs 繼續」SITL 驗證（2026-09-21，issues/060）。

## 要驗的事

056 讓**一鍵起飛**會先飛到任務起始點，但 `_fly_to_start` 全檔只有那一個呼叫點
——機已經在空中、要再跑一次同一條路徑時完全不經過，而 ArduCopter 一進 AUTO
就從最近的下一個航點開始，航線第一段永遠沒被飛到。

修法有兩半，而**兩半互為對方的反例**，所以必須一起驗：

  F. 已在空中、重新執行（預設）   → **要**飛到起始點，到位才切任務
  G. 已在空中、繼續執行（resume） → **不可以**飛回起點，接著原來的地方跑
  H. 已經站在起始點上            → 不另外飛（transit.skipped 說得出原因）
  I. 離起始點太遠、沒確認         → 409 far_start，**而且機不動**（它在空中，
                                    擋下來不等於安全——要確認它沒被送出去）

只驗 G 或只驗 F 都會過：前者放行一切，後者攔阻一切。

## 為什麼不走 HTTP

同 `sitl-fly-to-start.py`：SITL 沒有板號，在本系統的規則下恆為 unmanaged。
這裡直接呼叫端點函式，入列與能力 gate 換成放行樁——被驗的是序列本身。

## 跑法

    docker run -d --name sitl-msstart --network host -e INSTANCE=4 \\
        -e LAT=24.7814 -e LON=120.9947 -e ALT=100 radarku/ardupilot-sitl:latest
    docker run --rm --network host -e ENABLE_COMMANDS=true \\
        -v $PWD/apps/command/app:/srv/app -v $PWD/libs:/srv/libs \\
        -v $PWD/scripts:/srv/scripts -v $PWD/sim-fleet:/srv/sim-fleet \\
        uav-system-uav-command python /srv/scripts/sitl-mission-start-transit.py
"""
import asyncio
import math
import os
import subprocess
import sys
import time

sys.path[:0] = ["/srv", "/srv/libs"]

from fastapi import HTTPException                      # noqa: E402
from pymavlink.dialects.v20 import ardupilotmega as M  # noqa: E402

import plan_check                                      # noqa: E402
from app import guard_client, main, mav                # noqa: E402

SITL_TCP = os.environ.get("SITL_TCP", "127.0.0.1:5800")
UDP_PORT = 14662
SYSID = 1
ALT = 12.0


class FakePool:
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
    return (lat + north_m / plan_check.M_PER_DEG_LAT,
            lon + east_m / (plan_check.M_PER_DEG_LON_EQ * math.cos(math.radians(lat))))


def mission(start, wp2):
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


def dist_to(p):
    return plan_check.dist_m(d()["lat"], d()["lon"], *p)


async def takeoff_guided():
    """把機弄到空中。**用產品端自己的 `_do_takeoff`**，不要手工拼一套解鎖序列
    ——第一版手工拼的在 SITL 上 30 秒解不了鎖（GUIDED 的解鎖前置沒做齊），
    結果整個 F 情境掛在前置而不是在被驗的東西上。"""
    if d().get("armed") and (d().get("alt_rel") or 0) > ALT * 0.5:
        return True                      # 已經在空中，不用再起
    # **等到真的解得了鎖，不要猜一個 sleep 秒數。** SITL 的 3D fix 收斂時間
    # 不固定，第一版寫死 25 秒，於是整個 F 掛在 `PreArm: Need 3D Fix`
    # ——那是前置沒到位，不是被驗的東西壞了
    deadline = time.monotonic() + 180
    last = ""
    while time.monotonic() < deadline:
        try:
            await main._do_takeoff(SYSID, ALT)
            break
        except Exception as e:
            last = str(e)
            if "PreArm" not in last and "解鎖" not in last:
                print(f"    起飛失敗（不是預檢）：{last}")
                return False
            await asyncio.sleep(5)
    else:
        print(f"    180s 內一直解不了鎖：{last}")
        return False
    return wait(lambda: (d().get("alt_rel") or 0) > ALT * 0.8, 90, "離地")


def land_and_wait():
    router.submit(mav.job_set_mode, SYSID, "land")
    wait(lambda: not d().get("armed"), 150, "落地上鎖")
    time.sleep(3)


async def start_mission(**kw):
    try:
        res = await main.mission_start(SYSID, main.MissionStartIn(**kw))
    except HTTPException as e:
        det = e.detail if isinstance(e.detail, dict) else {"msg": e.detail}
        print(f"    HTTP {e.status_code} {det.get('code')}：{det.get('msg')}")
        return {"error": e.status_code, **det}
    tr = res["steps"].get("transit")
    print(f"    steps.mode={res['steps'].get('mode')}  transit="
          f"{ {k: tr.get(k) for k in ('arrived','skipped','from_m','seconds')} if tr else None}")
    return res


async def run():
    print("等 SITL 心跳與 EKF…")
    wait(lambda: d().get("custom_mode") is not None, 60, "心跳")
    router.submit(lambda r, s: r._sendto(s, lambda m: m.request_data_stream_encode(
        s, 1, M.MAV_DATA_STREAM_ALL, 4, 1)), SYSID)
    wait(lambda: d().get("lat") is not None, 120, "位置")
    time.sleep(25)

    # ── F：已在空中、重新執行 → 要飛到起始點 ───────────────────────
    print("\n── F. 已在空中、重新執行（預設）")
    check(await takeoff_guided(), "F：先把機弄到空中")
    here = (d()["lat"], d()["lon"])
    start = offset(*here, 70, 0)
    wp2 = offset(*start, 0, 60)
    pool.rows, items = mission(start, wp2)
    router.submit(mav.job_upload_mission, SYSID, items)
    time.sleep(2)
    res = await start_mission()
    tr = res.get("steps", {}).get("transit", {}) if "error" not in res else {}
    check(tr.get("arrived") is True, "F：transit 回報到位")
    check("error" not in res and dist_to(start) < 6.0,
          f"F：啟動任務當下離起始點 {dist_to(start):.1f} m（到位才啟動）")
    check(wait(lambda: dist_to(wp2) < 6, 120, "飛到第二點"), "F：接著飛往第二個點")

    # ── G：繼續執行 → **不可以**飛回起點 ──────────────────────────
    print("\n── G. 已在空中、繼續執行（resume）")
    router.submit(mav.job_set_mode, SYSID, "hold")
    time.sleep(3)
    d_before = dist_to(start)
    print(f"    暫停處離起始點 {d_before:.0f} m")
    res = await start_mission(resume=True)
    check("error" not in res, "G：繼續執行被接受")
    check(res.get("steps", {}).get("mode") == "resume", "G：留痕標成 resume")
    check("transit" not in res.get("steps", {}),
          "G：**沒有 transit 這一步**（不飛回起點）")
    check("mission" not in res.get("steps", {}),
          "G：**沒有送 MISSION_START**（param1=0 會把序號歸零）")
    # 行為面：接下來 25 秒不可以往起始點靠
    time.sleep(25)
    d_after = dist_to(start)
    print(f"    25 秒後離起始點 {d_after:.0f} m")
    check(d_after > d_before - 15,
          f"G：**沒有飛回起點**（{d_before:.0f} m → {d_after:.0f} m）")
    land_and_wait()

    # ── H：已經站在起始點上 → 不另外飛 ────────────────────────────
    print("\n── H. 已經在起始點上")
    check(await takeoff_guided(), "H：先把機弄到空中")
    here = (d()["lat"], d()["lon"])
    start = here
    wp2 = offset(*start, 0, 60)
    pool.rows, items = mission(start, wp2)
    router.submit(mav.job_upload_mission, SYSID, items)
    time.sleep(2)
    res = await start_mission()
    tr = res.get("steps", {}).get("transit", {}) if "error" not in res else {}
    check(bool(tr.get("skipped")), f"H：transit 跳過並說出原因：{tr.get('skipped')}")
    check("mission" in res.get("steps", {}), "H：仍然有啟動任務")

    # ── I：太遠、沒確認 → 擋下，而且機不動 ────────────────────────
    print("\n── I. 離起始點太遠、沒確認")
    # **H 的任務以 RTL 收尾**，跑完機會自己往回飛、下降、落地上鎖。第二輪
    # 就是栽在這裡：I 直接接在 H 後面，機當時還在 RTL 的下降段，goto 送出去
    # 之後位移 0.0 m。**讓 I 從乾淨狀態開始**——落地、重新起飛，
    # 而不是繼承上一個情境留下來的姿態
    land_and_wait()
    check(await takeoff_guided(), "I：從乾淨狀態重新起飛")
    router.submit(mav.job_set_mode, SYSID, "hold")
    time.sleep(5)
    print(f"    goto 之前：mode={d().get('custom_mode')} armed={d().get('armed')} "
          f"alt_rel={d().get('alt_rel')}")
    here = (d()["lat"], d()["lon"])
    far = offset(*here, 200, 0)
    pool.rows, items = mission(far, offset(*far, 0, 60))
    router.submit(mav.job_upload_mission, SYSID, items)
    time.sleep(2)
    p_before = (d()["lat"], d()["lon"])
    res = await start_mission()
    check(res.get("error") == 409 and res.get("code") == "far_start",
          "I：409 far_start")
    time.sleep(8)
    moved = plan_check.dist_m(d()["lat"], d()["lon"], *p_before)
    check(moved < 15, f"I：**機沒有被送出去**（位移 {moved:.1f} m）")
    # 帶確認再送一次要放行
    res = await start_mission(accept_start_distance_m=res.get("distance_m"))
    check("error" not in res, "I：帶確認距離後放行")
    land_and_wait()

    print("\n" + ("全部通過" if not fails else f"**{len(fails)} 項未通過**"))
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(run())
    finally:
        bridge.terminate()
    sys.exit(rc)
