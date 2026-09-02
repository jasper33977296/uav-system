#!/usr/bin/env python3
"""機上錄製的自動回傳，端到端（issues/014）。

**兩端都是真的**：機端跑 uav-agent 真正的 `uploader.Uploader`（不是模擬一個
HTTP 客戶端），地面站跑真的 backend——這條鏈路的價值就在於「兩邊講的是同一
套協定」，自己寫一個假的客戶端去打真的端點，測到的只有我自己的想像。

要驗的六件事：

1. **整份回傳而且逐 byte 相同**（sha256）——截斷的 tlog 看起來就是一個比較
   短的 tlog，格式裡沒有任何地方會說「我不完整」
2. **可續傳**：中斷後從已收到的位元組接續，不是整份重來
3. **校驗不符整份作廢**，而且不留半成品——留著它比沒有它更糟，它會被當成證據
4. **兩層分開列**：機上那份不會混進 `/api/captures`（地面站自己錄的那份）。
   兩者相差的正是 5G 斷線的那一段，混成一個清單就把那個差抹掉了
5. **不認得的 board_uid 開不出目錄**：沒有身分的東西不該在我們的磁碟上長東西
6. **檔名走樣式白名單**：上傳端沒有「比對既有清單」這個奢侈（檔案還不存在）

用法：python3 scripts/test-onboard-upload.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/home/k200/uav-agent")

API = "http://localhost:38000"
UID = "onboardupload-test-0001"
CWD = "/home/k200/uav-system"

try:
    import uploader                       # noqa: E402  uav-agent 真正的那支
except ImportError as e:
    print(f"skip：找不到 uav-agent 的模組（{e}）。**skip ≠ pass**")
    sys.exit(0)

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def psql(sql):
    r = subprocess.run(["docker", "compose", "exec", "-T", "uav-db", "psql",
                        "-U", "uav", "-d", "uav", "-tAc", sql],
                       capture_output=True, text=True, cwd=CWD)
    out = (r.stdout or "").strip().split("\n")
    return out[0].strip() if out else ""


def req(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode()
                                        if body is not None else None)
    r = urllib.request.Request(f"{API}{path}", data=data, method=method,
                               headers={"Content-Type": ctype} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, None


def purge():
    """把這支測試留下的東西全部清掉。

    **`flight_sessions.drone_id` 沒有 ON DELETE CASCADE**（2026-09-02 實測），
    所以刪機一定要先刪架次——不然 `DELETE` 靜靜失敗，測試機就永遠留在機隊
    清單裡，**而且它會出現在「回傳狀態」那一區，看起來像一台真的、沒有代理
    的機**。收拾失敗要看得見，所以結尾有一格斷言在盯它。
    """
    for t in ("blackouts", "telemetry", "events", "flight_sessions"):
        psql(f"DELETE FROM {t} WHERE drone_id IN "
             f"(SELECT id FROM drones WHERE board_uid = '{UID}')")
    psql(f"DELETE FROM drones WHERE board_uid = '{UID}'")


# ── 場景 ────────────────────────────────────────────────────────
# **`board_uid` 上沒有唯一索引**（2026-09-02 實測），所以不能用
# `ON CONFLICT (board_uid)`——先刪再建
purge()
drone_id = psql(f"INSERT INTO drones (name, mav_sysid, board_uid) VALUES "
                f"('回傳測試機', 199, '{UID}') RETURNING id")
print(f"場景：drone_id={drone_id}\n")

d = tempfile.mkdtemp()
NAME = "20260902-090000.tlog"
BLOB = os.urandom(700_000)
open(os.path.join(d, NAME), "wb").write(BLOB)
SHA = hashlib.sha256(BLOB).hexdigest()

print("── 1. 真的 Uploader × 真的 backend：整份回傳 ────────────")
u = uploader.Uploader(d, "localhost", 38000, lambda: UID, lambda: None,
                      lambda: (True, None), rate_kbs=100_000,
                      chunk_kb=128, settle_s=0.0)
u._pass()
chk("機端認為傳完了", u.files == 1 and u.backlog == 0, u.stats())
s, lst = req("GET", "/api/onboard-captures")
mine = [f for f in lst["files"] if f["drone_id"] == drone_id]
chk("地面站列得出來", len(mine) == 1, [f["name"] for f in mine])
chk("而且標成 complete", mine and mine[0]["complete"], mine)
chk("**歸屬到這台機**（board_uid → drone_id，不是拿檔名當身分）",
    mine and mine[0]["drone_name"] == "回傳測試機", mine and mine[0]["drone_name"])

print("\n── 2. 拿回來的東西逐 byte 相同 ─────────────────────────")
with urllib.request.urlopen(
        f"{API}/api/onboard-captures/{drone_id}/{NAME}", timeout=30) as r:
    got = r.read()
chk("大小相同", len(got) == len(BLOB), f"{len(got)} vs {len(BLOB)}")
chk("**sha256 相同**（截斷的 tlog 看起來就是一個比較短的 tlog）",
    hashlib.sha256(got).hexdigest() == SHA)

print("\n── 3. 重跑不重傳（sha 認得出是同一份）──────────────────")
os.remove(os.path.join(d, ".uploaded.json"))     # 機端狀態掉了
u2 = uploader.Uploader(d, "localhost", 38000, lambda: UID, lambda: None,
                       lambda: (True, None), rate_kbs=100_000, settle_s=0.0)
u2._pass()
chk("一個 byte 都沒重送", u2.bytes == 0, u2.stats())
chk("而且記回本地了", u2.is_uploaded(NAME))

print("\n── 4. 可續傳：斷在半路，接得回來 ───────────────────────")
N2, B2 = "20260902-091500.tlog", os.urandom(400_000)
SHA2 = hashlib.sha256(B2).hexdigest()
s, r = req("POST", "/api/onboard-captures/offer",
           {"board_uid": UID, "name": N2, "bytes": len(B2), "sha256": SHA2})
chk("宣告收下了，從 0 開始", s == 200 and r["have"] == 0, r)
q = f"?board_uid={UID}&name={N2}&offset=0"
s, r = req("PUT", f"/api/onboard-captures/chunk{q}", raw=B2[:150_000],
           ctype="application/octet-stream")
chk("第一塊收下了", s == 200 and r["have"] == 150_000, r)
s, r = req("POST", "/api/onboard-captures/offer",
           {"board_uid": UID, "name": N2, "bytes": len(B2), "sha256": SHA2})
chk("**再宣告一次就知道要從哪裡接**（續傳的權威在地面站）",
    r.get("have") == 150_000, r)
s, r = req("PUT", f"/api/onboard-captures/chunk?board_uid={UID}&name={N2}"
                  f"&offset=0", raw=B2[:100], ctype="application/octet-stream")
chk("**位移不符回 409 並帶上真值**（不是叫人重傳整份）",
    s == 409 and r["detail"]["have"] == 150_000, (s, r))
s, r = req("PUT", f"/api/onboard-captures/chunk?board_uid={UID}&name={N2}"
                  f"&offset=150000", raw=B2[150_000:],
           ctype="application/octet-stream")
chk("接完就收尾", s == 200 and r["complete"], r)

print("\n── 5. 校驗不符：整份作廢，不留半成品 ───────────────────")
N3, B3 = "20260902-092000.tlog", os.urandom(1000)
s, r = req("POST", "/api/onboard-captures/offer",
           {"board_uid": UID, "name": N3, "bytes": 1000, "sha256": "a" * 64})
s, r = req("PUT", f"/api/onboard-captures/chunk?board_uid={UID}&name={N3}"
                  f"&offset=0", raw=B3, ctype="application/octet-stream")
chk("sha 不符 → 422", s == 422, (s, r))
s, lst = req("GET", "/api/onboard-captures")
chk("**半成品沒有留在清單裡**（留著它會被當成證據）",
    not [f for f in lst["files"] if f["name"] == N3],
    [f["name"] for f in lst["files"]])
s, r = req("GET", f"/api/onboard-captures/{drone_id}/{N3}")
chk("而且下載不到", s == 404, s)

print("\n── 6. 兩層分開：機上那份不會混進地面站的錄製清單 ────────")
s, cap = req("GET", "/api/captures")
chk("`/api/captures` 裡沒有機上那份", not [f for f in cap["files"]
                                          if f["name"] in (NAME, N2)],
    [f["name"] for f in cap["files"]][:5])
chk("兩份清單的目錄不同（機上那份在 onboard/ 底下）",
    lst["dir"].endswith("/onboard") and cap["dir"] != lst["dir"],
    (cap["dir"], lst["dir"]))

print("\n── 7. 沒有身分就不長目錄，檔名走樣式白名單 ─────────────")
s, r = req("POST", "/api/onboard-captures/offer",
           {"board_uid": "no-such-board", "name": NAME, "bytes": 10,
            "sha256": "b" * 64})
chk("**不認得的 board_uid → 404**", s == 404, (s, r))
for bad in ("../../etc/passwd", "../20260902-093000.tlog", "x.tlog",
            "20260902-093000.tlog.sh"):
    s, r = req("POST", "/api/onboard-captures/offer",
               {"board_uid": UID, "name": bad, "bytes": 10, "sha256": "b" * 64})
    chk(f"檔名 {bad!r} 被擋", s == 422, s)
s, r = req("GET", f"/api/onboard-captures/{drone_id}/..%2F..%2F20260902.tlog")
chk("**下載的路徑穿越擋掉**（白名單而不是黑名單）", s == 404, s)

print("\n── 8. 撞名不覆蓋（機上的 RTC 沒有電池，1970 的檔名會重複）")
s, r = req("POST", "/api/onboard-captures/offer",
           {"board_uid": UID, "name": NAME, "bytes": 500, "sha256": "c" * 64})
chk("同名不同內容 → 換一個名字存，不覆蓋",
    r.get("stored_as") == NAME.replace(".tlog", "_2.tlog"), r)
with urllib.request.urlopen(
        f"{API}/api/onboard-captures/{drone_id}/{NAME}", timeout=30) as rr:
    chk("**原來那份還在而且沒被動過**",
        hashlib.sha256(rr.read()).hexdigest() == SHA)

print("\n── 9. 收尾驗章那一遍順手掃出時間範圍（TlogScan）────────")


def tlog(n, t0=1788300000.0, step=0.25):
    """合成一份 tlog：8-byte BE 微秒時間戳 ＋ MAVLink2 框架。"""
    fr = bytes([0xFD, 4, 0, 0, 7, 1, 1, 0, 0, 0]) + b"\xde\xad\xbe\xef" + b"\x00\x00"
    out = b""
    for i in range(n):
        out += int((t0 + i * step) * 1e6).to_bytes(8, "big") + fr
    return out


def push(name, blob):
    """走完整條 offer → chunk 的路，回傳清單裡的那一列。"""
    sha = hashlib.sha256(blob).hexdigest()
    st, r = req("POST", "/api/onboard-captures/offer",
                {"board_uid": UID, "name": name, "bytes": len(blob),
                 "sha256": sha})
    stored = r["stored_as"]
    req("PUT", f"/api/onboard-captures/chunk?board_uid={UID}&name={stored}"
               f"&offset=0", raw=blob, ctype="application/octet-stream")
    st, lst = req("GET", "/api/onboard-captures")
    return next((f for f in lst["files"] if f["name"] == stored), None)


N4, T0 = "20260902-093000.tlog", 1788300000.0
row = push(N4, tlog(400, T0))
cov = (row or {}).get("covers")
chk("時間範圍算出來了", cov is not None, cov)
chk("**起點是第一則訊息的時間戳**", cov and abs(cov["from"] - T0) < 0.01, cov)
chk("終點是最後一則", cov and abs(cov["to"] - (T0 + 399 * 0.25)) < 0.01, cov)
chk("框架數對得上", cov and cov["frames"] == 400, cov)

row = push("20260902-093500.tlog", os.urandom(4000))
chk("**切不動就回 null，不是猜一個範圍**（半套的答案看起來像答案）",
    (row or {}).get("covers") is None, (row or {}).get("covers"))

print("\n── 10. 未回傳即被刪除：立碑並進事件流 ──────────────────")
N5 = "20260830-120000.tlog"
s_, r = req("POST", "/api/onboard-captures/abandoned",
            {"board_uid": UID, "name": N5, "bytes": 3_500_000,
             "at": time.time() - 3600})
chk("收下了並記了一筆", s_ == 200 and r.get("noted"), r)
s_, lst = req("GET", "/api/onboard-captures")
tomb = next((f for f in lst["files"] if f["name"] == N5), None)
chk("**清單裡看得到它**（少了一趟，與「那一趟沒飛」同形）", tomb is not None)
chk("狀態是 lost，不是「沒傳完」", tomb and tomb["status"] == "lost", tomb)
chk("而且沒有下載連結", tomb and tomb["url"] is None)
ev = psql("SELECT type FROM events WHERE type = 'onboard_capture_lost' "
          f"AND drone_id = '{drone_id}'::uuid ORDER BY time DESC LIMIT 1")
chk("**進了事件流**（log 會被清掉，事件流才是留痕的地方）",
    ev == "onboard_capture_lost", ev)
s_, r = req("POST", "/api/onboard-captures/abandoned",
            {"board_uid": UID, "name": N5, "bytes": 3_500_000})
chk("重報不會多長一列", s_ == 200 and not r.get("noted"), r)
s_, r = req("POST", "/api/onboard-captures/abandoned",
            {"board_uid": UID, "name": N4, "bytes": 100})
chk("**我方已經有完整的一份就不立碑**（那不是損失）",
    s_ == 200 and not r.get("noted"), r)

print("\n── 11. 兩層覆蓋：地面站瞎掉的那一段，機上補到了嗎 ───────")
sid = psql(f"INSERT INTO flight_sessions (drone_id, started_at, ended_at) VALUES "
           f"('{drone_id}'::uuid, to_timestamp({T0 - 30}), "
           f"to_timestamp({T0 + 200})) RETURNING id")
psql(f"INSERT INTO blackouts (drone_id, session_id, started_at, ended_at, reason) "
     f"VALUES ('{drone_id}'::uuid, '{sid}'::uuid, to_timestamp({T0 + 20}), "
     f"to_timestamp({T0 + 60}), 'telemetry_lost')")
s_, cv = req("GET", f"/api/onboard-captures/coverage?session_id={sid}")
chk("查得到這個架次", s_ == 200 and cv["session_id"] == sid, s_)
chk("失明區間列出來了", len(cv["blackouts"]) == 1, cv.get("blackouts"))
b = cv["blackouts"][0]
chk("**那 40 秒機上補到了**（錄製涵蓋 0–99.75s，整段蓋住）",
    b["covered_onboard"] is True, b)
chk("機上那份也帶出來了", len(cv["onboard"]) >= 1,
    [o["name"] for o in cv["onboard"]])

psql(f"INSERT INTO blackouts (drone_id, session_id, started_at, ended_at, reason) "
     f"VALUES ('{drone_id}'::uuid, '{sid}'::uuid, to_timestamp({T0 + 150}), "
     f"to_timestamp({T0 + 190}), 'telemetry_lost')")
s_, cv = req("GET", f"/api/onboard-captures/coverage?session_id={sid}")
late = [x for x in cv["blackouts"] if x["from"] > T0 + 100][0]
chk("**錄製結束之後那一段沒補到，而且說 false 不說 true**",
    late["covered_onboard"] is False, late)

sid2 = psql(f"INSERT INTO flight_sessions (drone_id, started_at, ended_at) VALUES "
            f"('{drone_id}'::uuid, to_timestamp({T0 - 99999}), "
            f"to_timestamp({T0 - 99000})) RETURNING id")
psql(f"INSERT INTO blackouts (drone_id, session_id, started_at, ended_at, reason) "
     f"VALUES ('{drone_id}'::uuid, '{sid2}'::uuid, to_timestamp({T0 - 99500}), "
     f"to_timestamp({T0 - 99400}), 'telemetry_lost')")
s_, cv2 = req("GET", f"/api/onboard-captures/coverage?session_id={sid2}")
chk("**那個時候沒有任何機上錄製 → 回 false（我們確實有這台機的紀錄，"
    "只是沒蓋到）**", cv2["blackouts"][0]["covered_onboard"] is False, cv2["blackouts"])

print("\n── 12. metadata 在 DB、內容在磁碟（使用者裁定的形狀）───────")
row = psql("SELECT tier||'|'||status||'|'||bytes||'|'||path FROM captures "
           f"WHERE drone_id = '{drone_id}'::uuid AND name = '{N4}'")
tier, status, size, path = row.split("|")
chk("**metadata 是一列 SQL**（不是磁碟上的 .meta 檔）",
    tier == "onboard" and status == "complete" and int(size) == 9600, row)
chk("**那一列記的是路徑，內容留在磁碟**", path.endswith(f"/{N4}"), path)
inside = subprocess.run(["docker", "compose", "exec", "-T", "uav-backend",
                         "stat", "-c", "%s", path], capture_output=True,
                        text=True, cwd=CWD).stdout.strip()
chk("照那個路徑真的找得到檔案，而且大小對得上", inside == size, (inside, size))
chk("外鍵指回 drones（UID）", psql(
    "SELECT count(*) FROM captures c JOIN drones d ON d.id = c.drone_id "
    f"WHERE c.drone_id = '{drone_id}'::uuid") != "0")

print("\n── 13. 地面站那一層也在同一張表，而且對帳是冪等的 ────────")
n1 = psql("SELECT count(*) FROM captures WHERE tier = 'ground'")
req("GET", "/api/captures")
req("GET", "/api/captures")
n2 = psql("SELECT count(*) FROM captures WHERE tier = 'ground'")
chk("**對帳兩次不會長出重複列**（唯一鍵 NULLS NOT DISTINCT——"
    "普通唯一約束裡 NULL≠NULL，ON CONFLICT 永遠不成立）", n1 == n2, (n1, n2))

print("\n── 14. 刪機：外鍵連帶清列，程式負責清檔 ────────────────")
before_rows = psql(f"SELECT count(*) FROM captures WHERE drone_id = '{drone_id}'::uuid")
chk("刪之前這台機有錄製列", before_rows != "0", before_rows)
s_, r = req("DELETE", f"/api/drones/{drone_id}")
chk("刪除成功並回報清掉多少", s_ == 200 and "captures" in (r or {}).get("deleted", {}), r)
chk("**列被外鍵連帶清光**（不再靠程式一張張刪）",
    psql(f"SELECT count(*) FROM captures WHERE drone_id = '{drone_id}'::uuid") == "0")
gone = subprocess.run(["docker", "compose", "exec", "-T", "uav-backend",
                       "test", "-e", path], capture_output=True, cwd=CWD)
chk("**檔案也刪了**（外鍵清的是列，不是磁碟上的東西）", gone.returncode != 0)
chk("其他表也一起沒了（events）",
    psql(f"SELECT count(*) FROM events WHERE drone_id = '{drone_id}'::uuid") == "0")

# ── 收拾 ────────────────────────────────────────────────────────
subprocess.run(["docker", "compose", "exec", "-T", "uav-backend", "rm", "-rf",
                f"/data/mavcap/onboard/{drone_id}"], capture_output=True, cwd=CWD)
purge()
chk("**測試機收乾淨了**（留下來會出現在機隊清單裡，看起來像一台真的機）",
    psql(f"SELECT count(*) FROM drones WHERE board_uid = '{UID}'") == "0")

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
