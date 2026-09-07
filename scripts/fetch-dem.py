#!/usr/bin/env python3
"""抓 SRTM 圖磚放進 `data/dem/`（issues/047 §2）。

**這是佈署前的準備動作，不是執行期的功能。** 現場是離線的——圖磚要在
有網路的地方先抓好，跟著 repo 一起帶到現場（但**不進 git**：一塊 25 MB，
見 `data/dem/README.md`）。

    python3 scripts/fetch-dem.py 24.7738 121.0461      # 一個點所在的圖磚
    python3 scripts/fetch-dem.py 24.7 121.0 24.9 121.3 # 一個範圍（含四角）

來源是 AWS 的 `elevation-tiles-prod/skadi`（SRTM 1 弧秒，公開、免金鑰）。
已經存在的圖磚不重抓——**要更新請自己刪掉**，免得每次佈署都重下 25 MB。
"""
import gzip
import math
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import terrain  # noqa: E402

BASE = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "dem")


def fetch(name: str) -> str:
    path = os.path.join(OUT, name)
    if os.path.exists(path):
        return f"已有 {name}（{os.path.getsize(path) / 1e6:.0f} MB），跳過"
    url = f"{BASE}/{name[:3]}/{name}.gz"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            raw = gzip.decompress(r.read())
    except Exception as e:                      # noqa: BLE001
        # 海上的圖磚本來就不存在（沒有陸地就沒有 SRTM）——這不是錯誤，
        # 但也不能默默跳過：預檢那邊會因此說「沒有檢查」，使用者要知道為什麼
        return f"**抓不到 {name}**：{e}"
    n = int(round(math.sqrt(len(raw) / 2)))
    if n * n * 2 != len(raw):
        return f"**{name} 大小不對**（{len(raw)} 位元組），沒有寫入"
    os.makedirs(OUT, exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    return f"寫入 {name}：{n}×{n}（格距約 {3600 / (n - 1) * 30.9:.0f} m）、{len(raw) / 1e6:.0f} MB"


def main(argv: list[str]) -> int:
    a = [float(x) for x in argv]
    if len(a) == 2:
        lat0 = lat1 = a[0]
        lon0 = lon1 = a[1]
    elif len(a) == 4:
        lat0, lon0, lat1, lon1 = min(a[0], a[2]), min(a[1], a[3]), \
            max(a[0], a[2]), max(a[1], a[3])
    else:
        print(__doc__)
        return 2
    names = sorted({terrain.tile_name(la, lo)
                    for la in range(math.floor(lat0), math.floor(lat1) + 1)
                    for lo in range(math.floor(lon0), math.floor(lon1) + 1)})
    for nm in names:
        print(fetch(nm))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
