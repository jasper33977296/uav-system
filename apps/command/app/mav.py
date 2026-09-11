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
import json
import math
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
#: 位址表輸出路徑（issues/033 §4.2.1）。**心跳已經搬到獨立行程**（`app/hb.py`），
#: 它需要每台機的來源位址，而那只有正在收包的這裡知道。寫成檔案是為了讓它
#: **跨本服務的重啟存活**——本服務一重啟心跳就跟著瞎掉的話，等於沒有解耦。
PEERS_PATH = os.environ.get("GCS_PEERS_PATH", "/state/peers.json")
PEERS_WRITE_S = 1.0
#: 位址表裡的一筆最多留這麼久。比心跳行程的過期門檻大一個量級——過期由它判，
#: 這裡只是防止檔案無限長大（例如反覆換 sysid 的測試機）
PEERS_KEEP_S = 300.0
#: `EXTENDED_SYS_STATE.landed_state` → 人話。**與 backend 同一份**
#: （`app/mavlink_rx.py:_LANDED`）——兩份會漂，而這個字彙是「機在不在空中」
#: 的判準，兩邊講不同的話等於同一台機有兩個答案。
_LANDED = {1: "on_ground", 2: "in_air", 3: "takeoff", 4: "landing"}
#: `landed_state` 多久沒更新就不再拿它下判斷。**「我們不再聽到」不等於
#: 「機還在地上」**——過期就退回高度判準，並且說出退回了（同 RC_STALE_S 的紀律）。
#: ArduPilot／PX4 都把 EXTENDED_SYS_STATE 放在低頻串流，5 s 已經很寬鬆
LANDED_STALE_S = 5.0
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


#: 一則指令工作最多等多久。**這個數字要說得出口**——逾時訊息會引用它，
#: 而「30 秒沒回應」與「內部錯誤」是完全不同的兩句話
JOB_TIMEOUT_S = 30.0


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
        self._peers_warn_t = 0.0            # 位址表寫入失敗告警節流
        # 迴圈活性時戳（見 alive()／STALL_S）。初值設為現在而非 0：執行緒
        # start() 之前就被健康檢查問到時，不該回報成「卡住」。
        self._alive_t = time.monotonic()

    # ── API 層入口（任意執行緒呼叫；在 executor 裡跑，不阻塞事件迴圈）──
    def submit(self, fn, *args, timeout: float = JOB_TIMEOUT_S):
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
        # **心跳不在這裡發了**（issues/033 §4.2.1，2026-08-31 使用者裁定）。
        # 原本每秒在這裡送 `MAV_TYPE_GCS` 心跳，於是本服務的每一次重啟——
        # 包含只是存一個檔案觸發 `--reload`——都是一次真實的 GCS 心跳中斷，
        # 而那可能超過飛控的 `FS_GCS_TIMEOUT`。現在改由獨立行程 `app/hb.py` 發，
        # 這裡只負責把它需要的東西（每台機的來源位址）寫出去。
        if self.heartbeat and now - self._hb_t >= PEERS_WRITE_S:
            self._hb_t = now
            self._write_peers()

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
                elif msg.get_type() == "EXTENDED_SYS_STATE":
                    # **「機真的離地了沒」的唯一可信來源。**「等到高度才切
                    # AUTO」原本只看 alt_rel，而 alt_rel 在沒有 GPS 定位時是
                    # 漂的——前端量過停在地面的機漂到 4.4 m（CommandPanel
                    # 那條註解）。用一個會漂的數字去證明「離地了」，門檻訂多
                    # 低都證明不了，訂多高又只是把同一個漂移往上推。
                    # 字彙與 backend 同一份（mavlink_rx._LANDED），**不另立一套**。
                    d["landed_state"] = _LANDED.get(msg.landed_state)
                    d["landed_t"] = time.monotonic()
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

    def _write_peers(self) -> None:
        """把每台機的來源位址寫給心跳行程（issues/033 §4.2.1）。

        **用寫暫存檔再 rename**：rename 在同一個檔案系統上是原子的，所以讀端
        永遠讀到一份完整的表。直接覆寫的話，心跳行程有機會讀到寫了一半的 JSON
        ——而它每秒讀一次，撞上的機率不低。

        時戳用 `time.time()`（牆鐘）不是 monotonic：讀的是**另一個行程**，
        兩邊的 monotonic 沒有可比性。
        """
        # **先讀回舊的再疊上新的，不是直接覆寫。** 本服務重啟後 `self.drones`
        # 是空的，直接覆寫就會把位址表清成 `{}`——心跳行程於是在我們重啟的那
        # 幾秒沒有對象可發，**正好抵銷掉解耦本身**（2026-08-31 實測：最大間隔
        # 因此從 2s 變成 3s，逼近 FS_GCS_TIMEOUT 的 5s 預設）。
        # 舊的一筆不會永遠留著：時戳是「最後聽到」，過期由心跳行程自己判。
        peers = {}
        try:
            with open(PEERS_PATH, encoding="utf-8") as f:
                peers = json.load(f).get("peers") or {}
        except (OSError, ValueError):
            pass
        now_mono, now_wall = time.monotonic(), time.time()
        peers = {k: v for k, v in peers.items()
                 if isinstance(v, dict)
                 and now_wall - float(v.get("t") or 0) < PEERS_KEEP_S}
        for sysid, d in self.drones.items():
            addr, seen = d.get("addr"), d.get("seen_mono")
            if not addr or seen is None:
                continue
            # **時戳是「最後聽到這台機」，不是「寫這個檔案」。** 寫成後者的話
            # 每秒都會刷新，位址永遠不會過期——心跳行程就會對著一台早就不在的
            # 機一直發，而且 log 顯示一切正常。2026-08-31 的反向驗證抓到這個。
            peers[str(sysid)] = {"ip": addr[0], "port": addr[1],
                                 "t": now_wall - (now_mono - seen)}
        payload = json.dumps({"peers": peers}, ensure_ascii=False)
        tmp = f"{PEERS_PATH}.tmp"
        try:
            os.makedirs(os.path.dirname(PEERS_PATH), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, PEERS_PATH)
        except OSError as e:
            # **不能讓它殺掉主迴圈**：寫不出位址表只是心跳會停在舊位址，
            # 而主迴圈死掉是指令完全送不出去。節流告警，繼續跑
            if time.monotonic() - self._peers_warn_t > 30.0:
                self._peers_warn_t = time.monotonic()
                log.warning("位址表寫入失敗（%s）——心跳行程會沿用舊的一份", e)

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


