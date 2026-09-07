#!/usr/bin/env python3
"""起飛動作的方言驗證：**產品端真的吃驅動給的參數嗎？**

不需真機也不需 DB——攔掉 `job_command`／`job_set_mode`，直接看實際會送上線的
參數是什麼。

**為什麼要有這一支**（2026-09-07）：`driver.takeoff_plan()` 在三個驅動裡都
實作了、`test-driver-equivalence.py` 也逐項驗過它的回傳值，但**產品端沒有任何
呼叫者**——單機路徑自己從旗標重推一份（推對了），群飛路徑自己重推一份
（推成 PX4 語意：param7 用絕對海拔、空白參數用 NaN），對 ArduPilot 三條方言
全錯。等價測試全綠，而錯的那條路徑從來不在它的視野裡。

這正是 issues/026 B4-d 記下的那一課：**等價測試證明不了兩邊吃的是同樣的輸入。**
所以這支測的不是驅動，是**產品端的入口有沒有向驅動要答案**。

跑法：PYTHONPATH=libs:apps/command python3 scripts/test-takeoff-dialect.py
"""
import sys

sys.path[:0] = ["libs", "apps/command"]

import app.mav as mav                                          # noqa: E402

_sent = []


def _fake_command(r, sysid, cmd, params, **k):
    _sent.append((sysid, cmd, params))
    return {"accepted": True, "result": "ACCEPTED"}


def _fake_set_mode(r, sysid, mode, **k):
    _sent.append((sysid, "MODE", mode))
    return {"accepted": True, "mode_engaged": True}


mav.job_command = _fake_command
mav.job_set_mode = _fake_set_mode


class StubRouter:
    """最小 router：起飛路徑只讀 `drones[sysid]`。"""

    def __init__(self, autopilot_raw, armed=False):
        self.drones = {1: {"autopilot": autopilot_raw, "armed": armed}}


GROUND_AMSL = 122.0        # 現場實測的場地海拔量級（uav_plan 那份 .plan 的 home）
ALT = 2.0                  # 低空航線的離地高度——差異在這裡最看得出來

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def run(autopilot_raw, label):
    _sent.clear()
    r = StubRouter(autopilot_raw)
    prep = mav.job_arm_prep(r, 1)
    t = mav.job_takeoff_cmd(r, 1, ALT, GROUND_AMSL)
    _, cmd, params = _sent[-1]
    check(cmd == mav.M.MAV_CMD_NAV_TAKEOFF, f"{label}：送的不是 NAV_TAKEOFF（{cmd}）")
    print(f"  {label:<10} needs_guided={prep['needed']!s:<5} param7={t['alt_param7']:<7}"
          f" semantics={t['alt_semantics']:<8} blank={params[3]!r}")
    return prep, t, params


print(f"── 產品端送出的 NAV_TAKEOFF（地面海拔 {GROUND_AMSL} m、目標離地 {ALT} m）──")
ap_prep, ap_t, ap_params = run(3, "ArduPilot")
px_prep, px_t, px_params = run(12, "PX4")

# ArduPilot Copter：param7 是**相對高度**（送絕對海拔會差一整個地面海拔）、
# 空白參數用 **0.0**（實測 2026-08-12：NaN 的 NAV_TAKEOFF 連 ACK 都不回，
# 指令被靜默丟棄）、arm 之前必須**先進 GUIDED**
check(ap_t["alt_param7"] == ALT,
      f"ArduPilot param7 應為 {ALT}（相對高度），得到 {ap_t['alt_param7']}")
check(ap_t["alt_semantics"] == "relative", "ArduPilot alt_semantics 應為 relative")
check(ap_params[3] == 0.0,
      f"ArduPilot 空白參數應為 0.0（NaN 會被靜默丟棄），得到 {ap_params[3]!r}")
check(ap_prep["needed"] is True, "ArduPilot arm 前應先進 GUIDED")

# PX4：param7 是**絕對海拔**、空白參數 NaN＝「用當前值」、不需要模式前置
check(px_t["alt_param7"] == GROUND_AMSL + ALT,
      f"PX4 param7 應為 {GROUND_AMSL + ALT}（AMSL），得到 {px_t['alt_param7']}")
check(px_t["alt_semantics"] == "amsl", "PX4 alt_semantics 應為 amsl")
check(px_params[3] != px_params[3], f"PX4 空白參數應為 NaN，得到 {px_params[3]!r}")
check(px_prep["needed"] is False, "PX4 不需要 GUIDED 前置")

# **兩家不得算出同一個 param7**——算得一樣就代表方言沒被套用
check(ap_t["alt_param7"] != px_t["alt_param7"],
      "兩家 param7 相同：方言沒有被套用（這正是群飛路徑原本的病）")

# PX4 缺地面海拔要**說得出來**，不是靜默退回相對高度飛出一個沒人算過的高度
try:
    mav.job_takeoff_cmd(StubRouter(12), 1, ALT, None)
    fails.append("PX4 缺地面海拔應拋 CommandError，卻靜默通過")
except mav.CommandError as e:
    print(f"  PX4 缺地面海拔 → CommandError：{e}")

# 單機的 job_takeoff 是那三個動作組出來的，語意必須與逐一呼叫一致
_sent.clear()
full = mav.job_takeoff(StubRouter(3), 1, ALT, None)   # ArduPilot 不需要地面海拔
check(full["alt_param7"] == ALT and full["alt_semantics"] == "relative",
      f"job_takeoff（單機）語意與 job_takeoff_cmd 不一致：{full}")
check([c for c in _sent if c[1] == "MODE"], "job_takeoff（ArduPilot）應先切 GUIDED")
check(any(c[1] == 400 for c in _sent), "job_takeoff 應送 arm（400）")

print("\n全部通過" if not fails else "\nFAIL:\n  " + "\n  ".join(fails))
sys.exit(1 if fails else 0)
