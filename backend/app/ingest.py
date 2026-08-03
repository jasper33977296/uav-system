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
    async for armed in drone.telemetry.armed():
        if armed and not live.armed:
            live.session_id = await db.create_session(live.drone_id)
            log.info("session started: %s", live.session_id)
        elif not armed and live.armed and live.session_id:
            await db.end_session(live.session_id)
            log.info("session ended: %s", live.session_id)
            live.session_id = None
        live.armed = armed
