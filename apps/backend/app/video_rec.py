"""飛行影像錄製整合（issue 022；設計見 doc/flight-video-design.md）。

錄製本身由獨立的 uav-video（MediaMTX）容器做——backend 掛 --reload，改一行
程式就重啟，錄影不能綁在這裡。本模組只做三件事：

  1. 架次觸發：armed 開錄／disarmed 收錄（經 MediaMTX HTTP API）
  2. 片段入庫：定期把錄好的片段抄進 video_segments（錨點＋時長）
  3. 歸屬：用**時間區間**把片段對到架次（不是靠開關事件配對）

**最高原則：影像壞掉不准影響飛行資料。** 錄影是附加價值，架次/遙測/鏈路
才是研究主體。所以這裡每一個對外呼叫都是 best-effort：短逾時、吞例外、
只記日誌，絕不把錯誤丟回 _armed_transition 那條路徑上。

零新依賴：用標準庫 urllib 丟到執行緒跑（backend 沒有 httpx/aiohttp，為了幾個
本機小請求去重建映像不划算）。

錨點來源是**錄製器自己的 playback /list**（`start`＋`duration`），不是解析
檔名——檔名的 strftime 是寫檔當下的牆鐘，非即時輸入會與媒體時間脫節。
"""
import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import db
from .config import settings

log = logging.getLogger(__name__)

API = "http://127.0.0.1:9997"        # MediaMTX 控制 API（開關錄影）
PLAYBACK = "http://127.0.0.1:9996"   # MediaMTX playback（列片段：start/duration）
TIMEOUT = 2.0                        # 短逾時：錄影服務掛掉不准拖慢架次邏輯
SYNC_S = 30.0                        # 片段入庫週期（落地後才要看，不必即時）


def path_for(sysid: int) -> str:
    """MediaMTX path 名稱 ↔ 該機 sysid。"""
    return f"uav-{sysid}"


