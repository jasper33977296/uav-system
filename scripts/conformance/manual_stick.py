#!/usr/bin/env python3
"""一致性測試：搖桿真的能操縱（issue 026 B3）。與 `manual_failsafe` 共同覆蓋 `manual`。

**`MANUAL_CONTROL` 沒有 ACK**，所以「送出成功」不構成任何證據——ArduPilot 對
來源不符的搖桿指令是**靜默丟棄**（無錯誤、無回應）。唯一的證據是**機體真的照
指令位移**。

## 對照組是這條測試的核心

只證明「送了之後有動」不夠：機體可能本來就在飄。所以量三段，**全部在空中**：

1. **指令段**：持續送前進 → 應有明顯位移
2. **煞停段**：回中位 → 機體從速度中煞停，位移會遞減（這段不是對照，是過渡）
3. **靜止對照段**：確認已靜止後，再送一段中位 → 位移應接近 0

第 3 段是真正的對照。**2026-08-12 首次驗證時這一段是在機體已落地時量到的**，
所以它當時不構成有效對照——這次刻意在空中且確認靜止後才量。

判準：指令段位移 > 5m **且** > 靜止對照段的 3 倍。

## 安全性與復原

飛完一定降落並等到上鎖（`finally`）。deadman 要求 0.4s 內要有新設定點，所以
送設定點的迴圈不能有空窗（有空窗會觸發失聯降級，那是 `manual_failsafe` 在驗的
另一條路徑）。

跑法：
    python3 scripts/conformance/manual_stick.py [px4|ardupilot]
"""
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _harness import (Skip, _driver, assert_dialect, fleet,  # noqa: E402
                      pick, post, run)  # noqa: E402

TARGET_ALT = 15.0
STICK = 0.6
PHASE_S = 6.0
SETPOINT_HZ = 10.0
MIN_COMMANDED_M = 5.0
RATIO = 3.0


def _st(sysid):
    return fleet().get(str(sysid), {})


def _pos(sysid):
    d = _st(sysid)
    return (d.get("lat"), d.get("lon"))


