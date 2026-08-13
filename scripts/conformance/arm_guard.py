#!/usr/bin/env python3
"""一致性測試：自動模式下的裸 arm 防護（issue 031）。

**這條測的是「該擋的有沒有擋」，而不是「能不能動」**——與其他測項相反。

事故（2026-08-13）：有人對一台停在 MISSION 模式、機上載有任務的機下裸 `arm`，
它立即自主起飛爬到 50m。真機上就是 fly-away。

## 兩個方向都要驗，缺一不可

1. **該擋的擋住**：自動執行模式（mission/rtl/land）下的裸 arm → 409，且**機沒有解鎖**
2. **不該擋的放行**：切到 hold 之後 arm → 成功

只驗方向 1 的話，一個「什麼都擋」的防護也會通過——那種防護會在真正要用時被
繞過或關掉。**擋錯東西的安全機制，比沒有安全機制更危險**，因為它訓練使用者
去繞過它。

## 安全性

全程在地面。方向 1 的預期結果就是**不解鎖**；方向 2 會短暫解鎖（hold 模式、
地面、不會飛），測完立即上鎖並確認。

跑法：
    python3 scripts/conformance/arm_guard.py [px4|ardupilot]
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _harness import Skip, _driver, fleet, pick, post, run, wait_verb  # noqa: E402


def _armed(sysid):
    return fleet().get(str(sysid), {}).get("armed")


def _disarm_and_wait(sysid, timeout=40.0):
    end = time.time() + timeout
    while time.time() < end:
        if _armed(sysid) is False:
            return True
        post(f"/api/command/{sysid}/disarm")
        time.sleep(5.0)
    return False


def check(autopilot: str) -> str:
    sysid, info = pick(autopilot)
    if info.get("capabilities", {}).get("arm") != "ok":
        raise Skip(f"sysid {sysid} 的 arm 能力未開")
    drv = _driver(info.get("autopilot_raw"))
    try:
        # ── 方向 1：自動模式下必須擋 ────────────────────────────────
        ok, _ = post(f"/api/command/{sysid}/mode/mission")
        assert ok, "切 mission 模式失敗，無法建立測試前提"
        engaged, cm = wait_verb(sysid, drv, "mission", timeout=8.0)
        assert engaged, f"機端沒進 mission 模式（custom_mode={cm}），前提不成立"

        ok, r = post(f"/api/command/{sysid}/arm")
        assert not ok, (
            "**自動模式下的裸 arm 沒有被擋下**——這正是 031 的事故情境："
            "機會立即自主起飛。")
        text = r if isinstance(r, str) else str(r)
        for must in ("模式", "hold"):
            assert must in text, (
                f"拒絕訊息缺少「{must}」相關指引：{text[:160]}"
                "\n    → 擋下來但不告訴人怎麼辦，使用者只會去找繞過的方法。")
        assert _armed(sysid) is False, "被擋下卻還是解鎖了（防護沒有真的生效）"

        # ── 方向 2：切 hold 之後必須放行 ────────────────────────────
        ok, _ = post(f"/api/command/{sysid}/mode/hold")
        assert ok, "切 hold 失敗"
        assert wait_verb(sysid, drv, "hold", timeout=8.0)[0], "機端沒進 hold"
        ok, r2 = post(f"/api/command/{sysid}/arm")
        assert ok, (f"切到 hold 之後仍被擋下：{r2}"
                    "\n    → 防護擋錯範圍。擋錯東西的安全機制比沒有更危險，"
                    "它會訓練使用者去繞過它。")
        return (f"sysid {sysid}：mission 模式下裸 arm 被擋（機未解鎖，訊息含出路）；"
                f"切 hold 後 arm 放行")
    finally:
        if not _disarm_and_wait(sysid):
            print(f"  ⚠ sysid {sysid} 未能上鎖，請人工確認")
        post(f"/api/command/{sysid}/mode/hold")


if __name__ == "__main__":
    ap = sys.argv[1] if len(sys.argv) > 1 else "px4"
    sys.exit(run("arm_guard", ap, lambda: check(ap)))
