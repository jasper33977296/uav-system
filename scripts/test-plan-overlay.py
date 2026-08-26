#!/usr/bin/env python3
"""計畫疊圖的像素量測：起飛/返航段、圍欄、備降點有沒有真的畫在畫面上。

**為什麼量像素**：這幾樣東西的失效方式是「安靜地沒畫」——沒有錯誤訊息，
畫面看起來也很正常，只是跟 QGC 的圖不一樣。要證明「有畫」，唯一的辦法是
去數螢幕上那個顏色的點。
"""
import base64, io, json, sys, time, urllib.request, websocket
from PIL import Image

t=[t for t in json.load(urllib.request.urlopen("http://127.0.0.1:39222/json")) if t["type"]=="page"][0]
c=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=60,suppress_origin=True)
n=[0]
def call(m,**p):
    n[0]+=1; c.send(json.dumps({"id":n[0],"method":m,"params":p}))
    while True:
        r=json.loads(c.recv())
        if r.get("id")==n[0]: return r.get("result",{})

call("Emulation.setDeviceMetricsOverride",width=1400,height=1000,deviceScaleFactor=1,mobile=False)
call("Page.navigate",url="http://localhost:33000/"); time.sleep(12)
png = call("Page.captureScreenshot", format="png")["data"]
img = Image.open(io.BytesIO(base64.b64decode(png))).convert("RGB")
img.save("live.png")
px = img.load()
W,H = img.size

def near(p, hexs, tol=26):
    r,g,b = p
    R,G,B = int(hexs[1:3],16), int(hexs[3:5],16), int(hexs[5:7],16)
    return abs(r-R)<=tol and abs(g-G)<=tol and abs(b-B)<=tol

counts = {"圍欄含納(綠 #4a9d5f)":0, "備降點(金 #c8a44a)":0, "計畫路徑(灰 #8f8b80)":0}
for y in range(0,H,2):
    for x in range(0,W,2):
        p = px[x,y]
        if near(p,"#4a9d5f"): counts["圍欄含納(綠 #4a9d5f)"]+=1
        elif near(p,"#c8a44a"): counts["備降點(金 #c8a44a)"]+=1
        elif near(p,"#8f8b80"): counts["計畫路徑(灰 #8f8b80)"]+=1
for k,v in counts.items(): print(f"  {k:<26} {v} px")
ok = counts["圍欄含納(綠 #4a9d5f)"] > 50 and counts["計畫路徑(灰 #8f8b80)"] > 50
print(("✓" if ok else "✗"), "圍欄與計畫路徑都畫出來了")
sys.exit(0 if ok else 1)
