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
from .state import LiveState, fleet, live
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
# 模組的邊界定義（「不含改變機上狀態的能力」）。**PARAM_SET 永遠不得加入**——
# 參數編輯是 QGC 的職權，本系統只記錄「當時設定是什麼」，不去改它。
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
            st.mission_seq = msg.seq
            st.mission_total = getattr(msg, "total", None)
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
            prev_rc = st.rc_link
            st.rc_link = bool(h_ & rc_bit) if (p_ & rc_bit) else None
            if ent.get("rc_seen") and prev_rc != st.rc_link:
                # **「不知道 → 掉線」也要留痕。** 那是最該被看見的一次轉換，
                # 而只比對 True/False 的寫法會讓它安靜地過去
                txt = ("遙控器已連線" if st.rc_link else
                       "⚠ 遙控器離線——此時不得起飛／開始任務" if st.rc_link is False
                       else "遙控器狀態不明（韌體停止回報）")
                try:
                    ev = await db.insert_event(
                        st.drone_id, st.session_id,
                        "warn" if st.rc_link is not True else "info",
                        "rc_link", {"rc_link": st.rc_link, "text": txt,
                                    "note": "RC 是最後的接管手段（issues/033 第 3 層）"})
                    ev["drone"] = st.drone_name
                    await manager.broadcast({"type": "event", "event": ev})
                except Exception:
                    log.exception("rc_link 事件寫入失敗")
            ent["rc_seen"] = True
        elif t in dialect.EKF_MSG_TYPES:
            # 訊息層方言（差異 8→12）：PX4 發 ESTIMATOR_STATUS、ArduPilot 發
            # EKF_STATUS_REPORT，**同一件事兩個訊息名**，所需位元同義。等價的
            # 邊界（哪些位元可以互換、哪些不行）寫在 dialect.py §1。
            st.ekf_ok = dialect.ekf_ready(msg.flags)
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
        # 人話翻譯（issue 014）：翻得出就補 text，**翻不出維持 raw、事件不丟**。
        # 前端 EventModal 讀 text/message 雙名，落地即自動顯示。
        tr = px4_events.describe(eid, d["args_hex"])
        last = ent.get("evt_last")
        if last and last["event_id"] == eid and now - last["t"] < STX_FOLD_S:
            last["count"] += 1
            last["t"] = now
            detail = {"event_id": eid, "args": d["args_hex"], "count": last["count"],
                      **({"text": tr["text"], "event_name": tr["event_name"],
                          "dict_fw": tr["dict_fw"],
                          "dict_fw_match": tr["dict_fw_match"]} if tr else {})}
            upd = await db.bump_event(last["id"], detail)
            if upd is not None:
                await manager.broadcast({"type": "event", "fold": True, "event": {
                    "id": last["id"], "time": upd["time"], "severity": sev,
                    "type": "vehicle_event", "detail": detail, "source": "vehicle",
                    "drone": st.drone_name}})
                return
        ev = await db.insert_event(st.drone_id, st.session_id, sev, "vehicle_event",
                                   {"event_id": eid, "args": d["args_hex"], "count": 1,
                                    **({"text": tr["text"], "event_name": tr["event_name"],
                                        "dict_fw": tr["dict_fw"],
                                        "dict_fw_match": tr["dict_fw_match"]} if tr else {})},
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
