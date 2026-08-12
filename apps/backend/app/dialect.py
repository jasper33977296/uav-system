"""backend 端方言知識的**單一位置**（issue 026 B0）。

在此之前，backend 的廠牌差異散落在 `mavlink_rx.py` 各處（模式表、串流補送、
EKF 訊息名）與 `state.py`（就緒原因文字）。command 端上一輪已經收斂到
`app/mav.py` 的 `dialect()`；本檔是 backend 端的對應物，命名刻意對齊。

**收斂先於搬遷**：B1／B2 要把這些搬進 `libs/autopilot/` 的驅動介面，屆時是
「提取一處」而不是「全域搜捕」。本檔不定義驅動介面，只把知識集中。

## 兩類方言（QGC 對照後的分類，見 doc/autopilot-driver-architecture.md §5.3）

QGC 的 `FirmwarePlugin` 除了動詞，還有 `adjustIncomingMavlinkMessage`——而且
**PX4 與 APM 兩家都覆寫**，代表它是通用需求而非某廠牌的髒補丁。對照後我們才
發現方言不只存在於動詞層，還存在於訊息層：

- **§1 訊息層**：同一件事、不同訊息名／欄位名。正規化成單一標準形之後，下游
  完全不必知道廠牌。→ 未來的 `Driver.adjust_incoming()`（差異 12）
- **§2 解讀層**：同一個值、不同意義，必須知道語意才解得開。→ 未來的
  `Driver.decode_mode()`／`readiness()`／`on_connect()`（差異 2、6、7）

**判準（B1 將入規格）：需要知道「值代表什麼意思」的，就不屬於訊息層。**
正規化只認結構、不認語意——這一刀讓 `adjust_*` 不會退化成什麼都往裡塞的
垃圾抽屜。
"""

# ── §1 訊息層：等價訊息 ────────────────────────────────────────────
# 同一件事兩個訊息名：PX4 發 ESTIMATOR_STATUS、ArduPilot 發 EKF_STATUS_REPORT
# （015 實測）。只解 PX4 那個的話，ArduPilot 的 ekf_ok 永遠是 None——而
# ArduPilot 又不回報 PREARM 位，於是 readiness 永遠判不出來。
EKF_MSG_TYPES = ("ESTIMATOR_STATUS", "EKF_STATUS_REPORT")

# **這個等價只在 bit 1..512 成立，不是整個 enum。** 實測 pymavlink
# ardupilotmega 方言（2026-08-12）：
#
#     bit 1..512   ESTIMATOR_* 與 EKF_* 逐位同名同義（ATTITUDE／VELOCITY_*／
#                  POS_*／CONST_POS_MODE／PRED_POS_*）        → 可直接互換
#     bit 1024     ESTIMATOR_GPS_GLITCH  vs  EKF_UNINITIALIZED  → **同位元、
#                  不同意義**。當成同一個東西讀會得到靜默錯誤的結論
#     bit 32768    EKF_GPS_GLITCHING（ArduPilot 專有，ESTIMATOR_* 無對應）
#
# 所以「把 EKF_STATUS_REPORT 當 ESTIMATOR_STATUS 讀」是**有邊界的**正規化。
# 我們目前只用到 1|2|16，全在安全區內；這行常數存在的目的是：日後有人要讀
# 第 1024 位時，會先撞到這段說明而不是撞到一個錯誤的判斷。
EKF_ALIAS_SAFE_BITS = 0x03FF          # bit 1..512

# 就緒所需的 EKF 位元：姿態＋水平速度＋水平絕對位置。
EKF_READY_BITS = 0x0013               # ATTITUDE(1) | VELOCITY_HORIZ(2) | POS_HORIZ_ABS(16)


def ekf_ready(flags: int) -> bool:
    """EKF 是否收斂到可飛。兩家的 flags 在所需位元上同義（見上方邊界說明）。"""
    return (flags & EKF_READY_BITS) == EKF_READY_BITS


# ── §2 解讀層：要知道語意才解得開的 ────────────────────────────────
_AUTOPILOT_NAMES = {12: "px4", 3: "ardupilot"}   # MAV_AUTOPILOT_PX4 / _ARDUPILOTMEGA

# custom_mode → 人話。**方言分表**（issue 015／gap-analysis §2）：PX4 是
# main<<16|sub<<24 的 union；ArduPilot 是整數模式號（隨載具型別而異，此處
# Copter；Plane/Rover 另表，待驗）。名稱對齊前端顯示，前端純顯示不動。
_PX4_MAIN = {1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 5: "ACRO",
             6: "OFFBOARD", 7: "STABILIZED", 8: "RATTITUDE"}
_PX4_AUTO = {1: "READY", 2: "TAKEOFF", 3: "HOLD", 4: "MISSION",
             5: "RETURN_TO_LAUNCH", 6: "LAND", 8: "FOLLOW_ME", 9: "PRECLAND"}
# ArduPilot Copter 模式號（ardupilot/copter-mode.h）
_ARDU_COPTER = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
                5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT",
                13: "SPORT", 16: "POSHOLD", 17: "BRAKE", 18: "THROW",
                20: "GUIDED_NOGPS", 21: "SMART_RTL", 27: "AUTO_RTL"}


def autopilot_name(raw) -> str:
    return _AUTOPILOT_NAMES.get(raw, "unknown")


def mode_name(custom_mode: int, autopilot_raw=None) -> str:
    """HEARTBEAT.custom_mode → 顯示用模式名（差異 2）。"""
    if not custom_mode:              # 0＝尚未設定模式（開機瞬間），不顯 MODE_0
        return "—"
    if autopilot_name(autopilot_raw) == "ardupilot":
        return _ARDU_COPTER.get(custom_mode, f"MODE_{custom_mode}")
    # 預設 PX4（含 unknown 暫按 PX4 解，維持既有行為）
    main, sub = (custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF
    if main == 4:
        return _PX4_AUTO.get(sub, f"AUTO_{sub}")
    return _PX4_MAIN.get(main, f"MODE_{main}")


def needs_stream_request(autopilot_raw) -> bool:
    """要不要主動要求對方送遙測（差異 6）。

    015 實測：**ArduPilot 預設幾乎不送遙測**——我方只收得到 HEARTBEAT／
    PARAM_VALUE／STATUSTEXT／TIMESYNC 四種，沒有位置、GPS、電量、SYS_STATUS。
    送一次 REQUEST_DATA_STREAM 後變 32 種。PX4 預設就串流，不需要也不送。
    """
    return autopilot_name(autopilot_raw) == "ardupilot"


def prearm_label(autopilot_raw) -> str:
    """預檢未過的原因文字（差異 7 的殘留）。

    原本寫死「PX4 預檢未過」。ArduPilot 不回報 PREARM 位元、`prearm_ok` 恆為
    None，所以這行今天永遠不會對 ArduPilot 觸發——**但那是「條件剛好沒踩到」
    而不是「寫對了」**。哪天某廠牌開始回報，錯的廠牌名會直接顯示給操作員，
    而沒有任何機制會發現。改成依實際廠牌取名。
    """
    ap = autopilot_name(autopilot_raw)
    return {"px4": "PX4", "ardupilot": "ArduPilot"}.get(ap, "自駕儀")
