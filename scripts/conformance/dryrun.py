#!/usr/bin/env python3
"""切換前的 dry-run 對照（issue 026 B3，PM 2026-08-12 指定）。

**回答一個問題：如果現在把能力值改由測試結果推導，哪些鍵會翻鎖？**

清單為空 → 直接切（切換當下零可見變化）。
清單不為空 → 那些鍵就是「**有 ok 標記但沒有對應測試**」的缺口，先回報再決定
（補測試 vs 接受降級）。

這個順序讓「行為變嚴」從風險變成驗收：不會出現「實飛驗過的鍵因為測試還沒寫
而被鎖」的中間態——因為切換前就先看見了。

**本腳本唯讀**：只讀 healthz 與測試結果檔，不動任何狀態、不下任何指令。

跑法：
    python3 scripts/conformance/dryrun.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _caps import CAP_TESTS, NOT_IMPLEMENTED, derive  # noqa: E402
from _harness import RESULTS_DIR, fleet  # noqa: E402


def load(autopilot: str) -> dict:
    p = RESULTS_DIR / f"{autopilot}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    drones = fleet()
    if not drones:
        print("沒有連線中的機，無法對照現況能力值。")
        return 1

    flips = []
    print("=== 目前能力值 vs 改由測試推導後 ===\n")
    seen_ap = set()
    for sysid, d in sorted(drones.items(), key=lambda kv: int(kv[0])):
        ap = d.get("autopilot")
        if ap in seen_ap:            # 同廠牌只需對照一次（推導不看單機）
            continue
        seen_ap.add(ap)
        cur = d.get("capabilities", {})
        results = load(ap)
        new, _reasons = derive(ap, results)

        print(f"── {ap}（以 sysid {sysid} 的現況為對照）")
        print(f"   已跑過的測項：{sorted(results) or '（無）'}")
        for key in sorted(set(cur) | set(new)):
            c, n = cur.get(key), new.get(key)
            if key not in CAP_TESTS:
                print(f"   ⚠ {key:<15} {c} → **無證據定義**（CAP_TESTS 未列）")
                flips.append((ap, key, c, "無證據定義"))
                continue
            if c == "ok" and n != "ok":
                need = [t for t in CAP_TESTS[key]
                        if results.get(t, {}).get("status") != "pass"]
                tag = "／".join(t + ("（未實作）" if t in NOT_IMPLEMENTED else "")
                                for t in need)
                print(f"   ✘ {key:<15} ok → {n}   缺：{tag}")
                flips.append((ap, key, c, n))
            elif c != n:
                print(f"   ~ {key:<15} {c} → {n}")
                flips.append((ap, key, c, n))
            else:
                print(f"   ✔ {key:<15} {c}（不變）")
        print()

    print("=== 結論 ===")
    if not flips:
        print("翻鎖清單為空 → 可以直接切換，切換當下零可見變化。")
        return 0
    print(f"**{len(flips)} 個鍵會變動**，切換前需回報：")
    for ap, key, c, n in flips:
        print(f"  - {ap} / {key}：{c} → {n}")
    print("\n這些是「有 ok 標記但沒有對應測試」的缺口——正是測試套該暴露的東西。")
    print("由使用者決定：補測試，還是接受降級。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
