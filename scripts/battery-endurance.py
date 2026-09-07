#!/usr/bin/env python3
"""待機放電：整顆電池撐多久，以及順便把電流刻度校出來（2026-09-07）。

## 這支只讀，不寫

從 `telemetry` 讀電壓與飛控回報的電量，畫出放電曲線並估剩餘時間。
**不碰飛控、不改參數**——校正是 `calibrate-battery.py` 的事。

## 為什麼「待機放電」值得做

`calibrate-battery.py` 的說明寫著：累積消耗是**整數 mAh**，靜置電流 0.2 A
等級時窗太短會被量化誤差蓋過，要 30 分鐘以上。一場數小時的待機放電正好是
那個窗——而且它同時回答「Pi 撐得住多久」這個實務問題。

## 外推要小心

鋰聚合物的放電曲線**不是直線**：中段平坦、末段急墜（拐點）。所以線性外推
在平坦段會**高估**剩餘時間。本工具照實給兩個數字：最近一段的實際斜率，以及
據此的外推——**外推值只在拐點之前有意義**，過了 3.6 V/格就別信它。

用法：
    python3 scripts/battery-endurance.py                 # 從最後一次上電算起
    python3 scripts/battery-endurance.py --cutoff 14.0   # 換一個判準電壓
    python3 scripts/battery-endurance.py --sysid 1
"""
import argparse
import subprocess
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--sysid", type=int, default=1)
ap.add_argument("--cutoff", type=float, default=14.0,
                help="算到哪個電壓為止（預設 14.0＝BATT_LOW_VOLT＝3.5V/格）")
ap.add_argument("--cells", type=int, default=4)
ap.add_argument("--window-min", type=float, default=30.0,
                help="用最近幾分鐘的斜率做外推")
a = ap.parse_args()


def sql(q):
    r = subprocess.run(["docker", "exec", "uav-db", "psql", "-U", "uav",
                        "-d", "uav", "-tAF,", "-c", q],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"查詢失敗：{r.stderr.strip()[:300]}")
    return [l.split(",") for l in r.stdout.strip().splitlines() if l.strip()]


did = sql(f"select id::text from drones where mav_sysid={a.sysid}")
if not did:
    sys.exit(f"找不到 mav_sysid={a.sysid} 的機")
did = did[0][0]

# **從最後一次上電算起**：電壓突升代表換電池／重新上電，之前的不算同一段
rows = sql(f"""
    with t as (select time, battery_voltage v, battery_pct p,
                      lag(battery_voltage) over (order by time) pv
                 from telemetry
                where drone_id='{did}' and battery_voltage is not null
                  and time > now() - interval '48 hours'),
         b as (select max(time) t0 from t where v - coalesce(pv, v) > 1.0)
    select extract(epoch from time)::bigint, v, p from t
     where time >= coalesce((select t0 from b), (select min(time) from t))
     order by time""")
if len(rows) < 2:
    sys.exit("資料太少（這一段還沒累積足夠的樣本）")

t0, v0 = float(rows[0][0]), float(rows[0][1])
tn, vn = float(rows[-1][0]), float(rows[-1][1])
pn = rows[-1][2]
el = tn - t0
print(f"本段起算：{el / 3600:.2f} 小時前（{len(rows)} 筆）")
print(f"  起始 {v0:.3f} V（{v0 / a.cells:.3f} V/格）")
print(f"  現在 {vn:.3f} V（{vn / a.cells:.3f} V/格）｜飛控回報電量 {pn}%")
print(f"  已降 {v0 - vn:.3f} V")

win = [r for r in rows if float(r[0]) >= tn - a.window_min * 60]
if len(win) >= 2:
    wt = float(win[-1][0]) - float(win[0][0])
    wv = float(win[0][1]) - float(win[-1][1])
    if wt > 0:
        rate = wv / (wt / 3600)          # V/小時
        print(f"\n最近 {wt / 60:.0f} 分鐘：掉 {wv:.3f} V → {rate:.4f} V/小時")
        if rate > 0.0005:
            left = (vn - a.cutoff) / rate
            print(f"  外推到 {a.cutoff:.1f} V（{a.cutoff / a.cells:.2f} V/格）："
                  f"還有 **{left:.1f} 小時**，總計約 {el / 3600 + left:.1f} 小時")
            if vn / a.cells > 3.6:
                print("  ⚠ 現在還在平坦段——**這個外推會高估**，過了 3.6 V/格再看才準")
        else:
            print("  斜率還量不出來（電壓幾乎沒動）——再等一段時間")

print(f"\n放電曲線（每 30 分鐘取一點）")
step = max(1, len(rows) // 12)
for r in rows[::step]:
    t, v = (float(r[0]) - t0) / 3600, float(r[1])
    bar = "█" * int(max(0, (v - a.cutoff) / max(v0 - a.cutoff, 0.01)) * 30)
    print(f"  {t:5.2f}h  {v:6.3f} V  {v / a.cells:.3f}/格  {bar}")

print(f"""
── 結束前一定要做的事 ────────────────────────────────────
**拔電之前先讀飛控的累積消耗 mAh**：那是 RAM 裡的累加器，斷電就歸零，
而它正是電流刻度校正的一半。在機上跑：

    sudo systemctl stop uav-agent
    ./venv/bin/python calibrate-battery.py --check --check-seconds 60
    sudo systemctl start uav-agent

充回去之後，用充電器顯示的 mAh 與上面讀到的數字做校正：

    ./venv/bin/python calibrate-battery.py --charged-mah <充電器> --reported-mah <飛控>
""")
