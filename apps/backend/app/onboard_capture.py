"""機上錄製的回傳落地（issues/014）。

地面站自己錄的 tlog（`capture.py`）是**「送到地面站的東西」**；這裡收的是
機上錄的那一份——**「飛控送出的東西」**。**5G 斷線期間，兩者相差的正是我們
最想看的那一段。** 所以兩層**分開存、分開列**：混成一個清單就把「差在哪裡」
這件事抹掉了，而那正是機上那份唯一不可取代的價值。

## 為什麼這件事做得成，而 `.BIN` 回收做不成

同一個算術，方向相反。飛控內部的 dataflash `.BIN` 大，是因為**它從來不過
那條 57600 的線**；機上 tlog 小，是因為**它一定過那條線**——它的大小上限
就是 `UART 速率 × 飛行時間`：

    5.6 KB/s（57600 8N1）× 600 s ≈ 3.4 MB ＋ 每框架 8 byte 時間戳 ≈ **4 MB／十分鐘**

四 MB 走 5G 是幾秒到幾十秒的事。**「什麼時候傳」之所以還是個問題，不是因為
量大，是因為它與遙測共用同一條 5G**——所以守門在機上（見 uav-agent 的
`uploader.py`：只在地面傳、一解鎖立刻停）。

## 三個刻意的設計

* **可續傳。** 機上到地面是 5G，而 5G 在戶外會斷。一次斷線就整個檔案重來的
  話，**鏈路愈差的那一趟愈傳不回來**——而那正是最值得看的一趟。所以走
  「宣告 → 逐塊接 → 收尾驗章」，中斷後從已收到的位元組接續。
* **收尾要驗 sha256。** **截斷的 tlog 看起來就是一個比較短的 tlog**，格式裡
  沒有任何地方會說「我不完整」。沒有校驗的話，「已回傳」只是一句宣稱。
* **目錄名用我們自己查出來的 `drone_id`，不是對方送來的字串。** 下載端可以用
  「必須在我列得出來的清單裡」擋路徑穿越（`api.py` 的既有紀律）；**上傳端
  沒有這個奢侈**——檔案還不存在，沒有清單可比。所以檔名改用嚴格樣式擋，
  而目錄那一段乾脆不讓對方參與。

## 撞名不覆蓋

**這台 Pi 的 RTC 沒有電池**（見 uav-agent `backfill.py` 的同一顆時鐘）：冷開機
時牆鐘從 1970 起算，而錄製檔名就是開檔當下的時間——所以**兩次冷開機真的可能
產生同名的檔案**。同名不同內容時另存 `..._2.tlog`，**絕不覆蓋**：覆蓋會讓一趟
飛行的紀錄消失，而且是安靜地消失。認「同一份」用的是 sha256 不是檔名。
"""
import hashlib
import json
import logging
import pathlib
import re
import shutil
import time
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger(__name__)

#: 檔名樣式，與機上 `recorder.py` 的命名逐字對應（`%Y%m%d-%H%M%S.tlog`，
#: 同秒換檔時加 `-N`）。**上傳端只能用樣式擋，不能用白名單擋**（見模組說明）。
NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?\.tlog$")

#: 一次最多收多大一塊。**這不是效能參數，是記憶體參數**——整塊會進到記憶體，
#: 而 backend 與遙測入庫是同一個行程。
MAX_CHUNK = 8 * 1024 * 1024

#: 單一檔案的上限。機上 `RECORD_MAX_MB` 預設 512，這裡留兩倍餘裕；
#: 超過就是設定不一致或對方在亂送，寧可擋下也不要讓磁碟被一個檔吃掉。
MAX_FILE = 1024 * 1024 * 1024


