#!/usr/bin/env python3
"""對外即時串流，打正在跑的 backend（doc/external-live-api.md）。

只用隨機 UUID、臨時機與臨時任務，跑完刪掉；起飛流程用 notify 模擬，不送任何指令、不碰飛機。
約 70 秒。

用法（backend 映像裡有 websockets 與 asyncpg）：
  docker run --rm -i --network host -e DATABASE_URL=postgresql://uav:uav@localhost:35432/uav \
    uav-system-uav-backend python - < scripts/test-ext-stream-live.py
"""
import asyncio
import json
import os
import time
import urllib.request
import uuid

import asyncpg
import websockets

WS = "ws://localhost:38000/ws/v1/missions"
API = "http://localhost:38000/api"
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}", flush=True)


def post(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


async def recv_until(ws, pred, timeout):
    got = []
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            m = json.loads(await asyncio.wait_for(ws.recv(), end - time.monotonic()))
            m["_t"] = time.monotonic()
            got.append(m)
            if pred(m):
                break
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        pass
    return got


async def closed_code(ws, timeout=3):
    try:
        await asyncio.wait_for(ws.wait_closed(), timeout)
    except asyncio.TimeoutError:
        return None
    return ws.close_code


async def invalid():
    async with websockets.connect(f"{WS}/not-a-uuid") as ws:
        got = await recv_until(ws, lambda m: False, 2)
        code = await closed_code(ws)
    chk("編號不是 UUID → 先送 error 再以 4400 關閉",
        got and got[0]["type"] == "error" and got[0]["code"] == "mission_id_invalid"
        and got[0]["msg"] and code == 4400, (got[:1], code))


async def waiting():
    mid = str(uuid.uuid4())
    async with websockets.connect(f"{WS}/{mid}") as ws:
        got = await recv_until(ws, lambda m: m["type"] == "ended", 34)
        code = await closed_code(ws)
    hello, states = got[0], [m for m in got if m["type"] == "state"]
    chk("hello 是 waiting、不帶序號", hello["type"] == "hello" and hello["phase"] == "waiting"
        and "seq" not in hello, hello)
    gaps = [b["_t"] - a["_t"] for a, b in zip(states, states[1:])]
    chk("等待中每 0.5 秒一則 state（最大間隔 < 0.8 秒）",
        len(states) >= 50 and max(gaps) < 0.8, (len(states), round(max(gaps), 3)))
    seqs = [m["seq"] for m in got if "seq" in m]
    chk("序號嚴格遞增", all(b > a for a, b in zip(seqs, seqs[1:])))
    ended = got[-1]
    chk("30 秒沒人起飛 → ended（never_started）並以 1000 關閉",
        ended["type"] == "ended" and ended["reason"] == "never_started" and ended["msg"]
        and code == 1000, (ended, code))
    async with websockets.connect(f"{WS}/{mid}") as ws:
        again = await recv_until(ws, lambda m: m["type"] == "ended", 3)
        code = await closed_code(ws)
    chk("結束後 30 秒內重連：拿得到 ended、再關閉",
        again and again[0]["phase"] == "ended" and again[-1]["type"] == "ended"
        and again[-1].get("replay") is True and code == 1000, ([m["type"] for m in again], code))


async def external(pool):
    tag = uuid.uuid4().hex[:6]
    mid = str(uuid.uuid4())
    did = await pool.fetchval("INSERT INTO drones (name, connection_url) VALUES ($1, 'test://') "
                              "RETURNING id::text", f"zz-test-live-{tag}")
    plan = await pool.fetchrow("SELECT id::text AS id, name FROM plans p WHERE EXISTS "
                               "(SELECT 1 FROM waypoints w WHERE w.plan_id = p.id) "
                               "ORDER BY created_at DESC LIMIT 1")
    await pool.execute("INSERT INTO missions (id, name, external) VALUES ($1::uuid, $2, true)",
                       mid, f"zz-test-live-{tag}")
    await pool.execute("INSERT INTO mission_drones (mission_id, drone_id) VALUES ($1::uuid, $2::uuid)",
                       mid, did)
    try:
        async with websockets.connect(f"{WS}/{mid}") as ws:
            hello = (await recv_until(ws, lambda m: True, 3))[0]
            chk("外部任務已建立 → hello 是 starting，列出機", hello["phase"] == "starting"
                and [d["drone_id"] for d in hello["drones"]] == [did], hello)
            post(f"/ext/missions/{mid}/notify", {"kind": "start_begin", "drone_id": did,
                                                  "plan_id": plan["id"]})
            got = await recv_until(ws, lambda m: m["type"] == "event" and m["kind"] == "start_step", 3)
            route = next((m for m in got if m["type"] == "route"), None)
            chk("起飛流程開始 → 送預計航線", route and route["plan_name"] == plan["name"]
                and route["geojson"]["features"] and route["reason"] == "initial",
                route and {k: route[k] for k in ("plan_name", "reason")})
            st = next((m for m in got if m["type"] == "state"), None) or \
                (await recv_until(ws, lambda m: m["type"] == "state", 2))[-1]
            dr = st["drones"][0]
            chk("沒有遙測的機：freshness never，其餘欄位是 null",
                dr["freshness"] == "never" and dr["position"] is None and dr["link"]["state"] == "unknown", dr)
            t_fail = time.monotonic()
            post(f"/ext/missions/{mid}/notify", {"kind": "start_failed", "drone_id": did, "msg": "測試"})
            got = await recv_until(ws, lambda m: m["type"] == "ended", 8)
            code = await closed_code(ws)
            ended = got[-1] if got else {}
            fail_ev = [m for m in got if m["type"] == "event" and m["detail"].get("ok") is False]
            chk("起飛失敗有事件、附原因", fail_ev and "測試" in fail_ev[0]["text"])
            chk("失敗後約 3 秒自動結束，原因 start_failed，以 1000 關閉",
                ended.get("type") == "ended" and ended["drones"][0]["reason"] == "start_failed"
                and 2.4 < ended["_t"] - t_fail < 4.8 and code == 1000,
                (round(ended.get("_t", 0) - t_fail, 2), code))
        ended_at = await pool.fetchval("SELECT ended_at FROM missions WHERE id = $1::uuid", mid)
        chk("自動結束寫回任務的 ended_at", ended_at is not None)
        await asyncio.sleep(31.5)
        async with websockets.connect(f"{WS}/{mid}") as ws:
            got = await recv_until(ws, lambda m: False, 2)
            code = await closed_code(ws)
        chk("結束超過 30 秒 → error mission_gone 並以 4410 關閉",
            got and got[0]["type"] == "error" and got[0]["code"] == "mission_gone"
            and got[0]["how_to"] and code == 4410, (got[:1], code))
    finally:
        await pool.execute("DELETE FROM missions WHERE id = $1::uuid", mid)
        await pool.execute("DELETE FROM drones WHERE id = $1::uuid", did)


async def reconnect(pool):
    mid = str(uuid.uuid4())
    await pool.execute("INSERT INTO missions (id, name) VALUES ($1::uuid, $2)",
                       mid, f"zz-test-live-ui-{mid[:6]}")
    try:
        async with websockets.connect(f"{WS}/{mid}") as ws:
            got = await recv_until(ws, lambda m: False, 2.2)
        last = max(m["seq"] for m in got if "seq" in m)
        await asyncio.sleep(1.6)
        async with websockets.connect(f"{WS}/{mid}?after_seq={last}") as ws:
            got = await recv_until(ws, lambda m: False, 1.5)
            hello = got[0]
            replay = [m for m in got if m.get("replay") is True]
            chk("帶 after_seq 重連：沒有缺口", hello["replay"]["gap"] is None, hello["replay"])
            chk("補送從 after_seq 的下一號接起、每則標 replay",
                replay and replay[0]["seq"] == last + 1 and len(replay) >= 2,
                (last, [m["seq"] for m in replay][:3]))
            seqs = [m["seq"] for m in got if "seq" in m]
            chk("補送接回即時，序號連續", seqs == list(range(seqs[0], seqs[0] + len(seqs))))
            await pool.execute("UPDATE missions SET ended_at = now() WHERE id = $1::uuid", mid)
            got = await recv_until(ws, lambda m: m["type"] == "ended", 4)
            code = await closed_code(ws)
            chk("畫面建立的任務由人結束 → ended、以 1000 關閉",
                got and got[-1]["type"] == "ended" and code == 1000, code)
        async with websockets.connect(f"{WS}/{mid}?after_seq=1") as ws:
            hello = (await recv_until(ws, lambda m: True, 2))[0]
        chk("after_seq 比緩衝還舊 → hello.replay.gap 說明缺哪一段",
            hello["replay"]["gap"] and hello["replay"]["gap"]["from_seq"] == 2, hello["replay"])
    finally:
        await pool.execute("DELETE FROM missions WHERE id = $1::uuid", mid)


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    try:
        await invalid()
        await reconnect(pool)
        await asyncio.gather(waiting(), external(pool))
    finally:
        await pool.close()


asyncio.run(main())
print("\n全部通過" if ok else "\n有項目沒過")
raise SystemExit(0 if ok else 1)
