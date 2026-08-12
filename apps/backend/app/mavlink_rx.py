"""MAVLink 接收核心：單 socket、多機（sysid demux）、原始層錄製一體。

路線 B（2026-08-10 定案，issues/011）：mavsdk 退役——pymavlink 解碼、
capture 與 ingest 合體、零副程序。每個 datagram：

    先落盤 tlog（原始層，無損）→ 解碼 → 依 sysid 更新該機 LiveState（結構層）

「read-only」原則的精確表述（同日修正）：**不含改變機上狀態的能力**，
而非「只收不發」——本模組可發送的訊息類型由 SEND_WHITELIST 管制，
只有任務下載的查詢類；指令類（arm/上傳/切模式）物理上不存在於本服務
（那是 command 服務的職權，14541）。

- 新 sysid（自駕儀心跳）自動註冊 drones 列；既有部署升級時優先認領
  「mav_sysid 為空的主機」——單機環境不會因此多出一台幽靈機
- armed 轉換＝各機自己的架次邊界（沿用原 ingest.py 的賦值順序紀律：
  先建 session 再標 armed、先清旗標再結算）
- STATUSTEXT → 事件流（issue 014 結構層第一批：PX4 的警告與拒絕原因）
- 同 sysid 換來源位址 → 撞號告警（兩台同 sysid ＝ 靜默混料，必須看得見）
"""
import os

os.environ.setdefault("MAVLINK20", "1")

import asyncio
import logging
import math
import time

from pymavlink import mavutil

from . import db, msg_registry, video_rec
from .capture import Recorder
from .config import settings
from .state import LiveState, fleet, live
from .ws import manager

log = logging.getLogger(__name__)
M = mavutil.mavlink
GCS_SYSID = 254
ADDR_WARN_COOLDOWN = 30.0    # 同 sysid 撞號告警去抖（秒）：避免告警風暴
STX_CHUNK_LEN = 50           # STATUSTEXT text 欄位長度：滿 50＝還有下一段
STX_STALE_S = 3.0            # 分段末段掉包，殘段逾時丟棄（秒）
STX_FOLD_S = 15.0            # 同句連續重複在此窗內折疊計數（秒）

# read-only 邊界的實體：能離開這個 socket 的訊息類型只有這三種（任務下載
# 的查詢對話）。要發任何別的，這行 assert 就是攔你的人。
# 021 Phase 2 新增 PARAM_REQUEST_LIST／PARAM_REQUEST_READ：**唯讀查詢**，符合本
# 模組的邊界定義（「不含改變機上狀態的能力」）。**PARAM_SET 永遠不得加入**——
# 參數編輯是 QGC 的職權，本系統只記錄「當時設定是什麼」，不去改它。
SEND_WHITELIST = {"MISSION_REQUEST_LIST", "MISSION_REQUEST_INT", "MISSION_ACK",
                  "PARAM_REQUEST_LIST", "PARAM_REQUEST_READ"}

# custom_mode → 人話。**方言分表**（issue 015／gap-analysis §2）：PX4 是
# main<<16|sub<<24 的 union；ArduPilot 是整數模式號（隨載具型別而異，此處
# Copter；Plane/Rover 另表，待驗）。名稱對齊前端顯示，前端純顯示不動。
_AUTOPILOT_NAMES = {12: "px4", 3: "ardupilot"}   # MAV_AUTOPILOT_PX4 / _ARDUPILOTMEGA
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


