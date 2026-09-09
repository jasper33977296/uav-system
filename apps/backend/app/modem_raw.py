"""從 modem 的原始 AT 回應解出 serving cell 欄位。

## 為什麼要在這一層做

`link_metrics` 的 `pci`／`cell_id`／`band` 三欄今天全是 null，**而值一直都在
`raw` 裡**（實測 2026-09-07：107 筆全部如此；8/10–8/13 的舊資料相反——欄位有值、
沒有存 raw）。畫面上那三格顯示「—」，讀的人會以為「這個場域量不到細胞資訊」，
而事實是我們收到了、沒有解。**那是 §0.2e 的同族：我方的解析缺口穿上「沒有
資料」的外衣。**

解析放在後端而不是等機上代理改：

* `raw` 是**唯一**跨得過版本的東西——代理改版、對照表修正，歷史資料都能
  重算（`reference/fibocom-fm160/README.md` 當初存整包 raw 就是為了這件事）。
* 代理不在這個 repo 裡，而畫面現在就在說錯話。

## 欄位對照（手冊 §11.1.15，見 reference/fibocom-fm160/README.md）

    <IsServiceCell>,<rat>,<mcc>,<mnc>,<tac>,<cellid>,<narfcn>,
    <physicalcellId>,<band>,<bandwidth>,<ss_sinr>,<rxlev>,<ss_rsrp>,<ss_rsrq>

**進位：`tac`／`cellid`／`narfcn`／`physicalcellId` 是十六進位。**

參考文件原本把 `physicalcellId` 記成十進位——那是從一筆「85」推出來的，而 85
在兩種進位下都合法，**推不出結論**。本場域的實測值是 `8D`：它有字母，
十進位讀不了，所以這一欄是十六進位。已回頭更新那份對照表。

**認不得就不填。** 欄位數不足、進位解不開、值超出合法範圍（PCI 0–1007）時
一律留 null——**填一個看似合理的錯值，比空著更難發現**（`doc/verification-
checklist.md` §1.5 就是在講這件事）。

## 換算（3GPP TS 38.133，索引 → dB）

    SS-RSRP: dBm = index − 156      SS-RSRQ: dB = index / 2 − 43
    SS-SINR: dB  = index / 2 − 23

**這裡不用它們覆蓋 rsrp／rsrq／sinr**：那三個欄位機上走的是 `AT+CESQ`
（3GPP 標準指令、換算有明文規範），兩個指令的取樣時刻也不同。GTCCINFO 的
索引值收在 `_derived` 裡供對照，不當成量測值。

自我檢查（兩筆已知輸入）：

    docker exec -i -w /srv uav-backend python3 -m app.modem_raw
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

#: 解析規則版本。**寫進 `_derived`**：對照表日後若修正，看得出哪些列是用
#: 哪一版算的，才有辦法只重算該重算的那些。
RULE = "fm160-gtccinfo-v2"

_NR_RE = re.compile(r"NR service cell:\s*\r?\n\s*([0-9A-Fa-f,]+)")

#: NR PCI 合法範圍（3GPP TS 38.211 §7.4.2.1）：0–1007
PCI_MAX = 1007


def _hex(s: str) -> int | None:
    try:
        return int(s, 16)
    except ValueError:
        return None


def parse_gtccinfo(text: str) -> dict[str, Any] | None:
    """`AT+GTCCINFO?` 的 NR service cell 那一行 → 欄位 dict。認不得回 None。"""
    m = _NR_RE.search(text or "")
    if not m:
        return None
    f = m.group(1).split(",")
    if len(f) < 14:
        # **不猜。** 欄位少了就是這台模組回的格式不一樣（LTE／EN-DC 另有格式）
        log.debug("GTCCINFO 欄位數 %d，不是 NR service cell 的 14 欄", len(f))
        return None
    tac, cellid, narfcn, pci = _hex(f[4]), _hex(f[5]), _hex(f[6]), _hex(f[7])
    if pci is None or pci > PCI_MAX:
        pci = None                      # 超出合法範圍＝我們讀錯了，不填
    band = None
    if f[8].isdigit() and f[8].startswith("5") and len(f[8]) == 4:
        band = f"n{int(f[8]) - 5000}"   # 「5」前綴＝NR（與 narfcn 值域互相印證）
    out: dict[str, Any] = {
        "pci": pci, "cell_id": cellid, "band": band,
        "tac": tac, "narfcn": narfcn,
        "bandwidth_mhz": int(f[9]) if f[9].isdigit() else None,
        "rat": f[1], "mcc": f[2], "mnc": f[3],
    }
    # 索引值原樣留著並附換算，**但不當成量測值**（見檔頭）
    def idx(i: int) -> int | None:
        return int(f[i]) if f[i].isdigit() else None
    ss_sinr, rxlev, ss_rsrp, ss_rsrq = idx(10), idx(11), idx(12), idx(13)
    out["gtcc_idx"] = {"ss_sinr": ss_sinr, "rxlev": rxlev,
                       "ss_rsrp": ss_rsrp, "ss_rsrq": ss_rsrq}
    # **GTCCINFO 的刻度與 CESQ 差一格，而且只差在 RSRP／RSRQ。**
    # 2026-09-09 同一顆模組連取 10 對樣本：ss_rsrp／ss_rsrq 的索引每一筆都
    # 剛好比 CESQ 低 1（10/10），ss_sinr 則完全相同（差值隨機 ±2，是取樣噪聲）。
    # CESQ 那邊有 `AT+CESQ=?` 自報的值域可以把公式釘死（idx-157／idx/2-43.5／
    # idx/2-23.5），這裡沒有，所以**用那個實測的一格差把它對回同一條刻度**：
    #   ss_rsrp: (idx+1)-157 = idx-156      ss_rsrq: (idx+1)/2-43.5 = idx/2-43
    #   ss_sinr: 同刻度，直接用 idx/2-23.5
    # 這樣兩條路才會給出同一個 dB——不然畫面上的 rsrp 與 _derived 裡的
    # ss_rsrp 會永遠差 1，而沒有人說得出為什麼。
    out["gtcc_db"] = {
        "ss_sinr": None if ss_sinr is None else ss_sinr / 2 - 23.5,
        "ss_rsrp": None if ss_rsrp is None else ss_rsrp - 156,
        "ss_rsrq": None if ss_rsrq is None else ss_rsrq / 2 - 43,
    }
    return out


def enrich(sample: dict[str, Any]) -> list[str]:
    """就地補上 `pci`／`cell_id`／`band`。回補了哪幾欄（空 list＝沒補）。

    **這是唯一一份解碼**（2026-09-09 起）。原本機上代理也解一份，而這裡
    只補 null、不覆蓋它——理由寫的是「機上是第一手」。**那個理由是錯的**：
    兩邊解的是同一個原始字串，沒有誰比較第一手，第一手的是 `raw` 本身。
    結果就是機上把 PCI 的十六進位當十進位讀（`85` 讀成 85 而不是 133），
    而正確的這一份因為「不覆蓋」永遠沒有機會生效。

    仍然只補 null——但那現在是**保險**，不是讓步：代理已經不填這三欄了，
    真的補到值就代表有人又在機上解了一次，該回頭把它拿掉。
    """
    raw = sample.get("raw")
    if not isinstance(raw, dict):
        return []
    got = parse_gtccinfo(raw.get("GTCCINFO") or "")
    if not got:
        return []
    filled = []
    for k in ("pci", "cell_id", "band"):
        if sample.get(k) is None and got.get(k) is not None:
            sample[k] = got[k]
            filled.append(k)
    if filled:
        # **留下痕跡**：哪幾欄是解出來的、用哪一版規則。畫面要不要標示是它的事，
        # 但資料自己必須說得出來源——否則日後沒人分得出「模組報的」與「我們算的」
        raw["_derived"] = {"rule": RULE, "fields": filled,
                           "narfcn": got.get("narfcn"), "tac": got.get("tac"),
                           "bandwidth_mhz": got.get("bandwidth_mhz"),
                           "gtcc_idx": got.get("gtcc_idx"),
                           "gtcc_db": got.get("gtcc_db")}
    return filled


# ── 哨兵值：模組說「沒有值」的時候，那不是一個很差的量測 ──────────────
#
# 實測（2026-09-08）：`link_metrics` 有 4 筆 `sinr = -3276`。原始回應是
#     +QENG: "servingcell","LIMSRV",...,-95,-12,-3276,1,-
# ——`LIMSRV`（受限服務）狀態下模組把 SINR 回成無效標記（-32768 的縮放值），
# 而 RSRP／RSRQ 是好的。**代理照抄成一個數字送上來，我方照單全收存進資料庫**，
# 於是場域頁的弱區標籤寫著「最差 -3276 dB」，那一格的最差值從此永遠是它。
#
# 守門放在後端而不是等代理改：理由與這個模組存在的理由同一條——**代理不在
# 這個 repo 裡，而資料庫現在就在收假值**。這是最後一道我方控制得到的關卡。
#
# 上下界取物理上量得出來的範圍，不取「合理」範圍：目的是擋哨兵值，
# 不是替使用者判斷訊號好不好。範圍內的爛值是真的爛值，要照樣存。
SANE_RANGE: dict[str, tuple[float, float]] = {
    # 上界對齊刻度本身能表示的最大值（2026-09-09 由 `AT+CESQ=?` 的值域定案），
    # 不是「常見值」：SS-RSRQ 實務上不會超過 -3，但刻度到 +19.5——
    # 拿 +10 當上界會把刻度上端的合法值當成哨兵丟掉
    "sinr": (-30.0, 40.0),      # SS-SINR 刻度 -23…+40
    "rsrp": (-156.0, -20.0),    # SS-RSRP 刻度 -156…-31
    "rsrq": (-45.0, 20.0),      # SS-RSRQ 刻度 -43…+19.5
    "cqi": (0, 31),
}


def drop_sentinels(m: dict) -> dict[str, float]:
    """把超出量測範圍的欄位就地改成 None，回傳被拿掉的那些。

    **拿掉要留痕**：丟掉的值寫進 `raw._dropped`。悄悄丟掉與當成真值一樣糟
    ——事後要查「這一筆為什麼沒有 SINR」時，答案必須在資料裡，不是在某個人的
    記憶裡。
    """
    dropped: dict[str, float] = {}
    for field, (lo, hi) in SANE_RANGE.items():
        v = m.get(field)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv < lo or fv > hi:
            dropped[field] = fv
            m[field] = None
    if dropped:
        raw = m.get("raw")
        m["raw"] = {**raw, "_dropped": dropped} if isinstance(raw, dict) \
            else {"_dropped": dropped}
    return dropped


if __name__ == "__main__":       # 自我檢查：兩筆已知輸入，值都在文件裡對過
    ours = ('AT+GTCCINFO?\r\r\n+GTCCINFO: \r\nNR service cell: \r\n'
            '1,9,999,66,8D,234001,AFDA0,8D,5079,100,85,53,53,64\r\n\r\nOK')
    tmo = ('+GTCCINFO: \r\nNR service cell: \r\n'
           '1,9,310,260,A2E700,101E0212F,7EFAE,290,5041,100,113,87,87,64\r\n\r\nOK')
    a = parse_gtccinfo(ours)
    b = parse_gtccinfo(tmo)
    ok = True

    def chk(label: str, got: Any, want: Any) -> None:
        global ok
        good = got == want
        ok = ok and good
        print(f"{'✓' if good else '✗'} {label}: {got}" + ("" if good else f"（期望 {want}）"))

    chk("本場域 PCI（0x8D）", a["pci"], 141)
    # 2026-09-09 實測：PCI 欄全是數字的時候，十進位解析**不會報錯只會錯**
    # ——這一筆就是那個形狀（`85` 是十六進位的 133）。用真實觀測釘住它
    c = parse_gtccinfo('+GTCCINFO: \r\nNR service cell: \r\n'
                       '1,9,999,66,8D,214001,AFDA0,85,5079,100,105,91,91,64\r\n\r\nOK')
    chk("PCI 欄全是數字時仍是十六進位（0x85）", c["pci"], 133)
    chk("同一筆的 NCI", c["cell_id"], 2179073)
    chk("本場域 band", a["band"], "n79")
    chk("本場域 NCI（0x234001）", a["cell_id"], 2310145)
    chk("本場域 NR-ARFCN（0xAFDA0）", a["narfcn"], 720288)
    chk("本場域 頻寬", a["bandwidth_mhz"], 100)
    chk("T-Mobile 範例 band", b["band"], "n41")
    chk("T-Mobile 範例 NR-ARFCN（0x7EFAE）", b["narfcn"], 520110)
    chk("T-Mobile 範例 PCI（0x290）", b["pci"], 656)
    chk("欄位不足時不猜", parse_gtccinfo("NR service cell: \r\n1,9,999\r\n"), None)
    chk("沒有 NR 段時不猜", parse_gtccinfo("+GTCCINFO: \r\nLTE service cell:\r\n"), None)

    sent = {"sinr": -3276.0, "rsrp": -95.0, "raw": {"at_qeng": "…"}}
    got = drop_sentinels(sent)
    chk("哨兵值拿掉", (got, sent["sinr"], sent["rsrp"]), ({"sinr": -3276.0}, None, -95.0))
    chk("拿掉要留痕", sent["raw"]["_dropped"], {"sinr": -3276.0})
    keep = {"sinr": -12.0, "rsrp": -120.0}
    chk("範圍內的爛值照樣留著", (drop_sentinels(keep), keep["sinr"]), ({}, -12.0))
    print("全部通過" if ok else "有項目不通過")
    raise SystemExit(0 if ok else 1)
