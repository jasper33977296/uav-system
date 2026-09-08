#!/usr/bin/env python3
"""把 NLSC 正射影像先抓進 `data/ortho/`（doc/field-3d-model-design.md §7-2）。

現場離線，所以圖磚要在有網路的地方先抓好——與 `scripts/fetch-dem.py` 同一個
形狀：先抓、進 `data/`、不進 git、已有的不重抓。

    python3 scripts/fetch-ortho.py 24.7734 121.0459            # 該點周圍 400 m
    python3 scripts/fetch-ortho.py 24.770 121.042 24.777 121.050 --zoom 16-19

沒有影像的格子（境外、雲遮）**不重試也不留空檔**——那與「還沒抓」是兩件事，
留下來只會讓下次以為抓過了。
"""
import argparse
import math
import os
import sys
import urllib.request

URL = ("https://wmts.nlsc.gov.tw/wmts/PHOTO2/default/"
       "GoogleMapsCompatible/{z}/{y}/{x}")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "ortho")


def tiles(lat0, lon0, lat1, lon1, z):
    n = 2 ** z
    def xt(lon): return int((lon + 180) / 360 * n)
    def yt(lat):
        return int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    for x in range(xt(min(lon0, lon1)), xt(max(lon0, lon1)) + 1):
        for y in range(yt(max(lat0, lat1)), yt(min(lat0, lat1)) + 1):
            yield z, x, y


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("coords", nargs="+", type=float)
    ap.add_argument("--zoom", default="16-19")
    ap.add_argument("--radius-m", type=float, default=400.0)
    a = ap.parse_args(argv)
    if len(a.coords) == 2:
        lat, lon = a.coords
        d = a.radius_m / 110574.0
        box = (lat - d, lon - d / math.cos(math.radians(lat)),
               lat + d, lon + d / math.cos(math.radians(lat)))
    elif len(a.coords) == 4:
        box = tuple(a.coords)
    else:
        raise SystemExit("給兩個數（一個點）或四個數（一個範圍）")
    z0, _, z1 = a.zoom.partition("-")
    zs = range(int(z0), int(z1 or z0) + 1)

    got = skip = miss = 0
    for z in zs:
        for _, x, y in tiles(*box, z):
            path = os.path.join(OUT, str(z), str(x), f"{y}.jpg")
            if os.path.exists(path):
                skip += 1
                continue
            try:
                req = urllib.request.Request(URL.format(z=z, y=y, x=x),
                                             headers={"User-Agent": "uav-gcs"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
            except Exception:                                   # noqa: BLE001
                miss += 1
                continue
            if not data.startswith(b"\xff\xd8"):
                miss += 1
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            got += 1
            print(f"\r抓了 {got}、已有 {skip}、沒有影像 {miss}", end="", flush=True)
    print(f"\r抓了 {got}、已有 {skip}、沒有影像 {miss}"
          f"（{OUT}）                    ")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
