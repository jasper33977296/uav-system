#!/usr/bin/env python3
"""一致性測試：切模式（issue 026 B3）。

覆蓋能力鍵 `hold`／`rtl`／`land`（`position` 另計入 `manual`，見 `_caps.py`）。

**只看機端實際停在哪個模式，不看 ACK。** 015 的驗收紀律就是這條：ArduPilot 對
不支援的模式號**照樣回 ACCEPTED**，只有讀回 HEARTBEAT 才知道有沒有真的切過去。
「送出成功」與「切換成功」是兩件事。

比對用 `drv.mode_matches()`——測試本身不持有方言（見 `_harness` 的說明）。

## 安全性

全程**不解鎖、不起飛**：只對 disarmed 的機下模式指令，馬達不會轉。測完把機
收回 `hold`，不留在 `land`／`rtl` 這種帶行為語意的模式。

跑法：
    python3 scripts/conformance/mode_set.py [px4|ardupilot]
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _harness import _driver, pick, post, run, wait_verb  # noqa: E402

#: 要驗的動詞。**每一個都對應一個能力鍵**，不驗的鍵就不該宣告 ok。
VERBS = ("hold", "rtl", "land")


def check(autopilot: str) -> str:
    sysid, info = pick(autopilot)
    drv = _driver(info.get("autopilot_raw"))
    results = []
    try:
        for verb in VERBS:
            ok, r = post(f"/api/command/{sysid}/mode/{verb}")
            assert ok, f"切 {verb} 的請求失敗：{r}"
            # 送出成功不算數——讀回 HEARTBEAT 確認機端真的在那個模式
            reached, cm = wait_verb(sysid, drv, verb, timeout=8.0)
            got = drv.decode_mode(cm) if cm is not None else None
            assert reached, (
                f"切 {verb} 後機端停在 **{got}**（custom_mode={cm}），不是 {verb}。"
                f"\n    → ACK 可能回了 ACCEPTED，但模式沒真的切過去："
                "**送出成功與切換成功是兩件事**。")
            results.append(f"{verb}→{got}")
    finally:
        # 不論成敗都收回 hold：land／rtl 帶行為語意，不該留給下一個人
        post(f"/api/command/{sysid}/mode/hold")
    return f"sysid {sysid}：" + "、".join(results) + "（皆讀回 HEARTBEAT 確認）"


if __name__ == "__main__":
    ap = sys.argv[1] if len(sys.argv) > 1 else "ardupilot"
    sys.exit(run("mode_set", ap, lambda: check(ap)))
