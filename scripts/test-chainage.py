#!/usr/bin/env python3
"""弧長投影：方法參數必須可見，否則結論無法被檢驗（issues/027）。

**這支測試的重點不是「算得對不對」，是「算錯的時候看不看得出來」。**
027 記的三個要求各對應一種會產生**假結論**的失效：

  1. 參考路徑用某一趟 → 那趟的偏航變成零誤差，比較失去意義
  2. 偏航樣本硬塞進某個里程 → 數字看起來完全正常，沒有線索說它是垃圾
  3. 固定格寬套在稀疏架次 → 兩趟不落同格、對照欄全空，
     **畫面顯示「沒有差異」而真相是格子太細**

第 3 個最隱蔽，所以本測試用**實測過的那個情境**（兩趟各約 10 筆、路徑 300 m）
直接重現它。

跑法（不需要服務、不需要資料庫）：
    python3 scripts/test-chainage.py
"""
import sys

sys.path.insert(0, "/home/k200/uav-system/apps/backend")

from app import chainage as C  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


# 一條 300 m 的直線航線（南北向），起點台北附近
LAT0, LON0 = 25.0550, 121.5060
DLAT = 300 / C.M_LAT
PLAN = [{"lat": LAT0, "lon": LON0}, {"lat": LAT0 + DLAT, "lon": LON0}]


def trip(n, sinr, offset_m=0.0, phase=0.0):
    """沿線 n 個等距樣本。

    `offset_m`＝整趟**側偏**幾公尺（測投影上限）。
    `phase`＝沿路徑**前後錯開**的比例（0–1，測格寬）。**兩趟各飛各的，
    取樣點不會落在同一個位置**——這正是固定格寬會失效的原因，
    所以測試必須把它做出來，否則測到的是一個不存在的情境。
    """
    k = C.m_lon(LAT0)
    step = 1.0 / (n - 1)
    return [{"lat": LAT0 + DLAT * min(1.0, i * step + phase * step),
             "lon": LON0 + offset_m / k,
             "sinr": sinr, "rsrp": -90.0} for i in range(n)]


print("── 1. 稀疏架次：固定 10 m 格會做出「沒有差異」的假結論 ──")
# **B 沿路徑錯開半個取樣間距**——兩趟各飛各的，本來就不會取在同一點
a, b = trip(10, 20.0), trip(10, 12.0, phase=0.5)
fixed = C.compare_along_path(a, b, PLAN, grid_size=10.0)
auto = C.compare_along_path(a, b, PLAN)
# 用「幾乎沒有」而不是「等於 0」：偶爾會有一格湊巧對上，
# 而斷言寫死 0 會讓這支測試對無關的微小改動也變紅
chk("固定 10 m：**兩趟幾乎不落同格，對照欄是空的**",
    fixed["paired_cells"] <= 1,
    f"{fixed['paired_cells']}/{len(fixed['chainage'])} 格有對照"
    "——畫面會顯示「沒有差異」，而真相是格子太細")
chk("**自適應之後兩趟落進同一格**（真的比得出來）",
    auto["paired_cells"] > fixed["paired_cells"],
    f"自適應 {auto['paired_cells']} vs 固定 {fixed['paired_cells']}")
chk("格寬有隨樣本密度放大", auto["grid_size_used"] > 10.0,
    f"{auto['grid_size_used']} m")

print("\n── 2. 方法參數一律回傳（結論才可以被檢驗）──────────────")
for key in ("grid_size_used", "grid_size_source", "reference",
            "max_offset_m", "dropped", "paired_cells"):
    chk(f"回應帶 {key}", key in auto, auto.get(key))
chk("自適應時 grid_size_source 說得出是 auto",
    auto["grid_size_source"] == "auto")
chk("指定時說得出是 requested",
    fixed["grid_size_source"] == "requested")

print("\n── 3. 參考路徑是計畫航點；沒有時要說出退回用了哪一趟 ───")
chk("有計畫航點 → reference=plan", auto["reference"] == "plan")
no_plan = C.compare_along_path(a, b, None)
chk("**沒有計畫航點 → reference=trip_a**（不是靜靜用 A 當基準）",
    no_plan["reference"] == "trip_a")

print("\n── 4. 偏航樣本捨棄並計數，不硬塞 ───────────────────────")
far = trip(10, 20.0, offset_m=200.0)     # 整趟側偏 200 m，遠超 60 m 上限
# 註：側偏用經度位移，投影距離＝側偏量本身（路徑是南北向）
off = C.compare_along_path(far, b, PLAN)
chk("全部被捨棄", off["dropped"]["a"] == 10, off["dropped"])
chk("**捨棄有計數**（不是靜靜消失）", off["dropped"]["a"] > 0)
chk("而 b 那趟沒有被誤殺", off["dropped"]["b"] == 0)
near = C.compare_along_path(trip(10, 20.0, offset_m=30.0), b, PLAN)
chk("**反向驗證**：30 m 側偏在 60 m 上限內，不該被丟",
    near["dropped"]["a"] == 0, near["dropped"])

print("\n── 5. summary 的 p5：尾部才是鏈路品質的關鍵 ────────────")
vals = [{"lat": LAT0, "lon": LON0, "sinr": v} for v in
        [20, 20, 20, 20, 20, 20, 20, 20, 20, 2]]
r = C.compare_along_path(vals, b, PLAN)
s = r["summary"]["a_sinr"]
chk("平均看起來還好、p5 抓得到那個 2",
    s["mean"] > 15 and s["p5"] == 2, f"mean={s['mean']:.1f} p5={s['p5']}")
chk("n 有附（誠實原則）", s["n"] == 10)

print("\n── 6. 反向驗證：參考路徑不足兩點時不假裝算得出來 ────────")
bad = C.compare_along_path([], [], None)
chk("回 reference=none 並說明", bad["reference"] == "none" and "note" in bad,
    bad.get("note"))

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
