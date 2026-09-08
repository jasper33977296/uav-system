#!/usr/bin/env python3
"""補傳的閘門（issues/047 項次 6，2026-09-08）：地面資料不補、飛行資料補得回來。

**問題**：即時遙測只在 `armed and session_id` 時才寫 telemetry（`main.py`），
補傳原本無條件寫。於是同一個「飛機停在地上什麼都沒發生」的狀態，走即時路是
不記錄、走補傳路變成記錄——**差別只在於當時鏈路有沒有斷**。實測一次 72 秒
的中斷補進 59 筆停機坪資料。

**不能整套照抄那道門**，因為它有兩半：`armed` 樣本自己帶著（照抄），
`session_id` 只有地面站知道（不能照抄）——補傳存在的理由正是「飛機解鎖飛了，
而地面站在斷線中沒看到解鎖、所以沒建架次」，照抄會把最該補的那批丟掉。

四種情形各驗一次。**需要後端在跑**（會寫資料庫，跑完自己清乾淨）：
    python3 scripts/test-backfill-gate.py
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

API = "http://localhost:38000"
UID = "2a0020001151333139383538"      # 現場那台的 board_uid
#: 挑一個沒有任何架次的時間窗（2026-09-08 01:00 UTC 前後），
#: 免得撞到真實紀錄
T0 = 1788829200.0                      # 2026-09-08 01:00:00Z（今天，且沒有任何架次）

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def post(samples, stayed_armed=None):
    body = json.dumps({"board_uid": UID, "samples": samples,
                       "stayed_armed": stayed_armed}).encode()
    req = urllib.request.Request(f"{API}/api/telemetry/backfill", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read()[:300].decode()}


def sql(q):
    return subprocess.run(
        ["docker", "exec", "uav-db", "psql", "-U", "uav", "-d", "uav", "-At", "-c", q],
        capture_output=True, text=True).stdout.strip()


def sample(i, armed):
    return {"t": T0 + i, "alt_rel": 5.0 + i * 0.1, "armed": armed,
            "lat": 24.7734, "lon": 121.0459, "flight_mode": "AUTO"}


print("── 1. 全部在地面：一筆都不寫，也不宣稱補回了失明 ────────────")
r = post([sample(i, False) for i in range(10)])
chk("inserted 0", r.get("inserted") == 0, r)
chk("說得出是「在地面」被擋的，不是被當成重複",
    r.get("skipped_on_ground") == 10 and r.get("skipped_duplicate") == 0, r)
chk("沒有建架次", r.get("session_id") is None, r.get("session_id"))
chk("**沒有把任何失明記錄標成已補回**", r.get("blackouts_recovered") == [], r)

print("\n── 2. armed 但地面站沒有架次：把架次補建出來，不寫孤兒 ────────")
r = post([sample(100 + i, True) for i in range(10)], stayed_armed=True)
sid = r.get("session_id")
chk("10 筆都寫進去", r.get("inserted") == 10, r)
chk("補建了架次", bool(sid), r)
row = sql(f"select origin||'|'||end_reason from flight_sessions where id='{sid}'")
chk("**架次標明它是補出來的**（origin=backfilled）",
    row.startswith("backfilled|reconstructed_from_backfill"), row)
armed_n = sql("select count(*) from telemetry where backfilled and armed "
              f"and session_id='{sid}'")
chk("**armed 有寫進去**（原本這欄一直是 NULL）", armed_n == "10", armed_n)

print("\n── 3. 同一批重送：算重複，不是又補一次 ──────────────────")
r2 = post([sample(100 + i, True) for i in range(10)], stayed_armed=True)
chk("inserted 0、skipped_duplicate 10",
    r2.get("inserted") == 0 and r2.get("skipped_duplicate") == 10, r2)
chk("架次沿用同一個，沒有再建一個", r2.get("session_id") == sid, r2.get("session_id"))

print("\n── 4. armed 不知道又沒有旁證：不寫（不憑空生一筆飛行紀錄）──────")
r = post([sample(300 + i, None) for i in range(5)])
chk("inserted 0、算在 on_ground",
    r.get("inserted") == 0 and r.get("skipped_on_ground") == 5, r)
chk("沒有建架次", r.get("session_id") is None, r)

print("\n── 5. 混著送：只寫飛行那段，範圍也只算那段 ──────────────")
mixed = [sample(500 + i, False) for i in range(5)] + \
        [sample(510 + i, True) for i in range(5)]
r = post(mixed, stayed_armed=True)
chk("寫 5 筆、擋 5 筆", r.get("inserted") == 5 and r.get("skipped_on_ground") == 5, r)
sid2 = r.get("session_id")
span = sql(f"select round(extract(epoch from (ended_at-started_at))::numeric) "
           f"from flight_sessions where id='{sid2}'")
chk("**補建的架次只涵蓋飛行那段**（4 秒，不是 14 秒）", span == "4", span)

print("\n── 清理 ────────────────────────────────────────────")
n = sql(f"with d as (delete from telemetry where time between "
        f"to_timestamp({T0 - 1}) and to_timestamp({T0 + 600}) returning 1) "
        f"select count(*) from d")
m = sql("with d as (delete from flight_sessions where origin='backfilled' "
        f"and started_at between to_timestamp({T0 - 1}) and to_timestamp({T0 + 600}) "
        "returning 1) select count(*) from d")
print(f"刪掉 telemetry {n} 筆、flight_sessions {m} 筆")

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
