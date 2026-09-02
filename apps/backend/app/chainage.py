"""沿路徑弧長投影與前後對照（issues/027；ui-spec §6b.5 的契約形狀）。

**與前端 `lib/chainage.ts` 是同一套演算法的後端版**——搬過來的目的是讓
UI 與未來的 `compare_flights` 共用同一份邏輯，**不養兩份**（issues/019）。

## 為什麼是弧長不是時間

兩趟的速度不同，**時間對齊會把「同一地點」錯位成不同的 X**。投影＝把樣本打到
共同的參考路徑上，兩趟才有共同的 X 軸。

## 三條誠實原則（每一條都對應一種會產生假結論的失效）

1. **參考路徑是計畫航點，不是任一趟實飛軌跡。** 拿其中一趟當基準，
   等於讓那趟的偏航變成零誤差，比較就失去意義。沒有計畫航點時退回用 A 那趟，
   **但回應要說出用的是哪一種**。
2. **投影距離有上限**（預設 60 m）：超過的樣本**捨棄並計數**。
   硬塞偏航樣本到某個里程，數字看起來完全正常、**沒有任何線索說它是垃圾**。
3. **格寬自適應，而且回應必須說出用了多少。** 固定格寬在稀疏架次上會產生
   **假結論**：實測兩趟各約 10 筆、路徑 300 m，10 m 格時兩趟幾乎不落同格、
   對照欄全空，畫面顯示「沒有差異」——**真相是格子太細**。

> 通則（與 readiness 四態、EKF 位元範圍同一條線）：
> **方法參數必須可見，否則結論無法被檢驗。** 第 3 點的失效型態是
> **把方法產物誤報成研究結論**，比算錯數字更隱蔽——使用者會據此下
> 「改善無效」的判斷。
"""
import math

#: 一緯度的公尺數（球面近似；本用途的量級是數百公尺，誤差可忽略）
M_LAT = 110574.0
#: 預設投影距離上限。與前端客端版同值——**兩邊不同會讓同一份資料算出不同答案**
DEFAULT_MAX_OFFSET_M = 60.0
#: 格寬下限。再細下去就是在放大取樣噪音
MIN_GRID_M = 10.0


def m_lon(lat: float) -> float:
    return 111320.0 * math.cos(math.radians(lat))


def auto_grid(total_m: float, n_a: int, n_b: int) -> float:
    """依**較稀那趟**的平均樣本間距推格寬（×2，下限 10 m，取 5 的倍數）。

    用較稀的那趟：格子要大到連稀的那趟都落得進去，否則對照欄還是空的。
    **加大格寬是聚合選擇（樣本仍是真的），與插值（無中生有）不同。**
    """
    n = max(1, min(n_a, n_b))
    return max(MIN_GRID_M, round((total_m / n) * 2 / 5) * 5.0)


def _to_xy(pts: list[dict], origin: dict) -> list[tuple[float, float]]:
    k = m_lon(origin["lat"])
    return [((p["lon"] - origin["lon"]) * k,
             (p["lat"] - origin["lat"]) * M_LAT) for p in pts]


def _project(px: float, py: float, ref: list[tuple[float, float]],
             cum: list[float], max_off: float) -> float | None:
    """樣本 → 參考路徑里程。回傳 None＝離路徑太遠（**不硬塞**）。"""
    best, best_d = None, math.inf
    for i in range(len(ref) - 1):
        ax, ay = ref[i]
        bx, by = ref[i + 1]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        if len2 == 0:
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        if d < best_d:
            best_d, best = d, cum[i] + t * math.sqrt(len2)
    return best if best_d <= max_off else None


def _pct(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    i = int(q * (len(sorted_vals) - 1))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, i))]


def summarize(vals: list) -> dict:
    """**p5 是重點不是裝飾**：平均掉 2 dB 無感、最差掉 10 dB 就斷鏈。
    尾部才是鏈路品質的關鍵。"""
    v = sorted(x for x in vals if x is not None and math.isfinite(x))
    return {"mean": (sum(v) / len(v)) if v else None,
            "p50": _pct(v, 0.5), "p5": _pct(v, 0.05), "n": len(v)}