def _dist(a, b):
    if None in a or None in b:
        return None
    dy = (b[0] - a[0]) * 111320.0
    dx = (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def _stream(sysid, x, secs):
    """持續送設定點並回傳位移。**不留空窗**（deadman 0.4s）。"""
    base = _pos(sysid)
    n = int(secs * SETPOINT_HZ)
    for _ in range(n):
        post(f"/api/command/{sysid}/manual",
             {"x": x, "y": 0.0, "z": 0.0, "r": 0.0}, timeout=5)
        time.sleep(1.0 / SETPOINT_HZ)
    time.sleep(0.8)
    return _dist(base, _pos(sysid))


def _settle(sysid, max_s=12.0):
    """送中位直到機體真的靜止（連續兩秒位移 < 0.3m）才回傳。

    **這一步是第 3 段對照能成立的前提**：沒有先確認靜止，量到的會是煞停滑行。
    """
    end = time.time() + max_s
    prev = _pos(sysid)
    still = 0
    while time.time() < end:
        for _ in range(int(SETPOINT_HZ)):
            post(f"/api/command/{sysid}/manual", {"x": 0.0, "y": 0.0, "z": 0.0, "r": 0.0},
                 timeout=5)
            time.sleep(1.0 / SETPOINT_HZ)
        d = _dist(prev, _pos(sysid))
        prev = _pos(sysid)
        still = still + 1 if (d is not None and d < 0.3) else 0
        if still >= 2:
            return True
    return False


def _recover(sysid):
    post(f"/api/command/{sysid}/manual/stop")
    post(f"/api/command/{sysid}/mode/rtl")
    end = time.time() + 120.0
    while time.time() < end:
        if _st(sysid).get("armed") is False:
            return True
        time.sleep(2.0)
    return False


def check(autopilot: str) -> str:
    sysid, info = pick(autopilot)
    if info.get("capabilities", {}).get("manual") != "ok":
        raise Skip(f"sysid {sysid} 的 manual 能力未開："
                   f"{info.get('capability_reasons', {}).get('manual', '(無原因)')}")

    ok, r = post(f"/api/command/{sysid}/takeoff", {"alt": TARGET_ALT}, timeout=120)
    assert_dialect(ok, r, "起飛（搖桿測試的前置）")
    try:
        end = time.time() + 60
        while time.time() < end and (_st(sysid).get("alt_rel") or 0) < TARGET_ALT * 0.8:
            time.sleep(1.0)
        alt = _st(sysid).get("alt_rel")
        assert alt and alt >= TARGET_ALT * 0.8, f"未達測試高度（{alt} m）"

        # **PX4 要有「活著的手動控制來源」才肯進 POSCTL**（實測：回
        # "Switching to POSCTL is currently not available"）。所以不能送一次
        # manual/start 就乾等——中間空窗超過 2s，deadman 會停掉 MANUAL_CONTROL
        # 串流，PX4 看到的就是「沒有手動來源」而拒絕。第一版的重試迴圈每次間隔
        # 5s，等於每次重試都自己把前提破壞掉。
        #
        # 正確做法是**邊送設定點邊等模式真的切過去**——這也才是真人操作的樣子
        # （前端是持續握著搖桿的），並且照慣例**以讀回的模式為準，不看 ACK**。
        drv = _driver(_st(sysid).get("autopilot_raw"))
        ok, r2 = post(f"/api/command/{sysid}/manual/start")
        engaged = False
        deadline = time.time() + 25.0
        while time.time() < deadline:
            post(f"/api/command/{sysid}/manual",
                 {"x": 0.0, "y": 0.0, "z": 0.0, "r": 0.0}, timeout=5)
            time.sleep(1.0 / SETPOINT_HZ)
            cm = _st(sysid).get("custom_mode")
            if cm is not None and drv.mode_matches(cm, "position"):
                engaged = True
                break
        if not engaged:
            assert_dialect(False, r2 if not ok else
                           {"detail": "設定點持續送出 25s，機端仍未進入 position 模式"},
                           "manual/start（持續送設定點 25s）")

        d_cmd = _stream(sysid, STICK, PHASE_S)          # 1. 指令段
        settled = _settle(sysid)                        # 2. 煞停到靜止
        d_idle = _stream(sysid, 0.0, PHASE_S)           # 3. 靜止對照段

        assert settled, ("機體在中位下未能靜止，第 3 段不構成有效對照"
                         "——不宣告 pass（寧可沒有結論，也不要假的結論）")
        assert d_cmd is not None and d_idle is not None, "拿不到位置，無法量位移"
        assert d_cmd > MIN_COMMANDED_M and d_cmd > d_idle * RATIO, (
            f"指令段位移 {d_cmd:.2f}m、靜止對照段 {d_idle:.2f}m —— "
            f"未達判準（>{MIN_COMMANDED_M}m 且 >對照 {RATIO} 倍）。"
            "\n    → 若指令段接近 0：搖桿指令被靜默丟棄（ArduPilot 檢查 SYSID_MYGCS）。"
            "\n    → 若兩段都大：機體本來就在飄，位移不能歸因於我方指令。")
        return (f"sysid {sysid} 於 {alt:.1f}m：指令段位移 {d_cmd:.2f}m、"
                f"靜止對照段 {d_idle:.2f}m（{d_cmd / max(d_idle, 0.01):.0f} 倍）"
                f"——位移可歸因於我方 MANUAL_CONTROL")
    finally:
        if not _recover(sysid):
            print(f"  ⚠ sysid {sysid} 未在時限內上鎖，請人工確認")


if __name__ == "__main__":
    ap = sys.argv[1] if len(sys.argv) > 1 else "ardupilot"
    sys.exit(run("manual_stick", ap, lambda: check(ap)))
