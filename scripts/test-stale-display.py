#!/usr/bin/env python3
"""A 層：資料舊了，數值本身要被拿掉，不是只變灰。

**注入時要帶 primary:true**：store 只在 `id === (selectedId ?? primaryId)` 時
更新 `live`，不帶旗標的話注入的機永遠當不上 live——那時測到的是「什麼都沒
顯示」，而它會讓「數值被拿掉」那一格假通過。

**用注入的遙測驗**：現場那台機現在根本沒連線，而要測的是「資料從新鮮變舊」
這個過程——那在只有斷線狀態的環境裡測不到。
"""
import json, sys, time, urllib.request, websocket
t=[t for t in json.load(urllib.request.urlopen("http://127.0.0.1:39222/json")) if t["type"]=="page"][0]
c=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=30,suppress_origin=True)
n=[0]
def call(m,**p):
    n[0]+=1; c.send(json.dumps({"id":n[0],"method":m,"params":p}))
    while True:
        r=json.loads(c.recv())
        if r.get("id")==n[0]:
            if "error" in r: raise SystemExit(f"CDP {m}: {r['error']}")
            return r.get("result",{})
def ev(e): return call("Runtime.evaluate",expression=e,returnByValue=True)["result"].get("value")

# **要先 Page.enable**：沒開 domain 時 addScriptToEvaluateOnNewDocument
# 不會生效，而它不會報錯——症狀是注入的東西全部沒作用，看起來像元件壞了
call("Page.enable")
call("Page.addScriptToEvaluateOnNewDocument", source=r"""
(() => { const R = window.WebSocket; window.__ws = [];
  window.WebSocket = function (u, p) { const w = new R(u, p);
    if (String(u).includes('/ws/telemetry')) window.__ws.push(w); return w; };
  window.WebSocket.prototype = R.prototype; Object.assign(window.WebSocket, R);
  window.__feed = (o) => { for (const w of window.__ws)
    w.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(o)})); };
})();""")
call("Emulation.setDeviceMetricsOverride",width=1400,height=900,deviceScaleFactor=1,mobile=False)
call("Page.navigate",url="http://localhost:33000/"); time.sleep(8)

def feed(age):
    ev(f"""window.__feed({{type:'telemetry', drone_id:'stale-test', drone_name:'測試',
      primary:true, connected:true, armed:true, lat:24.773, lon:121.046, alt_rel:37.4,
      ground_speed:5.2, battery_pct:66, gps_fix:3, flight_mode:'AUTO',
      mode_verb:'mission', session_id:null, mav_sysid:7,
      telem_age_s:{age}, link:{{sinr:20}}}})""")

ok = True
def chk(l, cond, note=""):
    global ok; ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {l}{('｜'+str(note)) if note else ''}")

feed(0.5); time.sleep(1.5)
t1 = ev("document.body.innerText") or ""
chk("新鮮（0.5s）：高度顯示 37m", "37m" in t1)
chk("新鮮：沒有舊資料橫幅", "最後已知" not in t1)

feed(5); time.sleep(1.5)
t2 = ev("document.body.innerText") or ""
chk("稍舊（5s）：數值還在，但標出年齡", "37m" in t2 and "5 秒前" in t2, )

feed(600); time.sleep(1.5)
t3 = ev("document.body.innerText") or ""
chk("過舊（600s）：**數值被拿掉**（不是只變灰）", "37m" not in t3)
chk("過舊：常駐橫幅說出這是最後已知", "最後已知" in t3 and "10 分鐘前" in t3,
    next((l for l in t3.split("\n") if "最後已知" in l), "")[:70])

feed(0.5); time.sleep(1.5)
t4 = ev("document.body.innerText") or ""
chk("**反向驗證**：資料回來後數值也回來、橫幅消失",
    "37m" in t4 and "最後已知" not in t4)
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
