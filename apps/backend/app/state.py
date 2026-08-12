"""即時狀態。單一 asyncio event loop 內讀寫，不需要鎖。

多機（2026-08-10 路線 B）：`fleet` 以 drone_id 為鍵持有每台機的
LiveState；`live` 仍是「主機」那台的 state 物件（fleet 裡同一個參照），
既有單機程式碼（api、模擬迴圈）不需改。mavlink_rx 依 sysid 建檔。
"""
import time as _time
from dataclasses import dataclass, field

# IMU 卡欄位契約（與前端 lib/store.ts ImuData 對齊；ui-spec §2.6）。單位：角速率/陀螺
# rad/s（前端轉 °/s 顯示）、加速度 m/s²、磁力 µT（HIGHRES_IMU 原生 gauss，後端 ×100）、
# 溫度 °C、壓力 hPa、氣壓高度 m、振動水平、clipping 計數。缺項→None（feature-detect）。
_IMU_KEYS = (
    "rollspeed", "pitchspeed", "yawspeed",              # ATTITUDE，rad/s
    "xacc", "yacc", "zacc",                             # HIGHRES_IMU，m/s²
    "xgyro", "ygyro", "zgyro",                          # HIGHRES_IMU，rad/s
    "xmag", "ymag", "zmag",                             # HIGHRES_IMU，µT
    "temperature", "abs_pressure", "pressure_alt",      # HIGHRES_IMU：°C／hPa／m
    "vibration_x", "vibration_y", "vibration_z",        # VIBRATION
    "clipping_0", "clipping_1", "clipping_2",           # VIBRATION 計數
)


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
    flight_mode: str | None = None        # 機端原廠模式名（不翻譯）
    mode_verb: str | None = None          # 廠牌無關語意（hold/mission/rtl/land/position）
    mode_pending: str | None = None    # mode_change 防抖候選（連續 2 次才算，見 mavlink_rx）
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
    sysid: int | None = None              # 該機當前 MAVLink sysid（前端選中機↔指令對象）
    # IMU 面板（即時頁抽屜；ui-spec §2.6）：ATTITUDE 角速率＋HIGHRES_IMU 加速度/陀螺/
    # 磁力/溫度/氣壓＋VIBRATION 振動/clipping。訊息高頻進、只在 WS 廣播率（5Hz）送最新。
    # feature-detect：機上沒發的欄位維持缺→telemetry_dict 補 None（前端顯「無資料」）。
    imu: dict = field(default_factory=dict)
    # 014 Phase B 泛型訊息登錄表：msgid → {msg, last(mono), hz}。mavlink_rx 對每則
    # 收到的訊息 record()，_msg_registry_loop 定時 snapshot 廣播（見 msg_registry.py）。
    msg_registry: dict = field(default_factory=dict)
    # 本架次的錄影現況（022）：'on'／'off'／'no_source'，**沒有進行中的架次時為
    # None**——前端據此決定記錄燈的說明文字；None／非 on 就維持原文案，不宣告
    # 自己不知道的事。詞彙沿用 flight_sessions.video_mode，不另造一套。
    video_mode: str | None = None
    # 機上參數表（021 Phase 2）：name → value。**唯讀快照**，用於實驗可重現性
    # （這一趟到底是用什麼設定飛的）。連線時抓一次，之後靠 PX4 改參數時主動
    # 廣播的 PARAM_VALUE 自動更新——只做連線那一次的話，「用 QGC 調完參數再飛」
    # 這個最常見的流程就會讓快照過期。param_total 是機端宣告的總數，
    # len(params)==param_total 才算抓完整。
    params: dict = field(default_factory=dict)
    param_total: int | None = None
    # serving cell 追蹤（換手事件；issue 002 教訓＝防抖）：serving_pci 是已確認的
    # 現任 PCI，pci_pending 是待確認候選（連續 2 次才算換手，事件層防抖）
    serving_pci: int | None = None
    serving_band: str | None = None
    pci_pending: int | None = None

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

    def readiness(self) -> tuple[bool | None, list]:
        """就緒判定＋不就緒原因（給前端顯示；權威訊號是 prearm_ok）。

        **沒有依據時回 None（未知），不回 True。** 「就緒」是對飛安狀態的斷言，
        而剛連上、只收到心跳的那段時間，我們對預檢／GPS／感測器一無所知——
        那時候說「就緒」是憑空斷言。實測抓到（2026-08-12 ArduPilot 驗收機）：
        位置／GPS／電量全 null，卻因為「沒有任何反對證據」而回報 ready=true，
        前端據此點了綠燈。**缺乏證據不是通過的理由。**

        與本專案一貫做法同源：origin 不明留 `unknown` 不強標、影像零片段分
        `missing`／`off`／`no_source`、msg_registry 停掉的訊息 hz 留 null。
        """
        from .dialect import prearm_label     # 就地 import 避免載入序循環

        reasons = []
        if self.prearm_ok is False:
            reasons.append(f"{prearm_label(self.autopilot_raw)} 預檢未過（arming checks）")
        reasons += [f"感測器異常：{s}" for s in self.sensors_unhealthy]
        if self.ekf_ok is False:
            reasons.append("EKF 未就緒")
        if self.gps_fix is not None and self.gps_fix < 3:
            reasons.append(f"GPS 未定位（fix={self.gps_fix}）")
        if self.mav_state in ("CRITICAL", "EMERGENCY", "FLIGHT_TERMINATION"):
            reasons.append(f"failsafe 狀態：{self.mav_state}")
        if not reasons:
            # 沒有反對證據 ≠ 就緒——還要有**權威依據**才敢斷言「可飛」。
            # 權威訊號只有 prearm_ok（PX4 的 arming checks 總結）與 ekf_ok；
            # **GPS 好不算數**：GPS 定位良好但預檢未過（羅盤未校正、EKF 未收斂…）
            # 完全可能，拿 GPS 當「就緒」的依據就是用次級訊號冒充權威判斷。
            # 反向證據仍然有效——GPS 未定位這類「我們知道它不行」照樣回 False。
            if self.prearm_ok is None and self.ekf_ok is None:
                return None, ["尚未收到預檢／EKF 狀態，無法判定就緒"]
        # prearm_ok=None＝韌體不回報 PREARM 位元，退回次級訊號判定
        return (not reasons and self.prearm_ok is not False), reasons

    def telemetry_dict(self) -> dict:
        ready, reasons = self.readiness()
        from .dialect import autopilot_name      # 就地 import 避免載入序循環
        return {
            "ready": ready,
            "not_ready_reasons": reasons,
            "autopilot": autopilot_name(self.autopilot_raw),  # px4/ardupilot/unknown
            "mav_sysid": self.sysid,          # 前端：選中機（drone_id）→ 指令對象（sysid）
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
            # IMU 卡：固定形狀（缺欄補 None，前端 feature-detect 顯「無資料」）
            "imu": {k: self.imu.get(k) for k in _IMU_KEYS},
            "ground_speed": self.ground_speed,
            "vertical_speed": self.vertical_speed,
            "battery_pct": self.battery_pct,
            "battery_voltage": self.battery_voltage,
            "gps_fix": self.gps_fix, "satellites": self.satellites,
            "flight_mode": self.flight_mode, "armed": self.armed,
            # 顯示用 flight_mode（原廠名），判斷/分組用 mode_verb——PX4 的 HOLD
            # 與 ArduPilot 的 LOITER 是同一件事，前端不該靠比字串知道這件事
            "mode_verb": self.mode_verb,
            "link": self.link,
            # 錄影現況（022 §2.9 記錄燈說明用）：無進行中架次＝None
            "video_mode": self.video_mode,
            "link_state": self.link_state,
            "link_age_s": self.link_age_s,   # None = 從未收到；大於門檻 = 失聯
        }


live = LiveState()

# 全機隊：drone_id → LiveState（主機也在裡面，值就是上面的 live 物件；
# 由 main.lifespan 放入，mavlink_rx 自動註冊的其他機隨心跳加入）
fleet: dict[str, LiveState] = {}
