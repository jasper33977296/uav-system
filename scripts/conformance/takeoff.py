#!/usr/bin/env python3
"""一致性測試：解鎖＋起飛（issue 026 B3）。覆蓋能力鍵 `arm`、`takeoff`。

**這條會真的把機飛起來。** 不飛不行——起飛序列的方言差異（相對高度 vs 絕對海拔、
空白參數 NaN vs 0.0、要不要先進 GUIDED）全都只有在機真的離地時才驗得出來。
送錯絕對海拔的症狀是「飛到幾百公尺外的高度」，靜態上看起來完全正常。

驗的是**機端實際爬到的高度**，不是 ACK。

## 對照：高度是相對的

目標 15m，判準是 `alt_rel >= 12`（80%）。用 `alt_rel` 而不是 `alt_msl`——後者
含地面海拔，SITL 約 488m，拿它比會永遠通過。

## 安全性與復原

飛完**一定降落**（RTL）並等到 disarmed 才回傳，不論成敗（`finally`）。
用非主機（`pick` 只挑 disarmed 的機，主機通常是 sysid 1）。

跑法：
    python3 scripts/conformance/takeoff.py [px4|ardupilot]
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _harness import Skip, assert_dialect, fleet, pick, post, run  # noqa: E402

TARGET_ALT = 15.0
REACH_RATIO = 0.8
CLIMB_TIMEOUT = 60.0
LAND_TIMEOUT = 120.0


def _alt(sysid):
    return fleet().get(str(sysid), {}).get("alt_rel")


def _armed(sysid):
    return fleet().get(str(sysid), {}).get("armed")


def _recover(sysid):
    """降落並等到上鎖。**不論測試成敗都要跑**——不能把機留在空中。"""
    post(f"/api/command/{sysid}/mode/rtl")
    end = time.time() + LAND_TIMEOUT
    while time.time() < end:
        if _armed(sysid) is False:
            return True
        time.sleep(2.0)
    return False


def check(autopilot: str) -> str:
    sysid, info = pick(autopilot)
    caps = info.get("capabilities", {})
    for key in ("arm", "takeoff"):
        if caps.get(key) != "ok":
            raise Skip(f"sysid {sysid} 的 {key} 能力未開："
                       f"{info.get('capability_reasons', {}).get(key, '(無原因)')}")

    ok, r = post(f"/api/command/{sysid}/takeoff", {"alt": TARGET_ALT}, timeout=120)
    try:
        assert_dialect(ok, r, "起飛請求")
        # ACK 只代表指令合法。真正的判準是**爬到高度**。
        want = TARGET_ALT * REACH_RATIO
        end, peak = time.time() + CLIMB_TIMEOUT, None
        while time.time() < end:
            a = _alt(sysid)
            if a is not None:
                peak = a if peak is None else max(peak, a)
                if a >= want:
                    break
            time.sleep(1.0)
        assert peak is not None and peak >= want, (
            f"起飛後最高只到 {peak} m，未達 {want} m（目標 {TARGET_ALT} m）。"
            "\n    → ACK 可能是 ACCEPTED 但機沒真的爬升："
            "檢查 param7 的高度語意（相對 vs 絕對海拔）與空白參數慣例。")
        steps = r.get("steps", r) if isinstance(r, dict) else {}
        guided = "guided" in steps
        return (f"sysid {sysid}：目標 {TARGET_ALT}m、實際爬到 {peak:.1f}m"
                f"（判準 {want:.0f}m）；GUIDED 前置={guided}")
    finally:
        landed = _recover(sysid)
        if not landed:
            print(f"  ⚠ sysid {sysid} 在 {LAND_TIMEOUT:.0f}s 內未上鎖，請人工確認")


if __name__ == "__main__":
    ap = sys.argv[1] if len(sys.argv) > 1 else "ardupilot"
    sys.exit(run("takeoff", ap, lambda: check(ap)))
