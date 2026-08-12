#!/usr/bin/env python3
"""B2 搬遷等價測試：新驅動 vs 舊實作，**逐一比對輸出**（issue 026 B2）。

**為什麼要有這個測試**：B2 是搬遷不是改寫，所以唯一的驗收標準是「新舊輸出完全
相同」。端到端抽查（開一台看模式對不對）只能證明「常見情況沒壞」——真正會出事
的是邊角（未知模式號、0、超出表的值、ctx 缺欄位），而那些正是人工抽查不會碰的。

本測試在**切換呼叫端之前**跑：新舊兩份實作同時存在時比對，過了才動 call site。
順序反過來（先切再驗）的話，發現不一致時已經在線上了。

跑法（主機，不需容器；三邊都是純 stdlib）：
    python3 scripts/test-driver-equivalence.py
"""
import ast
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "libs"))

import autopilot  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _literal_from_source(path, varname):
    """從原始碼取字面量常數，**不 import 該模組**。

    `apps/command/app/mav.py` 會 import pymavlink 與起執行緒，主機上不一定裝得起來，
    而我們只需要它的方言表。用 ast 取值＝不執行任何程式碼。
    """
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == varname:
                    return ast.literal_eval(node.value)
    raise KeyError(f"{path} 裡找不到 {varname}")


