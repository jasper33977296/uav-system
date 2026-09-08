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
* 它是一份**被 30 m 格子抹平的表面**（C 波段雷達的回波來自樹冠與屋頂，
  只部分穿透）：**既不能當成乾淨的地面，也不能當成障礙物圖**。
  在有樹的地方「地面」被抬高、離地算得偏保守；而一棟 36 m 的樓落在 30 m 的
  格子裡會被周圍地面平均掉，可能只抬高幾公尺。**錯的方向隨地點改變**，
  所以不能靠加一個常數修掉（doc/field-3d-model-design.md §3）。
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
import zlib
from dataclasses import dataclass

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


@dataclass(frozen=True)
class Sample:
    """某一點正上方有什麼——**帶著出處的一份答案，不是一個純量**。

    `ground` 與 `top` 分開，是因為之後會有建物：那時 `top` 才是「飛機下方
    最高的東西」，而 `ground` 仍然是地面。今天只有 SRTM，兩者相同。

    `horiz_res_m` 不是裝飾：SRTM 的答案與航測 DSM 的答案在型別上一樣、
    在意義上差三個數量級，畫面要說得出這一段的判定來自多粗的格子
    （doc/field-3d-model-design.md §5）。
    """
    ground: float | None
    top: float | None
    source: str                  # srtm／nlsc20／lod1／survey／none
    kind: str | None = None      # building／tree／unknown
    horiz_res_m: float = 30.0


#: 查不到的那一份。**不是 0，也不是「通過」。**
NO_DATA = Sample(None, None, "none", None, float("inf"))


def surface(lat: float, lon: float, dem: "Dem | None" = None) -> Sample:
    """這一點的地面與上方最高點。由細往粗退，缺就說缺。

    現在只有 SRTM 一層；建物與自測 DSM 進來時在這裡往前加，
    **五個呼叫點不必改**（doc/field-3d-model-design.md §5）。
    """
    d = dem if dem is not None else shared()
    if d is None or not d.available:
        return NO_DATA
    g = d.elevation(lat, lon)
    if g is None:
        return NO_DATA
    return Sample(g, g, "srtm", None, 30.0)


#: 行程共用的一份（圖磚讀進來就留著；一塊 1 弧秒圖磚 25 MB，
#: 開一次比每次檢查都重讀便宜太多）
_shared: Dem | None = None


def shared() -> Dem:
    global _shared
    if _shared is None:
        _shared = Dem()
    return _shared


# ── 給 maplibre 吃的地形圖磚（issues/048 F1 的 3D 落地）──────────
#
# maplibre 的 `raster-dem` 只吃 XYZ 的 PNG 圖磚，而我們手上是 `.hgt`。
# 這裡就地換算，**不引進任何相依**：後端是會飛飛機的服務，為了畫圖多裝一個
# 影像函式庫不划算，而 PNG 的最小可用編碼只有二十幾行。

#: 高程編碼。`terrarium`：`h = R*256 + G + B/256 - 32768`。
#: 我們的 DEM 是整數公尺，所以 B 恆為 0——**那不是精度損失**，
#: 是來源本來就只有公尺。
TILE_PX = 256


def _png(width: int, height: int, rgb: bytes) -> bytes:
    """最小可用的 PNG（8-bit RGB、濾波器 0）。

    **只做我們需要的那一種**：不做調色盤、不做交錯、不做其他濾波器。
    多做的每一種都是一段沒有人會執行到、但會壞的程式。
    """
    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # 每列的濾波器型別：None
        raw += rgb[y * width * 3:(y + 1) * width * 3]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _tile_bounds(z: int, x: int, y: int):
    """XYZ 圖磚 → 經緯度範圍（Web Mercator）。"""
    n = 2 ** z
    lon0 = x / n * 360.0 - 180.0
    lon1 = (x + 1) / n * 360.0 - 180.0
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lon0, lat0, lon1, lat1      # 上緣 lat0 > 下緣 lat1


def terrarium_tile(dem: "Dem", z: int, x: int, y: int,
                   size: int = TILE_PX) -> bytes | None:
    """一張 `terrarium` 編碼的地形圖磚。整張都沒有資料時回 `None`。

    **沒有資料的像素填 0 m，而整張沒有資料就不給圖磚**——兩者要分開：
    前者是一張圖裡的破洞（邊界上一定會有），後者是「這一區我們沒有 DEM」，
    而讓 maplibre 拿到一張全 0 的圖磚，畫面上會是一片**海平面高度的假平地**。
    """
    lon0, lat0, lon1, lat1 = _tile_bounds(z, x, y)
    buf = bytearray(size * size * 3)
    any_data = False
    for py in range(size):
        lat = lat0 + (lat1 - lat0) * (py + 0.5) / size
        for px in range(size):
            lon = lon0 + (lon1 - lon0) * (px + 0.5) / size
            h = dem.elevation(lat, lon)
            if h is None:
                continue                    # 留 0（＝ -32768 m）？不：見下
            any_data = True
            v = int(round(h)) + 32768
            i = (py * size + px) * 3
            buf[i] = (v >> 8) & 0xFF
            buf[i + 1] = v & 0xFF
    if not any_data:
        return None
    # 破洞補成 0 m 而不是 -32768 m：前者是海平面，後者會在畫面上變成一個
    # 三萬公尺深的坑，把整張地形的縮放拉爛
    zero = 32768
    for i in range(0, len(buf), 3):
        if buf[i] == 0 and buf[i + 1] == 0:
            buf[i] = (zero >> 8) & 0xFF
            buf[i + 1] = zero & 0xFF
    return _png(size, size, bytes(buf))
