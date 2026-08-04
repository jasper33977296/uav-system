"""單機的即時狀態。單一 asyncio event loop 內讀寫，不需要鎖。

多機擴充時，把這裡換成 dict[drone_id, LiveState]，
ingest 任務一機一個，其餘程式碼不變。
"""
import time as _time
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
    roll: float | None = None        # 姿態：飛控多感測器融合的結果
    pitch: float | None = None
    ground_speed: float | None = None
    vertical_speed: float | None = None
    battery_pct: float | None = None
    battery_voltage: float | None = None
    gps_fix: int | None = None
    satellites: int | None = None
    flight_mode: str | None = None
    armed: bool = False

    # 5G 鏈路品質（模擬階段由 _link_and_db_loop 更新，真機由機上 node POST 進來）
    link: dict = field(default_factory=dict)

    # 鏈路狀態機的狀態（ok / degraded / lost）。放在這裡是因為模擬與真機兩條路徑
    # 都要用它——模擬走 _link_and_db_loop，真機走 /api/link-metrics/live。
    link_state: str = "ok"

    # 最後一次收到鏈路量測的時刻（monotonic clock，不受系統時間調整影響）。
    # 真機的即時通道會靜默失敗，前端需要據此顯示「已失聯 N 秒」——
    # 那與 link_lost 是不同的事：link_lost 是量到訊號差，失聯是量測送不回來。
    link_seen_mono: float | None = None

    def mark_link_seen(self) -> None:
        self.link_seen_mono = _time.monotonic()

    @property
    def link_age_s(self) -> float | None:
        """距上次收到鏈路量測幾秒。由後端計算，避免前後端時鐘偏差。"""
        if self.link_seen_mono is None:
            return None
        return round(_time.monotonic() - self.link_seen_mono, 2)

    def telemetry_dict(self) -> dict:
        return {
            "drone_id": self.drone_id,
            "session_id": self.session_id,
            "connected": self.connected,
            "lat": self.lat, "lon": self.lon,
            "alt_msl": self.alt_msl, "alt_rel": self.alt_rel,
            "heading": self.heading,
            "roll": self.roll, "pitch": self.pitch,
            "ground_speed": self.ground_speed,
            "vertical_speed": self.vertical_speed,
            "battery_pct": self.battery_pct,
            "battery_voltage": self.battery_voltage,
            "gps_fix": self.gps_fix, "satellites": self.satellites,
            "flight_mode": self.flight_mode, "armed": self.armed,
            "link": self.link,
            "link_state": self.link_state,
            "link_age_s": self.link_age_s,   # None = 從未收到；大於門檻 = 失聯
        }


live = LiveState()