class TlogScan:
    """在算 sha256 的同一遍裡，順手取出這份錄製涵蓋的時間。

    **零額外成本**：收尾驗章本來就要逐 byte 讀過整個檔案，這裡只是在那條
    串流上多跑一台狀態機。分兩遍讀才是浪費。

    為什麼要它：**「機上這份補上了地面站瞎掉的那一段」是一句可以被檢驗的話**
    ——但要檢驗它，得先知道這份錄製涵蓋哪一段時間。沒有它，畫面只能說
    「有一個檔案」，說不出「它蓋住了那個洞」。

    tlog ＝ 每則訊息前綴 8-byte big-endian 微秒時間戳，接一個 MAVLink 框架。
    **切不動就停手並回 None**：半套的時間範圍比沒有更糟——它看起來像個答案。
    """

    def __init__(self):
        self.buf = b""
        self.first = None
        self.last = None
        self.frames = 0
        self.ok = True

    def feed(self, block: bytes) -> None:
        if not self.ok:
            return
        self.buf += block
        while True:
            n = len(self.buf)
            if n < 9:
                return
            ts = int.from_bytes(self.buf[:8], "big") / 1e6
            m = self.buf[8]
            if m == 0xFD:                      # MAVLink 2
                if n < 11:
                    return
                end = 8 + 12 + self.buf[9] + (13 if self.buf[10] & 1 else 0)
            elif m == 0xFE:                    # MAVLink 1
                if n < 10:
                    return
                end = 8 + 8 + self.buf[9]
            else:
                self.ok = False                # 對不上就別猜
                return
            if n < end:
                return
            if self.first is None:
                self.first = ts
            self.last = ts
            self.frames += 1
            self.buf = self.buf[end:]

    def result(self) -> dict | None:
        # **框架太少不回範圍**：一兩則訊息湊不出「涵蓋一段時間」這個意思
        if not self.ok or self.first is None or self.frames < 2:
            return None
        return {"from": round(self.first, 3), "to": round(self.last, 3),
                "frames": self.frames}


def root() -> pathlib.Path:
    return pathlib.Path(settings.capture_dir) / "onboard"


def _dir(drone_id: str) -> pathlib.Path:
    return root() / str(drone_id)


def _meta_path(d: pathlib.Path, stored_as: str) -> pathlib.Path:
    return d / (stored_as + ".meta")


def _read_meta(p: pathlib.Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_meta(p: pathlib.Path, meta: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False))
    tmp.replace(p)          # 原子換檔：讀的人不會看到半截 JSON


def free_mb() -> float:
    d = root()
    d.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(d).free / 1e6


