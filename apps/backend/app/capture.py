"""原始層錄製：MAVLink 每一個框架先落盤，再轉發給 ingest（透明 tee）。

「任何無人機傳出的資訊都要收集到」的字面實作——不逐種訊息挑選解析，
先無損保留；結構層之後要什麼，從這裡重放即可（設計見
doc/gcs-replacement.md「兩層收集」、issues/014）。

- backend 對外綁 MAVLINK_URL 的埠（14540），mavsdk 改聽內部埠；
  機→地：錄製＋轉發；地→機（mavsdk 心跳/唯讀請求）：只轉發不錄。
- 格式：tlog（每則訊息前綴 8-byte big-endian μs 時間戳），
  與 QGC 回放、pymavlink（mavlogdump.py）工具鏈相容。
- 檔案依 UTC 日切檔，CAPTURE_KEEP_DAYS 滾動清理。
  實測待機流量 ~16.5 KB/s（61 MB/hr），與 30 天 retention 同量級。
- 錄製失敗絕不拖垮資料路徑（寫檔例外只記 log，轉發照走）。
"""
import asyncio
import logging
import os
import struct
import time
from datetime import datetime, timedelta, timezone

from .config import settings

log = logging.getLogger(__name__)


def _split_frames(buf: bytes) -> list[bytes]:
    """datagram → MAVLink 框架列表。結構性切分、不驗 CRC——錄製是無損保留，
    髒資料照樣落盤；切不動的殘段整塊保留（tlog 讀取端自己會重新同步）。"""
    frames = []
    i, n = 0, len(buf)
    while i < n:
        m = buf[i]
        if m == 0xFD and i + 12 <= n:      # v2: 10 header + payload + 2 crc [+13 sig]
            end = i + 12 + buf[i + 1] + (13 if buf[i + 2] & 1 else 0)
        elif m == 0xFE and i + 8 <= n:     # v1: 6 header + payload + 2 crc
            end = i + 8 + buf[i + 1]
        else:
            end = n
        frames.append(buf[i:min(end, n)])
        i = end
    return frames


class _Recorder:
    def __init__(self, directory: str, keep_days: int):
        self.dir = directory
        self.keep_days = keep_days
        self.f = None
        self.day = None
        self._last_flush = 0.0
        os.makedirs(directory, exist_ok=True)

    def write(self, data: bytes) -> None:
        now = time.time()
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
        if day != self.day:
            self._rotate(day)
        ts = struct.pack(">Q", int(now * 1e6))
        for fr in _split_frames(data):
            self.f.write(ts + fr)
        if now - self._last_flush > 1.0:
            self.f.flush()
            self._last_flush = now

    def _rotate(self, day: str) -> None:
        if self.f:
            self.f.close()
        self.day = day
        path = os.path.join(self.dir, day + ".tlog")
        self.f = open(path, "ab")
        log.info("raw capture → %s", path)
        self._prune()

    def _prune(self) -> None:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=self.keep_days)).strftime("%Y%m%d")
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".tlog") and name[:8] < cutoff:
                os.remove(os.path.join(self.dir, name))
                log.info("raw capture 清理過期檔 %s（保留 %d 天）", name, self.keep_days)

    def close(self) -> None:
        if self.f:
            self.f.close()
            self.f = None


class _ExtProto(asyncio.DatagramProtocol):
    """對外（機→地）：錄製＋轉發給內部 mavsdk。"""
    def __init__(self, tee: "Tee"):
        self.tee = tee

    def connection_made(self, transport):
        self.tee.ext = transport

    def datagram_received(self, data, addr):
        t = self.tee
        t.drone_addr = addr
        try:
            t.rec.write(data)
        except Exception:
            log.exception("capture 寫檔失敗（資料路徑不受影響）")
        if t.int_t:
            t.int_t.sendto(data, t.int_target)


class _IntProto(asyncio.DatagramProtocol):
    """對內（地→機，mavsdk 心跳/唯讀請求）：原路轉回，不錄。"""
    def __init__(self, tee: "Tee"):
        self.tee = tee

    def connection_made(self, transport):
        self.tee.int_t = transport

    def datagram_received(self, data, addr):
        t = self.tee
        if t.ext and t.drone_addr:
            t.ext.sendto(data, t.drone_addr)


class Tee:
    def __init__(self):
        self.rec = _Recorder(settings.capture_dir, settings.capture_keep_days)
        self.ext = None
        self.int_t = None
        self.drone_addr = None
        self.int_target = ("127.0.0.1", settings.capture_internal_port)

    def close(self) -> None:
        for tr in (self.ext, self.int_t):
            if tr:
                tr.close()
        self.rec.close()


async def start() -> Tee:
    """綁定 MAVLINK_URL 的埠開始錄製；ingest 應改連內部埠。"""
    u = settings.mavlink_url.replace("://", ":").split(":")
    host, port = u[-2], int(u[-1])
    tee = Tee()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: _ExtProto(tee), local_addr=(host, port))
    await loop.create_datagram_endpoint(lambda: _IntProto(tee), local_addr=("127.0.0.1", 0))
    log.info("raw capture：udp %s:%d →（錄製）→ 127.0.0.1:%d，目錄 %s",
             host, port, settings.capture_internal_port, settings.capture_dir)
    return tee