def main():
    fails = []
    old_dialect = _load(ROOT / "apps/backend/app/dialect.py", "old_dialect")
    old_caps = _load(ROOT / "apps/command/app/capabilities.py", "old_caps")
    px4_modes = _literal_from_source(ROOT / "apps/command/app/mav.py", "PX4_MODES")
    ardu_modes = _literal_from_source(ROOT / "apps/command/app/mav.py", "ARDU_COPTER_MODES")

    # ── 1. decode_mode：掃過整個會出現的值域，不是抽幾個 ────────────
    #    PX4 的 custom_mode 是 main<<16|sub<<24，兩個位元組各掃滿。
    for main in range(0, 16):
        for sub in range(0, 16):
            cm = (main << 16) | (sub << 24)
            for raw, drv in ((12, autopilot.get_driver(12)),
                             (None, autopilot.get_driver(None))):
                want = old_dialect.mode_name(cm, raw)
                got = drv.decode_mode(cm)
                if want != got:
                    fails.append(f"decode_mode(cm={cm:#x}, ap={raw}) 舊={want!r} 新={got!r}")
    for cm in list(range(0, 40)) + [99, 255, 1000]:
        want = old_dialect.mode_name(cm, 3)
        got = autopilot.get_driver(3).decode_mode(cm)
        if want != got:
            fails.append(f"decode_mode(cm={cm}, ardupilot) 舊={want!r} 新={got!r}")

    # ── 2. autopilot_name ───────────────────────────────────────────
    for raw in (12, 3, 0, 1, None, 99):
        if old_dialect.autopilot_name(raw) != autopilot.autopilot_name(raw):
            fails.append(f"autopilot_name({raw}) 不一致")
        if old_caps.autopilot_name(raw) != autopilot.autopilot_name(raw):
            fails.append(f"autopilot_name({raw}) 與 command 端不一致")

    # ── 3. 模式編碼表：新驅動必須與 mav.py 的方言表逐鍵相同 ─────────
    d4, d3 = autopilot.get_driver(12), autopilot.get_driver(3)
    if d4.modes != px4_modes:
        fails.append(f"PX4 模式表不一致：舊={px4_modes} 新={d4.modes}")
    if d3.modes != ardu_modes:
        fails.append(f"ArduPilot 模式表不一致：舊={ardu_modes} 新={d3.modes}")
    for name, val in px4_modes.items():
        if d4.encode_mode(name) != val:
            fails.append(f"PX4 encode_mode({name!r}) 不一致")
        cm = (val[0] << 16) | (val[1] << 24)
        if not d4.mode_matches(cm, name):
            fails.append(f"PX4 mode_matches({name!r}) 對自己的編碼竟然不成立")
    for name, val in ardu_modes.items():
        if d3.encode_mode(name) != (val, 0):
            fails.append(f"ArduPilot encode_mode({name!r}) 不一致")
        if not d3.mode_matches(val, name):
            fails.append(f"ArduPilot mode_matches({name!r}) 對自己的編碼竟然不成立")

    # ── 4. capabilities：含 ctx 的各種狀態，逐鍵比對值與原因文字 ────
    ctxs = [None, {}, {"sysid_mygcs": 254}, {"sysid_mygcs": 255},
            {"sysid_mygcs": 1}, {"sysid_mygcs": None}]
    for raw, ap in ((12, "px4"), (3, "ardupilot"), (0, "unknown"), (None, "unknown")):
        drv = autopilot.get_driver(raw)
        for ctx in ctxs:
            w_caps, w_reasons = old_caps.capabilities_for(ap, ctx)
            g_caps, g_reasons = drv.capabilities(ctx)
            if w_caps != g_caps:
                diff = {k: (w_caps.get(k), g_caps.get(k))
                        for k in set(w_caps) | set(g_caps) if w_caps.get(k) != g_caps.get(k)}
                fails.append(f"capabilities({ap}, ctx={ctx}) 值不一致：{diff}")
            if w_reasons != g_reasons:
                diff = {k: (w_reasons.get(k), g_reasons.get(k))
                        for k in set(w_reasons) | set(g_reasons)
                        if w_reasons.get(k) != g_reasons.get(k)}
                fails.append(f"capabilities({ap}, ctx={ctx}) **原因文字**不一致：{diff}")

    # ── 5. 串流策略：只有 ArduPilot 要主動要求 ──────────────────────
    for raw in (12, 3, None, 99):
        old_needs = old_dialect.needs_stream_request(raw)
        new_needs = bool(autopilot.get_driver(raw).on_connect())
        if old_needs != new_needs:
            fails.append(f"on_connect 與舊 needs_stream_request({raw}) 不一致")

    # ── 6. 起飛方言：相對 vs 絕對、空白參數、GUIDED 前置 ────────────
    p = autopilot.get_driver(12).takeoff_plan(10.0, 500.0)
    if p["param7"] != 510.0 or p["alt_semantics"] != "amsl" or p["needs_guided"]:
        fails.append(f"PX4 takeoff_plan 語意錯：{p}")
    if p["blank"] == p["blank"]:          # NaN != NaN
        fails.append("PX4 的空白參數必須是 NaN")
    try:
        autopilot.get_driver(12).takeoff_plan(10.0, None)
        fails.append("PX4 少了地面海拔竟然沒有拒絕")
    except ValueError:
        pass
    a = autopilot.get_driver(3).takeoff_plan(10.0, None)
    if a["param7"] != 10.0 or a["alt_semantics"] != "relative" or not a["needs_guided"]:
        fails.append(f"ArduPilot takeoff_plan 語意錯：{a}")
    if a["blank"] != 0.0:
        fails.append("ArduPilot 的空白參數必須是 0.0（NaN 會被靜默丟棄）")

    # ── 7. 任務線序：ArduPilot home 佔 seq 0，PX4 不動 ──────────────
    items = [{"seq": 0, "command": 16, "x": 1, "y": 2, "z": 3},
             {"seq": 1, "command": 16, "x": 4, "y": 5, "z": 6}]
    if autopilot.get_driver(12).mission_line(items) != items:
        fails.append("PX4 不該改動任務線序")
    line = autopilot.get_driver(3).mission_line(items)
    if len(line) != 3 or [i["seq"] for i in line] != [0, 1, 2]:
        fails.append(f"ArduPilot 線序錯：{[i['seq'] for i in line]}")
    if line[0]["x"] != 1 or line[0]["command"] != 16:
        fails.append("ArduPilot 的 home 應複製首點座標、command=16")

    # ── 8. 就緒訊號：ArduPilot 沒有 PREARM，要如實缺席 ──────────────
    if "prearm" in autopilot.get_driver(3).readiness_signals():
        fails.append("ArduPilot 不回報 PREARM，不該宣稱有這個訊號")
    if "prearm" not in autopilot.get_driver(12).readiness_signals():
        fails.append("PX4 有 PREARM_CHECK，不該漏")

    # ── 9. 等價宣告一定要帶適用範圍（B1 規格） ──────────────────────
    for raw in (12, 3):
        for eq in autopilot.get_driver(raw).MESSAGE_ADJUSTMENTS:
            if not eq.safe_field_bits:
                fails.append(f"{eq.src_type}→{eq.dst_type} 的等價宣告沒有適用範圍")
            if not eq.note:
                fails.append(f"{eq.src_type}→{eq.dst_type} 沒說明範圍外為何不成立")

    if fails:
        print("驅動等價測試 **失敗**（B2 搬遷改變了行為）：")
        for f in fails[:40]:
            print("  ✗ " + f)
        if len(fails) > 40:
            print(f"  …另有 {len(fails) - 40} 項")
        return 1
    print("驅動等價測試 OK：模式解讀 512+ 組、能力 4 廠牌 × 6 ctx 逐鍵含原因文字、"
          "模式表逐鍵、起飛/線序/就緒/等價宣告皆與舊實作相同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
