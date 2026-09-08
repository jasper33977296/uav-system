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

from . import db, dialect, msg_registry, px4_events, video_rec
from .capture import Recorder
from .config import settings
from .state import MISSION_STATE, LiveState, fleet, live
from .ws import manager

log = logging.getLogger(__name__)
M = mavutil.mavlink
#: 與 command 服務同一個值（見 apps/command/app/mav.py 的說明）。
#: 這裡只用來**排除自己送出去的封包**，不參與指令
GCS_SYSID = 255
STREAM_REQ_S = 30.0          # ArduPilot 串流請求補送間隔（見 _maintain_streams）
STREAM_HZ = 4                # 請求的串流率（夠前端 5Hz 顯示，不灌爆 5G）
ADDR_WARN_COOLDOWN = 30.0    # 同 sysid 撞號告警去抖（秒）：避免告警風暴
STX_CHUNK_LEN = 50           # STATUSTEXT text 欄位長度：滿 50＝還有下一段
STX_STALE_S = 3.0            # 分段末段掉包，殘段逾時丟棄（秒）
STX_FOLD_S = 15.0            # 同句連續重複在此窗內折疊計數（秒）

# read-only 邊界的實體：能離開這個 socket 的訊息類型只有這三種（任務下載
# 的查詢對話）。要發任何別的，這行 assert 就是攔你的人。
# 021 Phase 2 新增 PARAM_REQUEST_LIST／PARAM_REQUEST_READ：**唯讀查詢**，符合本
# 模組的邊界定義（「不含改變機上狀態的能力」）。
#
# **PARAM_SET 永遠不得加入這一條白名單**——這句話仍然成立，但 2026-09-07
# 之後它的理由只剩一個，要說清楚免得被誤讀：
#
#   * 舊的理由是「參數編輯是 QGC 的職權，本系統不去改」。**那一層已經由
#     使用者裁定改掉了**：指令服務現在寫得了參數（白名單、只在未解鎖時、
#     逐個讀回比對，見 apps/command/app/params.py）。
#   * 留下來的理由與參數無關，與**這條 socket 是什麼**有關：它是遙測與錄製
#     的路。錄製的路一旦能改變機上狀態，「我方記錄到的」與「我方造成的」
#     就再也分不開了。所以要寫東西到飛機，走指令服務那條路，不走這裡。
SEND_WHITELIST = {"MISSION_REQUEST_LIST", "MISSION_REQUEST_INT", "MISSION_ACK",
                  "PARAM_REQUEST_LIST", "PARAM_REQUEST_READ",
                  # 015 實測：**ArduPilot 預設幾乎不送遙測**——我方只收得到
                  # HEARTBEAT/PARAM_VALUE/STATUSTEXT/TIMESYNC 四種，沒有位置、
                  # GPS、電量、SYS_STATUS。送一次 REQUEST_DATA_STREAM 後變 32 種。
                  # 沒有它，接 ArduPilot 機＝「連得上但等於瞎的」。PX4 預設就串流，
                  # 所以這件事在只測 PX4 的時候永遠不會暴露。
                  # 同樣是**唯讀請求**（要求對方送資料，不改變機上狀態）。
                  "REQUEST_DATA_STREAM"}

# 方言知識全部集中在 `dialect.py`（issue 026 B0）。本檔不再持有任何廠牌表，
# 也**不轉出** dialect 的名字——留轉出等於留下第二個看起來權威的位置。


# MAV_SEVERITY(0-7) → 事件層級；7=DEBUG 不入流。EVENT 的外層 log level 也用
# 同一枚舉，另有 8=Protocol（框架內部、非給人看）落在表外 → 自然丟棄。
_SEVERITY = {0: "critical", 1: "critical", 2: "critical", 3: "critical",
             4: "warning", 5: "info", 6: "info"}


def forget(drone_id: str) -> int:
    """記錄被刪除時，把這台機從**執行期**狀態裡也拿掉。回傳清掉幾個 sysid。

    **刪掉資料庫那一列不會讓它從畫面上消失。** 執行期的 `fleet` 與這裡的
    sysid 對照表各自握著一份，廣播迴圈照樣每 0.2 秒送一次它的最後已知位置
    ——而那台機**已經不存在了**，畫面上卻與一台「只是斷線」的真機完全同形。

    注意：**這不保證它不會回來。** 那個 sysid 若還在發心跳，下一則就會重新
    自動註冊（`drone_for_sysid`）——那是對的，機還在天上就該看得到它。
    要它真的消失，得先讓它停止發送。
    """
    if rx is None:
        return 0
    n = 0
    for sysid, ent in list(rx.sysids.items()):
        if ent.get("drone_id") == drone_id:
            del rx.sysids[sysid]
            n += 1
    rx.by_drone.pop(drone_id, None)
    return n


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

def _decode_event_seq(msgbuf) -> dict | None:
    """手工解 `CURRENT_EVENT_SEQUENCE`（msg 411），理由同 410：
    當前 pymavlink 方言未定義它，顯示成 `UNKNOWN_411`、payload 解不出。

    線序（MAVLink2 依型別大小排序）：`sequence(u16) flags(u8)` ＝ 3 bytes。
    `flags` 的 bit0＝`RESET`：機端把序號歸零了（重開機／重連），
    **此時序號倒退不是掉包**。
    """
    mb = bytes(msgbuf)
    if len(mb) < 13 or mb[0] != 0xFD:
        return None
    payload = bytes(mb[10:10 + mb[1]]) + b"\x00" * 3
    return {"seq": int.from_bytes(payload[0:2], "little"), "flags": payload[2]}


