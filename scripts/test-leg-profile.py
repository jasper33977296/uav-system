#!/usr/bin/env python3
"""逐段剖面（issues/048，doc/route-planning-first-principles.md）。

**這支測試釘住的是四件事，每一件都對應一次真實的誤解：**

1. **有效速度不是航線裡寫的那個。** `DO_CHANGE_SPEED` 只從被執行到的那一項
   之後才生效——2026-09-07 使用者以為全程 0.3 m/s，起飛到第一個航點卻是
   機上的 `WP_SPD`（8 m/s）。**「來源」欄位存在的理由就是這個。**
2. **讀不到機上的 `WP_SPD` 不等於安全。** 沒讀過就是沒檢查，不是通過。
3. **低空帶速要擋。** 1.5 m 不離譜、8 m/s 不離譜，合起來才致命。
4. **樣本數 1 的估計要標樣本數 1。** 過衝那條來自單一次實測。

跑法（不需要服務、不需要網路、不需要飛機）：
    python3 scripts/test-leg-profile.py
"""
import math
import os
import struct
import sys
import tempfile

sys.path.insert(0, "/home/k200/uav-system/libs")

import plan_check as P  # noqa: E402
import terrain as T  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


# ── 一塊平地 100 m 的合成圖磚（含一條 +8 m 的土堤，第 1795～1797 列）──
N, TMP = 3601, tempfile.mkdtemp(prefix="dem-")
buf = bytearray(N * N * 2)
for r in range(N):
    v = 108 if 1795 <= r <= 1797 else 100
    for c in range(N):
        struct.pack_into(">h", buf, (r * N + c) * 2, v)
with open(os.path.join(TMP, "N00E000.hgt"), "wb") as f:
    f.write(buf)
DEM = T.Dem(TMP)
HOME = {"lat": 0.5, "lon": 0.5}
M_LAT = 1 / 110574.0          # 一公尺的緯度


M_LON = M_LAT / math.cos(math.radians(0.5))


def wp(seq, east_m, alt, cmd=16, frame=3):
    """**往東走**：合成圖磚的土堤是一條緯度帶，往北一定會穿過它，
    那樣「平地」的案例就不是平地了（第一版就踩到）。"""
    return {"seq": seq, "lat": 0.5, "lon": 0.5 + east_m * M_LON,
            "alt": alt, "command": cmd, "frame": frame}


def north(seq, dn_m, alt, cmd=16, frame=3):
    """往北走——專門用來穿過土堤。"""
    return {"seq": seq, "lat": 0.5 + dn_m * M_LAT, "lon": 0.5,
            "alt": alt, "command": cmd, "frame": frame}


def spd(seq, v):
    return {"seq": seq, "lat": 0, "lon": 0, "alt": 0, "command": 178,
            "frame": 2, "p2": v}


print("── 1. 有效速度：DO_CHANGE_SPEED 只從它之後生效 ──────────────")
plan = [wp(0, 0, 5, cmd=22), wp(1, 60, 5), spd(2, 0.3), wp(3, 120, 5),
        wp(4, 180, 5)]
r = P.leg_profile(plan, HOME, dem=DEM, wp_spd=8.0)
legs = r["legs"]
chk("三段", len(legs) == 3, len(legs))
chk("**第一段用機上的 8 m/s，不是航線寫的 0.3**",
    legs[0]["speed_ms"] == 8.0 and legs[0]["speed_src"] == "機上 WP_SPD",
    (legs[0]["speed_ms"], legs[0]["speed_src"]))
chk("改速度之後的段落才是 0.3，而且說得出來源",
    legs[1]["speed_ms"] == 0.3 and legs[1]["speed_src"] == "航線 seq 2",
    (legs[1]["speed_ms"], legs[1]["speed_src"]))
chk("再往後沿用 0.3", legs[2]["speed_ms"] == 0.3)

print("\n── 2. 讀不到 WP_SPD ＝ 沒檢查，不是通過 ────────────────────")
r = P.leg_profile(plan, HOME, dem=DEM, wp_spd=None)
chk("沒有 problem（不能憑空擋）", not r["problems"], r["problems"])
chk("**但明講「速度沒有檢查」**",
    any("速度沒有檢查" in w for w in r["warnings"]), r["warnings"])
