"""command 對共用驅動層的轉接（issue 026 B2）。

**能力判定已經搬到 `libs/autopilot/<廠牌>.py` 的 `Driver.capabilities()`**。
本檔剩下的是端點對映與轉出，讓既有呼叫端（`main.py` 的 gating）不必改形狀。

四態語意（PM 定案）對映到每鍵三值：
  可控   → "ok"          指令會照常送、已驗證
  未驗證 → "unverified"  方言未經 SITL 驗證＝僅觀察全鎖（含緊急鈕，比舊 guard 嚴）
  不可控 → "unsupported" 非 MAVLink／未知自駕儀
  （「受限」＝部分鍵 ok、部分非 ok，由逐鍵值自然表達）

**不要把新的能力規則加回這裡**——加在驅動裡，否則 backend 與 command 又會各自
長出一份判斷。
"""
from autopilot import CAP_KEYS, autopilot_name, get_driver  # noqa: F401

GCS_SYSID = 255            # 與 mav.GCS_SYSID 一致（2026-08-25 由 254 改）
#: **兩處各寫一份數字就是會漂移**，所以這裡加一個開機自檢：值不同時
#: 直接拒絕啟動，而不是讓能力判定拿著一個過期的數字去比對


_RAW_BY_NAME = {"px4": 12, "ardupilot": 3}


def capabilities_for(ap_name: str, ctx: dict | None = None):
    """回傳 (capabilities dict, reasons dict)。reasons 只含非 ok 的鍵。

    `ctx`＝該機的 router 狀態（可含 `sysid_mygcs`）。**前提可事前查證時就查**
    ——不要讓使用者按下按鈕、等一個永遠不會有的回應才發現前提沒滿足
    （ui-spec §0.2c 條款 6）。

    保留 `ap_name`（字串）介面：呼叫端手上就是字串，改成傳原值只是把轉換
    推給每個呼叫點。
    """
    return get_driver(_RAW_BY_NAME.get(ap_name)).capabilities(ctx)


# 端點 → 需要的能力鍵（gating 用）
ENDPOINT_CAP = {
    "arm": "arm", "disarm": "arm",
    "takeoff": "takeoff", "land": "land", "rtl": "rtl", "hold": "hold",
    "mission_upload": "mission_upload", "mission_start": "mission_start",
    "mission_fly": "mission_fly",
    # set_mode/{mode} 的 mode → 能力鍵
    "mode:rtl": "rtl", "mode:hold": "hold", "mode:land": "land",
    "mode:mission": "mission_start",
}
