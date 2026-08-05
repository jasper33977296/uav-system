#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""機上 5G 量測節點（跑在無人機的 companion computer 上）。

Python 3.6+（RB5 原廠 Ubuntu 18.04 映像可直接跑，零環境改動）。
同步式設計、無 asyncio；PX4 位置用 pymavlink 直讀（不需 mavsdk_server 副程序）。

每秒：讀 modem 的 RF 指標（AT+QENG）＋ 當下 PX4 位置與 GPS 時間 ＋ ping 地面站
量 RTT → 先寫入本地 SQLite 緩衝（先落盤再送）→ 兩條通道送往地面站：

  即時通道  POST /api/link-metrics/live   最新一筆，失敗不重試（下一秒有新的）
  記錄通道  POST /api/link-metrics/batch  未確認樣本批次補傳，唯一入庫路徑

執行緒分工（同步式但不讓網路卡住取樣節奏）：
  main    讀 modem ＋ 組樣本 ＋ 落盤（穩定 1Hz，不受網路影響）
  sender  live/batch 送出（逾時只拖慢送出，不拖慢取樣）
  ping    RTT／丟包量測
  px4     pymavlink 讀 GLOBAL_POSITION_INT 與 SYSTEM_TIME（只聽不發）

鏈路劣化（研究最想看的時刻）正是資料傳不回去的時刻——緩衝與補傳是
資料正確性的前提。設計文件見主系統 repo 的 doc/onboard-telemetry.md。

設定（環境變數）：
  GROUND_API   地面站 API，如 http://192.168.55.10:38000   （必填）
  AT_PORT      modem AT 埠，預設 /dev/ttyUSB2（RM500Q 常見）
  AT_BAUD      預設 115200
  SAMPLE_HZ    採樣頻率，預設 1.0（AT 查詢延遲 100–500ms，1Hz 是務實上限）
  BUFFER_PATH  持久緩衝，預設 /var/lib/uav-link/buffer.sqlite3
  PX4_URL      機上 PX4 MAVLink，預設 udpin:0.0.0.0:14540（udpin:// 寫法也接受）
  PING_HOST    RTT 目標，預設取 GROUND_API 的主機
  DRONE_ID     選填；不填則地面站記在「主機」名下（單機部署的正確預設）

模式：--probe 上機首驗（印 modem 原始回應）；--selftest 解析器測試（免硬體）
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

GROUND_API = os.environ.get("GROUND_API", "").rstrip("/")
AT_PORT = os.environ.get("AT_PORT", "/dev/ttyUSB2")
AT_BAUD = int(os.environ.get("AT_BAUD", "115200"))
SAMPLE_HZ = float(os.environ.get("SAMPLE_HZ", "1.0"))
BUFFER_PATH = os.environ.get("BUFFER_PATH", "/var/lib/uav-link/buffer.sqlite3")
PX4_URL = os.environ.get("PX4_URL", "udpin:0.0.0.0:14540")
DRONE_ID = os.environ.get("DRONE_ID") or None
PING_HOST = os.environ.get("PING_HOST") or (
    GROUND_API.split("//")[-1].split(":")[0] if GROUND_API else "")

BUFFER_KEEP_SENT_DAYS = 7      # 已送達樣本保留天數（地面站重建資料庫時可救援）
PING_LOSS_WINDOW = 20          # 丟包率統計視窗（最近 N 次 ping）


# ── AT 回應解析 ──────────────────────────────────────────────
# RM500Q AT+QENG="servingcell" 已文件化的格式（NR5G-SA）：
#   +QENG: "servingcell",<state>,"NR5G-SA",<duplex>,<MCC>,<MNC>,<cellID hex>,
#          <PCID>,<TAC>,<ARFCN>,<band>,<DL_bw>,<RSRP>,<RSRQ>,<SINR>,<scs>,<srxlev>
# NSA 模式為多行（"LTE" 一行＋"NR5G-NSA" 一行）。實機韌體可能有欄位差異——
# 解析刻意防禦性，原始回應永遠塞進 raw 欄位帶回地面站，供上機首驗校準。

