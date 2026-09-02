"""錄製檔的 metadata（issues/014）。

## 為什麼是這個形狀（2026-09-02 使用者裁定）

> **所有資料都要靠 DB 存，事實來源由一張 metadata 表記得，其他人透過 UID
> 外鍵指回去查。**
>
> **而資料本身很大的時候，SQL 欄位記路徑，要內容再到那個路徑下去看。**

所以：**metadata 在 `captures` 表（小、查得動、關聯得上），內容留在磁碟
（大），兩者用 `path` 相連**——與 `video_segments` 完全同一個形狀。

這一版之前不是這樣：機上錄製的 metadata 寫在磁碟上的 `.meta` JSON 檔裡，
清單靠 glob 目錄。那等於**把事實來源放在檔案系統裡**——查不了、關聯不了、
刪機時也不會連帶清，而且「地面站瞎掉的那一段機上補到了沒有」這種問題得把
整個目錄讀進記憶體才答得出來，現在它是一句 SQL。

## 兩層是同一張表的兩個 `tier`，不是兩張表

`ground`＝地面站錄的「送到地面站的東西」；`onboard`＝機上錄的「飛控送出的
東西」。**兩者相差的正是 5G 斷線那一段**——要能用一句 SQL 把兩層對起來，
它們就必須在同一張表裡。（畫面上仍然分開列：混成一個清單會把那個差抹掉。）

## 三個沒有變的紀律

* **可續傳。** 5G 在戶外會斷，一次斷線就整份重來的話，**鏈路愈差的那趟愈
  傳不回來**——而那正是最值得看的一趟。認「同一份」用 sha256 不是檔名。
* **收尾驗 sha256。** 截斷的 tlog 看起來就是一個比較短的 tlog，格式裡沒有
  任何地方會說「我不完整」。
* **目錄名用我們自己查出來的 `drone_id`。** 下載端的白名單現在更強：
  **它必須是 `captures` 表裡的一列**，而路徑從那一列讀出來，不是拼出來的。
"""
import hashlib
import logging
import pathlib
import re
import shutil
import time
from datetime import datetime, timezone

from . import db
from .config import settings

log = logging.getLogger(__name__)

#: 檔名樣式，與機上 `recorder.py` 的命名逐字對應（`%Y%m%d-%H%M%S.tlog`，
#: 同秒換檔時加 `-N`）。**上傳端只能用樣式擋**：檔案還不存在，沒有清單可比。
NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?\.tlog$")

#: 一次最多收多大一塊。**這不是效能參數，是記憶體參數**——整塊會進到記憶體，
#: 而 backend 與遙測入庫是同一個行程。
MAX_CHUNK = 8 * 1024 * 1024

#: 單一檔案的上限。機上 `RECORD_MAX_MB` 預設 512，這裡留兩倍餘裕。
MAX_FILE = 1024 * 1024 * 1024