def _param_read(r: MavRouter, sysid: int, name: str, timeout: float = 1.5):
    """讀一個參數的**現值與型別**。回 `PARAM_VALUE` 或 None。

    型別要讀回來不能猜：`PARAM_SET` 的 `param_value` 一律是 float，但
    `param_type` 得對——用浮點型別去寫一個整數參數，ArduPilot 存進去的
    會是**別的數字**。所以流程一定是「先讀（拿型別）→ 再寫 → 再讀（驗證）」。
    """
    enc = name.encode()[:16]
    for _ in range(3):
        r._sendto(sysid, lambda m: m.param_request_read_encode(sysid, 1, enc, -1))
        msg = r._wait(sysid, ("PARAM_VALUE",),
                      lambda x: x.param_id.strip("\x00") == name, timeout)
        if msg is not None:
            return msg
    return None


def job_get_params(r: MavRouter, sysid: int, names: list) -> dict:
    """讀一批參數。讀不到的**列在 `missing` 裡，不填 0**——「這台機沒有這個
    參數」與「這個參數是 0」是完全不同的兩件事，而 0 在這裡多半是合法值。

    **先把請求全部送出去，再一起收。** 逐個「送→等 3 秒」在 8 個參數上就是
    最壞 24 秒，已經超過 `JOB_TIMEOUT_S`（實測 2026-09-07 真的逾時了）。
    參數讀取本來就是一問一答的獨立對話，沒有順序需求。
    """
    # **param_id 必須是 bytes。** pymavlink 2.4.49 對 str 直接
    # `TypeError: must be str or None, not bytes`（2026-09-07 在容器裡實測）
    want = {n: n.encode()[:16] for n in names}
    values: dict[str, float] = {}
    other = 0                 # 期間收到的其他訊息數：用來分辨「鏈路斷了」與
                              # 「鏈路通、但參數對話被丟掉」
    t0 = time.monotonic()
    for attempt in range(3):
        pending = [n for n in names if n not in values]
        if not pending:
            break
        for n in pending:
            r._sendto(sysid, lambda m, e=want[n]: m.param_request_read_encode(
                sysid, 1, e, -1))
        # 一輪收 8 秒：**收到誰算誰**，不管順序（飛控回覆的順序不保證）。
        # 8 秒不是隨便取的：飛控與 Pi 之間是 57600 的序列埠，上面同時跑著
        # 約 27 種、每種 4Hz 的遙測（實測 ~110 msg/s，約線路容量的一半），
        # 而 ArduPilot 的參數回覆優先權低於遙測串流——它會排在後面慢慢送。
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and len(values) < len(names):
            msg = r._recv(0.3)
            if msg is None:
                continue
            other += 1
            if msg.get_type() != "PARAM_VALUE" or msg.get_srcSystem() != sysid:
                continue
            pid = msg.param_id
            pid = pid.decode() if isinstance(pid, bytes) else pid
            pid = pid.strip("\x00")
            if pid in names:
                values[pid] = float(msg.param_value)
    # **一個都沒回來、但這條 socket 明明在收東西**——那不是「這台機沒有這些
    # 參數」，而且**不是我們這條路的問題**。2026-09-07 查到底的結論寫在這裡，
    # 免得下一個人再查一次：
    #
    #   * 請求確實送到飛控：機上代理的 `fwd_to_fc` 在觸發前後差 37 則（我方
    #     送了 24 則請求＋心跳），代理兩個方向都不按型別過濾。
    #   * 飛控**答得出來**：代理自己開機時讀 FS_GCS_ENABLE／FS_GCS_TIMEOUT，
    #     **4 毫秒**就拿到答案——那一刻遙測串流還沒開始跑。
    #   * 現在讀不到，是因為**飛控↔Pi 的序列埠塞滿了**：SERIAL1 是 57600，
    #     上面跑著約 27 種、每種 4Hz 的串流（實測 98 msg/s，約線路容量六成）。
    #     ArduPilot 送 PARAM_VALUE 之前會檢查 `HAVE_PAYLOAD_SPACE`，**沒空間
    #     就安靜地丟掉，而且單筆讀取不排隊重試**。COMMAND_ACK 塞得進去（它小），
    #     PARAM_VALUE 塞不進去（25 bytes payload）——所以切模式會成功、讀參數不會。
    #
    # 機上那支 set-fc-params.py 的用法本身就是這個結論的旁證：它要求
    # **先停掉代理**再跑，那正是把串流關掉、把線路讓出來。
    if not values and other > 0:
        raise CommandError(
            f"飛控沒有回應具名參數讀取（這段期間這條鏈路收到 {other} 則其他"
            "訊息，鏈路與轉發都是通的）。已排除：轉發（機上代理逐則記下轉送到"
            "飛控的請求，名字與 target 都對）、頻寬（代理 v0.18.2 起在交換參數"
            "期間讓路，實測 98→8 msg/s，讓乾淨了一樣沒有回應）。"
            "**目前最可能的是 MAVLink 版本**：飛控的 SERIAL1_PROTOCOL=1（v1），"
            "而本服務送 v2；v2 會截掉尾端零位元組，而 param_id 尾巴正好是 NUL。"
            "驗證方式是把 SERIAL1_PROTOCOL 改成 2，或讓本服務改送 v1")
    return {"values": values, "elapsed_s": round(time.monotonic() - t0, 1),
            "missing": [n for n in names if n not in values]}


