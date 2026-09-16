#!/usr/bin/env python3
"""對外即時資料的兩種傳法＋路徑版本（doc/external-live-api.md），打正在跑的服務。

驗三件事：
  ① 每一支對外端點都吃 `/api/v1/…`，舊的無版本路徑照樣通（別名）
  ② 輪詢端點 `…/missions/{id}/live` 的快照、補送、gap 與兩種錯誤
  ③ **WS 與輪詢送的是同一份訊息**：同一個序號的那一則要逐欄相同

只讀既有資料，另外建一組臨時任務／機／架次／遙測驗 route 與 track，跑完刪掉。
不碰飛機。

用法：
  docker compose exec -T uav-backend env \
    DATABASE_URL=postgresql://uav:uav@localhost:35432/uav python - < scripts/test-ext-live-poll.py
"""
import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import websockets

BE = "http://localhost:38000"
CMD = "http://localhost:38001"
WS = "ws://localhost:38000"
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}", flush=True)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    tag = uuid.uuid4().hex[:6]

    # ── ① 路徑版本 ────────────────────────────────────────────────
    print("\n【① 版本別名】")
    pairs = [
        (f"{CMD}/api/v1/ext/drones", f"{CMD}/api/ext/drones"),
        (f"{CMD}/api/v1/missions", f"{CMD}/api/missions"),
        (f"{CMD}/api/v1/plans", f"{CMD}/api/plans"),
        (f"{BE}/api/v1/ext/missions?limit=1", f"{BE}/api/ext/missions?limit=1"),
        (f"{BE}/api/v1/ext/live", f"{BE}/api/ext/live"),
    ]
    for versioned, plain in pairs:
        # 前後各讀一次無版本路徑：這幾支反映的是**當下**的機隊狀態，兩次呼叫之間
        # 本來就可能變。要驗的是「同一個 handler」，不是「世界靜止」——
        # 只要帶版本的那一份對得上前後任一次，就是同一支
        s0, b0 = get(plain)
        sv, bv = get(versioned)
        s1, b1 = get(plain)
        chk(f"{versioned.split('/api')[1]:<34} 與無版本路徑同一支",
            s0 == sv == s1 == 200 and bv in (b0, b1), (s0, sv, s1))
    # 任務歷史先上線時用過 /api/ext/v1/…，那個拼法要照收
    s1, b1 = get(f"{BE}/api/ext/v1/missions?limit=1")
    s2, b2 = get(f"{BE}/api/v1/ext/missions?limit=1")
    chk("舊拼法 /api/ext/v1/… 仍然通", s1 == s2 == 200 and b1 == b2, (s1, s2))
    chk("沒有的版本不會被吞掉（/api/v9/… → 404）",
        get(f"{CMD}/api/v9/missions")[0] == 404)

    # ── ② 輪詢端點 ────────────────────────────────────────────────
    print("\n【② 輪詢】")
    st, body = get(f"{BE}/api/v1/ext/missions/not-a-uuid/live")
    chk("編號不是 UUID → 422 並說明", st == 422
        and body["detail"]["code"] == "mission_id_invalid" and "not-a-uuid" in body["detail"]["msg"],
        (st, body))
    gone = await pool.fetchrow("SELECT id::text AS id, name FROM missions "
                               "WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1")
    if gone:
        st, body = get(f"{BE}/api/v1/ext/missions/{gone['id']}/live")
        chk("早就結束的任務 → 410，並指去歷史 API", st == 410
            and body["detail"]["code"] == "mission_gone"
            and any("signal" in h for h in body["detail"]["how_to"]), (st, body.get("detail")))

    mid = str(uuid.uuid4())
    st, snap = get(f"{BE}/api/v1/ext/missions/{mid}/live")
    chk("沒見過的編號 → 開場（phase=waiting），不是錯誤",
        st == 200 and snap["phase"] == "waiting" and snap["drones"] == []
        and [m["type"] for m in snap["messages"]] == ["state"], (st, snap.get("phase")))
    chk("開場就說得出下一次怎麼拉", snap["poll_after_s"] == 0.5 and "seq" in snap, snap.get("poll_after_s"))
    await asyncio.sleep(2.2)
    _, snap2 = get(f"{BE}/api/v1/ext/missions/{mid}/live")
    kinds = [m["type"] for m in snap2["messages"]]
    chk("快照只給一則 state（不是一整段）", kinds == ["state"], kinds)
    chk("快照的 state 不佔序號（同 route／track，只屬於這一次呼叫）",
        "seq" not in snap2["messages"][0], snap2["messages"][0].keys())

    after = snap2["seq"]
    await asyncio.sleep(1.6)
    _, catch = get(f"{BE}/api/v1/ext/missions/{mid}/live?after_seq={after}")
    seqs = [m["seq"] for m in catch["messages"]]
    chk("帶 after_seq：補的是那之後的每一則，沒有缺號",
        len(seqs) >= 2 and seqs == list(range(after + 1, after + 1 + len(seqs)))
        and catch["seq"] == seqs[-1] and catch["replay"]["gap"] is None, seqs)
    _, old = get(f"{BE}/api/v1/ext/missions/{mid}/live?after_seq=1")
    chk("after_seq 太舊 → 說得出缺了哪一段", old["replay"]["gap"] is not None
        and old["replay"]["gap"]["from_seq"] == 2, old["replay"]["gap"])

    # ── ③ WS 與輪詢是同一份訊息 ───────────────────────────────────
    print("\n【③ 兩種傳法，同一份訊息】")
    pid = str(uuid.uuid4())
    seen: dict[int, dict] = {}

    async def listen():
        async with websockets.connect(f"{WS}/ws/v1/missions/{pid}") as ws:
            try:
                async with asyncio.timeout(4):
                    while True:
                        m = json.loads(await ws.recv())
                        if "seq" in m:
                            seen[m["seq"]] = m
            except (TimeoutError, asyncio.TimeoutError):
                pass

    async def poll():
        await asyncio.sleep(0.3)
        _, first = get(f"{BE}/api/v1/ext/missions/{pid}/live")
        cur, got = first["seq"], {}
        for _ in range(7):
            await asyncio.sleep(0.5)
            _, r = get(f"{BE}/api/v1/ext/missions/{pid}/live?after_seq={cur}")
            for m in r["messages"]:
                got[m["seq"]] = m
            cur = r["seq"]
        return got

    _, polled = await asyncio.gather(listen(), poll())
    both = sorted(set(seen) & set(polled))
    chk("同一個任務，兩邊都收得到", len(both) >= 3, f"WS {len(seen)} 則、輪詢 {len(polled)} 則、重疊 {len(both)}")
    diff = [s for s in both if seen[s] != polled[s]]
    chk("重疊的那幾則逐欄相同", not diff,
        diff[:1] and (seen[diff[0]], polled[diff[0]]))

    # ── ④ 臨時任務：快照要帶預計航線與已飛軌跡 ─────────────────────
    print("\n【④ 快照帶 route 與 track】")
    mid2, did = str(uuid.uuid4()), None
    try:
        plan = await pool.fetchrow(
            "SELECT p.id::text AS id FROM plans p JOIN waypoints w ON w.plan_id = p.id "
            "GROUP BY p.id HAVING count(*) >= 2 LIMIT 1")
        chk("找得到一份有航點的路徑當素材", plan is not None)
        if plan:
            did = await pool.fetchval(
                "INSERT INTO drones (name, connection_url, current_plan_id) "
                "VALUES ($1, 'test://', $2::uuid) RETURNING id::text",
                f"zz-test-poll-{tag}", plan["id"])
            await pool.execute("INSERT INTO missions (id, name) VALUES ($1::uuid, $2)",
                               mid2, f"zz-test-poll-{tag}")
            await pool.execute("INSERT INTO mission_drones (mission_id, drone_id) "
                               "VALUES ($1::uuid, $2::uuid)", mid2, did)
            t0 = datetime.now(timezone.utc) - timedelta(minutes=3)
            sid = await pool.fetchval(
                "INSERT INTO flight_sessions (drone_id, started_at, mission_id, plan_id) "
                "VALUES ($1::uuid, $2, $3::uuid, $4::uuid) RETURNING id::text",
                did, t0, mid2, plan["id"])
            for i in range(4):
                await pool.execute(
                    "INSERT INTO telemetry (time, drone_id, session_id, lat, lon, alt_rel) "
                    "VALUES ($1, $2::uuid, $3::uuid, $4, $5, 10.0)",
                    t0 + timedelta(seconds=i), did, sid, 24.7 + i * 1e-4, 121.0 + i * 1e-4)
            await asyncio.sleep(1.5)     # 等一輪 _sync 把成員與路徑讀進來
            _, s = get(f"{BE}/api/v1/ext/missions/{mid2}/live")
            types = [m["type"] for m in s["messages"]]
            chk("快照帶 route、track 與 state", set(types) >= {"route", "track", "state"}, types)
            route = next((m for m in s["messages"] if m["type"] == "route"), None)
            track = next((m for m in s["messages"] if m["type"] == "track"), None)
            chk("route 是這台機這份路徑", route and route["plan_id"] == plan["id"]
                and route["geojson"]["features"], route and route.get("plan_id"))
            chk("track 帶得出已飛的四點",
                track and track["geojson"]["properties"]["points"] == 4,
                track and track["geojson"]["properties"])
            chk("成員名單看得到這台機",
                [d["drone_id"] for d in s["drones"]] == [did], s["drones"])
            # 快照的 state 是現算的，串流的是 tick publish 的——兩邊不能長得不一樣
            snap_state = next(m for m in s["messages"] if m["type"] == "state")
            await asyncio.sleep(1.2)
            _, nxt = get(f"{BE}/api/v1/ext/missions/{mid2}/live?after_seq={s['seq']}")
            tick_state = next((m for m in nxt["messages"] if m["type"] == "state"), None)
            vol = ("ts", "seq", "type", "v", "mission_id")
            chk("快照現算的 state 與串流 tick 送的逐欄相同",
                tick_state is not None
                and {k: v for k, v in snap_state.items() if k not in vol}
                    == {k: v for k, v in tick_state.items() if k not in vol},
                [m["type"] for m in nxt["messages"]])
    finally:
        if did:
            await pool.execute("DELETE FROM telemetry WHERE drone_id = $1::uuid", did)
        await pool.execute("DELETE FROM flight_sessions WHERE mission_id = $1::uuid", mid2)
        await pool.execute("DELETE FROM mission_drones WHERE mission_id = $1::uuid", mid2)
        await pool.execute("DELETE FROM missions WHERE id = $1::uuid", mid2)
        if did:
            await pool.execute("DELETE FROM drones WHERE id = $1::uuid", did)
        left = await pool.fetchval(
            "SELECT (SELECT count(*) FROM drones WHERE name ILIKE $1) + "
            "(SELECT count(*) FROM missions WHERE name ILIKE $1)", f"zz-test-poll-{tag}%")
        chk("臨時資料都清掉了", left == 0, left)
        await pool.close()


asyncio.run(main())
print("\n全部通過" if ok else "\n有項目沒過")
raise SystemExit(0 if ok else 1)
