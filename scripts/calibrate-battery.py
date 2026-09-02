#!/usr/bin/env python3
"""電池校正：把「畫面上那個百分比」變成一個可信的數字。

## 為什麼百分比會是假的

`battery_remaining` **完全來自電流積分**，與電壓無關：

    battery_remaining = (BATT_CAPACITY − 累積消耗 mAh) / BATT_CAPACITY

本機實測驗證過這條：`(3300 − 1381) / 3300 = 58.2%`，而飛控回報 58%。
**所以電流感測器不準＝百分比不準**，而畫面上最顯眼的就是它。

### ⚠ 校正一定要用**真的電池**，不能在電源供應器上做

2026-09-02 我在這台機上量到 6.44 小時內累積消耗 0 → 1404 mAh、電壓
16.200 → 12.383 V，並據此推論「電流刻度少算了一半」。**那個推論是錯的**
——使用者指出當時機上接的是電源供應器，不是電池。

接電供時：電壓是人調出來的、不是放電曲線，所以「掉了多少電壓」推不出
「放掉多少 mAh」；而那正是那個推論的整個基礎。

**留下來的教訓寫在這裡，因為這支工具最容易被這樣誤用**：
`--tlog` 與 `--check` 都只是把數字讀出來，**它們分不出電池與電供**。
要做校正，先確認機上接的是要飛的那顆電池。

## 要校的是兩件不同的事

1. **電壓刻度**（`BATT_VOLT_MULT`）——低電量 failsafe 的門檻靠它。
   **先校這個**：`BATT_LOW_VOLT`／`BATT_CRT_VOLT` 已經按 4S 設好了，
   但那組數字假設飛控回報的電壓是準的，而那件事沒有人驗過。
2. **電流刻度與零點**（`BATT_AMP_PERVLT`／`BATT_AMP_OFFSET`）——百分比與
   mAh 門檻靠它。

用法（在機上跑，代理要先停）：

    python3 calibrate-battery.py --check                    # 現況＋靜置漂移
    python3 calibrate-battery.py --volts 12.41              # 用電表量到的實際電壓
    python3 calibrate-battery.py --charged-mah 2870 --reported-mah 1420
    加 --apply 才會真的寫入（一律讀回核對）

**沒有 `--apply` 就只是算給你看。** 而且**只算，不猜**：沒給量測值的那一項
不會被動到。
"""
import argparse
import sys
import time

from pymavlink import mavutil

ap = argparse.ArgumentParser()
ap.add_argument("--dev", default="/dev/ttyAMA0")
ap.add_argument("--baud", type=int, default=57600)
ap.add_argument("--check", action="store_true",
                help="只看現況：電壓／電流／累積消耗，以及靜置時的漂移")
ap.add_argument("--check-seconds", type=float, default=1800.0,
                help="--check 取樣多久。**累積消耗是整數 mAh**，窗太短會被量化誤差蓋過——靜置電流 0.2 A 等級時要 30 分鐘以上")
ap.add_argument("--volts", type=float,
                help="用電表量到的實際電池電壓（校 BATT_VOLT_MULT）")
ap.add_argument("--charged-mah", type=float,
                help="充電器充回去多少 mAh（校 BATT_AMP_PERVLT）")
ap.add_argument("--reported-mah", type=float,
                help="同一段期間飛控說消耗了多少 mAh")
ap.add_argument("--tlog", help="改從一份 tlog 算靜置漂移（**不必停代理**，"
                                "地面站錄的那份就有 BATTERY_STATUS）")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

if a.tlog:
    # **零停機的做法。** 序列埠只有一個，而代理握著它——為了看一小時的漂移
    # 停代理一小時是不划算的。飛控本來就把 BATTERY_STATUS 送給地面站，
    # 地面站本來就整條錄下來（issues/014 原始層），所以答案早就在檔案裡。
    src = mavutil.mavlink_connection(a.tlog)
    pts = []
    while True:
        msg = src.recv_match(type="BATTERY_STATUS", blocking=False)
        if msg is None:
            break
        t = getattr(msg, "_timestamp", None)
        if t and msg.current_consumed is not None and msg.current_consumed >= 0:
            pts.append((t, msg.current_consumed, msg.current_battery / 100.0,
                        msg.voltages[0] / 1000.0))
    if len(pts) < 2:
        sys.exit("✗ 這份 tlog 裡沒有足夠的 BATTERY_STATUS")
    (t0, c0, _, v0), (t1, c1, _, v1) = pts[0], pts[-1]
    hours = (t1 - t0) / 3600.0
    cur = [x[2] for x in pts]
    print(f"── {a.tlog}：{len(pts)} 筆、跨 {hours:.2f} 小時 ──")
    print(f"  累積消耗 {c0} → {c1} mAh（+{c1 - c0}）→ 等效 "
          f"{(c1 - c0) / hours / 1000:.3f} A")
    print(f"  回報電流 平均 {sum(cur) / len(cur):.3f} A"
          f"（{min(cur):.2f} … {max(cur):.2f}）")
    print(f"  電壓 {v0:.3f} → {v1:.3f} V"
          f"（4S 每 cell {v0 / 4:.2f} → {v1 / 4:.2f}）")
    print("\n  **對照這兩件事**：電壓掉了多少（＝實際放掉多少電），"
          "與飛控數了多少 mAh。\n  差得多，就是電流刻度不對——"
          "而百分比完全建立在那個刻度上。")
    sys.exit(0)

