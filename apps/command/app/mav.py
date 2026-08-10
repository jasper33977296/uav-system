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
import queue
import threading
import time

from pymavlink import mavutil

M = mavutil.mavlink
GCS_SYSID = 254

# PX4 custom mode（DO_SET_MODE 的 param2/param3：main_mode/sub_mode）
PX4_MODES = {
    "mission": (4, 4),   # AUTO.MISSION
    "hold":    (4, 3),   # AUTO.LOITER
    "rtl":     (4, 5),   # AUTO.RTL
    "land":    (4, 6),   # AUTO.LAND
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

    # ── API 層入口（任意執行緒呼叫；在 executor 裡跑，不阻塞事件迴圈）──
    def submit(self, fn, *args, timeout: float = 30.0):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.jobs.put((fn, args, fut))
        return fut.result(timeout=timeout)

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {
            str(sysid): {
                "age_s": round(now - d.get("seen_mono", now), 1),
                "armed": d.get("armed"),
                "custom_mode": d.get("custom_mode"),
            }
            for sysid, d in self.drones.items()
        }

    # ── 主迴圈（唯一碰 socket 的執行緒）──────────────────────────
    def run(self):
        while True:
            self._tick()
            try:
                fn, args, fut = self.jobs.get_nowait()
            except queue.Empty:
                self._recv(0.2)
                continue
            try:
                fut.set_result(fn(self, *args))
            except Exception as e:            # 失敗必須浮上去，不得靜默
                fut.set_exception(e)

    def _tick(self):
        now = time.monotonic()
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
                d = self.drones.setdefault(sysid, {})
            if d is not None:
                d["addr"] = self.conn.last_address   # 單埠多機的回程路由表
                d["seen_mono"] = time.monotonic()
                if msg.get_type() == "HEARTBEAT" and msg.type != M.MAV_TYPE_GCS:
                    d["armed"] = bool(msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED)
                    d["custom_mode"] = msg.custom_mode
        return msg

    def _sendto(self, sysid: int, encode_fn):
        """encode + 直接 sendto 該 sysid 的來源位址（不經 mavutil 的廣播式 write）。"""
        d = self.drones.get(sysid)
        if not d or "addr" not in d:
            raise CommandError(f"sysid {sysid} 未連線（心跳未見）")
        msg = encode_fn(self.conn.mav)
        buf = msg.pack(self.conn.mav)
        self.conn.port.sendto(buf, d["addr"])
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

def job_command(r: MavRouter, sysid: int, command: int, params: list,
                retries: int = 3, ack_timeout: float = 2.0) -> dict:
    """COMMAND_LONG → 等 ACK → 重送。無 ACK 一律例外，不得視為成功。"""
    p = (list(params) + [0.0] * 7)[:7]
    for attempt in range(1, retries + 1):
        r._sendto(sysid, lambda m: m.command_long_encode(sysid, 1, command, 0, *p))
        ack = r._wait(sysid, ("COMMAND_ACK",),
                      lambda msg: msg.command == command, ack_timeout)
        if ack is not None:
            name = M.enums["MAV_RESULT"][ack.result].name
            return {"result": name,
                    "accepted": ack.result == M.MAV_RESULT_ACCEPTED,
                    "attempts": attempt}
    raise CommandError(f"指令 {command} 無 ACK（重試 {retries} 次）")


def job_set_mode(r: MavRouter, sysid: int, mode: str) -> dict:
    main, sub = PX4_MODES[mode]
    return job_command(r, sysid, M.MAV_CMD_DO_SET_MODE,
                       [M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, main, sub])


def job_upload_mission(r: MavRouter, sysid: int, items: list) -> dict:
    """完整上傳握手 → 機端 ACK → 回讀下載逐項比對。比對不符即失敗。"""
    n = len(items)
    mt = M.MAV_MISSION_TYPE_MISSION
    r._sendto(sysid, lambda m: m.mission_count_encode(sysid, 1, n, mt))
    ack = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        msg = r._wait(sysid, ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"),
                      timeout=3.0)
        if msg is None:
            raise CommandError("任務上傳逾時（機端停止請求項目）")
        if msg.get_type() == "MISSION_ACK":
            ack = msg
            break
        it = items[msg.seq]
        r._sendto(sysid, lambda m, it=it: m.mission_item_int_encode(
            sysid, 1, it["seq"], it["frame"], it["command"], 0, 1,
            it["p1"], it["p2"], it["p3"], it["p4"], it["x"], it["y"], it["z"], mt))
    if ack is None:
        raise CommandError("任務上傳逾時（未收到 MISSION_ACK）")
    if ack.type != M.MAV_MISSION_ACCEPTED:
        raise CommandError(
            f"機端拒絕任務：{M.enums['MAV_MISSION_RESULT'][ack.type].name}")

    # 回讀比對：上傳成功的定義是「機上任務與我們要上傳的一致」，不是收到 ACK
    r._sendto(sysid, lambda m: m.mission_request_list_encode(sysid, 1, mt))
    cnt = r._wait(sysid, ("MISSION_COUNT",), timeout=3.0)
    if cnt is None or cnt.count != n:
        raise CommandError(f"回讀筆數不符：機上 {getattr(cnt, 'count', '無回應')}，預期 {n}")
    for seq in range(n):
        r._sendto(sysid, lambda m, s=seq: m.mission_request_int_encode(sysid, 1, s, mt))
        it = r._wait(sysid, ("MISSION_ITEM_INT",), lambda msg, s=seq: msg.seq == s, 3.0)
        if it is None:
            raise CommandError(f"回讀第 {seq} 項逾時")
        o = items[seq]
        if (it.command != o["command"] or abs(it.x - o["x"]) > 2
                or abs(it.y - o["y"]) > 2 or abs(it.z - o["z"]) > 0.5):
            raise CommandError(f"回讀比對不符（seq {seq}）：機上內容與上傳不一致")
    r._sendto(sysid, lambda m: m.mission_ack_encode(sysid, 1, M.MAV_MISSION_ACCEPTED, mt))
    return {"uploaded": n, "verified": True}