chk("**改速度之前**的段落標成 unknown（之後的仍然是航線給的）",
    r["legs"][0]["speed_src"] == "unknown"
    and r["legs"][1]["speed_src"] == "航線 seq 2",
    [l["speed_src"] for l in r["legs"]])

print("\n── 3. 低空帶速要擋；低空慢飛不擋 ──────────────────────────")
low_fast = [wp(0, 0, 1.5, cmd=22), wp(1, 60, 1.5), wp(2, 120, 1.5)]
r = P.leg_profile(low_fast, HOME, dem=DEM, wp_spd=8.0)
chk("**1.5 m 配 8 m/s → problem**", len(r["problems"]) == 1, r["problems"])
chk("訊息同時給出兩條解法（抬高度／降速度）",
    "抬到" in r["problems"][0] and "速度降到" in r["problems"][0])
chk("而且說得出速度是哪來的", "機上 WP_SPD" in r["problems"][0])

slow = [wp(0, 0, 1.0, cmd=22), spd(1, 0.3), wp(2, 60, 1.0), wp(3, 120, 1.0)]
r = P.leg_profile(slow, HOME, dem=DEM, wp_spd=8.0)
chk("**1 m 高、0.3 m/s 的貼地任務照樣過**（第一段之後）",
    not any("離地" in p for p in r["problems"][1:]), r["problems"])

high = [wp(0, 0, 10, cmd=22), wp(1, 60, 10), wp(2, 120, 10)]
r = P.leg_profile(high, HOME, dem=DEM, wp_spd=8.0)
chk("10 m 配 8 m/s 不擋", not r["problems"], r["problems"])

print("\n── 4. 離地是「段內最低」，不是端點 ────────────────────────")
# 土堤在起飛點北方約 90～155 m，兩端各有 5 m 餘裕、中間只有 −3
over = [north(0, 0, 5, cmd=22), north(1, 300, 5)]
r = P.leg_profile(over, HOME, dem=DEM, wp_spd=0.3)
chk("段內最低離地是 −3.0（不是端點的 5）",
    r["legs"][0]["agl_m"] == -3.0, r["legs"][0]["agl_m"])

print("\n── 5. 航段比到達半徑還短 ────────────────────────────────")
tiny = [wp(0, 0, 10, cmd=22), wp(1, 60, 10), wp(2, 61, 10)]
r = P.leg_profile(tiny, HOME, dem=DEM, wp_spd=2.0, wp_radius=2.0)
chk("**指出飛機不會真的飛到那個點**",
    any("比到達半徑" in p for p in r["problems"]), r["problems"])

print("\n── 6. 轉角＋速度的過衝估計要標明樣本數 ─────────────────────")
sharp = [wp(0, 0, 10, cmd=22), wp(1, 30, 10),
         {"seq": 2, "lat": 0.5 + 30 * M_LAT, "lon": 0.5 + 30 * M_LON,
          "alt": 10, "command": 16, "frame": 3},
         {"seq": 3, "lat": 0.5 + 30 * M_LAT, "lon": 0.5,
          "alt": 10, "command": 16, "frame": 3}]
r = P.leg_profile(sharp, HOME, dem=DEM, wp_spd=8.0, wp_radius=2.0)
w = [x for x in r["warnings"] if "衝過頭" in x]
chk("有過衝警告", bool(w), r["warnings"])
chk("**而且說出那是單一次實測**", w and "單一次實測" in w[0], w[:1])
chk("轉角有算出來", any(l.get("turn_deg", 0) >= 80 for l in r["legs"]),
    [l.get("turn_deg") for l in r["legs"]])

print("\n── 7. frame 10 的段落不在這裡判離地（交給飛控）─────────────")
t10 = [wp(0, 0, 5, cmd=22, frame=3), wp(1, 60, 5, frame=10), wp(2, 120, 5, frame=10)]
r = P.leg_profile(t10, HOME, dem=DEM, wp_spd=8.0)
chk("frame 10 的段落 agl 是 None（不是 0）",
    r["legs"][-1]["agl_m"] is None, r["legs"][-1]["agl_m"])
chk("因此也不會被低空帶速擋", not r["problems"], r["problems"])

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
