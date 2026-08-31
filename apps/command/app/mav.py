"""command 服務的 MAVLink 路由：單埠多機（sysid demux）＋指令協定。

設計定案（doc/gcs-replacement.md §1、issues/012）：
  - sysid 254（QGC 慣用 255，板凳期並存可辨識）
  - 單埠收發所有機：收包時維護「sysid → 來源位址」路由表，發送依表原路送回
  - 1Hz GCS 心跳逐台送——這是 PX4 datalink-loss failsafe 的觸發源，
    心跳一開始發，本服務就進入安全鏈（服務存活屬飛安相關）
  - 指令契約：送出 → 等 ACK → 重送 → 逾時明示失敗。**無 ACK 不得視為成功**
  - 任務上傳：MAVLink 2 MISSION_ITEM_INT 完整握手，上傳後**回讀比對**才算數

並發模型：單一執行緒獨佔 socket（收發與協定對話都在這裡，無競態）；
API 層用 submit() 丟工作進佇列、等 future。對話期間 _wait() 內仍持續
處理心跳與路由表更新。
"""
import os

os.environ.setdefault("MAVLINK20", "1")     # 強制 MAVLink 2（MISSION_ITEM_INT 需要）

import concurrent.futures
import logging
import queue
import threading
import time

from pymavlink import mavutil

import autopilot as _autopilot          # 共用驅動層（libs/，PYTHONPATH=/srv/libs）

from . import capabilities as caps

log = logging.getLogger("command.mav")
M = mavutil.mavlink
#: 我方 GCS 的 sysid。**2026-08-25 由 254 改為 255**（使用者裁定）：
#: ArduPilot 只信 `SYSID_MYGCS` 指定來源的部分指令，而機端那個參數是 255
#: （出廠預設，也是 QGC 慣用值）。原本的做法是叫使用者去把機端改成 254——
#: **要一台實體飛機為了配合地面站改參數，方向是反的**：改我方一個常數，
#: 比每次換機都要記得改飛控參數可靠。
#:
#: 代價：與 QGC 撞號。同一條鏈路上同時掛 QGC 與本系統時，飛控分不出誰是誰
#: （見 doc/gcs-replacement.md）。板凳期要兩者並存的話，改 QGC 那一側
#: （QGC 設定裡可改 MAVLink System ID），不要改回這裡——改回來就會退回
#: 「指令被靜默丟棄、沒有任何錯誤訊息」那個狀態。
GCS_SYSID = 255
#: 活性門檻：主迴圈超過這麼久沒跑過一圈＝卡住（見 MavRouter.alive）。
#: 正常節奏是 run() 每圈 ≤0.2s、指令對話期間 _wait() 每圈 ≤0.2s，
#: 兩條路徑都會呼叫 _tick()，所以 5s 對「正常但忙碌」有極大餘裕。
STALL_S = 5.0

# MAV_TYPE → 粗略載具類別（選配，前端徽章分 ArduCopter/ArduPlane 用）
_VEHICLE_TYPES = {2: "quadrotor", 13: "hexarotor", 14: "octorotor",
                  1: "fixed_wing", 10: "ground_rover", 12: "submarine"}

# ── 方言：**已搬到共用驅動層 `libs/autopilot/`**（issue 026 B2）────────
# backend 與 command 兩個服務共用同一份，編譯期就一致，不需要漂移偵測。
# **不要把廠牌知識加回這裡**——加在 libs/autopilot/<廠牌>.py。

#: 給 API 參數驗證用（main.py 檢查 mode 是否合法）。以 PX4 的模式集為準，
#: 與搬遷前相同——ArduPilot 多一個 guided，那是起飛序列內部用的，不對外開放。
PX4_MODES = _autopilot.Px4Driver.modes
ARDU_COPTER_MODES = _autopilot.ArduPilotDriver.modes


