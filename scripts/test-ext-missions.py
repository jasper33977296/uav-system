#!/usr/bin/env python3
"""外部起飛時的任務建立（apps/command/app/missions.py）。

只建臨時機與臨時任務，跑完刪掉；不送任何指令、不碰飛機。

用法（用 command 映像、掛上工作樹的原始碼、接同一個資料庫）：
  docker run --rm -i --network <compose 網路> -e DATABASE_URL=<同 uav-command> \
    -v "$PWD/apps/command/app:/srv/app:ro" <command 映像> python - < scripts/test-ext-missions.py
"""
import asyncio
import os
import uuid

import asyncpg

from app import missions as mx

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


async def expect(label, coro, status, code, extra=lambda e: True):
    try:
        r = await coro
        chk(label, False, f"沒有被擋：{r}")
        return r
    except mx.MissionError as e:
        chk(label, e.status == status and e.code == code and e.msg and extra(e),
            f"{e.status} {e.code} {e.msg}")


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    tag = uuid.uuid4().hex[:6]
    a = await pool.fetchval("INSERT INTO drones (name, connection_url) VALUES ($1, 'test://') "
                            "RETURNING id::text", f"zz-test-mx-a-{tag}")
    b = await pool.fetchval("INSERT INTO drones (name, connection_url) VALUES ($1, 'test://') "
                            "RETURNING id::text", f"zz-test-mx-b-{tag}")
    try:
        try:
            mx.parse_id("not-a-uuid")
            chk("不是 UUID → 422", False)
        except mx.MissionError as e:
            chk("不是 UUID → 422 mission_id_invalid，訊息帶收到的值",
                e.status == 422 and e.code == "mission_id_invalid" and "not-a-uuid" in e.msg)
        chk("沒給 mission_id 不算錯", mx.parse_id(None) is None)

        mid = str(uuid.uuid4())
        m1 = await mx.ensure(pool, mid, f"zz-test-mx-{tag}", [(a, "A")], "plan")
        ext = await pool.fetchval("SELECT external FROM missions WHERE id = $1::uuid", mid)
        chk("用呼叫端的 UUID 建立，標成外部建立",
            m1 == {"id": mid, "name": f"zz-test-mx-{tag}", "created": True} and ext is True, m1)

        m2 = await mx.ensure(pool, mid, None, [(b, "B")], "plan")
        n = await pool.fetchval("SELECT count(*) FROM mission_drones WHERE mission_id = $1::uuid", mid)
        chk("同一個 UUID 再起飛另一台 → 掛進同一個任務", m2["created"] is False and n == 2, (m2, n))

        m3 = await mx.ensure(pool, None, None, [(a, "A")], "plan")
        chk("沒給 UUID、機已在進行中的任務 → 用那個任務", m3["id"] == mid and m3["created"] is False, m3)

        await expect("另一個 UUID 起飛同一台 → 409 mission_busy，說得出是哪個任務",
                     mx.ensure(pool, str(uuid.uuid4()), None, [(a, "A")], "plan"),
                     409, "mission_busy", lambda e: mid in e.msg and e.how_to)
        await expect("名稱撞既有任務（不分大小寫）→ 409 mission_name_taken",
                     mx.ensure(pool, str(uuid.uuid4()), f"ZZ-TEST-MX-{tag}", [], "plan"),
                     409, "mission_name_taken")

        await pool.execute("UPDATE missions SET ended_at = now() WHERE id = $1::uuid", mid)
        await expect("任務已結束 → 409 mission_ended，並說要換新的 UUID",
                     mx.ensure(pool, mid, None, [(a, "A")], "plan"),
                     409, "mission_ended", lambda e: e.how_to)

        plan = f"zz-test-mx-plan-{tag}"
        m4 = await mx.ensure(pool, None, None, [(a, "A")], plan)
        await pool.execute("UPDATE missions SET ended_at = now() WHERE id = $1::uuid", m4["id"])
        m5 = await mx.ensure(pool, None, None, [(b, "B")], plan)
        chk("自動命名＝路徑名＋時間，撞名就再加秒",
            m4["name"].startswith(plan + " ") and m5["name"].startswith(plan + " ")
            and m4["name"] != m5["name"], (m4["name"], m5["name"]))
    finally:
        await pool.execute("DELETE FROM missions WHERE name ILIKE $1", f"zz-test-mx-%{tag}%")
        await pool.execute("DELETE FROM drones WHERE id = ANY($1::uuid[])", [a, b])
        left = await pool.fetchval(
            "SELECT (SELECT count(*) FROM missions WHERE name ILIKE $1) + "
            "(SELECT count(*) FROM drones WHERE name ILIKE $1)", f"zz-test-mx-%{tag}%")
        chk("臨時機與臨時任務都清掉了", left == 0, left)
        await pool.close()


asyncio.run(main())
print("\n全部通過" if ok else "\n有項目沒過")
raise SystemExit(0 if ok else 1)
