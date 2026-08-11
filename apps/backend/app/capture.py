"""原始層錄製器：MAVLink 框架無損落盤（tlog 格式）。

「任何無人機傳出的資訊都要收集到」的字面實作（issues/014）——不逐種
訊息挑選，先無損保留；結構層要什麼，從這裡重放即可。

歷史註：本模組原本是「tee」（錄了轉發給 mavsdk）；2026-08-10 路線 B
之後 mavsdk 退役，接收與解析由 mavlink_rx.py 負責，本模組只剩錄製器，
由 mavlink_rx 在每個 datagram 落盤時呼叫。

- 格式：tlog（每則訊息前綴 8-byte big-endian μs 時間戳），
  與 QGC 回放、pymavlink（mavlogdump.py）工具鏈相容。
- 檔案依 UTC 日切檔，CAPTURE_KEEP_DAYS 滾動清理。
  實測待機流量 ~16.5 KB/s（61 MB/hr）。
- 錄製失敗絕不拖垮資料路徑（呼叫端負責 try/except）。
"""
import logging
import os
import struct
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def split_frames(buf: bytes) -> list[bytes]:
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


class Recorder:
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
        if self.f is None:          # 已 close（關機瞬間仍有封包進來）——靜默略過，不噴 traceback
            return
        ts = struct.pack(">Q", int(now * 1e6))
        for fr in split_frames(data):
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
