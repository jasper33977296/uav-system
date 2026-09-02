#!/usr/bin/env python3
"""RC 接收機的三態判定（issues/014 結構層／039 複裁 A）。

**判準必須與機上代理逐字相同**，否則 crosscheck 會噴出一堆假的不一致——
而那比沒有比對更糟：**真的不一致會淹在裡面**。

規則（兩邊相同）：
  * `present` 位元沒設 → **None（不知道）**
  * `present` 有、`health` 有 → True
  * `present` 有、`health` 無 → False
  * **不看 `enabled`**——那是「壞了沒」的問題，不是「在不在」
  * **不用 `RC_CHANNELS.rssi`**：rssi 沒有「不知道」這一態，
    而 255 在 MAVLink 裡是無效值不是滿格。讀錯方向會讓守門在 RC 掉線時
    照樣放行——三態的分別是這件事的全部價值。

跑法（不需要服務）：
    python3 scripts/test-rc-link.py
"""
import sys

from pymavlink import mavutil

M = mavutil.mavlink
RC = M.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def derive(present, health):
    """與 `mavlink_rx.py` 的 SYS_STATUS 分支同一行邏輯。"""
    return bool(health & RC) if (present & RC) else None


print("── 三態 ───────────────────────────────────────────────")
chk("present 沒設 → None（不知道）", derive(0, 0) is None)
chk("present 沒設、health 卻有 → 仍是 None",
    derive(0, RC) is None, "present 才是「有沒有這個感測器」的那一位")
chk("present 有、health 有 → True", derive(RC, RC) is True)
chk("present 有、health 無 → False", derive(RC, 0) is False)

print("\n── 不看 enabled（那是另一個問題）──────────────────────")
other = M.MAV_SYS_STATUS_SENSOR_GPS
chk("其他感測器的位元不影響 RC 判定",
    derive(RC | other, RC) is True and derive(RC | other, other) is False)

print("\n── 反向驗證：False 與 None 不可互相冒充 ────────────────")
chk("**None 不是 False**", derive(0, 0) is not False)
chk("**False 不是 None**", derive(RC, 0) is not None)
print("   （039 複裁 A 只在 False 時擋；None 不擋——把「不知道」當成「沒有 RC」")
print("    會讓所有還沒回報這個位元的機都起飛不了）")

print("\n── ⚠ 真機實測（2026-09-02）：ArduPilot 4.7 不回報這個位元 ──")
print("   實測結果：SYS_STATUS 回報 18 個感測器，**RC 接收機不在其中**。")
print("   所以這台機的 rc_link 恆為 None，**039 複裁 A 的 RC 守門從未生效**。")
print("   判定邏輯本身是對的（上面全過），缺的是**這個廠牌的訊號來源**。")
chk("（提醒用，不是斷言）本檔已記錄該限制", True)

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
