#!/usr/bin/env python3
"""`/api/start` 的重試防護（外部逾時後重送不會再飛一趟）。

**為什麼要有這支**：2026-09-23 外部控制端觸發任務，伺服器端跑完要 19 秒，
而它的 HTTP 逾時比這短——於是畫面顯示 timeout，**但飛機照飛**；操作者重試，
**第二趟又真的飛了一次**（command_log：08:14 一趟、08:16 一趟）。

這支**不碰飛機**：把「序列進行中」的狀態塞進模組，然後直接呼叫 `_start`。
防護若有效，它會在碰到飛控之前就回覆——所以整支測試不會送出任何 MAVLink。

跑法（在指令服務容器內）：
    docker exec -w /srv uav-command python /tmp/test-start-retry-guard.py
"""
import asyncio
import sys

sys.path.insert(0, "/srv")
from fastapi import HTTPException                                   # noqa: E402

from app import main                                                # noqa: E402

SYSID = 1
BUSY_MISSION = "11111111-2222-3333-4444-555555555555"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  ✓ {name}")
    else:
        fail += 1; print(f"  ✗ {name} {detail}")


class _Router:
    """只回一台在線的假 router——`_resolve_sysid` 只用到 snapshot()。"""
    def snapshot(self):
        return {str(SYSID): {"age_s": 0.1, "armed": False}}


class _Req:
    """`_stream_of` 只用到 url.hostname。"""
    class _U:
        hostname = "10.141.2.21"
    url = _U()
    headers: dict = {}


async def call(mission_id=None, wait=True):
    body = main.StartIn(plan_id="任何路徑都不會被讀到", mission_id=mission_id, wait=wait)
    return await main._start(body, _Req())


async def run():
    main.router = _Router()
    main.settings.enable_commands = True

    print("── 1. 沒有序列在跑時，防護不該擋 ──")
    main._start_mission.clear(); main._start_step.clear()
    try:
        await call()
        check("走過了防護", True)
    except HTTPException as e:
        d = e.detail if isinstance(e.detail, dict) else {}
        check("沒被防護誤擋", d.get("code") != "start_in_progress", d)
    except Exception as e:
        # **走到資料庫才失敗，正是「沒被防護擋下」的證明**——這支測試沒有接
        # DB（也不該接：接了就可能真的去飛），所以往下走一定會在這裡斷
        check("沒被防護誤擋（往下走到查路徑才失敗）",
              type(e).__name__ == "AttributeError", f"{type(e).__name__}: {e}")

    print("\n── 2. 序列進行中、帶**不同**的 mission_id ──")
    main._start_mission[SYSID] = BUSY_MISSION
    main._start_step[SYSID] = "airborne"
    try:
        await call(mission_id="99999999-8888-7777-6666-555555555555")
        check("應該擋下", False, "居然放行了")
    except HTTPException as e:
        d = e.detail if isinstance(e.detail, dict) else {}
        check("409 start_in_progress", e.status_code == 409
              and d.get("code") == "start_in_progress", (e.status_code, d))
        check("訊息說得出跑到哪一步", "airborne" in str(d.get("msg", "")), d)
        check("說得出下一步怎麼做", bool(d.get("how_to")), d)

    print("\n── 3. 序列進行中、帶**同一個** mission_id（＝逾時後的重試）──")
    try:
        r = await call(mission_id=BUSY_MISSION)
        body = getattr(r, "body", None)
        import json
        j = json.loads(body) if body else r
        check("回 202 而不是錯誤", getattr(r, "status_code", None) == 202, r)
        check("明說已經在跑、沒有重新起飛", j.get("already_running") is True
              and "沒有重新起飛" in j.get("msg", ""), j)
        check("回同一個 mission_id", j.get("mission_id") == BUSY_MISSION, j)
        check("帶 stream 網址", bool(j.get("stream", {}).get("url")), j)
        check("說得出跑到哪一步", j.get("step") == "airborne", j)
    except HTTPException as e:
        check("不該丟例外", False, (e.status_code, e.detail))

    print("\n── 4. 別台機不受影響 ──")
    try:
        await call()          # sysid 仍解成 1（只有一台在線）→ 應該還是被擋
        check("同一台仍被擋", False)
    except HTTPException as e:
        d = e.detail if isinstance(e.detail, dict) else {}
        check("沒帶 mission_id 的重試也被擋（不是靜默放行）",
              d.get("code") == "start_in_progress", d)

    main._start_mission.clear(); main._start_step.clear()
    print(f"\n結果：{ok} 通過、{fail} 失敗")
    return 1 if fail else 0


sys.exit(asyncio.run(run()))
