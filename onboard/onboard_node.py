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

設定（腳本同目錄的 .env 或環境變數，環境變數優先）：
  GROUND_API   地面站 API，如 http://192.168.55.10:38000   （必填）
  AT_PORT      modem AT 埠，預設 /dev/ttyUSB2（RM500Q 常見）
  AT_BAUD      預設 115200
  SAMPLE_HZ    採樣頻率，預設 1.0（AT 查詢延遲 100–500ms，1Hz 是務實上限）
  BUFFER_PATH  持久緩衝，預設 /var/lib/uav-link/buffer.sqlite3
  PX4_URL      機上 PX4 MAVLink，預設 udpin:0.0.0.0:14540（udpin:// 寫法也接受）
  PING_HOST    RTT 目標，預設取 GROUND_API 的主機
  MAV_SYSID    這台的 MAVLink sysid（多機部署建議用這個）——啟動時向地面站
               查 GET /api/drones 比對解出 drone_id。**雞生蛋**：drone_id 是
               地面站的 UUID，要等該機首次 MAVLink 連上被自動註冊才存在，
               機上無從預先知道；sysid 則是機上本來就設好的（見 rb5-setup）
  DRONE_ID     直接給地面站的 drone_id UUID（相容路徑；知道 UUID 時可用）
               兩者皆不填＝單機部署，地面站記在「主機」名下；**多機必須設
               其一**，否則三台的即時訊號會全被記到主機身上（靜默混料）

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
import urllib.error
import urllib.request
from datetime import datetime, timezone

