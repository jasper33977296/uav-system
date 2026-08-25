#!/usr/bin/env python3
"""一致性測試：任務執行（issue 026 B3）。覆蓋能力鍵 `mission_start`、`mission_fly`。

驗完整序列：**上傳 → 解鎖 → 起飛 → 等到達高度 → 切 AUTO 任務**。

## 為什麼不能只驗「切 AUTO 有沒有 ACK」

實戰教訓（2026-08-11）：**地面直接 MISSION_START 在實機上會失敗**，所以序列
才設計成「先到高度再切任務」。而 028 又發現那個「等到達高度」讀的是**主機**的
高度——非主機的判斷完全與目標機無關。所以這條測試必須：

1. 用**非主機**跑（`pick` 會挑 disarmed 的機，主機通常已在用）
2. 驗**機端實際進入 mission 模式**（`mode_matches`，不是 ACK）
3. 驗**實際爬到高度**（`alt_rel`，不是 ACK）

## 對 ArduPilot 的意義

這是 issue 015 掛著的最後兩個鍵。它們一直是 `unverified`（AUTO 任務執行整段
未驗），而**混機編隊的第 3 步要切 MISSION**——所以這條測試通過與否，直接決定
混機編隊跑不跑得動。

跑法：
    python3 scripts/conformance/mission_fly.py [px4|ardupilot]
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _harness import (BACKEND, Skip, _driver, assert_dialect,  # noqa: E402
                      delete, fleet, local_wps, pick, post, run, wait_verb)

TAKEOFF_ALT = 15.0


def _st(sysid):
    return fleet().get(str(sysid), {})


def _recover(sysid):
    post(f"/api/command/{sysid}/mode/rtl")
    end = time.time() + 150.0
    while time.time() < end:
        if _st(sysid).get("armed") is False:
            return True
        time.sleep(2.0)
    return False


def check(autopilot: str) -> str:
    sysid, info = pick(autopilot)
    caps = info.get("capabilities", {})
    for key in ("mission_upload", "takeoff"):
        if caps.get(key) != "ok":
            raise Skip(f"前置能力 {key} 未開，無法驗任務執行："
                       f"{info.get('capability_reasons', {}).get(key, '(無原因)')}")
    drv = _driver(info.get("autopilot_raw"))

    # 航點以機體當下位置為原點——不寫死座標，理由見 _harness.local_wps
    wps = local_wps(info, TAKEOFF_ALT)
    wps.append({"seq": len(wps), "lat": 0.0, "lon": 0.0, "alt": None, "action": "rtl"})
    ok, m = post("/api/missions", {"name": f"conformance-fly-{autopilot}",
                                   "source": "plan-file", "waypoints": wps},
                 base=BACKEND)
    assert ok, f"建任務失敗：{m}"
    mission_id = m["id"]

    try:
        ok, r = post(f"/api/command/{sysid}/mission/fly",
                     {"mission_id": mission_id, "takeoff_alt": TAKEOFF_ALT},
                     timeout=180)
        assert_dialect(ok, r, "任務執行序列")
        steps = r.get("steps", {}) if isinstance(r, dict) else {}
        reached = steps.get("alt_reached", {}).get("alt_rel")

        # 序列宣稱成功不算數——**讀回 HEARTBEAT 確認機端真的在 mission 模式**
        engaged, cm = wait_verb(sysid, drv, "mission", timeout=15.0)
        got = drv.decode_mode(cm) if cm is not None else None
        assert engaged, (
            f"序列回報成功，但機端停在 **{got}**（custom_mode={cm}），不是 mission。"
            "\n    → ACK 與實際模式是兩件事（015 紀律）。")

        # 再確認它真的在空中執行，不是在地面切了個模式
        alt = _st(sysid).get("alt_rel")
        assert alt is not None and alt >= TAKEOFF_ALT * 0.5, (
            f"進了 mission 模式但高度只有 {alt} m——不像在空中執行任務")
        return (f"sysid {sysid}：上傳 {len(wps)} 項 → 起飛至 {reached} m → "
                f"機端實際進入 {got}（mission），當下高度 {alt:.1f} m")
    finally:
        if not _recover(sysid):
            print(f"  ⚠ sysid {sysid} 未在時限內上鎖，請人工確認")
        delete(f"/api/missions/{mission_id}", base=BACKEND)


if __name__ == "__main__":
    ap = sys.argv[1] if len(sys.argv) > 1 else "ardupilot"
    sys.exit(run("mission_fly", ap, lambda: check(ap)))
