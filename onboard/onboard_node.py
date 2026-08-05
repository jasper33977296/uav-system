#!/usr/bin/env python3
"""機上 5G 量測節點（跑在無人機的 companion computer 上）。

每秒：讀 modem 的 RF 指標（AT+QENG）＋ 從機上 PX4 取當下位置 ＋ ping 地面站
量 RTT → 先寫入本地 SQLite 緩衝（先落盤再送）→ 兩條通道送往地面站：

  即時通道  POST /api/link-metrics/live   最新一筆，失敗不重試（下一秒有新的）
  記錄通道  POST /api/link-metrics/batch  未確認樣本批次補傳，唯一入庫路徑

鏈路劣化（研究最想看的時刻）正是資料傳不回去的時刻——緩衝與補傳是
資料正確性的前提，不是優化。設計文件見主 repo doc/onboard-telemetry.md。

設定（環境變數）：
  GROUND_API   地面站 API，如 http://192.168.55.10:38000   （必填）
  AT_PORT      modem AT 埠，預設 /dev/ttyUSB2（RM500Q 常見）
  AT_BAUD      預設 115200
  SAMPLE_HZ    採樣頻率，預設 1.0（AT 查詢延遲 100–500ms，1Hz 是務實上限）
  BUFFER_PATH  持久緩衝，預設 /var/lib/uav-link/buffer.sqlite3（要在斷電保留的分割區）
  PX4_URL      機上 PX4 的 MAVLink，預設 udpin://0.0.0.0:14540
  PING_HOST    RTT 目標，預設取 GROUND_API 的主機
  DRONE_ID     選填；不填則地面站記在「主機」名下（單機部署的正確預設）

模式：
  （無參數）   正式運行
  --probe      上機首驗：印出 modem 原始回應（ATI / AT+QENG / AT+CSQ）後結束。
               把輸出貼回開發端校準解析——不要假設解析一次就對。
  --selftest   解析器自我測試（不需硬體）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GROUND_API = os.environ.get("GROUND_API", "").rstrip("/")
AT_PORT = os.environ.get("AT_PORT", "/dev/ttyUSB2")
AT_BAUD = int(os.environ.get("AT_BAUD", "115200"))
SAMPLE_HZ = float(os.environ.get("SAMPLE_HZ", "1.0"))
BUFFER_PATH = Path(os.environ.get("BUFFER_PATH", "/var/lib/uav-link/buffer.sqlite3"))
PX4_URL = os.environ.get("PX4_URL", "udpin://0.0.0.0:14540")
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

def _num(s: str, cast=float):
    try:
        return cast(s)
    except (ValueError, TypeError):
        return None


def parse_qeng(raw: str) -> dict:
    out: dict = {"raw": {"at_qeng": raw.strip()}}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("+QENG"):
            continue
        body = line.split(":", 1)[1].strip()
        parts = [p.strip().strip('"') for p in body.split(",")]
        if "NR5G-SA" in parts[:4] and len(parts) >= 15:
            i = parts.index("NR5G-SA")          # 通常是 parts[2]
            f = parts[i:]                        # ["NR5G-SA", duplex, MCC, MNC, cellID, PCID, TAC, ARFCN, band, bw, RSRP, RSRQ, SINR, scs, srxlev]
            if len(f) >= 13:
                out.update({
                    "nr_mode": "SA",
                    "cell_id": _num(f[4], lambda x: int(x, 16)) or _num(f[4], int),
                    "pci": _num(f[5], int),
                    "band": f"n{f[8]}" if _num(f[8], int) is not None else f[8] or None,
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


def parse_ping(output: str) -> float | None:
    m = re.search(r"time=([\d.]+)\s*ms", output)
    return float(m.group(1)) if m else None


# ── modem ────────────────────────────────────────────────────
class Modem:
    def __init__(self, port: str, baud: int):
        import serial                                   # 延遲載入：--selftest 不需要
        self.ser = serial.Serial(port, baud, timeout=1.0)

    def cmd(self, at: str, wait: float = 0.6) -> str:
        """送一條 AT 指令、收完回應。AT 介面一次一問一答，不必並發。"""
        self.ser.reset_input_buffer()
        self.ser.write((at + "\r").encode())
        time.sleep(wait)
        return self.ser.read(self.ser.in_waiting or 1).decode(errors="replace")


# ── PX4 位置（機上直讀，位置與 RF 同瞬間綁定）─────────────────
class Px4State:
    lat: float | None = None
    lon: float | None = None
    alt_rel: float | None = None
    gps_offset: float | None = None    # GPS 絕對時間 - monotonic（時鐘權威用 GPS）

    def now_iso(self) -> str:
        if self.gps_offset is not None:
            t = self.gps_offset + time.monotonic()
            return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()


async def px4_watcher(state: Px4State) -> None:
    """位置訂閱。PX4 連不上時節點照常運行（樣本無座標，仍具時序價值）。"""
    try:
        from mavsdk import System
    except ImportError:
        print("[px4] 未安裝 mavsdk，樣本將不含位置（pip install mavsdk）", flush=True)
        return
    while True:
        try:
            drone = System()
            await drone.connect(system_address=PX4_URL)
            async for cs in drone.core.connection_state():
                if cs.is_connected:
                    break
            print("[px4] connected", flush=True)

            async def _pos():
                async for p in drone.telemetry.position():
                    state.lat = p.latitude_deg
                    state.lon = p.longitude_deg
                    state.alt_rel = p.relative_altitude_m

            async def _time():
                async for t in drone.telemetry.unix_epoch_time():
                    if t.time_us:
                        state.gps_offset = t.time_us / 1e6 - time.monotonic()

            await asyncio.gather(_pos(), _time())
        except Exception as e:
            print(f"[px4] 中斷（{type(e).__name__}: {e}），5 秒後重連", flush=True)
            await asyncio.sleep(5)


# ── 緩衝（與 fake-onboard-node 同構，實測過的先落盤再送）──────
class Buffer:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS samples (
            seq INTEGER PRIMARY KEY, payload TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL)""")
        self.db.commit()
        row = self.db.execute("SELECT max(seq) FROM samples").fetchone()
        self.next_seq = (row[0] or 0) + 1

    def append(self, sample: dict) -> dict:
        sample["seq"] = self.next_seq
        self.db.execute("INSERT INTO samples (seq, payload, ts) VALUES (?, ?, ?)",
                        (self.next_seq, json.dumps(sample), time.time()))
        self.db.commit()                                # 先落盤，再由呼叫端送出
        self.next_seq += 1
        return sample

    def unsent(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload FROM samples WHERE sent=0 ORDER BY seq LIMIT ?", (limit,))
        return [json.loads(r[0]) for r in rows]

    def mark_sent(self, seqs: list[int]) -> None:
        self.db.executemany("UPDATE samples SET sent=1 WHERE seq=?", [(s,) for s in seqs])
        self.db.commit()

    def pending(self) -> int:
        return self.db.execute("SELECT count(*) FROM samples WHERE sent=0").fetchone()[0]

    def prune(self) -> None:
        cutoff = time.time() - BUFFER_KEEP_SENT_DAYS * 86400
        self.db.execute("DELETE FROM samples WHERE sent=1 AND ts < ?", (cutoff,))
        self.db.commit()


# ── HTTP（標準庫即可，1Hz 的量丟進執行緒足夠）─────────────────
async def to_thread(fn, *args):
    """asyncio.to_thread 是 3.9 才有；用 executor 相容到 3.7。"""
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(fn, *args)
    return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args))