m = mavutil.mavlink_connection(a.dev, baud=a.baud)
if m.wait_heartbeat(timeout=15) is None:
    sys.exit("✗ 沒有心跳——代理還開著嗎？（它握著序列埠）")
tgt = (m.target_system, m.target_component)
print(f"✓ 飛控 sysid={tgt[0]}")

NAMES = ["BATT_VOLT_MULT", "BATT_AMP_PERVLT", "BATT_AMP_OFFSET",
         "BATT_CAPACITY", "BATT_LOW_VOLT", "BATT_CRT_VOLT", "BATT_MONITOR"]


def read_params(names, timeout=20.0):
    got, want, deadline = {}, set(names), time.time() + timeout
    for n in names:
        m.mav.param_request_read_send(tgt[0], tgt[1], n.encode(), -1)
        time.sleep(0.05)
    while want and time.time() < deadline:
        msg = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=1)
        if msg is None:
            continue
        nm = msg.param_id if isinstance(msg.param_id, str) else msg.param_id.decode()
        nm = nm.rstrip("\x00")
        if nm in want:
            got[nm] = float(msg.param_value)
            want.discard(nm)
    return got


def read_batt(timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        msg = m.recv_match(type="BATTERY_STATUS", blocking=True, timeout=1)
        if msg is not None:
            return (msg.voltages[0] / 1000.0, msg.current_battery / 100.0,
                    msg.current_consumed, msg.battery_remaining)
    return None


p = read_params(NAMES)
b = read_batt()
if b is None:
    sys.exit("✗ 收不到 BATTERY_STATUS——BATT_MONITOR 設了嗎？")
volts, amps, consumed, remaining = b

print("\n── 現況 ─────────────────────────────────────────────")
print(f"  電壓          {volts:.3f} V"
      f"（{volts / 4:.2f} V/cell，若為 4S）")
print(f"  電流          {amps:.2f} A")
print(f"  累積消耗      {consumed} mAh")
print(f"  回報剩餘      {remaining}%")
cap = p.get("BATT_CAPACITY")
if cap and consumed is not None and consumed >= 0:
    calc = (cap - consumed) / cap * 100
    print(f"  ↑ 對照：(BATT_CAPACITY {cap:.0f} − {consumed}) / {cap:.0f}"
          f" = {calc:.1f}%　**百分比就是這樣來的，與電壓無關**")
for n in NAMES:
    print(f"  {n:16} = {p.get(n, '讀不到')}")

if a.check:
    print(f"\n── 靜置漂移（取樣 {a.check_seconds:.0f} 秒）────────────────────")
    print("**馬達不轉時累積消耗還在長，就是零點偏移**——它會一直吃掉你的"
          "百分比，而電池其實沒有在放電。")
    t0, c0 = time.time(), consumed
    samples = []
    while time.time() - t0 < a.check_seconds:
        r = read_batt(timeout=3)
        if r:
            samples.append(r[1])
        time.sleep(1)
    r = read_batt()
    dt = time.time() - t0
    if r and c0 is not None and c0 >= 0:
        dc = r[2] - c0
        # mAh / 小時 = mA。**除以 1000 才是安培**——第一版少了這一步，
        # 90 秒 +2 mAh 被印成「等效 79.66 A」，一個顯然不可能的數字
        implied = dc / (dt / 3600.0) / 1000.0
        avg = sum(samples) / len(samples) if samples else float("nan")
        # **累積消耗是整數 mAh**，短時間取樣會被量化誤差蓋過：90 秒內
        # ±1 mAh 就是 ±0.04 A，而靜置電流本身也才 0.2–0.3 A
        quant = 1.0 / (dt / 3600.0) / 1000.0
        print(f"  {dt:.0f} 秒內累積消耗 +{dc} mAh → 等效 {implied:.3f} A"
              f"（±{quant:.3f} A 是整數計數的量化誤差）")
        print(f"  同期回報電流平均 {avg:.3f} A")
        if quant > abs(implied) * 0.2:
            print(f"  ⚠ **這個窗太短，量化誤差比訊號還大**——把 "
                  f"--check-seconds 拉到 1800 以上再看，"
                  f"或直接從地面站的 tlog 取兩個相隔久一點的點（不必停代理）")
        print("\n  接下來用電表量同一時間的實際電流：")
        print("    · 量到接近 0 → 這 %.2f A 是**零點偏移**，" % avg)
        print("      BATT_AMP_OFFSET 要調（見下方公式），不然它會一直偷你的電量")
        print("    · 量到差不多 → 那是真的耗電（FC＋接收機＋Pi 都吃這顆電池），")
        print("      零點沒問題，但**刻度**仍然要用充電器回充法校")
    sys.exit(0)

todo = {}
print("\n── 計算 ─────────────────────────────────────────────")
if a.volts is not None:
    old = p.get("BATT_VOLT_MULT")
    if old is None:
        sys.exit("✗ 讀不到 BATT_VOLT_MULT")
    new = old * (a.volts / volts)
    print(f"  電壓刻度：電表 {a.volts:.3f} V ÷ 飛控 {volts:.3f} V"
          f" = ×{a.volts / volts:.4f}")
    print(f"    BATT_VOLT_MULT {old:.4f} → {new:.4f}")
    print(f"    **低電量門檻靠這個**：偏差 1% 在 4S 上就是 0.14 V ≈ 0.035 V/cell")
    todo["BATT_VOLT_MULT"] = new

if (a.charged_mah is None) != (a.reported_mah is None):
    sys.exit("✗ --charged-mah 與 --reported-mah 要一起給（兩個數字才構成一個比例）")
if a.charged_mah is not None:
    old = p.get("BATT_AMP_PERVLT")
    if old is None:
        sys.exit("✗ 讀不到 BATT_AMP_PERVLT")
    if a.reported_mah <= 0:
        sys.exit("✗ 飛控回報的 mAh 要 > 0")
    ratio = a.charged_mah / a.reported_mah
    new = old * ratio
    print(f"  電流刻度：充回 {a.charged_mah:.0f} mAh ÷ 飛控說的"
          f" {a.reported_mah:.0f} mAh = ×{ratio:.4f}")
    print(f"    BATT_AMP_PERVLT {old:.4f} → {new:.4f}")
    if ratio > 1.5 or ratio < 0.67:
        print(f"    ⚠ 比例 {ratio:.2f} 偏離 1 很多。**先確認不是別的原因**："
              "電池一開始沒充飽、中途換過電池、或 consumed 在期間被歸零過")
    print("    註：充電器的 mAh 含充電效率（約多 5%），所以這個校正會略微保守"
          "——**往「以為還有電比實際少」的方向偏，那是安全的那一側**")
    todo["BATT_AMP_PERVLT"] = new

if not todo:
    print("  （沒有給量測值，只印了現況）")
    print("\n零點偏移的公式：飛控讀的電流 = (腳位電壓 − BATT_AMP_OFFSET)"
          " × BATT_AMP_PERVLT")
    print("  所以無負載時若讀到 I0 安培，BATT_AMP_OFFSET 要加"
          " I0 / BATT_AMP_PERVLT 伏特。")
    print("  **先跑 --check 確認 I0 是偏移不是真的耗電**，再動它。")
    sys.exit(0)

if not a.apply:
    print(f"\n**乾跑**：{len(todo)} 個要改。真的要寫請加 --apply")
    sys.exit(0)

print(f"\n── 寫入 ─────────────────────────────────────────────")
for n, v in todo.items():
    m.mav.param_set_send(tgt[0], tgt[1], n.encode(), float(v),
                         mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
    time.sleep(0.2)
time.sleep(1.0)
back = read_params(list(todo))
ok = True
for n, v in todo.items():
    got = back.get(n)
    good = got is not None and abs(got - v) <= max(1e-6, abs(v) * 1e-5)
    ok &= good
    print(f"{'✓' if good else '✗'} {n:16} 讀回 {got}（要 {v:.4f}）")
if ok and "BATT_VOLT_MULT" in todo:
    print("\n**電壓刻度改了，回頭確認 BATT_LOW_VOLT／BATT_CRT_VOLT 還對不對**"
          "——它們是按舊刻度下的電壓訂的。")
sys.exit(0 if ok else 1)