def dialect(r: "MavRouter", sysid: int) -> dict:
    """該機的方言參數。**判斷只在這裡做一次，其餘 job_* 只讀這個 dict。**

    形狀維持搬遷前不變（呼叫端零改動）；內容改由驅動提供。
    """
    raw = (r.drones.get(sysid) or {}).get("autopilot")
    drv = _autopilot.get_driver(raw)
    return {
        "autopilot": _autopilot.autopilot_name(raw),
        "driver": drv,
        "home_at_seq0": drv.home_at_seq0,
        "wire_seq": drv.wire_seq,
        "takeoff_alt_is_relative": drv.takeoff_alt_is_relative,
        "takeoff_needs_guided": drv.takeoff_needs_guided,
        "mode_num": drv.encode_mode,
        "mode_matches": drv.mode_matches,
        "modes": drv.modes,
    }


class CommandError(Exception):
    """指令失敗（逾時、被拒、比對不符）。訊息可直接呈現給操作員。"""


class MavRouter(threading.Thread):
    daemon = True

    def __init__(self, url: str, heartbeat: bool = True):
        super().__init__(name="mav-router")
        self.conn = mavutil.mavlink_connection(
            url.replace("://", ":"),
            source_system=GCS_SYSID,
            source_component=M.MAV_COMP_ID_MISSIONPLANNER)
        # mavutil 的 udpin 不記錄封包來源（write 是對全部 client 廣播）——
        # 單埠多機需要逐 datagram 的來源位址才能按 sysid 路由回程，
        # 接管 recv 補上這件事（發送端見 _sendto：encode + sendto 指定位址）。
        import socket as _socket

        def _recv(n=None, _conn=self.conn):
            try:
                data, addr = _conn.port.recvfrom(65535)
            except _socket.error:
                return ""
            _conn.last_address = addr
            return data
        self.conn.recv = _recv
        self.heartbeat = heartbeat
        self.drones: dict[int, dict] = {}   # sysid → addr/seen_mono/armed/custom_mode
        self.jobs: queue.Queue = queue.Queue()
        self._hb_t = 0.0
        self._send_warn_t = 0.0             # sendto 失敗告警節流（見 _sendto）
        # 迴圈活性時戳（見 alive()／STALL_S）。初值設為現在而非 0：執行緒
        # start() 之前就被健康檢查問到時，不該回報成「卡住」。
        self._alive_t = time.monotonic()

    # ── API 層入口（任意執行緒呼叫；在 executor 裡跑，不阻塞事件迴圈）──
    def submit(self, fn, *args, timeout: float = 30.0):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.jobs.put((fn, args, fut))
        return fut.result(timeout=timeout)

    def snapshot(self) -> dict:
        now = time.monotonic()
        out = {}
        for sysid, d in self.drones.items():
            ap = caps.autopilot_name(d.get("autopilot"))
            cap, reasons = caps.capabilities_for(ap, d)
            out[str(sysid)] = {
                "age_s": round(now - d.get("seen_mono", now), 1),
                "armed": d.get("armed"),
                "custom_mode": d.get("custom_mode"),
                "autopilot": ap,                       # 字串枚舉 px4/ardupilot/unknown
                "autopilot_raw": d.get("autopilot"),   # MAV_AUTOPILOT_*，除錯用
                "vehicle_type": _VEHICLE_TYPES.get(d.get("type")),
                # per-sysid 高度：起飛序列與群組執行器的判斷依據。**列在這裡是
                # 因為它看不見的時候，「讀到別台的高度」這種 bug 也看不見**
                # （2026-08-12：單機 mission_fly 讀主機高度，錯了多久沒人知道）。
                "alt_rel": d.get("alt_rel"),
                "alt_msl": d.get("alt_msl"),
                "lat": d.get("lat"), "lon": d.get("lon"),
                "capabilities": cap,                   # 伺服器端 gating 唯一真相
                "capability_reasons": reasons,
            }
        return out

    # ── 主迴圈（唯一碰 socket 的執行緒）──────────────────────────
    def run(self):
        # **這個迴圈不准死。** 它是本服務與飛機唯一的收發者：死掉後 socket 沒人讀、
        # 心跳停發（PX4 依 COM_DL_LOSS_T 觸發 failsafe）、指令全逾時——而 HTTP 層無感、
        # /healthz 照回 ok（殭屍）。實戰踩過（2026-08-11）：5G 瞬斷讓心跳 sendto() 丟
        # ENETUNREACH，例外從 _tick() 上拋殺死執行緒，服務殭屍近一小時。網路瞬斷是常態，
        # 整個迴圈體包一層吞掉續跑（feat/command-external-trigger 併入）。
        while True:
            try:
                self._tick()
                try:
                    fn, args, fut = self.jobs.get_nowait()
                except queue.Empty:
                    self._recv(0.2)
                    continue
                try:
                    fut.set_result(fn(self, *args))
                except Exception as e:        # 工作失敗浮回呼叫端；不殺迴圈
                    fut.set_exception(e)
            except Exception:                 # _tick/_recv 的網路例外等——吞掉續跑
                log.exception("router 迴圈例外（網路瞬斷等），吞掉續跑")

    def alive(self) -> bool:
        """迴圈還在轉嗎——`/healthz` 用這個判斷服務是不是殭屍（issue 034）。

        兩件事都要問，因為它們是**不同的死法**：

        - `is_alive()`：執行緒還在嗎。run() 的 catch-all 之後純例外殺不死它，
          但 BaseException（MemoryError／SystemExit）仍會。
        - `_alive_t`：迴圈還在轉嗎。執行緒活著卻卡在某個不會回來的呼叫上
          （socket 進不明狀態、job 內無限等待），對飛機的效果與死掉相同：
          心跳停發、指令不動。只問 `is_alive()` 會漏掉這一種。
        """
        return self.is_alive() and (time.monotonic() - self._alive_t) < STALL_S

    def _tick(self):
        now = time.monotonic()
        # 活性時戳。蓋在 _tick 而不是 run()：指令對話期間 run() 停在 _wait()
        # 裡數十秒，但 _wait() 每圈都呼叫 _tick()（心跳不能斷），所以這裡才是
        # 「迴圈真的有在轉」的唯一共同點。
        self._alive_t = now
        if self.heartbeat and now - self._hb_t >= 1.0:
            self._hb_t = now
            for sysid in list(self.drones):
                try:
                    self._sendto(sysid, lambda m: m.heartbeat_encode(
                        M.MAV_TYPE_GCS, M.MAV_AUTOPILOT_INVALID, 0, 0, 0))
                except CommandError:
                    pass

    def _recv(self, timeout: float):
        msg = self.conn.recv_match(blocking=True, timeout=timeout)
        if msg is None:
            return None
        sysid = msg.get_srcSystem()
        if sysid and sysid != GCS_SYSID and self.conn.last_address:
            d = self.drones.get(sysid)
            # 只有「自駕儀的心跳」才建檔——PX4 會在鏈路間轉發訊息，
            # 其他 GCS（mavsdk 245、QGC 255）的訊息也會出現在這個埠
            if (d is None and msg.get_type() == "HEARTBEAT"
                    and msg.type != M.MAV_TYPE_GCS
                    and msg.autopilot != M.MAV_AUTOPILOT_INVALID):
                d = self.drones.setdefault(sysid, {"texts": []})
            if d is not None:
                d["addr"] = self.conn.last_address   # 單埠多機的回程路由表
                d["seen_mono"] = time.monotonic()
                if msg.get_type() == "HEARTBEAT" and msg.type != M.MAV_TYPE_GCS:
                    d["armed"] = bool(msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED)
                    d["custom_mode"] = msg.custom_mode
                    d["autopilot"] = msg.autopilot   # 飛安：模式指令方言分家
                    d["type"] = msg.type             # （issue 015／gap-analysis.md）
                    # 板子身分（issues/038）：AUTOPILOT_VERSION 帶飛控板的唯一
                    # ID 與韌體版本，而**兩家都不主動送，要開口問**。
                    # 由 command 問而不是 backend：請求要送 COMMAND_LONG，那是
                    # 通用信封（同一型別可以裝 arm），backend 的 read-only 邊界
                    # 不收它。回應是廣播式的，backend 照樣收得到並記錄。
                    # **一次就好**：板子身分不會變。機上有我方代理時代理也會問，
                    # 重複請求無副作用；沒有代理的機（SITL、他人的機）只有這裡問。
                    if self.heartbeat and not d.get("caps_req"):
                        d["caps_req"] = True
                        try:
                            self._sendto(sysid, lambda m: m.command_long_encode(
                                sysid, 1, 520, 0, 1, 0, 0, 0, 0, 0, 0))
                        except CommandError:
                            d["caps_req"] = False      # 送不出去就下次再試
                elif msg.get_type() == "GLOBAL_POSITION_INT":
                    # per-sysid 高度——「等到達高度才切 MISSION」的依據
                    # （issue 013-B；單機 mission_fly 教訓的多機版，不靠 backend）。
                    # **單機路徑原本漏了這一課**：mission_fly 與 _do_takeoff 讀
                    # backend `/api/live`，而那個端點只回**主機**——飛非主機時
                    # 拿到的是別台的高度（2026-08-12 前端驗收實測：uav-s2 起飛
                    # 成功卻回報 -0.04m，那是停在地面的主機）。已改為與這裡同源。
                    d["alt_rel"] = msg.relative_alt / 1000.0
                    d["alt_msl"] = msg.alt / 1000.0
                    # 經緯度：一致性測試量「搖桿有沒有真的讓機動」的唯一證據
                    # （MANUAL_CONTROL 無 ACK，位移是唯一可觀察的結果）。
                    # **0,0 是自駕儀的「不知道」哨兵，不是幾內亞灣外海。**
                    # 沒有 GPS 定位時 GLOBAL_POSITION_INT 會送 0/0；照收的話
                    # 改航線提案會從一個一萬公里外的位置去算「最近的航點」，
                    # 而且算得出一個看起來很正常的數字。backend 早就有這條
                    # 規則（mavlink_rx.py），指令服務漏了
                    if msg.lat or msg.lon:
                        d["lat"] = msg.lat / 1e7
                        d["lon"] = msg.lon / 1e7
                    # 航向：改航線提案要能說「續飛航點在你後方 N 度，機體會先
                    # 掉頭」。沒有它那句警告就永遠不會出現——**而不是不會發生**
                    if msg.hdg != 65535:          # 65535＝不知道
                        d["heading"] = msg.hdg / 100.0
                elif msg.get_type() == "STATUSTEXT":
                    # PX4 的解釋（"Arming denied: ..."）——被拒時要能拿出來給人看。
                    # 實戰教訓：沒有這段文字，操作員只看到 result code 乾瞪眼
                    d.setdefault("texts", []).append((time.monotonic(), msg.text.strip()))
                    del d["texts"][:-20]
        return msg

    def texts_since(self, sysid: int, t0: float) -> list:
        d = self.drones.get(sysid) or {}
        return [txt for ts, txt in d.get("texts", []) if ts >= t0]

    def autopilot_of(self, sysid: int):
        """該機的 HEARTBEAT.autopilot（MAV_AUTOPILOT_*）；未見心跳時 None。"""
        d = self.drones.get(sysid) or {}
        return d.get("autopilot")

    def _sendto(self, sysid: int, encode_fn):
        """encode + 直接 sendto 該 sysid 的來源位址（不經 mavutil 的廣播式 write）。"""
        d = self.drones.get(sysid)
        if not d or "addr" not in d:
            raise CommandError(f"sysid {sysid} 未連線（心跳未見）")
        msg = encode_fn(self.conn.mav)
        buf = msg.pack(self.conn.mav)
        try:
            self.conn.port.sendto(buf, d["addr"])
        except OSError as e:
            # 網路瞬斷（5G 常態）時 sendto() 丟的是**裸 OSError**（ENETUNREACH／
            # EHOSTUNREACH…），不是 CommandError——呼叫端 _tick 的
            # `except CommandError` 因此接不住，例外會逃到 run() 的 catch-all：
            # 執行緒雖然活著（那層 guard 是 2026-08-11 殭屍事故的修法），但
            # (1) 外層用 log.exception 印完整 traceback，多機時＝traceback 洪水，
            #     淹掉其他診斷；
            # (2) 例外從 _tick 的逐機心跳迴圈中逃出，該輪剩下的機沒送到心跳、
            #     _recv 也被跳過。
            # 包成 CommandError 讓既有的逐機 except 真的接得住＝安靜略過該機、
            # 該輪其他機照送。對 job_* 路徑則是更準的分類（502「指令失敗＋原因」
            # 而非 500「內部錯誤」）。
            now = time.monotonic()
            if now - self._send_warn_t >= 5.0:   # 節流：洪水無助診斷，5s 一筆就夠
                self._send_warn_t = now
                log.warning("送給 sysid %s 失敗：%s（網路瞬斷？5s 內同類不重複印）",
                            sysid, e)
            raise CommandError(
                "送給 sysid {} 失敗（{}: {}）".format(sysid, type(e).__name__, e))
        self.conn.mav.seq = (self.conn.mav.seq + 1) % 256

    def _wait(self, sysid: int, types: tuple, pred=None, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._tick()                     # 對話期間心跳不斷
            msg = self._recv(0.2)
            if msg is None or msg.get_srcSystem() != sysid:
                continue
            if msg.get_type() in types and (pred is None or pred(msg)):
                return msg
        return None


# ── 工作函式（在 router 執行緒內執行）─────────────────────────

# result code → 操作指引（來自現場工具 start_mission.py 的實戰註解）
RESULT_HINTS = {
    M.MAV_RESULT_TEMPORARILY_REJECTED: "暫時拒絕——EKF/GPS 暖機中，稍等 30–60 秒再試",
    M.MAV_RESULT_DENIED: "被拒——看 autopilot_notes 的具體原因（GPS/校準/RC/任務狀態）",
    M.MAV_RESULT_UNSUPPORTED: "不支援此指令",
    M.MAV_RESULT_FAILED: "執行失敗",
}


def job_command(r: MavRouter, sysid: int, command: int, params: list,
                retries: int = 3, ack_timeout: float = 2.0) -> dict:
    """COMMAND_LONG → 等 ACK → 重送。無 ACK 一律例外，不得視為成功。
    被拒時帶回同時段 PX4 的 STATUSTEXT——原因要能給人看。"""
    p = (list(params) + [0.0] * 7)[:7]
    t0 = time.monotonic()
    for attempt in range(1, retries + 1):
        t_send = time.monotonic()      # ACK 往返時序（issue 013-B 時序驗收 item 1）
        r._sendto(sysid, lambda m: m.command_long_encode(sysid, 1, command, 0, *p))
        ack = r._wait(sysid, ("COMMAND_ACK",),
                      lambda msg: msg.command == command, ack_timeout)
        if ack is not None:
            accepted = ack.result == M.MAV_RESULT_ACCEPTED
            res = {"result": M.enums["MAV_RESULT"][ack.result].name,
                   "accepted": accepted, "attempts": attempt,
                   "ack_ms": round((time.monotonic() - t_send) * 1000, 1)}
            if not accepted:
                # 多等 1.5 秒收 PX4 的解釋文字（拒絕原因常在 ACK 之後才廣播）
                r._wait(sysid, ("_none_",), timeout=1.5)
                res["hint"] = RESULT_HINTS.get(ack.result, "")
                res["autopilot_notes"] = r.texts_since(sysid, t0)
            return res
    raise CommandError(f"指令 {command} 無 ACK（重試 {retries} 次）"
                       f"｜autopilot_notes={r.texts_since(sysid, t0)}")


def job_mission_goto(r: MavRouter, sysid: int, index: int) -> dict:
    """指定機端從**我方航點索引 index** 續飛（MISSION_SET_CURRENT）。

    **為什麼這個動作是必要的而不是加分項**（2026-08-25 SITL 實測，兩家一致）：
    飛行中上傳新任務後，機端**不會把 MISSION_CURRENT 歸零**，而是把舊任務的
    索引原封沿用到新任務上。舊任務的第 2 點與新任務的第 2 點毫無關係——
    不主動指定的話，飛機會飛到一個純粹由「上一份任務進行到第幾點」決定的
    位置。那不是次佳解，是**未定義行為**。

    索引換算走驅動（`wire_seq`）：ArduPilot 的 home 佔 seq 0，我方索引 N 在
    機端是 N+1。**少了這層換算會差一個航點，而且沒有任何錯誤訊息。**
    """
    d = dialect(r, sysid)
    seq = d["wire_seq"](index)

    # **用 MISSION_SET_CURRENT（訊息 41）而不是 DO_SET_MISSION_CURRENT（指令 224）。**
    # 2026-08-25 實測：PX4 1.14.3 與 ArduPilot 4.0.3 對指令 224 都回
    # MAV_RESULT_UNSUPPORTED。224 是後來才加進 MAVLink 的指令形式，而 41 是
    # 任務協定的原生做法（QGC 一直用它）。
    #
    # 代價：**41 沒有 ACK**。所以驗證只能看效果——讀回 MISSION_CURRENT 確認
    # 機端真的跳過去了。這反而符合本專案的既有紀律（以機端實際狀態為準，
    # 不看我方送了什麼）。
    r._sendto(sysid, lambda m: m.mission_set_current_encode(sysid, 1, seq))

    deadline = time.monotonic() + 3.0
    got = None
    while time.monotonic() < deadline:
        msg = r._wait(sysid, ("MISSION_CURRENT",), timeout=0.5)
        if msg is not None:
            got = msg.seq
            if got == seq:
                break
    ok = got == seq
    if not ok:
        raise CommandError(
            f"送出 MISSION_SET_CURRENT(seq={seq}) 後，機端仍在 seq={got}"
            f"（3 秒內未跳轉）——**41 無 ACK，只能以機端狀態為準**")
    return {"ok": True, "index": index, "wire_seq": seq, "mission_seq": got, "autopilot": d["autopilot"],
            "verified_by": "MISSION_CURRENT 讀回"}


def job_set_mode(r: MavRouter, sysid: int, mode: str,
                 retries: int = 3, verify_timeout: float = 3.0) -> dict:
    """切模式 → ACK → **驗證真的切了**（HEARTBEAT.custom_mode 轉到目標）。

    關鍵（實測，2026-08-11）：PX4 對 DO_SET_MODE 常回 ACCEPTED，但 commander
    可能沒真的轉（前置條件、暫態）。只看 ACK 會**誤報成功**——操作員按了
    「原地降落」看到 OK，機卻沒切、繼續原本動作（若在爬升就像「降落讓它飛高」）。
    故 ACCEPTED 後還要看 HEARTBEAT 確認轉到目標模式，沒轉就重試、再不行明示失敗。
    """
    d = dialect(r, sysid)
    if mode not in d["modes"]:
        raise CommandError(f"{d['autopilot']} 不支援模式 {mode}")
    p2, p3 = d["mode_num"](mode)
    for attempt in range(1, retries + 1):
        res = job_command(r, sysid, M.MAV_CMD_DO_SET_MODE,
                          [M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, p2, p3])
        if not res.get("accepted"):
            return res            # 明確被拒（帶原因）——不是「沒生效」，直接回
        deadline = time.monotonic() + verify_timeout
        while time.monotonic() < deadline:
            r._wait(sysid, ("HEARTBEAT",), timeout=1.2)
            cm = (r.drones.get(sysid) or {}).get("custom_mode")
            if cm is not None and d["mode_matches"](cm, mode):
                return {**res, "mode_engaged": True, "attempts_mode": attempt}
    raise CommandError(
        f"模式 {mode} 已被接受但未生效——DO_SET_MODE 回 ACCEPTED，但機端 "
        f"{verify_timeout:.0f}s 內未切到目標模式（PX4 commander 未執行轉換，"
        f"檢查前置條件/狀態）｜autopilot_notes={r.texts_since(sysid, time.monotonic() - 5)}")


# ── 任務方言（issue 015 實測；issue 026 抽驅動時從這裡提取）──────────────
# **本檔的方言分支集中在這一處**，不要散落到各 job_* 裡——之後把廠牌差異收進
# 獨立驅動時，要能「把這一段提取出來」而不是全域搜捕。
def job_takeoff(r: MavRouter, sysid: int, alt: float, ground_amsl=None) -> dict:
    """起飛序列（方言差異集中在這裡，見 dialect()）。

    - **PX4**：NAV_TAKEOFF 的 param7 是**絕對海拔**，要用地面海拔＋目標高度。
    - **ArduPilot Copter**：param7 是**相對高度**（送絕對海拔會差一整個地面
      海拔、數百公尺），而且必須**先進 GUIDED 才能 arm 與起飛**。

    回傳逐步結果，任一步失敗就往上拋（呼叫端已有留痕與錯誤呈現）。
    """
    d = dialect(r, sysid)
    steps = {}
    if d["takeoff_needs_guided"]:
        # Copter 在 LOITER/STABILIZE 下 arm 了也不會照指令起飛——先進 GUIDED
        steps["guided"] = job_set_mode(r, sysid, "guided")
    if not (r.drones.get(sysid) or {}).get("armed"):
        res = job_command(r, sysid, 400, [1.0])
        steps["arm"] = res
        # **解鎖失敗就停**：繼續送 NAV_TAKEOFF 的話，機端會回 ACCEPTED（指令本身
        # 合法）但飛機根本沒解鎖、不會離地——操作員看到「takeoff: ACCEPTED」卻
        # 什麼也沒發生。舊版靠 _run 在 arm 被拒時直接拋出，改寫成 job 之後要自己
        # 顧這件事（2026-08-12 回歸測試抓到）。
        if not res.get("accepted"):
            raise CommandError(
                "解鎖被拒（%s），未送出起飛指令" % res.get("result", "?")
                + ("｜" + "；".join(res.get("autopilot_notes", []))
                   if res.get("autopilot_notes") else ""))
    if d["takeoff_alt_is_relative"]:
        p7 = alt
    else:
        if ground_amsl is None:
            raise CommandError("PX4 起飛需要地面海拔（GPS/EKF 未就緒）")
        p7 = ground_amsl + alt
    # 偏航/經緯度：PX4 用 NaN 表示「用當前值」；**ArduPilot 對 NaN 沒有 ACK**
    # （實測 2026-08-12：送 NaN 的 NAV_TAKEOFF 連 ACK 都沒有，指令被靜默丟棄），
    # 它的慣例是 0＝當前位置。又一個方言差異。
    blank = 0.0 if d["takeoff_alt_is_relative"] else float("nan")
    steps["takeoff"] = job_command(
        r, sysid, M.MAV_CMD_NAV_TAKEOFF,
        [0.0, 0.0, 0.0, blank, blank, blank, p7])
    return {"steps": steps, "alt_param7": p7,
            "alt_semantics": "relative" if d["takeoff_alt_is_relative"] else "amsl"}


def job_upload_mission(r: MavRouter, sysid: int, items: list) -> dict:
    """完整上傳握手 → 機端 ACK → 回讀逐項比對 → 收 PX4 廣播的驗證訊息。

    丟包韌性（對齊實戰工具 upload_mission.py v3，戶外 5G 實測經驗）：
    - MISSION_COUNT 每 2 秒重送直到機端開始請求（握手能不能開始的關鍵）
    - 項目遺失由機端重複請求同 seq 自然補（協定內建），總期限 30 秒
    - 回讀的 REQUEST_LIST 重試 3 次、逐項重試 2 次
    """
    mt = M.MAV_MISSION_TYPE_MISSION
    d = dialect(r, sysid)
    # ArduPilot：seq 0 留給 home，真航點往後移一格（line[i] 是要送給機上的第 i 項）。
    # 佔位用第一個航點的座標而不是 0,0,0——實測 ArduPilot 會用實際 home 覆蓋它，
    # 但萬一某版本沒覆蓋，一個「任務起點附近」的 home 遠比 (0,0,0) 安全。
    if d["home_at_seq0"] and items:
        f = items[0]
        home = {**f, "seq": 0, "command": M.MAV_CMD_NAV_WAYPOINT,
                "frame": M.MAV_FRAME_GLOBAL, "p1": 0, "p2": 0, "p3": 0, "p4": 0}
        line = [home] + [{**it, "seq": i + 1} for i, it in enumerate(items)]
    else:
        line = items
    n = len(line)
    r._sendto(sysid, lambda m: m.mission_count_encode(sysid, 1, n, mt))
    last_count_tx = time.monotonic()
    handshake_started = False
    ack = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        msg = r._wait(sysid, ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"),
                      timeout=1.0)
        if msg is None:
            # 沒動靜且握手未開始 → COUNT 可能丟包，重送
            if not handshake_started and time.monotonic() - last_count_tx >= 2.0:
                r._sendto(sysid, lambda m: m.mission_count_encode(sysid, 1, n, mt))
                last_count_tx = time.monotonic()
            continue
        if msg.get_type() == "MISSION_ACK":
            ack = msg
            break
        handshake_started = True
        it = line[msg.seq]
        r._sendto(sysid, lambda m, it=it: m.mission_item_int_encode(
            sysid, 1, it["seq"], it["frame"], it["command"], 0, 1,
            it["p1"], it["p2"], it["p3"], it["p4"], it["x"], it["y"], it["z"], mt))
    if ack is None:
        raise CommandError("30 秒內未完成上傳（排查：鏈路丟包／機端 MAVLink 實例）")
    if ack.type != M.MAV_MISSION_ACCEPTED:
        raise CommandError(
            f"機端拒絕任務：{M.enums['MAV_MISSION_RESULT'][ack.type].name}")

    # 回讀比對：上傳成功的定義是「機上任務與我們要上傳的一致」，不是收到 ACK
    cnt = None
    for _ in range(3):
        r._sendto(sysid, lambda m: m.mission_request_list_encode(sysid, 1, mt))
        cnt = r._wait(sysid, ("MISSION_COUNT",), timeout=3.0)
        if cnt is not None:
            break
    if cnt is None or cnt.count != n:
        raise CommandError(f"回讀筆數不符：機上 {getattr(cnt, 'count', '無回應')}，預期 {n}")
    # 逐項比座標（不是只比筆數）——這個檢查是唯一會發現「機上內容跟我們以為的
    # 不一樣」的東西。ArduPilot 的 seq 0 是機端自己的 home，內容本來就不等於我們
    # 送的佔位值，**跳過它的內容比對**但仍要求它存在（筆數已含）。
    skip = 1 if d["home_at_seq0"] else 0
    for seq in range(skip, n):
        it = None
        for _ in range(2):
            r._sendto(sysid, lambda m, s=seq: m.mission_request_int_encode(sysid, 1, s, mt))
            it = r._wait(sysid, ("MISSION_ITEM_INT",), lambda msg, s=seq: msg.seq == s, 3.0)
            if it is not None:
                break
        if it is None:
            raise CommandError(f"回讀第 {seq} 項逾時")
        o = line[seq]
        if (it.command != o["command"] or abs(it.x - o["x"]) > 2
                or abs(it.y - o["y"]) > 2 or abs(it.z - o["z"]) > 0.5):
            raise CommandError(f"回讀比對不符（seq {seq}）：機上內容與上傳不一致")
    r._sendto(sysid, lambda m: m.mission_ack_encode(sysid, 1, M.MAV_MISSION_ACCEPTED, mt))

    # 聽 3 秒 PX4 廣播的任務驗證結果（被拒原因直接看得到；
    # PX4 1.14 多走 Events 協定，STATUSTEXT 可能為空——有就帶回）
    notes = []
    t_end = time.monotonic() + 3.0
    while time.monotonic() < t_end:
        s = r._wait(sysid, ("STATUSTEXT",), timeout=0.5)
        if s is not None:
            notes.append(s.text.strip())
    # uploaded 回報**真航點數**（不含 ArduPilot 的 home 佔位），否則呼叫端與
    # UI 會看到莫名多一項
    return {"uploaded": len(items), "verified": True,
            "autopilot_notes": notes, "wire_items": n}
