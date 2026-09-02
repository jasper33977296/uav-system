#!/usr/bin/env python3
"""刪掉一台機之後，它要從**畫面上**消失，不只是從資料庫消失。

**2026-09-02 實際發生**：三台測試機從 `drones` 表刪掉了，即時頁卻還在顯示
`uav-s42`。原因是執行期另外握著三份狀態——機隊註冊表（廣播迴圈每 0.2 秒送
一次它的最後已知位置）、sysid 對照表、意圖通道——而刪除只動了資料庫。

**症狀最惡的地方在於它與「一台只是斷線的真機」完全同形**：兩者都是灰的、
都顯示最後已知位置，沒有任何地方說得出「這台已經不存在了」。

這支驗四件事：
  1. 刪除回應說得出清掉了幾個執行期 sysid
  2. **WebSocket 上會收到 `drone_removed`**（前端據此清自己的表——它也是累積的）
  3. 刪完之後 `/api/drones` 與廣播裡都沒有它
  4. 連線中的機拒刪（既有守門，一併回歸）

用法：python3 scripts/test-drone-delete-purge.py
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

API = "http://localhost:38000"
CWD = "/home/k200/uav-system"
UID = "deletepurge-test-0001"

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


def req(method, path):
    r = urllib.request.Request(f"{API}{path}", method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, None


psql(f"DELETE FROM drones WHERE board_uid = '{UID}'")
did = psql(f"INSERT INTO drones (name, mav_sysid, board_uid) VALUES "
           f"('刪除測試機', 198, '{UID}') RETURNING id")
print(f"場景：drone_id={did}\n")

print("── 1. 刪除同時清執行期，並在 WS 上說一聲 ──────────────")
# **在同一個行程裡邊聽 WS 邊刪**：`drone_removed` 是即時的，事後查不到
tap = subprocess.Popen(
    ["docker", "compose", "exec", "-T", "uav-backend", "python3", "-c", f"""
import asyncio, json, websockets
async def main():
    async with websockets.connect('ws://localhost:38000/ws/telemetry') as ws:
        print('READY', flush=True)
        try:
            async with asyncio.timeout(20):
                while True:
                    m = json.loads(await ws.recv())
                    if m.get('type') == 'drone_removed':
                        print('REMOVED ' + str(m.get('drone_id')), flush=True)
                        return
        except (asyncio.TimeoutError, TimeoutError):
            print('TIMEOUT', flush=True)
asyncio.run(main())
"""], stdout=subprocess.PIPE, text=True, cwd=CWD)
assert tap.stdout.readline().strip() == "READY"

s, r = req("DELETE", f"/api/drones/{did}")
chk("刪除成功", s == 200, (s, r))
chk("**回應說得出清掉幾個執行期 sysid**（沒有這個數字，"
    "「清了沒」就只能靠猜）", "runtime_sysids" in (r or {}).get("deleted", {}), r)

line = tap.stdout.readline().strip()
tap.wait(timeout=25)
chk("**WS 上收到 drone_removed**（前端的機隊表也是累積的，"
    "不主動說一聲就要重新整理才看得到）", line == f"REMOVED {did}", line)

print("\n── 2. 刪完之後哪裡都不該再看得到它 ────────────────────")
s, ds = req("GET", "/api/drones")
chk("`/api/drones` 沒有它", not [d for d in ds if d["id"] == did])
chk("資料庫也沒有", psql(f"SELECT count(*) FROM drones WHERE id = '{did}'::uuid") == "0")

tap2 = subprocess.run(
    ["docker", "compose", "exec", "-T", "uav-backend", "python3", "-c", f"""
import asyncio, json, websockets
async def main():
    ids = set()
    async with websockets.connect('ws://localhost:38000/ws/telemetry') as ws:
        try:
            async with asyncio.timeout(4):
                while True:
                    m = json.loads(await ws.recv())
                    if m.get('type') == 'telemetry':
                        ids.add(m.get('drone_id'))
        except (asyncio.TimeoutError, TimeoutError):
            pass
    print('{did}' in ids)
asyncio.run(main())
"""], capture_output=True, text=True, cwd=CWD)
chk("**廣播裡也沒有它了**（否則即時頁會繼續畫一台不存在的機，"
    "而且長得跟「只是斷線」一模一樣）",
    tap2.stdout.strip().endswith("False"), tap2.stdout.strip())

print("\n── 3. 連線中的機拒刪（既有守門）────────────────────────")
s, live = req("GET", "/api/live")
if live and live.get("drone_id"):
    s, r = req("DELETE", f"/api/drones/{live['drone_id']}")
    chk("連線中的機刪不掉", s == 409, (s, r))
else:
    print("· 現在沒有連線中的機，這一格跳過（**skip ≠ pass**）")

psql(f"DELETE FROM drones WHERE board_uid = '{UID}'")
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
