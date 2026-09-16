#!/usr/bin/env python3
"""對外任務歷史（doc/external-history-api.md），打正在跑的 backend。

只讀既有資料，另外建一組臨時任務／機／架次／訊號驗「沒有路徑就不給里程」，跑完刪掉。
不碰飛機。

用法：
  docker run --rm -i --network host -e DATABASE_URL=postgresql://uav:uav@localhost:35432/uav \
    uav-system-uav-backend python - < scripts/test-ext-history.py
"""
import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg

API = "http://localhost:38000/api/v1/ext"
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}", flush=True)


def get(path):
    try:
        with urllib.request.urlopen(API + path, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


SAMPLE_KEYS = {"time", "lat", "lon", "alt_rel", "along_m", "offset_m",
               "rsrp", "rsrq", "sinr", "cqi", "pci", "cell_id", "band", "nr_mode",
               "rtt_ms", "jitter_ms", "packet_loss_pct",
               "throughput_up_kbps", "throughput_down_kbps"}


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    tag = uuid.uuid4().hex[:6]
    st, body = get("/missions?limit=200")
    chk("任務清單回 200", st == 200 and isinstance(body.get("missions"), list), st)
    ms = body["missions"]
    chk("每個任務都有必要欄位", all(
        {"mission_id", "name", "external", "started_at", "ended_at",
         "drones", "plans", "sessions", "samples"} <= set(x) for x in ms))

    # 統計要與資料庫對得上（統計是查詢時算的，不存欄位）
    flown = [x for x in ms if x["sessions"]]
    chk("清單裡有飛過的任務", bool(flown), len(ms))
    if flown:
        m = max(flown, key=lambda x: x["samples"])
        row = await pool.fetchrow("""
            SELECT (SELECT count(*) FROM flight_sessions x WHERE x.mission_id = $1::uuid) AS sess,
                   (SELECT count(*) FROM link_metrics l JOIN flight_sessions x ON x.id = l.session_id
                     WHERE x.mission_id = $1::uuid) AS samples""", m["mission_id"])
        chk("架次數與樣本數與資料庫一致",
            m["sessions"] == row["sess"] and m["samples"] == row["samples"],
            (m["sessions"], row["sess"], m["samples"], row["samples"]))

        # 訊號本體
        st, sig = get(f"/missions/{m['mission_id']}/signal")
        samples = [s for d in sig["drones"] for x in d["sessions"] for s in x["samples"]]
        sess = [x for d in sig["drones"] for x in d["sessions"]]
        chk("訊號回 200，樣本數與清單一致",
            st == 200 and len(samples) == m["samples"], (st, len(samples), m["samples"]))
        chk("樣本欄位就是文件那一組", all(set(s) == SAMPLE_KEYS for s in samples),
            sorted(set(samples[0]) ^ SAMPLE_KEYS) if samples else "沒有樣本")
        chk("回應帶方法參數", sig["method"]["max_offset_m"] == 60.0
            and sig["method"]["sample_interval_s"] == 1, sig.get("method"))
        planned = [x for x in sess if x["plan_id"]]
        chk("綁路徑的架次：reference=plan、有預計航線",
            all(x["reference"] == "plan" and x["route"]["features"] for x in planned),
            [(x["reference"], bool(x["route"])) for x in planned])
        pts = [s for x in planned for s in x["samples"] if s["lat"] is not None]
        got = [s for s in pts if s["along_m"] is not None]
        chk("大多數樣本算得出沿航線里程",
            bool(pts) and len(got) >= len(pts) * 0.5 and all(s["offset_m"] is not None for s in pts),
            f"{len(got)}/{len(pts)}")
        chk("里程單調可用（0 ≤ along ≤ 路徑長）",
            all(0 <= s["along_m"] for s in got), got[:1])

        # 偏離上限：**不硬塞**——里程給 null、偏離照給
        st, tight = get(f"/missions/{m['mission_id']}/signal?max_offset_m=0.01")
        tp = [s for d in tight["drones"] for x in d["sessions"] for s in x["samples"]
              if s["lat"] is not None]
        chk("偏離超過上限：里程 null、偏離照給",
            st == 200 and tp and all(s["along_m"] is None and s["offset_m"] is not None for s in tp),
            f"{sum(1 for s in tp if s['along_m'] is not None)} 筆仍有里程")

    # 篩選
    if flown and flown[0]["plans"]:
        pid = flown[0]["plans"][0]["plan_id"]
        _, byplan = get(f"/missions?plan_id={pid}")
        chk("plan_id 篩選：回的都飛過那份路徑",
            byplan["missions"] and all(any(p["plan_id"] == pid for p in x["plans"])
                                       for x in byplan["missions"]))
    _, ext = get("/missions?external=true")
    chk("external 篩選只回外部建立的", all(x["external"] for x in ext["missions"]))
    _, future = get("/missions?since=2099-01-01")
    chk("時間窗篩得掉全部", future["missions"] == [])

    st, body = get("/missions/not-a-uuid/signal")
    chk("編號不是 UUID → 422 並說明", st == 422
        and body["detail"]["code"] == "mission_id_invalid" and "not-a-uuid" in body["detail"]["msg"],
        (st, body))
    st, body = get(f"/missions/{uuid.uuid4()}/signal")
    chk("沒有這個任務 → 404", st == 404 and body["detail"]["code"] == "mission_not_found", st)

    # 臨時：沒有綁路徑的架次 → 不給里程（不退回用軌跡）
    mid, did = str(uuid.uuid4()), None
    try:
        did = await pool.fetchval("INSERT INTO drones (name, connection_url) "
                                  "VALUES ($1, 'test://') RETURNING id::text", f"zz-test-hist-{tag}")
        await pool.execute("INSERT INTO missions (id, name) VALUES ($1::uuid, $2)",
                           mid, f"zz-test-hist-{tag}")
        t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
        sid = await pool.fetchval(
            "INSERT INTO flight_sessions (drone_id, started_at, ended_at, mission_id, end_reason) "
            "VALUES ($1::uuid, $2, $3, $4::uuid, 'disarmed') RETURNING id::text",
            did, t0, t0 + timedelta(minutes=1), mid)
        for i in range(3):
            await pool.execute(
                "INSERT INTO link_metrics (time, drone_id, session_id, lat, lon, alt_rel, sinr, rsrp) "
                "VALUES ($1, $2::uuid, $3::uuid, 24.7, 121.0, 5.0, 20.0, -80.0)",
                t0 + timedelta(seconds=i), did, sid)
        st, sig = get(f"/missions/{mid}/signal")
        s0 = sig["drones"][0]["sessions"][0]
        chk("沒有綁路徑：reference=null、里程與偏離都 null、樣本照給",
            st == 200 and s0["reference"] is None and s0["route"] is None
            and len(s0["samples"]) == 3
            and all(x["along_m"] is None and x["offset_m"] is None for x in s0["samples"]),
            (s0["reference"], len(s0["samples"])))
        chk("這一趟沒有失明記錄 → gaps 是空的", s0["gaps"] == [], s0["gaps"])
        _, one = get(f"/missions?drone_id={did}")
        chk("drone_id 篩選找得到這台機的任務",
            [x["mission_id"] for x in one["missions"]] == [mid], one["missions"])
    finally:
        await pool.execute("DELETE FROM link_metrics WHERE drone_id = $1::uuid", did)
        await pool.execute("DELETE FROM flight_sessions WHERE mission_id = $1::uuid", mid)
        await pool.execute("DELETE FROM missions WHERE id = $1::uuid", mid)
        await pool.execute("DELETE FROM drones WHERE id = $1::uuid", did)
        left = await pool.fetchval(
            "SELECT (SELECT count(*) FROM drones WHERE name ILIKE $1) + "
            "(SELECT count(*) FROM missions WHERE name ILIKE $1)", f"zz-test-hist-{tag}%")
        chk("臨時資料都清掉了", left == 0, left)
        await pool.close()


asyncio.run(main())
print("\n全部通過" if ok else "\n有項目沒過")
raise SystemExit(0 if ok else 1)
