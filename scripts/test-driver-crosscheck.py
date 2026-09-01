#!/usr/bin/env python3
"""驅動搬家的執行期護欄：機上算的與地面站算的不一致要報（issues/026 §9 B4-a）。

**搬家不是改寫，唯一的驗收標準是「新舊輸出完全相同」。** 而兩份實作並存的
這段期間，正是唯一能做這個比對的窗口——地面站那份刪掉之後就沒有對照組了。
所以這個比對不只在測試裡做，而是**執行期就在做**，本測試驗的是那道護欄。

四件事：
  1. 一致時**不報**（否則事件流會被淹掉，而淹掉等於沒有）
  2. 不一致要報，但**要持續超過門檻**——模式切換的當下兩邊必然短暫不一致
  3. **缺欄位＝代理沒說，不是不一致**（舊版代理不送這些欄位）
  4. 地面站不認得的廠牌**不比**（沒有意見時不要製造意見）

不需要真代理也不需要飛機：直接對 crosscheck() 餵狀態。
"""
import sys
import time

sys.path.insert(0, "/home/k200/uav-system/apps/backend")

from app import agent_link  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def link_with(derived):
    l = agent_link.AgentLink(board_uid="crosscheck-test")
    l.payload = {"derived": derived}
    return l


GROUND = {"mode_verb": "mission", "mode_name": "AUTO.MISSION", "ready": True}

print("── 1. 一致就不報 ─────────────────────────────────────")
l = link_with({"mode_verb": "mission", "mode_name": "AUTO.MISSION", "ready": True})
chk("完全一致 → 無事件", agent_link.crosscheck(l, GROUND) == [])

print("\n── 2. 不一致要持續超過門檻才報 ───────────────────────")
l = link_with({"mode_verb": "hold", "mode_name": "AUTO.MISSION", "ready": True})
first = agent_link.crosscheck(l, GROUND)
chk("**剛出現時不報**（模式切換的當下兩邊必然短暫不一致）", first == [], first)
# 把第一次看到的時刻往回撥，模擬已經持續了門檻以上
for k in l.disagree_since:
    l.disagree_since[k] -= agent_link.DISAGREE_HOLD_S + 0.1
held = agent_link.crosscheck(l, GROUND)
chk("持續超過門檻 → 報", len(held) == 1, held)
chk("訊息說得出兩邊各算什麼",
    held and "'hold'" in held[0] and "'mission'" in held[0], held[0] if held else "")

print("\n── 3. 恢復一致就清掉，不會殘留 ───────────────────────")
l.payload = {"derived": {"mode_verb": "mission", "mode_name": "AUTO.MISSION",
                         "ready": True}}
chk("回到一致 → 無事件且計時清掉",
    agent_link.crosscheck(l, GROUND) == [] and not l.disagree_since,
    l.disagree_since)

print("\n── 4. 缺欄位＝代理沒說，不是不一致 ───────────────────")
l = link_with({"mode_verb": "mission"})          # 舊版代理：只送 mode_verb
for _ in range(3):
    r = agent_link.crosscheck(l, GROUND)
    time.sleep(0.01)
chk("**舊版代理不會被報成不一致**", r == [], r)
l2 = link_with({})
chk("完全沒有 derived 也不報", agent_link.crosscheck(l2, GROUND) == [])

print("\n── 5. 地面站不認得的廠牌不比 ─────────────────────────")
l = link_with({"mode_verb": "hold", "mode_name": "SOMETHING", "ready": False})
for k in ("mode_verb",):
    pass
r1 = agent_link.crosscheck(l, None)
chk("ground=None（UnknownDriver）→ 不比", r1 == [], r1)
chk("**反向驗證**：同一份輸入在認得的廠牌下會累積計時",
    agent_link.crosscheck(l, GROUND) == [] and len(l.disagree_since) == 3,
    sorted(l.disagree_since))
# 不認得之後要把累積的計時清掉，否則換回認得時會立刻誤報
agent_link.crosscheck(l, None)
chk("轉為不認得時清掉累積的計時（免得之後誤報）", not l.disagree_since)

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
