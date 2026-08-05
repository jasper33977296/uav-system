#!/usr/bin/env python3
"""測試飛行：起飛 → 進入干擾區 → 飛出 → 返航降落。

用途是在 backend 執行中飛一趟，觀察完整的資料流：
SINR 驟降 → link_degraded / link_lost 事件 → 回升後 link_recovered → 架次摘要。

**連 14550 而非 14540**：backend 的 mavsdk_server 已經綁住 14540（onboard/API），
同一個 UDP 埠不能兩個程序同時用。PX4 SITL 對 14540 與 14550（GCS）都會送
MAVLink，控制指令走哪個埠都可以，所以這裡讓給 backend、自己走 QGC 那個埠。
見 issues/008。

用法：
    apps/backend/.venv/bin/python scripts/test-flight.py

若同時開著 QGroundControl，它也會佔用 14550——擇一使用。
"""
import asyncio
import sys

from mavsdk import System

ZONE_LAT, ZONE_LON = 47.3995, 8.5456   # seed 干擾區中心（PX4 SITL 蘇黎世起飛點以北）
HOME_LAT, HOME_LON = 47.3977, 8.5456
ALT_M = 538.0                           # 絕對高度（起飛點約 488m + 50m）


async def go() -> None:
    drone = System()
    await drone.connect("udpin://0.0.0.0:14550")

    print("等待 MAVLink 連線 ...", flush=True)
    async for state in drone.core.connection_state():
        if state.is_connected:
            break

    print("等待 pre-flight check（GPS 定位、home 位置）...", flush=True)
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok:
            break

    await drone.action.arm()
    await drone.action.set_takeoff_altitude(50.0)
    await drone.action.takeoff()
    print("起飛（架次開始，backend 這時才開始入庫）", flush=True)
    await asyncio.sleep(20)

    print("→ 飛向干擾區中心（預期 SINR 驟降，發 link_degraded → link_lost）", flush=True)
    await drone.action.goto_location(ZONE_LAT, ZONE_LON, ALT_M, 0.0)
    await asyncio.sleep(45)

    print("→ 飛回起點（預期 SINR 回升，發 link_degraded → link_recovered）", flush=True)
    await drone.action.goto_location(HOME_LAT, HOME_LON, ALT_M, 0.0)
    await asyncio.sleep(40)

    print("→ 返航降落", flush=True)
    await drone.action.return_to_launch()
    await asyncio.sleep(50)
    print("完成。落地自動上鎖後架次即結算，摘要見 /api/sessions", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        sys.exit(130)
