"""每機能力描述子（issue 015）：capabilities 是**伺服器端 gating 的唯一真相**，
同時給前端 UI——UI 與實際放行永不背離（descriptor 驅動）。

四態語意（PM 定案）對映到每鍵三值：
  可控   → "ok"          指令會照常送、已驗證
  未驗證 → "unverified"  方言未經 SITL 驗證＝僅觀察全鎖（含緊急鈕，比舊 guard 嚴）
  不可控 → "unsupported" 非 MAVLink／未知自駕儀
  （「受限」＝部分鍵 ok、部分非 ok，由逐鍵值自然表達）

短期靜態表：PX4 全 ok；ArduPilot 全 unverified（待可攜指令＋SITL 驗收後
逐鍵開 ok）；unknown 全 unsupported。可攜 RTL/Hold（NAV_RETURN_TO_LAUNCH／
NAV_LOITER_UNLIM）在某機型 SITL 驗過，就把該機的 rtl/hold 開 "ok"——
前端零改動自動開鈕、後端同步放行。
"""

# 鍵名對齊 command 端點語意（disarm 前端跟 arm 同值，不另列）
CAP_KEYS = ["arm", "takeoff", "land", "rtl", "hold",
            "mission_upload", "mission_start", "mission_fly", "manual"]

AUTOPILOT_NAMES = {12: "px4", 3: "ardupilot"}   # MAV_AUTOPILOT_PX4 / _ARDUPILOTMEGA


def autopilot_name(raw) -> str:
    return AUTOPILOT_NAMES.get(raw, "unknown")


def capabilities_for(ap_name: str):
    """回傳 (capabilities dict, reasons dict)。reasons 只含非 ok 的鍵。"""
    if ap_name == "px4":
        return {k: "ok" for k in CAP_KEYS}, {}
    if ap_name == "ardupilot":
        # 015 驗收進行中：**逐鍵開**，不整組開。只有實際驗過的鍵才是 ok。
        r = "ArduPilot 方言未經 SITL 驗證，僅觀察（見 issue 015）"
        caps = {k: "unverified" for k in CAP_KEYS}
        reasons = {k: r for k in CAP_KEYS}
        # mission_upload：方言分支（home 佔 seq 0）已實作並以 SITL 驗證
        # **逐鍵開，只開實際在 SITL 驗過的**（2026-08-12 015 驗收）：
        #   mission_upload：方言分支（home 佔 seq 0）＋回讀逐項比對通過
        #   hold/rtl/land：DO_SET_MODE 用 ArduPilot 模式號，且**讀回 HEARTBEAT
        #     確認真的切到**（mode_engaged=True），不是只看 ACK
        #   arm/takeoff：GUIDED→arm→NAV_TAKEOFF（相對高度、空白參數用 0 不用
        #     NaN）實測爬到 15.0m
        for _k in ("mission_upload", "hold", "rtl", "land", "arm", "takeoff"):
            caps[_k] = "ok"
            reasons.pop(_k, None)
        # 以下**維持 unverified**（沒驗過就不開）：
        #   manual：卡在機端 SYSID_MYGCS（我方 254 vs 預設 255），且 MANUAL_CONTROL
        #     沒有 ACK——能不能偵測失敗待設計師定案（見 issue 015）
        #   mission_start/mission_fly：任務執行整段尚未驗（AUTO 模式起跑行為未測）
        reasons["manual"] = "需機端設定 SYSID_MYGCS=254，且我方無法確認是否已設（issue 015）"
        return caps, reasons
    r = "非 MAVLink 或未知自駕儀，不支援指令"
    return {k: "unsupported" for k in CAP_KEYS}, {k: r for k in CAP_KEYS}


# 端點 → 需要的能力鍵（gating 用）
ENDPOINT_CAP = {
    "arm": "arm", "disarm": "arm",
    "takeoff": "takeoff", "land": "land", "rtl": "rtl", "hold": "hold",
    "mission_upload": "mission_upload", "mission_start": "mission_start",
    "mission_fly": "mission_fly", "manual": "manual",
    # set_mode/{mode} 的 mode → 能力鍵
    "mode:rtl": "rtl", "mode:hold": "hold", "mode:land": "land",
    "mode:mission": "mission_start", "mode:position": "manual",
}
