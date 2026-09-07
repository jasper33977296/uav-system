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

#: 預檢失敗原因多久沒再聽到就當它已經解決（秒）。ArduPilot 實測約每分鐘
#: 重講一次，取 3 分鐘＝漏掉兩次也還算數。**判準是「沒有再聽到」而不是
#: 「時間到了」**：只要它還在講，那一項就還在。
PREARM_TTL_S = 180.0

#: `MISSION_CURRENT.mission_state`（MAV_MISSION_STATE）→ 名字。**存的是數字、
#: 顯示才翻譯**：數字是機端說的原始事實，翻譯是我方的解讀，兩者分開放，
#: 日後翻錯了還原得回去（與 `mission_seq` 不換算是同一條紀律）。
MISSION_STATE = {
    0: "unknown",        # 機端沒說
    1: "no_mission",     # 沒有任務
    2: "not_started",    # 有任務，還沒開始
    3: "active",         # 正在飛
    4: "paused",         # 暫停
    5: "complete",       # 列在 MAV_MISSION_STATE 裡，但本機韌體實測不送
}


@dataclass
class LiveState:
    drone_id: str | None = None
    drone_name: str | None = None    # 事件流等 UI 顯示用（多機時必須能分辨）
    session_id: str | None = None
    connected: bool = False          # MAVLink 連線狀態（近期有訊息）
    #: 這台機**曾經**產生過遙測嗎。用來分辨兩種完全不同的「沒有資料」：
    #: 斷線（有最後已知位置，值得顯示）vs 從未連上（什麼都沒有，不該佔畫面）。
    #: 主機在啟動時就會被放進 fleet（見 main.py），所以「在 fleet 裡」不等於
    #: 「連過」——沒有這個旗標就分不出來。
    ever_connected: bool = False

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
    #: 機上自己說的預檢失敗原因（`PreArm: …` STATUSTEXT）→ 最後聽到的時刻
    #: （單調時鐘）。**`prearm_ok is False` 只說得出「有一項沒過」**，說不出
    #: 是哪一項——而那一句話飛控本來就在講，只是原本只進了事件流。
    #: 2026-09-02 實測：真機每分鐘噴一則 `PreArm: Battery 1 low voltage
    #: failsafe`，而畫面上只寫「預檢未過」，操作員得自己去事件流裡翻。
    prearm_msgs: dict = field(default_factory=dict)
    battery_voltage: float | None = None
    #: 電流（A）與從上電起的累積消耗（mAh）——**都來自 `BATTERY_STATUS`**。
    #: 這兩個原本只存在於 014 的原始層，`telemetry` 表沒有：於是「待機能撐
    #: 多久」「電流刻度準不準」這種問題只能去翻幾十 MB 的 tlog（2026-09-07）。
    #:
    #: **`consumed_mah` 是從上電起算的累加器，不是總量**：斷電歸零，所以它
    #: 只在同一段供電裡有意義。跨段比較要看時間戳有沒有跨過重新上電。
    battery_current: float | None = None
    battery_consumed_mah: float | None = None
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
    #: 機端正在飛第幾個任務項（MISSION_CURRENT.seq）。**這是機端的 seq，不是
    #: 我方航點索引**——ArduPilot 把 home 當 seq 0，兩者相差 1（issues/026 差異 5）。
    #: 換算是驅動層的事，這裡只忠實記錄機端說的數字。
    mission_seq: int | None = None
    mission_total: int | None = None       # 機端任務總項數（新韌體才有）
    #: 機端對「這個任務現在怎麼了」的說法（`MISSION_CURRENT.mission_state`，
    #: ArduPilot 4.5+／PX4 新韌體才有）。None＝舊韌體沒這欄。
    #:
    #: **實測本機（9/2 七趟，912 則 MISSION_CURRENT）從來沒有送過 5=complete。**
    #: 飛完的樣子是 `active → not_started`＋最後一項有 MISSION_ITEM_REACHED。
    #: 所以「飛完了」不是靠某一個欄位認出來的，是靠三件事湊出來的——這也是
    #: 為什麼三種事件都要記，少一種就湊不出來。
    mission_state: int | None = None
    autopilot_raw: int | None = None      # MAV_AUTOPILOT_*（方言分表；issue 015）
    #: 這台機的身分對得上這筆記錄嗎（issues/038 比對半邊）。False＝sysid 撞號、
    #: 新來的機不是這筆記錄原本那台。**資料從此不記在這筆記錄名下**——
    #: 混料比斷線嚴重，而且它是靜默發生的
    identity_ok: bool = True
    identity_reason: str | None = None
    #: 遙控器接收機在不在（issues/014 結構層／039 複裁 A）。**三態**：
    #: True／False／None＝不知道（韌體沒回報 present 位元）。
    #: 判準與機上代理**逐字相同**——不同的話 crosscheck 會噴出一堆假的不一致，
    #: 而那比沒有比對更糟（真的不一致會淹在裡面）
    rc_link: bool | None = None
    #: DB 記著的期望值（認領時回填）。**與 board_uid／autopilot_raw 分開存**：
    #: 後者是「機端現在說它是誰」，前者是「這筆記錄本來是誰」——混成一個欄位
    #: 就再也比不出來了
    expect_board_uid: str | None = None
    expect_autopilot: int | None = None
    # ── 板子身分（038）：AUTOPILOT_VERSION 帶的硬體識別 ──────────────
    #: 飛控板的唯一 ID（`uid2`，十六進位字串）。**這是目前唯一機器可驗證的
    #: 身分**——sysid 只是機上一個可以隨時改的參數，換板子、重刷韌體、
    #: 兩台都用預設 1 號，系統都分不出來（issues/038）。
    #: ⚠ 它認的是**飛控板**不是機架：板子拆到別台飛機上，這個 ID 跟著板子走。
    #: 機架序號只能由人維護。
    board_uid: str | None = None
    board_version: int | None = None
    board_vendor_id: int | None = None
    board_product_id: int | None = None
    flight_sw_version: str | None = None  # 已解碼的人話版本，如 "4.7.0 (official)"
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
    #: 失去遙測是從哪一刻開始（monotonic）。架次收尾的寬限期用它算，
    #: None＝目前沒有失聯
    _lost_since: float | None = None
    #: 最後一次收到**這台機的 MAVLink 訊息**（monotonic）。與 link_seen_mono
    #: 不同：那個是 5G 鏈路量測，這個是飛行遙測本身。
    #: **A 層（顯示）要的是這個**——畫面上的高度、模式、電量全部來自它，
    #: 所以「這些數字多舊」只能由它回答
    telem_seen_mono: float | None = None
    #: 目前這段失明的 blackout 記錄 id（B 層）。None＝沒有進行中的失明
    blackout_id: str | None = None

    # 最後一次收到鏈路量測的時刻（monotonic clock，不受系統時間調整影響）。
    # 真機的即時通道會靜默失敗，前端需要據此顯示「已失聯 N 秒」——
    # 那與 link_lost 是不同的事：link_lost 是量到訊號差，失聯是量測送不回來。
    link_seen_mono: float | None = None

    def mark_link_seen(self) -> None:
        self.link_seen_mono = _time.monotonic()

    def _link_state_now(self) -> str:
        """鏈路狀態要跟著資料年齡走。模擬路徑另有 SINR 分級的狀態機
        （link_events.transition），那時 link_state 已經是新鮮的判斷；
        真機路徑沒有那條，就用年齡說話——**不知道多久沒資料時不說 ok**。"""
        age = self.link_age_s
        if age is None:
            return "unknown"
        if age > 30:
            return "lost"
        if age > 5:
            return "stale"
        return self.link_state

    @property
    def telem_age_s(self) -> float | None:
        """畫面上那些數字有多舊（秒）。None＝從來沒收到過。

        **A 層的核心**：斷線時數值不會消失，它們只是變舊——而舊到某個程度
        之後，它們與「現在」的關係就只剩誤導。2026-08-26 實測：畫面顯示
        `armed=true / LAND / alt 1.07`，那是**兩個半小時前**的殘影，
        而畫面上唯一的線索是角落一個 `connected:false`。
        """
        if self.telem_seen_mono is None:
            return None
        return round(_time.monotonic() - self.telem_seen_mono, 2)

    @property
    def link_age_s(self) -> float | None:
        """距上次收到鏈路量測幾秒。由後端計算，避免前後端時鐘偏差。"""
        if self.link_seen_mono is None:
            return None
        return round(_time.monotonic() - self.link_seen_mono, 2)

    def prearm_said(self) -> list[str]:
        """機上此刻還在講的預檢失敗原因。

        **要過期。** ArduPilot 只在被擋住時週期性重講（實測約每分鐘一次），
        所以「很久沒再聽到」多半代表那一項已經解決了——把它繼續掛在畫面上，
        就是拿一個已經不成立的理由擋人。過期的判準是**沒有再聽到**，
        不是「時間到了」：只要它還在講，這一項就還在。
        """
        now = _time.monotonic()
        return [t for t, at in sorted(self.prearm_msgs.items(), key=lambda kv: -kv[1])
                if now - at < PREARM_TTL_S]

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
            label = prearm_label(self.autopilot_raw)
            said = self.prearm_said()
            if said:
                # **說得出是哪一項就說**——「預檢未過」是一句不可行動的話，
                # 而「Battery 1 low voltage failsafe」是一句可以去處理的話
                reasons += [f"{label} 預檢未過：{t}" for t in said]
            else:
                # 沒聽到機上說（韌體不講、或它講過而我們是之後才連上的）。
                # **要說出「不知道是哪一項」**，不要讓它看起來像一句完整的原因
                reasons.append(f"{label} 預檢未過（機上還沒說是哪一項）")
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
            # 038：板子身分。**不是每台機都有**——要主動請求 AUTOPILOT_VERSION
            # 才拿得到，還沒問到時是 None（誠實的「不知道」）
            "mission_seq": self.mission_seq,
            "mission_total": self.mission_total,
            "mission_state": MISSION_STATE.get(self.mission_state),
            "board_uid": self.board_uid,
            "flight_sw_version": self.flight_sw_version,
            "mav_sysid": self.sysid,          # 前端：選中機（drone_id）→ 指令對象（sysid）
            "mav_state": self.mav_state,
            "landed_state": self.landed_state,
            "prearm_ok": self.prearm_ok,
            "ekf_ok": self.ekf_ok,
            "sensors_unhealthy": self.sensors_unhealthy,
            "drone_id": self.drone_id,
            "drone_name": self.drone_name,
            "session_id": self.session_id,
            "connected": self.connected, "ever_connected": self.ever_connected,
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
            "battery_current": self.battery_current,
            "battery_consumed_mah": self.battery_consumed_mah,
            "gps_fix": self.gps_fix, "satellites": self.satellites,
            "rc_link": self.rc_link,
            "identity_ok": self.identity_ok,
            "identity_reason": self.identity_reason,
            "flight_mode": self.flight_mode, "armed": self.armed,
            # 顯示用 flight_mode（原廠名），判斷/分組用 mode_verb——PX4 的 HOLD
            # 與 ArduPilot 的 LOITER 是同一件事，前端不該靠比字串知道這件事
            "mode_verb": self.mode_verb,
            "link": self.link,
            # 錄影現況（022 §2.9 記錄燈說明用）：無進行中架次＝None
            "video_mode": self.video_mode,
            # **link_state 不能在資料過期時還說 ok。** 它原本只在模擬路徑上
            # 由 link_transition 更新，真機路徑設一次「ok」就再也不動——
            # 2026-08-26 看到 link_state=ok 配 link_age_s=9169（2.5 小時）。
            # 一個說「正常」、一個說「兩個半小時沒資料」，**同一份回應自相矛盾**
            "link_state": self._link_state_now(),
            # **每一個數值的年齡**（A 層）：前端據此決定顯示、變灰、或換成
            # 「最後已知」。None＝從來沒收到過
            "telem_age_s": self.telem_age_s,
            "link_age_s": self.link_age_s,   # None = 從未收到；大於門檻 = 失聯
        }


live = LiveState()

# 全機隊：drone_id → LiveState（主機也在裡面，值就是上面的 live 物件；
# 由 main.lifespan 放入，mavlink_rx 自動註冊的其他機隨心跳加入）
fleet: dict[str, LiveState] = {}
