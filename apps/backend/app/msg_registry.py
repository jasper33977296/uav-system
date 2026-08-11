"""014 Phase B：泛型 MAVLink 訊息登錄表（per-drone message registry）。

QGC 的 MAVLink Inspector／vehicle-messages 同類：把每台機收到的每種訊息
（msgid）最新一筆、速率、欄位值原樣列出，讓研究者看機上真正在吐什麼。
**不設白名單**——所有 msgid 進表。單位取自 pymavlink 的 `fieldunits_by_name`
（與 QGC 同源＝MAVLink XML），值原樣、單位原樣（wire 單位如 degE7/mV/cdegC），
誠實配對不換算；bitmask 欄附 `displays` 提示（值仍原樣）。

與結構層（LiveState 的 lat/battery… 等萃取欄）互補：那些是本系統主動解讀、
驅動 UI 與研究的欄位；登錄表是「完整、未解讀」的一手鏡像。

契約（與前端 lib/store.ts 對過，115030d）：WS 廣播
  {type:"msg_registry", drone_id,
   sensors:[{name, ok}],                         # SYS_STATUS present 位、ok=health 位
   messages:[{id, name, hz, age_s, fields, units, displays?}]}
per-drone、每 tick 全量快照（前端整包替換，無狀態）。
"""
import math
import time

from pymavlink import mavutil

M = mavutil.mavlink

# SYS_STATUS 感測器位 → 友善短名（前端建議的 key）。未列的用 enum 名去前綴小寫
# （未知位原始位名，前端原樣顯示）。名稱權威來源＝MAVLink enum，非手維護清單。
_SENSOR_ALIAS = {
    "MAV_SYS_STATUS_SENSOR_3D_GYRO": "gyro",
    "MAV_SYS_STATUS_SENSOR_3D_ACCEL": "accel",
    "MAV_SYS_STATUS_SENSOR_3D_MAG": "mag",
    "MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE": "baro",
    "MAV_SYS_STATUS_SENSOR_GPS": "gps",
    "MAV_SYS_STATUS_SENSOR_BATTERY": "battery",
    "MAV_SYS_STATUS_SENSOR_MOTOR_OUTPUTS": "motor",
    "MAV_SYS_STATUS_SENSOR_RC_RECEIVER": "rc",
}


def _build_sensor_bits():
    """從 MAVLink enum 一次性建 [(bit, 友善名)]（present/health 位共用這張表）。"""
    out = []
    for bit, entry in M.enums.get("MAV_SYS_STATUS_SENSOR", {}).items():
        # 只收單一位（2 的冪）：排除 ENUM_END 哨兵（值＝max+1，非 2 的冪）與任何
        # 組合旗標——真正的感測器位全是個別的 2 的冪。
        if not isinstance(bit, int) or bit <= 0 or (bit & (bit - 1)) != 0:
            continue
        friendly = _SENSOR_ALIAS.get(
            entry.name, entry.name.replace("MAV_SYS_STATUS_SENSOR_", "")
                                  .replace("MAV_SYS_STATUS_", "").lower())
        out.append((bit, friendly))
    return sorted(out)


_SENSOR_BITS = _build_sensor_bits()
_EWMA_A = 0.3            # hz 速率的指數移動平均係數（平滑瞬時 1/dt 抖動）


def record(st, msg) -> None:
    """收到任一訊息就更新該機登錄表——在 mavlink_rx._handle 對每則 msg 呼叫
    （在 sysid→st 解出之後、型別分派之前，故無專屬 handler 的訊息也進表）。"""
    if msg.get_type() == "BAD_DATA":
        return                       # 解析失敗哨兵（非 MAVLink 型別，msgid=-2）——不入表
    try:
        mid = msg.get_msgId()
    except Exception:
        return                       # 取不到 msgid（極罕見）——略過，不擋資料路徑
    if mid < 0:
        return                       # 保險：任何負 msgid 哨兵都排除（勿讓其污染 hz 統計）
    now = time.monotonic()
    e = st.msg_registry.get(mid)
    if e is None:
        st.msg_registry[mid] = {"msg": msg, "last": now, "hz": None}
        return
    dt = now - e["last"]
    if dt > 0:
        inst = 1.0 / dt
        e["hz"] = inst if e["hz"] is None else _EWMA_A * inst + (1 - _EWMA_A) * e["hz"]
    e["msg"] = msg
    e["last"] = now


def _clean(v):
    """欄位值轉 JSON-safe：
    - float NaN/±Inf → None：瀏覽器 JSON.parse 不吃裸 NaN／Infinity，整包 throw
      （PX4 常見：POSITION_TARGET_LOCAL_NED 的 x/y/z、ALTITUDE 的 *_terrain）。
      **不可**改用 json allow_nan=False——那會變成後端 dumps 時 throw、整輪廣播掛。
    - bytes → utf-8（截到首個 NUL）失敗則 list。
    - list/tuple → 逐項遞迴（陣列欄位裡也可能藏 NaN）。"""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).split(b"\x00", 1)[0].decode("utf-8", "replace")
        except Exception:
            return list(v)
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _decompose(msg):
    """(fields, units, displays)：值原樣、單位／顯示提示取自 pymavlink 類別屬性
    （fieldunits_by_name／fielddisplays_by_name，與 QGC 同源）。只放有值的欄。"""
    cls = type(msg)
    units = getattr(cls, "fieldunits_by_name", {}) or {}
    displays = getattr(cls, "fielddisplays_by_name", {}) or {}
    d = msg.to_dict()
    d.pop("mavpackettype", None)
    fields = {k: _clean(v) for k, v in d.items()}
    u_out = {k: units[k] for k in fields if k in units}
    d_out = {k: displays[k] for k in fields if k in displays}
    return fields, u_out, d_out


def _sensors(sys_status_msg) -> list:
    """SYS_STATUS → [{name, ok}]：只發 present 位（有這顆），ok＝health 位。
    present=0 的直接省——「沒這顆」與「有但不健康(ok=false)」分明。"""
    present = getattr(sys_status_msg, "onboard_control_sensors_present", 0) or 0
    health = getattr(sys_status_msg, "onboard_control_sensors_health", 0) or 0
    return [{"name": name, "ok": bool(health & bit)}
            for bit, name in _SENSOR_BITS if present & bit]


def snapshot(st) -> dict:
    """該機登錄表的全量快照（broadcast 用）。"""
    now = time.monotonic()
    messages, sensors = [], []
    for mid, e in sorted(st.msg_registry.items()):
        msg = e["msg"]
        t = msg.get_type()
        fields, units, displays = _decompose(msg)
        entry = {
            "id": mid,
            "name": None if t.startswith("UNKNOWN_") else t,   # 未知 msgid→null，UI 顯 #id
            "hz": round(e["hz"], 2) if e["hz"] else None,
            "age_s": round(now - e["last"], 2),
            "fields": fields,
            "units": units,
        }
        if displays:
            entry["displays"] = displays
        messages.append(entry)
        if t == "SYS_STATUS":
            sensors = _sensors(msg)
    return {"sensors": sensors, "messages": messages}
