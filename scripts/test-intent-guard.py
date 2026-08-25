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


def on_intent(msg):
    """機端收到意圖時做的事——**用真的 guard.check**，只是不真的送 MAVLink。"""
    action = msg.get("action")
    st = STATE["v"]
    ok, why = guard.check(st, action)
    base = {"intent_id": msg.get("intent_id"), "action": action, "state": st,
            "executor": guard.executor(action or "")}
    if not ok:
        return {**base, "event": "guard_refused", "reason": why}
    if guard.executor(action) == "ground":
        return {**base, "event": "cleared", "reason": "守門通過，由地面站執行"}
    return {**base, "event": "sent", "reason": "已下達（測試不真的送）"}


link = IntentLink(f"ws://localhost:38000/ws/agent",
                  {"agent_version": "guardtest", "board_uid": UID,
                   "inputs": ["telemetry"], "protocol": [1],
                   "executes": sorted(guard.AGENT_ACTIONS),
                   "vets": sorted(guard.ACTIONS)},
                  on_intent=on_intent)
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


def ask(action, intent_id=None):
    body = json.dumps({"action": action, "board_uid": UID,
                       "intent_id": intent_id}).encode()
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

print("\n── 代理不見了：不能把指令擋死，也不能假裝放行 ──────────")
link.stop()
time.sleep(1.5)
r = ask("change_route")
chk("代理斷線 → no_agent（沿用本地檢查，不是擋死）",
    r["verdict"] == "no_agent", r.get("reason"))

psql("DELETE FROM drones WHERE serial_no = '__guardtest';")
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
