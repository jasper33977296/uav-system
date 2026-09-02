#!/usr/bin/env python3
"""配號登錄（issues/040 A1）：撞號不是僵局，是換一個號碼。

**識別的唯一鍵值是板號**（2026-09-02 使用者裁定），`sysid` 只是地址。
本測試釘住四種註冊情況與兩條邊界：

  1. 板號已登錄且號碼相符          → keep
  2. 板號已登錄但號碼不符          → change（改回登錄上那個）
  3. 板號沒見過、號碼沒人用        → keep（就配這個，機端不用動）
  4. 板號沒見過、號碼被別的板佔用  → change（配一個新的）
  邊界：255（我方 GCS）永不配出；號碼用盡要**明說**而不是靜默給重複的。

09-01 那個情境就是第 4 種：SITL 宣告 sysid 1，而 1 已配給真機的板號。

跑法（**要在 backend 容器內**，asyncpg 只在那裡）：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-sysid-allocation.py
"""
import asyncio
import os
import sys

import asyncpg

from app import db

PREFIX = "alloctest-"
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


async def mk(name, board, assigned=None):
    await db.pool.execute(
        "INSERT INTO drones (name, serial_no, is_simulated, status, "
        "board_uid, assigned_sysid) VALUES ($1,$1,true,'idle',$2,$3)",
        PREFIX + name, PREFIX + board, assigned)


async def main():
    db.pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL"), min_size=1, max_size=2)
    await db.pool.execute(
        "DELETE FROM drones WHERE serial_no LIKE $1", PREFIX + "%")
    try:
        await mk("已登錄", "boardA", 200)
        await mk("待配號", "boardB", None)

        print("── 1. 板號已登錄，號碼相符 → keep ─────────────────────")
        r = await db.allocate_sysid(PREFIX + "boardA", 200)
        chk("keep 且不改號", r["action"] == "keep" and r["sysid"] == 200, r["reason"])

        print("\n── 2. 板號已登錄，號碼不符 → change 回登錄的那個 ───────")
        r = await db.allocate_sysid(PREFIX + "boardA", 7)
        chk("change 且回登錄值 200（不是配新的）",
            r["action"] == "change" and r["sysid"] == 200, r["reason"])

        print("\n── 3. 板號沒見過、號碼沒人用 → 就配這個 ────────────────")
        r = await db.allocate_sysid(PREFIX + "boardB", 201)
        chk("keep（機端完全不用動）", r["action"] == "keep" and r["sysid"] == 201,
            r["reason"])

        print("\n── 4. 板號沒見過、號碼被別的板佔用 → 配新的（09-01 情境）──")
        r = await db.allocate_sysid(PREFIX + "boardB", 200)
        chk("change 且不是 200", r["action"] == "change" and r["sysid"] != 200,
            r["reason"])
        chk("理由說得出是被誰佔的", "已經配給板號" in r["reason"], r["reason"])
        chk("**配出來的號碼真的沒人用**",
            await db.pool.fetchval(
                "SELECT count(*) FROM drones WHERE assigned_sysid=$1",
                r["sysid"]) == 0, r["sysid"])

        print("\n── 邊界：255 是我方 GCS，永不配出 ─────────────────────")
        chk("號碼池上限是 254", db.SYSID_MAX == 254 and db.SYSID_GCS == 255)
        free = await db._next_free_sysid()
        chk("下一個可用號碼不是 255", free != 255, free)

        print("\n── 邊界：號碼用盡要明說，不是靜默給重複的 ──────────────")
        await db.pool.execute(
            "UPDATE drones SET assigned_sysid = NULL WHERE serial_no LIKE $1",
            PREFIX + "%")
        # 塞滿整個池（用測試機佔號，測完刪）。**只填還沒被佔的**——
        # 這台機的 DB 裡本來就有真機佔著 1 號
        rows = await db.pool.fetch(
            "SELECT assigned_sysid FROM drones WHERE assigned_sysid IS NOT NULL")
        held = {r["assigned_sysid"] for r in rows}
        await db.pool.executemany(
            "INSERT INTO drones (name, serial_no, is_simulated, status, "
            "board_uid, assigned_sysid) VALUES ($1,$1,true,'idle',$2,$3)",
            [(f"{PREFIX}fill{n}", f"{PREFIX}fb{n}", n)
             for n in range(db.SYSID_MIN, db.SYSID_MAX + 1) if n not in held])
        raised = None
        try:
            await db.allocate_sysid(PREFIX + "never-seen", None)
        except RuntimeError as e:
            raised = str(e)
        chk("用盡時丟出明確錯誤（不是靜默重複）", raised is not None, (raised or "")[:70])
        chk("錯誤訊息說得出該怎麼辦（釋放號碼）",
            raised and "釋放" in raised)

        print("\n── 反向驗證：唯一索引真的擋得住兩塊板共用一個號碼 ──────")
        dup, victim = None, None
        try:
            victim = await db.pool.fetchval(
                "SELECT serial_no FROM drones WHERE serial_no LIKE $1 "
                "AND assigned_sysid <> 254 LIMIT 1", PREFIX + "fill%")
            await db.pool.execute(
                "UPDATE drones SET assigned_sysid = 254 WHERE serial_no = $1",
                victim)
        except asyncpg.UniqueViolationError as e:
            dup = str(e)
        chk("（前置）真的抓到一台可以拿來撞的測試機", victim is not None, victim)
        chk("資料庫層擋下重複配號", dup is not None, (dup or "")[:60])
    finally:
        await db.pool.execute(
            "DELETE FROM drones WHERE serial_no LIKE $1", PREFIX + "%")
        await db.pool.close()

    print("\n" + ("全部通過" if ok else "**有未通過項目**"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
