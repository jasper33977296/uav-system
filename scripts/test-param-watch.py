#!/usr/bin/env python3
"""參數變了要說得出來（issues/058 A）——比對與歸因的邏輯。

在後端容器裡跑（要 app 套件）：
    docker cp scripts/test-param-watch.py uav-backend:/tmp/ && \\
    docker exec -w /srv uav-backend python /tmp/test-param-watch.py

端到端（真的飛控讀一輪 → DB → 事件）另外做，見 issues/058 的〈解決方式〉。
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/srv")
from app.param_watch import attribute, diff, same, written_of  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


T0 = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)

print("── 同一個值嗎 ───────────────────────────────────────────")
chk("float32 表示差不算變", same(0.30000001192092896, 0.3))
chk("真的變了就是變了", not same(6.0, 100.0))
chk("NaN 與 NaN 相同", same(float("nan"), float("nan")), "NaN 是合法的參數值")
chk("NaN 與數字不同", not same(float("nan"), 1.0))
chk("整數與同值浮點相同", same(5, 5.0), "decode_param 可能回 int")
chk("0 與極小值相同", same(0.0, 1e-12))
chk("快照裡的 null 對 NaN 算相同", same(None, float("nan")) and same(float("nan"), None),
    "param_sets 是 JSONB，NaN 存成 null；不然每個 NaN 參數都會被報成變了")
chk("null 對數字不同", not same(None, 1.0))

print("\n── 比對 ─────────────────────────────────────────────────")
known = {"FENCE_ENABLE": (0.0, T0), "FENCE_ALT_MAX": (100.0, T0), "WP_SPD": (5.0, T0)}
ch, add = diff(known, {"FENCE_ENABLE": 1.0, "FENCE_ALT_MAX": 6.0, "WP_SPD": 5.0,
                       "NEW_PARAM": 3.0})
chk("抓到兩個變了的", [c[0] for c in ch] == ["FENCE_ALT_MAX", "FENCE_ENABLE"], ch)
chk("帶舊值、新值與上次確認時刻", ch[0][1:] == (100.0, 6.0, T0))
chk("沒變的不列", "WP_SPD" not in [c[0] for c in ch])
chk("新參數另外列（沒有舊值可比）", add == ["NEW_PARAM"])
ch, add = diff(known, {"WP_SPD": 5.0})
chk("**沒讀到的不算消失**（可能只是還沒收到）", ch == [] and add == [])

print("\n── 是不是經由指令服務改的 ───────────────────────────────")
changes = [("FENCE_ALT_MAX", 100.0, 6.0, T0), ("WP_SPD", 5.0, 3.0, T0)]
rows = [(10, T0 + H, {"WP_SPD": 3.0})]
v = attribute(changes, rows)
chk("名字與值都對上＝本系統改的", v == {"WP_SPD": 10})
chk("沒有寫入紀錄的＝不是經由指令服務", "FENCE_ALT_MAX" not in v)
v = attribute(changes, [(11, T0 + H, {"WP_SPD": 4.0})])
chk("**名字對但值不對＝不算**", v == {},
    "我們寫了 4、之後有人又改成 3，不能說成是我們")
v = attribute(changes, [(12, T0 - H, {"WP_SPD": 3.0})])
chk("**上次確認之前的寫入不算**", v == {},
    "那筆寫入之後我們還確認過舊值，所以它不是這次變更的原因")
v = attribute(changes, [(13, T0 + H, {"WP_SPD": 3.0}), (14, T0 + 2 * H, {"WP_SPD": 3.0})])
chk("多筆取最後一筆", v == {"WP_SPD": 14})

print("\n── 一筆指令紀錄實際寫了什麼 ─────────────────────────────")
# 形狀照 command_log 實際的列（2026-09-14 #783）
P = '{"why": "fc_fence", "FENCE_TYPE": 4, "FENCE_ACTION": 1}'
D = '{"written": {"FENCE_TYPE": 4.0, "FENCE_ACTION": 1.0}, "clamped": [], "verified": true}'
chk("看 detail.written（讀回確認過的值）", written_of(P, D) == {"FENCE_TYPE": 4.0, "FENCE_ACTION": 1.0})
chk("**呼叫端的 why 不是參數**", "why" not in written_of(P, D))
chk("飛控夾過值時信 written 不信 params",
    written_of('{"WP_SPD": 50}', '{"written": {"WP_SPD": 20.0}}') == {"WP_SPD": 20.0})
chk("detail 被截斷解不開 → 退回 params 的數值欄位",
    written_of(P, '{"written": {"FENCE_TY…【留痕截斷') == {"FENCE_TYPE": 4, "FENCE_ACTION": 1})
chk("布林不當參數值", written_of('{"force": true, "X": 1}', None) == {"X": 1})

print("\n" + ("✓ 全部通過" if ok else "✗ 有失敗"))
sys.exit(0 if ok else 1)
