#!/usr/bin/env python3
"""把「本來就不該被插進來」的補傳遙測列刪掉。**預設只看不動。**

## 這些列是怎麼來的

`telemetry` 有兩個來源：即時串流（直接來自飛控的封包）與機上補傳（代理在
斷線期間緩衝、恢復後補送）。補傳端點本來就有去重，但它比對的是
`round(t, 2)` ——而兩條路的百分秒天生不同：

    即時  時間戳是「收到封包的時刻」  .775 .784 .795⋯逐筆漂移
    補傳  機上 1Hz 取樣的刻度         整齊的 .51

**永遠不會落在同一個百分秒，所以每一筆補傳都被當成新資料插進去**（d6dea0b，
2026-09-07 21:06 改成時間窗去重、即時優先）。修法只擋住未來——已經寫進去的
列還在，而它們與正確的即時列**一比一交錯**：飛機正在 3 m 空中的那 20 秒裡，
夾著「LOITER、高度 −0.5 m、機在地上」的樣本。事後看那段資料，兩種互相
矛盾的說法長得一樣可信。

## 這支腳本刪什麼、不刪什麼

**只刪「今天的規則不會再插入」的那些列**：一筆補傳樣本，若 ±0.6 秒內有
即時樣本，它今天根本進不來——那就是判準，與 `api.py` 的 `DEDUP_WINDOW_S`
同一把尺。

**不碰真正補回缺口的那些列。** 補傳存在的理由就是斷線那段沒有即時資料，
那些列是這個系統唯一的紀錄，刪掉就真的沒有了。實測 2026-09-07：六趟受影響
的架次裡有 462 筆補傳，其中只有 127 筆撞到即時資料——其餘 335 筆是真的缺口。

**不改任何一列的內容。** 要嘛整列刪掉、要嘛原樣留著：把補傳列「修正」成
即時列的值，等於製造一筆從來沒有人量到的資料。

用法（**在 backend 容器內跑**，asyncpg 只裝在那裡——同 scripts/test-*.py 慣例）：

    docker exec -i -w /srv uav-backend python3 - < scripts/clean-backfill-conflicts.py
    docker exec -i -w /srv uav-backend python3 - --apply < scripts/clean-backfill-conflicts.py

沒有 `--apply` 就只列出，什麼都不動。備份檔寫在容器的 `/srv/data/`，
容器沒掛那個目錄時用 `--out /data/mavcap/...`（mavcap 是掛出來的）。

刪除前一律先把要刪的列**匯出成 JSON**（`--out`，預設 `data/backfill-conflicts-<時間>.json`）
——**不可逆的操作要留下可以還原的東西**，而 DB 的保留期只有 30 天。
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

WINDOW_S = 0.6          # 與 api.py 的 DEDUP_WINDOW_S 同一把尺
#: 時間一律印台北時間並標明。**容器的 TZ 是 UTC**，直接 astimezone() 會印出
#: 一個與畫面上差八小時的時刻——對著資訊頁核對的人會以為看的是別一趟。
TZ = ZoneInfo("Asia/Taipei")

#: 撞到即時樣本的補傳列。`LATERAL` 取時間上最近的那一筆即時列，用來算差多遠。
SQL = """
SELECT b.session_id::text AS session_id,
       b.time, b.lat, b.lon, b.alt_rel, b.flight_mode,
       l.time AS lv_time, l.lat AS lv_lat, l.lon AS lv_lon,
       l.alt_rel AS lv_alt, l.flight_mode AS lv_mode,
       CASE WHEN b.lat IS NULL OR l.lat IS NULL THEN NULL
            ELSE 111320 * sqrt((l.lat - b.lat) ^ 2
                 + ((l.lon - b.lon) * cos(radians(l.lat))) ^ 2) END AS gap_m
  FROM telemetry b
  JOIN LATERAL (
       SELECT time, lat, lon, alt_rel, flight_mode FROM telemetry l
        WHERE l.drone_id = b.drone_id AND NOT l.backfilled
          AND l.time BETWEEN b.time - ($1 || ' s')::interval
                         AND b.time + ($1 || ' s')::interval
        ORDER BY abs(extract(epoch FROM l.time - b.time)) LIMIT 1
  ) l ON true
 WHERE b.backfilled
   AND ($2::uuid IS NULL OR b.session_id = $2::uuid)
 ORDER BY b.time
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="真的刪除（不給就只列出，什麼都不動）")
    ap.add_argument("--session", help="只處理這一趟")
    ap.add_argument("--out", help="備份檔路徑（預設 data/backfill-conflicts-<時間>.json）")
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL", "postgresql://uav:uav@localhost:35432/uav"))
    a = ap.parse_args()

    pool = await asyncpg.create_pool(a.dsn, min_size=1, max_size=2)
    rows = await pool.fetch(SQL, str(WINDOW_S), a.session)

    if not rows:
        print("沒有任何補傳列撞到即時資料——不用清。")
        await pool.close()
        return 0

    # 逐趟摘要：**先讓人看見要動什麼，再問要不要動**
    per: dict[str, list] = {}
    for r in rows:
        per.setdefault(r["session_id"], []).append(r)
    print(f"{len(rows)} 筆補傳列落在即時資料的 ±{WINDOW_S} 秒內"
          f"（{len(per)} 趟，時間為台北時間）：\n")
    for sid, rs in per.items():
        gaps = [r["gap_m"] for r in rs if r["gap_m"] is not None]
        modes = sum(1 for r in rs if r["flight_mode"] != r["lv_mode"])
        t0 = rs[0]["time"].astimezone(TZ).strftime("%m/%d %H:%M:%S")
        t1 = rs[-1]["time"].astimezone(TZ).strftime("%H:%M:%S")
        line = f"  {sid[:8]}  {len(rs):>4} 筆  {t0}–{t1}"
        if gaps:
            line += f"  位置最遠差 {max(gaps):.1f} m"
        print(line)
        if modes:
            print(f"            其中 {modes} 筆連飛行模式都與即時那筆不同"
                  f"（補傳說 {rs[0]['flight_mode']}、即時說 {rs[0]['lv_mode']}）")

    if not a.apply:
        print("\n乾跑：什麼都沒有動。確認以上是你要刪的，再加 --apply。")
        await pool.close()
        return 0

    # **先備份再刪。** 保留期只有 30 天，刪掉就真的沒有了
    out = pathlib.Path(a.out or ("data/backfill-conflicts-"
                                 + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([{k: (v.isoformat() if isinstance(v, dt.datetime) else v)
                                for k, v in dict(r).items()} for r in rows],
                              ensure_ascii=False, indent=1), encoding="utf8")
    print(f"\n備份 {len(rows)} 筆 → {out}")

    # 刪除鍵＝(session_id, time, backfilled)：補傳列的時間戳是機上刻度，
    # 同一趟裡不會重複；即時列不在條件內，誤刪不了
    n = 0
    async with pool.acquire() as con:
        async with con.transaction():
            for r in rows:
                res = await con.execute(
                    "DELETE FROM telemetry WHERE session_id = $1::uuid "
                    "AND time = $2 AND backfilled", r["session_id"], r["time"])
                n += int(res.split()[-1])
    print(f"已刪除 {n} 列。剩下的補傳列是真的缺口，原樣留著。")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
