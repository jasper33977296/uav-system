#!/usr/bin/env python3
"""把 `link_metrics` 裡的哨兵值 SINR 改成 NULL。**預設只看不動。**

## 這些值是怎麼來的

模組在受限服務（`LIMSRV`）下會把 SINR 回成一個無效標記。實測那一筆的原始
回應是：

    +QENG: "servingcell","LIMSRV","NR5G-SA","TDD",...,-95,-12,-3276,1,-

RSRP（−95）與 RSRQ（−12）是好的，**只有 SINR 是「沒有值」**。代理照抄成一個
數字送上來、我方照單全收存進資料庫，於是場域訊號頁的弱區標籤寫著
「最差 −3276 dB」，而那一格的最差值從此永遠是它——一個從來沒有人量到的數字，
壓過了所有真的量到的。

未來的入口已經擋住（`modem_raw.drop_sentinels`，兩條 push 路徑都過）。
**這支腳本處理的是已經寫進去的那幾筆。**

## 改什麼、不改什麼

* **只把超出量測範圍的那個欄位改成 NULL**，同一列的其他欄位一個都不動——
  RSRP 是真的量到的，沒有理由陪葬。
* **改掉的值寫進 `raw._dropped`**，形狀與 `drop_sentinels` 一致。
  悄悄丟掉與當成真值一樣糟：事後要查「這一筆為什麼沒有 SINR」，
  答案要在資料裡，不是在某個人的記憶裡。
* **不刪列。** 那一筆採樣真的發生過，位置與 RSRP 都還有用。

用法（**在 backend 容器內跑**，asyncpg 只裝在那裡）：

    docker exec -i -w /srv uav-backend python3 - < scripts/null-sentinel-sinr.py
    docker exec -i -w /srv uav-backend python3 - --apply < scripts/null-sentinel-sinr.py

改動前一律先把要動的列**匯出成 JSON**（`--out`）——不可逆的操作要留下可以
還原的東西，而這幾筆的原值只存在於這裡。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

import asyncpg

#: 與 app/modem_raw.py 的 SANE_RANGE 同一把尺。**這裡刻意複寫一份而不 import**：
#: 這支腳本是一次性的資料修正，它要記住的是「當時用的是哪個範圍」——
#: 之後那份範圍改了，這支腳本做過什麼不該跟著變。
SANE_RANGE = {
    "sinr": (-30.0, 40.0),
    "rsrp": (-156.0, -20.0),
    "rsrq": (-45.0, 10.0),
}
TZ = ZoneInfo("Asia/Taipei")

WHERE = " OR ".join(
    f"({f} IS NOT NULL AND ({f} < {lo} OR {f} > {hi}))"
    for f, (lo, hi) in SANE_RANGE.items())
SQL = f"""
SELECT time, drone_id::text AS drone_id, session_id::text AS session_id,
       lat, lon, alt_rel, rsrp, rsrq, sinr, cqi, source, raw
  FROM link_metrics
 WHERE {WHERE}
 ORDER BY time
"""


def offending(row: asyncpg.Record) -> dict[str, float]:
    out: dict[str, float] = {}
    for f, (lo, hi) in SANE_RANGE.items():
        v = row[f]
        if v is not None and (float(v) < lo or float(v) > hi):
            out[f] = float(v)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="真的改（不給就只列出，什麼都不動）")
    ap.add_argument("--out", help="備份檔路徑（預設 data/sentinel-sinr-<時間>.json）")
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL", "postgresql://uav:uav@localhost:35432/uav"))
    a = ap.parse_args()

    pool = await asyncpg.create_pool(a.dsn, min_size=1, max_size=2)
    rows = await pool.fetch(SQL)
    if not rows:
        print("沒有任何欄位超出量測範圍——不用改。")
        await pool.close()
        return 0

    print(f"{len(rows)} 筆有欄位超出量測範圍（時間為台北時間）：\n")
    for r in rows:
        bad = offending(r)
        t = r["time"].astimezone(TZ).strftime("%m/%d %H:%M:%S")
        pos = "無座標" if r["lat"] is None else f"{r['lat']:.6f},{r['lon']:.6f}"
        print(f"  {t}  {r['session_id'][:8]}  {pos}"
              f"  要改成 NULL：{bad}"
              f"  同列保留：rsrp={r['rsrp']} rsrq={r['rsrq']}")

    stamp = dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    out = pathlib.Path(a.out or f"data/sentinel-sinr-{stamp}.json")
    if not a.apply:
        print(f"\n（沒有 --apply，什麼都沒改。備份會寫到 {out}）")
        await pool.close()
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    dump = []
    for r in rows:
        d = dict(r)
        d["time"] = d["time"].isoformat()
        raw = d.get("raw")
        d["raw"] = json.loads(raw) if isinstance(raw, str) else raw
        d["_will_null"] = offending(r)
        dump.append(d)
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n原值已備份到 {out}")

    changed = 0
    async with pool.acquire() as con:
        async with con.transaction():
            for r in rows:
                bad = offending(r)
                raw = r["raw"]
                raw = json.loads(raw) if isinstance(raw, str) else (raw or {})
                raw = {**raw, "_dropped": {**raw.get("_dropped", {}), **bad}}
                sets = ", ".join(f"{f} = NULL" for f in bad)
                await con.execute(
                    f"UPDATE link_metrics SET {sets}, raw = $3::jsonb "
                    "WHERE drone_id = $1 AND time = $2",
                    r["drone_id"], r["time"], json.dumps(raw, ensure_ascii=False))
                changed += 1
    print(f"已改 {changed} 筆。")

    left = await pool.fetchval(f"SELECT count(*) FROM link_metrics WHERE {WHERE}")
    print(f"複查：還有 {left} 筆超出量測範圍（應為 0）。")
    await pool.close()
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
