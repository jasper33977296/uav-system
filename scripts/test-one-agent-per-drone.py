#!/usr/bin/env python3
"""一台機一個代理：第二條連線進來時，舊的要讓位而不是兩條並存。

**為什麼不是「先到先贏」**：最常見的情況是半開的 TCP——舊連線其實已經死了、
只是還沒 FIN。讓舊的贏會讓真正活著的代理永遠連不上，而畫面上看起來一切正常
（有連線、有狀態），只是狀態永遠停在斷線前那一刻。
"""
import json, sys, time, websocket

UID = "twoagent-test-0000"
WS = "ws://localhost:38000/ws/agent"
def env(**kw): return json.dumps(dict(v=1, ts="2026-08-25T00:00:00.000Z", **kw))
def hello(): return env(type="hello", board_uid=UID, agent_version="A",
                        inputs=["telemetry"], protocol=[1], vets=["rtl"])

a = websocket.create_connection(WS, timeout=10, suppress_origin=True)
a.send(hello()); a.recv()
a.send(env(type="state", state="READY")); a.recv()
print("✓ 第一條連線建立並推了狀態")

b = websocket.create_connection(WS, timeout=10, suppress_origin=True)
b.send(hello()); b.recv()
print("✓ 第二條連線建立")

# 舊的應該被關掉：讀它會拿到 close
a.settimeout(5)
closed = False
try:
    while True:
        a.recv()
except websocket.WebSocketConnectionClosedException:
    closed = True
except Exception as e:
    closed = "closed" in str(e).lower()
print(f"{'✓' if closed else '✗'} 舊連線被關掉（不是兩條並存）")

# 新的還能用
b.send(env(type="state", state="FLYING_MISSION"))
b.settimeout(5)
ok2 = json.loads(b.recv()).get("type") == "ack"
print(f"{'✓' if ok2 else '✗'} 新連線正常運作")
b.close()
sys.exit(0 if closed and ok2 else 1)
