#!/usr/bin/env python3
"""以 QGC 的身分上傳並執行 missions/ 的任務檔——驗證任務疊圖用。

走 14550（GCS 埠、控制側），與 QGC 同一個角色；backend 只透過
GET /api/mission/current 唯讀讀回。真機階段這整支腳本的工作由 QGC 做。

用法：
    apps/backend/.venv/bin/python scripts/fly-mission.py [plan檔路徑]
"""
import asyncio
import sys
from pathlib import Path

from mavsdk import System

PLAN = sys.argv[1] if len(sys.argv) > 1 else str(
    Path(__file__).resolve().parent.parent / "missions" / "interference-survey.plan")


async def go() -> None:
    # port=50052：避開 backend 的 mavsdk_server（host network 下共用 50051 會
    # 誤連進容器，開 plan 檔也會發生在容器裡——實際踩過）
    d = System(port=50052)
    await d.connect("udpin://0.0.0.0:14550")
    print("等待 MAVLink 連線 ...", flush=True)
    async for s in d.core.connection_state():
        if s.is_connected:
            break

    print(f"匯入並上傳任務：{PLAN}", flush=True)
    imported = await d.mission_raw.import_qgroundcontrol_mission(PLAN)
    await d.mission_raw.upload_mission(imported.mission_items)
    print(f"已上傳 {len(imported.mission_items)} 個任務項", flush=True)

    async for h in d.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok:
            break

    await d.action.arm()
    await d.mission.start_mission()
    print("任務開始", flush=True)

    for _ in range(60):                       # 最多等 5 分鐘
        await asyncio.sleep(5)
        if await d.mission.is_mission_finished():
            break
    print("任務完成，返航", flush=True)
    try:
        await d.action.return_to_launch()
    except Exception:
        pass                                  # 任務尾端已含 RTL 時會拒絕，無妨
    await asyncio.sleep(45)
    print("完成", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        sys.exit(130)
