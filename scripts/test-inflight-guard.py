#!/usr/bin/env python3
"""空中上傳守門的判定測試（狀態機文件 §3-A）。

**為什麼要有這支**：這道門擋的是「上傳在地面是存檔、在空中是立即改航線」——
同一個動作、兩種語意，而畫面上長得一模一樣。判斷錯的兩個方向代價完全不對稱：
該擋沒擋＝飛機轉向一個沒人指定過的位置；不該擋卻擋了＝地面上傳不了、
使用者會去找繞道。所以四種情境都要跑一次。

**用真的驅動**（libs/autopilot），不自己複製一份模式號對照——複製一份就是
第二個事實來源，而它會漂移。PX4 與 ArduPilot 的 mission 模式號不同，
兩家都要驗。

用法（在 command 容器內跑）：
    docker compose exec -T uav-command python /srv/scripts/test-inflight-guard.py
"""
import sys
import types

sys.path.insert(0, "/srv")

from app import main as M            # noqa: E402
from libs import autopilot           # noqa: E402

# **兩家的 encode_mode 回傳的東西不一樣**，這是個容易踩的陷阱：
#   ArduPilot → (custom_mode, 0)      ← 第一個元素就是心跳裡的 custom_mode
#   PX4       → (main_mode, sub_mode) ← 心跳裡的 custom_mode 是 main<<16 | sub<<24
# 第一版測試直接拿 [0] 當 custom_mode 餵給 PX4，於是「空中飛任務」被判成放行，
# 看起來像守門壞了——其實是測試餵錯值。用 mode_matches 反推才不會再踩一次。
ARDU = autopilot.get_driver(3)
PX4 = autopilot.get_driver(12)
ARDU_AUTO = ARDU.encode_mode("mission")[0]
ARDU_HOLD = ARDU.encode_mode("hold")[0]


def px4_custom(verb):
    main, sub = PX4.encode_mode(verb)
    cm = (main << 16) | (sub << 24)
    assert PX4.mode_matches(cm, verb), "組出來的 custom_mode 驅動自己不認得"
    return cm


PX4_AUTO = px4_custom("mission")


def guard(**d):
    """把一台機的狀態塞進 router，問守門要不要擋。"""
    M.router = types.SimpleNamespace(drones={1: d})
    return M._inflight_upload_block(1)


ok = True


def case(label, want_block, **d):
    global ok
    got = guard(**d)
    good = bool(got) == want_block
    ok &= good
    print(f"{'✓' if good else '✗'} {label:<40} → "
          f"{'擋下' if got else '放行'}")


print("── ArduPilot ──────────────────────────────────────────")
case("未 armed（地面存檔）", False,
     armed=False, autopilot=3, custom_mode=ARDU_AUTO, alt_rel=0.0)
case("armed 但還在地上", False,
     armed=True, autopilot=3, custom_mode=ARDU_AUTO, alt_rel=0.3)
case("armed、空中、正在飛任務", True,
     armed=True, autopilot=3, custom_mode=ARDU_AUTO, alt_rel=42.0)
case("armed、空中、hold（A2 的第二步）", False,
     armed=True, autopilot=3, custom_mode=ARDU_HOLD, alt_rel=42.0)
case("armed、空中、飛手手飛（STABILIZE）", False,
     armed=True, autopilot=3, custom_mode=0, alt_rel=42.0)
case("模式未知（不擋不確定的狀態）", False,
     armed=True, autopilot=3, custom_mode=None, alt_rel=42.0)
case("高度未知但在飛任務（保守擋下）", True,
     armed=True, autopilot=3, custom_mode=ARDU_AUTO, alt_rel=None)

print("\n── PX4 ────────────────────────────────────────────────")
case("armed、空中、正在飛任務", True,
     armed=True, autopilot=12, custom_mode=PX4_AUTO, alt_rel=42.0)
case("未 armed", False,
     armed=True, autopilot=12, custom_mode=PX4_AUTO, alt_rel=0.2)

print("\n── 反向驗證：擋下的理由要說得出替代路徑 ────────────────")
why = guard(armed=True, autopilot=3, custom_mode=ARDU_AUTO, alt_rel=42.0)
has_path = why and "hold" in why and "三步" in why
print(f"{'✓' if has_path else '✗'} 擋下的訊息包含合法替代路徑（切 hold → 上傳 → 切回）")
ok &= bool(has_path)

# **PX4 與 ArduPilot 的任務模式號本來就不同**：若守門誤用同一個數字，
# 這一格會露餡——拿 ArduPilot 的 AUTO 號碼給 PX4 的機，不該被判成飛任務
cross = guard(armed=True, autopilot=12, custom_mode=ARDU_AUTO, alt_rel=42.0)
print(f"{'✓' if not cross else '✗'} 方言沒有混用（ArduPilot 的模式號套到 PX4 不算飛任務）")
ok &= not cross

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
