#!/usr/bin/env python3
"""模式解碼的方言：**ArduPilot 的 custom_mode 0 是 STABILIZE，不是「未設定」。**

原本 ArduPilot 的 decode_mode 抄了 PX4 的 `if not custom_mode: return "—"`。
那條對 PX4 成立（它把 main/sub 模式打包在高位元組，0＝什麼都沒設），
對 ArduPilot 是錯的。

**後果不只是顯示難看**：STABILIZE 正是飛手用遙控器手飛的模式，而畫面用它
判斷「飛手是不是接管了」。譯成「—」時，地面站分不出「飛手拿著」與「不知道
現在什麼模式」——那是狀態機文件 §1 說 PILOT_CONTROL「必須是顯式狀態」的那一格。
2026-08-26 實機看到：機在 STABILIZE，畫面顯示「—」。

用法：python3 scripts/test-mode-decode.py
"""
import sys
sys.path.insert(0, "/home/k200/uav-system/libs")
import autopilot  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


ap, px = autopilot.get_driver(3), autopilot.get_driver(12)
chk("ArduPilot 0 → STABILIZE（不是「—」）", ap.decode_mode(0) == "STABILIZE",
    ap.decode_mode(0))
chk("**反向驗證**：PX4 的 0 仍是「—」（它真的是未設定）",
    px.decode_mode(0) == "—", px.decode_mode(0))
chk("STABILIZE 沒有廠牌無關動詞（它不是 mission/hold/rtl/land）",
    ap.decode_verb(0) is None)
for cm, want in ((3, "AUTO"), (5, "LOITER"), (6, "RTL"), (9, "LAND")):
    chk(f"ArduPilot {cm} → {want}", ap.decode_mode(cm) == want, ap.decode_mode(cm))
for cm in (0, 3, 5, 6, 9):
    chk(f"認不得的值不留空（{cm}）", bool(ap.decode_mode(cm)))
chk("認不得的模式號原樣顯示（不寫「未知」）",
    ap.decode_mode(99) == "MODE_99", ap.decode_mode(99))

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
