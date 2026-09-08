#!/usr/bin/env python3
"""補傳的前導環形（issues/047 項次 6，2026-09-08）：偵測到斷線之前那幾秒。

**判定「上行不通」本身要花時間**（agent 的 `UPLINK_STALE_S`，約 8 秒），
而在那之前資料其實已經在掉了。原本的實作是「判定不通之後才開始取樣」，
所以每一次斷線都固定少補開頭那幾秒——**而那幾秒正是狀態在變化的那幾秒**
（鏈路開始壞的時候，飛機多半正在做什麼）。

修法：不管通不通都一直取樣進一個小環形；斷線成立時，把裡面
「**最後一次被地面站確認之後**」的那幾筆補進待送佇列。界線用確認時刻、
不用偵測時刻——後者晚了一整個偵測延遲。

跑法（不需要服務、不需要網路、不需要飛機）：
    python3 scripts/test-backfill-lead.py
"""
import sys
import time

sys.path.insert(0, "/home/k200/uav-agent")

import backfill as B  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


class Rig:
    """直接驅動 `Backfill` 的內部邏輯，不起執行緒、不碰網路。"""

    def __init__(self, armed=True):
        self.bf = B.Backfill("x", 1, lambda: "uid", lambda: self.state,
                             lambda: self.link_ok,
                             confirmed_fn=lambda: self.confirmed)
        self.state = {"armed": armed, "alt_rel_m": 5.0, "mode": "AUTO",
                      "gps_fix": "3D", "lat": 24.77, "lon": 121.04}
        self.link_ok = True
        self.confirmed = 0.0
        self.t = 1000.0

    def tick(self, n=1):
        """走 n 拍（每拍 1 秒）。**呼叫的是出貨的那一份**（`Backfill.tick`）
        ——測試自己複製一份迴圈邏輯的話，被測程式改了它會安靜地變成
        測另一個東西。"""
        for _ in range(n):
            self.t += 1.0
            self.bf.tick(self.t)


print("── 1. 鏈路正常：一直取樣進前導環形，但一筆都不排進待送 ──────")
r = Rig()
r.tick(20)
chk("前導環形有 20 筆", len(r.bf.lead) == 20, len(r.bf.lead))
chk("**待送佇列是空的**", len(r.bf.buf) == 0, len(r.bf.buf))
chk("sampled 還是 0（那是「要送幾筆」的計數）", r.bf.sampled == 0)

print("\n── 2. 斷線成立：把「最後一次被確認之後」的補回來 ────────────")
# 地面站最後一次確認是 8 秒前（＝偵測延遲），斷線其實從那時就開始了
r.confirmed = r.t - 8.0
r.link_ok = False
r.tick(1)
chk("**補回 8 筆前導**（偵測延遲那 8 秒）", r.bf.lead_in == 8, r.bf.lead_in)
chk("待送佇列 = 8 筆前導 + 這一拍的 1 筆", len(r.bf.buf) == 9, len(r.bf.buf))
ms = [x["_m"] for x in r.bf.buf]
chk("時間戳嚴格遞增、沒有重複", ms == sorted(set(ms)) and len(ms) == 9, ms)
chk("最早那筆就是確認之後的第一筆", ms[0] == r.confirmed + 1.0, (ms[0], r.confirmed))

print("\n── 3. 斷線期間照常累積 ──────────────────────────────")
r.tick(10)
chk("再多 10 筆", len(r.bf.buf) == 19, len(r.bf.buf))

print("\n── 4. 反向驗證：沒有前導環形會少補的就是那 8 筆 ──────────")
r2 = Rig()
r2.tick(20)
r2.confirmed = 0.0            # 從來沒被確認過 → 整個環形都算可疑
r2.link_ok = False
r2.tick(1)
chk("從沒被確認過時，保守地整個環形都補", r2.bf.lead_in == 20, r2.bf.lead_in)

print("\n── 5. 沒解鎖：前導環形也不留（與即時閘門一致）──────────────")
r3 = Rig(armed=False)
r3.tick(10)
chk("前導環形是空的", len(r3.bf.lead) == 0, len(r3.bf.lead))
r3.link_ok = False
r3.tick(5)
chk("斷線也不補、只記 skipped_ground",
    len(r3.bf.buf) == 0 and r3.bf.skipped_ground == 5,
    (len(r3.bf.buf), r3.bf.skipped_ground))

print("\n── 6. 同一個時間戳不會進兩次 ───────────────────────────")
r4 = Rig()
r4.tick(5)
r4.confirmed = 0.0
r4.link_ok = False
r4.tick(1)
r4.link_ok = True             # 恢復
r4.tick(1)
r4.link_ok = False            # 又斷（前導環形裡有已經排進去的那幾筆）
r4.tick(1)
ms = [x["_m"] for x in r4.bf.buf]
chk("**待送佇列裡沒有重複的時間戳**", len(ms) == len(set(ms)), ms)

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
