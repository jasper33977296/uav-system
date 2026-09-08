#!/usr/bin/env python3
"""量建物資料拿不拿得到（doc/field-3d-model-design.md §7-3、§8）。

兩條路都量，把量到的原樣寫下來：
  A. NLSC 多維度平台 —— 連通性、LOD1 圖層清單、圖層是不是開放格式的端點
  B. OSM Overpass —— 場域 bbox 內的建物數與高度標籤覆蓋率

    python3 scripts/probe-buildings.py
    python3 scripts/probe-buildings.py --lat 24.773449 --lon 121.045864 --radius-m 600

結果寫到 `data/probe-buildings.json`。
"""
import argparse
import json
import math
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "uav-gcs-probe"}
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "probe-buildings.json")

NLSC_HOST = "3dmaps.nlsc.gov.tw"
# 這台送的葉憑證 CN 是 track.nlsc.gov.tw、鏈也接不到本機信任庫，
# 所以驗證一定失敗；分開記「連得到」與「憑證過不過」，別混成一句「不通」。
NOVERIFY = ssl.create_default_context()
NOVERIFY.check_hostname = False
NOVERIFY.verify_mode = ssl.CERT_NONE

MENU = f"https://{NLSC_HOST}/FrontMap/API/DefaultFrontMapMenu.aspx"
FRONTMAP = f"https://{NLSC_HOST}/FrontMap/"
RELATED = ["3dtiles.nlsc.gov.tw", "i3s.nlsc.gov.tw", "3dtest.nlsc.gov.tw",
           "mapserver01.nlsc.gov.tw", "wmts.nlsc.gov.tw"]

OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]


def get(url, timeout=20, data=None, raw=False):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA, data=data)
        with urllib.request.urlopen(req, timeout=timeout, context=NOVERIFY) as r:
            body = r.read()
            out = {"ok": True, "status": r.status, "secs": round(time.time() - t0, 2),
                   "type": r.headers.get("Content-Type"), "bytes": len(body)}
            return (out, body) if raw else out
    except urllib.error.HTTPError as e:
        out = {"ok": False, "status": e.code, "secs": round(time.time() - t0, 2)}
    except Exception as e:                                      # noqa: BLE001
        out = {"ok": False, "error": f"{type(e).__name__}: {e}",
               "secs": round(time.time() - t0, 2)}
    return (out, b"") if raw else out


def probe_nlsc(lat, lon):
    out = {"host": NLSC_HOST}
    try:
        out["dns"] = socket.gethostbyname(NLSC_HOST)
    except Exception as e:                                      # noqa: BLE001
        out["dns"] = None
        out["dns_error"] = str(e)
        return out
    for port in (443, 80):
        s = socket.socket()
        s.settimeout(6)
        try:
            s.connect((NLSC_HOST, port))
            out[f"tcp{port}"] = "通"
        except Exception as e:                                  # noqa: BLE001
            out[f"tcp{port}"] = f"不通（{type(e).__name__}）"
        finally:
            s.close()

    out["tls"] = {}
    try:
        c = ssl.create_default_context().wrap_socket(
            socket.create_connection((NLSC_HOST, 443), 6), server_hostname=NLSC_HOST)
        out["tls"]["驗證"] = "過"
        c.close()
    except ssl.SSLCertVerificationError as e:
        out["tls"]["驗證"] = f"不過：{e.verify_message}"
    except Exception as e:                                      # noqa: BLE001
        out["tls"]["驗證"] = f"{type(e).__name__}: {e}"
    try:
        c = NOVERIFY.wrap_socket(socket.create_connection((NLSC_HOST, 443), 6),
                                 server_hostname=NLSC_HOST)
        out["tls"]["葉憑證 CN"] = dict(
            x[0] for x in (c.getpeercert() or {}).get("subject", ()))
        c.close()
    except Exception:                                           # noqa: BLE001
        pass

    out["frontmap"] = get(FRONTMAP, timeout=40)
    out["其他主機"] = {h: get(f"https://{h}/", timeout=12) for h in RELATED}
    out.update(probe_menu(lat, lon))
    return out


def probe_menu(lat, lon):
    """圖層目錄：找出涵蓋這個座標的 LOD1 建物圖層，並看它是不是開放端點。"""
    meta, body = get(MENU, timeout=60, raw=True)
    out = {"目錄": meta}
    if not meta.get("ok"):
        return out
    try:
        nodes = json.loads(body.decode("utf-8"))
    except Exception as e:                                      # noqa: BLE001
        out["目錄"]["parse_error"] = str(e)
        return out

    out["目錄"]["節點數"] = len(nodes)
    lod1, near = [], []
    for n in nodes:
        if "LOD1" not in (n.get("TAG") or ""):
            continue
        rec = {"標題": n.get("TITLE"), "圖層名": n.get("LAYERNAME"),
               "型別": n.get("TYPE")}
        lod1.append(rec)
        g = n.get("GOTO") or ""
        m = re.search(r'"POINT":\s*\[([\d.]+),\s*([\d.]+)\]', g)
        if m:
            dlon, dlat = float(m.group(1)) - lon, float(m.group(2)) - lat
            km = math.hypot(dlat * 110.574, dlon * 111.320 * math.cos(math.radians(lat)))
            rec["定位點距場域_km"] = round(km, 1)
            if km < 60:
                near.append(rec)
    out["LOD1 圖層數"] = len(lod1)
    out["定位點在 60 km 內的 LOD1 圖層"] = sorted(
        near, key=lambda r: r["定位點距場域_km"])[:10]

    # 目錄裡有沒有任何**開放格式**的建物端點（3D Tiles tileset.json／i3s SceneServer）
    urls = set(re.findall(r'https?://[^"\\\s,]+', body.decode("utf-8", "replace")))
    openish = sorted(u for u in urls
                     if re.search(r'(tileset\.json|SceneServer|cesium.*\.json)', u, re.I))
    out["目錄內的開放格式端點"] = {
        "數量": len(openish),
        "主機": sorted({u.split("/")[2] for u in openish}),
        "取樣": openish[:3],
    }
    if openish:
        out["開放端點抽驗"] = {u: get(u, timeout=20) for u in openish[:2]}
    return out


