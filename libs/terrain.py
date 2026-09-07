"""地形高程（DEM）查詢——**規劃時的離地高度檢查用**，不參與飛行。

## 為什麼需要它（2026-09-07 摔機）

`lowspeed-test-260907` 的航點是 `frame 3`（`GLOBAL_RELATIVE_ALT`），
高度的意思是**離起飛點**，不是離地面。地面往上抬 1 m，這份 1 m 的航線
就貼地了；抬 2 m 就是往土裡飛。飛控不會抱怨——它忠實地維持「離起飛點
1 m」，然後撞上去。

所以規劃端要自己算：

    預期離地 = (起飛點 AMSL + 相對高度) − DEM 高程(航點)

## 為什麼「起飛點 AMSL 也用 DEM」不是偷懶

離線規劃時我們沒有起飛點的真實 AMSL，只能一起用 DEM。展開來看：

    預期離地 = DEM(起飛點) + 相對高度 − DEM(航點)
             = 相對高度 − (DEM(航點) − DEM(起飛點))

**DEM 的絕對誤差在相減時消掉了**，剩下的是同一塊圖磚內兩點的**相對**誤差
——那正是 SRTM 相對可靠的部分（絕對 LE90 約 16 m，鄰近點相對誤差數公尺）。
真實的起飛點 AMSL（GPS 或飛控的地形庫）反而會把絕對誤差**加回來**，
除非兩邊用的是同一個基準。所以離線這條路不是次等的近似，
它問的是另一個問題：「地面沿著這條航線起伏多少」。

## 誠實話（要跟著報告上到畫面）

* SRTM 水平解析度 1 弧秒 ≈ **30 m**：一格之內的東西它不知道。
  能防「整片地比起飛點高 8 m」，防不了「前面有個 1 m 土堆」。
* 它是**地形**高程，不是地表：樹、電線、車、貨櫃一概沒有。
* 1 m 貼地任務要安全，需要的是測距儀（本機 `RNGFND1_TYPE = 0`），
  不是更好的 DEM。

## 圖磚

SRTM `.hgt`：大端 int16、由北向南逐列、每列由西向東，
`N24E121.hgt` 的 (24,121) 是**西南角**。1 弧秒版 3601²、3 弧秒版 1201²，
由檔案大小判定。`-32768` 是空洞（雷達沒測到），照樣視為「沒資料」。
"""
import math
import os
import struct

VOID = -32768

#: 圖磚放這裡。容器裡由 compose 掛進來；沒有這個目錄不是錯誤——
#: **沒有資料就說「沒檢查」，不要說「通過」**（見 `Dem.available`）。
DEFAULT_DIR = os.getenv("DEM_DIR", "/srv/dem")


def tile_name(lat: float, lon: float) -> str:
    """(lat, lon) 落在哪一塊圖磚。用 floor 不是 int：南半球/西半球的
    -0.5 屬於 S01/W001，`int()` 會給出 N00/E000。"""
    la, lo = math.floor(lat), math.floor(lon)
    return (f"{'N' if la >= 0 else 'S'}{abs(la):02d}"
            f"{'E' if lo >= 0 else 'W'}{abs(lo):03d}.hgt")


class Dem:
    """一個圖磚目錄。查得到就回高程（公尺，AMSL），查不到回 `None`。

    **查不到與 0 m 是兩件事**，所以回 `None` 而不是 0——海平面高度 0
    是一個合法的答案，「我沒有這塊地的資料」不是。
    """

    def __init__(self, path: str | None = None):
        self.path = path or DEFAULT_DIR
        self._tiles: dict[str, tuple[bytes, int] | None] = {}
        #: 查詢過但沒有圖磚的名字——報告要說得出**缺哪一塊**，
        #: 操作員才知道要去補什麼
        self.missing: set[str] = set()

    @property
    def available(self) -> bool:
        """目錄裡有沒有任何圖磚。整個目錄是空的時候，逐點回報「缺 N24E121」
        沒有意義——那是「這套系統還沒裝地形資料」，是另一句話。"""
        try:
            return any(f.endswith(".hgt") for f in os.listdir(self.path))
        except OSError:
            return False

    def _tile(self, name: str):
        if name in self._tiles:
            return self._tiles[name]
        t = None
        try:
            with open(os.path.join(self.path, name), "rb") as f:
                buf = f.read()
            n = int(round(math.sqrt(len(buf) / 2)))
            if n * n * 2 == len(buf) and n > 1:
                t = (buf, n)
        except OSError:
            t = None
        self._tiles[name] = t
        return t

    def _sample(self, buf: bytes, n: int, row: int, col: int) -> int | None:
        if not (0 <= row < n and 0 <= col < n):
            return None
        v = struct.unpack_from(">h", buf, (row * n + col) * 2)[0]
        return None if v == VOID else v

    def elevation(self, lat: float, lon: float) -> float | None:
        """雙線性內插的地形高程（m, AMSL）。圖磚缺、或四個角有任何一個是
        空洞，都回 `None`——**內插一個含空洞的格子等於自己編一個數字**。"""
        name = tile_name(lat, lon)
        t = self._tile(name)
        if t is None:
            self.missing.add(name)
            return None
        buf, n = t
        # 圖磚的第 0 列是**北邊**：緯度往北，列號往小
        y = (math.floor(lat) + 1 - lat) * (n - 1)
        x = (lon - math.floor(lon)) * (n - 1)
        r, c = int(math.floor(y)), int(math.floor(x))
        # 落在最東/最北那一條線上時退一格，讓 r+1/c+1 還在圖磚內
        r = min(max(r, 0), n - 2)
        c = min(max(c, 0), n - 2)
        fy, fx = y - r, x - c
        z00 = self._sample(buf, n, r, c)
        z01 = self._sample(buf, n, r, c + 1)
        z10 = self._sample(buf, n, r + 1, c)
        z11 = self._sample(buf, n, r + 1, c + 1)
        if None in (z00, z01, z10, z11):
            return None
        return ((z00 * (1 - fx) + z01 * fx) * (1 - fy)
                + (z10 * (1 - fx) + z11 * fx) * fy)


#: 行程共用的一份（圖磚讀進來就留著；一塊 1 弧秒圖磚 25 MB，
#: 開一次比每次檢查都重讀便宜太多）
_shared: Dem | None = None


def shared() -> Dem:
    global _shared
    if _shared is None:
        _shared = Dem()
    return _shared