#: 一次最多問幾個點。`TERRAIN_REPORT` 是 43 bytes 的 payload，跟 `PARAM_VALUE`
#: 一樣得跟 27 種 4Hz 的遙測搶那條 57600 的序列埠——問太多點只會逾時。
#: 8 個點 × 2.5 秒 ＝ 最壞 20 秒，留得下 `JOB_TIMEOUT_S` 的餘裕。
TERRAIN_PROBE_MAX = 8
TERRAIN_PROBE_TIMEOUT_S = 2.5
#: 回覆的座標離問的點多遠還算「這是我問的那一點」。飛控會把查詢吸附到自己的
#: 格點（`TERRAIN_SPACING`，實測 100 m），所以容忍度要比格距大；200 m 夠寬，
#: 又足以擋掉不請自來的那種（實測是 `0,0`——離現場 12541 km）。
TERRAIN_MATCH_M = 200.0
#: 送第一個查詢之前先清掉緩衝區裡的舊報告，最多花這麼久。
TERRAIN_DRAIN_S = 0.4


#: 換算住 `libs/plan_check`（Python 這一側唯一一處，field-3d-model-design §9-G）
from plan_check import dist_m as _ll_dist_m  # noqa: E402


def job_log_list(r: MavRouter, sysid: int, timeout: float = 12.0) -> dict:
    """問飛控 SD 卡上有哪些 dataflash 紀錄（`LOG_REQUEST_LIST` → `LOG_ENTRY`）。

    **只問清單，不下載。** 清單是決定「值不值得走 MAVLink 這條線」的依據：
    這條 FC↔Pi 的序列埠是 57600，扣掉遙測之後留給檔案傳輸的頻寬很窄，
    幾 MB 的紀錄用聊天的速度傳會是幾十分鐘起跳。先看大小再決定要不要傳，
    比傳到一半才發現不划算好。
    """
    r._sendto(sysid, lambda m: m.log_request_list_encode(sysid, 1, 0, 0xFFFF))
    entries: dict[int, dict] = {}
    total = None
    deadline = time.monotonic() + timeout
    other = 0
    while time.monotonic() < deadline:
        msg = r._recv(0.3)
        if msg is None:
            continue
        other += 1
        if msg.get_type() != "LOG_ENTRY" or msg.get_srcSystem() != sysid:
            continue
        total = int(msg.num_logs)
        if total == 0:
            break
        entries[int(msg.id)] = {
            "id": int(msg.id), "size": int(msg.size),
            # `time_utc` 是 0 的話代表**飛控當時不知道時間**（沒有 GPS 定位），
            # 不是 1970 年的紀錄。照實回 None，不要換算出一個假日期
            "time_utc": int(msg.time_utc) or None,
        }
        if len(entries) >= total:
            break
    if total is None:
        raise CommandError(
            f"飛控沒有回應紀錄清單（期間收到 {other} 則其他訊息）。"
            "可能是 LOG_BITMASK 關著、SD 卡沒插，或回應被塞滿的序列埠丟掉了")
    return {"num_logs": total, "listed": len(entries),
            "logs": sorted(entries.values(), key=lambda x: -x["id"])}


