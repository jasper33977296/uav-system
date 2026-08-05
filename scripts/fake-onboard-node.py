#!/usr/bin/env python3
"""模擬機上 ROS node，用來驗證地面站的機上量測回傳管線。

真機上這支程式會是 RB5 上的 ROS 2 node：讀 Quectel RM500Q-GL 的
`AT+QENG="servingcell"`、從 PX4 取位置、量 RTT，然後照本檔的協定送回地面站。
這裡把「讀 modem」換成模擬器產生的數值，其餘行為（持久化緩衝、兩條通道、
斷線重送、批次確認）都與真機一致，因此可以拿來驗證地面站側是否正確。

設計見 doc/onboard-telemetry.md。

用法：
    apps/backend/.venv/bin/python scripts/fake-onboard-node.py
    apps/backend/.venv/bin/python scripts/fake-onboard-node.py --outage 30:20

    --outage START:LEN   起動後第 START 秒開始，模擬 LEN 秒的鏈路中斷
                         （即時通道全丟、記錄通道送不出去），用來驗證補傳
"""
import argparse
import asyncio
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "backend"))

import websockets

from app.link_sim import SimulatedLinkSource

API = "http://localhost:38000"
BUFFER_DB = Path(__file__).resolve().parent.parent / ".onboard-buffer.sqlite3"
_keepalive: list = []   # 保留背景 task 的強參照，見 main()


# 用標準庫而非 httpx／requests：這是開發工具，不值得為它在 requirements.txt
# 增加 production 相依。1 Hz 的請求量下，把阻塞呼叫丟進執行緒完全足夠。
def _blocking_request(url: str, payload: dict | None, timeout: float):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else None


async def post_json(url: str, payload: dict, timeout: float = 3.0):
    return await asyncio.to_thread(_blocking_request, url, payload, timeout)


async def get_json(url: str, timeout: float = 5.0):
    return await asyncio.to_thread(_blocking_request, url, None, timeout)


class Buffer:
    """機上持久化緩衝。真機用同樣的結構，差別只在跑在 RB5 上。

    關鍵是**先落盤再嘗試送出**——順序顛倒的話，程式在送出後、寫入前被中斷
    就會遺失那一筆，而飛行中被中斷並不罕見。
    """

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS samples (
              seq INTEGER PRIMARY KEY,
              payload TEXT NOT NULL,
              sent INTEGER NOT NULL DEFAULT 0
            )""")
        self.db.commit()
        row = self.db.execute("SELECT max(seq) FROM samples").fetchone()
        self.next_seq = (row[0] or 0) + 1

    def append(self, sample: dict) -> dict:
        sample["seq"] = self.next_seq
        self.db.execute("INSERT INTO samples (seq, payload) VALUES (?, ?)",
                        (self.next_seq, json.dumps(sample)))
        self.db.commit()                      # 先落盤，再由呼叫端嘗試送出
        self.next_seq += 1
        return sample

    def unsent(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload FROM samples WHERE sent = 0 ORDER BY seq LIMIT ?", (limit,))
        return [json.loads(r[0]) for r in rows]

    def mark_sent(self, seqs: list[int]) -> None:
        self.db.executemany("UPDATE samples SET sent = 1 WHERE seq = ?",
                            [(s,) for s in seqs])
        self.db.commit()

    def pending(self) -> int:
        return self.db.execute("SELECT count(*) FROM samples WHERE sent = 0").fetchone()[0]


async def watch_position(state: dict) -> None:
    """從地面站的 WebSocket 取位置。真機上這裡改成從 PX4 讀（同一台機器）。

    例外要印出來，不要靜默吞掉——這支程式的用途就是診斷，
    看不見的失敗會讓人誤判成「地面站沒收到資料」。
    """
    while True:
        try:
            async with websockets.connect("ws://localhost:38000/ws/telemetry") as ws:
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "telemetry":
                        state.update(lat=m.get("lat"), lon=m.get("lon"),
                                     alt_rel=m.get("alt_rel"), armed=m.get("armed"))
            print("  [位置] WebSocket 正常關閉，2 秒後重連", flush=True)
        except Exception as e:
            print(f"  [位置] WebSocket 中斷（{type(e).__name__}: {e}），2 秒後重連", flush=True)
        await asyncio.sleep(2)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outage", help="START:LEN，模擬鏈路中斷（秒）")
    args = ap.parse_args()
    outage_start, outage_len = (0, 0)
    if args.outage:
        outage_start, outage_len = (int(x) for x in args.outage.split(":"))

    buf = Buffer(BUFFER_DB)
    state: dict = {}
    # 必須保留 task 的參照：event loop 只持有弱參照，沒有強參照的 task
    # 可能被 GC 掉而中途消失——這正是先前位置停在起飛點不動的原因。
    watcher = asyncio.create_task(watch_position(state))
    _keepalive.append(watcher)

    cells = await get_json(f"{API}/api/cells")
    zones = [z for z in await get_json(f"{API}/api/zones") if z.get("enabled")]
    sim = SimulatedLinkSource(cells, zones)
    print(f"取得 {len(cells)} 個 cell、{len(zones)} 個干擾區；緩衝 {BUFFER_DB}", flush=True)

    t0 = time.monotonic()
    while True:
        await asyncio.sleep(1.0)
        elapsed = time.monotonic() - t0
        blackout = outage_len and outage_start <= elapsed < outage_start + outage_len

        if state.get("lat") is None:
            continue

        # 1. 採樣（真機：讀 AT+QENG + 從 PX4 取位置 + ping）
        m = sim.sample(state["lat"], state["lon"], state.get("alt_rel"))
        sample = {
            "time": datetime.now(timezone.utc).isoformat(),
            "lat": state["lat"], "lon": state["lon"], "alt_rel": state.get("alt_rel"),
            "rsrp": m["rsrp"], "rsrq": m["rsrq"], "sinr": m["sinr"], "cqi": m["cqi"],
            "pci": m["pci"], "cell_id": None, "band": m["band"], "nr_mode": m["nr_mode"],
            "rtt_ms": m["rtt_ms"], "jitter_ms": m["jitter_ms"],
            "packet_loss_pct": m["packet_loss_pct"],
            "throughput_up_kbps": m["throughput_up_kbps"],
            "throughput_down_kbps": m["throughput_down_kbps"],
            "in_interference_zone": m["in_interference_zone"],
            "raw": {"note": "fake-onboard-node"},
        }
        sample = buf.append(sample)       # 先落盤

        if blackout:
            print(f"  [{elapsed:5.0f}s] 模擬中斷中，累積 {buf.pending()} 筆待補傳", flush=True)
            continue

        # 2. 即時通道：只送最新一筆，失敗就放棄（下一秒會有新的取代它）
        try:
            await post_json(f"{API}/api/link-metrics/live", sample)
        except Exception:
            pass

        # 3. 記錄通道：送所有未確認的，成功才標記
        pending = buf.unsent()
        if pending:
            try:
                res = await post_json(f"{API}/api/link-metrics/batch", {"samples": pending})
                buf.mark_sent(res["accepted_seq"])
                if len(pending) > 1 or res["outside_session"]:
                    print(f"  [{elapsed:5.0f}s] 批次 {len(pending)} 筆 → "
                          f"入庫 {res['stored']}、重複 {res['duplicate']}、"
                          f"架次外 {res['outside_session']}", flush=True)
            except Exception as e:
                print(f"  [{elapsed:5.0f}s] 批次送出失敗（{type(e).__name__}），"
                      f"累積 {buf.pending()} 筆", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
