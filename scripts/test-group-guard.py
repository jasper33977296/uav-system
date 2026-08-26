#!/usr/bin/env python3
"""群組執行器的每個下指令點都要經過守門。**不連飛機、不送指令。**

**為什麼要專門測這個**：守門原本只裝在單機路徑上，群組那條直接呼叫底層
MAVLink——同一個危險操作，用單機介面會被擋、用群組介面直接執行，而多機的
風險本來就更高（一次動好幾台）。這種漏洞的特徵是**不會有任何錯誤訊息**：
指令成功送出、飛機照做，只是沒有人問過「現在能不能做」。

兩件事要釘住：
  1. 每個 `_submit_audited` 呼叫都先過 `ask_guard`
  2. 群組用的動作名都在守門的意圖對映表裡——**名字漏一個就等於沒守到那個
     動作**，而且是安靜地跳過

用法：docker compose exec -T uav-command python /tmp/gg.py
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, "/srv")

from app import group_exec, guard_client  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


print("── 每個下指令點都經過同一個閘 ─────────────────────────")
src = Path("/srv/app/group_exec.py").read_text(encoding="utf-8")
tree = ast.parse(src)
# 直接呼叫 self._submit(...) 而不經 _submit_audited ＝ 繞過守門
direct = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "_submit" and \
            isinstance(f.value, ast.Name) and f.value.id == "self":
        direct.append(node.lineno)
# _submit_audited 自己內部呼叫 _submit 是正常的（守門就在它前面）。
# **用函式的 lineno..end_lineno 區間判斷**：第一版自己拼行號集合，
# 結果把區間內的呼叫誤報成繞過——斷言錯了比沒有斷言更糟，它會讓人去改
# 一段本來就對的程式
spans = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
         and n.name == "_submit_audited"]
outside = [ln for ln in direct
           if not any(a <= ln <= b for a, b in spans)]
chk("沒有繞過 _submit_audited 的直接呼叫", not outside,
    f"第 {outside} 行" if outside else "")

has_guard = "guard_client.ask_guard" in src
chk("_submit_audited 內有守門呼叫", has_guard)

print("\n── 群組用的動作名都在意圖對映表裡 ─────────────────────")
# 從原始碼抓出所有傳給 _submit_audited 的 action 字串
actions = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "_submit_audited":
        for a in node.args[1:2]:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                actions.add(a.value)
print("   群組會下的動作：", sorted(actions))
missing = sorted(a for a in actions if a not in guard_client._INTENT_OF)
chk("每個動作都對得到一個意圖", not missing,
    f"漏了 {missing}——這些會安靜地跳過守門" if missing else "")

print("\n── **反向驗證**：對映表不是把所有東西都對到同一個意圖 ──")
mapped = {guard_client._INTENT_OF[a] for a in actions
          if a in guard_client._INTENT_OF}
chk("群組動作對到不只一種意圖", len(mapped) > 1, sorted(mapped))

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