def _num(s, cast=float):
    try:
        return cast(s)
    except (ValueError, TypeError):
        return None


def parse_qeng(raw):
    out = {"raw": {"at_qeng": raw.strip()}}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("+QENG"):
            continue
        body = line.split(":", 1)[1].strip()
        parts = [p.strip().strip('"') for p in body.split(",")]
        if "NR5G-SA" in parts[:4] and len(parts) >= 15:
            i = parts.index("NR5G-SA")
            f = parts[i:]   # [NR5G-SA, duplex, MCC, MNC, cellID, PCID, TAC, ARFCN, band, bw, RSRP, RSRQ, SINR, scs, srxlev]
            if len(f) >= 13:
                out.update({
                    "nr_mode": "SA",
                    "cell_id": _num(f[4], lambda x: int(x, 16)) or _num(f[4], int),
                    "pci": _num(f[5], int),
                    "band": "n{}".format(f[8]) if _num(f[8], int) is not None else (f[8] or None),
                    "rsrp": _num(f[10]),
                    "rsrq": _num(f[11]),
                    "sinr": _num(f[12]),
                })
        elif parts[0] == "NR5G-NSA" and len(parts) >= 7:
            # +QENG: "NR5G-NSA",<MCC>,<MNC>,<PCID>,<RSRP>,<SINR>,<RSRQ>,...
            out.update({
                "nr_mode": "NSA",
                "pci": _num(parts[3], int),
                "rsrp": _num(parts[4]),
                "sinr": _num(parts[5]),
                "rsrq": _num(parts[6]),
            })
    return out


def parse_ping(output):
    m = re.search(r"time=([\d.]+)\s*ms", output)
    return float(m.group(1)) if m else None


# ── modem ────────────────────────────────────────────────────
class Modem(object):
    def __init__(self, port, baud):
        import serial                          # 延遲載入：--selftest 不需要
        self.ser = serial.Serial(port, baud, timeout=1.0)

    def cmd(self, at, wait=0.6):
        """送一條 AT 指令、收完回應。AT 介面一問一答，不必並發。"""
        self.ser.reset_input_buffer()
        self.ser.write((at + "\r").encode())
        time.sleep(wait)
        return self.ser.read(self.ser.in_waiting or 1).decode(errors="replace")


# ── PX4（pymavlink 直讀，只聽不發——read-only 原則）────────────
class Px4State(object):
    def __init__(self):
        self.lat = None
        self.lon = None
        self.alt_rel = None
        self.gps_offset = None    # GPS 絕對時間 - monotonic（時鐘權威用 GPS）

    def now_iso(self):
        if self.gps_offset is not None:
            t = self.gps_offset + time.monotonic()
            return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()


def px4_thread(state):
    """背景執行緒：GLOBAL_POSITION_INT → 位置、SYSTEM_TIME／GPS_RAW_INT → GPS 時間。
    連不上時節點照常運行（樣本無座標，仍具時序價值）。"""
    try:
        from pymavlink import mavutil
    except ImportError:
        print("[px4] 未安裝 pymavlink，樣本將不含位置（pip install pymavlink）", flush=True)
        return
    url = PX4_URL.replace("://", ":")          # 接受 mavsdk 式 udpin://host:port 寫法
    while True:
        try:
            conn = mavutil.mavlink_connection(url)
            print("[px4] listening {}".format(url), flush=True)
            while True:
                msg = conn.recv_match(
                    type=["GLOBAL_POSITION_INT", "SYSTEM_TIME", "GPS_RAW_INT"],
                    blocking=True, timeout=5)
                if msg is None:
                    continue
                t = msg.get_type()
                if t == "GLOBAL_POSITION_INT":
                    state.lat = msg.lat / 1e7
                    state.lon = msg.lon / 1e7
                    state.alt_rel = msg.relative_alt / 1000.0
                elif t == "SYSTEM_TIME" and msg.time_unix_usec:
                    state.gps_offset = msg.time_unix_usec / 1e6 - time.monotonic()
                elif t == "GPS_RAW_INT" and msg.time_usec > 1e15:
                    # 規格：time_usec 可能是 epoch 或開機時間，以數量級判別
                    # （>1e15 μs ≈ 2001 年之後才視為 epoch）；SITL 給的是開機時間
                    state.gps_offset = msg.time_usec / 1e6 - time.monotonic()
        except Exception as e:
            print("[px4] 中斷（{}: {}），5 秒後重連".format(type(e).__name__, e), flush=True)
            time.sleep(5)


