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

import video_stream                  # libs/ 的共用實作（PYTHONPATH=/srv/libs）

from . import db
from .config import settings

log = logging.getLogger(__name__)

API = "http://127.0.0.1:9997"        # MediaMTX 控制 API（開關錄影）
PLAYBACK = "http://127.0.0.1:9996"   # MediaMTX playback（列片段：start/duration）
TIMEOUT = 2.0                        # 短逾時：錄影服務掛掉不准拖慢架次邏輯
_stream_ok: dict[int, bool] = {}   # sysid → 上一輪來源是否正常（事件去抖）
SYNC_S = 30.0                        # 片段入庫週期（落地後才要看，不必即時）


# path 名稱的規則在 `libs/video_stream.py`——**只能有一份**。command 服務的
# 對外端點也要算出同一個名字，抄第二份的下場是兩邊不一致，而錄影綁在名字上。
#
# 為什麼綁 `drones.id` 不綁 sysid：**sysid 會被重新指派**（issues/040）。
# 2026-09-08 已經看過一次後果：`uav-1` 的來源指向另一台機的相機，一旦相機通了，
# 這台的架次會錄到**另一台的畫面**，而 `sync_segments` 用時間區間歸屬，會照樣
# 把它記在這台名下——事後幾乎救不回來。
# 改名時機（2026-09-23）：`video_segments` 還是 0 列，沒有歷史要搬。
path_for = video_stream.path_for


