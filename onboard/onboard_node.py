#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""機上 5G 量測節點（跑在無人機的 companion computer 上）。

Python 3.6+、**零第三方依賴（純標準庫）**——RB5 上不需要 venv、不需要 pip：
serial 用 termios 直開 tty；MAVLink 只聽三種訊息且只收不發，內建迷你解析器
（框架＋X.25 CRC 驗證），不值得為此拉整包 pymavlink。
同步式設計、無 asyncio。

每秒：讀 modem 的 RF 指標（AT+QENG）＋ 當下 PX4 位置與 GPS 時間 ＋ ping 地面站
量 RTT → 先寫入本地 SQLite 緩衝（先落盤再送）→ 兩條通道送往地面站：

  即時通道  POST /api/link-metrics/live   最新一筆，失敗不重試（下一秒有新的）
  記錄通道  POST /api/link-metrics/batch  未確認樣本批次補傳，唯一入庫路徑

執行緒分工（同步式但不讓網路卡住取樣節奏）：
  main    讀 modem ＋ 組樣本 ＋ 落盤（穩定 1Hz，不受網路影響）
  sender  live/batch 送出（逾時只拖慢送出，不拖慢取樣）
  ping    RTT／丟包量測
  px4     讀 GLOBAL_POSITION_INT / SYSTEM_TIME / GPS_RAW_INT（只聽不發）

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
import socket
import sqlite3
import struct
import subprocess
import sys
import termios
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


# ── modem（termios 直開 tty，不需要 pyserial）─────────────────
class Modem(object):
    def __init__(self, port, baud):
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = 0                                   # iflag：關掉所有輸入處理
        a[1] = 0                                   # oflag：raw 輸出
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0                                   # lflag：非 canonical、無 echo
        speed = getattr(termios, "B{}".format(baud), termios.B115200)
        a[4] = a[5] = speed                        # RM500Q 是 USB CDC，實際忽略 baud
        termios.tcsetattr(self.fd, termios.TCSANOW, a)

    def cmd(self, at, wait=0.6):
        """送一條 AT 指令、收完回應。AT 介面一問一答，不必並發。"""
        termios.tcflush(self.fd, termios.TCIFLUSH)
        os.write(self.fd, (at + "\r").encode())
        time.sleep(wait)
        chunks = []
        while True:
            try:
                b = os.read(self.fd, 4096)
            except OSError:                        # EAGAIN＝目前沒資料了
                break
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode(errors="replace")


# ── 迷你 MAVLink 解析器（只收不發，只認三種訊息）──────────────
# 拉整包 pymavlink 只為了聽三種訊息不值得（且要 pip）。框架格式與 CRC
# 演算法是 MAVLink 規格的穩定部分；CRC_EXTRA 是各訊息定義的常數。
MAV_CRC_EXTRA = {2: 137, 24: 24, 33: 104}   # SYSTEM_TIME, GPS_RAW_INT, GLOBAL_POSITION_INT


def _x25(data, crc=0xFFFF):
    for b in data:
        t = (b ^ crc) & 0xFF
        t = (t ^ (t << 4)) & 0xFF
        crc = ((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4)) & 0xFFFF
    return crc


def mav_frames(buf):
    """從一個 UDP datagram 取出 (msgid, payload)。v1/v2 都認；
    只對我們認識的 msgid 驗 CRC，其他依框架長度跳過。
    MAVLink 2 會截掉 payload 尾端的 0，使用前要補回。"""
    out = []
    i, n = 0, len(buf)
    while i < n:
        m = buf[i]
        if m == 0xFD and i + 12 <= n:              # v2: FD len incompat compat seq sys comp msgid×3
            plen = buf[i + 1]
            sig = 13 if (buf[i + 2] & 1) else 0
            end = i + 10 + plen + 2 + sig
            if end > n:
                break
            msgid = buf[i + 7] | (buf[i + 8] << 8) | (buf[i + 9] << 16)
            if msgid in MAV_CRC_EXTRA:
                crc = buf[i + 10 + plen] | (buf[i + 11 + plen] << 8)
                if _x25(bytes(buf[i + 1:i + 10 + plen]) + bytes([MAV_CRC_EXTRA[msgid]])) == crc:
                    out.append((msgid, bytes(buf[i + 10:i + 10 + plen])))
            i = end
        elif m == 0xFE and i + 8 <= n:             # v1: FE len seq sys comp msgid
            plen = buf[i + 1]
            end = i + 6 + plen + 2
            if end > n:
                break
            msgid = buf[i + 5]
            if msgid in MAV_CRC_EXTRA:
                crc = buf[i + 6 + plen] | (buf[i + 7 + plen] << 8)
                if _x25(bytes(buf[i + 1:i + 6 + plen]) + bytes([MAV_CRC_EXTRA[msgid]])) == crc:
                    out.append((msgid, bytes(buf[i + 6:i + 6 + plen])))
            i = end
        else:
            i += 1                                 # 不是框架開頭，逐位元組重新同步
    return out


