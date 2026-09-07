#!/usr/bin/env python3
"""把 `link_metrics` 裡「有 raw、沒有欄位」的 serving cell 值解出來補回去。

**預設只看不動；而且改得回來。**

## 為什麼有這一支

`pci`／`cell_id`／`band` 三欄在今天的資料裡全是 null，**而值一直都在 `raw`
裡**（`AT+GTCCINFO?` 的原始回應）。畫面上那三格顯示「—」，讀的人會以為
「這個場域量不到細胞資訊」——而事實是我們收到了、沒有解。

`reference/fibocom-fm160/README.md` 當初決定「兩個指令的原始回應整包存進
`LinkSample.raw`」，理由就是這個：**對照表日後若有修正，可以拿歷史資料回頭
重算，不必重飛**。這支腳本就是那句話的兌現。

新資料已在入庫時解好（`app/modem_raw.py`，live 與 batch 兩條路都接）；
這裡處理的是那之前寫進去的列。

## 規則

* **只補 null 的欄位。** 機上代理自己填的才是第一手，不覆蓋。
* **解不開就不填**：欄位數不足、進位讀不了、PCI 超出 0–1007 一律略過。
  填一個看似合理的錯值，比空著更難發現。
* **留下痕跡**：補過的列在 `raw._derived` 記下規則版本與補了哪幾欄，
  所以 `--revert` 可以精準地只清掉我方算出來的值。

用法（在 backend 容器內，asyncpg 只裝在那裡）：

    docker exec -i -w /srv uav-backend python3 - < scripts/backfill-link-cell.py
    docker exec -i -w /srv uav-backend python3 - --apply  < scripts/backfill-link-cell.py
    docker exec -i -w /srv uav-backend python3 - --revert < scripts/backfill-link-cell.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

sys.path.insert(0, "/srv")
from app.modem_raw import RULE, parse_gtccinfo    # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真的寫回去")
    ap.add_argument("--revert", action="store_true",
                    help="把本腳本補過的欄位清回 null（依 raw._derived 判斷）")
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL", "postgresql://uav:uav@localhost:35432/uav"))
    a = ap.parse_args()

    pool = await asyncpg.create_pool(a.dsn, min_size=1, max_size=2)

    if a.revert:
        rows = await pool.fetch(
            "SELECT time, drone_id, raw FROM link_metrics "
            "WHERE raw ? '_derived' ORDER BY time")
        print(f"{len(rows)} 列是本腳本補過的。")
        if not a.apply:
            print("加 --apply 才會真的清回 null。")
            await pool.close()
            return 0
        n = 0
        for r in rows:
            raw = json.loads(r["raw"]) if isinstance(r["raw"], str) else r["raw"]
            fields = (raw.get("_derived") or {}).get("fields") or []
            raw.pop("_derived", None)
            sets = ", ".join(f"{f} = NULL" for f in fields if f in
                             ("pci", "cell_id", "band"))
            await pool.execute(
                f"UPDATE link_metrics SET raw = $3::jsonb{',' + sets if sets else ''} "
                "WHERE time = $1 AND drone_id = $2",
                r["time"], r["drone_id"], json.dumps(raw, ensure_ascii=False))
            n += 1
        print(f"已還原 {n} 列。")
        await pool.close()
        return 0

    rows = await pool.fetch(
        """SELECT time, drone_id, pci, cell_id, band, raw FROM link_metrics
            WHERE raw IS NOT NULL AND raw::text LIKE '%GTCCINFO%'
              AND (pci IS NULL OR cell_id IS NULL OR band IS NULL)
            ORDER BY time""")
    if not rows:
        print("沒有可補的列——欄位要嘛已經有值，要嘛 raw 裡沒有 GTCCINFO。")
        await pool.close()
        return 0

    plan, skipped = [], 0
    for r in rows:
        raw = json.loads(r["raw"]) if isinstance(r["raw"], str) else r["raw"]
        got = parse_gtccinfo((raw or {}).get("GTCCINFO") or "")
        if not got:
            skipped += 1
            continue
        fills = {k: got[k] for k in ("pci", "cell_id", "band")
                 if r[k] is None and got.get(k) is not None}
        if fills:
            plan.append((r["time"], r["drone_id"], fills, raw, got))

    print(f"{len(rows)} 列有 raw 且欄位缺；其中 {len(plan)} 列解得出來"
          f"{f'，{skipped} 列解不開（格式不符，略過）' if skipped else ''}。")
    if plan:
        _t, _d, f0, _raw, g0 = plan[0]
        print(f"  第一列示例：{dict(f0)}")
        print(f"  同筆另外解出（收進 raw._derived，不進欄位）："
              f"NR-ARFCN {g0['narfcn']}、TAC {g0['tac']}、"
              f"頻寬 {g0['bandwidth_mhz']} MHz")
        vals = {k: sorted({str(p[2].get(k)) for p in plan if k in p[2]})
                for k in ("pci", "cell_id", "band")}
        print(f"  出現過的值：{ {k: v for k, v in vals.items() if v} }")

    if not a.apply:
        print("\n乾跑：什麼都沒有動。確認以上再加 --apply。（補完可用 --revert --apply 還原）")
        await pool.close()
        return 0

    n = 0
    async with pool.acquire() as con:
        async with con.transaction():
            for t, did, fills, raw, got in plan:
                raw["_derived"] = {"rule": RULE, "fields": list(fills),
                                   "narfcn": got.get("narfcn"),
                                   "tac": got.get("tac"),
                                   "bandwidth_mhz": got.get("bandwidth_mhz"),
                                   "gtcc_idx": got.get("gtcc_idx"),
                                   "gtcc_db": got.get("gtcc_db")}
                sets = ", ".join(f"{k} = ${i + 4}" for i, k in enumerate(fills))
                await con.execute(
                    f"UPDATE link_metrics SET raw = $3::jsonb, {sets} "
                    "WHERE time = $1 AND drone_id = $2",
                    t, did, json.dumps(raw, ensure_ascii=False), *fills.values())
                n += 1
    print(f"已補 {n} 列。要還原：--revert --apply")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
