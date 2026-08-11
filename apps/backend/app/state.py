"""即時狀態。單一 asyncio event loop 內讀寫，不需要鎖。

多機（2026-08-10 路線 B）：`fleet` 以 drone_id 為鍵持有每台機的
LiveState；`live` 仍是「主機」那台的 state 物件（fleet 裡同一個參照），
既有單機程式碼（api、模擬迴圈）不需改。mavlink_rx 依 sysid 建檔。
"""
import time as _time
from dataclasses import dataclass, field


@dataclass
class LiveState:
    drone_id: str | None = None
    drone_name: str | None = None    # 事件流等 UI 顯示用（多機時必須能分辨）
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

    # 飛行就緒（QGC「Ready To Fly」的同源訊號，2026-08-11）：
    # PX4 的 arming checks 總結果直接讀 SYS_STATUS 的 PREARM_CHECK 健康位，
    # 不需要 events metadata；逐項失敗原因的完整清單走 PX4 Events 介面
    # （解碼列 issues/014），這裡以感測器健康位＋EKF＋GPS 近似。
    mav_state: str | None = None          # STANDBY / ACTIVE / CRITICAL…
    prearm_ok: bool | None = None         # PX4 預檢總結果（None=未知）
    sensors_unhealthy: list = field(default_factory=list)
    ekf_ok: bool | None = None
    landed_state: str | None = None       # on_ground / in_air / takeoff / landing
    autopilot_raw: int | None = None      # MAV_AUTOPILOT_*（方言分表；issue 015）
    vehicle_type_raw: int | None = None   # MAV_TYPE_*

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

    def readiness(self) -> tuple[bool, list]:
        """就緒判定＋不就緒原因（給前端顯示；權威訊號是 prearm_ok）。"""
        reasons = []
        if self.prearm_ok is False:
            reasons.append("PX4 預檢未過（arming checks）")
        reasons += [f"感測器異常：{s}" for s in self.sensors_unhealthy]
        if self.ekf_ok is False:
            reasons.append("EKF 未就緒")
        if self.gps_fix is not None and self.gps_fix < 3:
            reasons.append(f"GPS 未定位（fix={self.gps_fix}）")
        if self.mav_state in ("CRITICAL", "EMERGENCY", "FLIGHT_TERMINATION"):
            reasons.append(f"failsafe 狀態：{self.mav_state}")
        # prearm_ok=None＝韌體不回報 PREARM 位元，退回次級訊號判定
        return (not reasons and self.prearm_ok is not False), reasons

    def telemetry_dict(self) -> dict:
        ready, reasons = self.readiness()
        from .mavlink_rx import autopilot_name   # 就地 import 避免載入序循環
        return {
            "ready": ready,
            "not_ready_reasons": reasons,
            "autopilot": autopilot_name(self.autopilot_raw),  # px4/ardupilot/unknown
            "mav_state": self.mav_state,
            "landed_state": self.landed_state,
            "prearm_ok": self.prearm_ok,
            "ekf_ok": self.ekf_ok,
            "sensors_unhealthy": self.sensors_unhealthy,
            "drone_id": self.drone_id,
            "drone_name": self.drone_name,
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

# 全機隊：drone_id → LiveState（主機也在裡面，值就是上面的 live 物件；
# 由 main.lifespan 放入，mavlink_rx 自動註冊的其他機隨心跳加入）
fleet: dict[str, LiveState] = {}
