"""能力鍵 ← 一致性測項的對應（issue 026 B3）。

**這張表是「宣告 ok 需要什麼證據」的定義。** 能力值不再由人工字典決定，而是
「該鍵要求的測項**全部** pass」∩「這台機的執行期前提滿足」。

## 為什麼一個鍵可以要求多個測項

`manual` 是實例：它同時承諾兩件事——**搖桿真的能操縱**，以及**失聯時真的會自動
懸停**。後者是安全鏈（issue 030：修復前 ArduPilot 會切進 GUIDED 而不是 Hold）。
只驗前者就宣告 ok，等於承諾了一個沒驗過的安全行為。

## 為什麼「沒有對應測項」不等於「不可用」

它等於**我們沒有證據**。四態語意裡那是 `unverified`（僅觀察、全鎖），不是
`unsupported`（機型不支援）。兩者的差別在於：前者是我們的功課沒做，後者是對方
真的不支援——**混為一談會讓「我們還沒驗」看起來像「這台機不行」**。
"""

#: 能力鍵 → 需要全部通過的測項名。
#: 未列在此表的鍵＝**還沒定義證據要求**，推導時一律當作沒有證據。
CAP_TESTS = {
    "hold":           ("mode_set",),
    "rtl":            ("mode_set",),
    "land":           ("mode_set",),
    "mission_upload": ("mission_upload",),
    # manual 要兩條：能操縱 ＋ 失聯會自動懸停（見模組說明）
    "manual":         ("manual_stick", "manual_failsafe"),
    # arm 與 takeoff 由同一條起飛序列測項覆蓋（解鎖是它的必經步驟）
    "arm":            ("takeoff",),
    "takeoff":        ("takeoff",),
    # 任務執行整段：起飛→到高度→切 AUTO
    "mission_start":  ("mission_fly",),
    "mission_fly":    ("mission_fly",),
}

#: 尚未實作的測項——列出來是為了**讓缺口有名字**。
#: 沒有這份清單的話，「某鍵拿不到 ok」看起來會像 bug 而不是待辦。
NOT_IMPLEMENTED = ("manual_stick", "takeoff", "mission_fly")


def derive(autopilot: str, results: dict) -> tuple[dict, dict]:
    """由測試結果推導能力值。回傳 (caps, reasons)——**不含執行期前提**。

    執行期前提（例如 ArduPilot 的 `SYSID_MYGCS`）由驅動另外套用；兩者是不同的
    東西，混在一起會得到「用測試結果宣告一台沒設好的機可用」這種錯誤。
    """
    caps, reasons = {}, {}
    for key, tests in CAP_TESTS.items():
        missing = [t for t in tests if results.get(t, {}).get("status") != "pass"]
        if not missing:
            caps[key] = "ok"
            continue
        caps[key] = "unverified"
        detail = []
        for t in missing:
            st = results.get(t, {}).get("status")
            if t in NOT_IMPLEMENTED and st is None:
                detail.append(f"{t}（測項尚未實作）")
            elif st is None:
                detail.append(f"{t}（未跑過）")
            else:
                detail.append(f"{t}（{st}）")
        reasons[key] = (f"一致性測試未通過：{'、'.join(detail)}"
                        f"——{autopilot} 的方言在此動詞上**沒有證據**，僅觀察")
    return caps, reasons
