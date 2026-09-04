#!/usr/bin/env python3
"""通道斷了不等於失去身分（2026-09-04 使用者裁定）。

## 問題

原本 `/api/admission/{sysid}` 只認兩種：代理連著＝`admitted`、沒連著＝
`unmanaged`。而 `unmanaged` 的語意是**「不知道這台機是不是我們的」**——
於是**一條 WebSocket 斷掉，這台機就失去了身分**，板號與配號明明都還在。

後果（2026-09-02 現場）：操作員在試飛，意圖通道 flapping，所有指令端點 403。
**而同一時間指令送得到**——指令走 UDP，意圖通道走 TCP，是兩條路。

## 這支驗什麼

  1. 從來沒有代理 → `unmanaged`（不變）
  2. 代理連上且身分對得上 → `admitted`（不變）
  3. **代理斷線 → `admitted_offline`**，不是 `unmanaged`
  4. `admitted_offline` 時**只放行把飛機帶回地面的動作**（rtl／land），
     其餘 403——因為問不到機上守門，而那兩個動作不需要那個判斷
  5. 被擋的每一次都留痕（`command_log.result='refused'`）

用法：python3 scripts/test-admission-offline.py
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import websocket

WS = "ws://localhost:38000/ws/agent"
API = "http://localhost:38000"
CMD = "http://localhost:38001"
CWD = "/home/k200/uav-system"
#: **每次跑用一個新的板號。** `agent_link.links` 以 board_uid 為鍵，而它在
#: 斷線時**刻意不清空**（要保留最後已知狀態）。上一次跑留下的那筆會指著一個
#: 已經被刪掉的 drone_id，於是這次的 `hello` 對不上——**第一次跑會過、
#: 第二次開始失敗**（2026-09-04 實際踩到）。用唯一鍵把跑與跑之間隔開。
UID = f"offlinetest-{int(time.time())}"
SYSID = 196
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


def admission():
    with urllib.request.urlopen(f"{API}/api/admission/{SYSID}", timeout=8) as r:
        return json.loads(r.read())


def purge():
    """**先走刪除端點，再用 SQL 收尾。**

    裸 SQL 刪掉 `drones` 那一列**不會清掉 backend 記憶體裡的
    `agent_link.links`**——那筆記錄還指著已經不存在的 drone_id，於是下一次
    跑這支測試時 `hello` 建了新的一台，而 admission 拿舊的 link 去比對，
    永遠對不上。第一次跑會過、第二次開始失敗（2026-09-04 實際踩到）。
    """
    ids = psql("SELECT string_agg(id::text,' ') FROM drones "
               f"WHERE board_uid = '{UID}' OR mav_sysid = {SYSID}").split()
    for did in [i for i in ids if i]:
        req = urllib.request.Request(f"{API}/api/drones/{did}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=20):
                pass
        except Exception:
            pass
    for t in ("link_metrics", "telemetry", "events", "blackouts",
              "captures", "flight_sessions"):
        psql(f"DELETE FROM {t} WHERE drone_id IN "
             f"(SELECT id FROM drones WHERE board_uid = '{UID}')")
    psql(f"DELETE FROM drones WHERE board_uid = '{UID}'")


purge()
drone_id = psql(f"INSERT INTO drones (name, mav_sysid, assigned_sysid, board_uid) "
                f"VALUES ('離線入列測試機', {SYSID}, {SYSID}, '{UID}') RETURNING id")
print(f"場景：drone={drone_id[:8]} sysid={SYSID}\n")

# `admission` 的第一關看 fleet 有沒有這個 sysid，而 fleet 只有真的 MAVLink
# 遙測才會長出來。開一台假機餵它——**這不是模擬 admission，是給它真的輸入**
fake = subprocess.Popen(
    [sys.executable, "scripts/fake-drone.py", "--sysid", str(SYSID)],
    cwd=CWD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(30):
        time.sleep(1)
        if admission().get("state") != "seen":
            break
    st = admission()
    if st.get("state") == "seen":
        print("· 假機起不來或 backend 沒收到它的遙測。**skip ≠ pass**")
        raise SystemExit(0)

    print("── 1. 從來沒有代理 → unmanaged ─────────────────────────")
    chk("沒有代理時是 unmanaged", st.get("state") == "unmanaged", st.get("state"))

    print("\n── 2. 代理連上 → admitted ──────────────────────────────")
    c = websocket.create_connection(WS, timeout=10, suppress_origin=True)
    c.send(json.dumps({"v": 1, "ts": "2026-09-04T00:00:00.000Z", "type": "hello",
                       "board_uid": UID, "agent_version": "0.9.0",
                       "inputs": [], "executes": [], "vets": []}))
    time.sleep(1.5)
    st = admission()
    chk("代理連上之後是 admitted", st.get("state") == "admitted", st.get("state"))

    print("\n── 3. 代理斷線 → admitted_offline（不是 unmanaged）─────")
    c.close()
    time.sleep(2.0)
    st = admission()
    chk("**斷線之後是 admitted_offline**——身分還在，只是問不到守門",
        st.get("state") == "admitted_offline", st.get("state"))
    chk("而且說得出為什麼", "意圖通道斷了" in (st.get("reason") or ""),
        (st.get("reason") or "")[:40])
    chk("**不是 unmanaged**（那是「不知道這台機是不是我們的」）",
        st.get("state") != "unmanaged")

    print("\n── 4. 離線時只放行把飛機帶回地面的動作 ─────────────────")

    def post(path):
        req = urllib.request.Request(f"{CMD}/api/command/{SYSID}{path}",
                                     data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, {}
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    for path, label in [("/mode/hold", "暫停"), ("/arm", "解鎖"),
                        ("/mission/start", "開始任務")]:
        code, body = post(path)
        d = body.get("detail") or {}
        good = code == 403 and d.get("admission") == "admitted_offline"
        chk(f"{label} 被擋（403）", good, f"HTTP {code} {d.get('admission')}")
    code, body = post("/mode/rtl")
    d = (body.get("detail") or {})
    chk("**返航沒有被入列擋下**（它在任何狀態下的意思都一樣）",
        d.get("code") != "not_admitted", f"HTTP {code} code={d.get('code')}")

    print("\n── 5. 每一次被擋都留痕 ─────────────────────────────────")
    n = psql(f"SELECT count(*) FROM command_log WHERE sysid = {SYSID} "
             f"AND result = 'refused' AND time > now() - interval '3 minutes'")
    chk("`command_log` 裡有 refused 的紀錄", int(n or 0) >= 3, n)
    det = psql(f"SELECT detail FROM command_log WHERE sysid = {SYSID} "
               f"AND result='refused' ORDER BY time DESC LIMIT 1")
    chk("**理由說得出是通道斷了、不是身分有問題**", "意圖通道斷了" in det, det[:50])
finally:
    fake.terminate()
    fake.wait(timeout=5)
    time.sleep(1)
    purge()
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