#: 一次抓多少位元組。**不是越大越好**：這條 57600 的序列埠上，一塊 64 KB
#: 已經要十幾秒，而工作跑在 router 的單一執行緒上——塊太大就等於在那段時間
#: **整台機指揮不動**。分塊讓其他指令插得進來，代價只是多幾次來回。
LOG_CHUNK_B = 65536
LOG_PKT_B = 90                    # ArduPilot 的 LOG_DATA 一則固定 90 bytes


def job_log_fetch(r: MavRouter, sysid: int, log_id: int, ofs: int,
                  want: int = LOG_CHUNK_B, path: str = "",
                  timeout: float = 20.0) -> dict:
    """抓一塊 dataflash（`LOG_REQUEST_DATA` → `LOG_DATA`）。

    **只抓一塊就回。** 一份 1.8 MB 的紀錄在這條線上要六分鐘，而工作是跑在
    router 那條唯一的執行緒上的——整段抓完等於那六分鐘裡解鎖、切模式、
    緊急降落全部排在後面。分塊之後其他指令插得進來。

    **缺塊要說出來，不要靜靜地補零。** UDP 會掉，掉的那 90 bytes 如果用 0
    填起來，`.bin` 解析出來會是一筆看起來很正常的假資料。這裡回報實際收到的
    範圍，補洞交給呼叫端再要一次。

    **位元組由這裡直接落盤，不經回傳值。** `_run` 會把工作的結果整份
    `json.dumps` 進 `command_log`——幾十 KB 的二進位走那條路會塞爆留痕，
    而留痕的用途是「誰在什麼時候下了什麼指令」，不是存檔案。
    """
    got: dict[int, bytes] = {}
    end = ofs + want
    for attempt in range(3):
        missing = [o for o in range(ofs, end, LOG_PKT_B) if o not in got]
        if not missing:
            break
        # 一次要一段連續的；ArduPilot 收到 LOG_REQUEST_DATA 會自己連續送
        lo = missing[0]
        r._sendto(sysid, lambda m, a=lo: m.log_request_data_encode(
            sysid, 1, log_id, a, end - a))
        deadline = time.monotonic() + timeout / 3
        while time.monotonic() < deadline:
            msg = r._recv(0.3)
            if msg is None:
                continue
            if msg.get_type() != "LOG_DATA" or msg.get_srcSystem() != sysid:
                continue
            if int(msg.id) != log_id:
                continue
            o, n = int(msg.ofs), int(msg.count)
            if n:
                got[o] = bytes(bytearray(msg.data)[:n])
            if o + n >= end or n < LOG_PKT_B:
                break                     # 到尾了（最後一塊會短）
    if not got:
        raise CommandError(
            f"飛控沒有回應紀錄 {log_id} 的資料（位移 {ofs}）。"
            "紀錄編號對不對？SD 卡還在嗎？")
    # 從 ofs 開始能連得起來多少，就回多少——**中間有洞就停在洞前面**，
    # 呼叫端從回報的 next 繼續要，不會把洞跳過去
    out = bytearray()
    o = ofs
    while o in got:
        out += got[o]
        o += len(got[o])
    if path:
        with open(path, "ab") as f:
            f.write(bytes(out))
    return {"ofs": ofs, "bytes": len(out), "next": o,
            "holes": len(got) - (len(out) + LOG_PKT_B - 1) // LOG_PKT_B}


def job_terrain_check(r: MavRouter, sysid: int, points: list) -> dict:
    """問飛控「你認為這幾個點的地面多高」（issues/047 §2 的核對那一半）。

    `points`：`[(lat, lon, 標籤), ...]`。逐點送 `TERRAIN_CHECK`，收
    `TERRAIN_REPORT`。回每一點的 `terrain_height`（飛控認為的地面 AMSL）、
    `pending`（它還缺幾格）、`loaded`（已載入幾格）、`spacing`（它的格距）。

    **一次只有一個未回覆的請求，而且回覆的座標要對得上。**

    先前只做了前半，2026-09-08 實機打臉：飛控**自己會送不請自來的
    `TERRAIN_REPORT`**（室內沒有 GPS 時內容是 `0,0`）。那一則卡在緩衝區裡，
    被當成第一個查詢的答案，於是**整串答案錯開一格**——每個航點拿到的是
    前一個航點的地面高度。五個點全部「有答案」、數字也都很合理，
    **看不出哪裡不對**，這正是它危險的地方。

    所以現在兩道都做：送第一個查詢之前先清一次緩衝區；每一則回覆都要
    離問的那一點 `TERRAIN_MATCH_M` 以內才收（吸附到格點會差幾十公尺，
    不請自來的那種差幾千公里）。收不到就是收不到，**不拿隔壁的答案頂替**。

    **`pending > 0` 是一個獨立的結論，不是雜訊**：那代表飛控自己也沒有那塊
    地形資料。ArduPilot 的地形圖磚來自 SD 卡或**會供圖的地面站**（Mission
    Planner／MAVProxy），而本系統不供圖——所以缺的那塊不會自己補上，
    那一段用地形跟隨飛就是在等失效返航。
    """
    out: list[dict] = []
    seen_any = 0
    stray = 0                 # 收到但配不上任何問題的報告：證據要留著
    # 開場先清緩衝區裡的舊報告（只丟 TERRAIN_REPORT，其他照舊留在佇列裡
    # 由後面的迴圈跑掉）。這條 socket 上一直有遙測在流，所以是**限時**清，
    # 不是「清到空為止」——後者在一條每秒 100 則的鏈路上不會結束
    drain_until = time.monotonic() + TERRAIN_DRAIN_S
    while time.monotonic() < drain_until:
        m0 = r._recv(0.05)
        if m0 is None:
            continue
        # 清場讀到的也算進 `seen_any`——那是「鏈路還活著」的證據，
        # 不能因為清場先讀走就消失（否則會誤判成「鏈路斷了」）
        seen_any += 1
        if m0.get_type() == "TERRAIN_REPORT":
            stray += 1
    for lat, lon, label in points[:TERRAIN_PROBE_MAX]:
        r._sendto(sysid, lambda m, a=lat, o=lon: m.terrain_check_encode(
            int(round(a * 1e7)), int(round(o * 1e7))))
        rec = {"label": label, "lat": lat, "lon": lon}
        deadline = time.monotonic() + TERRAIN_PROBE_TIMEOUT_S
        while time.monotonic() < deadline:
            msg = r._recv(0.3)
            if msg is None:
                continue
            seen_any += 1
            if msg.get_type() != "TERRAIN_REPORT" or msg.get_srcSystem() != sysid:
                continue
            off = _ll_dist_m(lat, lon, msg.lat / 1e7, msg.lon / 1e7)
            if off > TERRAIN_MATCH_M:
                stray += 1        # 不是這一點的答案——丟掉，繼續等
                continue
            rec.update({
                "terrain_height_m": float(msg.terrain_height),
                "current_height_m": float(msg.current_height),
                "spacing_m": int(msg.spacing),
                "pending": int(msg.pending), "loaded": int(msg.loaded),
                # 飛控回的座標**照實記下來**：它跟我方問的差多少，就是
                # 「這個高度其實是哪一點的」——差一格就是差 100 m
                "reported_lat": msg.lat / 1e7, "reported_lon": msg.lon / 1e7,
            })
            break
        out.append(rec)
    answered = [x for x in out if "terrain_height_m" in x]
    if not answered and seen_any:
        raise CommandError(
            f"飛控沒有回應地形查詢（這段期間收到 {seen_any} 則其他訊息，"
            "鏈路是通的）。兩種可能：TERRAIN_ENABLE 是 0（它根本不做地形），"
            "或 TERRAIN_REPORT 跟 PARAM_VALUE 一樣被塞滿的序列埠丟掉了")
    return {"points": out, "answered": len(answered), "asked": len(out),
            # **配不上的報告要說出來**：它是「答案錯開一格」那個 bug 唯一的
            # 外顯訊號，數字看起來全都很合理的時候就只剩它了
            "stray": stray}


def job_set_params(r: MavRouter, sysid: int, items: dict) -> dict:
    """寫一批參數，**每一個都讀回來比對**。

    比對不過就 `CommandError`——與任務上傳同一條紀律：**沒有讀回確認就不算
    寫成功**。飛控對 `PARAM_SET` 不回 ACK，它回的是一則 `PARAM_VALUE`；
    那則可能因為丟包而收不到，也可能因為值被飛控自己夾過而與送出的不同
    （ArduPilot 會夾，而且不會告訴你）。兩種情況都必須讓操作員看見。

    **逐個寫、逐個驗**，不批次：一批裡有一個沒過時，說得出是哪一個。
    """
    written, clamped = {}, []
    for name, want in items.items():
        cur = _param_read(r, sysid, name)
        if cur is None:
            raise CommandError(f"讀不到參數 {name}——這台機可能沒有這個參數")
        ptype = cur.param_type
        enc = name.encode()[:16]
        got = None
        for _ in range(3):
            r._sendto(sysid, lambda m, e=enc, w=want, t=ptype:
                      m.param_set_encode(sysid, 1, e, float(w), t))
            msg = r._wait(sysid, ("PARAM_VALUE",),
                          lambda x, n=name: x.param_id.strip("\x00") == n, 3.0)
            if msg is not None:
                got = float(msg.param_value)
                break
        if got is None:
            # 再主動讀一次：PARAM_VALUE 的回覆可能在路上掉了，而參數其實寫進去了
            re = _param_read(r, sysid, name)
            if re is None:
                raise CommandError(f"{name} 寫出去了，但讀不回來——現在的值不明")
            got = float(re.param_value)
        if abs(got - float(want)) > 1e-4:
            # **飛控把值夾掉了**。不是失敗（它確實接受了一個值），但也不是
            # 成功（那不是你要的值）——照實回報，讓操作員自己判斷
            clamped.append(f"{name}：送出 {want:g}，飛控存成 {got:g}")
        written[name] = got
    return {"written": written, "clamped": clamped,
            "verified": not clamped, "accepted": True}


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
# **起飛拆成三個可組合的動作**，而不是一顆 job。
#
# 理由是編隊：群飛的序列是「全體 arm → 全體 takeoff → 全體等 → 全體切 MISSION」，
# 拆步驟才能讓 N 台幾乎同時離地。原本 `job_takeoff` 把「切 GUIDED＋arm＋起飛」
# 綁成一顆，群飛用不了，於是 `group_exec` **自己重寫了一份起飛**——而它重寫的是
# PX4 的語意（param7 用絕對海拔、空白參數用 NaN），對 ArduPilot 三條方言全錯。
#
# **順序（群飛邏輯）由呼叫端決定，動作內容（方言）由驅動決定。** 這一層只負責
# 後者，而且**只有這一份**：三個動作全部向 `driver.takeoff_plan()` 要參數，不再
# 自己從旗標推。原本 `takeoff_plan()` 在驅動裡定義了卻沒有任何產品呼叫者，只有
# 等價測試在跑它——**一個沒有人用的抽象不會讓兩條路徑一致**（issues/026 B4-d 的
# 同一課：等價測試證明不了兩邊吃的是同樣的輸入）。


def _takeoff_plan(r: MavRouter, sysid: int, alt: float, ground_amsl) -> dict:
    """向驅動要起飛參數。驅動說不行（PX4 缺地面海拔）就轉成 CommandError。"""
    try:
        return dialect(r, sysid)["driver"].takeoff_plan(alt, ground_amsl)
    except ValueError as e:
        raise CommandError(str(e))


def airborne_of(r: MavRouter, sysid: int) -> tuple[bool | None, str, float | None]:
    """這台機離地了沒 →（判定, 依據, alt_rel）。判定 None＝**還不知道**。

    **先看 `landed_state`，拿不到才退回高度。** `alt_rel` 在沒有 GPS 定位時是
    漂的——前端量過停在地面的機漂到 4.4 m（`CommandPanel.tsx` 那條註解）。拿一個
    會漂的數字去證明「機真的離地了」，門檻訂多低都證明不了，訂多高又只是把同一個
    漂移往上推。`EXTENDED_SYS_STATE` 是機端自己說的。

    **退回時要說得出退回了**：操作員必須分得出「機端說它在空中」與「機端沒說，
    我在拿高度猜」——後者才是 2026-08-11 那條教訓（地面直接切 AUTO 會失敗）的
    風險面。回傳的依據字串就是給留痕與錯誤訊息用的。

    **單機（`mission_fly`）與群飛（`group_exec`）共用這一份。** 這件事本身不是
    方言——`landed_state` 兩家都送，所以它在這裡而不是在驅動裡。
    """
    d = (r.drones.get(sysid) or {})
    alt = d.get("alt_rel")
    ls, lt = d.get("landed_state"), d.get("landed_t")
    if ls is not None and lt is not None and time.monotonic() - lt <= LANDED_STALE_S:
        return ls == "in_air", f"機端 landed_state={ls}", alt
    why = "機端沒送 landed_state" if ls is None else "機端的 landed_state 已過期"
    return None, why, alt


def job_arm_prep(r: MavRouter, sysid: int) -> dict:
    """arm 之前的方言前置。ArduPilot Copter 在 LOITER/STABILIZE 下 arm 了也不會
    照指令起飛，必須先進 GUIDED；PX4 不需要，回 `{"needed": False}`。

    **不需要時不送任何東西**——對不需要的廠牌多切一次模式是自己製造狀態變化。
    """
    if not dialect(r, sysid)["takeoff_needs_guided"]:
        return {"needed": False}
    return {"needed": True, "guided": job_set_mode(r, sysid, "guided")}


def job_takeoff_cmd(r: MavRouter, sysid: int, alt: float, ground_amsl=None) -> dict:
    """只送 NAV_TAKEOFF（不切模式、不 arm）。參數全部由驅動的 `takeoff_plan()` 給：

    - param7 是**相對高度還是絕對海拔**（送錯會差一整個地面海拔、數百公尺）
    - 空白參數用 **NaN 還是 0.0**（實測 2026-08-12：ArduPilot 對 NaN 的
      NAV_TAKEOFF 連 ACK 都不回，指令被靜默丟棄）
    """
    p = _takeoff_plan(r, sysid, alt, ground_amsl)
    b, p7 = p["blank"], p["param7"]
    res = job_command(r, sysid, M.MAV_CMD_NAV_TAKEOFF, [0.0, 0.0, 0.0, b, b, b, p7])
    # **`accepted`／`verified` 攤在頂層**：呼叫端（群飛的 `_submit_audited`）
    # 是靠這兩個鍵判「這台到底有沒有被接受」的。包進子物件的話，被拒的起飛
    # 會被記成 accepted——留痕說謊比沒留痕更糟
    return {**res, "alt_param7": p7, "alt_semantics": p["alt_semantics"]}


def job_takeoff(r: MavRouter, sysid: int, alt: float, ground_amsl=None) -> dict:
    """單機起飛序列＝前置 → arm → 起飛。三步都走上面那三個動作，**方言不在這裡**。

    回傳逐步結果，任一步失敗就往上拋（呼叫端已有留痕與錯誤呈現）。
    """
    steps = {}
    prep = job_arm_prep(r, sysid)
    if prep.get("needed"):
        steps["guided"] = prep["guided"]
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
    t = job_takeoff_cmd(r, sysid, alt, ground_amsl)
    steps["takeoff"] = t
    return {"steps": steps, "alt_param7": t["alt_param7"],
            "alt_semantics": t["alt_semantics"]}


def job_clear_mission(r: MavRouter, sysid: int) -> dict:
    """清掉機上那份任務（`MISSION_CLEAR_ALL`）＋ 讀回確認真的變成 0 項。

    **為什麼要有這個**：原本要換掉一份任務只能「上傳另一份蓋過去」，而那是
    一個比清除**更重**的動作——它要跑完整的握手、逐項送、逐項讀回比對。
    人真正想做的是「把它清掉」，卻被迫做一件更複雜的事。

    **讀回確認不能省。** MISSION_ACK 是「我收到了」不是「我清乾淨了」——
    這條規矩在本專案已經踩過一次（換 sysid 那次）。所以清完再問一次
    `MISSION_REQUEST_LIST`，看機端回報的 count 是不是 0。
    """
    mt = M.MAV_MISSION_TYPE_MISSION
    r._sendto(sysid, lambda m: m.mission_clear_all_encode(sysid, 1, mt))
    ack = r._wait(sysid, ("MISSION_ACK",), timeout=5.0)
    if ack is None:
        raise CommandError("清除任務沒有收到 ACK")
    if ack.type != M.MAV_MISSION_ACCEPTED:
        raise CommandError(f"機端拒絕清除任務（MISSION_ACK type={ack.type}）")
    # ── 讀回：機上真的沒有任務了嗎 ──────────────────────────
    r._sendto(sysid, lambda m: m.mission_request_list_encode(sysid, 1, mt))
    cnt = r._wait(sysid, ("MISSION_COUNT",), timeout=5.0)
    if cnt is None:
        # 清除本身成功了，但我們沒讀回。**說出來**，不要當成完全成功
        return {"accepted": True, "verified": False,
                "note": "清除已被接受，但讀不回機端的任務數——請自行確認"}
    left = int(cnt.count)
    return {"accepted": True, "verified": left == 0, "remaining": left,
            "note": "機上已無任務" if left == 0 else f"機上還有 {left} 項"}


def fence_wire_items(items: list[dict]) -> list[dict]:
    """`plan_check.fc_fence_plan` 的圍欄項 → MISSION_ITEM_INT 的欄位。"""
    return [{"seq": i, "frame": M.MAV_FRAME_GLOBAL, "command": int(it["command"]),
             "p1": float(it["p1"]), "p2": 0.0, "p3": 0.0, "p4": 0.0,
             "x": int(round(it["lat"] * 1e7)), "y": int(round(it["lon"] * 1e7)),
             "z": 0.0} for i, it in enumerate(items)]


def job_upload_mission(r: MavRouter, sysid: int, items: list,
                       mission_type: int | None = None) -> dict:
    """完整上傳握手 → 機端 ACK → 回讀逐項比對 → 收 PX4 廣播的驗證訊息。

    丟包韌性（對齊實戰工具 upload_mission.py v3，戶外 5G 實測經驗）：
    - MISSION_COUNT 每 2 秒重送直到機端開始請求（握手能不能開始的關鍵）
    - 項目遺失由機端重複請求同 seq 自然補（協定內建），總期限 30 秒
    - 回讀的 REQUEST_LIST 重試 3 次、逐項重試 2 次
    """
    mt = M.MAV_MISSION_TYPE_MISSION if mission_type is None else mission_type
    fence = mt == M.MAV_MISSION_TYPE_FENCE
    d = dialect(r, sysid)
    # ArduPilot：seq 0 留給 home，真航點往後移一格（line[i] 是要送給機上的第 i 項）。
    # 佔位用第一個航點的座標而不是 0,0,0——實測 ArduPilot 會用實際 home 覆蓋它，
    # 但萬一某版本沒覆蓋，一個「任務起點附近」的 home 遠比 (0,0,0) 安全。
    # **圍欄任務沒有這一格**：home 佔位只屬於航線任務
    home_slot = d["home_at_seq0"] and not fence
    if home_slot and items:
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
    skip = 1 if home_slot else 0
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
        # 圍欄項的 param1 是頂點數或圓的半徑——**半徑不對，圈就不是那個圈**
        if fence and abs(it.param1 - o["p1"]) > 0.05:
            raise CommandError(
                f"回讀比對不符（圍欄第 {seq} 項）：param1 機上 {it.param1:g}，"
                f"上傳的是 {o['p1']:g}")
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
