"""無人機群模擬（開發鷹架，僅 simulated 模式）。

目的：在多台真機（多 RB5、多條 MAVLink）到位之前，驗證整條**多機資料管線**
——註冊、航線切分、遙測與鏈路入庫、事件、WS 廣播、前端多機渲染。

作法：僚機用運動學模型（沿航點等速飛行，無物理模擬）產生位置，
其餘與主機（真 SITL）走完全相同的程式路徑：同一個 LiveState、
同一個 SimulatedLinkSource、同一個鏈路狀態機、同一批 insert_*。
因此 DB 裡的資料形狀與真多機一致，只有位置的來源不同。

不追求飛行動力學擬真——判準同 link_sim：走到相同的程式路徑即可。
"""
import asyncio
import logging
import math

from . import db
from .link_events import transition
from .link_sim import SimulatedLinkSource
from .state import LiveState
from .ws import manager

log = logging.getLogger(__name__)

M_LAT = 110574.0
BASE = (47.397742, 8.545594)          # SITL 起飛點


def _mlon(lat: float) -> float:
    return 111320.0 * math.cos(math.radians(lat))


def _off(north_m: float, east_m: float, alt: float) -> tuple[float, float, float]:
    """以 SITL 起飛點為原點的公尺偏移 → (lat, lon, alt)。"""
    lat = BASE[0] + north_m / M_LAT
    return (lat, BASE[1] + east_m / _mlon(lat), alt)


def build_paths(count: int) -> list[tuple[str, list, float]]:
    """count 台僚機的（名稱, 航點, 速度）。幾何彼此不同：
    矩形繞區東、西側南北往返、斜向高空穿越。干擾區中心約在北 195m 處。"""
    paths = [
        ("swarm-2", [_off(-30, 60, 0), _off(-30, 60, 35), _off(150, 140, 35),
                     _off(260, 140, 35), _off(260, 40, 35), _off(150, 40, 35),
                     _off(-30, 60, 35), _off(-30, 60, 0)], 8.0),
        ("swarm-3", [_off(-30, -60, 0), _off(-30, -60, 55), _off(280, -90, 55),
                     _off(-30, -60, 55), _off(-30, -60, 0)], 9.0),
        ("swarm-4", [_off(-60, 0, 0), _off(-60, 0, 70), _off(300, 120, 70),
                     _off(-60, 0, 70), _off(-60, 0, 0)], 10.0),
    ]
    return paths[:count]


class SimDrone:
    def __init__(self, name: str, wps: list, speed: float):
        self.name, self.wps, self.speed = name, wps, speed
        self.i = 1                      # wps[0] 是起點
        self.done = False
        s = self.state = LiveState()
        s.lat, s.lon, s.alt_rel = wps[0]
        s.connected = True
        s.gps_fix, s.satellites = 3, 10
        s.battery_pct, s.battery_voltage = 100.0, 16.8
        s.flight_mode = "MISSION"
        s.roll, s.pitch = 0.0, 0.0

    def step(self, dt: float) -> None:
        if self.done:
            return
        s = self.state
        tgt = self.wps[self.i]
        dx = (tgt[1] - s.lon) * _mlon(s.lat)
        dy = (tgt[0] - s.lat) * M_LAT
        dz = tgt[2] - (s.alt_rel or 0.0)
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        move = self.speed * dt
        if dist <= move:                # 抵達航點
            s.lat, s.lon, s.alt_rel = tgt
            self.i += 1
            if self.i >= len(self.wps):
                self.done = True
                s.flight_mode = "LAND"
                s.ground_speed = s.vertical_speed = 0.0
            return
        s.lat += (dy / dist * move) / M_LAT
        s.lon += (dx / dist * move) / _mlon(s.lat)
        s.alt_rel = (s.alt_rel or 0.0) + dz / dist * move
        h = math.hypot(dx, dy)
        s.heading = math.degrees(math.atan2(dx, dy)) % 360
        s.ground_speed = self.speed * (h / dist)
        s.vertical_speed = self.speed * (dz / dist)
        s.pitch = max(-15.0, min(15.0, -12.0 * dz / dist))
        s.battery_pct = max(20.0, (s.battery_pct or 100.0) - 0.06 * dt)


_drones: list[SimDrone] = []
_task: asyncio.Task | None = None


def status() -> dict:
    running = _task is not None and not _task.done()
    return {"running": running,
            "drones": [{"name": d.name, "done": d.done} for d in _drones] if running else []}


async def start(count: int = 3) -> list[str]:
    global _drones, _task
    if _task and not _task.done():
        raise RuntimeError("群飛模擬已在執行中")
    _drones = []
    cells, zones = await db.fetch_cells(), await db.fetch_zones(enabled_only=True)
    for name, wps, speed in build_paths(count):
        d = SimDrone(name, wps, speed)
        d.state.drone_id = await db.ensure_drone(name, "swarm-sim://kinematic")
        d.state.drone_name = name
        # 僚機飛自己的幾何路徑，不關聯任務庫的啟用路徑（那是主機宣告的）
        d.state.session_id = await db.create_session(d.state.drone_id, link_mission=False)
        d.state.armed = True
        d.source = SimulatedLinkSource(cells, zones)   # 各自的 serving-cell 記憶
        _drones.append(d)
    _task = asyncio.create_task(_run())
    log.info("swarm started: %s", [d.name for d in _drones])
    return [d.name for d in _drones]


async def stop() -> None:
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    await _teardown()


async def _teardown() -> None:
    for d in _drones:
        s = d.state
        if s.session_id:
            sid, s.session_id, s.armed = s.session_id, None, False
            await db.end_session(sid)
        await manager.broadcast({"type": "telemetry", **s.telemetry_dict()})


async def _run() -> None:
    tick = 0
    try:
        while any(not d.done for d in _drones):
            await asyncio.sleep(0.2)                 # 5Hz 前進與廣播
            tick += 1
            for d in _drones:
                d.step(0.2)
                s = d.state
                if tick % 5 == 0 and not d.done:     # 1Hz 取樣與入庫（同主機節奏）
                    m = d.source.sample(s.lat, s.lon, s.alt_rel)
                    s.link = m
                    s.mark_link_seen()
                    await transition(s, m)
                    await db.insert_telemetry(s)
                    await db.insert_link(s)
                await manager.broadcast({"type": "telemetry", **s.telemetry_dict()})
        log.info("swarm finished")
    finally:
        await _teardown()