class TlogScan:
    """在算 sha256 的同一遍裡，順手取出這份錄製涵蓋的時間。

    **零額外成本**：收尾驗章本來就要逐 byte 讀過整個檔案，這裡只是在那條
    串流上多跑一台狀態機。分兩遍讀才是浪費。

    為什麼要它：**「機上這份補上了地面站瞎掉的那一段」是一句可以被檢驗的
    話**——但要檢驗它，得先知道這份錄製涵蓋哪一段時間。

    tlog ＝ 每則訊息前綴 8-byte big-endian 微秒時間戳，接一個 MAVLink 框架。
    **切不動就停手並回 None**：半套的時間範圍比沒有更糟——它看起來像答案。
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
        return {"from": self.first, "to": self.last, "frames": self.frames}


class Conflict(Exception):
    """對方送來的位移與我方實際收到的不一致。帶上真值讓它自己對回來。"""

    def __init__(self, have: int):
        super().__init__(f"位移不符，我方已收到 {have} bytes")
        self.have = have


class NoSpace(Exception):
    pass


# ── 磁碟位置 ────────────────────────────────────────────────────
def root() -> pathlib.Path:
    return pathlib.Path(settings.capture_dir)


def _onboard_dir(drone_id: str) -> pathlib.Path:
    return root() / "onboard" / str(drone_id)


def _safe(path: str) -> pathlib.Path | None:
    """DB 裡的路徑仍然要驗一次在不在錄製根目錄底下。

    **這一列是我們自己寫的，但「我們自己寫的」不是一個安全機制**——
    往後多一個寫入端，這裡就是唯一擋得住的地方。
    """
    p = pathlib.Path(path).resolve()
    try:
        p.relative_to(root().resolve())
    except ValueError:
        log.error("錄製檔路徑跑出錄製根目錄之外，拒絕：%s", path)
        return None
    return p


def free_mb() -> float:
    d = root()
    d.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(d).free / 1e6


def _ts(v):
    return datetime.fromtimestamp(v, tz=timezone.utc) if v else None


# ── 機上回傳：宣告 → 逐塊 → 收尾 ────────────────────────────────
async def offer(drone_id: str, name: str, size: int, sha256: str) -> dict:
    """機上宣告「我有這個檔案要回傳」，回覆「我已經有幾個 byte」。

    **這一步就是續傳的全部機制**：機端不記得傳到哪裡也沒關係——重開機、
    換行程、狀態檔掉了，重新宣告一次就知道要從哪裡接。**認「同一份」用
    sha256**，所以即使檔名撞了也不會接錯檔。
    """
    if not NAME_RE.match(name):
        raise ValueError(f"檔名不合樣式：{name!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("sha256 要是 64 個小寫十六進位字元")
    if not 0 < size <= MAX_FILE:
        raise ValueError(f"檔案大小 {size} 不在 1..{MAX_FILE} 之內")

    d = _onboard_dir(drone_id)
    d.mkdir(parents=True, exist_ok=True)

    # ① 已經認識這一份（sha 相同）→ 接續它，不管它現在叫什麼名字
    row = await db.pool.fetchrow(
        "SELECT name, status, bytes FROM captures "
        "WHERE tier = 'onboard' AND drone_id = $1::uuid AND sha256 = $2",
        drone_id, sha256)
    if row:
        if row["status"] == "complete":
            return {"stored_as": row["name"], "have": size, "complete": True}
        part = d / (row["name"] + ".part")
        return {"stored_as": row["name"],
                "have": part.stat().st_size if part.exists() else 0,
                "complete": False}

    # ② 新的一份。撞名就換一個名字存——**不覆蓋**
    #
    # **機上的 RTC 沒有電池**：冷開機時牆鐘從 1970 起算，而檔名就是開檔當下
    # 的時間——所以兩次冷開機真的會產生同名的檔案。覆蓋會讓一趟飛行的紀錄
    # 消失，而且是安靜地消失。
    taken = {r["name"] for r in await db.pool.fetch(
        "SELECT name FROM captures WHERE tier = 'onboard' AND drone_id = $1::uuid",
        drone_id)}
    stored_as, n = name, 1
    while stored_as in taken or (d / stored_as).exists() or (d / (stored_as + ".part")).exists():
        n += 1
        stored_as = f"{name[:-len('.tlog')]}_{n}.tlog"
    if stored_as != name:
        log.warning("機上錄製回傳撞名：%s 已存在且內容不同，另存為 %s"
                    "（機上 RTC 沒電池，冷開機的檔名會重複）", name, stored_as)

    await db.pool.execute(
        """INSERT INTO captures (drone_id, tier, name, onboard_name, path,
             bytes, expected_bytes, sha256, status)
           VALUES ($1::uuid, 'onboard', $2, $3, $4, 0, $5, $6, 'partial')""",
        drone_id, stored_as, name, str(d / stored_as), size, sha256)
    (d / (stored_as + ".part")).touch()
    return {"stored_as": stored_as, "have": 0, "complete": False}


async def append(drone_id: str, stored_as: str, offset: int, data: bytes) -> dict:
    """收一塊。滿了就收尾（驗 sha256 → 改名 → 寫回 DB → 清理過期）。

    **`path` 一路指著最終位置**，收到一半的內容住在 `path + '.part'`：
    路徑是這一列的身分，不該因為傳到一半而變來變去。
    """
    row = await db.pool.fetchrow(
        "SELECT name, path, expected_bytes, sha256, status FROM captures "
        "WHERE tier = 'onboard' AND drone_id = $1::uuid AND name = $2",
        drone_id, stored_as)
    if row is None:
        raise FileNotFoundError(f"沒有宣告過這一份：{stored_as}")
    if row["status"] == "complete":
        return {"have": row["expected_bytes"], "complete": True,
                "stored_as": stored_as}

    final = _safe(row["path"])
    if final is None:
        raise ValueError("這一列的路徑不合法")
    part = final.with_name(final.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if offset != have:
        raise Conflict(have)
    if have + len(data) > row["expected_bytes"]:
        raise ValueError(f"這一塊會超出宣告的大小（{row['expected_bytes']}）")
    # **空間不夠就明說並拒收**。地面站的磁碟滿了會拖垮資料庫，而機上那份
    # 還在原地——現在收不下不等於資料沒了，硬收才會兩邊都出事
    if free_mb() < settings.onboard_min_free_mb:
        raise NoSpace(f"地面站磁碟剩餘 {free_mb():.0f} MB，"
                      f"低於下限 {settings.onboard_min_free_mb} MB")

    with open(part, "ab") as f:
        f.write(data)
    have += len(data)
    if have < row["expected_bytes"]:
        await db.pool.execute(
            "UPDATE captures SET bytes = $3 WHERE tier = 'onboard' "
            "AND drone_id = $1::uuid AND name = $2", drone_id, stored_as, have)
        return {"have": have, "complete": False, "stored_as": stored_as}

    # ── 收尾：驗章（順手掃出時間範圍，見 TlogScan）────────────
    h, scan = hashlib.sha256(), TlogScan()
    with open(part, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
            scan.feed(blk)
    if h.hexdigest() != row["sha256"]:
        # **整份丟掉重來，不留半成品**：截斷或錯亂的 tlog 看起來完全正常，
        # 留著它比沒有它更糟——它會被當成證據
        part.unlink(missing_ok=True)
        await db.pool.execute(
            "DELETE FROM captures WHERE tier = 'onboard' "
            "AND drone_id = $1::uuid AND name = $2", drone_id, stored_as)
        log.error("機上錄製回傳 %s 校驗不符（算得 %s、宣告 %s）——整份作廢重傳",
                  stored_as, h.hexdigest()[:12], row["sha256"][:12])
        raise ValueError("sha256 不符，整份作廢；請從 0 重傳")

    part.replace(final)
    cov = scan.result()
    await db.pool.execute(
        """UPDATE captures SET bytes = $3, status = 'complete', received_at = now(),
             covers_from = $4, covers_to = $5, frames = $6
           WHERE tier = 'onboard' AND drone_id = $1::uuid AND name = $2""",
        drone_id, stored_as, have,
        _ts(cov and cov["from"]), _ts(cov and cov["to"]), cov and cov["frames"])
    log.info("機上錄製回傳完成：%s（%.1f MB，drone %s）",
             stored_as, have / 1e6, drone_id)
    await prune()
    return {"have": have, "complete": True, "stored_as": stored_as}


async def abandoned(drone_id: str, name: str, size: int, at: float) -> dict:
    """機上把一份**從來沒有回傳成功**的錄製滾動刪掉了。留一塊墓碑。

    **不是統計數字，是一列。** 那份東西不會再回來了，而「它曾經存在過」
    這件事只剩下這一列——沒有它，事後看到的只是清單裡少了一趟，
    而少了一趟與「那一趟沒有飛」在畫面上完全同形。
    """
    if not NAME_RE.match(name):
        raise ValueError(f"檔名不合樣式：{name!r}")
    done = await db.pool.fetchrow(
        "SELECT 1 FROM captures WHERE tier = 'onboard' AND drone_id = $1::uuid "
        "AND onboard_name = $2 AND status = 'complete'", drone_id, name)
    if done:
        # **我方已經有完整的一份**：機上刪的是它自己那一份，不是損失
        return {"ok": True, "noted": False, "reason": "地面站已經有這一份了"}
    prev = await db.pool.fetchrow(
        "SELECT status FROM captures WHERE tier = 'onboard' "
        "AND drone_id = $1::uuid AND name = $2", drone_id, name)
    if prev and prev["status"] == "lost":
        return {"ok": True, "noted": False, "reason": "已經記過了"}
    await db.pool.execute(
        """INSERT INTO captures (drone_id, tier, name, onboard_name, path,
             expected_bytes, status, lost_at)
           VALUES ($1::uuid, 'onboard', $2, $2, $3, $4, 'lost', to_timestamp($5))
           ON CONFLICT (tier, drone_id, name) DO UPDATE
             SET status = 'lost', lost_at = to_timestamp($5)""",
        drone_id, name, str(_onboard_dir(drone_id) / name), size, at)
    log.warning("機上錄製 %s（drone %s）未回傳即被滾動刪除——**這一趟的機上"
                "紀錄已經不存在**", name, drone_id)
    return {"ok": True, "noted": True}


# ── 地面站那一層：檔案是 capture.py 寫的，這裡把它登錄進 DB ──────
async def reconcile_ground() -> int:
    """把地面站錄製目錄裡的檔案登錄進 `captures`。回傳新增幾列。

    **地面站那一層的檔案不是我們建的**（`capture.py` 每天換一個檔），
    所以它沒有一個「建檔時機」可以掛。定期對帳是最誠實的做法：
    **目錄裡有什麼，表裡就有什麼**。

    `covers_*` 留空：要填它得把整個檔案讀一遍（實測 50 MB／天），
    而地面站這一層的涵蓋範圍**不需要**——兩層對照時要檢驗的是「機上那份
    有沒有蓋住地面站的洞」，地面站的洞來自 `blackouts` 表，不來自檔案。
    """
    d = root()
    if not d.is_dir():
        return 0
    n = 0
    for f in sorted(d.glob("*.tlog")):
        r = await db.pool.execute(
            """INSERT INTO captures (tier, name, path, bytes, status, received_at)
               VALUES ('ground', $1, $2, $3, 'complete', to_timestamp($4))
               ON CONFLICT (tier, drone_id, name) DO UPDATE SET bytes = $3""",
            f.name, str(f), f.stat().st_size, f.stat().st_mtime)
        if r.endswith(" 1"):
            n += 1
    return n


# ── 查詢 ────────────────────────────────────────────────────────
async def listing(tier: str) -> dict:
    """某一層的錄製一覽。**一句 SQL，不再 glob 目錄。**

    **半成品與墓碑也要列出來**：「傳到一半」「根本沒傳」「已經永遠沒了」
    三者要做的事完全不同，而在畫面上它們同形。
    """
    rows = await db.pool.fetch(
        """SELECT c.*, d.name AS drone_name
           FROM captures c LEFT JOIN drones d ON d.id = c.drone_id
           WHERE c.tier = $1
           ORDER BY coalesce(c.received_at, c.lost_at, c.created_at) DESC""", tier)
    files = []
    for r in rows:
        files.append({
            "drone_id": str(r["drone_id"]) if r["drone_id"] else None,
            "drone_name": r["drone_name"],
            "name": r["name"], "onboard_name": r["onboard_name"],
            "bytes": r["bytes"], "expected_bytes": r["expected_bytes"],
            "status": r["status"], "complete": r["status"] == "complete",
            "sha256": r["sha256"],
            "covers": ({"from": r["covers_from"].timestamp(),
                        "to": r["covers_to"].timestamp(),
                        "frames": r["frames"]} if r["covers_from"] else None),
            "received": r["received_at"].isoformat() if r["received_at"] else None,
            "lost_at": r["lost_at"].isoformat() if r["lost_at"] else None,
            "url": (f"/api/{'onboard-' if tier == 'onboard' else ''}captures/"
                    + (f"{r['drone_id']}/{r['name']}" if tier == "onboard"
                       else r["name"]) if r["status"] == "complete" else None),
        })
    return {"dir": str(root() / "onboard" if tier == "onboard" else root()),
            "files": files,
            "total_bytes": sum(f["bytes"] for f in files),
            "keep_days": (settings.onboard_keep_days if tier == "onboard"
                          else settings.capture_keep_days),
            "free_mb": round(free_mb())}


async def find(tier: str, name: str, drone_id: str | None = None) -> pathlib.Path | None:
    """下載用。**白名單現在是「它必須是 captures 表裡的一列」**——
    路徑從那一列讀出來，不是拼出來的（再驗一次在根目錄底下，見 `_safe`）。"""
    row = await db.pool.fetchrow(
        "SELECT path FROM captures WHERE tier = $1 AND name = $2 "
        "AND status = 'complete' AND drone_id IS NOT DISTINCT FROM $3::uuid",
        tier, name, drone_id)
    if row is None:
        return None
    p = _safe(row["path"])
    return p if p and p.is_file() else None


async def prune() -> int:
    """依保留天數滾動清理（檔案與那一列一起）。回傳刪掉幾份。

    **保留期不跟著地面站那份走**：機上這份小兩個數量級（一趟約 4 MB，
    對比地面站的 61 MB/hr），而且它是斷線那一段的**唯一副本**。
    """
    cutoff = time.time() - settings.onboard_keep_days * 86400
    rows = await db.pool.fetch(
        "SELECT name, drone_id, path FROM captures WHERE tier = 'onboard' "
        "AND coalesce(received_at, lost_at, created_at) < to_timestamp($1)",
        cutoff)
    for r in rows:
        p = _safe(r["path"])
        if p:
            p.unlink(missing_ok=True)
            p.with_name(p.name + ".part").unlink(missing_ok=True)
        await db.pool.execute(
            "DELETE FROM captures WHERE tier = 'onboard' AND drone_id = $1 "
            "AND name = $2", r["drone_id"], r["name"])
        log.info("機上錄製回傳清理過期檔 %s（保留 %d 天）",
                 r["name"], settings.onboard_keep_days)
    return len(rows)
