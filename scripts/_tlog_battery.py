#!/usr/bin/env python3
"""從地面站原始層 tlog 讀最新一筆 `BATTERY_STATUS`。**在 backend 容器裡跑。**

輸出一行：`<已耗mAh> <電流A> <飛控算的剩餘%>`，讀不到就印 `NONE`／`NOFILE`。

## 為什麼要有這支

電流與累積消耗**只存在於原始層**——`telemetry` 表只有 `battery_voltage`
與 `battery_pct`，沒有電流也沒有 `current_consumed`。而後者正是電流刻度
校正的一半，也是「待機能撐多久」最直接的依據。

014 的原始層逐框架落盤，所以那個數字其實一直都在，只是沒有人去讀。
**不必上機、不必停代理**：停代理的理由只是序列埠獨佔，而地面站這邊本來
就有同一份資料。

只掃檔尾 3 MB：要的是「現在」的值，整檔掃到晚上會變成分鐘級的等待。
"""
import os
import sys

from pymavlink.dialects.v20 import ardupilotmega as M

TAIL = 3_000_000

path = sys.argv[1] if len(sys.argv) > 1 else None
if not path or not os.path.exists(path):
    print("NOFILE")
    raise SystemExit

size = os.path.getsize(path)
with open(path, "rb") as fh:
    fh.seek(max(0, size - TAIL))
    data = fh.read()

# tlog＝8 byte BE 微秒時間戳＋一個框架。**逐 byte 餵**讓解析器自己對齊——
# 從檔案中間切進去時，開頭一定是半個框架
mav = M.MAVLink(None)
mav.robust_parsing = True
last = None
for b in data:
    try:
        msg = mav.parse_char(bytes([b]))
    except Exception:
        continue
    if msg is not None and msg.get_type() == "BATTERY_STATUS":
        last = msg

if last is None:
    print("NONE")
else:
    print(f"{last.current_consumed} {last.current_battery / 100.0:.2f} "
          f"{last.battery_remaining}")
