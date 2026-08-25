#!/usr/bin/env python3
"""意圖守門端到端：backend 端點 → WebSocket → **真的守門模組** → event → 判決。

**用 uav-agent 真正的 guard.py 與 intent.py**，不是模擬——這條鏈路的價值在於
「機上說不行，地面站就真的做不了」，而中間任何一段自己寫一份假的，測到的就
不是那件事。假的只有飛行狀態（手餵），因為總不能為了測試把真飛機弄成 RETURNING。

用法：python3 scripts/test-intent-guard.py
"""
import json
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, "/home/k200/uav-agent")

try:
    import guard
    from intent import IntentLink
except ImportError as e:
    print(f"skip：找不到 uav-agent 的模組（{e}）。**skip ≠ pass**")
    sys.exit(2)

API = "http://localhost:38000"
UID = "guardtest-board-0000"
STATE = {"v": "READY"}          # 測試中手動改這台假機的狀態


def psql(sql):
    subprocess.run(["docker", "compose", "exec", "-T", "uav-db", "psql",
                    "-U", "uav", "-d", "uav", "-c", sql],
                   capture_output=True, cwd="/home/k200/uav-system")


psql("INSERT INTO drones (name, serial_no, is_simulated, status, board_uid) "
     f"VALUES ('__guardtest','__guardtest',true,'idle','{UID}') "
     "ON CONFLICT (serial_no) DO NOTHING;")


import route  # noqa: E402

LAT, LON = 25.0550, 121.5060
POS = {"lat": LAT, "lon": LON, "alt_rel": 30.0, "heading": 0.0}
WPS = [{"seq": 0, "lat": LAT - 400 / 111000.0, "lon": LON, "alt": 30.0,
        "action": "waypoint", "command": 16},
       {"seq": 1, "lat": LAT + 800 / 111000.0, "lon": LON, "alt": 30.0,
        "action": "waypoint", "command": 16}]
PROPS, SEQ = {}, {"v": None}


def on_intent(msg):
    """機端收到意圖時做的事——**用真的 guard.check 與 route**，
    只是狀態與位置是手餵的（總不能為了測試把真飛機弄成 FLYING_MISSION）。"""
    action = msg.get("action")
    st = STATE["v"]
    ok, why = guard.check(st, action)
    base = {"intent_id": msg.get("intent_id"), "action": action, "state": st,
            "executor": guard.executor(action or "")}
    if not ok:
        return {**base, "event": "guard_refused", "reason": why}
    if action == "change_route":
        prop = route.build_proposal(
            wps=WPS, cur=dict(POS), hold_alt=25.0,
            mission_name="測試航線", mission_id="m1")
        PROPS[msg["intent_id"]] = prop
        return {**base, "event": "proposal", "proposal": prop}
    if guard.executor(action) == "ground":
        return {**base, "event": "cleared", "reason": "守門通過，由地面站執行"}
    return {**base, "event": "sent", "reason": "已下達（測試不真的送）"}


def on_decision(msg):
    iid = msg.get("intent_id")
    base = {"intent_id": iid, "action": "change_route", "state": STATE["v"],
            "executor": "ground"}
    prior = PROPS.get(iid)
    if prior is None:
        return {**base, "event": "failed", "reason": "沒有這個提案"}
    if not msg.get("approved"):
        PROPS.pop(iid, None)
        return {**base, "event": "cancelled", "reason": "人取消"}
    ok, why = guard.check(STATE["v"], "change_route")
    if not ok:
        return {**base, "event": "guard_refused", "reason": why}
    now = route.build_proposal(wps=WPS, cur=dict(POS), hold_alt=25.0,
                               mission_name="測試航線", mission_id="m1")
    drift = route.drift_reason(prior, now)
    if drift:
        PROPS[iid] = now
        return {**base, "event": "guard_refused", "proposal": now,
                "reason": f"提案已經過期：{drift}"}
    SEQ["v"] = {"intent_id": iid, "step": "approved"}
    return {**base, "event": "cleared", "proposal": now}


def on_progress(msg):
    iid, stp = msg.get("intent_id"), msg.get("step")
    if SEQ["v"] and SEQ["v"]["intent_id"] == iid:
        SEQ["v"]["step"] = stp
        if stp in ("resume", "done") or not msg.get("ok", True):
            SEQ["v"] = None
    return {"intent_id": iid, "action": "change_route", "step": stp,
            "state": STATE["v"], "executor": "ground", "event": "noted"}


link = IntentLink(f"ws://localhost:38000/ws/agent",
                  {"agent_version": "guardtest", "board_uid": UID,
                   "inputs": ["telemetry"], "protocol": [1],
                   "executes": sorted(guard.AGENT_ACTIONS),
                   "vets": sorted(guard.ACTIONS)},
                  on_intent=on_intent, on_decision=on_decision,
                  on_progress=on_progress)