def _mode_name(custom_mode: int, autopilot_raw=None) -> str:
    if not custom_mode:              # 0＝尚未設定模式（開機瞬間），不顯 MODE_0
        return "—"
    if autopilot_name(autopilot_raw) == "ardupilot":
        return _ARDU_COPTER.get(custom_mode, f"MODE_{custom_mode}")
    # 預設 PX4（含 unknown 暫按 PX4 解，維持既有行為）
    main, sub = (custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF
    if main == 4:
        return _PX4_AUTO.get(sub, f"AUTO_{sub}")
    return _PX4_MAIN.get(main, f"MODE_{main}")


# MAV_SEVERITY(0-7) → 事件層級；7=DEBUG 不入流。EVENT 的外層 log level 也用
# 同一枚舉，另有 8=Protocol（框架內部、非給人看）落在表外 → 自然丟棄。
_SEVERITY = {0: "critical", 1: "critical", 2: "critical", 3: "critical",
             4: "warning", 5: "info", 6: "info"}


def _decode_event(msgbuf) -> dict | None:
    """手工解 MAVLink EVENT（msg 410）裸 frame（issue 014 Phase A.2）。

    PX4 1.14 的 vehicle 通知（Armed/Takeoff…）走 Events 協定、**不走
    STATUSTEXT**（實測：整天 tlog STATUSTEXT=0、EVENT=65）。當前 pymavlink
    方言未定義 410（顯示 UNKNOWN_410、payload 解不出），故從 frame bytes 手解。
    人話文字要逐韌體 event metadata 才翻得出（QGC 那套）——本階段先給
    severity＋event id＋args，文字翻譯排後續（metadata 落地時同一列自動升級）。

    EVENT 欄位線序（MAVLink2 依型別大小排序）：id(u32) event_time_boot_ms(u32)
    sequence(u16) destination_component(u8) destination_system(u8)
    log_levels(u8) arguments(u8[40])＝共 53 bytes；MAVLink2 尾零截斷，補回。
    """
    mb = bytes(msgbuf)
    if len(mb) < 13 or mb[0] != 0xFD:        # 只認 MAVLink2 frame
        return None
    plen = mb[1]
    payload = mb[10:10 + plen]
    payload = payload + b"\x00" * (53 - len(payload))
    ev_id = int.from_bytes(payload[0:4], "little")
    seq = int.from_bytes(payload[8:10], "little")
    log_levels = payload[12]
    args = payload[13:53].rstrip(b"\x00")
    return {"event_id": ev_id, "seq": seq,
            "severity_ext": log_levels & 0x0F, "args_hex": args.hex()}

# 飛行就緒訊號（QGC「Ready To Fly」同源；docs.px4.io pre_flight_checks）
_MAV_STATE = {0: "UNINIT", 1: "BOOT", 2: "CALIBRATING", 3: "STANDBY",
              4: "ACTIVE", 5: "CRITICAL", 6: "EMERGENCY", 7: "POWEROFF",
              8: "FLIGHT_TERMINATION"}
_FAILSAFE_STATES = ("CRITICAL", "EMERGENCY", "FLIGHT_TERMINATION")
_SENSOR_BITS = [
    ("陀螺儀", M.MAV_SYS_STATUS_SENSOR_3D_GYRO),
    ("加速度計", M.MAV_SYS_STATUS_SENSOR_3D_ACCEL),
    ("磁力計", M.MAV_SYS_STATUS_SENSOR_3D_MAG),
    ("氣壓計", M.MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE),
    ("GPS", M.MAV_SYS_STATUS_SENSOR_GPS),
    ("RC 接收器", M.MAV_SYS_STATUS_SENSOR_RC_RECEIVER),
    ("AHRS 姿態解算", M.MAV_SYS_STATUS_AHRS),
    ("電池", M.MAV_SYS_STATUS_SENSOR_BATTERY),
]
_LANDED = {1: "on_ground", 2: "in_air", 3: "takeoff", 4: "landing"}


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, rx: "MavlinkRx"):
        self.rx = rx

    def connection_made(self, transport):
        self.rx.transport = transport

    def datagram_received(self, data, addr):
        rx = self.rx
        if rx.rec:
            try:
                rx.rec.write(data)               # 原始層：先落盤，解析失敗也保留
            except Exception:
                log.exception("capture 寫檔失敗（資料路徑不受影響）")
        try:
            msgs = rx.parser.parse_buffer(data) or []
        except Exception:                        # 髒資料：解析器重建，繼續
            rx.parser = rx._new_parser()
            return
        for m in msgs:
            try:
                rx.queue.put_nowait((m, addr))
            except asyncio.QueueFull:
                pass                             # 消化不及時丟新不丟舊，原始層仍完整


