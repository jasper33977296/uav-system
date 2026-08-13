"""backend 對共用驅動層的轉接（issue 026 B2）。

**方言知識已經全部搬到 `libs/autopilot/`**（兩個服務共用同一份，編譯期就一致，
不需要漂移偵測）。本檔剩下的職責只有一件：**把 backend 既有的呼叫形狀轉成驅動
介面的呼叫形狀**，讓 `mavlink_rx.py` 不必在每個呼叫點自己取驅動。

B0 時這裡是方言的家；B2 之後它是門口。**不要把新的廠牌知識加回這裡**——加在
`libs/autopilot/<廠牌>.py`，否則兩個服務又會各自長出一份。
"""
from autopilot import autopilot_name, get_driver  # noqa: F401  （轉出給既有呼叫端）

# ── 訊息層等價：哪些訊息型別要當成同一件事處理 ────────────────────
# 這份集合是從**所有驅動的 MESSAGE_ADJUSTMENTS 推導**出來的，不是手寫清單——
# 手寫的話，新增驅動時這裡會漏（而漏掉的症狀是「某廠牌的 EKF 永遠是 None」，
# 沒有任何錯誤訊息）。
def _ekf_types() -> tuple[str, ...]:
    names = {"ESTIMATOR_STATUS"}
    for raw in (12, 3):
        for eq in get_driver(raw).MESSAGE_ADJUSTMENTS:
            if eq.dst_type == "ESTIMATOR_STATUS":
                names.add(eq.src_type)
    return tuple(sorted(names))


EKF_MSG_TYPES = _ekf_types()

# 就緒所需的 EKF 位元：姿態＋水平速度＋水平絕對位置。
# 兩家在**這三個位元**上同義——但只到 bit 512 為止，詳見驅動的
# MESSAGE_ADJUSTMENTS.safe_field_bits 與 scripts/test-dialect-boundary.py。
EKF_READY_BITS = 0x0013
EKF_ALIAS_SAFE_BITS = 0x03FF


def ekf_ready(flags: int) -> bool:
    return (flags & EKF_READY_BITS) == EKF_READY_BITS


def mode_name(custom_mode: int, autopilot_raw=None) -> str:
    return get_driver(autopilot_raw).decode_mode(custom_mode)


def mode_verb(custom_mode: int, autopilot_raw=None) -> str | None:
    """廠牌無關的模式語意（見驅動的 decode_verb）。"""
    return get_driver(autopilot_raw).decode_verb(custom_mode)


def decode_param(value, param_type=None, autopilot_raw=None):
    """PARAM_VALUE 的值 → 真正的參數值（廠牌慣例不同，見驅動）。"""
    return get_driver(autopilot_raw).decode_param(value, param_type)


def needs_stream_request(autopilot_raw) -> bool:
    return bool(get_driver(autopilot_raw).on_connect())


def prearm_label(autopilot_raw) -> str:
    """預檢未過的原因文字。廠牌名不寫死——見 B0 的說明。"""
    return {"px4": "PX4", "ardupilot": "ArduPilot"}.get(
        autopilot_name(autopilot_raw), "自駕儀")
