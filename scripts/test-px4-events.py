#!/usr/bin/env python3
"""PX4 事件的翻譯與掉包偵測（issues/014 結構層 #1）。

兩件事，各對應一種「看起來正常」的失效：

  1. **版本不符時不翻譯**。事件 id 是事件名稱的雜湊，跨韌體重編譯可能改變——
     拿 A 版字典翻 B 版韌體，翻出來的是**看起來合理但完全錯誤**的句子，
     **比顯示原始 id 危險得多，因為讀的人不會懷疑它**。
  2. **事件序號的缺口要看得見**。EVENT 走 UDP，掉了就是掉了；而
     **「沒有事件」與「事件掉了」在畫面上完全同形**——一段安靜的事件流
     可能代表飛得很順，也可能代表我們瞎了那一段。

跑法（要在 backend 容器內，字典在那裡）：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-px4-events.py
"""
import sys

from app import px4_events as P

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


print("── 1. 韌體版本比對（三態）─────────────────────────────")
chk("拿不到版本 → unknown（**不是 match**）", P.fw_match(None) == "unknown")
chk("版本相同 → match", P.fw_match(P.DICT_FW) == "match")
chk("**帶建置型別後綴也算相同**（(official) 不改變事件 id）",
    P.fw_match(f"{P.DICT_FW} (official)") == "match",
    "不正規化的話，相符的會被判成不符")
chk("版本不同 → mismatch", P.fw_match("4.7.0 (official)") == "mismatch")
chk("看不懂的字串 → unknown（不猜）", P.fw_match("不知道") == "unknown")

print("\n── 2. 版本不符時不給 text（錯翻譯比沒翻譯危險）────────")
# 字典是延遲載入的——先觸發一次，否則 `_events` 是空的而測試會「skip 成 pass」
P._load()
# 找一個字典裡真的存在的事件
eid = None
for (comp, key) in list(P._events)[:200]:
    eid = (comp << 24) | key
    break
if eid is None:
    print("  （字典是空的——**skip ≠ pass**）")
    sys.exit(2)

good = P.describe(eid, "", P.DICT_FW)
bad = P.describe(eid, "", "4.7.0 (official)")
unk = P.describe(eid, "")
chk("版本相符 → 有 text", good and good.get("text"), (good or {}).get("text"))
chk("**版本不符 → 沒有 text**", bad is not None and "text" not in bad)
chk("而且說得出為什麼沒有翻譯",
    bad and "no_text_reason" in bad, (bad or {}).get("no_text_reason", "")[:50])
chk("**事件本身不丟**（id／名稱／版本狀態都還在）",
    bad and bad.get("event_name") and bad.get("dict_fw_match") == "mismatch")
chk("拿不到版本時仍然翻（但標成 unknown 讓 UI 提示）",
    unk and unk.get("text") and unk["dict_fw_match"] == "unknown")

print("\n── 3. 411 的線序解得對 ────────────────────────────────")
from app.mavlink_rx import _decode_event_seq, _EVT_SEQ_RESET
frame = bytes([0xFD, 3, 0, 0, 0, 0, 0, 0x9B, 0x01, 0]) + \
    (1234).to_bytes(2, "little") + bytes([_EVT_SEQ_RESET])
d = _decode_event_seq(frame)
chk("seq 解得出", d and d["seq"] == 1234, d)
chk("RESET 旗標讀得到", d and d["flags"] & _EVT_SEQ_RESET, d)
chk("非 MAVLink2 frame → None", _decode_event_seq(b"\xFE" * 20) is None)

print("\n── 4. 序號缺口：安靜的事件流可能是掉包，不是沒事 ──────")
import asyncio
import types

from app import db as _db, mavlink_rx as _rx

logged = []


async def _fake_insert(drone_id, session_id, sev, typ, detail):
    logged.append((sev, typ, detail))
    return {"id": 1, "type": typ, "severity": sev, "detail": detail}


class _NoBroadcast:
    async def broadcast(self, _m):
        pass


_db.insert_event = _fake_insert
_rx.manager = _NoBroadcast()
rx = _rx.MavlinkRx.__new__(_rx.MavlinkRx)
st = types.SimpleNamespace(drone_id="d", session_id=None, drone_name="t")


async def feed(ent, seq, reset=False):
    before = len(logged)
    await rx._event_gap(st, ent, seq, reset, "test")
    return len(logged) - before


async def gaps():
    e = {}
    chk("第一次看到不算掉包（沒有「之前」）", await feed(e, 100) == 0)
    chk("連號不算", await feed(e, 101) == 0)
    n = await feed(e, 106)
    chk("跳號 → 記一筆", n == 1)
    chk("**說得出漏了幾則**", logged[-1][2]["missed"] == 4, logged[-1][2])
    chk("嚴重度是 warning（不是 info——事件流有洞不是小事）",
        logged[-1][0] == "warning")
    chk("**序號倒退不當成掉包**（機端重連可能倒退）", await feed(e, 50) == 0)
    e2 = {"evt_seq": 65530}
    # 65531,65532,65533,65534,65535,0,1,2 ＝ 8 則。
    # **這一格第一次寫錯的是測試不是程式**：我隨手寫「漏 4 則」，
    # 而正確答案是 8——迴繞的算術要真的算一次，不能憑感覺
    chk("u16 迴繞：65530 → 3 是漏 8 則，不是漏 65533",
        await feed(e2, 3) == 1 and logged[-1][2]["missed"] == 8,
        logged[-1][2]["missed"])
    e3 = {"evt_seq": 500}
    chk("**RESET 旗標時不報**（機端歸零不是我們漏了）",
        await feed(e3, 0, reset=True) == 0)
    chk("而且之後從新序號續算（不是拿舊的比）",
        e3["evt_seq"] == 0, e3)


asyncio.run(gaps())

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
