#!/usr/bin/env python3
"""重新執行要先飛到起始點，繼續執行不可以（issues/060）。

056 讓「一鍵起飛」會先飛到任務起始點，但 `_fly_to_start` 全檔只有那一個呼叫點
——機已經在空中、要再跑一次同一條路徑時完全不經過，而 ArduCopter 一進 AUTO
就從最近的下一個航點開始，**航線第一段永遠沒被飛到**。

這支測的是那個修法的兩半，而**兩半互為對方的反例**：

  * 重新執行（預設）：一定要有 transit，而且要從頭跑（`MISSION_START` param1=0）
  * 繼續執行（`resume: true`）：**一定不可以**有 transit，也不可以送
    `MISSION_START`——那會把序號歸零，正是「繼續」最不該發生的事

不先把這兩個分開就加 fly-to-start，任務中斷後想從第 7 點續會被變成
「飛回第 1 點重來」——在空中那是一段沒有人預期的航程。

跑法（不需要服務、不需要資料庫）：
    python3 scripts/test-mission-start-transit.py
"""
import ast
import sys
from pathlib import Path

SRC = Path("/home/k200/uav-system/apps/command/app/main.py")
tree = ast.parse(SRC.read_text(encoding="utf-8"))
src = SRC.read_text(encoding="utf-8")
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n
    return None


def calls(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.append(f.id if isinstance(f, ast.Name)
                       else getattr(f, "attr", ""))
    return out


def consts(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)]


print("— 進任務的入口都要經過 transit —")
ms, mf, ts = fn("mission_start"), fn("mission_fly"), fn("_transit_step")
chk("`_transit_step` 存在", ts is not None)
chk("`mission_start` 會走 transit", "_transit_step" in calls(ms),
    "056 之前它只送 MISSION_START，不飛起始點")
chk("`mission_fly` 仍然走 `_fly_to_start`", "_fly_to_start" in calls(mf),
    "它的 leg 算在解鎖前，刻意不共用 _transit_step")
chk("兩者共用同一組判斷", all(
    c in calls(ts) for c in ("_start_leg", "_check_start_leg", "_fly_to_start")),
    "起始點／距離關卡／飛過去三件事只有一份")

print("\n— 高度：以 plan 的起始點高度為絕對主導 —")
sl = fn("_start_leg")
chk("`_start_leg` 取航線的起飛項高度", "takeoff_alt" in calls(sl))
chk("**不拿機當下的高度去比**",
    not any(x in src[sl.lineno:sl.end_lineno] for x in ("max(", "alt_rel")),
    "不取兩者較高、不維持當前高度——同一航線從哪裡重跑都走同一個高度")

print("\n— 重新 vs 繼續 —")
body = fn("MissionStartIn") or next(
    (n for n in ast.walk(tree)
     if isinstance(n, ast.ClassDef) and n.name == "MissionStartIn"), None)
chk("有 `MissionStartIn` 這個 body", body is not None)
fields = [t.target.id for t in ast.walk(body) if isinstance(t, ast.AnnAssign)] if body else []
chk("帶 `resume` 欄位", "resume" in fields)
chk("**預設是重新執行**",
    any(isinstance(t, ast.AnnAssign) and getattr(t.target, "id", "") == "resume"
        and isinstance(t.value, ast.Constant) and t.value.value is False
        for t in ast.walk(body)),
    "主要用途是同一條路徑飛多趟")

seg = src[src.index("async def mission_start"):src.index("async def mission_start") + 3000]
resume_blk = seg[seg.index("if body.resume:"):seg.index("else:", seg.index("if body.resume:"))]
chk("繼續那一支**不呼叫 transit**", "_transit_step" not in resume_blk,
    "接上中斷處，不是飛回起點")
chk("繼續那一支**不送 MISSION_START**", "300" not in resume_blk,
    "param1=0 會把序號歸零——「繼續」最不該發生的事")
chk("繼續那一支只切模式", "job_set_mode" in resume_blk)
chk("守門動作跟著分", "resume" in seg[:seg.index("steps")] and "start_mission" in seg,
    "HOLDING 只允許 resume，地面狀態只允許 start_mission")

print("\n— 留痕要看得出 transit 有沒有跑（2026-09-21 卡在這裡）—")
chk("`_clip` 取代了寫死的 detail[:500]", "detail[:500]" not in src)
chk("截斷時會說出來", "留痕截斷" in src,
    "不說的話事後讀的人會以為那就是全部")
chk("上限夠放得下 transit", "AUDIT_DETAIL_MAX = 20000" in src)

print("\n" + ("✓ 全部通過" if ok else "✗ 有失敗"))
sys.exit(0 if ok else 1)
