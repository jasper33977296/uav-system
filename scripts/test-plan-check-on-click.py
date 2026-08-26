#!/usr/bin/env python3
"""點開一份航線 → 展開區要出現這一份的預檢報告（每次點都重算）。"""
import json, time, urllib.request, websocket
t=[t for t in json.load(urllib.request.urlopen("http://127.0.0.1:39222/json")) if t["type"]=="page"][0]
c=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=30,suppress_origin=True)
n=[0]
def call(m,**p):
    n[0]+=1; c.send(json.dumps({"id":n[0],"method":m,"params":p}))
    while True:
        r=json.loads(c.recv())
        if r.get("id")==n[0]: return r.get("result",{})
def ev(e): return call("Runtime.evaluate",expression=e,returnByValue=True)["result"].get("value")

call("Emulation.setDeviceMetricsOverride",width=1400,height=1000,deviceScaleFactor=1,mobile=False)
call("Page.navigate",url="http://localhost:33000/missions"); time.sleep(5)
names = ev("[...document.querySelectorAll('.mcard-name')].map(e=>e.textContent)")
print("卡片：", names)

ok = True
for idx in (0, 1):
    ev(f"document.querySelectorAll('.mcard')[{idx}].click()")
    time.sleep(2.5)
    txt = ev("document.body.innerText")
    # 報告有四種長相：通過(✅)／有問題(❌)／只有警告(⚠️)／載入中。
    # 第一版漏了 ⚠️，於是「只有警告」的那份被判成沒有報告——**斷言比被測的
    # 東西還窄，測到的就不是那件事**
    has = any(k in txt for k in ("幾何預檢通過", "❌", "⚠️", "檢查中"))
    print(f"{'✓' if has else '✗'} 點第 {idx+1} 張卡 → 展開區有預檢結果")
    ok &= has
    line = next((l for l in txt.split("\n") if l.startswith(("❌","✅","⚠️"))), "")
    print("   首則：", line[:100])
print("全部通過" if ok else "**有未通過**")