def _load_env(path):
    """讀腳本同目錄的 .env（KEY=VALUE，# 註解）。真環境變數優先——
    systemd 與手動執行共用同一份設定，unit 檔不需要 Environment=。

    encoding 必須顯式指定：systemd 環境沒有 locale，Python 3.6 會用
    ASCII 開檔，.env 的中文註解直接炸（實際發生過，服務無限重啟）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except (IOError, OSError):
        pass


_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

GROUND_API = os.environ.get("GROUND_API", "").rstrip("/")
AT_PORT = os.environ.get("AT_PORT", "/dev/ttyUSB2")
AT_BAUD = int(os.environ.get("AT_BAUD", "115200"))
SAMPLE_HZ = float(os.environ.get("SAMPLE_HZ", "1.0"))
BUFFER_PATH = os.environ.get("BUFFER_PATH", "/var/lib/uav-link/buffer.sqlite3")
PX4_URL = os.environ.get("PX4_URL", "udpin:0.0.0.0:14540")
DRONE_ID = os.environ.get("DRONE_ID") or None
_MAV_SYSID_RAW = (os.environ.get("MAV_SYSID") or "").strip()
try:                                              # 空＝未設；非數字＝設錯（run() 會擋）
    MAV_SYSID = int(_MAV_SYSID_RAW or 0) or None
except ValueError:
    MAV_SYSID = None
PING_HOST = os.environ.get("PING_HOST") or (
    GROUND_API.split("//")[-1].split(":")[0] if GROUND_API else "")

BUFFER_KEEP_SENT_DAYS = 7      # 已送達樣本保留天數（地面站重建資料庫時可救援）
PING_LOSS_WINDOW = 20          # 丟包率統計視窗（最近 N 次 ping，jitter 同窗）
IDENTITY_RETRY_S = 30          # drone_id 解析重試間隔（該機連上地面站前解不到）


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
        self.jitter_ms = None


def jitter_ms(rtts):
    """連續 RTT 的平均變動量（mean |Δ|，RFC 3550 的簡化版）。

    不另外發探測：直接用既有 ping 序列算，成本為零而 schema 早有 jitter_ms 欄。
    少於兩筆無從談變動 → None（誠實留空，不填 0 假裝很穩）。"""
    vals = [r for r in rtts if r is not None]
    if len(vals) < 2:
        return None
    diffs = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    return round(sum(diffs) / len(diffs), 1)


def ping_thread(state, host):
    results = []
    rtts = []
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
        if rtt is not None:            # 只把成功的 RTT 納入變動量（逾時不是「變慢」）
            rtts.append(rtt)
            del rtts[:-PING_LOSS_WINDOW]
        state.jitter_ms = jitter_ms(rtts)
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


def get_json(url, timeout=3.0):
    r = urllib.request.urlopen(url, timeout=timeout)
    try:
        return json.loads(r.read().decode())
    finally:
        r.close()


def match_sysid(drones, sysid):
    """GET /api/drones 的回應 → 該 sysid 對應的 drone_id（找不到回 None）。
    sysid 為 None 時一律不匹配——否則會誤中「還沒綁 sysid」（mav_sysid=null）的列。"""
    if sysid is None:
        return None
    for d in drones or []:
        if d.get("mav_sysid") == sysid and d.get("id"):
            return str(d["id"])
    return None


class Identity(object):
    """這台機在地面站的身分（drone_id）。

    兩條路徑：DRONE_ID 直接給 UUID（相容）；MAV_SYSID=N 則向地面站查
    GET /api/drones 比對 mav_sysid 解出 UUID——**雞生蛋**的解法：UUID 要等
    該機首次 MAVLink 連上被自動註冊才存在，機上無從預先寫進 .env。

    `blocked`＝「該解但還沒解出」：此時**兩條通道都不送**。不帶 drone_id 送出
    會讓地面站 fallback 記到「主機」名下（api.py：`fleet.get(drone_id) if
    drone_id else live`）——多機時三台的即時訊號全蓋到主機身上、靜默混料。
    寧可讓緩衝堆著（本來就是為斷線設計的）等解出後補傳，也不要送錯身分的資料。
    兩者皆不設＝單機部署，blocked=False、不帶 drone_id（維持舊行為）。
    """

    def __init__(self, drone_id=None, sysid=None):
        self.drone_id = drone_id
        self.sysid = sysid
        self.blocked = bool(sysid) and not drone_id


def identity_thread(ident):
    """背景解析 sysid → drone_id，解到為止（解不到就吵，不靜默降級）。"""
    n = 0
    while ident.drone_id is None:
        try:
            did = match_sysid(get_json(GROUND_API + "/api/drones"), ident.sysid)
            if did:
                ident.drone_id = did
                ident.blocked = False
                print("[identity] sysid {} → drone_id {}（樣本開始送出）".format(
                    ident.sysid, did), flush=True)
                return
            if n % 10 == 0:        # 每 10 輪印一次，別洗版
                print("[identity] 地面站還沒有 mav_sysid={} 的機——該機首次 MAVLink "
                      "連上地面站才會自動註冊。樣本先進緩衝不送出（避免記到主機"
                      "名下），{}s 後重試".format(ident.sysid, IDENTITY_RETRY_S), flush=True)
        except Exception as e:
            if n % 10 == 0:
                print("[identity] 查地面站失敗（{}: {}），{}s 後重試".format(
                    type(e).__name__, e, IDENTITY_RETRY_S), flush=True)
        n += 1
        time.sleep(IDENTITY_RETRY_S)


class Latest(object):
    """main → sender 的最新樣本交接（即時通道用）。GIL 下單一參照賦值安全。"""
    sample = None


def sender_thread(latest, ident):
    """送出執行緒：網路逾時只拖慢送出，永不拖慢取樣。"""
    buf = Buffer(BUFFER_PATH)                  # 自己的 sqlite 連線
    n = 0
    while True:
        t0 = time.monotonic()
        if ident.blocked:      # 身分未解出：兩條通道都先不送（見 Identity）
            time.sleep(1.0)
            continue
        did = ident.drone_id
        s = latest.sample
        if s is not None:
            try:                               # 即時通道：失敗放棄
                # **身分要跟著即時通道走**：不帶 drone_id 的話地面站會 fallback
                # 到主機，多機時三台的即時訊號全蓋到主機身上（而記錄通道帶了
                # drone_id 入庫是對的）＝畫面與資料不一致。
                post_json(GROUND_API + "/api/link-metrics/live",
                          dict(s, drone_id=did) if did else s, timeout=1.5)
            except Exception:
                pass
        pending = buf.unsent()
        if pending:
            try:                               # 記錄通道：補傳到成功
                body = {"samples": pending}
                if did:
                    body["drone_id"] = did
                res = post_json(GROUND_API + "/api/link-metrics/batch", body)
                buf.mark_sent(res["accepted_seq"])
            except urllib.error.HTTPError as e:
                # 伺服器有回應＝拒絕帶原因，必須讓人看得到（409 常見：
                # 地面站 .env 的 LINK_SOURCE 還是 simulated）
                if n % 15 == 0:
                    try:
                        detail = e.read().decode(errors="replace")[:200]
                    except Exception:
                        detail = ""
                    print("[batch] 伺服器拒絕 HTTP {}：{}（累積 {} 筆待補傳）".format(
                        e.code, detail, buf.pending()), flush=True)
            except Exception as e:
                if n % 15 == 0:
                    print("[batch] 送出失敗（{}: {}），累積 {} 筆待補傳".format(
                        type(e).__name__, e, buf.pending()), flush=True)
        n += 1
        if n % 600 == 0:
            buf.prune()
        time.sleep(max(0.2, 1.0 - (time.monotonic() - t0)))


# ── 主迴圈：讀 modem ＋ 組樣本 ＋ 落盤（穩定節奏）─────────────
def run():
    if not GROUND_API:
        sys.exit("GROUND_API 未設定（如 http://10.141.2.32:38000）")
    if _MAV_SYSID_RAW and MAV_SYSID is None:
        # 設了卻讀不出數字＝設定打錯。不能默默當「沒設」跑下去——那會讓這台的
        # 樣本記到地面站主機名下（靜默混料），錯得無聲無息。寧可立刻停、講清楚。
        sys.exit("MAV_SYSID={!r} 不是有效的 sysid（要正整數，如 MAV_SYSID=2）".format(
            _MAV_SYSID_RAW))
    modem = Modem(AT_PORT, AT_BAUD)
    print("[modem] {}@{} 開啟；ATI → {!r}".format(
        AT_PORT, AT_BAUD, modem.cmd("ATI", 0.3).strip()[:60]), flush=True)
    buf = Buffer(BUFFER_PATH)
    print("[buffer] {}（待補傳 {} 筆）".format(BUFFER_PATH, buf.pending()), flush=True)

    px4 = Px4State()
    ping = PingState()
    latest = Latest()
    ident = Identity(DRONE_ID, MAV_SYSID)
    if ident.drone_id:
        print("[identity] drone_id {}（直接指定）".format(ident.drone_id), flush=True)
    elif ident.sysid:
        print("[identity] 以 MAV_SYSID={} 向地面站解析 drone_id…".format(ident.sysid),
              flush=True)
    else:
        print("[identity] 未設 MAV_SYSID／DRONE_ID：樣本記在地面站「主機」名下"
              "（單機部署的預設；多機部署請設 MAV_SYSID）", flush=True)
    threads = [(px4_thread, (px4,)), (sender_thread, (latest, ident))]
    if ident.blocked:
        threads.append((identity_thread, (ident,)))
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
            "jitter_ms": ping.jitter_ms,
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

    # 身分解析（多機）：GET /api/drones 回應 → drone_id
    drones = [{"id": "aaa", "name": "uav-1", "mav_sysid": 1},
              {"id": "bbb", "name": "uav-2", "mav_sysid": 2},
              {"id": "ccc", "name": "no-sysid", "mav_sysid": None}]
    assert match_sysid(drones, 2) == "bbb", match_sysid(drones, 2)
    assert match_sysid(drones, 9) is None          # 該機還沒連上＝解不到，不亂猜
    assert match_sysid([], 1) is None and match_sysid(None, 1) is None
    # blocked 語意：設了 sysid 但還沒解出＝不送（避免記到主機名下）
    assert Identity(None, 2).blocked is True
    assert Identity("uuid-x", 2).blocked is False  # 直接給 UUID＝不必解
    assert Identity(None, None).blocked is False   # 單機部署＝維持舊行為
    # 即時通道必須帶 drone_id（本次修的真 bug：不帶會被地面站記到主機名下）
    live_payload = dict({"sinr": 13.0}, drone_id="bbb")
    assert live_payload["drone_id"] == "bbb" and live_payload["sinr"] == 13.0

    # jitter：連續 RTT 的平均變動量，不足兩筆回 None（不填 0 假裝很穩）
    assert jitter_ms([10.0, 12.0, 11.0]) == 1.5    # |2| + |1| = 3 / 2
    assert jitter_ms([20.0]) is None and jitter_ms([]) is None
    assert jitter_ms([None, 10.0, 14.0]) == 4.0    # None（逾時）不計入
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