class MavlinkRx:
    def __init__(self):
        self.transport = None
        self.rec = (Recorder(settings.capture_dir, settings.capture_keep_days)
                    if settings.capture_enabled else None)
        self.parser = self._new_parser()
        # 發送端（僅白名單查詢）：獨立 encoder 維護自己的 seq
        self.enc = M.MAVLink(None, srcSystem=GCS_SYSID,
                             srcComponent=M.MAV_COMP_ID_MISSIONPLANNER)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self.sysids: dict[int, dict] = {}        # sysid → {drone_id,state,addr,seen}
        self.by_drone: dict[str, int] = {}       # drone_id → sysid
        self._collector = None                   # 任務下載的訊息收集器
        self._dl_lock = asyncio.Lock()

    @staticmethod
    def _new_parser():
        p = M.MAVLink(None)
        p.robust_parsing = True
        return p

    async def start(self) -> asyncio.Task:
        u = settings.mavlink_url.replace("://", ":").split(":")
        host, port = u[-2], int(u[-1])
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: _Proto(self),
                                            local_addr=(host, port))
        log.info("MAVLink RX：udp %s:%d（pymavlink 解碼＋tlog 錄製%s；"
                 "發送白名單=%s）", host, port,
                 "開" if self.rec else "關", sorted(SEND_WHITELIST))
        return asyncio.create_task(self._worker(), name="mavlink-rx")

    def refresh_connected(self, stale_s: float = 5.0) -> None:
        """由外部週期迴圈呼叫：太久沒訊息的機標記失聯。"""
        now = time.monotonic()
        for ent in self.sysids.values():
            ent["state"].connected = (now - ent["seen"]) < stale_s

    # ── 訊息消化（單一 worker，順序保證＝架次賦值紀律的前提）────────
    async def _worker(self):
        while True:
            msg, addr = await self.queue.get()
            try:
                await self._handle(msg, addr)
            except Exception:
                log.exception("處理 %s 失敗", msg.get_type())

    async def _handle(self, msg, addr):
        sysid = msg.get_srcSystem()
        t = msg.get_type()
        if not sysid and t.startswith("UNKNOWN_"):
            # pymavlink 對未知 msgid 不填 header→get_srcSystem()=0，會被下方
            # 「if not sysid: return」丟掉（EVENT 410 全被吞的元凶）。從裸 frame
            # 補回真正 srcSystem（MAVLink2 在 byte 5），才找得到該機 ent。
            mb = msg.get_msgbuf()
            if mb is not None and len(mb) > 5 and mb[0] == 0xFD:
                sysid = bytes(mb)[5]
        if (self._collector and sysid == self._collector[0]
                and t in self._collector[1]):
            self._collector[2].put_nowait(msg)
        if not sysid or sysid == GCS_SYSID:
            return

        ent = self.sysids.get(sysid)
        if ent is None:
            # 只有「自駕儀的心跳」能建檔——PX4 會轉發其他 GCS 的訊息過來
            if (t != "HEARTBEAT" or msg.type == M.MAV_TYPE_GCS
                    or msg.autopilot == M.MAV_AUTOPILOT_INVALID):
                return
            drone_id, name = await db.drone_for_sysid(sysid)
            if drone_id == live.drone_id:
                st = live                        # 既有主機：沿用同一個 state 物件
                st.drone_name = name
            else:
                st = LiveState(drone_id=drone_id, drone_name=name)
            st.sysid = sysid                 # 前端「選中機統一」的事實源（issue 011）
            fleet[drone_id] = st
            ent = self.sysids[sysid] = {"drone_id": drone_id, "state": st,
                                        "addr": addr, "seen": time.monotonic()}
            self.by_drone[drone_id] = sysid
            log.info("sysid %d → %s（%s）", sysid, name,
                     "既有主機" if st is live else "自動註冊")
            # 021 Phase 2：連線即抓一次參數表（唯讀）。之後改參數時 PX4 會主動
            # 廣播 PARAM_VALUE，由下面的處理分支自動更新，不必重抓。
            try:
                self._send(sysid, self.enc.param_request_list_encode(sysid, 1))
            except Exception:
                log.exception("參數表請求送出失敗（不影響其他資料）")
        elif ent["addr"] != addr:
            # 撞號（兩台同 sysid）或換網路——必須看得見，混料比斷線嚴重。
            # 去抖（issue 016 RB5 sysid=1 bug 場景）：兩源撞號會每次心跳交替、
            # 每次都改 addr——不去抖會告警風暴淹掉事件流。同 sysid 每
            # ADDR_WARN_COOLDOWN 秒最多發一次。
            st = ent["state"]
            now = time.monotonic()
            if now - ent.get("addr_warn_t", 0) >= ADDR_WARN_COOLDOWN:
                ent["addr_warn_t"] = now
                ev = await db.insert_event(
                    st.drone_id, st.session_id, "warning", "sysid_addr_change",
                    {"sysid": sysid,
                     "note": "同 sysid 從多個來源收到——撞號或換網路，資料可能混料",
                     "from_addr": "%s:%d" % ent["addr"], "to_addr": "%s:%d" % addr})
                ev["drone"] = st.drone_name
                await manager.broadcast({"type": "event", "event": ev})
                log.warning("sysid %d 來源位址改變 %s → %s（撞號？換網路？）",
                            sysid, ent["addr"], addr)
            ent["addr"] = addr

        ent["seen"] = time.monotonic()
        st = ent["state"]
        st.connected = True
        msg_registry.record(st, msg)     # 014-B：每則訊息進該機登錄表（型別分派前）

        if t == "HEARTBEAT":
            # MAV_STATE：進入 failsafe 狀態（CRITICAL/EMERGENCY）要大聲
            state_name = _MAV_STATE.get(msg.system_status)
            if state_name in _FAILSAFE_STATES and st.mav_state != state_name:
                ev = await db.insert_event(st.drone_id, st.session_id, "critical",
                                           "failsafe", {"state": state_name})
                ev["drone"] = st.drone_name
                await manager.broadcast({"type": "event", "event": ev})
            st.mav_state = state_name
            st.autopilot_raw = msg.autopilot        # 方言分表解碼與 UI 徽章用
            st.vehicle_type_raw = msg.type
            mode = _mode_name(msg.custom_mode, msg.autopilot)
            # 防抖（同 cell_change 的 2-連續紀律）：撞號多來源會讓同 sysid 的模式
            # 每顆心跳在多值間翻打，噴 15/秒 mode_change 灌爆事件流（2026-08-11 事故）。
            # 新模式**連續 2 次**才提交＋發事件——翻打源每次都不同、永遠湊不齊 2 次→不噴；
            # 真的換模式是穩定的、下一顆心跳即確認（~0.5s 延遲，事件日誌可接受）。
            if mode == st.flight_mode:
                st.mode_pending = None
            elif mode == st.mode_pending:              # 第 2 次見到同一新模式 → 確認
                if st.flight_mode is not None:
                    ev = await db.insert_event(st.drone_id, st.session_id, "info",
                                               "mode_change",
                                               {"from": st.flight_mode, "to": mode})
                    ev["drone"] = st.drone_name
                    await manager.broadcast({"type": "event", "event": ev})
                st.flight_mode, st.mode_pending = mode, None
            else:                                      # 第 1 次見到新模式，設為候選待確認
                st.mode_pending = mode
                if st.flight_mode is None:             # 開機首次：直接定，不發事件
                    st.flight_mode = mode
            await self._armed_transition(
                st, bool(msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED))
        elif t == "PARAM_VALUE":
            # 兩個來源共用這條路徑：連線時我方請求的整批回應、以及**有人改參數時
            # PX4 主動廣播的單筆**。後者是快照保持忠實的關鍵（QGC 調完參數再飛）。
            st.params[msg.param_id] = msg.param_value
            st.param_total = msg.param_count
        elif t == "GLOBAL_POSITION_INT":
            st.lat = msg.lat / 1e7
            st.lon = msg.lon / 1e7
            st.alt_msl = msg.alt / 1000.0
            st.alt_rel = msg.relative_alt / 1000.0
            if msg.hdg != 65535:
                st.heading = msg.hdg / 100.0
        elif t == "VFR_HUD":
            st.ground_speed = msg.groundspeed
            st.vertical_speed = msg.climb
        elif t == "ATTITUDE":
            st.roll = math.degrees(msg.roll)
            st.pitch = math.degrees(msg.pitch)
            # IMU 卡：角速率（rad/s 原生，前端轉 °/s）
            st.imu.update(rollspeed=msg.rollspeed, pitchspeed=msg.pitchspeed,
                          yawspeed=msg.yawspeed)
        elif t == "HIGHRES_IMU":
            # IMU 卡：加速度/陀螺/磁力/溫度/氣壓。訊息結構固定、每筆都帶最新值
            # （fields_updated 只是「本筆哪些變了」的提示、非有效性遮罩，別拿來 null
            # 否則低頻的磁力/氣壓會被誤清）。feature-detect＝訊息層級（機上不發
            # HIGHRES_IMU→整組 None）＋「0＝不提供」慣例（溫度）。
            st.imu.update(
                xacc=msg.xacc, yacc=msg.yacc, zacc=msg.zacc,          # m/s²
                xgyro=msg.xgyro, ygyro=msg.ygyro, zgyro=msg.zgyro,    # rad/s
                xmag=msg.xmag * 100.0, ymag=msg.ymag * 100.0,         # gauss→µT
                zmag=msg.zmag * 100.0,
                # abs_pressure 正規化成 hPa：MAVLink 定 hPa，但 PX4 SITL 的 Gazebo 氣壓
                # sensor 送 Pa（實測 95605＝956 hPa@487m）。氣壓永遠 <2000 hPa，>2000 判定
                # 是 Pa÷100——SITL 與真機（送 hPa）都正規化到 hPa。
                abs_pressure=(msg.abs_pressure / 100.0 if msg.abs_pressure > 2000
                              else msg.abs_pressure),
                pressure_alt=msg.pressure_alt,                        # m
                temperature=(msg.temperature or None))               # 0＝不提供→None
        elif t == "VIBRATION":
            st.imu.update(
                vibration_x=msg.vibration_x, vibration_y=msg.vibration_y,
                vibration_z=msg.vibration_z, clipping_0=msg.clipping_0,
                clipping_1=msg.clipping_1, clipping_2=msg.clipping_2)
        elif t == "GPS_RAW_INT":
            st.gps_fix = msg.fix_type
            st.satellites = msg.satellites_visible
        elif t == "SYS_STATUS":
            if msg.battery_remaining >= 0:       # -1 = 未知
                st.battery_pct = float(msg.battery_remaining)
            if msg.voltage_battery != 65535:
                st.battery_voltage = msg.voltage_battery / 1000.0
            # PX4 預檢總結果：PREARM_CHECK 健康位（QGC「Ready To Fly」的核心）
            p_, e_, h_ = (msg.onboard_control_sensors_present,
                          msg.onboard_control_sensors_enabled,
                          msg.onboard_control_sensors_health)
            # 實測（SITL PX4 1.14）：PREARM 位元只出現在 health 遮罩，
            # enabled/present 都不設——見過一次就信 health 位元（黏性），
            # 從未見過＝韌體不支援，維持 None（就緒判定退回次級訊號）
            pre = M.MAV_SYS_STATUS_PREARM_CHECK
            if (p_ | e_ | h_) & pre:
                ent["prearm_seen"] = True
            st.prearm_ok = bool(h_ & pre) if ent.get("prearm_seen") else None
            st.sensors_unhealthy = [
                name for name, bit in _SENSOR_BITS
                if (p_ & bit) and (e_ & bit) and not (h_ & bit)]
        elif t == "ESTIMATOR_STATUS":
            need = (M.ESTIMATOR_ATTITUDE | M.ESTIMATOR_VELOCITY_HORIZ
                    | M.ESTIMATOR_POS_HORIZ_ABS)
            st.ekf_ok = (msg.flags & need) == need
        elif t == "EXTENDED_SYS_STATE":
            st.landed_state = _LANDED.get(msg.landed_state)
        elif t == "STATUSTEXT":
            await self._statustext(ent, st, msg)
        elif t == "UNKNOWN_410":         # MAVLink EVENT（PX4 vehicle 通知，Phase A.2）
            await self._vehicle_event(ent, st, msg)

    # ── STATUSTEXT → 事件流（issue 014 Phase A）───────────────────────
    # 自駕儀的 log。三件事：長訊息**分段重組**（MAVLink2 STATUSTEXT 切 50 字
    # 一段、同 id 遞增 chunk_seq）、**重複折疊**（PX4 會每秒噴同一句 prearm 失敗
    # ——折成一筆帶 count，不淹沒事件流）、標 source='vehicle'（QGC vehicle-
    # messages 面板同源，前端分「機上訊息」與「系統事件」）。
    def _stx_reassemble(self, ent: dict, msg) -> str | None:
        """回完整整句；分段未收完回 None。末段掉包的殘段逾時丟棄（不吐半句）。"""
        now = time.monotonic()
        buf = ent.get("stx_buf")
        if buf:
            for k in [k for k, v in buf.items() if now - v["t"] > STX_STALE_S]:
                del buf[k]
        cid = getattr(msg, "id", 0) or 0
        raw = msg.text or ""
        if cid == 0:                      # 未分段（≤50 字，或舊韌體不切段）
            return raw
        seq = getattr(msg, "chunk_seq", 0) or 0
        buf = ent.setdefault("stx_buf", {})
        slot = buf.setdefault(cid, {"parts": {}, "t": now})
        slot["parts"][seq] = raw
        slot["t"] = now
        if len(raw) < STX_CHUNK_LEN:      # 末段（pymavlink 去尾 NUL 後 <50）
            parts = buf.pop(cid)["parts"]
            return "".join(parts[k] for k in sorted(parts))
        return None

    async def _statustext(self, ent: dict, st: LiveState, msg) -> None:
        sev = _SEVERITY.get(msg.severity)
        if not sev:                       # 7=DEBUG 不入流
            return
        text = self._stx_reassemble(ent, msg)
        if text is None:                  # 還在收分段
            return
        text = text.strip()
        if not text:
            return
        now = time.monotonic()
        last = ent.get("stx_last")
        if (last and last["text"] == text and last["sev"] == sev
                and now - last["t"] < STX_FOLD_S):
            # 折疊：同句連續重複 → count++、就地更新既有那筆、原地重播
            last["count"] += 1
            last["t"] = now
            detail = {"text": text, "count": last["count"]}
            upd = await db.bump_event(last["id"], detail)
            if upd is not None:
                await manager.broadcast({"type": "event", "fold": True, "event": {
                    "id": last["id"], "time": upd["time"], "severity": sev,
                    "type": "statustext", "detail": detail, "source": "vehicle",
                    "drone": st.drone_name}})
                return
            # 那筆已被清理輪替掉 → 落到下面新插一筆
        ev = await db.insert_event(st.drone_id, st.session_id, sev, "statustext",
                                   {"text": text, "count": 1}, source="vehicle")
        ev["drone"] = st.drone_name
        ent["stx_last"] = {"id": ev["id"], "text": text, "sev": sev,
                           "count": 1, "t": now}
        await manager.broadcast({"type": "event", "event": ev})

    async def _vehicle_event(self, ent: dict, st: LiveState, msg) -> None:
        """MAVLink EVENT（410）→ 事件流（issue 014 Phase A.2）。折疊同 STATUSTEXT：
        同 event_id 連續重複折成一筆帶 count。type='vehicle_event'、source='vehicle'；
        detail 帶 event_id＋args（前端顯示「機上事件 #id（severity）」骨架，metadata
        文字落地時同列升級全文）。"""
        try:
            d = _decode_event(msg.get_msgbuf())
        except Exception:
            log.exception("EVENT 解碼失敗")
            return
        if not d:
            return
        sev = _SEVERITY.get(d["severity_ext"])   # 7=debug／8=protocol 等 → 不入流
        if not sev:
            return
        now = time.monotonic()
        eid = d["event_id"]
        last = ent.get("evt_last")
        if last and last["event_id"] == eid and now - last["t"] < STX_FOLD_S:
            last["count"] += 1
            last["t"] = now
            detail = {"event_id": eid, "args": d["args_hex"], "count": last["count"]}
            upd = await db.bump_event(last["id"], detail)
            if upd is not None:
                await manager.broadcast({"type": "event", "fold": True, "event": {
                    "id": last["id"], "time": upd["time"], "severity": sev,
                    "type": "vehicle_event", "detail": detail, "source": "vehicle",
                    "drone": st.drone_name}})
                return
        ev = await db.insert_event(st.drone_id, st.session_id, sev, "vehicle_event",
                                   {"event_id": eid, "args": d["args_hex"], "count": 1},
                                   source="vehicle")
        ev["drone"] = st.drone_name
        ent["evt_last"] = {"id": ev["id"], "event_id": eid, "count": 1, "t": now}
        await manager.broadcast({"type": "event", "event": ev})

    async def _armed_transition(self, st: LiveState, armed: bool):
        """架次邊界。賦值順序沿用原 ingest.py 的紀律（見該處歷史註解）：
        解鎖先建 session 再標 armed；上鎖先清旗標再結算。"""
        if armed and not st.armed:
            st.session_id = await db.create_session(st.drone_id)
            st.armed = True
            log.info("session started: %s（%s）", st.session_id, st.drone_name)
            # 影像（022）走背景：本 worker 是單執行緒，這裡 await 住（HTTP 逾時
            # 2s）會讓整條 MAVLink 處理停擺。影像是附加價值，不准拖累飛行資料。
            asyncio.create_task(video_rec.on_session_start(st.session_id, st.sysid, st))
            # 021 Phase 2：參數快照也走背景（851 筆的 JSONB 寫入不該卡住 rx worker）
            asyncio.create_task(
                db.snapshot_params_for_session(st.session_id, st))
        elif not armed and st.armed:
            sid, st.session_id, st.armed = st.session_id, None, False
            if sid:
                await db.end_session(sid)
                log.info("session ended: %s", sid)
            asyncio.create_task(video_rec.on_session_end(st.sysid, st))

    # ── 任務讀回（白名單內的查詢對話）───────────────────────────
    def _send(self, sysid: int, msg_obj) -> None:
        name = msg_obj.get_type()
        if name not in SEND_WHITELIST:
            # **明確 raise 而非 assert**：assert 在 `python -O` 下會被整段移除，
            # 而這裡是本服務 read-only 邊界的唯一守門員（模擬環境的拓撲限制已
            # 拿掉，見 sim-fleet/mav_fanout.py），不能是一個可被最佳化掉的檢查。
            raise PermissionError(
                f"{name} 不在發送白名單——read-only 邊界"
                "（改變機上狀態的指令走 command 服務）")
        ent = self.sysids.get(sysid)
        if not ent:
            raise RuntimeError(f"sysid {sysid} 未連線")
        self.transport.sendto(msg_obj.pack(self.enc), ent["addr"])
        self.enc.seq = (self.enc.seq + 1) % 256

    async def _expect(self, q: asyncio.Queue, type_: str, seq: int | None = None,
                      timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise asyncio.TimeoutError(f"等 {type_} 逾時")
            msg = await asyncio.wait_for(q.get(), remain)
            if msg.get_type() == type_ and (seq is None or msg.seq == seq):
                return msg

    async def download_mission(self, drone_id: str | None = None) -> list:
        """從機上下載任務（MISSION_REQUEST_LIST 握手）。唯讀查詢。"""
        drone_id = drone_id or live.drone_id
        sysid = self.by_drone.get(drone_id)
        if sysid is None:
            raise RuntimeError("MAVLink 未連線（該機未見心跳）")
        mt = M.MAV_MISSION_TYPE_MISSION
        async with self._dl_lock:
            q: asyncio.Queue = asyncio.Queue()
            self._collector = (sysid, {"MISSION_COUNT", "MISSION_ITEM_INT"}, q)
            try:
                self._send(sysid, self.enc.mission_request_list_encode(sysid, 1, mt))
                cnt = await self._expect(q, "MISSION_COUNT")
                items = []
                for seq in range(cnt.count):
                    for attempt in (1, 2):       # 每項一次重試
                        self._send(sysid,
                                   self.enc.mission_request_int_encode(sysid, 1, seq, mt))
                        try:
                            items.append(await self._expect(q, "MISSION_ITEM_INT", seq))
                            break
                        except asyncio.TimeoutError:
                            if attempt == 2:
                                raise
                self._send(sysid,
                           self.enc.mission_ack_encode(sysid, 1,
                                                       M.MAV_MISSION_ACCEPTED, mt))
                return items
            finally:
                self._collector = None


rx: MavlinkRx | None = None


async def start() -> asyncio.Task:
    global rx
    rx = MavlinkRx()
    return await rx.start()
