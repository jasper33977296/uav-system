#!/usr/bin/env python3
"""每個自駕儀驅動都要宣告完整的方言旗標。

**為什麼需要這支**：`libs/autopilot` 的驅動是 duck typing（Protocol），
不是繼承——所以少宣告一個旗標**編譯期不會有任何抱怨**，要等到執行期某條
路徑真的去讀它才炸。2026-08-26 就發生過：`no_coord_frames` 加在
UnknownDriver 與 ArduPilotDriver 上、漏了 Px4Driver，於是任務預檢對 PX4 的
航線直接 AttributeError。

「加一個新方言旗標」是這個專案會反覆做的事（026 統計過：一天之內從 3 處
差異長到 10 處），所以擋住這個失效模式比修一次值錢。

用法：python3 scripts/test-driver-flags.py
"""
import sys

sys.path.insert(0, "/home/k200/uav-system/libs")

import autopilot  # noqa: E402

#: 每個驅動都必須有的方言旗標，以及型別
FLAGS = {
    "home_at_seq0": bool,
    "takeoff_alt_is_relative": bool,
    "takeoff_needs_guided": bool,
    "no_coord_frames": frozenset,
}
#: MAV_AUTOPILOT 值 → 名字。0 走 UnknownDriver
KNOWN = {0: "unknown", 3: "ardupilot", 12: "px4"}

ok = True
for raw, name in KNOWN.items():
    drv = autopilot.get_driver(raw)
    for flag, typ in FLAGS.items():
        has = hasattr(drv, flag)
        good = has and isinstance(getattr(drv, flag), typ)
        ok &= good
        val = getattr(drv, flag, "**沒有宣告**")
        print(f"{'✓' if good else '✗'} {name:<10} {flag:<24} = {val}")

print("\n── 方言旗標不能兩家一模一樣（那就不叫方言了）──────────")
ap, px = autopilot.get_driver(3), autopilot.get_driver(12)
diff = [f for f in FLAGS if getattr(ap, f, None) != getattr(px, f, None)]
print(f"{'✓' if diff else '✗'} ArduPilot 與 PX4 有 {len(diff)} 項不同：{diff}")
ok &= bool(diff)

print("\n── no_coord_frames 的內容要對得上實測 ─────────────────")
# PX4：2026-08-12 SITL 實測，只有 frame 2 過
# ArduPilot：2026-08-26 實機證據，從機上下載回來的 RTL 就是 frame 0
cases = [("px4 只收 2", autopilot.get_driver(12).no_coord_frames == {2}),
         ("ardupilot 收 0 與 2", autopilot.get_driver(3).no_coord_frames == {0, 2})]
for label, cond in cases:
    ok &= cond
    print(f"{'✓' if cond else '✗'} {label}")

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
