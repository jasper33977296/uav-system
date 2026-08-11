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
        r = "ArduPilot 方言未經 SITL 驗證，僅觀察（見 issue 015）"
        return {k: "unverified" for k in CAP_KEYS}, {k: r for k in CAP_KEYS}
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