def offer(drone_id: str, name: str, size: int, sha256: str) -> dict:
    """機上宣告「我有這個檔案要回傳」，回覆「我已經有幾個 byte」。

    **這一步就是續傳的全部機制**：機端不記得傳到哪裡也沒關係——重開機、
    換行程、狀態檔掉了，重新宣告一次就知道要從哪裡接。認「同一份」用 sha256，
    所以即使檔名撞了也不會接錯檔。
    """
    if not NAME_RE.match(name):
        raise ValueError(f"檔名不合樣式：{name!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("sha256 要是 64 個小寫十六進位字元")
    if not 0 < size <= MAX_FILE:
        raise ValueError(f"檔案大小 {size} 不在 1..{MAX_FILE} 之內")

    d = _dir(drone_id)
    d.mkdir(parents=True, exist_ok=True)

    # ① 已經認識這一份（sha 相同）→ 接續它，不管它現在叫什麼名字
    for m in sorted(d.glob("*.meta")):
        meta = _read_meta(m)
        if not meta or meta.get("sha256") != sha256:
            continue
        stored_as = m.name[:-len(".meta")]
        if meta.get("complete"):
            return {"stored_as": stored_as, "have": size, "complete": True}
        part = d / (stored_as + ".part")
        return {"stored_as": stored_as,
                "have": part.stat().st_size if part.exists() else 0,
                "complete": False}

    # ② 新的一份。撞名就換一個名字存——**不覆蓋**（見模組說明）
    stored_as, n = name, 1
    while (d / stored_as).exists() or (d / (stored_as + ".part")).exists():
        n += 1
        stored_as = f"{name[:-len('.tlog')]}_{n}.tlog"
    if stored_as != name:
        log.warning("機上錄製回傳撞名：%s 已存在且內容不同，另存為 %s"
                    "（機上 RTC 沒電池，冷開機的檔名會重複）", name, stored_as)
    _write_meta(_meta_path(d, stored_as), {
        "name": name, "stored_as": stored_as, "drone_id": str(drone_id),
        "bytes": size, "sha256": sha256, "complete": False,
        "offered_at": time.time()})
    (d / (stored_as + ".part")).touch()
    return {"stored_as": stored_as, "have": 0, "complete": False}


class Conflict(Exception):
    """對方送來的位移與我方實際收到的不一致。帶上真值讓它自己對回來。"""

    def __init__(self, have: int):
        super().__init__(f"位移不符，我方已收到 {have} bytes")
        self.have = have


class NoSpace(Exception):
    pass


def append(drone_id: str, stored_as: str, offset: int, data: bytes) -> dict:
    """收一塊。滿了就收尾（驗 sha256 → 改名 → 清理過期）。"""
    d = _dir(drone_id)
    mp = _meta_path(d, stored_as)
    meta = _read_meta(mp)
    if meta is None:
        raise FileNotFoundError(f"沒有宣告過這一份：{stored_as}")
    if meta.get("complete"):
        return {"have": meta["bytes"], "complete": True, "stored_as": stored_as}

    part = d / (stored_as + ".part")
    have = part.stat().st_size if part.exists() else 0
    if offset != have:
        raise Conflict(have)
    if have + len(data) > meta["bytes"]:
        raise ValueError(f"這一塊會超出宣告的大小（{meta['bytes']}）")
    # **空間不夠就明說並拒收**。地面站的磁碟滿了會拖垮資料庫，而機上那份
    # 還在原地——現在收不下不等於資料沒了，硬收才會兩邊都出事
    if free_mb() < settings.onboard_min_free_mb:
        raise NoSpace(f"地面站磁碟剩餘 {free_mb():.0f} MB，"
                      f"低於下限 {settings.onboard_min_free_mb} MB")

    with open(part, "ab") as f:
        f.write(data)
    have += len(data)
    if have < meta["bytes"]:
        return {"have": have, "complete": False, "stored_as": stored_as}

    # ── 收尾：驗章（順手掃出時間範圍，見 TlogScan）────────────
    h, scan = hashlib.sha256(), TlogScan()
    with open(part, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
            scan.feed(blk)
    if h.hexdigest() != meta["sha256"]:
        # **整份丟掉重來，不留半成品**：截斷或錯亂的 tlog 看起來完全正常，
        # 留著它比沒有它更糟——它會被當成證據
        part.unlink(missing_ok=True)
        mp.unlink(missing_ok=True)
        log.error("機上錄製回傳 %s 校驗不符（算得 %s、宣告 %s）——整份作廢重傳",
                  stored_as, h.hexdigest()[:12], meta["sha256"][:12])
        raise ValueError("sha256 不符，整份作廢；請從 0 重傳")

    part.replace(d / stored_as)
    meta.update(complete=True, received_at=time.time(), covers=scan.result())
    _write_meta(mp, meta)
    log.info("機上錄製回傳完成：%s（%.1f MB，drone %s）",
             stored_as, meta["bytes"] / 1e6, drone_id)
    prune()
    return {"have": have, "complete": True, "stored_as": stored_as}


def abandoned(drone_id: str, name: str, size: int, at: float) -> dict:
    """機上把一份**從來沒有回傳成功**的錄製滾動刪掉了。留一塊墓碑。

    **不是統計數字，是一列。** 那份東西不會再回來了，而「它曾經存在過」
    這件事只剩下這一列——沒有它，事後看到的只是清單裡少了一趟，
    而少了一趟與「那一趟沒有飛」在畫面上完全同形。

    墓碑就是一個沒有資料檔的 `.meta`，所以清單、清理、保留期全部沿用同一套。
    """
    if not NAME_RE.match(name):
        raise ValueError(f"檔名不合樣式：{name!r}")
    d = _dir(drone_id)
    d.mkdir(parents=True, exist_ok=True)
    if (d / stored_name(d, name)).exists():
        # **已經有完整的一份了**：機上刪的是它自己那一份，我們手上這份還在。
        # 這不是損失，不立碑
        return {"ok": True, "noted": False, "reason": "地面站已經有這一份了"}
    mp = _meta_path(d, name)
    prev = _read_meta(mp)
    if prev and prev.get("lost"):
        return {"ok": True, "noted": False, "reason": "已經記過了"}
    _write_meta(mp, {"name": name, "stored_as": name, "drone_id": str(drone_id),
                     "bytes": size, "sha256": None, "complete": False,
                     "lost": True, "lost_at": at})
    log.warning("機上錄製 %s（drone %s）未回傳即被滾動刪除——**這一趟的機上"
                "紀錄已經不存在**", name, drone_id)
    return {"ok": True, "noted": True}


def stored_name(d: pathlib.Path, name: str) -> str:
    """這個機上檔名在我們這裡實際存成什麼（撞名時會是 `..._2.tlog`）。"""
    for m in sorted(d.glob("*.meta")):
        meta = _read_meta(m)
        if meta and meta.get("name") == name and meta.get("complete"):
            return meta["stored_as"]
    return name


def prune() -> int:
    """依保留天數滾動清理。回傳刪掉幾份。

    **保留期不跟著地面站那份走**：機上這份小兩個數量級（一趟約 4 MB，
    對比地面站的 61 MB/hr），而且它是斷線那一段的**唯一副本**。
    """
    cutoff = time.time() - settings.onboard_keep_days * 86400
    n = 0
    for f in root().glob("*/*"):
        try:
            if f.stat().st_mtime >= cutoff:
                continue
            if f.suffix == ".meta":
                # **墓碑也會過期。** 它沒有資料檔，所以不會被下面那條掃到——
                # 不特別處理的話，「已遺失」那幾列會永遠留在清單上，
                # 而其他同期的紀錄早就清掉了
                if (_read_meta(f) or {}).get("lost"):
                    f.unlink()
                    n += 1
                continue
            f.unlink()
            _meta_path(f.parent, f.name[:-len(".part")] if f.suffix == ".part"
                       else f.name).unlink(missing_ok=True)
            n += 1
            log.info("機上錄製回傳清理過期檔 %s（保留 %d 天）",
                     f.name, settings.onboard_keep_days)
        except OSError:
            pass
    return n


def listing(names: dict[str, str] | None = None) -> dict:
    """已回傳的機上錄製一覽。`names`：drone_id → 顯示名稱。

    **半成品也要列出來**：「傳到一半」與「沒有傳」在畫面上完全同形，
    而兩者要做的事不同——前者等它自己接續，後者要去看機上發生了什麼。
    """
    out, total = [], 0
    for m in sorted(root().glob("*/*.meta"), reverse=True):
        meta = _read_meta(m)
        if not meta:
            continue
        drone_id = m.parent.name
        stored_as = meta["stored_as"]
        done = bool(meta.get("complete"))
        lost = bool(meta.get("lost"))
        f = m.parent / (stored_as if done else stored_as + ".part")
        got = 0 if lost else (f.stat().st_size if f.exists() else 0)
        total += got
        out.append({
            "drone_id": drone_id,
            "drone_name": (names or {}).get(drone_id),
            "name": stored_as,
            "onboard_name": meta.get("name"),
            "bytes": got,
            "expected_bytes": meta["bytes"],
            # **三態，不是兩態**：已回傳／傳到一半／機上已刪且永遠拿不到了。
            # 把最後一種混進「沒傳完」，就會有人一直等它自己傳完
            "status": "lost" if lost else ("complete" if done else "partial"),
            "complete": done,
            "sha256": meta["sha256"],
            "covers": meta.get("covers"),
            "received": (datetime.fromtimestamp(meta["received_at"],
                                                tz=timezone.utc).isoformat()
                         if meta.get("received_at") else None),
            "lost_at": (datetime.fromtimestamp(meta["lost_at"],
                                               tz=timezone.utc).isoformat()
                        if meta.get("lost_at") else None),
            "url": f"/api/onboard-captures/{drone_id}/{stored_as}" if done else None,
        })
    return {"dir": str(root()), "files": out, "total_bytes": total,
            "keep_days": settings.onboard_keep_days,
            "free_mb": round(free_mb()),
            "note": "機上錄的是「飛控送出的東西」；地面站那份（/api/captures）"
                    "是「送到地面站的東西」。兩者相差的就是 5G 斷線的那一段"}


def find(drone_id: str, name: str) -> pathlib.Path | None:
    """下載用的白名單比對：**它必須是我列得出來的那些檔案之一**。"""
    for f in root().glob("*/*.tlog"):
        if f.parent.name == drone_id and f.name == name:
            return f
    return None