def probe_overpass(lat, lon, radius_m):
    d = radius_m / 110574.0
    dl = d / math.cos(math.radians(lat))
    bbox = (round(lat - d, 6), round(lon - dl, 6), round(lat + d, 6), round(lon + dl, 6))
    # §9-B：body 級 `out geom`（不是 `out tags geom`），而且 way ＋ relation 都要
    q = (f"[out:json][timeout:60];("
         f'way["building"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});'
         f'relation["building"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});'
         f");out geom;")
    out = {"bbox": bbox, "query": q, "mirrors": {}}
    for url in OVERPASS:
        meta, body = get(url, timeout=90, data=q.encode(), raw=True)
        out["mirrors"][url] = meta
        if not meta.get("ok"):
            continue
        try:
            els = json.loads(body.decode("utf-8")).get("elements", [])
        except Exception as e:                                  # noqa: BLE001
            out["mirrors"][url]["parse_error"] = str(e)
            continue
        tag = lambda e, k: (e.get("tags") or {}).get(k)          # noqa: E731
        with_h = [e for e in els if tag(e, "height")]
        with_l = [e for e in els if tag(e, "building:levels")]
        out["counts"] = {
            "way": sum(1 for e in els if e.get("type") == "way"),
            "relation": sum(1 for e in els if e.get("type") == "relation"),
            "total": len(els),
            "有 height": len(with_h), "有 building:levels": len(with_l),
            "兩者都沒有": len(els) - len({id(e) for e in with_h + with_l}),
        }
        out["kinds"] = {}
        for e in els:
            k = tag(e, "building") or "yes"
            out["kinds"][k] = out["kinds"].get(k, 0) + 1
        # §9-C 要丟掉點數不足的輪廓，先知道有幾個
        out["輪廓點數不足"] = [e.get("id") for e in els
                              if len(e.get("geometry") or []) < 4][:20]
        break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=24.773449)
    ap.add_argument("--lon", type=float, default=121.045864)
    ap.add_argument("--radius-m", type=float, default=600.0)
    a = ap.parse_args()

    res = {"when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "where": {"lat": a.lat, "lon": a.lon, "radius_m": a.radius_m},
           "nlsc": probe_nlsc(a.lat, a.lon),
           "overpass": probe_overpass(a.lat, a.lon, a.radius_m)}

    n = res["nlsc"]
    print(f"── A. {NLSC_HOST} ──")
    print(f"  DNS {n.get('dns') or n.get('dns_error')}"
          f" ｜ 443 {n.get('tcp443')} ｜ 80 {n.get('tcp80')}")
    for k, v in (n.get("tls") or {}).items():
        print(f"  TLS {k}：{v}")
    fm = n.get("frontmap") or {}
    print(f"  FrontMap HTTP {fm.get('status', fm.get('error'))} {fm.get('bytes', '')}B")
    for h, r in (n.get("其他主機") or {}).items():
        print(f"    {h:<26} " + (f"HTTP {r['status']}" if "status" in r
                                 else r.get("error", "")))
    if n.get("LOD1 圖層數") is not None:
        print(f"  圖層目錄 {n['目錄'].get('節點數')} 節點"
              f"，標 LOD1 的 {n['LOD1 圖層數']} 個")
        for r in n["定位點在 60 km 內的 LOD1 圖層"]:
            print(f"    {r['圖層名']:<12} {r['標題']}  ({r['定位點距場域_km']} km)")
        o = n["目錄內的開放格式端點"]
        print(f"  開放格式端點 {o['數量']} 個，主機 {o['主機']}")
        for u, r in (n.get("開放端點抽驗") or {}).items():
            print(f"    {u} → " + (f"HTTP {r['status']} {r.get('bytes')}B"
                                   if "status" in r else r.get("error", "")))

    o = res["overpass"]
    print("\n── B. Overpass ──")
    for url, r in o["mirrors"].items():
        print(f"  {url.split('//')[1].split('/')[0]:<26} "
              + (f"HTTP {r.get('status')} {r.get('secs')}s" if r.get("ok")
                 else r.get("error", str(r.get("status")))))
    if o.get("counts"):
        print(f"  {o['bbox']}")
        for k, v in o["counts"].items():
            print(f"    {k}：{v}")
        print(f"    building 型別：{o['kinds']}")
        if o["輪廓點數不足"]:
            print(f"    輪廓點數不足：{o['輪廓點數不足']}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\n完整結果：{os.path.normpath(OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