# ── HTTP（標準庫；同步函式，呼叫端用 to_thread 包）──────────────────────
def _req(url: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


async def _api(url: str, method: str = "GET", body=None):
    return await asyncio.to_thread(_req, url, method, body)


# ── 1. 架次觸發 ────────────────────────────────────────────────────────
async def set_record(sysid: int, on: bool) -> bool:
    """開／關某台的錄影。回傳是否成功（失敗只記日誌，不拋）。

    冪等寫法：先 PATCH（path 已存在的情形），404 再 POST add。**API 改動不會
    寫回唯讀設定檔，錄製器一重啟就回到預設 record: no**——所以不能假設設過
    就永久有效，reconcile() 會定期補回（實測踩過：容器重啟後 PATCH 回 404）。
    """
    name = path_for(sysid)
    try:
        await _api(f"{API}/v3/config/paths/patch/{name}", "PATCH", {"record": on})
        return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log.warning("影像：設定 %s record=%s 失敗 HTTP %s", name, on, e.code)
            return False
    except Exception as e:
        log.warning("影像：錄製服務無回應（%s: %s）——不影響架次記錄",
                    type(e).__name__, e)
        return False
    try:                                  # path 尚未宣告 → 新增
        await _api(f"{API}/v3/config/paths/add/{name}", "POST", {"record": on})
        return True
    except Exception as e:
        log.warning("影像：新增 path %s 失敗（%s）", name, e)
        return False


async def stream_ready(sysid: int) -> bool:
    """該機此刻有沒有影像流進來（決定 video_mode 是 on 還是 no_source）。"""
    try:
        d = await _api(f"{API}/v3/paths/get/{path_for(sysid)}")
        return bool(d and d.get("ready"))
    except Exception:
        return False


async def decide_video_mode(sysid: int | None) -> str:
    """架次建立時決定 video_mode。**零片段有三種意思，這欄讓它們分得開**：
    'off'＝本趟刻意不錄（實驗設定）、'no_source'＝這台沒有影像來源、
    'on'＝預期要錄（事後若零片段就是故障，不是正常）。"""
    if not settings.video_record_enabled:
        return "off"
    if sysid is None or not await stream_ready(sysid):
        return "no_source"
    return "on"


# 下面兩支**一律以 create_task 背景執行**（呼叫端見 mavlink_rx._armed_transition）。
# 理由：rx worker 是**單一執行緒**依序消化訊息，這裡若 await 住（HTTP 最多 2s、
# 收錄還要等 3s），整條 MAVLink 處理就停擺——影像絕不能拖累飛行資料。
# 也因此兩支都自己吞例外：背景 task 的例外沒人接，會變成靜默的
# 「Task exception was never retrieved」。
async def on_session_start(session_id: str, sysid: int | None) -> None:
    """架次開始：標 video_mode（零片段的三種意思靠它分辨）＋開錄。"""
    try:
        mode = await decide_video_mode(sysid)
        await db.pool.execute(
            "UPDATE flight_sessions SET video_mode = $2 WHERE id = $1",
            session_id, mode)
        if mode == "on" and sysid is not None:
            await set_record(sysid, True)
            log.info("影像：架次 %s 開錄（sysid %s）", session_id[:8], sysid)
    except Exception:
        log.exception("影像：架次開始處理失敗（架次記錄本身不受影響）")


async def on_session_end(sysid: int | None) -> None:
    """架次結束：延遲收錄——收尾片段還在寫，馬上關會切掉最後幾秒。"""
    if sysid is None:
        return
    try:
        await asyncio.sleep(3.0)
        await set_record(sysid, False)
        # 落地後追加幾次同步：週期迴圈是 30s 一輪，光靠它最久要 ~60s 才會把長度
        # 結算並標 final，而使用者落地後**馬上就會開回放**。這裡快速收斂。
        for delay in (5.0, 15.0, 30.0):
            await asyncio.sleep(delay)
            await sync_segments()
    except Exception:
        log.exception("影像：收錄失敗（不影響架次記錄）")


# ── 2＋3. 片段入庫與歸屬 ───────────────────────────────────────────────
def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def sync_segments() -> int:
    """把錄好的片段抄進 video_segments。回傳新增/更新筆數。

    以錄製器的 playback /list 為準（start＝影片第 0 秒的絕對時間、duration）。
    歸屬用**時間區間**查架次：開關晚幾秒或服務中途重啟都不影響正確性。
    重跑安全（UNIQUE(drone_id, started_at)；duration 會隨錄製中的段成長而更新）。
    """
    rows = await db.pool.fetch(
        "SELECT id::text AS id, mav_sysid FROM drones WHERE mav_sysid IS NOT NULL")
    n = 0
    for r in rows:
        sysid, drone_id = r["mav_sysid"], r["id"]
        name = path_for(sysid)
        try:
            items = await _api(f"{PLAYBACK}/list?path={name}")
        except Exception:
            continue                      # 沒錄過這台＝沒有目錄，屬正常
        for it in items or []:
            started = _parse_ts(it.get("start", ""))
            if started is None:
                continue
            session_id = await db.find_session_at(drone_id, started)
            await db.pool.execute(
                # final：長度**與上一輪相同**才算定案。錄製中的段每輪都會變長，
                # 所以「不再變長」就是結束的可靠訊號，不必額外去問錄製器狀態。
                # 定案前 UI 不對尾端做斷言（否則會把還沒結算完的尾巴讀成斷流）。
                """INSERT INTO video_segments
                     (drone_id, session_id, started_at, duration_s, path, source)
                   VALUES ($1, $2, $3, $4, $5, 'ground')
                   ON CONFLICT (drone_id, started_at) DO UPDATE
                     SET duration_s = EXCLUDED.duration_s,
                         final = (video_segments.duration_s IS NOT NULL
                                  AND video_segments.duration_s = EXCLUDED.duration_s),
                         session_id = COALESCE(video_segments.session_id,
                                               EXCLUDED.session_id)""",
                drone_id, session_id, started, it.get("duration"), name)
            n += 1
    return n


async def prune_segments() -> int:
    """清掉過保留期的片段列——**必須與檔案同步消失**。

    錄製器自己會依 `recordDeleteAfter` 刪檔（同一個 .env 的保留天數），但它不
    知道 DB。只刪檔不刪列的話，回放頁會畫出一條**指向已刪檔的涵蓋帶**——點了
    沒反應，等於騙人（UI/UX 定案：不留幽靈輪廓，`expired` 一句話講清楚就好）。
    兩邊用同一個 retention 設定，所以刪除時機自然對齊。
    """
    r = await db.pool.execute(
        "DELETE FROM video_segments WHERE started_at < now() - ($1 || ' days')::interval",
        str(settings.video_retention_days))
    n = int(r.split()[-1]) if r else 0
    if n:
        log.info("影像：清掉 %d 段過保留期（%d 天）的片段列", n,
                 settings.video_retention_days)
    return n


async def reconcile() -> None:
    """確保「正在飛的機」確實在錄。

    需要這個是因為錄製器的 API 改動不寫回設定檔：uav-video 一重啟就全部回到
    record: no，飛行中就會**靜默停錄**。定期補回比事後才發現沒錄好。
    """
    if not settings.video_record_enabled:
        return
    from .state import fleet
    for st in list(fleet.values()):
        if st.armed and st.sysid:
            await set_record(st.sysid, True)


async def loop() -> None:
    """週期任務：片段入庫＋錄製狀態校正。整段包例外——影像的問題不准
    影響其他迴圈（同 _broadcast_loop 的紀律）。"""
    while True:
        await asyncio.sleep(SYNC_S)
        try:
            await reconcile()
            await sync_segments()
            await prune_segments()   # 列與檔案同步消失，不留幽靈涵蓋帶
        except Exception:
            log.exception("影像同步失敗，略過這一輪（不影響飛行資料）")


# ── API 用：某架次的影像狀態（契約見設計 §8b）──────────────────────────
async def session_video(session_id: str) -> dict:
    """回傳該架次的影像片段與狀態。五態由後端算好，UI 不做日期運算。"""
    s = await db.pool.fetchrow(
        "SELECT drone_id::text AS drone_id, started_at, ended_at, video_mode "
        "FROM flight_sessions WHERE id = $1", session_id)
    if s is None:
        return {}
    segs = await db.pool.fetch(
        """SELECT id::text AS id, started_at, duration_s, codec, width, height,
                  fps, bytes, final
           FROM video_segments WHERE session_id = $1 ORDER BY started_at""",
        session_id)
    # NULL **不能當成 'on'**：影像功能上線前的舊架次全是 NULL，當成 'on' 會讓
    # 每一趟歷史飛行都被判成 'missing'（錄製故障）＝整片假警報。NULL 一律視為
    # 'off'——「本趟未啟用錄影」對舊架次是事實，對「標記失敗」的架次也仍然成立
    # （沒開錄就是沒影像），寧可少報一次故障也不要製造警報疲勞。
    mode = s["video_mode"] or "off"
    if segs:
        status = "available"
    elif mode in ("off", "no_source"):
        status = mode
    else:
        # 零片段且本趟預期要錄：過了保留期＝已清除；還在保留期內＝**故障**
        # （該錄卻整趟沒收到流）。兩者對研究的意義相反，不能混為一談。
        end = s["ended_at"] or s["started_at"]
        age_days = (datetime.now(timezone.utc) - end).total_seconds() / 86400.0
        status = "expired" if age_days > settings.video_retention_days else "missing"
    return {
        "retention_days": settings.video_retention_days,
        "video_status": status,
        "segments": [
            {"id": g["id"], "started_at": g["started_at"].isoformat(),
             "duration_s": g["duration_s"], "codec": g["codec"],
             "width": g["width"], "height": g["height"], "fps": g["fps"],
             "bytes": g["bytes"],
             # false＝這段長度還可能變長（錄製器仍在結算），UI 不要對尾端斷言
             # 「此時段無影像」——那會把正常錄影說成故障
             "final": g["final"],
             "url": f"/api/video/segments/{g['id']}/file"}
            for g in segs
        ],
    }