async def path_of(sysid: int | None) -> str | None:
    """sysid → path。**每次重查，不快取**：快取一個會被重新指派的號碼，
    正是上面那個坑的來源。查不到（還沒建檔）回 None，呼叫端就不要動錄影。"""
    if sysid is None:
        return None
    try:
        row = await db.pool.fetchrow(
            "SELECT id::text AS id FROM drones WHERE mav_sysid = $1", sysid)
    except Exception:
        log.exception("影像：查不到 sysid %s 的機體記錄", sysid)
        return None
    return path_for(row["id"]) if row else None


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

    **開錄同時要關掉 `sourceOnDemand`。** 2026-09-23 實測：只設 `record: yes`
    而來源是 on-demand 的話，Pi 上根本不會起 ffmpeg，`ready` 一直是 false、
    `bytesReceived` 是 0——MediaMTX 的 on-demand 只認「有讀者在看」，**錄影不
    算讀者**。架次開始時通常沒人開著即時頁，於是整趟一段都錄不到。
    收錄時再設回 on-demand，飛完就不佔上行（與 5G 量測共用一條，見 set_source）。
    """
    name = await path_of(sysid)
    if name is None:
        log.warning("影像：sysid %s 還沒有機體記錄，不動錄影", sysid)
        return False
    body = {"record": on, "sourceOnDemand": not on}
    try:
        await _api(f"{API}/v3/config/paths/patch/{name}", "PATCH", body)
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
        await _api(f"{API}/v3/config/paths/add/{name}", "POST", body)
        return True
    except Exception as e:
        log.warning("影像：新增 path %s 失敗（%s）", name, e)
        return False


async def set_source(drone_id: str, camera_url: str | None) -> bool:
    """把某台機的 path 設成**去拉**它機上的 RTSP（issue 022，2026-09-23 使用者裁定）。

    `sourceOnDemand: yes`＝**沒人看、也沒在錄的時候完全不拉**。這在本專案不是
    省頻寬而已：影像與 5G 量測共用同一條上行，一直傳等於**量到的不再是原本那條
    鏈路的品質**（設計 §9）。

    `camera_url` 給 None／空＝把來源拿掉（path 留著，錄影開關仍由架次控制）。
    失敗只記日誌——影像壞掉不准影響飛行資料。

    **正在錄的時候不把 on-demand 設回來**：飛行中換相機來源（少見但做得到）
    若順手打開 on-demand，等於當場把錄影的來源關掉（見 `set_record`）。
    """
    name = path_for(drone_id)
    body = {"source": camera_url or "", "sourceOnDemand": bool(camera_url)}
    if body["sourceOnDemand"] and await _recording(name):
        body["sourceOnDemand"] = False
    try:
        await _api(f"{API}/v3/config/paths/patch/{name}", "PATCH", body)
        return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log.warning("影像：設定 %s 的來源失敗 HTTP %s", name, e.code)
            return False
    except Exception as e:
        log.warning("影像：錄製服務無回應（%s）——來源沒設成", type(e).__name__)
        return False
    try:
        await _api(f"{API}/v3/config/paths/add/{name}", "POST", {**body, "record": False})
        return True
    except Exception as e:
        log.warning("影像：新增 path %s 失敗（%s）", name, e)
        return False


async def stream_ready(sysid: int) -> bool:
    """該機**此刻**有沒有影像流進來。

    拉流之後這支只代表「現在有沒有在拉」，不代表「有沒有相機」——沒人看又沒在
    錄的時候本來就是 false（on-demand）。判斷有沒有來源請用 `has_source()`。
    """
    name = await path_of(sysid)
    if name is None:
        return False
    try:
        d = await _api(f"{API}/v3/paths/get/{name}")
        return bool(d and d.get("ready"))
    except Exception:
        return False


async def _conf(name: str) -> dict | None:
    try:
        return await _api(f"{API}/v3/config/paths/get/{name}")
    except Exception:
        return None


async def _recording(name: str) -> bool:
    c = await _conf(name)
    return bool(c and c.get("record"))


async def has_source(sysid: int | None) -> bool:
    """這台**有沒有影像來源可用**——不是「現在有沒有在傳」。

    2026-09-23 改拉流後這兩件事分家了：on-demand 的 path 平時是 `ready: false`，
    照舊寫法每一趟開始時都會被判成 `no_source`、整趟不錄。所以先看設定裡有沒有
    指定來源（拉流），沒有的話才退回看現在有沒有人在推（推流，`source: publisher`）。
    """
    if sysid is None:
        return False
    name = await path_of(sysid)
    if name is None:
        return False
    c = await _conf(name)
    src = (c or {}).get("source") or ""
    if src and src != "publisher":
        return True                       # 拉流：設定裡有來源就算有
    return await stream_ready(sysid)      # 推流：只能看現在有沒有東西進來


async def decide_video_mode(sysid: int | None) -> str:
    """架次建立時決定 video_mode。**零片段有三種意思，這欄讓它們分得開**：
    'off'＝本趟刻意不錄（實驗設定）、'no_source'＝這台沒有影像來源、
    'on'＝預期要錄（事後若零片段就是故障，不是正常）。"""
    if not settings.video_record_enabled:
        return "off"
    if not await has_source(sysid):
        return "no_source"
    return "on"


# 下面兩支**一律以 create_task 背景執行**（呼叫端見 mavlink_rx._armed_transition）。
# 理由：rx worker 是**單一執行緒**依序消化訊息，這裡若 await 住（HTTP 最多 2s、
# 收錄還要等 3s），整條 MAVLink 處理就停擺——影像絕不能拖累飛行資料。
# 也因此兩支都自己吞例外：背景 task 的例外沒人接，會變成靜默的
# 「Task exception was never retrieved」。
async def on_session_start(session_id: str, sysid: int | None, st=None) -> None:
    """架次開始：標 video_mode（零片段的三種意思靠它分辨）＋開錄。"""
    try:
        mode = await decide_video_mode(sysid)
        await db.pool.execute(
            "UPDATE flight_sessions SET video_mode = $2 WHERE id = $1",
            session_id, mode)
        if st is not None:
            st.video_mode = mode          # 前端記錄燈說明用（telemetry 帶出去）
        if mode == "on" and sysid is not None:
            ok = await set_record(sysid, True)
            if ok:
                log.info("影像：架次 %s 開錄（sysid %s）", session_id[:8], sysid)
            else:
                # 該錄卻開不起來——這是故障，必須讓操作員看得見，不能只留在日誌
                await _emit(st, "video_recording_failed",
                            "錄製服務未回應，本架次開錄失敗")
    except Exception:
        log.exception("影像：架次開始處理失敗（架次記錄本身不受影響）")


async def _emit(st, type_: str, reason: str, severity: str = "warning") -> None:
    """發錄影相關事件到事件流（前端據此出人話句＋toast）。

    影像的問題**不准影響飛行資料**，所以這裡自己吞例外：發不出事件也只是少一則
    通知，不能反過來把架次流程弄壞。"""
    if st is None or not st.drone_id:
        return
    try:
        from .ws import manager
        ev = await db.insert_event(st.drone_id, st.session_id, severity, type_,
                                   {"reason": reason})
        ev["drone"] = st.drone_name
        await manager.broadcast({"type": "event", "event": ev})
    except Exception:
        log.exception("影像：事件送出失敗（不影響飛行資料）")


def should_stop_for_landing(st, now: float) -> bool:
    """落地夠久了沒——**判準只有一份**，迴圈與離線測試共用（§8c）。

    四個條件缺一不可：
      1. 這一趟還在（`session_id`）；
      2. **曾經離地**——`on_ground` 一出現就停會把 arm→起飛那 10–25 秒殺掉；
      3. 現在飛控說在地上，而且**這個說法沒有過期**（`on_ground_since` 是在
         收到 `on_ground` 的那一刻設的；過期由呼叫端的 `landed_state` 判）；
      4. 已經停了就不再停（否則每秒對錄製器打一次 PATCH）。
    """
    return bool(
        st.session_id and st.airborne_seen and not st.landed_stopped
        and st.landed_state == "on_ground" and st.on_ground_since is not None
        and now - st.on_ground_since >= settings.video_landed_stop_s)


async def stop_for_landing(st) -> None:
    """落地滿 `VIDEO_LANDED_STOP_S` 秒 → 停止錄影，**但架次不收**。

    架次的邊界是 arm／disarm（那是「這一趟」的定義），錄影的邊界是飛行——
    落地之後停在地上 armed 著做檢查、看資料的那幾分鐘沒有錄的價值。
    **兩件事分開，才可以一個停一個不停。**

    只做一次（`landed_stopped`）：這個判斷在每秒的迴圈裡，不設旗標會每秒
    對錄製器打一次 PATCH。
    """
    if st is None or st.sysid is None or st.landed_stopped:
        return
    st.landed_stopped = True
    try:
        await set_record(st.sysid, False)
        log.info("影像：%s 落地滿 %.0f 秒，停止錄影（架次仍在）",
                 st.drone_name, settings.video_landed_stop_s)
        await _emit(st, "video_recording_stopped",
                    f"落地滿 {settings.video_landed_stop_s:.0f} 秒，錄影已停",
                    severity="info")
        for delay in (5.0, 15.0):
            await asyncio.sleep(delay)
            await sync_segments()
    except Exception:
        log.exception("影像：落地停錄失敗（不影響架次記錄）")


async def discard_if_never_airborne(session_id: str, sysid: int | None, st=None) -> None:
    """**確定沒離地**的那一趟，把影像刪掉（使用者定案 2026-09-08）。

    三種情況要分得開（flight-video-design §8c）：

    | `landed_state_seen` | `airborne_from` | 結論 | 影像 |
    |---|---|---|---|
    | true | 有值 | 飛過 | 留 |
    | true | NULL | **確定沒飛過** | **刪** |
    | false | — | **不知道有沒有飛** | **留** |

    **「不知道」不得觸發刪除**：飛控沒送 `landed_state`、或那一趟我們根本沒
    收到，都不能推論成「沒飛」（§0.2e 的同一條）。

    刪除**走錄製器自己的 API**，不是 backend 去動 `/rec`——那個目錄 backend
    是唯讀掛載，而且「寫入是 uav-video 的職責」。刪掉的要發事件，不能悄悄消失。
    """
    if sysid is None:
        return
    try:
        row = await db.airborne_of_session(session_id)
        if not row or row.get("video_mode") != "on":
            return                                   # 本來就沒錄，沒有東西要刪
        if not row.get("landed_state_seen"):
            log.info("影像：架次 %s 沒收到過 landed_state——不知道有沒有飛，"
                     "影像照留", session_id[:8])
            return
        # **失聯收尾的那一趟一律留。** 我們是在「看不到它」的情況下收的架次，
        # 而 armed 之後、失聯之前它可能還沒起飛——`airborne_from` 是 NULL 只
        # 代表**我們沒看到起飛**，不代表沒起飛。§0.2e：不知道 ≠ 沒有
        if row.get("end_reason") in ("telemetry_lost", "telemetry_lost_backfilled"):
            log.info("影像：架次 %s 是失聯收尾的——沒看到起飛不等於沒起飛，影像照留",
                     session_id[:8])
            return
        if row.get("airborne_from") is not None:
            return                                   # 飛過了
        n = await _delete_segments(session_id, sysid)
        await db.pool.execute(
            "UPDATE flight_sessions SET video_mode = 'discarded' WHERE id = $1",
            session_id)
        await _emit(st, "video_discarded",
                    f"這一趟從未離地（飛控說全程 on_ground），已刪除 {n} 段影像",
                    severity="info")
        log.info("影像：架次 %s 從未離地，刪除 %d 段", session_id[:8], n)
    except Exception:
        log.exception("影像：未離地影像清理失敗（不影響架次記錄）")


async def _delete_segments(session_id: str, sysid: int) -> int:
    """刪掉這一趟時間範圍內的錄影片段，並把 `video_segments` 那幾列一併移除。

    以**資料庫裡已入庫的片段**為準：它們的 `started_at` 就是錄製器要的
    `start`，不必自己解析檔名（檔名的 strftime 是寫檔當下的牆鐘）。
    還沒入庫的先同步一次再刪。
    """
    await sync_segments()
    rows = await db.pool.fetch(
        "SELECT started_at FROM video_segments WHERE session_id = $1", session_id)
    name, n = await path_of(sysid), 0
    if name is None:
        await db.pool.execute("DELETE FROM video_segments WHERE session_id = $1", session_id)
        return 0
    for r in rows:
        start = r["started_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            await _api(f"{API}/v3/recordings/deletesegment"
                       f"?path={name}&start={start}", "DELETE")
            n += 1
        except Exception as e:
            log.warning("影像：刪除片段 %s 失敗（%s）", start, type(e).__name__)
    await db.pool.execute("DELETE FROM video_segments WHERE session_id = $1", session_id)
    return n


async def on_session_end(sysid: int | None, st=None) -> None:
    """架次結束：延遲收錄——收尾片段還在寫，馬上關會切掉最後幾秒。"""
    if sysid is None:
        return
    try:
        if st is not None:
            st.video_mode = None      # 架次結束＝沒有「當前錄影現況」可言
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
        name = path_for(drone_id)
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


async def ensure_sources() -> None:
    """確保每台有相機的機，錄製器那邊的「去哪裡拉」還在。

    **MediaMTX 的 API 改動不寫回唯讀設定檔**——這件事 `set_record` 的註解裡
    早就寫過，但當時只補了 `record`。2026-09-23 開 HLS 重啟 uav-video 時發現
    **連來源也一起消失了**：path 直接 404，而畫面只會安靜地變成「沒有影像」，
    沒有任何東西說「我不知道要去哪裡拉」。

    以資料庫為事實源定期補回。`set_source` 自己會處理「正在錄的時候不要把
    on-demand 設回來」，這裡不必重複那個判斷。
    """
    try:
        rows = await db.pool.fetch(
            "SELECT id::text AS id, camera_url FROM drones "
            "WHERE camera_url IS NOT NULL AND camera_url <> ''")
    except Exception:
        log.exception("影像：查相機來源失敗（不影響飛行資料）")
        return
    for r in rows:
        name = path_for(r["id"])
        c = await _conf(name)
        src = (c or {}).get("source") or ""
        if c is None or not src.strip() or src == "publisher":
            if await set_source(r["id"], r["camera_url"]):
                log.info("影像：補回 %s 的來源——錄製器重啟過，設定沒留下來", name)


async def reconcile() -> None:
    """確保「正在飛的機」確實在錄。

    需要這個是因為錄製器的 API 改動不寫回設定檔：uav-video 一重啟就全部回到
    record: no，飛行中就會**靜默停錄**。定期補回比事後才發現沒錄好。
    """
    if not settings.video_record_enabled:
        return
    from .state import fleet
    for st in list(fleet.values()):
        if not (st.armed and st.sysid):
            continue
        # **落地已經停過的不要再打開。** 這裡的條件是「armed」，而落地停錄
        # 之後架次還開著（飛機停在地上 armed 著）——不擋的話 30 秒後這一圈
        # 就把剛停掉的錄影又打開，新的停止條件等於白做
        if st.landed_stopped:
            continue
        await set_record(st.sysid, True)
        if st.video_mode != "on":
            continue
        # 飛行中來源斷了＝錄影中斷。**只在狀態變化時發事件**（去抖），否則每
        # 30 秒一則會淹掉事件流。恢復時也發一則，讓時間軸看得出中斷區間。
        ready = await stream_ready(st.sysid)
        was_ok = _stream_ok.get(st.sysid, True)
        if not ready and was_ok:
            await _emit(st, "video_recording_failed", "影像來源中斷，錄影暫停")
        elif ready and not was_ok:
            await _emit(st, "video_recording_resumed", "影像來源恢復，錄影續錄",
                        severity="info")
        _stream_ok[st.sysid] = ready


async def loop() -> None:
    """週期任務：片段入庫＋錄製狀態校正。整段包例外——影像的問題不准
    影響其他迴圈（同 _broadcast_loop 的紀律）。"""
    # **開機先補一次**，不要等 30 秒：地面站重啟時常常是連著 uav-video 一起
    # 重啟的，而那正是來源會消失的時機
    try:
        await ensure_sources()
    except Exception:
        log.exception("影像：開機補來源失敗（不影響飛行資料）")
    while True:
        await asyncio.sleep(SYNC_S)
        try:
            await ensure_sources()
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
