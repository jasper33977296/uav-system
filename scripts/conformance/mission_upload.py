#!/usr/bin/env python3
"""一致性測試：任務上傳（issue 026 B3）。覆蓋能力鍵 `mission_upload`。

驗兩件方言差異：

1. **線序慣例**——ArduPilot 把 home 當 seq 0，實際航點從 seq 1 起算；PX4 不用。
   驗法是看**上傳後機端回報的項數**：ArduPilot 應比我方給的多一項。
2. **無座標項的 frame**——RTL 用 `MAV_FRAME_MISSION`(2)，不是 GLOBAL_RELATIVE_ALT(3)
   （issue 029：預設錯的時候，**任何以返航結尾的任務都上不去**，而 plan_check
   同時在建議以返航結尾）。所以本測試的任務**刻意以 RTL 結尾**。

上傳本身走完整握手＋**回讀逐項比對**（`verified`），不是送出就算數。

## 安全性

全程**不解鎖、不起飛**。上傳會覆蓋機上現有任務——這是 SITL 驗證環境的既定用途，
但跑之前會先確認機是 disarmed（`pick`）。

跑法：
    python3 scripts/conformance/mission_upload.py [px4|ardupilot]
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _harness import (BACKEND, _driver, delete, local_wps,  # noqa: E402
                      pick, post, run)

#: 三個航點＋一個 RTL 結尾。RTL 是刻意的（見模組說明第 2 點）。


def check(autopilot: str) -> str:
    sysid, info = pick(autopilot)
    drv = _driver(info.get("autopilot_raw"))

    # 以機體當下位置為原點（見 _harness.local_wps）：原本寫死蘇黎世座標，
    # 上傳雖然會過，但上傳的是一份飛機永遠飛不到的任務
    wps = local_wps(info)
    wps.append({"seq": len(wps), "lat": 0.0, "lon": 0.0, "alt": None,
                "action": "rtl"})                     # ← 029 的回歸點

    ok, r = post("/api/missions", {"name": f"conformance-{autopilot}",
                                   "source": "plan-file", "waypoints": wps},
                 base=BACKEND)
    assert ok, f"建任務失敗：{r}"
    mission_id = r["id"]
    try:
        ok, up = post(f"/api/command/{sysid}/mission/upload", {"mission_id": mission_id})
        assert ok, (f"上傳被拒：{up}"
                    "\n    → 若是 MAV_MISSION_UNSUPPORTED，檢查無座標項的 frame"
                    "（issue 029：RTL 要 MAV_FRAME_MISSION=2）")
        assert up.get("verified"), f"上傳未通過回讀比對：{up}"

        # 線序慣例：ArduPilot 多一個 home 項
        sent, wire = len(wps), up.get("wire_items")
        expect = sent + 1 if drv.home_at_seq0 else sent
        assert wire == expect, (
            f"上線項數 {wire}，預期 {expect}（送 {sent} 個航點，"
            f"{autopilot} 的 home_at_seq0={drv.home_at_seq0}）")
    finally:
        # 只刪自己造的資料（共用環境紀律）
        delete(f"/api/missions/{mission_id}", base=BACKEND)
    return (f"sysid {sysid}：{sent} 航點（以 RTL 結尾）上傳並回讀比對通過，"
            f"上線 {wire} 項（home_at_seq0={drv.home_at_seq0}）")


if __name__ == "__main__":
    ap = sys.argv[1] if len(sys.argv) > 1 else "ardupilot"
    sys.exit(run("mission_upload", ap, lambda: check(ap)))
