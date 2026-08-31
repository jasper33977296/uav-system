#!/usr/bin/env python3
"""失聯期間的意圖：壓下來、恢復後補送（issues/039 複裁 G）。

**測的是「系統沒有替人做決定」這件事。** 三個容易寫錯的地方：

  1. 失聯中送 intent 要回 `queued` 而**不是** `no_agent`——後者在指令服務那端
     是**放行**（沿用本地檢查），而失聯時放行沒有意義：指令與意圖走同一條
     5G，那一刻它根本送不到飛機。
  2. 補送過去的每一則都必須帶 `dry_run`。少了它，一個人在四十秒前按下的
     「更換任務」會在恢復的那一瞬間真的執行——而那正是本案要防的事。
  3. 補送只做一次。留著自動重試，等於把過期的意圖變成一顆定時炸彈。

不需要真代理：直接扮一個代理連上 /ws/agent。
"""
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

import websocket

WS = "ws://localhost:38000/ws/agent"
TELEM = "ws://localhost:38000/ws/telemetry"
API = "http://localhost:38000/api/agent/intent"
# 專屬測試板子：真代理的 1Hz state 會蓋掉共用 UID 的值（test-ws-agent.py 的教訓）
UID = "replaytest-board-0000"
VETS = ["change_route", "start_mission", "rtl", "land", "abort", "pause",
        "resume", "disarm"]

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  ← {detail}" if detail else ""))


def env(**kw):
    return json.dumps(dict(v=1, ts="2026-08-31T07:00:00.000Z", **kw))


def psql(sql):
    subprocess.run(["docker", "compose", "exec", "-T", "uav-db", "psql",
                    "-U", "uav", "-d", "uav", "-c", sql], capture_output=True)


def post(**body):
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.loads(e.read().decode() or "{}")}


def agent_connect():
    c = websocket.create_connection(WS, timeout=6)
    c.send(env(type="hello", board_uid=UID, agent_version="test-replay",
               autopilot="ardupilot", fw="4.7.0", inputs=["telemetry"],
               executes=["rtl", "land", "abort"], vets=VETS, protocol=[1]))
    c.settimeout(6)
    json.loads(c.recv())          # hello 的 ack
    return c


psql("INSERT INTO drones (name, serial_no, is_simulated, status, board_uid) "
     f"VALUES ('__replaytest','__replaytest',true,'idle','{UID}') "
     "ON CONFLICT (serial_no) DO NOTHING;")

# 前端鏡像那一端：補送結果要真的推到 /ws/telemetry，不是只寫進 log
seen, stop = [], threading.Event()


def watch():
    t = websocket.create_connection(TELEM, timeout=6)
    t.settimeout(1.0)
    while not stop.is_set():
        try:
            seen.append(json.loads(t.recv()))
        except Exception:
            pass
    t.close()


th = threading.Thread(target=watch, daemon=True)
th.start()
time.sleep(0.5)

# ── 1. 先連一次再斷：讓地面站認得這塊板子，然後進入「有代理但失聯」 ──
c = agent_connect()
c.send(env(type="state", state="FLYING_MISSION", mission_seq=2,
           mission_total=6, rc_link=True))
time.sleep(0.4)
c.close()
time.sleep(0.6)

# ── 2. 失聯中送 intent：壓下來，回 queued ──────────────────────
r1 = post(kind="intent", action="change_route", board_uid=UID,
          params={"mission_id": "m-1", "hold_alt": 30})
check("失聯中的 intent 回 queued（不是 no_agent＝放行）",
      r1.get("verdict") == "queued", json.dumps(r1, ensure_ascii=False)[:140])
check("回話說得出「沒有送出去」", "沒有送出去" in (r1.get("reason") or ""))
r2 = post(kind="intent", action="start_mission", board_uid=UID, params={})
check("第二則也壓進佇列（pending=2）", r2.get("pending") == 2,
      f"pending={r2.get('pending')}")

# ── 3. 不該壓的兩類 ────────────────────────────────────────
r3 = post(kind="decision", action="change_route", board_uid=UID,
          params={"approved": True})
check("decision 不壓佇列（那件事在機上早就自己收尾了）",
      r3.get("verdict") == "unknown", json.dumps(r3, ensure_ascii=False)[:120])
r4 = post(kind="intent", action="mission_upload", board_uid=UID, params={})
check("代理沒宣告守的意圖仍回 no_agent（守門這層不存在，不是失聯）",
      r4.get("verdict") == "no_agent", json.dumps(r4, ensure_ascii=False)[:120])

# ── 4. 佇列上限：滿了丟最舊的，而且說得出丟了幾則 ────────────
for i in range(8):
    rN = post(kind="intent", action="rtl", board_uid=UID, params={"n": i})
check("佇列有上限（PENDING_MAX=8）", rN.get("pending") == 8,
      f"pending={rN.get('pending')}")
check("丟掉的則數有回報（不是安靜地丟）", rN.get("dropped", 0) > 0,
      f"dropped={rN.get('dropped')}")

# ── 5. 重連：補送，而且每一則都必須是乾跑 ──────────────────
seen.clear()
c = agent_connect()
c.settimeout(8)
replayed, deadline = [], time.time() + 12
while time.time() < deadline and len(replayed) < 8:
    try:
        m = json.loads(c.recv())
    except Exception:
        break
    if m.get("type") == "intent":
        replayed.append(m)
        # 回一則 event，讓地面站那邊的等待結束（否則它會等滿 4 秒逾時）
        c.send(env(type="event", event="cleared", intent_id=m["intent_id"],
                   action=m.get("action"), state="FLYING_MISSION"))

check("重連後補送了佇列裡的 intent", len(replayed) == 8, f"收到 {len(replayed)} 則")
check("**每一則都帶 dry_run**（補送是重新問判決，不是重新執行）",
      replayed and all(m.get("params", {}).get("dry_run") is True
                       for m in replayed),
      json.dumps([m.get("params") for m in replayed[:2]], ensure_ascii=False)[:140])
check("丟掉最舊的之後，留下的是最後那幾則",
      [m["action"] for m in replayed] == ["rtl"] * 8,
      str([m["action"] for m in replayed]))

time.sleep(1.0)
mine = [m for m in seen if m.get("type") == "agent_intent_replay"]
check("補送結果推到前端（不是只寫進 log）", len(mine) == 8, f"推了 {len(mine)} 則")
check("推播帶得出「多久以前按的」", mine and all(m.get("age_s") is not None
                                              for m in mine))

# ── 6. 補送只做一次 ──────────────────────────────────────
c.close()
time.sleep(0.6)
c2 = agent_connect()
c2.settimeout(3)
again = []
end = time.time() + 3
while time.time() < end:
    try:
        m = json.loads(c2.recv())
    except Exception:
        break
    if m.get("type") == "intent":
        again.append(m)
check("再連一次不會重播（留著自動重試＝過期意圖變定時炸彈）",
      not again, f"又收到 {len(again)} 則")

# ── 7. ack 是合法型別，不能被當成「未實作」退回 ─────────────
c2.send(env(type="ack", of="intent", intent_id="no-such-id"))
time.sleep(0.5)
c2.settimeout(1.0)
errs = []
try:
    while True:
        m = json.loads(c2.recv())
        if m.get("type") == "error":
            errs.append(m)
except Exception:
    pass
check("代理的 ack 不會被退成「型別尚未實作」", not errs,
      json.dumps(errs[:1], ensure_ascii=False)[:120])

c2.close()
stop.set()
th.join(timeout=3)
psql("DELETE FROM drones WHERE serial_no = '__replaytest';")
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
raise SystemExit(0 if ok else 1)
