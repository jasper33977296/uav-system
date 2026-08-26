#!/usr/bin/env python3
"""事件卡的分機切換：單機時不出現、多機時出現並真的分得開。

**多機的部分用注入的 WS 訊息驗**：地面站的 fleet 是遙測餵出來的，而現場
只有一台機——但這正是要驗的東西不能只靠讀碼的原因：「兩台機的事件會不會
分開」在只有一台機的環境裡永遠測不到。
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

# **在頁面腳本跑之前**接管 WebSocket：這樣才攔得到 /ws/telemetry
call("Page.enable")
call("Page.addScriptToEvaluateOnNewDocument", source=r"""
(() => {
  const Real = window.WebSocket;
  window.__wsHooked = [];
  window.WebSocket = function (url, proto) {
    const ws = new Real(url, proto);
    if (String(url).includes('/ws/telemetry')) window.__wsHooked.push(ws);
    return ws;
  };
  window.WebSocket.prototype = Real.prototype;
  Object.assign(window.WebSocket, Real);
  window.__feed = (obj) => {
    for (const ws of window.__wsHooked)
      ws.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(obj)}));
  };
})();
""")
call("Emulation.setDeviceMetricsOverride",width=1500,height=1000,deviceScaleFactor=1,mobile=False)
call("Page.navigate",url="http://localhost:33000/"); time.sleep(8)

ok = True
def chk(l, cond, note=""):
    global ok; ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {l}{('｜'+str(note)) if note else ''}")

def openDrawer():
    ev("""(() => {
      const b = [...document.querySelectorAll('button,span')].find(
        e => e.textContent.trim() === '▤');
      b?.click(); return 1; })()""")

openDrawer(); time.sleep(1.5)
txt = ev("document.body.innerText") or ""
chk("單機時不出現分機切換（多一顆按鈕只是多一個要理解的東西）",
    "全機" not in txt)

# 注入第二台機的遙測 → fleet 變兩台
for i, (did, name) in enumerate([("fake-a-0001", "測試機A"), ("fake-b-0002", "測試機B")]):
    ev(f"""window.__feed({{type:'telemetry', drone_id:'{did}', drone_name:'{name}',
      connected:true, armed:false, lat:24.77, lon:121.04, alt_rel:0,
      session_id:null, link:{{}}, mav_sysid:{20+i}}})""")
time.sleep(1.5)
# 兩台各一則事件
ev("""window.__feed({type:'event', event:{id:900001, time:new Date().toISOString(),
   severity:'info', type:'statustext', detail:{text:'AAA 只屬於測試機A'},
   drone:'測試機A', drone_id:'fake-a-0001', source:'vehicle'}})""")
ev("""window.__feed({type:'event', event:{id:900002, time:new Date().toISOString(),
   severity:'info', type:'statustext', detail:{text:'BBB 只屬於測試機B'},
   drone:'測試機B', drone_id:'fake-b-0002', source:'vehicle'}})""")
ev("""window.__feed({type:'event', event:{id:900003, time:new Date().toISOString(),
   severity:'warning', type:'sysid_addr_change', detail:{note:'CCC 系統層事件'},
   drone:null, drone_id:null, source:'system'}})""")
time.sleep(2)
txt = ev("document.body.innerText") or ""
chk("多機時出現分機切換", "全機" in txt)
# **焦點預設是原本選中的那台**（現場真的那台），所以此刻 A、B 都不該出現。
# 這本身就是一個要驗的行為：分機檢視不會把別台的事件混進來
chk("預設焦點不是注入的那兩台 → A、B 都不顯示",
    "AAA" not in txt and "BBB" not in txt)
# 點色點切到測試機A
ev("""(() => { const b=[...document.querySelectorAll('button')]
    .find(e=>e.textContent.trim()==='測試機A'); b?.click(); return 1; })()""")
time.sleep(1.5)
txt = ev("document.body.innerText") or ""
chk("切到測試機A → 看得到 A 的事件", "AAA" in txt)
chk("預設只看選中機：看不到 B 的事件", "BBB" not in txt)
chk("**來源不明的系統事件不藏**（那是最不該被藏的一類）", "CCC" in txt)

# 切「全機」
ev("""(() => { const b=[...document.querySelectorAll('button')]
    .find(e=>e.textContent.trim()==='全機'); b?.click(); return 1; })()""")
time.sleep(1.5)
txt2 = ev("document.body.innerText") or ""
chk("切「全機」後兩台的事件都看得到",
    "AAA" in txt2 and "BBB" in txt2)
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