def _pad(payload, size):
    return payload + b"\x00" * (size - len(payload)) if len(payload) < size else payload


# ── PX4（UDP 被動監聽——read-only 原則）───────────────────────
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


def _apply_mav(state, msgid, payload):
    if msgid == 33:                                # GLOBAL_POSITION_INT
        _, lat, lon, _, rel = struct.unpack_from("<Iiiii", _pad(payload, 20))
        state.lat = lat / 1e7
        state.lon = lon / 1e7
        state.alt_rel = rel / 1000.0
    elif msgid == 2:                               # SYSTEM_TIME
        usec = struct.unpack_from("<Q", _pad(payload, 8))[0]
        if usec:
            state.gps_offset = usec / 1e6 - time.monotonic()
    elif msgid == 24:                              # GPS_RAW_INT
        usec = struct.unpack_from("<Q", _pad(payload, 8))[0]
        # 規格：time_usec 可能是 epoch 或開機時間，以數量級判別
        # （>1e15 μs ≈ 2001 年之後才視為 epoch）；SITL 給的是開機時間
        if usec > 1e15:
            state.gps_offset = usec / 1e6 - time.monotonic()


def px4_thread(state):
    """背景執行緒：GLOBAL_POSITION_INT → 位置、SYSTEM_TIME／GPS_RAW_INT → GPS 時間。
    連不上時節點照常運行（樣本無座標，仍具時序價值）。"""
    # 接受 udpin://host:port（mavsdk 式）與 udpin:host:port 兩種寫法
    hp = PX4_URL.replace("://", ":").split(":")
    host, port = hp[-2], int(hp[-1])
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((host, port))
            sock.settimeout(5)
            print("[px4] listening udp {}:{}".format(host, port), flush=True)
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                for msgid, payload in mav_frames(data):
                    _apply_mav(state, msgid, payload)
        except Exception as e:
            print("[px4] 中斷（{}: {}），5 秒後重連".format(type(e).__name__, e), flush=True)
            time.sleep(5)
        finally:
            if sock:
                sock.close()


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
    # 實機首驗樣本（2026-08-10，RM502Q-AE R11A04，私網 PLMN 999/66，n79）
    real = ('+QENG: "servingcell","NOCONN","NR5G-SA","TDD",999,66,000214001,'
            '133,8D,720288,79,12,-68,-11,32,1,-')
    r = parse_qeng(real)
    assert r["nr_mode"] == "SA" and r["pci"] == 133 and r["sinr"] == 32.0, r
    assert r["band"] == "n79" and r["rsrp"] == -68.0 and r["cell_id"] == 0x214001, r
    nsa = '+QENG: "NR5G-NSA",466,92,205,-88,15,-12,633984,78,12,1'
    r = parse_qeng(nsa)
    assert r["nr_mode"] == "NSA" and r["sinr"] == 15.0 and r["pci"] == 205, r
    assert parse_ping("64 bytes from x: icmp_seq=1 ttl=64 time=23.4 ms") == 23.4

    # MAVLink 解析：組一個 GLOBAL_POSITION_INT v2 框架（含 MAVLink2 的
    # 尾端零截斷），CRC 用同一套 _x25——與真 PX4 的互通由 SITL 整合測試把關
    def frame(msgid, payload):
        p = payload.rstrip(b"\x00") or b"\x00"     # v2 零截斷
        hdr = bytes([0xFD, len(p), 0, 0, 7, 1, 1, msgid & 0xFF, (msgid >> 8) & 0xFF, msgid >> 16])
        crc = _x25(hdr[1:] + p + bytes([MAV_CRC_EXTRA[msgid]]))
        return hdr + p + bytes([crc & 0xFF, crc >> 8])

    st = Px4State()
    gpi = struct.pack("<IiiiihhhH", 1000, 251234567, 1215554433, 0, 12500, 0, 0, 0, 0)
    for msgid, payload in mav_frames(b"\x00garbage" + frame(33, gpi) + frame(2, struct.pack("<QI", 1754800000000000, 99))):
        _apply_mav(st, msgid, payload)
    assert st.lat == 25.1234567 and st.lon == 121.5554433 and st.alt_rel == 12.5, vars(st)
    assert st.gps_offset is not None
    st2 = Px4State()
    for msgid, payload in mav_frames(frame(24, struct.pack("<Q", 4698204000))):
        _apply_mav(st2, msgid, payload)
    assert st2.gps_offset is None      # GPS_RAW_INT 的開機時間必須被拒收
    bad = bytearray(frame(33, gpi)); bad[12] ^= 0xFF
    assert mav_frames(bytes(bad)) == []            # CRC 錯的框架不得採用
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
