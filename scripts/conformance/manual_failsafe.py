#!/usr/bin/env python3
"""一致性測試：手動控制的「操作者失聯 → 自動懸停」降級（issue 026 B3）。

**這是整套測試的試金石。** issue 030 就是這條路徑上的 bug：失聯降級寫死了 PX4
的模式編碼，在 ArduPilot 上送出模式號 4＝GUIDED，而不是承諾的 LOITER(5)。

那個 bug **躲過了 B0/B1/B2 三輪重構**，因為它直接讀模組層的表、沒有經過
`dialect()`——靜態上它只是取一個常數。所以本測試刻意**只看機端最後停在哪個
模式**，不看我方送了什麼、更不看程式碼長什麼樣。

驗收判準（PM 定）：**本測試若在 030 修復前的程式碼上跑，必須 fail。** 通不過這
一關就是測試套的覆蓋不夠。

## 為什麼失聯降級值得單獨一條測試

它是**安全鏈**：操作者失去連線時，讓飛機自主停在一個安全狀態。切錯模式的後果
不是「功能沒作用」，而是**飛機進入一個預期地面站會繼續下指令的模式**——與這段
程式存在的目的正好相反。而且它是 fire-and-forget（不等 ACK，避免阻塞 router
執行緒），**不進指令稽核表**，事後從紀錄上完全看不出來。

## 安全性

全程**不解鎖、不起飛**：只在地面對 disarmed 的機下模式指令。挑機時就排除
armed 的機（見 `_harness.pick`），不會打斷別人正在飛的架次。

跑法：
    python3 scripts/conformance/manual_failsafe.py [px4|ardupilot]
"""
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _harness import (Skip, custom_mode_of, mode_of, pick, post, run,  # noqa: E402
                      wait_verb, _driver)

#: 失聯降級之後，機端**應該**進入的動詞。對應到哪個實際模式由驅動決定，
#: 測試本身不持有方言——寫死的話它會複製它要抓的那個 bug（030 就是寫死造成的）。
DEGRADE_VERB = "hold"

#: 失聯門檻是 2.0s（mav._tick_manual）。等久一點確保降級一定已經發生。
WAIT_AFTER_LAST_SETPOINT = 4.0


def check(autopilot: str) -> str:
    sysid, info = pick(autopilot)
    drv = _driver(info.get("autopilot_raw"))

    if info.get("capabilities", {}).get("manual") != "ok":
        raise Skip(f"sysid {sysid} 的 manual 能力未開："
                   f"{info.get('capability_reasons', {}).get('manual', '(無原因)')}")

    before = mode_of(sysid)
    ok, r = post(f"/api/command/{sysid}/manual/start")
    assert ok, f"manual/start 失敗：{r}"

    # **不再送任何設定點** → deadman 逾時 → 失聯降級應該觸發
    time.sleep(WAIT_AFTER_LAST_SETPOINT)
    reached, cm = wait_verb(sysid, drv, DEGRADE_VERB, timeout=6.0)
    got = drv.decode_mode(cm) if cm is not None else None

    # 收尾：不論結果都把機收回 hold，不留在半途的模式
    post(f"/api/command/{sysid}/manual/stop")

    assert reached, (
        f"失聯降級後機端停在 **{got}**（custom_mode={cm}），"
        f"應為 {autopilot} 的 {DEGRADE_VERB}。起始模式={before}。"
        "\n    → 這正是 issue 030 的症狀：降級送出的模式號用了別家的編碼。"
        "\n    → 後果：操作者失聯時，飛機沒有進入程式所承諾的安全懸停狀態。")
    return (f"sysid {sysid}：停送設定點 {WAIT_AFTER_LAST_SETPOINT:.0f}s 後，"
            f"機端實際停在 {got}（＝{autopilot} 的 {DEGRADE_VERB}），起始模式 {before}")


if __name__ == "__main__":
    ap = sys.argv[1] if len(sys.argv) > 1 else "ardupilot"
    sys.exit(run("manual_failsafe", ap, lambda: check(ap)))
