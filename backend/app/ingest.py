"""MAVSDK 遙測接收：訂閱 SITL/真機的 telemetry stream 寫入 live state。

armed 狀態轉換即為架次邊界：False→True 開 session、True→False 關 session。
"""
import asyncio
import logging

from mavsdk import System

from . import db
from .config import settings
from .state import live
from .ws import manager

log = logging.getLogger(__name__)


async def run() -> None:
    drone = System()
    log.info("connecting MAVLink at %s ...", settings.mavlink_url)
    await drone.connect(system_address=settings.mavlink_url)

    async for cs in drone.core.connection_state():
        if cs.is_connected:
            live.connected = True
            log.info("MAVLink connected")
            break

    await asyncio.gather(
        _position(drone), _velocity(drone), _battery(drone),
        _gps(drone), _mode(drone), _armed(drone),
    )


async def _position(drone: System) -> None:
    async for p in drone.telemetry.position():
        live.lat = p.latitude_deg
        live.lon = p.longitude_deg
        live.alt_msl = p.absolute_altitude_m
        live.alt_rel = p.relative_altitude_m


async def _velocity(drone: System) -> None:
    async for v in drone.telemetry.velocity_ned():
        live.ground_speed = (v.north_m_s ** 2 + v.east_m_s ** 2) ** 0.5
        live.vertical_speed = -v.down_m_s


async def _battery(drone: System) -> None:
    async for b in drone.telemetry.battery():
        live.battery_pct = b.remaining_percent * 100.0
        live.battery_voltage = b.voltage_v


async def _gps(drone: System) -> None:
    async for g in drone.telemetry.gps_info():
        live.gps_fix = int(g.fix_type.value) if hasattr(g.fix_type, "value") else None
        live.satellites = g.num_satellites


async def _mode(drone: System) -> None:
    async for m in drone.telemetry.flight_mode():
        mode = str(m)
        if live.flight_mode is not None and mode != live.flight_mode:
            ev = await db.insert_event(live.drone_id, live.session_id, "info",
                                       "mode_change", {"from": live.flight_mode, "to": mode})
            await manager.broadcast({"type": "event", **ev})
        live.flight_mode = mode


async def _armed(drone: System) -> None:
    """armed 轉換即架次邊界。

    賦值順序是刻意的——`_link_and_db_loop` 以 `armed and session_id` 為入庫條件，
    兩者都是同一個 event loop 內的協程，會在 await 點交錯執行：

    - 解鎖：先建好 session 再標記 armed。反過來的話，中間那一秒 armed=True 但
      session_id 還是 None，那筆資料就成了無主孤兒。
    - 上鎖：先清掉 armed／session_id 再關 session。`end_session()` 內含 await，
      若在那期間 `_link_and_db_loop` 被排程到，會把資料寫進已經結算完摘要的架次。
    """
    async for armed in drone.telemetry.armed():
        if armed and not live.armed:
            live.session_id = await db.create_session(live.drone_id)
            live.armed = True
            log.info("session started: %s", live.session_id)
        elif not armed and live.armed:
            sid, live.session_id, live.armed = live.session_id, None, False
            if sid:
                await db.end_session(sid)
                log.info("session ended: %s", sid)
        else:
            live.armed = armed
