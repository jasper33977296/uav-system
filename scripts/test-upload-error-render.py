#!/usr/bin/env python3
"""**原始 bug 的端到端重現**：後端回 422 時，前端會不會崩潰。

422 的 detail 是**物件陣列**，直接丟進 JSX 會拋
"Objects are not valid as a React child"，整頁白畫面——
**錯誤處理本身把畫面弄壞，比原本那個錯誤嚴重得多。**
"""
import json, os, sys, time, urllib.request, websocket
t=[t for t in json.load(urllib.request.urlopen("http://127.0.0.1:39222/json")) if t["type"]=="page"][0]
c=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=30,suppress_origin=True)
n=[0]; EV=[]
def call(m,**p):
    n[0]+=1; c.send(json.dumps({"id":n[0],"method":m,"params":p}))
    while True:
        r=json.loads(c.recv())
        if r.get("id")==n[0]:
            if "error" in r: raise SystemExit(f"CDP {m}: {r['error']}")
            return r.get("result",{})
        EV.append(r)
def ev(e): return call("Runtime.evaluate",expression=e,returnByValue=True)["result"].get("value")

call("Runtime.enable"); call("DOM.enable")
call("Page.navigate",url="http://localhost:33000/missions"); time.sleep(5)
doc = call("DOM.getDocument")["root"]["nodeId"]
node = call("DOM.querySelector", nodeId=doc, selector='input[type=file]')["nodeId"]
call("DOM.setFileInputFiles", files=[os.path.abspath("too_many.plan")], nodeId=node)
time.sleep(4)
try:
    c.settimeout(0.5)
    while True: EV.append(json.loads(c.recv()))
except Exception: pass
c.settimeout(30)
crash = [e for e in EV if e.get("method")=="Runtime.exceptionThrown"]
txt = ev("document.body.innerText") or ""
ok = True
def chk(l, cond, note=""):
    global ok; ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {l}{('｜'+str(note)) if note else ''}")
chk("沒有未捕捉例外", not crash,
    (crash[0]['params']['exceptionDetails'].get('exception',{}).get('description','')[:90]) if crash else "")
chk("頁面沒有變成白畫面", "上傳 .plan" in txt, f"可見 {len(txt)} 字")
line = next((l for l in txt.split("\n") if "waypoints" in l or "至少" in l or "2 items" in l), "")
chk("錯誤訊息讀得懂（不是 [object Object]）",
    line and "[object Object]" not in txt, line[:100])
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
