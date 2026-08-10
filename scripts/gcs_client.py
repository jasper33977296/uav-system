"""測試腳本共用的 GCS REST 客戶端（純標準庫）。

2026-08-10 起測試腳本不再自己講 MAVLink（mavsdk 鷹架退役）：所有指令
一律經 command 服務（:38001）——吃 ENABLE_COMMANDS gate、ACK 契約、
幾何預檢與 command_log 留痕，維持單一指揮權。監看走 backend /api/live。
"""
import json
import sys
import time
import urllib.error
import urllib.request

BACKEND = "http://localhost:38000"
COMMAND = "http://localhost:38001"


def call(url, payload=None, method=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if payload is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read()
            return json.loads(body.decode()) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        sys.exit(f"❌ HTTP {e.code} {url}\n   {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"❌ 連不到 {url}：{e.reason}\n"
                 "   backend/command 服務有起來嗎？docker compose ps")


def pick_sysid():
    h = call(f"{COMMAND}/healthz")
    if not h.get("enabled"):
        sys.exit("❌ 指令能力未啟用：.env 設 ENABLE_COMMANDS=true 後 docker compose up -d")
    ids = sorted(h.get("drones") or {})
    if not ids:
        sys.exit("❌ command 服務還沒看到機（14541 未通）：curl :38001/healthz 排查")
    return ids[0]


def monitor_until_disarm(note=""):
    """每 2 秒印一行：模式/高度/SINR/鏈路狀態，直到上鎖。"""
    print(f"\n📡 監看中{note}（上鎖即結束）…")
    was_armed = False
    while True:
        time.sleep(2)
        d = call(f"{BACKEND}/api/live")
        link = d.get("link") or {}
        print(f"   {d.get('flight_mode') or '—':18s} alt={d.get('alt_rel') or 0:6.1f}m"
              f"  SINR={link.get('sinr')}  link={d.get('link_state')}"
              f"  {'armed' if d.get('armed') else 'disarmed'}", flush=True)
        if d.get("armed"):
            was_armed = True
        elif was_armed:
            return


def print_last_session():
    rows = call(f"{BACKEND}/api/sessions?limit=1")
    if not rows:
        return
    s = rows[0]
    summary = s.get("summary")
    if isinstance(summary, str):
        summary = json.loads(summary)
    print("\n📋 架次摘要：")
    for k in ("samples_total", "avg_sinr", "min_sinr", "avg_rtt_ms",
              "max_alt_rel", "samples_in_zone"):
        print(f"   {k:16s} {summary.get(k) if summary else '—'}")