def _ping_once(host: str):
    return subprocess.run(["ping", "-c", "1", "-W", "1", host],
                          capture_output=True, text=True)


def _post(url: str, payload: dict, timeout: float = 3.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else None


async def post_json(url: str, payload: dict, timeout: float = 3.0):
    return await to_thread(_post, url, payload, timeout)


# ── 主迴圈 ───────────────────────────────────────────────────
async def run() -> None:
    if not GROUND_API:
        sys.exit("GROUND_API 未設定（如 http://192.168.55.10:38000）")
    modem = Modem(AT_PORT, AT_BAUD)
    print(f"[modem] {AT_PORT}@{AT_BAUD} 開啟；ATI → "
          f"{modem.cmd('ATI', 0.3).strip()[:60]!r}", flush=True)
    buf = Buffer(BUFFER_PATH)
    print(f"[buffer] {BUFFER_PATH}（待補傳 {buf.pending()} 筆）", flush=True)
    px4 = Px4State()
    px4_task = asyncio.create_task(px4_watcher(px4))
    _keep = [px4_task]                     # 保留強參照，防 task 被 GC

    ping_results: list[bool] = []
    n = 0
    while True:
        t0 = time.monotonic()

        # 1) modem RF 指標
        m = await to_thread(modem.cmd, 'AT+QENG="servingcell"')
        sample = parse_qeng(m)

        # 2) RTT／丟包（ping 對頻寬影響極小；吞吐量的主動測試刻意不做——
        #    它會消耗被量測的頻寬本身，見設計文件）
        rtt = None
        if PING_HOST:
            out = await to_thread(_ping_once, PING_HOST)
            rtt = parse_ping(out.stdout) if out.returncode == 0 else None
        ping_results.append(rtt is not None)
        ping_results[:] = ping_results[-PING_LOSS_WINDOW:]
        loss = 100.0 * (1 - sum(ping_results) / len(ping_results))

        # 3) 位置與時間在採樣當下綁定（同一時鐘、同一瞬間，補傳不影響正確性）
        sample.update({
            "time": px4.now_iso(),
            "lat": px4.lat, "lon": px4.lon, "alt_rel": px4.alt_rel,
            "rtt_ms": rtt, "packet_loss_pct": round(loss, 1),
        })
        sample = buf.append(sample)                     # ← 先落盤

        # 4) 即時通道：只送最新一筆，失敗放棄
        try:
            await post_json(f"{GROUND_API}/api/link-metrics/live", sample)
        except Exception:
            pass

        # 5) 記錄通道：補傳所有未確認的
        pending = buf.unsent()
        if pending:
            try:
                body = {"samples": pending}
                if DRONE_ID:
                    body["drone_id"] = DRONE_ID
                res = await post_json(f"{GROUND_API}/api/link-metrics/batch", body)
                buf.mark_sent(res["accepted_seq"])
            except Exception as e:
                if n % 10 == 0:
                    print(f"[batch] 送出失敗（{type(e).__name__}），"
                          f"累積 {buf.pending()} 筆待補傳", flush=True)

        n += 1
        if n % 600 == 0:
            buf.prune()
        await asyncio.sleep(max(0.0, 1.0 / SAMPLE_HZ - (time.monotonic() - t0)))


# ── 上機首驗 ─────────────────────────────────────────────────
def probe() -> None:
    """印出 modem 原始回應。把整段輸出貼回開發端，校準解析器。"""
    modem = Modem(AT_PORT, AT_BAUD)
    for at in ("ATI", 'AT+QENG="servingcell"', "AT+CSQ", "AT+QNWINFO"):
        print(f"\n>>> {at}")
        print(modem.cmd(at, 0.8))
    print("\n（把以上輸出完整貼回，用於校準 parse_qeng）")


def selftest() -> None:
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
            asyncio.run(run())
        except KeyboardInterrupt:
            sys.exit(130)
