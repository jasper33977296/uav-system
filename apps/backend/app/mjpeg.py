"""H.264 → MJPEG：給 `<img>` 直接接的即時畫面（issue 022，2026-09-23 使用者裁定）。

做法照 `/home/k200/temp` 那支 UE agent 的鏡頭模組：一路來源多人共用、
沒人看就關掉、人數有上限。**但轉碼放在地面站，不是機上**——那支的鏡頭就在
本機，MJPEG 只走區網；我們的鏡頭在無人機上，而 720p30 的 MJPEG 約
**10–20 Mbps**，會把這套系統要量測的那條 5G 上行吃垮。所以：

    機上 ── H.264 2.5 Mbps（5G 上行，不變）──→ 地面站 ── MJPEG ──→ 看的人

為什麼要有這條路：對外原本走 HLS，實測**落後 3.5 秒**（切片與緩衝造成的，
不是位元率，所以降畫質沒用）。MJPEG 沒有 GOP、沒有切片，一張就是一張。

**沒人看就收，這不只是省 CPU**：收掉之後地面站對機上的拉流也會跟著停
（`sourceOnDemand`），上行就真的不再有影像——而那條上行正在被量測。
"""
import asyncio
import contextlib
import logging
import time

import video_stream                  # libs/ 的共用實作（PYTHONPATH=/srv/libs）

from .config import settings

log = logging.getLogger(__name__)

#: 地面站自己的 RTSP（MediaMTX）。轉碼是從**已經拉進來的那一份**再轉，
#: 不是自己另外去機上拉——後者會變成機上的第二個讀者，上行直接變兩倍。
SRC = "rtsp://127.0.0.1:8554/{path}"
SOI, EOI = b"\xff\xd8", b"\xff\xd9"      # JPEG 的開頭與結尾標記
READ_CHUNK = 65536


class Transcoder:
    """一台機一個。多少人看都只跑一個 ffmpeg。"""

    def __init__(self, drone_id: str) -> None:
        self.drone_id = drone_id
        self.path = video_stream.path_for(drone_id)
        self.frame: bytes | None = None
        self.seq = 0
        self.frame_at = 0.0
        self.viewers = 0
        self.demand_at = time.monotonic()
        self.last_error: str | None = None
        self.frames = 0
        self.started_at: float | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    # ── 生命週期 ──────────────────────────────────────────────────────
    def touch(self) -> None:
        self.demand_at = time.monotonic()

    def add_viewer(self) -> bool:
        if self.viewers >= settings.mjpeg_max_viewers:
            return False
        self.viewers += 1
        self.touch()
        return True

    def remove_viewer(self) -> None:
        self.viewers = max(0, self.viewers - 1)
        self.touch()

    async def ensure(self) -> None:
        self.touch()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"mjpeg:{self.path}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    def status(self) -> dict:
        age = round(time.monotonic() - self.frame_at, 2) if self.frame_at else None
        return {
            "drone_id": self.drone_id, "path": self.path,
            "streaming": self._task is not None and not self._task.done(),
            "viewers": self.viewers, "max_viewers": settings.mjpeg_max_viewers,
            "frames": self.frames,
            # **秒數而不是布林**：「有畫面」與「畫面是 12 秒前的」不一樣
            "last_frame_age_s": age,
            "last_error": self.last_error,
            "config": {"size": settings.mjpeg_size, "fps": settings.mjpeg_fps,
                       "quality": settings.mjpeg_quality,
                       "idle_timeout_s": settings.mjpeg_idle_s},
        }

    # ── 轉碼 ──────────────────────────────────────────────────────────
    def _args(self) -> list[str]:
        a = ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-fflags", "nobuffer", "-flags", "low_delay",
             "-rtsp_transport", "tcp", "-i", SRC.format(path=self.path),
             "-an", "-f", "mpjpeg", "-q:v", str(settings.mjpeg_quality)]
        if settings.mjpeg_fps:
            a += ["-r", str(settings.mjpeg_fps)]
        if settings.mjpeg_size:
            a += ["-s", settings.mjpeg_size]
        return a + ["-"]

    async def _run(self) -> None:
        """跑到沒人看為止。**例外一律吞掉**——影像壞了不准影響飛行資料。"""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._args(), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except Exception as e:
            self.last_error = f"啟動 ffmpeg 失敗：{type(e).__name__}: {e}"
            log.warning("影像：%s", self.last_error)
            return
        self.started_at = time.monotonic()
        log.info("影像：%s 開始轉 MJPEG", self.path)
        buf = b""
        try:
            while True:
                if (self.viewers == 0
                        and time.monotonic() - self.demand_at > settings.mjpeg_idle_s):
                    log.info("影像：%s 沒人看滿 %.0f 秒，停止轉碼",
                             self.path, settings.mjpeg_idle_s)
                    break
                try:
                    chunk = await asyncio.wait_for(
                        self._proc.stdout.read(READ_CHUNK), timeout=5.0)
                except asyncio.TimeoutError:
                    continue                      # 沒資料不代表壞了，回上面重檢查
                if not chunk:
                    err = (await self._proc.stderr.read(2000)).decode(errors="replace")
                    self.last_error = err.strip() or "ffmpeg 結束了，沒有說原因"
                    log.warning("影像：%s 轉碼結束——%s", self.path, self.last_error)
                    break
                buf += chunk
                # 一個 chunk 裡可能有好幾張，只留**最新的那張**：
                # 看的人要的是現在，不是把積壓的畫面依序補完
                while True:
                    i = buf.find(SOI)
                    j = buf.find(EOI, i + 2) if i >= 0 else -1
                    if i < 0 or j < 0:
                        break
                    self.frame = buf[i:j + 2]
                    self.seq += 1
                    self.frames += 1
                    self.frame_at = time.monotonic()
                    buf = buf[j + 2:]
                if len(buf) > 4 * READ_CHUNK:     # 垃圾累積：丟掉，不要無限長大
                    buf = b""
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("影像：%s 轉碼迴圈出錯", self.path)
        finally:
            with contextlib.suppress(ProcessLookupError, Exception):
                if self._proc and self._proc.returncode is None:
                    self._proc.terminate()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            self._proc = None


_streams: dict[str, Transcoder] = {}


def get(drone_id: str) -> Transcoder:
    t = _streams.get(drone_id)
    if t is None:
        t = _streams[drone_id] = Transcoder(drone_id)
    return t


async def next_frame(t: Transcoder, after_seq: int, timeout: float):
    """等一張比 `after_seq` 新的。

    用輪詢而不是卡住一個工作執行緒：backend 的 executor 還要跑 MAVLink 與
    資料庫，幾個閒著的觀看者不該把它佔滿（同參考專案的理由）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t.touch()
        if t.seq > after_seq and t.frame is not None:
            return t.seq, t.frame
        await asyncio.sleep(0.02)
    return None


async def shutdown() -> None:
    for t in list(_streams.values()):
        await t.stop()
