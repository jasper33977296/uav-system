#!/usr/bin/env python3
"""認領以板號為鍵，不以 is_primary（issues/040 A4）。

**2026-08-24 出過事**：一筆早已停用的舊記錄仍是主機、`mav_sysid` 空著，
新接上的機一開機就被認領進那筆記錄，`/api/live` 顯示的是**別台機的名字**
——而全程只有一行 log。

問題不在那條規則寫錯，**在它用錯了鍵**：`is_primary` 是「哪一台是主要顯示
對象」，它從來就不是身分。拿一個排版設定去做身分判斷，遲早會認錯機。

四件事：
  1. **主機不再吸走新號碼**（那條路整個沒了）
  2. 配號登錄說這個號碼是誰的 → 歸給誰（**繞一層號碼，鍵仍是板號**）
  3. 板號註冊時**收養**同號碼的佔位記錄，不長出第二筆
  4. **只收養沒有板號的**——有板號的那筆已經有身分，蓋掉等於把兩台機併成一台

跑法（要在 backend 容器內，asyncpg 只在那裡）：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-claim-by-board.py
"""
import asyncio
import os
import sys

import asyncpg

from app import db

P = "claimtest-"
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


async def mk(name, **kw):
    cols = "name, serial_no, is_simulated, status"
    vals = "$1, $1, true, 'idle'"
    args = [P + name]
    for i, (k, v) in enumerate(kw.items(), start=2):
        cols += f", {k}"
        vals += f", ${i}"
        args.append(v)
    return await db.pool.fetchval(
        f"INSERT INTO drones ({cols}) VALUES ({vals}) RETURNING id::text", *args)


async def main():
    # **整個測試跑在一個一定會回滾的交易裡。** 兩個好處：
    #   (1) 什麼都不會留下——不必靠結尾的 DELETE 收拾，而結尾的收拾在測試中途
    #       失敗時本來就不會執行；
    #   (2) 可以暫時動到真實記錄（例如 `is_primary` 有唯一索引，要測那條路就得
    #       先把現有的主機讓開）——**而真實記錄本來就不該被測試改到**。
    # `db.pool` 只用到 fetchrow／fetchval／execute／fetch，Connection 的 API
    # 與 Pool 相同，所以直接換掉即可，被測函式全部進到同一個交易。
    conn = await asyncpg.connect(os.environ.get("DATABASE_URL"))
    tr = conn.transaction()
    await tr.start()
    db.pool = conn
    try:
        # 讓開現有的主機（只在這個交易裡；回滾後原樣）
        await conn.execute("UPDATE drones SET is_primary = false WHERE is_primary")
        print("── 1. 主機不再吸走新號碼（2026-08-24 那條路）──────────")
        # 一筆「停用的舊主機」：is_primary 但 mav_sysid 空著——正是出事那筆的形狀
        old_id = await mk("停用的舊主機", is_primary=True)
        did, name = await db.drone_for_sysid(180)
        chk("**沒有被認領進舊主機**", did != old_id, name)
        chk("而是自動建了佔位記錄", name == "uav-s180", name)
        still = await db.pool.fetchval(
            "SELECT mav_sysid FROM drones WHERE id = $1::uuid", old_id)
        chk("舊主機的 mav_sysid 仍然是空的（沒被動過）", still is None, still)

        print("\n── 2. 配號登錄說是誰的，就歸給誰 ──────────────────────")
        reg_id = await mk("已配號的機", board_uid=P + "boardX", assigned_sysid=181)
        did2, name2 = await db.drone_for_sysid(181)
        chk("依配號登錄歸戶（不是新建一台）", did2 == reg_id, name2)
        got = await db.pool.fetchval(
            "SELECT mav_sysid FROM drones WHERE id = $1::uuid", reg_id)
        chk("順手把 mav_sysid 補上", got == 181, got)

        print("\n── 3. 板號註冊收養佔位記錄，不長出第二筆 ──────────────")
        before = await db.pool.fetchval(
            "SELECT count(*) FROM drones WHERE serial_no LIKE $1", P + "%")
        did3, name3, created = await db.ensure_drone_by_board(
            P + "boardY", claimed_sysid=180)
        after = await db.pool.fetchval(
            "SELECT count(*) FROM drones WHERE serial_no LIKE $1", P + "%")
        chk("收養而不是新建（記錄數不變）", after == before, f"{before} → {after}")
        chk("收養的正是那筆佔位記錄", name3 == "uav-s180", name3)
        chk("created=False（它不是新機）", created is False)

        print("\n── 4. 反向驗證：**不收養已經有板號的記錄** ─────────────")
        # 那等於把兩台機併成一台——09-01 的 SITL 事件就是這種合併的鏡像
        did4, name4, created4 = await db.ensure_drone_by_board(
            P + "boardZ", claimed_sysid=181)   # 181 那筆已有 boardX
        chk("沒有蓋掉 boardX 那筆", did4 != reg_id, name4)
        chk("而是新建一筆（誠實：這是我們沒見過的板子）", created4 is True, name4)
        keep = await db.pool.fetchval(
            "SELECT board_uid FROM drones WHERE id = $1::uuid", reg_id)
        chk("**原本那筆的板號沒有被改寫**", keep == P + "boardX", keep)
    finally:
        await tr.rollback()          # **一定回滾**：測試不留下任何痕跡
        await conn.close()

    print("\n" + ("全部通過" if ok else "**有未通過項目**"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