#: 411 flags 的 bit0：機端序號歸零
_EVT_SEQ_RESET = 0x01


def _enum_name(enum: str, value) -> str:
    """MAVLink 枚舉值 → 名字。**認不得就回原值的字串**，不猜、不留空——
    「MAV_CMD_400」比空白有用：它至少查得到，而空白只是消失。"""
    if value is None:
        return "（未提供）"
    e = M.enums.get(enum, {}).get(value)
    return e.name if e is not None else f"{enum}_{value}"

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

#: `RC_CHANNELS` 多久沒來就不再拿它下判斷。**「我們不再聽到」不等於「RC 在」**
#: ——留著一個過期的計數，等於用一個舊事實回答一個關於現在的問題。
#: 實測真機是 4 Hz，5 秒＝漏 20 則才會轉成「不知道」
RC_STALE_S = 5.0


def _derive_rc(ent: dict) -> bool | None:
    """RC 接收機在不在。**三態**：True／False／None＝不知道。

    兩個來源分層，**不是二選一**：

    1. **`SYS_STATUS` 的 `RC_RECEIVER` 位元**（`present` 決定知不知道、
       `health` 決定真假）——PX4 有設，權威。
    2. **`RC_CHANNELS.chancount`**——ArduPilot 4.7 **不設**上面那個位元，
       但它有送這則（實測 4 Hz）。規格：「正在接收的 RC 通道總數；
       **沒有可用的 RC 通道時應為 0**」。**那是計數不是哨兵**，所以 0 是真值。

    **同一則訊息裡的 `rssi` 不能用**：實測真機回 **255**，而 255 在 MAVLink 裡
    是「無效」不是「滿格」。一則訊息裡一個欄位可用、一個不可用——
    這就是規格要逐欄讀、不能整包信的原因。

    兩者都沒有 → `None`。**`None` 不擋**（039 複裁 A）：把「不知道」當成
    「沒有 RC」會讓所有還沒回報的機都起飛不了。
    """
    v = ent.get("rc_sys_status")
    if v is not None:
        ent["rc_source"] = "sys_status"
        return v
    n, t0 = ent.get("rc_chancount"), ent.get("rc_chan_t")
    if n is not None and t0 is not None and time.monotonic() - t0 <= RC_STALE_S:
        ent["rc_source"] = "rc_channels"
        return n > 0
    ent["rc_source"] = None
    return None


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
        """由外部週期迴圈呼叫：太久沒訊息的機標記失聯，順便維持 ArduPilot 串流。"""
        now = time.monotonic()
        for ent in self.sysids.values():
            ent["state"].connected = (now - ent["seen"]) < stale_s
        self._maintain_streams()

    def _maintain_streams(self) -> None:
        """ArduPilot 要主動要求才會送遙測（015 實測，見 SEND_WHITELIST 註解）。

        **定期補送而不是只在註冊時送一次**：串流率是設在自駕儀端的，機端重開機、
        換連線通道、或我方重連之後就沒了——只送一次的話，那些情況下會靜默失去
        全部遙測（只剩心跳，看起來還「連著」）。每 STREAM_REQ_S 補一次，成本是
        一則小訊息。PX4 預設就串流，不需要也不送。
        """
        now = time.monotonic()
        for sysid, ent in list(self.sysids.items()):
            if not dialect.needs_stream_request(ent["state"].autopilot_raw):
                continue
            if now - ent.get("stream_req_t", 0.0) < STREAM_REQ_S:
                continue
            ent["stream_req_t"] = now
            try:
                self._send(sysid, self.enc.request_data_stream_encode(
                    sysid, 1, M.MAV_DATA_STREAM_ALL, STREAM_HZ, 1))
            except Exception:
                log.exception("ArduPilot 串流請求送出失敗（sysid %s）", sysid)

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
            # 回填上次記錄的板子身分：**它是板子的穩定屬性，不該因為 backend
            # 重啟就從畫面上消失**（請求 AUTOPILOT_VERSION 的是 command 服務，
            # 它不會因為我們重啟而重問）。機端之後回報新值時會覆蓋。
            st.board_uid, st.flight_sw_version, st.expect_autopilot = \
                await db.load_board_identity(drone_id)
            # **回填的 uid 同時是期望值。** 2026-09-01 的教訓：這一步把一台真機
            # 的 board_uid 填到了 PX4 SITL 的狀態上（兩者都用 sysid 1），
            # 於是身分鏈被我們自己接反了——機端還沒開口，記錄就先替它答了
            st.expect_board_uid = st.board_uid
            claimed = st is live
            log.info("sysid %d → %s（%s）", sysid, name,
                     "既有主機" if claimed else "自動註冊")
            # **認領要看得見。** 2026-08-24 實際發生：一筆早已停用的舊機記錄
            # 仍是主機且 mav_sysid 空著，新接上的機一開機就被認領進那筆記錄，
            # /api/live 顯示的是別台機的名字——而整個過程只有一行 log.info。
            # 記成事件，讓它出現在事件流與畫面上（issues/038）。
            try:
                ev = await db.insert_event(
                    drone_id, None, "info", "sysid_claimed",
                    {"sysid": sysid, "drone": name,
                     "how": "既有主機認領" if claimed else "自動建檔",
                     "note": "這台機的遙測從此記在這筆記錄名下——"
                             "名字不對就是認領到錯的記錄了"})
                ev["drone"] = name
                await manager.broadcast({"type": "event", "event": ev})
            except Exception:
                log.exception("sysid 認領事件寫入失敗（不影響資料路徑）")
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
            # **同 IP 換 port ≠ 撞號。** 機上代理每次 5G 斷線重連都會拿到新的
            # 來源埠——那是正常重連，不是「兩台機用同一個 sysid」。原本兩者
            # 都發 warning，於是這條警告在本場域一天響好幾次（2026-08-26 實測
            # 一小時內三次），而**一天到晚響的警告等於沒有警告**：真的撞號時
            # 沒有人會多看一眼。IP 不同才是需要人介入的那一種。
            same_host = ent["addr"][0] == addr[0]
            if same_host:
                log.info("sysid %d 來源埠改變 %s → %s（同一台主機，重連）",
                         sysid, ent["addr"], addr)
            elif now - ent.get("addr_warn_t", 0) >= ADDR_WARN_COOLDOWN:
                ent["addr_warn_t"] = now
                ev = await db.insert_event(
                    st.drone_id, st.session_id, "warning", "sysid_addr_change",
                    {"sysid": sysid,
                     "note": "同一個 sysid 從**不同主機**收到——兩台機用了同一個"
                             "sysid，遙測會互相覆蓋。先確認機上的 SYSID 參數",
                     "from_addr": "%s:%d" % ent["addr"], "to_addr": "%s:%d" % addr})
                ev["drone"] = st.drone_name
                await manager.broadcast({"type": "event", "event": ev})
                log.warning("sysid %d 來源**主機**改變 %s → %s（撞號）",
                            sysid, ent["addr"], addr)
            ent["addr"] = addr

        ent["seen"] = time.monotonic()
        st = ent["state"]
        st.telem_seen_mono = ent["seen"]      # A 層：畫面上那些數字的年齡
        st.connected = True
        st.ever_connected = True
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
            await self._identity_guard(st, autopilot=msg.autopilot)
            st.vehicle_type_raw = msg.type
            mode = dialect.mode_name(msg.custom_mode, msg.autopilot)
            verb = dialect.mode_verb(msg.custom_mode, msg.autopilot)
            # **mode_verb 必須跟著 flight_mode 一起提交**，不能在這裡就寫進 st：
            # 那會繞過下面的 2-連續防抖，讓「已確認的模式名」與「語意」在翻打期間
            # 各說各話（顯示 HOLD 但 verb 已經跳成別的）。兩者是同一個事實的兩種
            # 表達，任何時刻都必須一致。
            # 防抖（同 cell_change 的 2-連續紀律）：撞號多來源會讓同 sysid 的模式
            # 每顆心跳在多值間翻打，噴 15/秒 mode_change 灌爆事件流（2026-08-11 事故）。
            # 新模式**連續 2 次**才提交＋發事件——翻打源每次都不同、永遠湊不齊 2 次→不噴；
            # 真的換模式是穩定的、下一顆心跳即確認（~0.5s 延遲，事件日誌可接受）。
            if mode == st.flight_mode:
                st.mode_pending = None
            elif mode == st.mode_pending:              # 第 2 次見到同一新模式 → 確認
                if st.flight_mode is not None:
                    # 帶上廠牌無關的語意：事件流要在混機下說「LOITER 等於 HOLD」，
                    # 就必須有 verb。**不能讓前端自己從模式名反查**——那會複製一份
                    # 驅動層的表在前端，然後兩份各自漂移（030 就是兩份表的下場）。
                    ev = await db.insert_event(st.drone_id, st.session_id, "info",
                                               "mode_change",
                                               {"from": st.flight_mode, "to": mode,
                                                "from_verb": st.mode_verb,
                                                "to_verb": verb})
                    ev["drone"] = st.drone_name
                    await manager.broadcast({"type": "event", "event": ev})
                st.flight_mode, st.mode_verb, st.mode_pending = mode, verb, None
            else:                                      # 第 1 次見到新模式，設為候選待確認
                st.mode_pending = mode
                if st.flight_mode is None:             # 開機首次：直接定，不發事件
                    st.flight_mode, st.mode_verb = mode, verb
            await self._armed_transition(
                st, bool(msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED))
        elif t == "PARAM_VALUE":
            # 兩個來源共用這條路徑：連線時我方請求的整批回應、以及**有人改參數時
            # PX4 主動廣播的單筆**。後者是快照保持忠實的關鍵（QGC 調完參數再飛）。
            # **整數參數要按 param_type 解碼**：MAVLink 把所有值塞進一個 float32
            # 欄位，而 PX4 放的是整數的**位元組**不是數值。不解碼的話快照裡 13%
            # 的值是非正規化浮點數垃圾（實測 851 個參數中的 112 個），而且長得
            # 像合理數字、不會有任何錯誤——參數快照的存在理由正是「這一趟到底
            # 是用什麼設定飛的」，存錯就是這個功能失效（issue 021 Phase 2）。
            st.params[msg.param_id] = dialect.decode_param(
                msg.param_value, getattr(msg, "param_type", None), msg.autopilot
                if hasattr(msg, "autopilot") else st.autopilot_raw)
            st.param_total = msg.param_count
        elif t == "MISSION_CURRENT":
            # 機端正在飛第幾項。**系統原本完全沒有解這則訊息**——收得到但沒人看，
            # 於是「飛機正在飛第幾個航點」這件事在系統裡不存在（issues/039 需要它）。
            # 忠實記錄機端的 seq，**不在這裡換算成我方索引**：ArduPilot 的 home
            # 佔 seq 0，換算是驅動層的職責，在 ingest 就換會讓原始事實消失。
            await self._mission_progress(st, msg)
            st.mission_seq = msg.seq
            st.mission_total = self._said(getattr(msg, "total", None))
            st.mission_state = self._said(getattr(msg, "mission_state", None))
        elif t == "MISSION_ITEM_REACHED":
            # 「我到第 N 點了」。**與 MISSION_CURRENT 是兩件事**：後者說的是
            # 「正在飛向第幾項」，這則說的是「已經到了第幾項」。任務事後要
            # 回答「每個航點幾點到的」，只有這則答得出來。
            ev = await db.insert_event(
                st.drone_id, st.session_id, "info", "waypoint_reached",
                {"seq": msg.seq, "total": st.mission_total}, source="vehicle")
            ev["drone"] = st.drone_name
            await manager.broadcast({"type": "event", "event": ev})
        elif t == "AUTOPILOT_VERSION":
            # 038：板子身分。**只收不請求**——請求要送 COMMAND_LONG，那是個
            # 通用信封（同一型別可以裝 arm），把它加進 SEND_WHITELIST 等於在
            # 本模組的 read-only 邊界上開洞。請求由機上代理發出（026 定案：
            # 代理才是下達者），回應廣播回來，這裡照收即可。
            uid2 = bytes(getattr(msg, "uid2", b"") or b"")
            # 尾端補位的 0 去掉再存：長度隨板子而異，留著會讓同一塊板子在
            # 不同韌體上算出不同字串
            uid2 = uid2.rstrip(b"\x00")
            # **`uid2` 全 0 就是沒有板號，不退回舊的 `uid` 欄位**
            # （2026-09-02 使用者裁定，issues/040）。原本會退回去，而實測 PX4
            # SITL 的 `uid2` 全是 0、`uid` 是 `0x4954414c44494e4f`＝ASCII
            # **"ITALDINO"**——**一個常數**。把常數當成板號等於**發明一個身分**，
            # 而在「板號是唯一鍵值」的制度下，那會讓每一台 SITL 都解析成同一筆
            # 記錄、**安靜地合併成一台飛機**。那比 sysid 撞號更嚴重：撞號至少
            # 會被 `_identity_guard` 抓到（廠牌不同），合併連矛盾都不會產生。
            #
            # 代價講明白：**沒有 uid2 的機從此沒有身分，也就不可被指揮**。
            # 那正是「代理強制」裁定下應有的結果——要被指揮就得拿得出身分，
            # 而不是讓系統從一個常數裡誤讀出一個。
            st.board_uid = uid2.hex() if uid2 else None
            st.board_version = getattr(msg, "board_version", None)
            st.board_vendor_id = getattr(msg, "vendor_id", None)
            st.board_product_id = getattr(msg, "product_id", None)
            v = getattr(msg, "flight_sw_version", 0) or 0
            if v:
                # MAVLink 編碼：major<<24 | minor<<16 | patch<<8 | type
                kind = {255: "official", 128: "rc", 64: "beta",
                        192: "dev"}.get(v & 0xFF)
                st.flight_sw_version = (
                    f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"
                    + (f" ({kind})" if kind else f" (type {v & 0xFF})"))
            if st.board_uid:
                log.info("sysid %d 板子身分：uid=%s 韌體=%s",
                         sysid, st.board_uid, st.flight_sw_version)
                if await self._identity_guard(st, board_uid=st.board_uid):
                    await db.set_board_uid(st.drone_id, st.board_uid,
                                           st.flight_sw_version)
        elif t == "BATTERY_STATUS":
            # **電流積分的兩個數字**（2026-09-07）：飛控的 `battery_remaining`
            # 就是拿 `(BATT_CAPACITY − current_consumed) / BATT_CAPACITY` 算的，
            # 所以少了 `current_consumed`，畫面上那個百分比就只剩結論、
            # 沒有推導過程——而那個推導的刻度（`BATT_AMP_PERVLT`）本專案
            # 還沒有人驗過。要驗它就得先把這兩個數字留下來。
            #
            # **只收第一顆電池**（instance 0）：多電池機還沒有，等有了再說；
            # 現在無條件覆蓋的話，第二顆的讀數會蓋掉主電池的。
            if getattr(msg, "id", 0) == 0:
                if getattr(msg, "current_consumed", -1) >= 0:
                    st.battery_consumed_mah = float(msg.current_consumed)
                if getattr(msg, "current_battery", -1) >= 0:
                    st.battery_current = msg.current_battery / 100.0
        elif t == "GLOBAL_POSITION_INT":
            # **0,0 是自駕儀的「不知道」哨兵，不是幾內亞灣外海。**
            # GLOBAL_POSITION_INT 在沒有位置估計時送 lat=lon=0；照寫會把
            # `lat: float | None` 這個誠實的型別（None＝不知道）覆蓋成一個
            # 看起來有效的假座標，地圖的 `!= null` 過濾器擋不住 0。
            # 判準用哨兵值而不是 gps_fix：位置來源不一定是 GPS（室內光流、
            # 動捕都可能），拿「GPS 沒定位」去否定位置會誤殺那些來源。
            # **只擋經緯度**：氣壓高度與羅盤航向不需要位置估計就成立，
            # 一起跳過會讓沒有 GPS 的機連高度都讀不到（室內測試就是這種狀態）。
            if not (msg.lat == 0 and msg.lon == 0):
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
            # **電流是次要來源**：`SYS_STATUS` 只有電流沒有累積消耗，
            # 而兩者要同源才對得起來——所以下面的 BATTERY_STATUS 會覆蓋它。
            # 這裡收著是為了「舊韌體只送 SYS_STATUS」的情況。
            # 單位是 cA（10 mA），-1＝不知道
            if getattr(msg, "current_battery", -1) >= 0:
                st.battery_current = msg.current_battery / 100.0
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
            # ── RC 接收機（issues/014 結構層／039 複裁 A）─────────────
            # **判準與機上代理逐字相同**：`present` 決定「知不知道」、
            # `health` 決定真假，**不看 `enabled`**（上面 `sensors_unhealthy`
            # 那條有看，兩者是不同的問題：那條問「壞了沒」，這條問「在不在」）。
            #
            # **刻意不用 `RC_CHANNELS.rssi`**：rssi 沒有「不知道」這一態——
            # 255 在 MAVLink 裡是無效值不是滿格，讀錯方向會讓守門在 RC 掉線時
            # 照樣放行。三態的分別是這件事的全部價值。
            rc_bit = M.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
            ent["rc_sys_status"] = (
                bool(h_ & rc_bit) if (p_ & rc_bit) else None)
            prev_rc = st.rc_link
            st.rc_link = _derive_rc(ent)
            await self._rc_event(st, ent, prev_rc)
        elif t in dialect.EKF_MSG_TYPES:
            # 訊息層方言（差異 8→12）：PX4 發 ESTIMATOR_STATUS、ArduPilot 發
            # EKF_STATUS_REPORT，**同一件事兩個訊息名**，所需位元同義。等價的
            # 邊界（哪些位元可以互換、哪些不行）寫在 dialect.py §1。
            st.ekf_ok = dialect.ekf_ready(msg.flags)
        elif t == "RC_CHANNELS":
            # **第二個訊號來源**（2026-09-02 實測後新增）：ArduPilot 4.7 不設
            # `SYS_STATUS` 的 RC_RECEIVER present 位元，但**它有在送
            # RC_CHANNELS**（實測 4 Hz）。`chancount` 在 MAVLink 規格裡的定義
            # 是「正在接收的 RC 通道總數；**沒有可用的 RC 通道時應為 0**」
            # ——那是一個**計數**，0 是真值不是哨兵，所以它給得出三態。
            #
            # 對照組：同一則訊息的 `rssi` 實測是 **255**，而 255 在 MAVLink 裡
            # 是「無效」不是「滿格」。**同一則訊息裡一個欄位可用、一個不可用**
            # ——這就是當初否決 rssi 的判斷為什麼是對的。
            ent["rc_chancount"] = getattr(msg, "chancount", None)
            ent["rc_chan_t"] = time.monotonic()
            prev_rc = st.rc_link
            st.rc_link = _derive_rc(ent)
            await self._rc_event(st, ent, prev_rc)
        elif t == "EXTENDED_SYS_STATE":
            st.landed_state = _LANDED.get(msg.landed_state)
            await self._landed_transition(st)
        elif t == "STATUSTEXT":
            await self._statustext(ent, st, msg)
        elif t in ("COMMAND_ACK", "MISSION_ACK"):
            await self._ack_event(ent, st, msg, t)
        elif t == "UNKNOWN_410":         # MAVLink EVENT（PX4 vehicle 通知，Phase A.2）
            await self._vehicle_event(ent, st, msg)
        elif t == "UNKNOWN_411":         # CURRENT_EVENT_SEQUENCE（掉包偵測）
            d = _decode_event_seq(msg.get_msgbuf())
            if d:
                await self._event_gap(st, ent, d["seq"],
                                      bool(d["flags"] & _EVT_SEQ_RESET), "411")

    # ── 任務進度 → 事件流（2026-09-06）────────────────────────────────
    # `MISSION_CURRENT` 每秒都來，**只有變化才落盤**：不然一趟飛行會多出幾千
    # 筆一模一樣的列，把事件流淹掉——那等於沒記。
    #
    # 為什麼一定要落盤：原本 seq 只更新 live state，而 live state 是**現在**，
    # 不是**歷史**。任務飛完之後回頭看，「第幾秒到第幾點」在系統裡不存在，
    # 只能拿軌跡點去跟航點座標算距離用猜的——那是推論不是紀錄。
    #: `MISSION_CURRENT` 的 `total`／`mission_state` 是 MAVLink 擴充欄位
    #: （ArduPilot 4.5+ 才送）。**pymavlink 對缺席的擴充欄位填 0，不是 None**
    #: ——照收就會把「韌體沒說」記成「總共 0 項」，畫面上寫出「共 0 項」，
    #: 而那趟任務明明有 5 項（2026-09-07 用 ArduPilot 4.0.3 的 SITL 抓到）。
    @staticmethod
    def _said(v):
        """擴充欄位：0＝沒說（None），不是 0 這個數值。"""
        return v if v else None

    async def _mission_progress(self, st: LiveState, msg) -> None:
        seq = msg.seq
        state = self._said(getattr(msg, "mission_state", None))
        total = self._said(getattr(msg, "total", None))
        first = st.mission_seq is None            # 這條連線第一次看到
        # **第一次看到也可能是有意義的**：失聯回來時「它已經飛到第 5 點」是
        # 新資訊。但沒有任務時每次連線都報一次就是噪音，所以要求機端說得出
        # 「有任務」（總項數 > 0，或狀態不是無任務／不知道）才記。
        loaded = bool(total) or state not in (None, 0, 1)
        if seq != st.mission_seq and (not first or loaded):
            ev = await db.insert_event(
                st.drone_id, st.session_id, "info", "mission_progress",
                {"from": st.mission_seq, "to": seq, "total": total,
                 "state": MISSION_STATE.get(state),
                 **({"first_sight": True} if first else {})}, source="vehicle")
            ev["drone"] = st.drone_name
            await manager.broadcast({"type": "event", "event": ev})
        # 任務狀態另外記一筆。**光看 seq 分不出「飛完了」與「被切走」**：
        # 兩者都是 seq 停在某一項不動。實測本機飛完的樣子是
        # `active → not_started`（不是 5=complete，那個值本機從來不送），
        # 中途被切走則是 active 之後沒有最後一項的 MISSION_ITEM_REACHED。
        if state is not None and state != st.mission_state and not (
                first and state in (0, 1)):
            ev = await db.insert_event(
                st.drone_id, st.session_id,
                "info" if state != 5 else "notice", "mission_state",
                {"from": MISSION_STATE.get(st.mission_state),
                 "to": MISSION_STATE.get(state), "seq": seq, "total": total},
                source="vehicle")
            ev["drone"] = st.drone_name
            await manager.broadcast({"type": "event", "event": ev})

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
        # **預檢失敗的原因，飛控本來就在講**——原本只進事件流，於是畫面上
        # 只寫得出「預檢未過」，操作員得自己去事件流裡翻。記到 state 上，
        # 就緒判定就說得出是哪一項（見 state.readiness）。
        # `PreArm:` 是被擋在解鎖之前，`Arm:` 是解鎖當下被拒——兩者都是同一個
        # 問題的答案：「為什麼現在不能飛」
        for pfx in ("PreArm:", "Arm:"):
            if text.startswith(pfx):
                st.prearm_msgs[text[len(pfx):].strip()] = now
                break
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

    async def _ack_event(self, ent: dict, st: LiveState, msg, kind: str) -> None:
        """飛控對指令的回應 → 事件流（issues/014 結構層 #2）。

        **為什麼 command 服務已經留痕了還要收這個**：`command_log` 記的是
        **我們送出去的**指令與它拿到的回應。而飛控會回應**任何人**送的指令
        ——QGC、機上代理（失聯處置的 RTL）、驗收 rig、直接打端點的腳本。
        那些回應現在完全看不到，於是事後查「這台機為什麼突然回家」時，
        證據鏈斷在「誰下的令」這一格。

        **`IN_PROGRESS` 不入流**：長時間動作（校正、任務上傳）會每秒重複回報，
        而它不帶新資訊——事件流被它淹掉的話，真正的失敗就沒有人看得見。

        > 老規矩：**ACK 是「我收到了」，不是「我做到了」。** 所以這裡記的是
        > 「飛控說它收到並接受了」，不是「那件事發生了」——文案照這個寫。
        """
        if kind == "COMMAND_ACK":
            res = getattr(msg, "result", None)
            if res == M.MAV_RESULT_IN_PROGRESS:
                return
            cmd = getattr(msg, "command", None)
            cmd_name = _enum_name("MAV_CMD", cmd)
            res_name = _enum_name("MAV_RESULT", res)
            okay = res == M.MAV_RESULT_ACCEPTED
            detail = {"kind": "command", "command": cmd, "command_name": cmd_name,
                      "result": res, "result_name": res_name,
                      "text": (f"飛控**收下**了 {cmd_name}" if okay
                               else f"飛控拒絕 {cmd_name}：{res_name}"),
                      "note": "ACK 是「我收到了」，不是「我做到了」"}
            key = ("command", cmd, res)
        else:
            typ = getattr(msg, "type", None)
            okay = typ == M.MAV_MISSION_ACCEPTED
            name = _enum_name("MAV_MISSION_RESULT", typ)
            detail = {"kind": "mission", "result": typ, "result_name": name,
                      "mission_type": getattr(msg, "mission_type", None),
                      "text": ("飛控**收下**了任務傳輸" if okay
                               else f"任務傳輸被拒：{name}")}
            key = ("mission", typ, None)
        sev = "info" if okay else "warning"
        now = time.monotonic()
        last = ent.get("ack_last")
        if last and last["key"] == key and now - last["t"] < STX_FOLD_S:
            last["count"] += 1
            last["t"] = now
            upd = await db.bump_event(last["id"], {**detail, "count": last["count"]})
            if upd is not None:
                await manager.broadcast({"type": "event", "fold": True, "event": {
                    "id": last["id"], "time": upd["time"], "severity": sev,
                    "type": "vehicle_ack", "detail": {**detail, "count": last["count"]},
                    "source": "vehicle", "drone": st.drone_name}})
                return
        ev = await db.insert_event(st.drone_id, st.session_id, sev, "vehicle_ack",
                                   {**detail, "count": 1}, source="vehicle")
        ev["drone"] = st.drone_name
        ent["ack_last"] = {"key": key, "id": ev["id"], "count": 1, "t": now}
        await manager.broadcast({"type": "event", "event": ev})

    async def _event_gap(self, st: LiveState, ent: dict, seq: int,
                         reset: bool, src: str) -> None:
        """機上事件的序號缺口（issues/014）。

        **為什麼要做**：EVENT 走 UDP，掉了就是掉了。而**「沒有事件」與
        「事件掉了」在畫面上完全同形**——一段安靜的事件流可能代表飛得很順，
        也可能代表我們瞎了那一段。序號是唯一分得出來的東西。

        兩個來源都餵進這裡：410 自己帶的 `sequence`，以及 411
        `CURRENT_EVENT_SEQUENCE`（**機端主動報「我現在到第幾號」**——
        它讓「整批都沒收到」也偵測得出來，那是 410 自己看不到的情況）。

        **只偵測不補請求**：補請求要送 `MAV_CMD_REQUEST_EVENT`＝`COMMAND_LONG`，
        而 backend 的唯讀邊界是**依訊息型別**擋的——放行它等於同時放行 arm
        與切模式（見 `scripts/test-readonly-boundary.py`）。要補請求該由
        command 服務做，與 038 的 `AUTOPILOT_VERSION` 同一條路。
        """
        prev = ent.get("evt_seq")
        ent["evt_seq"] = seq
        if reset or prev is None:
            # 機端歸零（重開機／重連）或我們第一次看到——**都不是掉包**
            if reset:
                ent["evt_seq"] = seq
            return
        gap = (seq - prev) & 0xFFFF        # u16 迴繞
        if gap <= 1 or gap > 0x8000:
            return                          # 沒缺口，或序號倒退（不當成掉包）
        missed = gap - 1
        log.warning("機上事件序號缺口：%d → %d（漏 %d 則，來源 %s）",
                    prev, seq, missed, src)
        try:
            ev = await db.insert_event(
                st.drone_id, st.session_id, "warning", "vehicle_events_missed",
                {"from_seq": prev, "to_seq": seq, "missed": missed, "source": src,
                 "note": "這段期間的機上事件沒有收到。**事件流的安靜在這裡"
                         "不代表沒事**——只代表我們沒看到"})
            ev["drone"] = st.drone_name
            await manager.broadcast({"type": "event", "event": ev})
        except Exception:
            log.exception("事件缺口紀錄寫入失敗")

    async def _rc_event(self, st: LiveState, ent: dict, prev) -> None:
        """RC 狀態轉態時留痕。

        **「不知道 → 掉線」也要留痕**：那是最該被看見的一次轉換，而只比對
        True/False 的寫法會讓它安靜地過去。第一次讀到不算轉態（沒有「之前」）。
        """
        if not ent.get("rc_seen"):
            ent["rc_seen"] = True
            return
        if prev == st.rc_link:
            return
        txt = ("遙控器已連線" if st.rc_link else
               "⚠ 遙控器離線——此時不得起飛／開始任務" if st.rc_link is False
               else "遙控器狀態不明（收不到判定所需的訊息）")
        try:
            ev = await db.insert_event(
                st.drone_id, st.session_id,
                "warn" if st.rc_link is not True else "info",
                "rc_link", {"rc_link": st.rc_link, "text": txt,
                            "source": ent.get("rc_source"),
                            "note": "RC 是最後的接管手段（issues/033 第 3 層）"})
            ev["drone"] = st.drone_name
            await manager.broadcast({"type": "event", "event": ev})
        except Exception:
            log.exception("rc_link 事件寫入失敗")

    async def _identity_guard(self, st: LiveState, autopilot: int | None = None,
                              board_uid: str | None = None) -> bool:
        """新接上的機，是不是這筆記錄原本那一台（issues/038 的比對半邊）。

        **2026-09-01 實際發生**：PX4 SITL 用 sysid 1 連上，系統把它認領進一台
        ArduPilot 真機的記錄，33 筆 PX4 的 `vehicle_event` 寫進了那台真機的
        事件流——而全程只有一行 `log.info`。遙測沒被污染純粹是因為 issues/004
        的修法（未 armed 不入庫），不是因為有人擋住。

        **兩個訊號的強度不同，所以處置也不同**：

        * **廠牌不合 → 硬擋**。一台機不會重開機之後從 ArduPilot 變成 PX4，
          這個判準沒有誤判空間。
        * **`board_uid` 不合 → 只示警、不擋、也不覆蓋**。`set_board_uid` 的
          註解記著一個仍然成立的顧慮：uid2 在同一塊板子上跨重開機／韌體升級
          穩不穩定還沒有真實資料，沒驗證過就硬擋只會製造假警報。
          但**不再覆蓋**——覆蓋等於把唯一的期望值抹掉，之後就永遠比不出來。

        回傳「可以照常記錄嗎」。
        """
        bad = None
        if (autopilot is not None and st.expect_autopilot is not None
                and autopilot != st.expect_autopilot):
            from .dialect import autopilot_name
            bad = (f"這筆記錄是 {autopilot_name(st.expect_autopilot)} 的機，"
                   f"但現在這台自報 {autopilot_name(autopilot)}"
                   "——**同一個 sysid，不同的飛機**")
        elif (board_uid and st.expect_board_uid
                and board_uid != st.expect_board_uid):
            # 只示警：uid 的穩定性還沒有實測支撐（見上）
            log.warning("⚠ sysid %s 的 board_uid 與記錄不符（記錄 %s、現在 %s）"
                        "——**不覆蓋**，也不擋；uid 跨韌體升級穩不穩定尚未驗證",
                        st.sysid, st.expect_board_uid, board_uid)
            try:
                ev = await db.insert_event(
                    st.drone_id, st.session_id, "warn", "board_uid_changed",
                    {"expected": st.expect_board_uid, "got": board_uid,
                     "note": "這筆記錄現在指的可能是別塊板子。沒有硬擋，"
                             "因為 uid2 跨重開機／韌體升級的穩定性還沒實測"})
                ev["drone"] = st.drone_name
                await manager.broadcast({"type": "event", "event": ev})
            except Exception:
                log.exception("board_uid 變更事件寫入失敗")
            return False          # 不覆蓋 DB 裡的期望值
        if bad is None:
            if autopilot is not None and st.expect_autopilot is None:
                # 第一次認得：記下來當期望值。**只在原本是 NULL 時寫**
                st.expect_autopilot = autopilot
                await db.set_autopilot(st.drone_id, autopilot)
            return True
        if st.identity_ok:        # 只在轉態的那一次報，不要每拍都噴
            st.identity_ok, st.identity_reason = False, bad
            log.error("⛔ 身分不符：sysid %s %s。**這台機的資料不再記在這筆記錄"
                      "名下**——混料比斷線嚴重，而且它是靜默發生的",
                      st.sysid, bad)
            try:
                ev = await db.insert_event(
                    st.drone_id, None, "critical", "identity_mismatch",
                    {"sysid": st.sysid, "reason": bad,
                     "expected_autopilot": st.expect_autopilot,
                     "got_autopilot": autopilot,
                     "note": "資料已停止記入這筆記錄。sysid 撞號時新來的機會"
                             "繼承舊記錄——要根治得由系統指派 sysid"})
                ev["drone"] = st.drone_name
                await manager.broadcast({"type": "event", "event": ev})
            except Exception:
                log.exception("身分不符事件寫入失敗")
        return False

    async def _vehicle_event(self, ent: dict, st: LiveState, msg) -> None:
        """MAVLink EVENT（410）→ 事件流（issue 014 Phase A.2）。折疊同 STATUSTEXT：
        同 event_id 連續重複折成一筆帶 count。type='vehicle_event'、source='vehicle'；
        detail 帶 event_id＋args（前端顯示「機上事件 #id（severity）」骨架，metadata
        文字落地時同列升級全文）。"""
        if not st.identity_ok:
            # 身分不符時連事件都不記（issues/038）。2026-09-01 就是這條路徑
            # 把 33 筆 PX4 SITL 的 vehicle_event 寫進一台 ArduPilot 真機的
            # 事件流——**遙測沒被污染只是因為它沒解鎖，不是因為有人擋住**
            return

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
        await self._event_gap(st, ent, d["seq"], False, "410")
        # 人話翻譯（issue 014）：翻得出就補 text，**翻不出維持 raw、事件不丟**。
        # 前端 EventModal 讀 text/message 雙名，落地即自動顯示。
        # **把機上韌體版本傳進去**（2026-09-02）：`fw_match` 早就實作了三態，
        # 但呼叫端一直沒傳版本，所以它恆為 `unknown`——**一個接了一半的守門**。
        # 版本現在拿得到（issues/038 讓 command 服務問 AUTOPILOT_VERSION、
        # backend 記進 `flight_sw_version`），那段阻塞已經不在了。
        tr = px4_events.describe(eid, d["args_hex"], st.flight_sw_version)
        last = ent.get("evt_last")
        if last and last["event_id"] == eid and now - last["t"] < STX_FOLD_S:
            last["count"] += 1
            last["t"] = now
            detail = {"event_id": eid, "args": d["args_hex"], "count": last["count"],
                      **(tr or {})}
            upd = await db.bump_event(last["id"], detail)
            if upd is not None:
                await manager.broadcast({"type": "event", "fold": True, "event": {
                    "id": last["id"], "time": upd["time"], "severity": sev,
                    "type": "vehicle_event", "detail": detail, "source": "vehicle",
                    "drone": st.drone_name}})
                return
        ev = await db.insert_event(st.drone_id, st.session_id, sev, "vehicle_event",
                                   {"event_id": eid, "args": d["args_hex"], "count": 1,
                                    **(tr or {})},
                                   source="vehicle")
        ev["drone"] = st.drone_name
        ent["evt_last"] = {"id": ev["id"], "event_id": eid, "count": 1, "t": now}
        await manager.broadcast({"type": "event", "event": ev})

    async def _landed_transition(self, st: LiveState) -> None:
        """飛控說的「我在地上還是空中」→ 這一趟的離地區間（§8c）。

        **三個非 `on_ground` 的值都算離地**：短跳可能來不及進 `IN_AIR` 就落地
        （實測 `20260902-081800.tlog` 整份只有 `on_ground` 與 `takeoff`）。

        停止錄影不在這裡做——它需要「在地上持續 N 秒」，而那是一個時間條件，
        由每秒跑一次的迴圈判（`main._close_orphan_sessions` 同一圈）。
        """
        ls = st.landed_state
        if ls is None:
            return
        if not st.landed_state_seen:
            st.landed_state_seen = True
            if st.session_id:
                await db.mark_landed_seen(st.session_id)
        if ls == "on_ground":
            if st.on_ground_since is not None:
                return                      # 已經在地上了，不是轉換
            st.on_ground_since = time.monotonic()
            # 落地那一刻記終點（每次覆蓋——一趟可能起降好幾次，要最後一次）
            if st.airborne_seen and st.session_id:
                await db.mark_airborne(st.session_id, first=False)
            return
        # 非 on_ground＝在空中（含 takeoff / landing）
        st.on_ground_since = None
        if not st.airborne_seen:
            st.airborne_seen = True
            if st.session_id:
                await db.mark_airborne(st.session_id, first=True)

    async def _armed_transition(self, st: LiveState, armed: bool):
        """架次邊界。賦值順序沿用原 ingest.py 的紀律（見該處歷史註解）：
        解鎖先建 session 再標 armed；上鎖先清旗標再結算。"""
        if armed and not st.armed:
            st.session_id = await db.create_session(st.drone_id)
            st.armed = True
            # **每一趟從零開始**：上一趟的「飛過了」留著會讓這一趟一 arm 就
            # 具備停止錄影的條件
            st.airborne_seen = False
            st.landed_state_seen = False
            st.landed_stopped = False
            st.on_ground_since = None
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
            # 從未離地的那一趟，影像自動不留（使用者定案 2026-09-08）。
            # **架次紀錄一律留著**——它真的發生過，刪的只有影像
            if sid:
                asyncio.create_task(
                    video_rec.discard_if_never_airborne(sid, st.sysid, st))

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
