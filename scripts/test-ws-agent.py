#!/usr/bin/env python3
"""/ws/agent 的契約測試——**不用真代理**，直接把協定規定的合法與非法訊息餵進去。

測的是三件會讓人上當的事：
  1. 版本不合要**拒絕**（協定 §3：半懂的指令比不懂的危險）
  2. 沒 hello 就送 state 要被擋（否則狀態記成無主資料）
  3. 未實作的型別要**明說**，不能靜靜吞掉（機上會以為送出去了）
外加：state 真的有推到前端的 /ws/telemetry（鏡像通不通，看的是那一端）。
"""
import json, threading, time, websocket

WS = "ws://localhost:38000/ws/agent"
TELEM = "ws://localhost:38000/ws/telemetry"
# **用專屬的測試板子 UID，不借真機那一塊**（2026-08-25 教訓）：真代理連著的
# 時候，它 1Hz 推的 state 會蓋掉測試剛寫進去的值，斷言就開始隨機失敗——
# 而那不是程式錯，是測試自己選錯了共用資源。測完刪掉。
UID = "wstest-board-0000"
def env(**kw): return json.dumps(dict(v=1, ts="2026-08-25T07:00:00.000Z", **kw))


def recv_until(c, want, timeout=5.0):
    """讀到指定型別為止。**不能只讀一則**：地面站現在對每則 state 都回 ack
    （靜默逾時機制），只讀一則會拿到 ack 而不是要驗的那則。"""
    c.settimeout(timeout)
    end = time.time() + timeout
    while time.time() < end:
        m = json.loads(c.recv())
        if m.get("type") == want:
            return m
    raise AssertionError(f"等不到 type={want}")


def setup_drone():
    """建一筆測試機（測完刪）。要有 drone_id 才驗得到「hello 認出是哪台機」。"""
    import subprocess
    subprocess.run(["docker", "compose", "exec", "-T", "uav-db", "psql",
                    "-U", "uav", "-d", "uav", "-c",
                    "INSERT INTO drones (name, serial_no, is_simulated, status, "
                    f"board_uid) VALUES ('__wstest','__wstest',true,'idle','{UID}') "
                    "ON CONFLICT (serial_no) DO NOTHING;"],
                   capture_output=True)


def teardown_drone():
    import subprocess
    subprocess.run(["docker", "compose", "exec", "-T", "uav-db", "psql",
                    "-U", "uav", "-d", "uav", "-c",
                    "DELETE FROM drones WHERE serial_no = '__wstest';"],
                   capture_output=True)


setup_drone()

seen, stop = [], threading.Event()
def watch():
    t = websocket.create_connection(TELEM, timeout=15, suppress_origin=True)
    t.settimeout(1)
    while not stop.is_set():
        try:
            m = json.loads(t.recv())
            if m.get("type") == "agent_state": seen.append(m)
        except Exception: pass
    t.close()
th = threading.Thread(target=watch, daemon=True); th.start(); time.sleep(1)

ok = True
def check(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + note) if note else ''}")

# ── 1. 版本不合 ────────────────────────────────────────────
c = websocket.create_connection(WS, timeout=10, suppress_origin=True)
c.send(json.dumps({"v": 99, "type": "hello", "board_uid": UID}))
r = json.loads(c.recv())
check("協定版本 99 被拒絕", r.get("type") == "error" and "99" in r.get("reason", ""),
      r.get("reason"))

# ── 2. 沒 hello 就送 state ──────────────────────────────────
c.send(env(type="state", state="HOLDING"))
r = json.loads(c.recv())
check("沒 hello 先送 state 被擋", r.get("type") == "error" and "hello" in r.get("reason",""),
      r.get("reason"))

# ── 3. 正常 hello ──────────────────────────────────────────
c.send(env(type="hello", board_uid=UID, agent_version="0.3.0",
           autopilot="ardupilot", fw="4.7.0 (official)",
           inputs=["telemetry"], protocol=[1]))
r = recv_until(c, "ack")
check("hello 收到 ack 並認出是哪台機",
      r.get("type") == "ack" and r.get("drone_id"), f"drone_id={r.get('drone_id')}")
check("ack 說明本站目前收哪些型別",
      r.get("accepts") == ["state", "event", "ack"], str(r.get("accepts")))

# ── 4. state 推得到前端 ────────────────────────────────────
c.send(env(type="state", state="FLYING_MISSION", sysid=1,
           mission_seq=3, mission_total=9,
           derived={"armed": True, "landed": "in_air", "mode": "AUTO",
                    "alt_rel": 41.2, "battery_pct": 76, "gs_link_ok": True}))
time.sleep(1.5)
mirrored = [m for m in seen if m.get("state") == "FLYING_MISSION"
            and m.get("board_uid") == UID]
check("state 鏡像到前端通道", mirrored,
      json.dumps(mirrored[-1], ensure_ascii=False)[:150] if mirrored else "沒收到")
check("鏡像帶著 fresh=true", mirrored and mirrored[-1].get("fresh") is True)

# ── 5. 未實作的型別要明說 ───────────────────────────────────
c.send(env(type="intent", intent_id="x", action="pause"))
r = recv_until(c, "error")
check("intent 明說未實作（不靜靜丟掉）",
      r.get("type") == "error" and "尚未實作" in r.get("reason", ""), r.get("reason"))

# ── 6. /api/drones 也看得到 ────────────────────────────────
import urllib.request
rows = json.load(urllib.request.urlopen("http://localhost:38000/api/drones"))
row = [d for d in rows if d.get("board_uid") == UID]
check("整頁載入就有值（/api/drones 帶 agent）",
      row and row[0].get("agent", {}).get("state") == "FLYING_MISSION",
      json.dumps(row[0].get("agent"), ensure_ascii=False)[:120] if row else "找不到機")

# ── 7. 斷線＝失聯，但保留最後已知狀態 ────────────────────────
seen.clear()
c.close(); time.sleep(1.5)
mine = [m for m in seen if m.get("board_uid") == UID]
last = mine[-1] if mine else None
check("斷線推播 connected=false", last and last.get("connected") is False)
check("斷線後仍保留最後已知狀態（不清成空白）",
      last and last.get("state") == "FLYING_MISSION" and last.get("fresh") is False,
      f"state={last.get('state') if last else None} fresh={last.get('fresh') if last else None}")

stop.set(); th.join(timeout=3)
teardown_drone()
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
raise SystemExit(0 if ok else 1)
