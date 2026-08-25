#!/usr/bin/env python3
"""續飛計算的兩份實作必須給出**一模一樣**的答案。

`apps/command/app/change_route.py`（地面站，沒有代理的機的備援）與
`uav-agent/route.py`（機上，權威）是刻意的兩份相同實作——兩邊都要能算。
**兩份會漂移**，而漂移的症狀是「同一台機，有代理時飛去 A 點、代理掉線時飛去
B 點」，那種缺陷沒有錯誤訊息，只會在事後看軌跡時覺得哪裡怪怪的。

這支測試拿同一組輸入餵兩邊，斷言結果完全相同。改其中一個檔案就要改另一個，
而這裡會在忘記時說話。

用法：python3 scripts/test-route-equivalence.py
"""
import json
import sys
import types
from pathlib import Path


def load(name, path):
    """**直接讀原始碼執行，不走 import 機制。**

    第一版用 importlib 載，結果讀到的是 __pycache__ 裡的舊 .pyc——檔案早就
    改回去了，測試卻還在報漂移。一支專門用來偵測「兩份檔案不一致」的測試，
    自己卻沒有真的讀那兩個檔案，那它報的東西一律不可信。
    """
    src = Path(path).read_text(encoding="utf-8")
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    exec(compile(src, str(path), "exec"), mod.__dict__)
    return mod


GS = load("gs_route", Path("/home/k200/uav-system/apps/command/app/change_route.py"))
AIR = load("air_route", Path("/home/k200/uav-agent/route.py"))

LAT, LON = 25.0550, 121.5060


def wp(i, dlat=0.0, dlon=0.0, alt=30.0, command=16):
    return {"seq": i, "lat": LAT + dlat / 111000.0, "lon": LON + dlon / 101000.0,
            "alt": alt, "action": "waypoint", "command": command}


#: 每一組都對應一條規則，不是隨便湊的：排除項、水平距離、掉頭、下降、
#: 沒位置、0,0 哨兵、沒有可續飛點、長距離警告
CASES = [
    ("一般情況", [wp(0, -400), wp(1, 800)],
     {"lat": LAT, "lon": LON, "alt_rel": 20.0, "heading": 0.0}, 25.0),
    ("排除起飛/降落/返航", [wp(0, 0, command=22), wp(1, 0, command=21),
                            wp(2, 0, command=20), wp(3, 300)],
     {"lat": LAT, "lon": LON, "alt_rel": 20.0, "heading": 90.0}, 25.0),
    ("水平最近但低很多", [wp(0, 5, alt=2.0), wp(1, 40, alt=30.0)],
     {"lat": LAT, "lon": LON, "alt_rel": 30.0, "heading": 0.0}, 30.0),
    ("沒有位置", [wp(0, 100)],
     {"lat": None, "lon": None, "alt_rel": 20.0}, 25.0),
    ("0,0 哨兵", [wp(0, 100)],
     {"lat": 0.0, "lon": 0.0, "alt_rel": 20.0}, 25.0),
    ("沒有可續飛點", [wp(0, 0, command=21)],
     {"lat": LAT, "lon": LON, "alt_rel": 20.0}, None),
    ("長距離警告", [wp(0, 2000)],
     {"lat": LAT, "lon": LON, "alt_rel": 50.0, "heading": 0.0}, 50.0),
    ("沒有航向（不該生出掉頭警告）", [wp(0, -400)],
     {"lat": LAT, "lon": LON, "alt_rel": 20.0}, 25.0),
    ("沒有懸停高度", [wp(0, 100)],
     {"lat": LAT, "lon": LON, "alt_rel": 20.0, "heading": 0.0}, None),
]

ok = True
for label, wps, cur, hold in CASES:
    a = GS.build_proposal(wps=wps, cur=dict(cur), hold_alt=hold,
                          mission_name="X", mission_id="m1")
    b = AIR.build_proposal(wps=wps, cur=dict(cur), hold_alt=hold,
                           mission_name="X", mission_id="m1")
    same = json.dumps(a, sort_keys=True, ensure_ascii=False) == \
        json.dumps(b, sort_keys=True, ensure_ascii=False)
    ok &= same
    print(f"{'✓' if same else '✗'} {label}")
    if not same:
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                print(f"    {k}:\n      地面 {a.get(k)}\n      機上 {b.get(k)}")

print("\n── 過期判定也要一致 ───────────────────────────────────")
old_p = GS.build_proposal(wps=[wp(0, 100), wp(1, 900)],
                          cur={"lat": LAT, "lon": LON, "alt_rel": 20.0},
                          hold_alt=None, mission_name="X", mission_id="m1")
for label, dlat in (("飄一點點", 5.0), ("飄過門檻", 900.0)):
    new_p = GS.build_proposal(wps=[wp(0, 100), wp(1, 900)],
                              cur={"lat": LAT + dlat / 111000.0, "lon": LON,
                                   "alt_rel": 20.0},
                              hold_alt=None, mission_name="X", mission_id="m1")
    same = GS.drift_reason(old_p, new_p) == AIR.drift_reason(old_p, new_p)
    ok &= same
    print(f"{'✓' if same else '✗'} {label}｜{GS.drift_reason(old_p, new_p)}")

print("\n── 門檻值不能各自為政 ─────────────────────────────────")
for k in ("DRIFT_M", "DRIFT_WP", "_EXCLUDE", "_NAV"):
    same = getattr(GS, k) == getattr(AIR, k)
    ok &= same
    print(f"{'✓' if same else '✗'} {k}｜地面 {getattr(GS, k)} / 機上 {getattr(AIR, k)}")

print("\n" + ("兩份實作等價" if ok else "**兩份實作已經漂移**"))
sys.exit(0 if ok else 1)