# ── ping（獨立執行緒：RTT 逾時不拖慢取樣節奏）─────────────────
class PingState(object):
    def __init__(self):
        self.rtt_ms = None
        self.loss_pct = 0.0


def ping_thread(state, host):
    results = []
    while True:
        t0 = time.monotonic()
        try:
            out = subprocess.run(
                ["ping", "-c", "1", "-W", "1", host],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True)
            rtt = parse_ping(out.stdout) if out.returncode == 0 else None
        except Exception:
            rtt = None
        state.rtt_ms = rtt
        results.append(rtt is not None)
        del results[:-PING_LOSS_WINDOW]
        state.loss_pct = round(100.0 * (1 - float(sum(results)) / len(results)), 1)
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))


# ── 緩衝（先落盤再送；sqlite 連線不跨執行緒，main 與 sender 各開一條）──
class Buffer(object):
    def __init__(self, path):
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS samples (
            seq INTEGER PRIMARY KEY, payload TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL)""")
        self.db.commit()
        row = self.db.execute("SELECT max(seq) FROM samples").fetchone()
        self.next_seq = (row[0] or 0) + 1

    def append(self, sample):
        sample["seq"] = self.next_seq
        self.db.execute("INSERT INTO samples (seq, payload, ts) VALUES (?, ?, ?)",
                        (self.next_seq, json.dumps(sample), time.time()))
        self.db.commit()                       # 先落盤，送出交給 sender 執行緒
        self.next_seq += 1
        return sample

    def unsent(self, limit=200):
        rows = self.db.execute(
            "SELECT payload FROM samples WHERE sent=0 ORDER BY seq LIMIT ?", (limit,))
        return [json.loads(r[0]) for r in rows]

    def mark_sent(self, seqs):
        self.db.executemany("UPDATE samples SET sent=1 WHERE seq=?", [(s,) for s in seqs])
        self.db.commit()

    def pending(self):
        return self.db.execute("SELECT count(*) FROM samples WHERE sent=0").fetchone()[0]

    def prune(self):
        cutoff = time.time() - BUFFER_KEEP_SENT_DAYS * 86400
        self.db.execute("DELETE FROM samples WHERE sent=1 AND ts < ?", (cutoff,))
        self.db.commit()


# ── HTTP ─────────────────────────────────────────────────────
def post_json(url, payload, timeout=3.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    r = urllib.request.urlopen(req, timeout=timeout)
    try:
        body = r.read()
        return json.loads(body.decode()) if body else None
    finally:
        r.close()


class Latest(object):
    """main → sender 的最新樣本交接（即時通道用）。GIL 下單一參照賦值安全。"""
    sample = None


def sender_thread(latest):
    """送出執行緒：網路逾時只拖慢送出，永不拖慢取樣。"""
    buf = Buffer(BUFFER_PATH)                  # 自己的 sqlite 連線
    n = 0
    while True:
        t0 = time.monotonic()
        s = latest.sample
        if s is not None:
            try:                               # 即時通道：失敗放棄
                post_json(GROUND_API + "/api/link-metrics/live", s, timeout=1.5)
            except Exception:
                pass
        pending = buf.unsent()
        if pending:
            try:                               # 記錄通道：補傳到成功
                body = {"samples": pending}
                if DRONE_ID:
                    body["drone_id"] = DRONE_ID
                res = post_json(GROUND_API + "/api/link-metrics/batch", body)
                buf.mark_sent(res["accepted_seq"])
            except Exception as e:
                if n % 15 == 0:
                    print("[batch] 送出失敗（{}），累積 {} 筆待補傳".format(
                        type(e).__name__, buf.pending()), flush=True)
        n += 1
        if n % 600 == 0:
            buf.prune()
        time.sleep(max(0.2, 1.0 - (time.monotonic() - t0)))


# ── 主迴圈：讀 modem ＋ 組樣本 ＋ 落盤（穩定節奏）─────────────
def run():
    if not GROUND_API:
        sys.exit("GROUND_API 未設定（如 http://192.168.55.10:38000）")
    modem = Modem(AT_PORT, AT_BAUD)
    print("[modem] {}@{} 開啟；ATI → {!r}".format(
        AT_PORT, AT_BAUD, modem.cmd("ATI", 0.3).strip()[:60]), flush=True)
    buf = Buffer(BUFFER_PATH)
    print("[buffer] {}（待補傳 {} 筆）".format(BUFFER_PATH, buf.pending()), flush=True)

    px4 = Px4State()
    ping = PingState()
    latest = Latest()
    threads = [(px4_thread, (px4,)), (sender_thread, (latest,))]
    if PING_HOST:
        threads.append((ping_thread, (ping, PING_HOST)))
    for fn, args in threads:
        t = threading.Thread(target=fn, args=args)
        t.daemon = True
        t.start()

    while True:
        t0 = time.monotonic()
        sample = parse_qeng(modem.cmd('AT+QENG="servingcell"'))
        # 位置與時間在採樣當下綁定（同一時鐘、同一瞬間，補傳不影響正確性）
        sample.update({
            "time": px4.now_iso(),
            "lat": px4.lat, "lon": px4.lon, "alt_rel": px4.alt_rel,
            "rtt_ms": ping.rtt_ms, "packet_loss_pct": ping.loss_pct,
        })
        latest.sample = buf.append(sample)     # 先落盤，再供 sender 取用
        time.sleep(max(0.0, 1.0 / SAMPLE_HZ - (time.monotonic() - t0)))


# ── 上機首驗與自我測試 ────────────────────────────────────────
def probe():
    """印出 modem 原始回應。把整段輸出貼回開發端，校準解析器。"""
    modem = Modem(AT_PORT, AT_BAUD)
    for at in ("ATI", 'AT+QENG="servingcell"', "AT+CSQ", "AT+QNWINFO"):
        print("\n>>> {}".format(at))
        print(modem.cmd(at, 0.8))
    print("\n（把以上輸出完整貼回，用於校準 parse_qeng）")


def selftest():
    sa = ('+QENG: "servingcell","NOCONN","NR5G-SA","TDD",466,92,690BB01,'
          '101,690BB,633984,78,12,-85,-11,13,1,-')
    r = parse_qeng(sa)
    assert r["nr_mode"] == "SA" and r["sinr"] == 13.0 and r["pci"] == 101, r
    assert r["rsrp"] == -85.0 and r["band"] == "n78", r
    assert r["cell_id"] == int("690BB01", 16), r
    nsa = '+QENG: "NR5G-NSA",466,92,205,-88,15,-12,633984,78,12,1'
    r = parse_qeng(nsa)
    assert r["nr_mode"] == "NSA" and r["sinr"] == 15.0 and r["pci"] == 205, r
    assert parse_ping("64 bytes from x: icmp_seq=1 ttl=64 time=23.4 ms") == 23.4
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--probe" in sys.argv:
        probe()
    else:
        try:
            run()
        except KeyboardInterrupt:
            sys.exit(130)