link.start()
for _ in range(80):
    if link.connected:
        break
    time.sleep(0.1)

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def ask(action, intent_id=None, kind="intent", params=None):
    body = json.dumps({"kind": kind, "action": action, "board_uid": UID,
                       "intent_id": intent_id, "params": params}).encode()
    req = urllib.request.Request(f"{API}/api/agent/intent", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


chk("假代理連上意圖通道", link.connected)
link.publish({"state": STATE["v"], "sysid": 9, "derived": {"armed": False}})
time.sleep(1)

print("\n── 守門的判決真的傳得回來 ─────────────────────────────")
STATE["v"] = "PILOT_CONTROL"
r = ask("change_route")
chk("飛手接管時改航線被擋", r["verdict"] == "refused", r.get("reason"))
chk("擋下的理由原樣傳回地面站", "遙控器" in (r.get("reason") or ""),
    r.get("reason"))

STATE["v"] = "FLYING_MISSION"
r = ask("change_route")
chk("飛任務中改航線放行（由地面站執行）",
    r["verdict"] == "cleared" and r["event"]["executor"] == "ground",
    f"{r['verdict']}/{r['event']['executor']}")

r = ask("start_mission")
chk("飛任務中重下 start_mission 被擋", r["verdict"] == "refused", r.get("reason"))

r = ask("rtl")
chk("飛任務中 rtl 由**代理**執行",
    r["verdict"] == "done" and r["event"]["executor"] == "agent",
    f"{r['verdict']}/{r['event']['executor']}")

STATE["v"] = "READY"
r = ask("rtl")
chk("機在地上時 rtl 被擋（那是沒有人按下的起飛）",
    r["verdict"] == "refused", r.get("reason"))

print("\n── 冪等：同一個 intent_id 重送不會再做一次 ─────────────")
STATE["v"] = "FLYING_MISSION"
iid = "fixed-intent-id-0001"
r1 = ask("rtl", iid)
r2 = ask("rtl", iid)
chk("重送回同一個結果", r1["event"]["event"] == r2["event"]["event"],
    f"{r1['event']['event']} / {r2['event']['event']}")
chk("重送被標記為 replayed（沒有再執行一次）",
    r2["event"].get("replayed") is True, r2["event"])

print("\n── 守門的判決要進事件流（不是只有呼叫端看得到）─────────")
with urllib.request.urlopen(f"{API}/api/events?limit=30", timeout=10) as r:
    evs = json.loads(r.read().decode())
kinds = [e.get("type") for e in evs]
chk("事件流有 intent_guard_refused", "intent_guard_refused" in kinds,
    sorted(set(k for k in kinds if k.startswith("intent_"))))

print("\n── 提案 → 確認 → 逐步回報（協定 §4.4／§4.5／§4.6）─────────")
STATE["v"] = "FLYING_MISSION"
r = ask("change_route")
prop = r["event"].get("proposal")
iid = r["intent_id"]
chk("提案算得出續飛航點", prop and prop["resume_wp"], prop and prop["resume_wp"])
chk("提案說得出會先掉頭",
    any("掉頭" in w for w in (prop or {}).get("warnings", [])),
    (prop or {}).get("warnings"))

r = ask("change_route", iid, kind="decision", params={"approved": True})
chk("確認 → cleared 並開始序列", r["verdict"] == "cleared", r.get("reason"))
chk("代理記下序列開始", SEQ["v"] and SEQ["v"]["step"] == "approved", SEQ["v"])

for stp in ("hold", "upload", "goto"):
    r = ask("change_route", iid, kind="progress",
            params={"step": stp, "ok": True})
    chk(f"回報 {stp} → noted", r["event"]["event"] == "noted")
chk("序列走到 goto 仍在進行中", SEQ["v"] and SEQ["v"]["step"] == "goto", SEQ["v"])
ask("change_route", iid, kind="progress", params={"step": "resume", "ok": True})
chk("最後一步之後序列結束（旗標清掉）", SEQ["v"] is None, SEQ["v"])

print("\n── 提案過期：**不照過期的做，也不自作主張用新的** ───────")
r = ask("change_route")
iid2 = r["intent_id"]
POS["lat"] = LAT + 800 / 111000.0        # 機體飛到第二個航點附近＝最近點換人
r = ask("change_route", iid2, kind="decision", params={"approved": True})
chk("確認時提案已過期 → 擋下", r["verdict"] == "refused", r.get("reason"))
chk("擋下時附上新提案要人重看", (r["event"] or {}).get("proposal") is not None)
POS["lat"] = LAT

print("\n── 人取消 ─────────────────────────────────────────────")
r = ask("change_route")
r = ask("change_route", r["intent_id"], kind="decision",
        params={"approved": False})
chk("取消 → cancelled，不執行", r["verdict"] == "cancelled", r.get("reason"))

print("\n── 確認之後狀態變了：守門位階高於人的確認（§5.2）─────────")
r = ask("change_route")
iid3 = r["intent_id"]
STATE["v"] = "PILOT_CONTROL"             # 飛手在這段時間拿起遙控器
r = ask("change_route", iid3, kind="decision", params={"approved": True})
chk("人按了確認，代理仍然拒絕", r["verdict"] == "refused", r.get("reason"))
STATE["v"] = "FLYING_MISSION"

print("\n── 代理不見了：不能把指令擋死，也不能假裝放行 ──────────")
link.stop()
time.sleep(1.5)
r = ask("change_route")
chk("代理斷線 → no_agent（沿用本地檢查，不是擋死）",
    r["verdict"] == "no_agent", r.get("reason"))

psql("DELETE FROM drones WHERE serial_no = '__guardtest';")
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