def compare_along_path(a: list[dict], b: list[dict],
                       ref_path: list[dict] | None,
                       grid_size: float | None = None,
                       max_offset_m: float = DEFAULT_MAX_OFFSET_M) -> dict:
    """兩趟 → 沿路徑對照。回傳形狀與前端客端版一致（ui-spec §6b.5）。

    `a`／`b`／`ref_path` 的元素要有 `lat`／`lon`；樣本另可有 `sinr`／`rsrp`。
    """
    ref_src = "plan"
    ref = [p for p in (ref_path or []) if p.get("lat") is not None
           and p.get("lon") is not None]
    if len(ref) < 2:
        # 退回用 A 那趟。**這件事要說出來**——它讓 A 的偏航變成零誤差
        ref = [p for p in a if p.get("lat") is not None and p.get("lon") is not None]
        ref_src = "trip_a"
    if len(ref) < 2:
        return {"chainage": [], "total_m": 0.0, "grid_size_used": grid_size or MIN_GRID_M,
                "grid_size_source": "requested" if grid_size else "floor",
                "reference": "none", "max_offset_m": max_offset_m,
                "dropped": {"a": 0, "b": 0},
                "summary": {k: summarize([]) for k in
                            ("a_sinr", "b_sinr", "a_rsrp", "b_rsrp")},
                "note": "沒有足夠的參考路徑點（至少要兩點）——無法投影"}

    origin = ref[0]
    rxy = _to_xy(ref, origin)
    cum = [0.0]
    for i in range(1, len(rxy)):
        cum.append(cum[i - 1] + math.hypot(rxy[i][0] - rxy[i - 1][0],
                                           rxy[i][1] - rxy[i - 1][1]))
    total_m = cum[-1]
    k = m_lon(origin["lat"])
    grid_src = "requested" if grid_size else "auto"
    bw = grid_size or auto_grid(total_m, len(a), len(b))

    bins: dict[int, dict] = {}
    dropped = {"a": 0, "b": 0}

    def put(rows, side):
        for r in rows:
            if r.get("lat") is None or r.get("lon") is None:
                continue
            m = _project((r["lon"] - origin["lon"]) * k,
                         (r["lat"] - origin["lat"]) * M_LAT, rxy, cum, max_offset_m)
            if m is None:
                dropped[side] += 1
                continue
            slot = bins.setdefault(int(m // bw),
                                   {"a": [], "b": [], "ar": [], "br": []})
            if r.get("sinr") is not None:
                slot[side].append(r["sinr"])
            if r.get("rsrp") is not None:
                slot["ar" if side == "a" else "br"].append(r["rsrp"])

    put(a, "a")
    put(b, "b")

    def avg(v):
        return (sum(v) / len(v)) if v else None

    chainage = [{"m": (key + 0.5) * bw,
                 "a_sinr": avg(s["a"]), "b_sinr": avg(s["b"]),
                 "a_rsrp": avg(s["ar"]), "b_rsrp": avg(s["br"]),
                 "a_n": len(s["a"]), "b_n": len(s["b"])}
                for key, s in sorted(bins.items())]

    # **兩趟都有樣本的格子有幾個**：這個數字才回答「這份對照有沒有意義」。
    # 它是 0 而 chainage 很長時，畫面會顯示「沒有差異」而真相是格子太細
    paired = sum(1 for c in chainage if c["a_n"] and c["b_n"])

    return {
        "chainage": chainage,
        "total_m": total_m,
        # ── 方法參數：**一律回傳，讓結論可以被檢驗** ──────────────
        "grid_size_used": bw,
        "grid_size_source": grid_src,      # requested / auto
        "reference": ref_src,              # plan / trip_a
        "max_offset_m": max_offset_m,
        "dropped": dropped,                # 離參考路徑太遠而未納入
        "paired_cells": paired,
        "summary": {
            "a_sinr": summarize([r.get("sinr") for r in a]),
            "b_sinr": summarize([r.get("sinr") for r in b]),
            "a_rsrp": summarize([r.get("rsrp") for r in a]),
            "b_rsrp": summarize([r.get("rsrp") for r in b]),
        },
    }
