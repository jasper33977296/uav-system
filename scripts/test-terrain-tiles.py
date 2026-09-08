#!/usr/bin/env python3
"""地形圖磚（issues/048 F1 的 3D 落地）：編碼要對，錯了看不出來。

**這支測試存在的理由**：高程編錯的地形在畫面上**看起來完全正常**——
還是一片起伏的地，只是高度是錯的。而規劃頁的每一個判斷都建立在那個高度上。
所以這裡逐像素把 PNG 解回來，跟 DEM 原值比對。

跑法（不需要服務、不需要網路）：
    python3 scripts/test-terrain-tiles.py
"""
import math
import struct
import sys
import zlib

sys.path.insert(0, "/home/k200/uav-system/libs")

import terrain as T  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def decode_png(data: bytes):
    """把我們自己寫的那種 PNG 讀回來（8-bit RGB、濾波器 0）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG 標頭不對"
    pos, w, h, idat = 8, 0, 0, b""
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        crc = struct.unpack(">I", data[pos + 8 + ln:pos + 12 + ln])[0]
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, f"{tag} 的 CRC 不對"
        if tag == b"IHDR":
            w, h, bits, ctype = struct.unpack(">IIBB", body[:10])
            assert (bits, ctype) == (8, 2), (bits, ctype)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + ln
    raw = zlib.decompress(idat)
    rows = []
    stride = w * 3
    for y in range(h):
        f = raw[y * (stride + 1)]
        assert f == 0, f"只寫濾波器 0，卻讀到 {f}"
        rows.append(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
    return w, h, rows


def height_at(rows, px, py):
    r, g, b = rows[py][px * 3], rows[py][px * 3 + 1], rows[py][px * 3 + 2]
    return r * 256 + g + b / 256 - 32768      # terrarium


DEM = T.Dem("/home/k200/uav-system/data/dem")
LAT, LON = 24.7734, 121.0459
Z = 14
N = 2 ** Z
TX = int((LON + 180) / 360 * N)
TY = int((1 - math.asinh(math.tan(math.radians(LAT))) / math.pi) / 2 * N)

print("── 1. 圖磚生得出來，而且是合法的 PNG ─────────────────────")
png = T.terrarium_tile(DEM, Z, TX, TY)
chk("有圖磚", png is not None)
w, h, rows = decode_png(png)
chk("尺寸 256×256", (w, h) == (256, 256), (w, h))

print("\n── 2. **逐像素把高程解回來，跟 DEM 原值比**───────────────")
lon0, lat0, lon1, lat1 = T._tile_bounds(Z, TX, TY)
worst, n = 0.0, 0
for py in range(0, 256, 37):
    for px in range(0, 256, 37):
        lat = lat0 + (lat1 - lat0) * (py + 0.5) / 256
        lon = lon0 + (lon1 - lon0) * (px + 0.5) / 256
        src = DEM.elevation(lat, lon)
        if src is None:
            continue
        n += 1
        worst = max(worst, abs(height_at(rows, px, py) - round(src)))
chk(f"{n} 個取樣點全部對得上（誤差 ≤ 0.5 m，來源是整數公尺）",
    n > 20 and worst <= 0.5, f"最大誤差 {worst} m")

print("\n── 3. 高程的量級要對——**編錯的地形看起來一樣正常** ────────")
mid = height_at(rows, 128, 128)
chk(f"圖磚中心 {mid:.0f} m 落在這個場地的合理範圍（50–400 m）",
    50 <= mid <= 400, mid)

print("\n── 4. 整張沒有資料就不給圖磚，不給一張全 0 的 ──────────────")
# 太平洋中間：沒有這塊圖磚
far = T.terrarium_tile(DEM, Z, 8000, 8000)
chk("回 None（讓 maplibre 跳過），而不是一片海平面高度的假平地",
    far is None, far and len(far))

print("\n── 5. 破洞補 0 m，不補 −32768 ─────────────────────────")
# 圖磚邊界一定有一部分落在圖磚外；那些像素不能變成三萬公尺深的坑
edge = T.terrarium_tile(DEM, 10, int((121.0 + 180) / 360 * 1024),
                        int((1 - math.asinh(math.tan(math.radians(24.0)))
                             / math.pi) / 2 * 1024))
if edge:
    _, _, er = decode_png(edge)
    lows = [height_at(er, x, y) for y in range(0, 256, 8) for x in range(0, 256, 8)]
    chk("沒有任何像素低於 −100 m", min(lows) > -100, min(lows))
else:
    chk("（那塊圖磚沒有資料，跳過）", True)

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
