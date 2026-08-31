#!/usr/bin/env python3
"""失明記錄與回補的端到端：**斷線期間的資料本來就沒有遺失，只是送不出來。**

模擬一整條時間軸：架次開始 → 遙測中斷（失明開始）→ 鏈路恢復 → 代理補傳 →
失明被標成「已補回」。

**假的只有「機上」那一端**（我們沒辦法真的把飛機的 5G 拔掉再插回去）；
backend 的失明記錄、補傳入庫、架次歸屬、去重全部是真的走一遍。

用法：python3 scripts/test-blackout-backfill.py
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:38000"
UID = "backfilltest-0000"
CWD = "/home/k200/uav-system"

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def psql(sql):
    """**只取第一行**：`INSERT … RETURNING` 的輸出後面還跟著「INSERT 0 1」，
    整包 strip 之後那行會混進 id，讓後續的 SQL 全部變成非法字串——
    而症狀是「場景沒建起來」，看起來像資料庫的問題。"""
    r = subprocess.run(["docker", "compose", "exec", "-T", "uav-db", "psql",
                        "-U", "uav", "-d", "uav", "-tAc", sql],
                       capture_output=True, text=True, cwd=CWD)
    out = (r.stdout or "").strip().split("\n")
    return out[0].strip() if out else ""


def post(path, body):
    req = urllib.request.Request(f"{API}{path}", data=json.dumps(body).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read().decode() or "{}")}


# ── 場景佈置：一台測試機、一個已經結束的架次、一段失明 ─────────
psql(f"INSERT INTO drones (name, serial_no, is_simulated, status, board_uid) "
     f"VALUES ('__bf','__bf',true,'idle','{UID}') ON CONFLICT (serial_no) "
     f"DO UPDATE SET board_uid = EXCLUDED.board_uid;")
did = psql(f"SELECT id FROM drones WHERE board_uid = '{UID}';")
now = time.time()
t_start, t_lost, t_back = now - 600, now - 500, now - 200
sid = psql(
    f"INSERT INTO flight_sessions (drone_id, started_at, ended_at, end_reason) "
    f"VALUES ('{did}', to_timestamp({t_start}), to_timestamp({t_lost}), "
    f"'telemetry_lost') RETURNING id;")
bid = psql(
    f"INSERT INTO blackouts (drone_id, session_id, started_at, reason, "
    f"armed_at_start) VALUES ('{did}', '{sid}', to_timestamp({t_lost}), "
    f"'telemetry_lost', true) RETURNING id;")
chk("場景就緒（機／架次／失明記錄）", bool(did and sid and bid))

print("\n── 補傳：那段時間的資料補回來 ─────────────────────────")
samples = [{"t": t_lost + i * 1.0, "lat": 24.773 + i * 1e-5, "lon": 121.046,
            "alt_rel": 30.0, "flight_mode": "AUTO", "battery_pct": 70 - i * 0.01,
            "gps_fix": 3, "satellites": 12, "armed": True}
           for i in range(120)]
r = post("/api/telemetry/backfill",
         {"board_uid": UID, "samples": samples, "stayed_armed": True})
chk("補傳成功", r.get("ok") is True, r.get("_status") or r.get("inserted"))
chk("120 筆全進去", r.get("inserted") == 120, r.get("inserted"))
chk("**歸到當時那個架次**（用時間找，不是用「現在的架次」）",
    r.get("session_id") == sid, r.get("session_id"))
chk("失明記錄被標成已補回", bid in (r.get("blackouts_recovered") or []),
    r.get("blackouts_recovered"))

n = psql(f"SELECT count(*) FROM telemetry WHERE session_id = '{sid}' "
         f"AND backfilled;")
chk("入庫時標記 backfilled（事後分得出哪些是後補的）", n == "120", n)
live = psql(f"SELECT count(*) FROM telemetry WHERE session_id = '{sid}' "
            f"AND NOT backfilled;")
chk("**反向驗證**：沒有把補傳的混進即時資料", live == "0", live)

print("\n── 去重：重連後代理可能重送同一段 ─────────────────────")
r2 = post("/api/telemetry/backfill",
          {"board_uid": UID, "samples": samples[:50], "stayed_armed": True})
n2 = psql(f"SELECT count(*) FROM telemetry WHERE session_id = '{sid}';")
chk("重送不會變成兩份", n2 == "120", f"重送後 {n2} 筆")
chk("而且明說跳過了幾筆（不是安靜地丟掉）",
    r2.get("skipped_duplicate") == 50, r2.get("skipped_duplicate"))

print("\n── 機上時鐘還沒對過時：1970 的時間戳不能進來 ─────────────")
# 機上 Pi 的 RTC 沒有電池，冷開機時系統時間從 1970 起算、靠 NTP 修正，而 NTP
# 走 5G——正是斷線期間不通的那條。取樣在同步之前就開始，所以一批補傳裡會混著
# 1970。**危害不是那幾筆假資料本身**：`lo` 變成 1970 之後，blackouts 那條
# UPDATE 的範圍條件會匹配到這台機**有史以來每一段失明記錄**，把從來沒補回的
# 洞全部標成「已補回」——歷史就此永遠說了謊。
epoch_junk = [{"t": 14.0 + i, "lat": 25.05, "lon": 121.50, "alt_rel": 30.0}
              for i in range(5)]
r4 = post("/api/telemetry/backfill",
          {"board_uid": UID, "samples": epoch_junk, "stayed_armed": True})
chk("整批都是 1970 → 422 拒收（不是靜靜寫進歷史）",
    r4.get("_status") == 422, r4.get("detail"))
n_junk = psql("SELECT count(*) FROM telemetry WHERE time < '2000-01-01';")
chk("**反向驗證**：1970 那幾筆一筆都沒進資料庫", n_junk == "0", n_junk)

# 種一段**上個月的、沒補回的**失明。沒有它，下面那條斷言是空的（0 → 0）——
# 而一條永遠成立的斷言擋不住任何迴歸
old_bid = psql(
    f"INSERT INTO blackouts (drone_id, started_at, ended_at, reason, "
    f"armed_at_start) VALUES ('{did}', now() - interval '30 days', "
    f"now() - interval '30 days' + interval '5 min', 'telemetry_gap', true) "
    f"RETURNING id;")
before = psql(f"SELECT count(*) FROM blackouts WHERE drone_id = '{did}' "
              f"AND recovered_by IS NULL;")
chk("（前置）確實有一段沒補回的舊失明可以被誤標", before == "1", before)
mixed = epoch_junk + [{"t": t_lost + 200 + i, "lat": 25.05, "lon": 121.50,
                       "alt_rel": 30.0} for i in range(3)]
r5 = post("/api/telemetry/backfill",
          {"board_uid": UID, "samples": mixed, "stayed_armed": True})
chk("混著 1970 的一批：好的收下、壞的丟掉",
    r5.get("inserted") == 3 and r5.get("rejected_implausible") == 5,
    f"inserted={r5.get('inserted')} rejected={r5.get('rejected_implausible')}")
chk("丟掉的筆數與重複的筆數分開報（不能混成一個數字）",
    r5.get("skipped_duplicate") == 0, r5.get("skipped_duplicate"))
after = psql(f"SELECT count(*) FROM blackouts WHERE drone_id = '{did}' "
             f"AND recovered_by IS NULL;")
chk("**沒有把無關的失明記錄一起標成已補回**", before == after == "1",
    f"未補回的失明：{before} → {after}")
still = psql(f"SELECT coalesce(recovered_by, '(null)') FROM blackouts "
             f"WHERE id = '{old_bid}';")
chk("上個月那段失明仍然是沒補回的", still == "(null)", still)

print("\n── 認不得的板子要拒絕，不是安靜地丟掉 ─────────────────")
r3 = post("/api/telemetry/backfill",
          {"board_uid": "nobody-knows-me", "samples": samples[:2]})
chk("未知 board_uid → 404", r3.get("_status") == 404, r3.get("detail"))

print("\n── D 層：代理說「一直沒落地」→ 沿用同一趟，不重開 ─────")
n_sess = psql(f"SELECT count(*) FROM flight_sessions WHERE drone_id = '{did}';")
chk("沒有多開一個架次（一趟飛行不該變成兩趟）", n_sess == "1", n_sess)
reason = psql(f"SELECT end_reason FROM flight_sessions WHERE id = '{sid}';")
chk("結束理由改記成已補回", reason == "telemetry_lost_backfilled", reason)
ended = psql(f"SELECT round(extract(epoch FROM ended_at)) FROM flight_sessions "
             f"WHERE id = '{sid}';")
chk("結束時間往後推到補傳的尾端",
    ended.isdigit() and int(ended) >= int(t_lost + 119), f"{ended} vs {int(t_lost+119)}")

print("\n── 失明記錄查得到、算得出長度 ─────────────────────────")
secs = psql(f"SELECT round(extract(epoch FROM coalesce(ended_at, now()) "
            f"- started_at)) FROM blackouts WHERE id = '{bid}';")
chk("失明長度算得出來", secs.isdigit() and int(secs) > 0, f"{secs} 秒")
armed = psql(f"SELECT armed_at_start FROM blackouts WHERE id = '{bid}';")
chk("記得失明開始時機體是 armed（D 層要用）", armed == "t", armed)

# 收拾（flight_sessions 沒有 ON DELETE CASCADE，順序要對）
psql(f"DELETE FROM telemetry WHERE drone_id = '{did}';")
psql(f"DELETE FROM blackouts WHERE drone_id = '{did}';")
psql(f"DELETE FROM flight_sessions WHERE drone_id = '{did}';")
psql(f"DELETE FROM drones WHERE id = '{did}';")
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
