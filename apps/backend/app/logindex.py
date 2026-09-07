"""錄製檔的摘要索引：**在網頁上打開一份 tlog，不必先下載再找工具**。

## 為什麼推翻原本的決定

`api.py` 的 `/captures` 原本寫著「tlog 與 QGC 回放、`mavlogdump.py` 相容——
所以取得檔案就是取得全部，不需要我們再做一套檢視器」。那句話在「要不要重做
一個回放器」上是對的，但它同時擋掉了一個**每次飛完都會問**的問題：

> 這份檔裡到底有什麼？機上有在送哪些訊息、頻率多少、那幾句警告是什麼時候
> 說的、模式什麼時候換的？

而回答它現在的代價是：下載 116 MB、裝 pymavlink、記得 `mavlogdump.py` 的參數。
**能力沒有缺，只是遠**（使用者 2026-09-07：「log 只能下載來看」）。

## 索引裡有什麼、沒有什麼

**有**：訊息型別 × 筆數 × 頻率 × 在檔案裡的分布、每個型別最後一筆的欄位值
（原樣不翻譯）、STATUSTEXT 全文、模式切換、幾條欄位曲線、出現過的 sysid。

**沒有**：逐幀瀏覽。那是另一件事，而且做得起（`mavlogdump.py` 就是），
這裡不假裝取代它——**索引是「這份檔長什麼樣」，不是「這份檔的全部」**。

## 三條紀律

1. **頻率是算出來的，不是宣稱的**：`(筆數−1) ÷ (首末間隔)`。一次性訊息
   （`MISSION_ACK`、`MISSION_COUNT`）沒有頻率，欄位留 `null`——
   **「還不知道」不是 0.0**（同前端訊息登錄表的規矩）。
2. **模式用驅動層解**（`libs/autopilot`）：ArduPilot 的 `custom_mode 0` 是
   STABILIZE、PX4 的 0 是「還沒設定」，拿其中一家的解法套另一家必錯。
   認不得的值原樣顯示 `MODE_<n>`，不猜。
3. **索引是快取，檔案是事實**：檔案長大了（今天的地面站 tlog 一直在寫）
   就如實說「索引只到這裡」，不假裝索引是完整的。

## 成本

實測：機上那份 1.8 MB／44,075 frames 約 0.5 秒；地面站今天那份 116 MB／
約 290 萬 frames 約 35 秒。**35 秒不能綁在一個請求上**，所以解析在背景跑，
端點先回 202＋進度，做完再回 200。索引寫在檔案旁邊（`<檔名>.index.json`），
重啟後照樣在。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: 密度條格數（前端畫成小長條圖）
BUCKETS = 60
#: 每條曲線最多幾點。**抽樣不平滑**——平滑會產生量測點之間沒有量到的值
SERIES_MAX = 400
#: 每個型別列幾個欄位（最後一筆）
FIELD_MAX = 18
#: STATUSTEXT **折疊後**的句數上限。同一句話重複只佔一列（同前端 foldEvents
#: 的規矩）——實測地面站整天那份檔裡 1,878 則 STATUSTEXT 其實只有幾十句話，
#: 其中 `PreArm: RC not found` 一句就重複兩千多次。硬截斷會把後半天的訊息
#: 整段丟掉，折疊不會。超過上限時仍然說出丟了幾句（不靜默）
TEXT_MAX = 300
#: 每一句最多記幾個發生時刻（密度條用）
TEXT_TIMES = 60
#: 分箱**自適應**：從 1 秒開始，某個型別的箱數超過上限就兩兩合併、寬度加倍。
#:
#: 原本寫死 10 秒，於是**短檔案的密度條只剩三根**（實測 24 秒的機上錄製）；
#: 而直接存每一筆的時刻，一份整天的檔案會是幾百萬個數字。自適應兩邊都成立：
#: 解析度永遠 ≥ 檔長 ÷ BIN_MAX，記憶體永遠 ≤ 型別數 × BIN_MAX。
BIN_S0 = 0.25
BIN_MAX = 2000

#: 曲線：名稱 → (訊息型別, 欄位, 倍率, 單位, 標題)
SERIES_SPEC: dict[str, tuple[str, str, float, str, str]] = {
    "alt_rel": ("GLOBAL_POSITION_INT", "relative_alt", 1e-3, "m", "高度（相對起飛點）"),
    "voltage": ("SYS_STATUS", "voltage_battery", 1e-3, "V", "電池電壓"),
    "sats": ("GPS_RAW_INT", "satellites_visible", 1.0, "顆", "GPS 衛星數"),
}
#: 振動要三軸取最大，不套上面那張表
VIBE_REFS = (30.0, 60.0)     # PX4／ArduPilot 共用的判讀門檻——有權威值才畫參考線


class _Thin:
    """有上限的抽樣器：超過 4×上限就抽掉一半、間隔加倍。

    **不做平均**。平均會生出一個沒有人量到的值，而這條線是拿來看形狀的。
    """

    def __init__(self, cap: int = SERIES_MAX):
        self.cap = cap
        self.step = 1
        self.i = 0
        self.pts: list[list[float]] = []

    def add(self, t: float, v: float) -> None:
        if math.isnan(v) or math.isinf(v):
            return                    # 沒有值的樣本不進曲線，也不佔抽樣位置
        if self.i % self.step == 0:
            self.pts.append([t, v])
            if len(self.pts) > self.cap * 4:
                self.pts = self.pts[::2]
                self.step *= 2
        self.i += 1

    def out(self, t0: float) -> list[list[float]]:
        pts = self.pts
        if len(pts) > self.cap:
            k = math.ceil(len(pts) / self.cap)
            pts = pts[::k]
        return [[round(t - t0, 2), round(v, 3)] for t, v in pts]


def _num(v: float) -> float | None:
    """NaN／inf → None。

    **MAVLink 真的會送 NaN**（`NAV_CONTROLLER_OUTPUT`、`WIND`、未填的欄位），
    而 NaN 不是合法 JSON——原本整支端點會 500，一份檔案就這樣打不開。
    轉成 `null` 是誠實的：那個欄位機上就是沒有給值。
    """
    return None if (math.isnan(v) or math.isinf(v)) else round(v, 5)


def _scalar(v: Any) -> Any:
    """一個欄位值 → 能放進 JSON 的東西。**不假裝它是別的型別。**

    MAVLink 的位元組欄位（`V2_EXTENSION.payload`、`FILE_TRANSFER_PROTOCOL`⋯）
    在 pymavlink 裡是 `bytearray`，JSON 序列化不了——**整支端點會 500，一份
    116 MB 的檔就這樣打不開**（實測）。可列印的當文字，否則給長度＋前 16 byte
    的 hex：那是原樣，不是翻譯。
    """
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        try:
            t = b.decode("utf8").rstrip("\x00")
            if t.isprintable():
                return t
        except UnicodeDecodeError:
            pass
        return f"<{len(b)} bytes> {b[:16].hex()}"
    if isinstance(v, float):
        return _num(v)
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_scalar(x) for x in list(v)[:6]]
    return str(v)          # 認不得的型別照樣看得到，不整支端點陪葬


def _fields(msg: Any) -> tuple[dict, dict]:
    """最後一筆的欄位值與單位。**原樣不翻譯**——線上單位配線上數值。"""
    out: dict[str, Any] = {}
    for k in msg.get_fieldnames()[:FIELD_MAX]:
        out[k] = _scalar(getattr(msg, k, None))
    units = {}
    by_name = getattr(type(msg), "fieldunits_by_name", None) or {}
    for k in out:
        if by_name.get(k):
            units[k] = by_name[k]
    return out, units


def build(path: Path, on_progress: Callable[[float], None] | None = None) -> dict:
    """解析一份 tlog，回索引 dict。**阻塞**——呼叫端負責丟到背景執行緒。"""
    from pymavlink import mavutil
    try:
        from autopilot import get_driver          # PYTHONPATH=/srv/libs
    except Exception:                             # pragma: no cover - 開發環境
        get_driver = None                         # type: ignore[assignment]

    st = path.stat()
    started = time.time()
    m = mavutil.mavlink_connection(str(path))

    counts: dict[str, int] = {}
    first: dict[str, float] = {}
    last: dict[str, float] = {}
    lastmsg: dict[str, Any] = {}
    bins: dict[str, dict[int, int]] = {}
    binw: dict[str, float] = {}
    sysids: dict[str, int] = {}
    # 折疊：鍵＝(嚴重度, 原句)。**原句不翻譯**——那是飛控真正說的話
    texts: dict[tuple[int, str], dict] = {}
    texts_dropped = 0
    # **模式要分 sysid。** 一份地面站 tlog 裡不只一台機的心跳（實測今天那份有
    # sysid 1 與 7），混在一起看會變成兩台機的模式互相交錯——實測 4,689 次
    # 「切換」，其實兩台各自只換了幾次
    modes: dict[str, list[dict]] = {}
    prev_mode: dict[str, str] = {}
    series = {k: _Thin() for k in SERIES_SPEC}
    vibe = _Thin()
    t0: float | None = None
    t1: float | None = None
    frames = 0

    while True:
        msg = m.recv_match(blocking=False)
        if msg is None:
            break
        ty = msg.get_type()
        if ty == "BAD_DATA":
            continue
        t = getattr(msg, "_timestamp", None)
        if t is None:
            continue
        frames += 1
        if t0 is None:
            t0 = t
        t1 = t
        counts[ty] = counts.get(ty, 0) + 1
        if ty not in first:
            first[ty] = t
        last[ty] = t
        lastmsg[ty] = msg
        d = bins.setdefault(ty, {})
        w = binw.setdefault(ty, BIN_S0)
        b = int((t - t0) / w)
        d[b] = d.get(b, 0) + 1
        if len(d) > BIN_MAX:                 # 太細了：兩兩合併、寬度加倍
            merged: dict[int, int] = {}
            for k, v in d.items():
                merged[k // 2] = merged.get(k // 2, 0) + v
            bins[ty] = merged
            binw[ty] = w * 2
        key = f"{msg.get_srcSystem()}.{msg.get_srcComponent()}"
        sysids[key] = sysids.get(key, 0) + 1

        if ty == "STATUSTEXT":
            k = (int(getattr(msg, "severity", 6)), str(msg.text))
            g = texts.get(k)
            if g is None:
                if len(texts) < TEXT_MAX:
                    texts[k] = {"n": 1, "first": t, "last": t, "times": [t]}
                else:
                    texts_dropped += 1
            else:
                g["n"] += 1
                g["last"] = t
                if len(g["times"]) < TEXT_TIMES:
                    g["times"].append(t)
        elif ty == "HEARTBEAT":
            # **GCS 的心跳不是飛機的模式。** autopilot=8（INVALID）＝地面站/
            # 攝影機之類的元件，拿它的 custom_mode 去解模式會解出垃圾
            ap = getattr(msg, "autopilot", 8)
            if ap != 8:
                cm = getattr(msg, "custom_mode", 0)
                name = (get_driver(ap).decode_mode(cm) if get_driver
                        else f"MODE_{cm}")
                if name != prev_mode.get(key):
                    modes.setdefault(key, []).append({"t": t, "mode": name})
                    prev_mode[key] = name
        elif ty == "VIBRATION":
            vibe.add(t, max(msg.vibration_x, msg.vibration_y, msg.vibration_z))
        for name, (mtype, field, scale, _u, _lab) in SERIES_SPEC.items():
            if ty == mtype:
                v = getattr(msg, field, None)
                if v is not None:
                    series[name].add(t, float(v) * scale)

        if on_progress and frames % 100_000 == 0:
            on_progress(float(getattr(m, "percent", 0.0)))

    if t0 is None or t1 is None:
        return {"file": path.name, "bytes": st.st_size, "frames": 0,
                "empty_reason": "這份檔裡沒有一筆帶時間戳的 MAVLink 訊息",
                "indexed_bytes": st.st_size, "mtime": st.st_mtime,
                "built_at": time.time(), "build_s": round(time.time() - started, 2)}

    span = max(t1 - t0, 1e-6)

    def rebin(ty: str) -> list[int]:
        out = [0] * BUCKETS
        w = binw.get(ty, BIN_S0)
        for b, n in bins[ty].items():
            i = min(BUCKETS - 1, int((b * w) / span * BUCKETS))
            out[i] += n
        return out

    types = []
    for ty, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        dur = last[ty] - first[ty]
        # **一次性訊息沒有頻率**——留 null，不寫 0.0（那會被讀成「都不送」）
        hz = round((n - 1) / dur, 2) if n > 1 and dur > 0.5 else None
        f, u = _fields(lastmsg[ty])
        types.append({"name": ty, "n": n, "hz": hz,
                      "first": round(first[ty] - t0, 1),
                      "last": round(last[ty] - t0, 1),
                      "fields": f, "units": u, "buckets": rebin(ty)})

    out = {
        "file": path.name,
        "bytes": st.st_size,
        "indexed_bytes": st.st_size,
        "mtime": st.st_mtime,
        "frames": frames,
        "span": round(span, 1),
        "t_start": t0,
        "t_end": t1,
        "types": types,
        "sysids": [{"id": k, "n": v} for k, v in
                   sorted(sysids.items(), key=lambda kv: -kv[1])],
        #: 主要來源＝訊息最多的那個。**畫面預設看它，但不隱藏其他的**
        "main_sys": (max(sysids.items(), key=lambda kv: kv[1])[0]
                     if sysids else None),
        # 折疊後依**首次**時間正序：讀一份紀錄是從頭讀到尾
        "statustext": [
            {"sev": k[0], "text": k[1], "n": g["n"],
             "t": round(g["first"] - t0, 1), "last": round(g["last"] - t0, 1),
             "unix": g["first"],
             "times": [round(x - t0, 1) for x in g["times"]]}
            for k, g in sorted(texts.items(), key=lambda kv: kv[1]["first"])],
        "statustext_dropped": texts_dropped,
        "statustext_total": sum(g["n"] for g in texts.values()) + texts_dropped,
        "modes": [{"sys": sysk, "t": round(x["t"] - t0, 1), "unix": x["t"],
                   "mode": x["mode"]}
                  for sysk, lst in modes.items() for x in lst],
        "series": [
            {"key": k, "label": lab, "unit": u, "points": series[k].out(t0)}
            for k, (_mt, _f, _sc, u, lab) in SERIES_SPEC.items()
            if series[k].pts
        ],
        "built_at": time.time(),
        "build_s": round(time.time() - started, 2),
    }
    if vibe.pts:
        out["series"].append({"key": "vibe", "label": "振動（三軸最大）",
                              "unit": "", "points": vibe.out(t0),
                              "refs": list(VIBE_REFS)})
    return out


# ── 快取與背景工作 ────────────────────────────────────────────────
#
# **索引是快取，檔案是事實。** 地面站那份 tlog 每天一檔、整天都在寫，所以
# 索引一定會過期——過期時如實說「索引只到這裡」，不重算也不假裝完整。

def _reject_constant(name: str):
    raise ValueError(f"索引快取裡有 {name}——不是合法 JSON")


def cache_path(path: Path) -> Path:
    return path.with_name(path.name + ".index.json")


def load_cached(path: Path) -> dict | None:
    cp = cache_path(path)
    if not cp.exists():
        return None
    try:
        # **NaN 一律當成壞快取。** Python 的 json 兩邊都放行 `NaN`（寫得出、
        # 讀得回），但那不是合法 JSON——FastAPI 序列化時才炸，而那時錯誤看
        # 起來像端點壞了，不像快取壞了。`parse_constant` 讓它在這裡就攔下來
        d = json.loads(cp.read_text(encoding="utf8"),
                       parse_constant=_reject_constant)
    except Exception as e:                       # 壞掉的快取＝沒有快取
        log.warning("索引快取讀不了（%s）：%s——重建", cp.name, e)
        return None
    return d if isinstance(d, dict) else None


def save_cached(path: Path, index: dict) -> None:
    try:
        # **allow_nan=False**：寫得出 NaN 的快取，是一顆延後引爆的地雷——
        # 寫的時候沒事，下一次讀出來才在序列化那一層炸，而那時看起來像端點壞了
        cache_path(path).write_text(
            json.dumps(index, ensure_ascii=False, allow_nan=False),
            encoding="utf8")
    except (OSError, ValueError) as e:
        # 寫不了就算了：索引還在記憶體裡、這次請求照樣回得出去
        log.warning("索引快取寫不了（%s）：%s", path.name, e)


#: 進行中的解析工作，鍵為檔案絕對路徑
_jobs: dict[str, dict] = {}


async def get_or_start(path: Path) -> tuple[str, dict]:
    """回 (狀態, 內容)。狀態＝`ready`／`building`。

    `ready` 的內容可能是**過期的索引**（檔案又長大了）——此時
    `indexed_bytes < bytes`，畫面要照實說，不是靜默給一份不完整的東西。
    """
    key = str(path)
    st = path.stat()

    job = _jobs.get(key)
    if job and not job["task"].done():
        return "building", {"percent": round(job["percent"], 1),
                            "file": path.name, "bytes": st.st_size}
    if job and job["task"].done():
        _jobs.pop(key, None)
        exc = job["task"].exception()
        if exc:
            raise exc

    cached = load_cached(path)
    if cached and cached.get("mtime") == st.st_mtime \
            and cached.get("indexed_bytes") == st.st_size:
        return "ready", cached
    if cached and cached.get("indexed_bytes", 0) > 0 \
            and st.st_size > cached["indexed_bytes"]:
        # 檔案還在寫。**回舊索引並標明落後多少**——重算一份 116 MB 要 35 秒，
        # 而使用者要的多半是「這份檔長什麼樣」，不是最後那幾秒
        stale = dict(cached)
        stale["bytes"] = st.st_size
        return "ready", stale

    job = {"percent": 0.0, "task": None}
    _jobs[key] = job

    def progress(p: float) -> None:
        job["percent"] = p

    async def run() -> dict:
        idx = await asyncio.to_thread(build, path, progress)
        save_cached(path, idx)
        return idx

    job["task"] = asyncio.create_task(run())
    # 小檔案不值得讓人多跑一趟：等一下下，做完就直接回結果
    done, _ = await asyncio.wait({job["task"]}, timeout=2.0)
    if done:
        _jobs.pop(key, None)
        return "ready", job["task"].result()
    return "building", {"percent": round(job["percent"], 1),
                        "file": path.name, "bytes": st.st_size}
