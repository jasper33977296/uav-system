"""單機的即時狀態。單一 asyncio event loop 內讀寫，不需要鎖。

多機擴充時，把這裡換成 dict[drone_id, LiveState]，
ingest 任務一機一個，其餘程式碼不變。
"""
from dataclasses import dataclass, field


@dataclass
class LiveState:
    drone_id: str | None = None
    session_id: str | None = None
    connected: bool = False          # MAVLink 連線狀態

    # 飛行遙測
    lat: float | None = None
    lon: float | None = None
    alt_msl: float | None = None
    alt_rel: float | None = None
    heading: float | None = None
    ground_speed: float | None = None
    vertical_speed: float | None = None
    battery_pct: float | None = None
    battery_voltage: float | None = None
    gps_fix: int | None = None
    satellites: int | None = None
    flight_mode: str | None = None
    armed: bool = False

    # 5G 鏈路品質（由 link source 每秒更新）
    link: dict = field(default_factory=dict)

    def telemetry_dict(self) -> dict:
        return {
            "drone_id": self.drone_id,
            "session_id": self.session_id,
            "connected": self.connected,
            "lat": self.lat, "lon": self.lon,
            "alt_msl": self.alt_msl, "alt_rel": self.alt_rel,
            "heading": self.heading,
            "ground_speed": self.ground_speed,
            "vertical_speed": self.vertical_speed,
            "battery_pct": self.battery_pct,
            "battery_voltage": self.battery_voltage,
            "gps_fix": self.gps_fix, "satellites": self.satellites,
            "flight_mode": self.flight_mode, "armed": self.armed,
            "link": self.link,
        }


live = LiveState()
