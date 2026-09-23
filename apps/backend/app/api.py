import bisect
from collections import deque
import asyncio
import io
import tarfile

import asyncpg
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

import logging

import buildings
import mission_time        # libs/ 的共用實作（PYTHONPATH=/srv/libs）
import plan_check
import terrain

from . import (agent_link, captures, chainage, db, groups, logindex,
               mavlink_rx, modem_raw, signing)
from .config import settings
from .ws import manager

log = logging.getLogger("app.api")
from .jsonsafe import dumps as jdumps
from .link_events import transition as link_transition
from .state import live

router = APIRouter(prefix="/api")


@router.get("/drones")
async def list_drones():
    from .state import fleet
    from .dialect import autopilot_name
    rows = await db.pool.fetch("SELECT * FROM drones ORDER BY created_at")
    out = []
    for r in rows:
        d = dict(r)
        # autopilot：從即時 fleet 帶（runtime，非 DB 欄位）；沒連過 MAVLink＝null
        st = fleet.get(str(d["id"]))
        d["autopilot"] = (autopilot_name(st.autopilot_raw)
                          if st and st.autopilot_raw is not None else None)
        # 038：韌體版本與板子 UID 也是 runtime 值（非 DB 欄位）。
        # 一致性測試靠它把「對哪一版驗過」寫進證據——沒有版本的證據，
        # 等於宣稱「驗過」卻說不出驗的是什麼。
        # runtime 優先、**DB 值墊底**：這兩項是板子的穩定屬性，機此刻沒連線
        # 不代表我們不知道它是誰。原本無條件覆寫，會把已持久化的身分在離線時
        # 抹成 null——前端就會宣告「無板子 UID」，那是假話
        if st and st.flight_sw_version:
            d["flight_sw_version"] = st.flight_sw_version
        if st and st.board_uid:
            d["board_uid"] = st.board_uid
        # 意圖通道現況（協定 §4.2 的鏡像）。**整頁載入時就要有值**——
        # 只靠 WS 推播的話，剛打開頁面到下一拍之間是空白，而空白會被讀成
        # 「沒有代理」，那與「代理在、只是還沒推」是兩件事
        al = agent_link.links.get(d.get("board_uid"))
        d["agent"] = al.as_dict() if al else None
        out.append(d)
    return out


class DroneIn(BaseModel):
    name: str
    connection_url: str | None = None
    note: str | None = None
    # ── 人工維護的身分（038 兩層模型的下半）────────────────────
    # 機器可驗證的那層（sysid／board_uid／韌體版本）由機體自報，**不在此填**：
    # 人填只會製造第二個事實來源。這兩欄是機器問不到的東西。
    airframe_serial: str | None = None    # 機架序號（貼在機身上的那個）
    model: str | None = None              # 型號，如 "X500 v2"


@router.post("/drones")
async def register_drone(d: DroneIn):
    """註冊一台無人機（真機階段用；模擬機由 backend 啟動時自動註冊）。"""
    row = await db.pool.fetchrow(
        """
        INSERT INTO drones (name, serial_no, is_simulated, connection_url, status,
                            airframe_serial, model)
        VALUES ($1, $1, false, $2, 'idle', $3, $4)
        ON CONFLICT (serial_no) DO NOTHING
        RETURNING *
        """,
        d.name, d.connection_url,
        (d.airframe_serial or "").strip() or None,
        (d.model or "").strip() or None,
    )
    if row is None:
        raise HTTPException(409, f"名稱 {d.name} 已存在")
    return dict(row)


class DronePatch(BaseModel):
    name: str | None = None
    video_url: str | None = None      # 空字串＝清除
    airframe_serial: str | None = None   # 空字串＝清除
    model: str | None = None             # 空字串＝清除
    #: 槳徑（mm）。**不參與任何判定**——見 issues/048 第 4 項與
    #: `db.migrate` 那段註解。0／null＝沒填
    prop_diameter_mm: int | None = None


#: 入列狀態（issues/040 A2／`doc/drone-admission-protocol.md` §3）。
#: **只有 `admitted` 可以被指揮。**
ADMISSION_STATES = ("seen", "identifying", "reassigning", "admitted",
                   "admitted_offline", "quarantined", "unmanaged")


@router.get("/captures", tags=["原始層"])
async def list_captures():
    """地面站自己錄的 tlog 一覽（issues/014）。

    **事實來源是 `captures` 表，不是目錄。** 但這一層的檔案是 `capture.py`
    每天換檔寫出來的，沒有一個「建檔時機」可以掛登錄——所以進來時先對帳一次
    （目錄裡有什麼，表裡就有什麼）。**這個端點是人按出來的，不是熱路徑。**

    tlog 與 QGC 回放、`pymavlink` 的 `mavlogdump.py` 相容——所以「取得檔案」
    就是取得全部，不需要我們再做一套檢視器。
    """
    await captures.reconcile_ground()
    out = await captures.listing("ground")
    if not out["files"]:
        out["note"] = "錄製目錄裡沒有檔案——原始層可能沒有在錄（檢查 CAPTURE_* 設定）"
    return out


@router.get("/captures/{name}", tags=["原始層"])
async def get_capture(name: str):
    """下載一份地面站錄製檔。

    **白名單是「它必須是 `captures` 表裡的一列」**，路徑從那一列讀出來
    ——不是把使用者給的字串拼進路徑裡。`../` 這種東西不該靠字串檢查擋。
    """
    f = await captures.find("ground", name)
    if f is None:
        raise HTTPException(404, f"沒有這份錄製檔：{name}")
    return FileResponse(str(f), media_type="application/octet-stream",
                        filename=f.name)


# ── 機上錄製的自動回傳（issues/014）──────────────────────────────
#
# **地面站錄的是「送到地面站的東西」，機上錄的是「飛控送出的東西」。**
# 兩者相差的正是 5G 斷線的那一段——所以清單刻意分開列（混成一張表就把那個
# 差抹掉了），但**存在同一張 `captures` 表的兩個 tier**：要能用一句 SQL 把
# 兩層對起來，它們就必須在同一張表裡。
#
# 守門在機上（uav-agent `uploader.py`：只在地面傳、一解鎖立刻停）：這裡收得下
# 多少不是問題，**問題是它與遙測共用同一條 5G**，而現在正在飛的那台優先。


class OnboardOffer(BaseModel):
    """機上宣告「我有這個檔案要回傳」。"""
    board_uid: str
    name: str
    bytes: int = Field(gt=0)
    sha256: str


class OnboardAbandoned(BaseModel):
    """機上把一份從來沒有回傳成功的錄製滾動刪掉了。"""
    board_uid: str
    name: str
    bytes: int = 0
    at: float | None = None


async def _drone_of_board(board_uid: str) -> str:
    """board_uid → drone_id。**鍵是板號不是 sysid**（issues/038／040）。

    找不到就 404：**沒有身分的東西不該在我們的磁碟上長出目錄**。
    """
    link = agent_link.links.get(board_uid)
    drone_id = link.drone_id if link else None
    if drone_id is None:
        row = await db.pool.fetchrow(
            "SELECT id::text AS id FROM drones WHERE board_uid = $1", board_uid)
        drone_id = row["id"] if row else None
    if drone_id is None:
        raise HTTPException(404, f"不認得的 board_uid {board_uid}")
    return drone_id


@router.post("/onboard-captures/offer", tags=["原始層"])
async def onboard_offer(body: OnboardOffer):
    """宣告一份要回傳的機上錄製，回覆「我已經有幾個 byte」。

    **這一步就是續傳的全部機制。** 機端不必記得自己傳到哪裡——重開機、
    換行程、狀態檔掉了，重新宣告一次就知道從哪裡接。**認「同一份」用
    sha256 不是檔名**：機上的 RTC 沒有電池，冷開機的檔名真的會重複。
    """
    drone_id = await _drone_of_board(body.board_uid)
    try:
        return await captures.offer(drone_id, body.name, body.bytes, body.sha256)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.put("/onboard-captures/chunk", tags=["原始層"])
async def onboard_chunk(request: Request, board_uid: str, name: str,
                        offset: int):
    """收一塊（body 是原始位元組）。滿了就驗 sha256 並收尾。

    **位移不符回 409 並帶上我方的真值**，讓機端自己對回來——比起「重傳整份」，
    這條路在 5G 抖動時便宜得多，而抖動在本場域是常態。
    """
    drone_id = await _drone_of_board(board_uid)
    data = await request.body()
    if not data:
        raise HTTPException(422, "空的塊")
    if len(data) > captures.MAX_CHUNK:
        raise HTTPException(413, f"一塊最多 {captures.MAX_CHUNK} bytes")
    try:
        return await captures.append(drone_id, name, offset, data)
    except captures.Conflict as e:
        raise HTTPException(409, {"code": "offset_mismatch", "have": e.have,
                                  "msg": str(e)})
    except captures.NoSpace as e:
        # 507＝地面站的問題，不是機端的。機端該做的是稍後再試，**不是放棄**
        raise HTTPException(507, {"code": "no_space", "msg": str(e)})
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/onboard-captures/abandoned", tags=["原始層"])
async def onboard_abandoned(body: OnboardAbandoned):
    """立一塊墓碑，並讓它進事件流。

    **機上的滾動優先於保住紀錄**（卡滿了會讓整台機出問題，包括代理自己），
    所以這件事會發生、而且是對的。但**它是永久的**：那一趟的機上紀錄從此
    不存在。只寫進機上的 log 等於沒說——journald 會被清掉，而**清單裡少了
    一趟，與「那一趟沒有飛」在畫面上完全同形**。
    """
    drone_id = await _drone_of_board(body.board_uid)
    at = body.at or time.time()
    try:
        res = await captures.abandoned(drone_id, body.name, body.bytes, at)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if res.get("noted"):
        try:
            ev = await db.insert_event(
                drone_id, None, "warn", "onboard_capture_lost",
                {"name": body.name, "bytes": body.bytes,
                 "msg": f"機上錄製 {body.name} 未回傳即被滾動刪除"})
            await manager.broadcast({"type": "event", "event": ev})
        except Exception:
            log.exception("遺失事件寫入失敗（墓碑已經立了）")
    return res


@router.get("/onboard-captures/coverage", tags=["原始層"])
async def onboard_coverage(session_id: str):
    """一個架次的兩層覆蓋：地面站瞎掉的那幾段，機上補到了嗎。

    **這是兩層並存的全部理由，所以它要能被檢驗。** 失明區間來自 `blackouts`
    表，機上覆蓋來自 `captures.covers_from/to`（收尾驗章時順手掃出來的）
    ——兩邊都是資料庫裡的列，所以這是一句 SQL 對得起來的事，
    不是把一個目錄讀進記憶體才答得出來的事。

    `covered` 三態：`true`／`false`／**`null`＝不知道**（這台機根本沒有任何
    機上錄製）。**「不知道」不寫成「沒補到」**——後者是一個結論，而我們沒有
    做出它的依據。
    """
    row = await db.pool.fetchrow(
        "SELECT s.id::text AS id, s.drone_id::text AS drone_id, d.name AS drone_name, "
        "extract(epoch FROM s.started_at) AS t0, "
        "extract(epoch FROM s.ended_at) AS t1 "
        "FROM flight_sessions s JOIN drones d ON d.id = s.drone_id "
        "WHERE s.id = $1::uuid", session_id)
    if row is None:
        raise HTTPException(404, "無此架次")
    t0 = float(row["t0"])
    t1 = float(row["t1"]) if row["t1"] is not None else time.time()

    outs = await db.pool.fetch(
        "SELECT extract(epoch FROM started_at) AS a, "
        "extract(epoch FROM ended_at) AS b, reason, recovered_by "
        "FROM blackouts WHERE drone_id = $1::uuid "
        "AND started_at <= to_timestamp($3) "
        "AND coalesce(ended_at, now()) >= to_timestamp($2) "
        "ORDER BY started_at", row["drone_id"], t0, t1)

    # 這台機、與這段時間有重疊的機上錄製
    spans = await db.pool.fetch(
        "SELECT name, bytes, extract(epoch FROM covers_from) AS a, "
        "extract(epoch FROM covers_to) AS b FROM captures "
        "WHERE tier = 'onboard' AND drone_id = $1::uuid AND status = 'complete' "
        "AND covers_from IS NOT NULL AND covers_from <= to_timestamp($3) "
        "AND covers_to >= to_timestamp($2) ORDER BY covers_from",
        row["drone_id"], t0, t1)
    any_mine = await db.pool.fetchval(
        "SELECT count(*) FROM captures WHERE tier = 'onboard' "
        "AND drone_id = $1::uuid", row["drone_id"])

    def covered(a: float, b: float) -> bool | None:
        if not spans:
            # **一份機上錄製都沒有 → 不知道，不是「沒補到」。** 這台機可能
            # 根本沒有代理、代理太舊、或那一份還在機上等著傳——三種情況都
            # 不等於「我們確認過那一段沒有備份」
            return None if not any_mine else False
        # **要整段被蓋住才算補到。** 蓋一半就宣告「補到了」，等於把一個
        # 仍然存在的洞說成已經填平
        return any(float(f["a"]) <= a and float(f["b"]) >= b for f in spans)

    blackouts = []
    for o in outs:
        a = max(float(o["a"]), t0)
        b = min(float(o["b"]) if o["b"] is not None else t1, t1)
        blackouts.append({"from": a, "to": b, "seconds": round(b - a, 1),
                          "reason": o["reason"],
                          "recovered_by": o["recovered_by"],
                          "covered_onboard": covered(a, b)})
    return {
        "session_id": row["id"], "drone_id": row["drone_id"],
        "drone_name": row["drone_name"],
        "from": t0, "to": t1, "ended": row["t1"] is not None,
        "blackouts": blackouts,
        "onboard": [{"name": f["name"], "bytes": f["bytes"],
                     "covers": {"from": float(f["a"]), "to": float(f["b"])},
                     "url": f"/api/onboard-captures/{row['drone_id']}/{f['name']}"}
                    for f in spans],
        "onboard_known": bool(spans),
    }


@router.get("/onboard-captures", tags=["原始層"])
async def list_onboard_captures():
    """已回傳的機上錄製一覽。

    **「自動回傳」如果看不到，就跟 scp 沒有兩樣**——差別只在誰按的。
    半成品與墓碑也列：「傳到一半」「根本沒傳」「已經永遠沒了」三者要做的事
    完全不同，而在畫面上它們同形。
    """
    return await captures.listing("onboard")


@router.get("/onboard-captures/{drone_id}/{name}", tags=["原始層"])
async def get_onboard_capture(drone_id: str, name: str):
    """下載一份回傳回來的機上錄製。

    **白名單是「它必須是 `captures` 表裡的一列」**，路徑從那一列讀出來。
    """
    f = await captures.find("onboard", name, drone_id)
    if f is None:
        raise HTTPException(404, f"沒有這份機上錄製：{drone_id}/{name}")
    return FileResponse(str(f), media_type="application/octet-stream",
                        filename=f.name)


# ── 錄製檔的摘要索引（2026-09-07）─────────────────────────────
#
# **推翻了 `/captures` 上那句「取得檔案就是取得全部，不需要我們再做一套
# 檢視器」。** 那句話在「要不要重做一個回放器」上仍然是對的，但它擋掉了一個
# 每次飛完都會問的問題：這份檔裡有什麼？而回答它現在的代價是下載 116 MB、
# 裝 pymavlink、記得 mavlogdump 的參數——**能力沒有缺，只是遠**。
#
# 兩層共用同一支解析（`logindex.py`）：白名單一樣是「它必須是 captures 表裡
# 的一列」，路徑從那一列讀出來，不把使用者給的字串拼進路徑。


async def _index_response(f, refresh: bool):
    """共用的索引回應：做好了回 200，還在做回 202＋進度。"""
    if refresh:
        logindex.cache_path(f).unlink(missing_ok=True)
    try:
        status, data = await logindex.get_or_start(f)
    except Exception as e:                       # 解析炸掉要說出是哪一份
        log.exception("索引失敗：%s", f)
        raise HTTPException(500, f"這份檔的索引做不出來（{f.name}）：{e}")
    if status == "ready":
        return data
    # **202 不是錯誤**：大檔要 35 秒，前端據此顯示進度並回頭再問一次
    return JSONResponse(status_code=202, content={"status": "building", **data})


@router.get("/captures/{name}/index", tags=["原始層"])
async def ground_capture_index(name: str, refresh: bool = False):
    """地面站錄製的摘要索引（訊息型別／頻率／分布、STATUSTEXT、模式、曲線）。"""
    f = await captures.find("ground", name)
    if f is None:
        raise HTTPException(404, f"沒有這份錄製檔：{name}")
    return await _index_response(f, refresh)


@router.get("/onboard-captures/{drone_id}/{name}/index", tags=["原始層"])
async def onboard_capture_index(drone_id: str, name: str, refresh: bool = False):
    """機上錄製的摘要索引。內容與地面站那支相同——**兩層本來就該用同一把尺**。"""
    f = await captures.find("onboard", name, drone_id)
    if f is None:
        raise HTTPException(404, f"沒有這份機上錄製：{drone_id}/{name}")
    return await _index_response(f, refresh)


@router.get("/compare/chainage")
async def compare_chainage(a: str, b: str, plan_id: str | None = None,
                           grid_size: float | None = None,
                           max_offset_m: float = chainage.DEFAULT_MAX_OFFSET_M):
    """兩個架次沿路徑的訊號對照（issues/027）。

    **與前端 `lib/chainage.ts` 是同一套演算法**——搬到後端是為了讓 UI 與未來的
    `compare_flights` 共用一份邏輯，不養兩份（issues/019）。

    * `a`／`b`：兩個 `flight_sessions.id`
    * `plan_id`：**參考路徑用的計畫航點**。省略就退回用 A 那趟的軌跡，
      而回應的 `reference` 會說是 `trip_a`——**那讓 A 的偏航變成零誤差**，
      比較的意義因此打折，所以它必須看得見。
    * `grid_size`：省略＝依較稀那趟的樣本密度自適應（回應帶 `grid_size_used`）
    * `max_offset_m`：離參考路徑超過就捨棄並計數（不硬塞）

    **回應一律帶方法參數**（`grid_size_used`／`grid_size_source`／`reference`／
    `max_offset_m`／`dropped`／`paired_cells`）：
    **方法參數必須可見，否則結論無法被檢驗。**
    """
    async def samples(sid: str) -> list[dict]:
        rows = await db.pool.fetch(
            "SELECT lat, lon, sinr, rsrp FROM link_metrics "
            "WHERE session_id = $1::uuid AND lat IS NOT NULL AND lon IS NOT NULL "
            "ORDER BY time", sid)
        return [dict(r) for r in rows]

    ref = None
    if plan_id:
        rows = await db.pool.fetch(
            "SELECT lat, lon FROM waypoints WHERE plan_id = $1::uuid "
            "AND (lat <> 0 OR lon <> 0) ORDER BY seq", plan_id)
        ref = [dict(r) for r in rows]
    try:
        sa, sb = await samples(a), await samples(b)
    except Exception as e:
        raise HTTPException(422, f"架次 id 不合法或查詢失敗：{e}")
    out = chainage.compare_along_path(sa, sb, ref, grid_size, max_offset_m)
    out["sessions"] = {"a": a, "b": b}
    # **樣本數 0 要說得出是哪一邊**：兩邊都空與只有一邊空，要查的地方不同
    if not sa or not sb:
        out["note"] = (f"樣本數 a={len(sa)} b={len(sb)}——"
                       "有一邊沒有帶座標的訊號樣本，對照不會有內容")
    return out


@router.get("/admission/{sysid}")
async def admission_state(sysid: int):
    """這台機可不可以被指揮（issues/040 A2）。

    **由 backend 回答而不是 command 自己判斷**：入列要看的三樣東西——板號、
    代理連線、配號登錄——全都在這一側。command 只有 MAVLink router，
    它看得到號碼但看不到身分。

    **代理強制**（使用者 2026-09-02 裁定）：沒有代理的機一律 `unmanaged`，
    看得到、指不動。這不是降級處理，是明確分類。
    """
    from . import agent_link
    from .state import fleet
    st = next((s for s in fleet.values() if s.sysid == sysid), None)
    if st is None:
        return {"sysid": sysid, "state": "seen",
                "reason": "這個號碼上沒有任何遙測——不知道它是誰"}
    base = {"sysid": sysid, "drone_id": st.drone_id, "drone": st.drone_name,
            "board_uid": st.board_uid}
    if not st.identity_ok:
        return {**base, "state": "quarantined",
                "reason": st.identity_reason or "身分與記錄矛盾"}
    # **「連線中的代理」與「這台機有沒有代理」是兩件事。**（2026-09-04 裁定）
    # 意圖通道是 WebSocket，需要回程；而指令走 UDP，是另一條路——今天實測
    # 通道斷著的同時指令仍然送得到。原本兩者一律判 `unmanaged`（＝身分不明），
    # 於是**一條 TCP 斷掉就等於這台機失去了身分**，而它的板號、配號都還在。
    link = next((l for l in agent_link.links.values()
                 if l.drone_id and l.drone_id == st.drone_id and l.connected),
                None)
    #: 最後已知的那條（可能已斷）。`agent_link` 斷線時**不清空**這筆記錄，
    #: 所以它就是「這台機曾經有過代理」的證據
    last = link or next((l for l in agent_link.links.values()
                         if l.drone_id and l.drone_id == st.drone_id), None)
    if last is None:
        return {**base, "state": "unmanaged",
                "reason": "這台機沒有連線中的機上代理——本系統只指揮有代理的機"}
    # 換號中（A3）：**這不是失聯也不是身分矛盾，是我們自己叫它去換的**。
    # 排在 board_uid 檢查之前——重開飛控期間代理收不到 AUTOPILOT_VERSION，
    # 若先判 identifying，畫面會說「身分未定」而不是「換號中」，
    # 那會讓一個我們主動發起的動作看起來像故障
    re = (last.payload or {}).get("reassigning")
    if re:
        return {**base, "state": "reassigning", "reassigning": re,
                "reason": f"正在把號碼從 {re.get('from')} 換成 {re.get('to')}"
                          "（重開飛控中，稍候它會用新號碼回來）"}
    if not st.board_uid:
        return {**base, "state": "identifying",
                "reason": "還沒拿到飛控板 UID，身分未定"}
    row = await db.pool.fetchrow(
        "SELECT assigned_sysid FROM drones WHERE id = $1::uuid", st.drone_id)
    assigned = row["assigned_sysid"] if row else None
    if assigned is None:
        return {**base, "state": "identifying", "reason": "這塊板子還沒有配號"}
    if assigned != sysid:
        # 它在用一個不是配給它的號碼。**這不是隔離，是還沒換過來**——
        # 換號屬 A3，本階段只是不放行
        return {**base, "state": "identifying", "assigned_sysid": assigned,
                "reason": f"配給這塊板子的號碼是 {assigned}，它現在用 {sysid}"}
    if link is None:
        # **身分驗過了，只是現在問不到守門。** 板號、配號都對得上，變的只有
        # 那條 WebSocket。這一格與 `unmanaged`（從來沒有代理）刻意分開——
        # 前者該保留「把飛機帶回來」的能力，後者不該有任何能力。
        return {**base, "state": "admitted_offline", "assigned_sysid": assigned,
                "agent_version": last.agent_version,
                "last_state": last.state,
                "reason": "板號與配號都對得上，但**機上代理的意圖通道斷了**"
                          "——問不到機上守門，所以只放行把飛機帶回來的動作"
                          f"（最後已知狀態：{last.state or '不明'}）",
                "hint": "指令走的是另一條路（UDP），通道斷了不代表指令送不到。"
                        "要恢復完整指揮，先讓代理的意圖通道連回來"}
    return {**base, "state": "admitted", "assigned_sysid": assigned,
            "reason": "板號、配號、代理連線三者相符"}


class AgentHello(BaseModel):
    """機上代理上線時自報。**註冊是收到它的副作用**（issues/038、協定 §4.1）。

    只收**機器問得到**的東西。機架序號與型號不在這裡——代理知道的是板子，
    不是機架，那兩項只能由人維護。
    """
    board_uid: str                       # 飛控板 UID＝這是哪一架飛機
    agent_uid: str | None = None         # 樹莓派序號＝這是哪一台伴飛電腦
    autopilot: str | None = None
    fw: str | None = None                # 已解碼的韌體版本
    vehicle_type: int | None = None      # MAV_TYPE
    agent_version: str | None = None
    #: 這台機**現在**用的 sysid（040 A1）。**與配號是兩件事**：這是觀察，
    #: 回應裡的 `assigned_sysid` 才是決定。舊代理不送＝None，此時只能配號
    #: 給它、無法判斷「它跑錯號碼了」
    sysid: int | None = None
    #: 簽章金鑰的**指紋**（040 A5-c）。**永遠不是金鑰本身**——交換指紋是為了
    #: 在開簽章之前就發現兩邊不同，而簽章不符是靜默丟棄（設計 §3）
    signing_fp: str | None = None


@router.post("/agent/hello")
async def agent_hello(h: AgentHello):
    """代理上線：確保這塊飛控板有一筆機體記錄，沒有就自動建。

    **這取代了人工註冊。** 代理知道的（板子 UID、廠牌、韌體、機型）全部是
    機器問得到的，讓人打字只會打錯；而人該維護的（機架序號、型號、名稱）
    代理問不到。前端因此只需要**編輯**，不需要註冊表單。

    冪等：同一塊板子重複上線只會更新，不會長出第二筆。
    """
    uid = (h.board_uid or "").strip()
    if not uid:
        raise HTTPException(422, "board_uid 不可為空——沒有它就沒有穩定的身分")
    drone_id, name, created = await db.ensure_drone_by_board(
        uid, autopilot=h.autopilot, fw=h.fw, agent_uid=h.agent_uid,
        vehicle_type=h.vehicle_type,
        # A4：代理自報的號碼讓我們認得出「那筆佔位記錄就是它」，
        # 免得同一台飛機長出兩筆
        claimed_sysid=h.sysid)
    if created:
        # **新機出現不該是靜默的。** 自動化省掉的是打字，不是知情。
        ev = await db.insert_event(
            drone_id, None, "info", "drone_registered",
            {"drone": name, "board_uid": uid, "agent_uid": h.agent_uid,
             "autopilot": h.autopilot, "fw": h.fw,
             "note": "代理自報上線、系統自動建檔——請到無人機頁改名並補機架序號"})
        ev["drone"] = name
        await manager.broadcast({"type": "event", "event": ev})
        log.info("代理自動註冊：%s（board_uid=%s agent_uid=%s）",
                 name, uid, h.agent_uid)
    # ── 040 A1：配號（本階段只回答，不執法、不下發）─────────────────
    # **識別的唯一鍵值是板號**，sysid 只是地址——所以撞號不是「要隔離誰」的
    # 兩難，是換一個號碼。這裡把答案算出來回給代理；要不要照做、怎麼落到飛控，
    # 是 A3。A2 之前拿到 `change` 只用於顯示與留痕。
    assign = None
    try:
        assign = await db.allocate_sysid(uid, h.sysid)
        if assign["sysid"] is not None:
            # **`keep` 與 `change` 都要登錄。** 一度只在 keep 時寫，理由是
            # 「機端還沒改過來，登錄與實況會對不上」——但實測立刻打臉：兩塊
            # 板子先後連上、都被告知「改用 2」，因為第一次的 change 沒有登錄，
            # 2 仍然是空的。**配號不登錄就不是配號，只是建議。**
            #
            # 而「登錄與實況對不上」根本不是問題，是**設計**：`assigned_sysid`
            # 是我們的決定、`mav_sysid` 是觀察到的事實，**兩欄分開的全部理由
            # 就是讓它們能夠不一致**——那個不一致正是「它還沒改過來」。
            await db.record_assignment(uid, assign["sysid"], drone_id)
        if assign["action"] == "change":
            log.warning("配號不符：%s（board_uid=%s）→ %s",
                        assign["reason"], uid, assign["sysid"])
            ev = await db.insert_event(
                drone_id, None, "warn", "sysid_reassign_needed",
                {"board_uid": uid, "claimed": h.sysid,
                 "assigned": assign["sysid"], "reason": assign["reason"],
                 "note": "本階段只通知不下發——改號要重開飛控，屬 issues/040 A3"})
            ev["drone"] = name
            await manager.broadcast({"type": "event", "event": ev})
    except Exception:
        # **配號失敗不擋註冊**：註冊本身是身分的地基，讓它因為配號出錯而失敗，
        # 等於為了修好號碼把整台機擋在門外
        log.exception("配號失敗（不影響註冊）board_uid=%s", uid)

    # ── A5-c：簽章金鑰的指紋自檢 ──────────────────────────────────
    # **這一批還沒有在簽任何東西**，所以措辭一律是「尚未啟用」。先做偵測的
    # 理由：簽章不符是**靜默丟棄**——金鑰一旦不對，畫面上一切正常而指令全部
    # 消失。偵測要先於啟用，不能反過來。
    sign = signing.check(uid, h.signing_fp)
    if sign["state"] in ("mismatch", "agent_missing", "ground_missing"):
        log.error("⚠ 簽章金鑰自檢：%s（board_uid=%s）——%s",
                  sign["state"], uid, sign["reason"])
        try:
            ev = await db.insert_event(
                drone_id, None, "warn", "signing_key_check",
                {**sign, "board_uid": uid,
                 "note": "線上尚未啟用簽章，所以現在不影響飛行；"
                         "但在啟用之前必須修好，否則屆時是靜默丟棄"})
            ev["drone"] = name
            await manager.broadcast({"type": "event", "event": ev})
        except Exception:
            log.exception("簽章自檢事件寫入失敗")

    out = {"drone_id": drone_id, "name": name, "created": created,
           "signing": sign["state"], "signing_reason": sign["reason"],
           # **只回指紋，永遠不回金鑰。** 代理用它確認自己手上那把對不對
           "signing_fp": sign.get("ground_fp")}
    if assign:
        out["assigned_sysid"] = assign["sysid"]
        out["sysid_action"] = assign["action"]      # keep / change
        out["sysid_reason"] = assign["reason"]
    return out


class GuardIn(BaseModel):
    #: params 裡帶 dry_run:true ＝**只問判決不動飛機**。加這個是因為要驗證
    #: 守門「放行」那幾格，本來一定得真的下指令——2026-08-25 就對一台停在
    #: 地上的真機送出了 RTL／LAND／LOITER，只為了確認守門會放行。
    #: **驗證不該需要動到飛機。**
    """指令服務執行飛行操作前，先問機上守門（協定 §5.2、丙案分工）。"""
    #: 協定訊息型別：intent（問守門／要提案）／decision（人的確認）／
    #: progress（序列逐步回報）。**分開而不是塞進 action**：它們是不同的事，
    #: 混在一個欄位裡，冪等鍵就會把 decision 之後的 progress 當成重送吞掉
    kind: str = "intent"
    action: str
    drone_id: str | None = None
    board_uid: str | None = None
    intent_id: str | None = None
    params: dict | None = None


@router.post("/agent/intent")
async def agent_intent(body: GuardIn):
    """把意圖送給機上代理：守門在那邊，執行看動作分工。

    **這個端點由指令服務呼叫，不是由前端。** 守門如果只在前端問，那它就不是
    守門——前端可以不問、也可能有別的呼叫端（驗收 rig、MCP、curl）。
    要擋得住，就得擋在真的會動到飛機的那條路上。

    **沒有代理不等於不能飛**：這台機可能根本沒裝代理（他人的 QGC、SITL）。
    那種情況回 `no_agent`，由呼叫端沿用自己原本的檢查——守門是額外一層，
    不是唯一一層。但**代理在、卻問不到**（逾時）要當成拒絕：那是「不知道
    守門怎麼說」，而不知道在飛安路徑上就是不行。

    **有代理但失聯中回 `queued`**（039 複裁 G）：操作不送出、壓進佇列，
    鏈路恢復時重新問一次判決並攤給人確認。這與 `no_agent` 分開，是因為原本
    兩者同形——而「這台機沒裝代理」與「這台機失聯了」在飛安上完全不同。

    > **已知限制**：`links` 是行程內的記憶體登錄表。地面站重啟後，一台失聯中
    > 的機會退回 `no_agent`（＝放行沿用本地檢查），因為那條 hello 從來沒進來
    > 過。要修就得把代理登錄持久化，屬 issues/033 的範圍。
    """
    import uuid as _uuid
    from . import agent_link
    link = None
    if body.board_uid:
        link = agent_link.links.get(body.board_uid)
    elif body.drone_id:
        link = next((l for l in agent_link.links.values()
                     if l.drone_id == body.drone_id), None)
    if body.kind not in ("intent", "decision", "progress"):
        raise HTTPException(422, f"不認得的協定型別 {body.kind}")
    if link is None:
        return {"verdict": "no_agent",
                "reason": "這台機沒有機上代理，守門這一層不存在"}
    if not link.connected:
        # **有代理但失聯中**——這與「沒有代理」是兩件事，原本兩者都回
        # `no_agent`（＝放行沿用本地檢查）。039 複裁 G：壓下來，恢復後補送。
        iid = body.intent_id or str(_uuid.uuid4())
        if body.kind != "intent":
            # decision／progress 是「已經開始的那件事」的後續，壓下來沒有意義：
            # 那件事在機上早就因為失聯自己收尾了（協定 §6）
            return {"verdict": "unknown", "intent_id": iid,
                    "reason": "意圖通道失聯中，這則後續送不出去"}
        if body.action not in (link.vets or []):
            return {"verdict": "no_agent",
                    "reason": f"機上代理（{link.agent_version}）沒有宣告守 "
                              f"{body.action}，守門這一層不存在"}
        n, dropped = agent_link.queue_intent(link, body.action, body.params, iid)
        return {"verdict": "queued", "intent_id": iid, "pending": n,
                "dropped": dropped,
                "reason": "這台機的意圖通道失聯中，操作**沒有送出去**。"
                          "已經記下來，鏈路恢復後會重新問一次判決並攤給你確認"
                          "——不會自動執行"}
    # **版本協商**：代理在 hello 裡宣告它守哪些意圖（`vets`）。舊版代理沒有
    # 這個欄位，也不會回 event——不先問清楚就送過去，只會等到逾時，然後
    # 「不知道＝不行」把所有飛行操作擋死。**能力宣告要用問的，不要用試的。**
    # decision／progress 是**已經開始的那件事的後續**，守門在 intent 那一關
    # 已經問過了；這裡只檢查 intent
    if body.kind == "intent" and body.action not in (link.vets or []):
        # **分辨「代理不守這個」與「根本沒有這個意圖」**：前者是版本差異、
        # 該放行讓本地檢查接手；後者是呼叫端寫錯，放行等於把一個打錯的字
        # 當成合法操作放過去
        known = set().union(*(l.vets or [] for l in agent_link.links.values())) \
            if agent_link.links else set()
        if known and body.action not in known:
            raise HTTPException(422, f"不認得的意圖 {body.action}")
        return {"verdict": "no_agent",
                "reason": f"機上代理（{link.agent_version}）沒有宣告守 "
                          f"{body.action}，守門這一層不存在"}
    iid = body.intent_id or str(_uuid.uuid4())
    try:
        ev = await agent_link.send_intent(link, body.action, body.params, iid,
                                          kind=body.kind)
    except TimeoutError:
        return {"verdict": "unknown", "intent_id": iid,
                "reason": "代理沒有在時限內回覆守門判決——**不知道不等於可以**"}
    except ConnectionError as e:
        return {"verdict": "unknown", "intent_id": iid, "reason": str(e)}
    kind = ev.get("event")
    verdict = {"guard_refused": "refused", "cleared": "cleared",
               "proposal": "cleared", "sent": "done", "noted": "done",
               "would_execute": "would_execute",
               "cancelled": "cancelled", "failed": "failed"}.get(kind, kind)
    return {"verdict": verdict, "intent_id": iid, "event": ev,
            "reason": ev.get("reason"), "state": ev.get("state")}


class BackfillSample(BaseModel):
    """機上緩衝的一筆取樣。**欄位刻意等同 telemetry 表**——補傳不是另一種
    資料，它就是那段時間我們本來該收到的資料。"""
    t: float                          # 機上的 unix 秒（浮點）
    lat: float | None = None
    lon: float | None = None
    alt_msl: float | None = None
    alt_rel: float | None = None
    heading: float | None = None
    ground_speed: float | None = None
    battery_pct: float | None = None
    battery_voltage: float | None = None
    gps_fix: int | None = None
    satellites: int | None = None
    flight_mode: str | None = None
    armed: bool | None = None


#: 補傳樣本的時間戳下限（2025-01-01Z）。**機端的時鐘不可信**：機上 Pi 的 RTC
#: 沒有電池，每次冷開機系統時間都從 1970 起算、靠 NTP 修正——而 NTP 走 5G，
#: 正是斷線期間不通的那一條。取樣條件（板子 UID 來自 UART、gs_link_ok 預設
#: False）在 NTP 同步**之前**就成立，所以一批補傳裡可能混著 1970 的時間戳。
BACKFILL_MIN_T = 1735689600.0
#: 未來多少秒內還算合理（時鐘小偏差）。再遠就是壞掉的時鐘，不是慢半拍
BACKFILL_FUTURE_S = 120.0
#: 補傳去重的時間窗（秒）。**不是精確比對**——即時那條路的時間戳是「收到
#: 封包的時刻」（逐筆漂移），補傳是「機上 1Hz 取樣的刻度」（整齊的固定小數），
#: 兩者永遠不會落在同一個百分秒。
#:
#: **0.6 不是 0.5**：即時入庫的間隔實測是 1.01 秒（逐筆漂移），所以最壞情況
#: 下一個補傳樣本離最近的即時列有 0.505 秒。用 0.5 會讓那一筆漏網——
#: 實測 10 筆裡漏了 1 筆（0.563 秒）。0.6 涵蓋得住，又遠小於真正的缺口
#: （真的斷線是好幾秒到好幾分鐘），不會把該補的洞也吃掉。
DEDUP_WINDOW_S = 0.6


class BackfillIn(BaseModel):
    """C 層：代理把 5G 斷線期間的取樣補傳上來。"""
    board_uid: str
    samples: list[BackfillSample] = Field(min_length=1, max_length=5000)
    #: 這段期間機體有沒有一直保持 armed（代理看得到，地面站看不到）。
    #: D 層用它判斷「回來之後還算不算同一趟」
    stayed_armed: bool | None = None


@router.post("/telemetry/backfill")
async def telemetry_backfill(body: BackfillIn):
    """**這段資料本來就沒有遺失，只是送不出來。**

    5G 斷線時代理照樣看得到飛控的一切；現在我們把它丟掉。這個端點讓它補回來。

    三條紀律：

    * **標記 `backfilled`**。它的時間戳是機上的（可能與地面站有偏差），
      而且它不該觸發任何即時判斷。不標的話，事後分不出哪些是後補的。
    * **不碰即時狀態**。補傳不更新 `live`、不發事件、不改 `connected`——
      那是過去的資料，讓它影響「現在」就是把歷史當成現況。
    * **時間戳去重**。重連後代理可能重送一段，同一秒的資料以先到的為準。
    """
    from . import agent_link
    link = agent_link.links.get(body.board_uid)
    drone_id = link.drone_id if link else None
    if drone_id is None:
        row = await db.pool.fetchrow(
            "SELECT id::text AS id FROM drones WHERE board_uid = $1",
            body.board_uid)
        drone_id = row["id"] if row else None
    if drone_id is None:
        raise HTTPException(404, f"不認得的 board_uid {body.board_uid}")

    # **時間戳先過濾，再拿去算範圍。** 混進一筆 1970 的樣本，`lo` 就變成 1970，
    # 而下面兩條範圍查詢會因此掃過整段歷史——最嚴重的是 blackouts 那條
    # UPDATE：它會把這台機**有史以來每一段失明記錄**都標成「已補回」，
    # 而那些洞其實從來沒有被補上。壞資料進來一次，歷史就永遠說了謊。
    now_t = time.time()

    def plausible(t: float) -> bool:
        return BACKFILL_MIN_T <= t <= now_t + BACKFILL_FUTURE_S

    good = [s for s in body.samples if plausible(s.t)]
    rej = [s.t for s in body.samples if not plausible(s.t)]
    bad = len(rej)
    if bad:
        # **不安靜地丟**：回報幾筆、範圍多少，機端才查得出自己的時鐘出了事
        log.warning("補傳丟掉 %d 筆時間戳不合理的樣本（%s，最早 %.0f、最晚 %.0f）"
                    "——機上時鐘可能還沒與 NTP 同步", bad, body.board_uid,
                    min(rej), max(rej))
    if not good:
        raise HTTPException(422, {
            "code": "implausible_timestamps",
            "msg": f"{len(body.samples)} 筆樣本的時間戳全部不合理"
                   f"（下限 {BACKFILL_MIN_T:.0f}）——機上時鐘還沒對過時，"
                   "補上來只會在歷史裡長出假資料",
            "rejected": bad})
    lo = min(s.t for s in good)
    hi = max(s.t for s in good)
    # 這段時間屬於哪個架次？**用時間去找**，不是用「現在的架次」——補傳的
    # 是過去的資料，而那時開著的架次可能已經因為失聯被收掉了
    sess = await db.pool.fetchrow(
        "SELECT id::text AS id FROM flight_sessions "
        "WHERE drone_id = $1::uuid AND started_at <= to_timestamp($2) "
        "AND (ended_at IS NULL OR ended_at >= to_timestamp($3)) "
        "ORDER BY started_at DESC LIMIT 1", drone_id, hi, lo)
    session_id = sess["id"] if sess else None

    # ── 補傳要跟即時走同一道閘門（2026-09-08）────────────────────
    #
    # 即時路徑**只在 `armed and session_id` 時才寫 telemetry**（`main.py`）。
    # 補傳原本無條件寫，於是同一個「飛機停在地上什麼都沒發生」的狀態，
    # 走即時路是不記錄、走補傳路變成記錄——**差別只在於當時鏈路有沒有斷**。
    # 實測一次 72 秒的中斷補進 59 筆停機坪資料，而且後端回報「跳過 0 筆重複」
    # （地面上根本沒有即時列可比），2026-09-07 做的時間窗去重完全用不上。
    #
    # **但不能整套照抄。** 那道門有兩半，性質不同：
    #
    # * `armed`——樣本自己就帶著，照抄。
    # * `session_id`——**不能照抄**。補傳存在的理由正是「飛機解鎖飛了，
    #   而地面站在斷線中沒看到解鎖、所以沒建架次」。照抄會把最該補的那批
    #   資料整個丟掉。那種情況要**把架次補建出來**（見下）。
    #
    # `armed is None`（不知道）：只有在**地面站自己已經知道那時有一趟**
    # （時間對得上某個架次）才收。不知道又沒有旁證時不寫——那是在憑空
    # 製造一筆飛行紀錄。
    flight = [x for x in good
              if x.armed is True or (x.armed is None and session_id)]
    on_ground = len(good) - len(flight)
    if not flight:
        log.info("補傳全部落在地面（%d 筆，%s）——即時路徑在這種狀態下本來就"
                 "不寫，補傳沒有理由比它更積極", on_ground, body.board_uid)
        return {"ok": True, "inserted": 0, "skipped_duplicate": 0,
                "skipped_on_ground": on_ground, "rejected_implausible": bad,
                "session_id": None, "blackouts_recovered": [],
                "stayed_armed": body.stayed_armed}
    # **範圍要用真正會寫進去的那批算**：拿被丟掉的地面樣本去撐大範圍，
    # 下面的 blackouts UPDATE 就會把不相干的失明記錄標成「已補回」
    lo = min(x.t for x in flight)
    hi = max(x.t for x in flight)

    # **架次補建**：這批樣本說機體是 armed，而地面站沒有對得上的架次
    # ——那就是「解鎖本身發生在斷線期間」。**不寫孤兒、也不丟掉**，
    # 把那一趟補出來，並用 `origin` 說清楚它是怎麼來的：這條紀錄地面站
    # 從頭到尾沒有即時看過，判讀時要知道。
    if session_id is None and any(x.armed for x in flight):
        row = await db.pool.fetchrow(
            """INSERT INTO flight_sessions
                 (drone_id, started_at, ended_at, origin, end_reason,
                  plan_id, plan_name)
               SELECT $1::uuid, to_timestamp($2), to_timestamp($3),
                      'backfilled', 'reconstructed_from_backfill',
                      d.current_plan_id,
                      (SELECT name FROM plans m WHERE m.id = d.current_plan_id)
                 FROM drones d WHERE d.id = $1::uuid
               RETURNING id::text AS id""", drone_id, lo, hi)
        session_id = row["id"] if row else None
        log.warning("補傳補建了一個架次（%s，%.0f–%.0f）：這台機在斷線期間"
                    "解鎖飛過，而地面站從頭到尾沒有即時看到——"
                    "紀錄的 origin 標成 backfilled", session_id, lo, hi)

    # **去重靠先查再濾，不靠 ON CONFLICT**：telemetry 是 hypertable，
    # (drone_id, time) 上沒有唯一索引，`ON CONFLICT DO NOTHING` 因此什麼也不做
    # ——重連後代理重送一段，資料就會變成兩份（2026-08-26 測出來的）。
    # 補一個唯一索引要處理既有重複列，而這裡查一次範圍內的時間戳更直接。
    exist = await db.pool.fetch(
        "SELECT extract(epoch FROM time) AS t FROM telemetry "
        "WHERE drone_id = $1::uuid AND time BETWEEN to_timestamp($2) "
        "AND to_timestamp($3)", drone_id, lo - DEDUP_WINDOW_S,
        hi + DEDUP_WINDOW_S)
    # **去重要用時間窗，不能比對確切的時間戳**（2026-09-07 實測踩到）。
    #
    # 原本是 `round(t, 2) not in have`——百分之一秒的精確比對。而即時那條路
    # 的時間戳是收到封包的時刻（`.775`、`.784`、`.795`⋯逐筆漂移），補傳那條
    # 是機上 1Hz 取樣的刻度（整齊的 `.51`）。**兩邊永遠不會落在同一個百分秒，
    # 所以去重從來沒有生效過。**
    #
    # 實測後果：v4 那一趟飛行中，補傳把「LOITER、高度 −0.5 m、機在地上」
    # 的樣本插進了飛機正在 3.4 m 空中的那 20 秒，與正確的即時列**一比一交錯**。
    # 事後看那段軌跡，兩種互相矛盾的資料長得一樣可信。
    #
    # 改成「這一秒已經有即時資料就不補」。**即時的一律優先**：
    # 補傳的樣本是代理對自己狀態的 1Hz 快照（會落後），而即時那筆直接來自
    # 飛控的封包。同一秒有兩份時，補傳那份不會更好，只會更矛盾。
    have = sorted(float(r["t"]) for r in exist)

    def covered(t: float) -> bool:
        i = bisect.bisect_left(have, t - DEDUP_WINDOW_S)
        return i < len(have) and have[i] <= t + DEDUP_WINDOW_S

    fresh = [s for s in flight if not covered(s.t)]
    rows = [(s.t, drone_id, session_id, s.lat, s.lon, s.alt_msl, s.alt_rel,
             s.heading, s.ground_speed, s.battery_pct, s.battery_voltage,
             s.gps_fix, s.satellites, s.flight_mode, s.armed) for s in fresh]
    await db.pool.executemany(
        # **`armed` 要寫進去。** 代理一直有送，而這句 INSERT 沒列這個欄位
        # ——於是每一筆補傳的 armed 都是 NULL，任何想照它判斷的地方都判不了
        # （2026-09-08：連「刪掉地面上那批」都得改寫條件才刪得到）
        """INSERT INTO telemetry (time, drone_id, session_id, lat, lon,
             alt_msl, alt_rel, heading, ground_speed, battery_pct,
             battery_voltage, gps_fix, satellites, flight_mode, armed,
             backfilled)
           VALUES (to_timestamp($1), $2::uuid, $3::uuid, $4, $5, $6, $7, $8,
                   $9, $10, $11, $12, $13, $14, $15, true)""", rows)

    # 把這段時間的失明記錄標成「已補回」。
    #
    # **只標「已經結束、而且整段都被這批資料涵蓋」的失明**（2026-09-08 修）。
    # 原本的條件是 `coalesce(ended_at, now()) >= lo`，意思是**還沒結束的失明
    # 一律視為延伸到現在**——於是任何一次補傳都會與它重疊，把它標成已補回。
    # 實測：10 筆、跨度 10 秒的樣本，一次標掉 8 段從幾天前開始、從來沒結束
    # 的失明記錄。「我們從沒看到它回來」與「那段資料補回來了」是互相矛盾的
    # 兩句話，而系統同時說了。
    #
    # 寧可少標也不要多標：少標只是報告保守，多標是**歷史說謊**，
    # 而且沒有任何線索指出它在說謊。
    closed = await db.pool.fetch(
        "UPDATE blackouts SET recovered_by = 'backfilled' "
        "WHERE drone_id = $1::uuid AND ended_at IS NOT NULL "
        "AND started_at >= to_timestamp($2) AND ended_at <= to_timestamp($3) "
        "RETURNING id::text AS id", drone_id, lo - DEDUP_WINDOW_S,
        hi + DEDUP_WINDOW_S)
    # ── D 層：回來之後，那還算不算同一趟 ────────────────────────
    # 代理看得到整段（它就在機上），所以它說得出「這段期間機體有沒有一直
    # armed」。地面站看不到，只能問它。
    #
    # **有代理說話 → 相信它**：沒落地就沿用同一個架次（上面已經用時間找到）。
    # **沒有代理 → 誠實說不知道**：那時 stayed_armed 是 None，我們不去猜，
    # 只把資料補進去、把失明記錄留著。**不要假裝是同一趟**。
    if body.stayed_armed and session_id:
        # 架次被失聯收尾過，但機體其實一直在飛——把結束時間往後推到補傳的
        # 尾端，並改記理由。**不重開架次**：重開會讓一趟飛行在記錄上變成兩趟
        await db.pool.execute(
            "UPDATE flight_sessions SET ended_at = greatest(ended_at, "
            "to_timestamp($2)), end_reason = 'telemetry_lost_backfilled' "
            "WHERE id = $1::uuid AND end_reason = 'telemetry_lost'",
            session_id, hi)
    # **重複與時間戳不合理要分開報**：兩者都是「沒寫進去」，但一個是正常的
    # 重送、一個是機上時鐘壞了，混成一個數字等於把後者藏起來
    log.info("補傳 %d 筆、跳過 %d 筆重複、%d 筆在地面、丟棄 %d 筆時間戳不合理"
             "（%s，架次 %s，補回 %d 段失明）",
             len(rows), len(flight) - len(rows), on_ground, bad,
             body.board_uid, session_id, len(closed))
    return {"ok": True, "inserted": len(rows),
            "skipped_duplicate": len(flight) - len(rows),
            "skipped_on_ground": on_ground,
            "rejected_implausible": bad,
            "session_id": session_id,
            "blackouts_recovered": [r["id"] for r in closed],
            "stayed_armed": body.stayed_armed}


@router.patch("/drones/{drone_id}")
async def patch_drone(drone_id: str, body: DronePatch):
    """改名／設定影像串流位址（系統端管理機的身分與屬性，不走環境變數）。
    serial_no 保持原值當穩定鍵。"""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(422, "沒有要更新的欄位")
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise HTTPException(422, "名稱不可為空")
        fields["name"] = name
    if "prop_diameter_mm" in fields:
        v = fields["prop_diameter_mm"]
        # 0 或負數＝清除。**上限只是防手滑**：最大的多旋翼槳也不到 1 m
        if v is not None and not (0 < int(v) <= 1000):
            fields["prop_diameter_mm"] = None
    for k in ("video_url", "airframe_serial", "model"):
        if k in fields:
            # 空字串＝清除（存 NULL）。**不要存空字串**——那會讓「沒填」與
            # 「填了又刪掉」在資料上長得不一樣，但意思相同。
            fields[k] = (fields[k] or "").strip() or None
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    r = await db.pool.execute(
        f"UPDATE drones SET {sets} WHERE id = $1", drone_id, *fields.values())
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此無人機")
    if "name" in fields:
        # **執行期是快取，資料庫才是事實來源。** 改完要把快取更新掉並通知畫面
        # ——不然即時頁會一直顯示舊名字，直到 backend 重啟。
        #
        # 原本只更新 `live`（主機那一台），所以**改一台僚機的名字，即時頁
        # 永遠不會變**。這是刪除那一格的同族問題：寫入端只動了資料庫。
        from .state import fleet
        st = fleet.get(drone_id)
        if st is not None:
            st.drone_name = fields["name"]
        await manager.broadcast({"type": "drone_renamed", "drone_id": drone_id,
                                 "name": fields["name"]})
    return {"ok": True}


@router.post("/drones/{drone_id}/primary")
async def set_primary(drone_id: str):
    """指定哪台是 MAVLink 主機（14540 收到的遙測記在這台名下）。

    飛行中拒切——切換身分會讓進行中的航線歸屬錯亂。
    切換立即生效（api 與 ingest 同進程，直接改 live state），不需重啟。
    """
    if live.armed:
        raise HTTPException(409, "飛行中無法切換主機")
    row = await db.pool.fetchrow(
        "SELECT id::text AS id, name, is_simulated FROM drones WHERE id = $1", drone_id)
    if row is None:
        raise HTTPException(404, "無此無人機")
    if row["name"].startswith("swarm-"):
        raise HTTPException(409, "群飛模擬僚機不能設為主機")
    async with db.pool.acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE drones SET is_primary = false WHERE is_primary")
            await con.execute("UPDATE drones SET is_primary = true WHERE id = $1", drone_id)
    live.drone_id, live.drone_name = row["id"], row["name"]
    live.session_id = None
    return {"ok": True, "primary": row["name"]}


@router.delete("/drones/{drone_id}")
async def delete_drone(drone_id: str):
    """刪除無人機與其**全部**架次、遙測、鏈路與事件資料。不可復原。

    連線中的無人機拒刪：live 迴圈還在用它的 id 寫入，刪了會整路 FK 錯誤；
    模擬機由系統啟動時自動註冊，刪了重啟也會重新出現。
    """
    if live.drone_id == drone_id:
        raise HTTPException(409, "此無人機目前連線中（模擬機由系統自動註冊），無法刪除")
    # **磁碟上的檔案要自己刪**：外鍵連帶清掉的是 `captures` 那幾列，
    # 不是那幾個檔案。順序也有講究——先讀出路徑，刪完機再刪檔，
    # 這樣萬一刪機失敗，檔案還在，那幾列也還指得到它
    files = [r["path"] for r in await db.pool.fetch(
        "SELECT path FROM captures WHERE drone_id = $1::uuid", drone_id)]
    async with db.pool.acquire() as con:
        async with con.transaction():
            # **刪之前先數**：外鍵是 ON DELETE CASCADE，刪完就查不到了，
            # 而「刪掉了多少東西」是這個端點唯一的回執
            counts = {}
            for table in ("telemetry", "link_metrics", "events",
                          "flight_sessions", "blackouts", "captures",
                          "drone_params"):
                counts[table] = await con.fetchval(
                    f"SELECT count(*) FROM {table} WHERE drone_id = $1::uuid",
                    drone_id)
            # 路徑不陪葬：plans 是「路徑快照」不綁機（issues/010、023）。
            #
            # **這一行現在會連帶清掉上面那六張表**（2026-09-02：全部掛上
            # `ON DELETE CASCADE`）。在那之前是一張張手動刪的，而**漏一張
            # 就長孤兒**——實測漏出過 284 筆指向 22 台已不存在的機的事件。
            r = await con.execute("DELETE FROM drones WHERE id = $1", drone_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此無人機")
    import pathlib as _pl
    for f in files:
        try:
            _pl.Path(f).unlink(missing_ok=True)
            _pl.Path(f + ".part").unlink(missing_ok=True)
        except OSError:
            log.warning("刪不掉錄製檔 %s（那一列已經沒了）", f)
    try:
        _pl.Path(captures.root() / "onboard" / drone_id).rmdir()
    except OSError:
        pass
    # **刪掉資料庫那一列不會讓它從畫面上消失。** 執行期還握著三份：機隊
    # 註冊表（廣播迴圈每 0.2 秒送一次它的最後已知位置）、sysid 對照表、
    # 意圖通道。不清的話，即時頁會繼續顯示一台**已經不存在的機**，
    # 而且它與一台「只是斷線」的真機完全同形——要等 backend 重啟才會不見。
    from .state import fleet
    fleet.pop(drone_id, None)
    counts["runtime_sysids"] = mavlink_rx.forget(drone_id)
    for uid, l in list(agent_link.links.items()):
        if l.drone_id == drone_id:
            agent_link.links.pop(uid, None)
    # **還要跟畫面說一聲。** 前端的機隊表也是累積的，沒有這一則的話，
    # 已經開著的分頁要重新整理才看得到刪除的結果
    await manager.broadcast({"type": "drone_removed", "drone_id": drone_id})
    return {"deleted": counts}


@router.get("/live")
async def live_snapshot():
    """主機的即時狀態快照（與 WS 廣播同一份資料）。

    給不方便開 WebSocket 的取用端：驗收腳本（scripts/check-onboard.py）、
    curl 排查。輪詢頻率不宜超過 broadcast_hz，即時顯示請走 WS。
    """
    return live.telemetry_dict()


#: 對外的即時快照白名單（`doc/external-api-v1.html` §5；串流上線後移除，見 `doc/external-live-api.md` §12）。**白名單而不是黑名單**：
#: 內部欄位日後只會愈加愈多，用排除法的話每加一個欄位就會**默默流到外部系統**，
#: 而且外部整合的形狀會跟著我方的內部演進一起變。列舉法讓「對外承諾的形狀」
#: 是一個看得見、改得動的東西。
EXT_LIVE_KEYS = ("drone_name", "mav_sysid", "connected", "armed",
                 "lat", "lon", "alt_rel", "alt_msl",
                 "ground_speed", "vertical_speed", "heading",
                 "telem_age_s", "link_state", "link_age_s")
#: 5G 鏈路指標（同上，白名單）。**不含 `raw`**——那是 modem 的原始 AT 回應，
#: 給我方事後追查用，對外沒有意義而且量很大
EXT_LINK_KEYS = ("time", "rsrp", "rsrq", "sinr", "cqi", "pci", "cell_id",
                 "band", "nr_mode", "rtt_ms", "jitter_ms", "packet_loss_pct",
                 "throughput_up_kbps", "throughput_down_kbps")


@router.get("/ext/live")
async def ext_live(sysid: int | None = None):
    """**對外**的即時快照：位置、高度、速度、5G 鏈路指標。

    與 `/api/live` 同一份資料，但只回上面白名單裡的欄位——外部系統不需要
    知道 EKF、預檢、板號、任務索引這些內部狀態，而它們本來就會隨我方演進而變。

    `sysid` 省略＝主機；指定就回那一台（`/api/live` 只回主機，多機時外部
    無從指定，那正是這個參數存在的理由）。
    """
    from .state import fleet
    st = live
    if sysid is not None:
        st = next((s for s in fleet.values() if s.sysid == sysid), None)
        if st is None:
            raise HTTPException(404, f"sysid {sysid} 沒有遙測——查 GET /api/ext/live 或指令服務的機隊清單")
    d = st.telemetry_dict()
    out = {k: d.get(k) for k in EXT_LIVE_KEYS}
    lk = d.get("link") or {}
    out["link"] = {k: lk.get(k) for k in EXT_LINK_KEYS}
    return out


def _require_uuid(v: str | None) -> str | None:
    """群組任務的 mission/drone id 必須是合法 UUID。擋掉截斷／亂填字串，否則會一路
    帶到建群 DDL 的 ::uuid cast 才炸 asyncpg DataError → 500（該回 422 才對）。
    保留 str 型別（回傳原值），下游程式不受影響。"""
    if v is None:
        return v
    try:
        UUID(v)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"不是合法的 UUID：{v!r}")
    return v


class GroupDroneIn(BaseModel):
    drone_id: str
    layer_index: int | None = None
    plan_id: str | None = None     # separate 模式各自任務

    @field_validator("drone_id", "plan_id")
    @classmethod
    def _v_uuid(cls, v):
        return _require_uuid(v)


class GroupIn(BaseModel):
    name: str
    mode: str = "unified"             # unified / separate
    base_plan_id: str | None = None
    #: 直接給機（既有用法），或給 `squad_id` 讓後端展開成員（小隊）。
    #: **展開之後走的是同一條路**——預檢、衝突檢查、gate 全部不變
    drones: list[GroupDroneIn] = Field(default_factory=list, max_length=8)
    squad_id: str | None = None
    params: dict | None = None

    @field_validator("base_plan_id")
    @classmethod
    def _v_uuid(cls, v):
        return _require_uuid(v)


@router.post("/groups")
async def create_group(g: GroupIn):
    """建群組任務（issue 013-A）：unified 從 base 展開 per-drone 具體任務、
    separate 用各自任務。回 assignments＋跨路徑衝突預檢（capability 嚴格
    gate 在 execute／command 服務，此處只做幾何互檢＋materialize）。

    **小隊只是選機的捷徑**（doc/squads-design.md）：給 `squad_id` 時把成員依
    `position` 展開成 `drones`，其餘一律照舊——不新增第二條執行路徑。
    `name` 預設帶隊名快照，所以日後小隊被刪掉，這一筆仍說得出當時是哪一隊。
    """
    name, squad_id = g.name, None
    drones = list(g.drones)
    if g.squad_id:
        squad_id = _require_uuid(g.squad_id)
        row = await db.pool.fetchrow(
            "SELECT name FROM squads WHERE id = $1::uuid", squad_id)
        if row is None:
            raise HTTPException(404, "無此小隊")
        mem = await db.pool.fetch(
            "SELECT drone_id::text AS drone_id FROM squad_members "
            "WHERE squad_id = $1::uuid ORDER BY position", squad_id)
        if not mem:
            # **空小隊不是「零台的隊」，是還沒編好**——這裡不猜，直接說
            raise HTTPException(422, f"小隊「{row['name']}」還沒有成員")
        if drones:
            raise HTTPException(422, "同時給了 squad_id 與 drones——"
                                     "指定哪幾台飛只能有一個來源")
        # position 是**預設種子**，不是 layer_index：這裡當起始順序用，
        # 派任務頁仍可逐台調整（doc/squads-design.md 決定②）
        drones = [GroupDroneIn(drone_id=m["drone_id"]) for m in mem]
        if not (name or "").strip():
            stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
            name = f"{row['name']} · {stamp}"
    if not drones:
        raise HTTPException(422, "至少要指定一台機（或給 squad_id）")
    if g.mode == "unified" and not g.base_plan_id:
        raise HTTPException(422, "unified 模式需要 base_plan_id")
    if g.mode == "separate" and any(d.plan_id is None for d in drones):
        raise HTTPException(422, "separate 模式每台需要 plan_id")
    try:
        return await groups.create_group(
            name, g.mode, g.base_plan_id,
            [d.model_dump() for d in drones], g.params, squad_id=squad_id)
    except groups.GroupError as e:
        raise HTTPException(422, str(e))


# ── 小隊：常設編組（doc/squads-design.md）──────────────────────
#
# **小隊是一份名單，不是任務設定。** 隊形、高度分層、航線一律在派任務時決定
# ——否則同一個決定會有兩個家，而它們遲早不一致。
# **也不影響任何飛安判定**：入列、預檢、守門、能力 gate 全部照舊逐機判定，
# 小隊只是選機的捷徑，不是繞過檢查的捷徑。


class SquadIn(BaseModel):
    name: str
    note: str | None = None
    members: list[str] = Field(min_length=1)     # drone_id；一隊至少一台


class SquadPatch(BaseModel):
    name: str | None = None
    note: str | None = None
    #: 給了就是**整份取代**。差異比對在前端做——「加一台」與「換一批」混在
    #: 同一支端點裡，日後一定會有人只送了一台就把整隊洗掉
    members: list[str] | None = None


async def _squad_rows() -> list[dict]:
    """全部小隊＋成員＋群飛統計。**成員只回 id**——狀態（在線／訊號／電量）
    前端 store 已經有，join 在畫面做；這裡回一份快照只會過期。"""
    squads = await db.pool.fetch(
        """SELECT s.id::text AS id, s.name, s.note, s.created_at,
                  (SELECT max(g.created_at) FROM mission_groups g
                    WHERE g.squad_id = s.id AND g.status <> 'draft') AS last_flight,
                  (SELECT count(*) FROM mission_groups g
                    WHERE g.squad_id = s.id AND g.status <> 'draft') AS flights
             FROM squads s ORDER BY s.created_at""")
    mem = await db.pool.fetch(
        """SELECT m.squad_id::text AS squad_id, m.drone_id::text AS drone_id,
                  m.position, d.name AS drone_name
             FROM squad_members m JOIN drones d ON d.id = m.drone_id
            ORDER BY m.position, d.name""")
    by: dict[str, list] = {}
    for r in mem:
        by.setdefault(r["squad_id"], []).append(
            {"drone_id": r["drone_id"], "position": r["position"],
             "drone_name": r["drone_name"]})
    out = []
    for s in squads:
        d = dict(s)
        d["created_at"] = s["created_at"].isoformat()
        d["last_flight"] = s["last_flight"].isoformat() if s["last_flight"] else None
        d["members"] = by.get(d["id"], [])
        out.append(d)
    return out


async def _check_drones(ids: list[str]) -> None:
    """成員必須是真的機。**不做部分成功**：找不到就整批 422 並列出是哪幾個。"""
    if not ids:
        raise HTTPException(422, "一隊至少要有一台機")
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "同一台機在同一隊裡只能出現一次")
    rows = await db.pool.fetch(
        "SELECT id::text AS id FROM drones WHERE id = ANY($1::uuid[])", ids)
    known = {r["id"] for r in rows}
    missing = [i for i in ids if i not in known]
    if missing:
        raise HTTPException(422, f"名單裡有 {len(missing)} 台機的記錄不存在："
                                 + "、".join(missing))


@router.get("/squads")
async def list_squads():
    """全部小隊。`flights`＝**這一隊一起飛過幾次**（group 數），不是架次數
    ——三台一起飛一次會產生三個架次，顯示成「6 趟」會被讀成飛了六次。"""
    return await _squad_rows()


@router.post("/squads", status_code=201)
async def create_squad(body: SquadIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "小隊要有名字")
    await _check_drones(body.members)
    try:
        async with db.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    "INSERT INTO squads (name, note) VALUES ($1, $2) RETURNING id::text",
                    name, (body.note or "").strip() or None)
                sid = row["id"]
                await con.executemany(
                    "INSERT INTO squad_members (squad_id, drone_id, position) "
                    "VALUES ($1::uuid, $2::uuid, $3)",
                    [(sid, d, i) for i, d in enumerate(body.members)])
    except asyncpg.UniqueViolationError:
        # **撞名要說撞到哪一個名字**：小隊是拿來喊的，同名等於現場叫不動
        raise HTTPException(409, f"已經有一隊叫「{name}」")
    return {"id": sid}


@router.patch("/squads/{squad_id}")
async def patch_squad(squad_id: str, body: SquadPatch):
    """改名、改備註、換成員。**改名不影響歷史**——過去的群飛紀錄留的是當時的
    隊名快照（`mission_groups.name`）。"""
    sid = _require_uuid(squad_id)
    if body.members is not None:
        await _check_drones(body.members)
    try:
        async with db.pool.acquire() as con:
            async with con.transaction():
                if body.name is not None:
                    name = body.name.strip()
                    if not name:
                        raise HTTPException(422, "小隊要有名字")
                    r = await con.execute(
                        "UPDATE squads SET name = $2 WHERE id = $1::uuid", sid, name)
                    if r.endswith(" 0"):
                        raise HTTPException(404, "無此小隊")
                if body.note is not None:
                    await con.execute(
                        "UPDATE squads SET note = $2 WHERE id = $1::uuid",
                        sid, body.note.strip() or None)
                if body.members is not None:
                    await con.execute(
                        "DELETE FROM squad_members WHERE squad_id = $1::uuid", sid)
                    await con.executemany(
                        "INSERT INTO squad_members (squad_id, drone_id, position) "
                        "VALUES ($1::uuid, $2::uuid, $3)",
                        [(sid, d, i) for i, d in enumerate(body.members)])
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"已經有一隊叫「{(body.name or '').strip()}」")
    return {"ok": True}


@router.delete("/squads/{squad_id}")
async def delete_squad(squad_id: str):
    """只刪編組。**不動任何一台機與其紀錄**，過去的群飛也留著
    （`mission_groups.squad_id` 置 NULL，隊名快照仍在 `name` 裡）。"""
    r = await db.pool.execute("DELETE FROM squads WHERE id = $1::uuid",
                              _require_uuid(squad_id))
    if r.endswith(" 0"):
        raise HTTPException(404, "無此小隊")
    return {"ok": True}


@router.get("/groups/{group_id}")
async def get_group(group_id: str):
    grp = await groups.get_group(group_id)
    if grp is None:
        raise HTTPException(404, "無此群組")
    return grp


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str):
    """刪 draft 群組（使用者調整成員／參數會重建、draft 會堆積——前端回饋 #2）。
    只允許 draft：執行中/已飛過的回 409，保留稽核。"""
    r = await groups.delete_group(group_id)
    if r == "not_found":
        raise HTTPException(404, "無此群組")
    if r == "locked":
        raise HTTPException(409, "非 draft 群組不可刪除（保留稽核紀錄）")
    return {"deleted": group_id}


@router.get("/sessions")
async def list_sessions(limit: int = 50, plan_id: str | None = None,
                        since: str | None = None, min_samples: int | None = None,
                        include_test: bool = False, drone_id: str | None = None,
                        with_events: bool = False):
    """架次清單。可選 plan_id（綁定任務）／since（ISO 時間窗）／min_samples／include_test。

    `min_samples`：只回鏈路樣本數 ≥ 此值的架次（場域訊號頁用，避免空/測試殘留架次
    佔滿載入窗——空架次不是「飛行」，誠實原則）。用架次結束時算好的 summary.samples_total
    篩，不掃 link_metrics；未結束（summary NULL）視為 0、min_samples≥1 時自然排除。

    `include_test`：預設 False＝隱藏 origin='test' 的架次（測試殘留混研究庫的治理；
    見 backfill-session-origin.sql）；True 顯示全部。'unknown'／'research'／NULL 一律顯示。

    `drone_id`：只看這一台機。

    `with_events`：多回三個計數（事件總數／警告／危急）。**預設關掉**——
    它要對 events 逐架次數一次，而只想列架次的呼叫端不該替資訊頁付這個代價。
    有了它，「哪一趟出過事」在清單上就看得出來，不必逐趟點進去才知道。"""
    conds, args = [], []
    if not include_test:
        conds.append("COALESCE(s.origin, 'unknown') <> 'test'")
    if plan_id:
        args.append(plan_id)
        conds.append(f"s.plan_id = ${len(args) + 1}")
    if drone_id:
        args.append(drone_id)
        conds.append(f"s.drone_id = ${len(args) + 1}")
    if since:
        args.append(since)
        conds.append(f"s.started_at >= ${len(args) + 1}::text::timestamptz")
    if min_samples is not None:
        args.append(min_samples)
        conds.append(
            f"COALESCE((s.summary->>'samples_total')::int, 0) >= ${len(args) + 1}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    # 飛行中換過幾次路徑（doc/data-schema §3.4）。**衍生，不存欄位**——
    # 這件事已經在 command_log 裡（_audit 解得出 session_id），再開一欄等於
    # 同一件事有兩個家。0＝全程同一份
    plans = """,
          (SELECT count(*) FROM command_log c WHERE c.session_id = s.id
             AND c.action = 'mission_upload' AND c.result = 'accepted')
            AS plan_changes"""
    ev = """,
          (SELECT count(*) FROM events e WHERE e.session_id = s.id) AS events_total,
          (SELECT count(*) FROM events e WHERE e.session_id = s.id
             AND e.severity = 'warning') AS events_warning,
          (SELECT count(*) FROM events e WHERE e.session_id = s.id
             AND e.severity = 'critical') AS events_critical""" if with_events else ""
    q = f"""
        SELECT s.*, d.name AS drone_name, m.name AS plan_name{plans}{ev}
        FROM flight_sessions s
        JOIN drones d ON d.id = s.drone_id
        LEFT JOIN plans m ON m.id = s.plan_id
        {where}
        ORDER BY s.started_at DESC LIMIT $1
        """
    rows = await db.pool.fetch(q, limit, *args)
    return [dict(r) for r in rows]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """單一架次。清單抓得到就不必打這支；**深連結（重整、貼網址）需要它**
    ——那時候手上只有一個 id，而清單可能根本沒載到那一頁。"""
    row = await db.pool.fetchrow(
        """SELECT s.*, d.name AS drone_name, m.name AS plan_name,
                  (SELECT count(*) FROM command_log c WHERE c.session_id = s.id
                     AND c.action = 'mission_upload' AND c.result = 'accepted')
                    AS plan_changes,
                  (SELECT count(*) FROM events e WHERE e.session_id = s.id)
                    AS events_total,
                  (SELECT count(*) FROM events e WHERE e.session_id = s.id
                     AND e.severity = 'warning') AS events_warning,
                  (SELECT count(*) FROM events e WHERE e.session_id = s.id
                     AND e.severity = 'critical') AS events_critical
           FROM flight_sessions s
           JOIN drones d ON d.id = s.drone_id
           LEFT JOIN plans m ON m.id = s.plan_id
           WHERE s.id = $1""", session_id)
    if row is None:
        raise HTTPException(404, "無此架次")
    return dict(row)


class SessionPatch(BaseModel):
    note: str | None = None            # 自訂備註（實驗條件標註）；空字串/None＝清除
    #: 這一趟屬於哪個任務（階段 2）。**空字串＝取消指派**，None＝這次不改
    #: ——兩者不同：前者是一個決定，後者是「這個欄位沒有出現在請求裡」
    mission_id: str | None = None


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: str, body: SessionPatch):
    """更新架次的備註，以及**這一趟屬於哪個任務**。

    指派是人做的，系統不猜（doc/mission-vs-plan-design.md §4.1②）：
    時間相近、路徑相同都不足以證明是同一件事，而**猜錯的歸類比沒有歸類
    更難發現**。

    `mission_id` 傳空字串＝取消指派。指派的同時把**當下的任務名寫成快照**
    ——任務被刪掉之後，歷史仍要說得出當時屬於哪個任務。
    """
    sets, args = [], []

    def arg(v):
        args.append(v)
        return f"${len(args) + 1}"

    if body.note is not None:
        sets.append(f"note = {arg((body.note or '').strip() or None)}")
    if body.mission_id is not None:
        mid = (body.mission_id or "").strip() or None
        if mid is None:
            sets.append("mission_id = NULL, mission_name = NULL")
        else:
            name = await db.pool.fetchval("SELECT name FROM missions WHERE id = $1", mid)
            if name is None:
                raise HTTPException(404, "無此任務")
            sets.append(f"mission_id = {arg(mid)}, mission_name = {arg(name)}")
    if not sets:
        raise HTTPException(422, "沒有要改的欄位")
    row = await db.pool.fetchrow(
        f"UPDATE flight_sessions SET {', '.join(sets)} WHERE id = $1 "
        "RETURNING id::text, note, mission_id::text, mission_name",
        session_id, *args)
    if row is None:
        raise HTTPException(404, "無此架次")
    return dict(row)


@router.get("/sessions/{session_id}/telemetry-quality")
async def session_telemetry_quality(session_id: str):
    """這一趟的遙測是誰寫進來的，兩個來源有沒有互相矛盾。

    **背景（2026-09-07，d6dea0b）**：補傳的去重原本比對 `round(t, 2)`，而即時
    那條路的時間戳是「收到封包的時刻」（逐筆漂移的百分秒）、補傳那條是機上
    1Hz 取樣的刻度（整齊的 `.51`）——兩邊永遠不會落在同一個百分秒，所以
    **每一筆補傳都被當成新資料插進去**。實測後果：飛機正在 3 m 空中的那 20 秒
    裡，被插進「LOITER、高度 −0.5 m、機在地上」的樣本，與正確的即時列一比一
    交錯。去重已改成時間窗（即時優先），但**修法不會回頭改已經寫進去的列**。

    所以這支端點用**現行那把尺**去量歷史：一筆補傳樣本，若 0.6 秒內有即時
    樣本，它今天就不會被插進來——那就是「本來不該在這裡」的列。

    回的是數字不是判決：**多少筆、位置差多遠、模式說的是不是同一件事**。
    畫面據此決定要不要提醒判讀的人，而不是由這裡替他決定。
    """
    row = await db.pool.fetchrow(
        """
        WITH bf AS (SELECT time, lat, lon, alt_rel, flight_mode FROM telemetry
                     WHERE session_id = $1 AND backfilled),
             lv AS (SELECT time, lat, lon, alt_rel, flight_mode FROM telemetry
                     WHERE session_id = $1 AND NOT backfilled),
             pair AS (
               SELECT b.flight_mode AS bf_mode, l.flight_mode AS lv_mode,
                      b.alt_rel AS bf_alt, l.alt_rel AS lv_alt,
                      CASE WHEN b.lat IS NULL OR l.lat IS NULL THEN NULL
                           ELSE 111320 * sqrt((l.lat - b.lat) ^ 2
                                + ((l.lon - b.lon) * cos(radians(l.lat))) ^ 2)
                      END AS gap_m
                 FROM bf b
                 JOIN LATERAL (
                   SELECT lat, lon, alt_rel, flight_mode FROM lv
                    WHERE lv.time BETWEEN b.time - interval '0.6 s'
                                      AND b.time + interval '0.6 s'
                    ORDER BY abs(extract(epoch FROM lv.time - b.time)) LIMIT 1
                 ) l ON true)
        SELECT (SELECT count(*) FROM lv) AS live,
               (SELECT count(*) FROM bf) AS backfilled,
               (SELECT count(*) FROM pair) AS conflicts,
               (SELECT max(gap_m) FROM pair) AS max_gap_m,
               (SELECT count(*) FROM pair
                 WHERE bf_mode IS DISTINCT FROM lv_mode) AS mode_mismatch
        """, session_id)
    d = dict(row) if row else {}
    return {
        "live": d.get("live", 0),
        "backfilled": d.get("backfilled", 0),
        # **落在即時資料已覆蓋的秒數上**＝現行規則不會再插入的那些列
        "conflicts": d.get("conflicts", 0),
        "max_gap_m": (round(float(d["max_gap_m"]), 1)
                      if d.get("max_gap_m") is not None else None),
        "mode_mismatch": d.get("mode_mismatch", 0),
        "rule": "0.6 秒內已有即時樣本則不補（d6dea0b 起）",
    }


@router.get("/sessions/{session_id}/track")
async def session_track(session_id: str):
    """回放用：一條航線的軌跡 + 鏈路時序 + 關聯任務（供疊預計路徑）。"""
    sess = await db.pool.fetchrow(
        """SELECT s.id, s.plan_id, s.started_at, s.ended_at, m.name AS plan_name
           FROM flight_sessions s LEFT JOIN plans m ON m.id = s.plan_id
           WHERE s.id = $1""", session_id)
    telemetry = await db.pool.fetch(
        "SELECT * FROM telemetry WHERE session_id = $1 ORDER BY time", session_id)
    link = await db.pool.fetch(
        "SELECT * FROM link_metrics WHERE session_id = $1 ORDER BY time", session_id)
    # 回放要看得到「這一刻是誰下的指令」——尤其是被擋下的那些
    commands = await db.pool.fetch(
        "SELECT time, action, result, detail, client FROM command_log "
        "WHERE session_id = $1 ORDER BY time", session_id)
    return {"session": dict(sess) if sess else None,
            "telemetry": [dict(r) for r in telemetry],
            "link": [dict(r) for r in link],
            "commands": [dict(r) for r in commands]}


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str):
    """整條航線匯出成單一 JSON（lossless，可離線分析或封存）。
    原始資料不設保留期限；要封存或騰空間時先匯出，
    再呼叫 DELETE 移除 DB 內的資料（UI 的「匯出並移除」流程）。"""
    sess = await db.pool.fetchrow(
        """SELECT s.*, d.name AS drone_name, m.name AS plan_name
           FROM flight_sessions s
           JOIN drones d ON d.id = s.drone_id
           LEFT JOIN plans m ON m.id = s.plan_id WHERE s.id = $1""", session_id)
    if sess is None:
        raise HTTPException(404, "無此航線")
    payload = {
        "format": "uav-system-session-export",
        "version": 1,
        "session": dict(sess),
        "telemetry": [dict(r) for r in await db.pool.fetch(
            "SELECT * FROM telemetry WHERE session_id = $1 ORDER BY time", session_id)],
        "link_metrics": [dict(r) for r in await db.pool.fetch(
            "SELECT * FROM link_metrics WHERE session_id = $1 ORDER BY time", session_id)],
        "events": [dict(r) for r in await db.pool.fetch(
            "SELECT * FROM events WHERE session_id = $1 ORDER BY time", session_id)],
        # 2026-09-06：匯出檔原本沒有這一段，於是「這趟飛行我下了什麼指令、
        # 系統擋了我幾次」在封存資料裡等於不存在——而 9/2 才剛補上「被系統
        # 擋下的也要留痕」，那些痕當時掛不到任何一趟飛行上。
        "commands": [dict(r) for r in await db.pool.fetch(
            "SELECT * FROM command_log WHERE session_id = $1 ORDER BY time",
            session_id)],
    }
    started = sess["started_at"].strftime("%Y%m%d-%H%M")
    from fastapi.responses import JSONResponse
    return JSONResponse(
        jsonable(payload),
        headers={"Content-Disposition":
                 f'attachment; filename="flight-{started}-{session_id[:8]}.json"'})


def jsonable(o):
    """asyncpg Row 值轉 JSON 可序列化（datetime→ISO、UUID→str、JSONB字串→物件）。"""
    import uuid as _uuid
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [jsonable(v) for v in o]
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, _uuid.UUID):
        return str(o)
    if isinstance(o, str) and o[:1] in "[{":
        try:
            import json as _json
            return _json.loads(o)
        except Exception:
            return o
    return o


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """刪除一條航線與其全部時序資料。搭配匯出使用（匯出後移除）。"""
    if live.session_id == session_id:
        raise HTTPException(409, "此航線正在飛行中")
    async with db.pool.acquire() as con:
        async with con.transaction():
            counts = {}
            for table in ("telemetry", "link_metrics", "events"):
                r = await con.execute(f"DELETE FROM {table} WHERE session_id = $1", session_id)
                counts[table] = int(r.split()[-1])
            r = await con.execute("DELETE FROM flight_sessions WHERE id = $1", session_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此航線")
    return {"deleted": counts}


@router.get("/sessions/{session_id}/params")
async def session_params(session_id: str):
    """該架次的機上參數快照（021 Phase 2，唯讀）。

    實驗可重現性用：這一趟到底是用什麼設定飛的。**沒有快照就誠實回 null**
    ——可能是該機還沒抓完參數、或是影像功能上線前的舊架次，不要拿「現在的」
    參數冒充「當時的」（那會讓事後分析以為設定沒變過）。
    """
    row = await db.pool.fetchrow(
        """SELECT ps.id::text AS id, ps.hash, ps.param_count, ps.params,
                  ps.first_seen
           FROM flight_sessions fs
           LEFT JOIN param_sets ps ON ps.id = fs.param_set_id
           WHERE fs.id = $1""", session_id)
    if row is None:
        raise HTTPException(404, "無此航線")
    if row["id"] is None:
        return {"param_set_id": None, "params": None,
                "note": "本架次沒有參數快照（該機未完成參數讀取，或為功能上線前的舊架次）"}
    return {"param_set_id": row["id"], "hash": row["hash"],
            "param_count": row["param_count"],
            "first_seen": row["first_seen"].isoformat(),
            "params": json.loads(row["params"]) if isinstance(row["params"], str)
                      else row["params"]}


@router.get("/sessions/{session_id}/video")
async def session_video(session_id: str):
    """該架次的影像片段與狀態（契約見 doc/flight-video-design.md §8b）。

    retention_days 動態帶出——事實源是 .env 的 VIDEO_RETENTION_DAYS，同一個
    變數也餵給錄製器清檔，所以 UI 顯示的天數不可能與實際清檔行為不一致。
    """
    from . import video_rec
    r = await video_rec.session_video(session_id)
    if not r:
        raise HTTPException(404, "無此航線")
    return r


@router.get("/video/segments/{segment_id}/file")
async def video_segment_file(segment_id: str):
    """單段影片檔（同源直供，前端免 CORS）。

    影片檔不轉碼——原樣就是機上送來的樣子，畫質劣化是研究證據不是瑕疵。
    """
    from fastapi.responses import FileResponse
    import os
    row = await db.pool.fetchrow(
        "SELECT path, started_at FROM video_segments WHERE id = $1", segment_id)
    if row is None:
        raise HTTPException(404, "無此影片片段")
    # 片段檔名＝錄製器以段起始時間命名（見設計 §4：檔名只是線索，錨點在 DB）
    d = os.path.join(settings.video_rec_dir, row["path"])
    if not os.path.isdir(d):
        raise HTTPException(410, "影像已不存在（可能已過保留期清除）")
    stamp = row["started_at"].strftime("%Y-%m-%d_%H-%M-%S")
    for name in sorted(os.listdir(d)):
        if name.startswith(stamp) and name.endswith(".mp4"):
            return FileResponse(os.path.join(d, name), media_type="video/mp4")
    raise HTTPException(410, "影像已不存在（可能已過保留期清除）")


@router.get("/events")
async def list_events(limit: int = 100, session_id: str | None = None,
                      drone_id: str | None = None, severity: str | None = None,
                      source: str | None = None, type: str | None = None,
                      q: str | None = None,
                      since: str | None = None, until: str | None = None,
                      before_id: int | None = None):
    """事件查詢。無參數＝最新 N 則（即時頁開頁補歷史用，行為不變）。

    資訊頁（歷史檢視）要的是**往回翻得完**，所以多了篩選與游標：

    * `session_id`：這一趟的事件（時間**正序**——讀一趟飛行是從頭讀到尾）。
    * 其餘篩選（`drone_id`／`severity`／`source`／`type`／`q`／`since`／`until`）
      走跨架次檢視，時間**倒序**（最近的先看）。
    * `before_id`：游標分頁。**用 id 不用 offset**——事件是持續寫入的，
      offset 會在新事件進來時把同一則推到下一頁去（或跳過一則）。

    `q` 比對 `type` 與 `detail` 的文字，讓「搜 failsafe」這種事做得到；
    比對的是 detail 的 JSON 文字表示，**認不得的欄位也搜得到**。"""
    conds: list[str] = []
    args: list = []

    def arg(v) -> str:
        args.append(v)
        return f"${len(args)}"

    if session_id:
        conds.append(f"session_id = {arg(session_id)}")
    if drone_id:
        conds.append(f"drone_id = {arg(drone_id)}")
    if severity:
        # **選「警告」要選得到舊資料裡的 `warn`。** 寫入端已正規化
        # （db.SEVERITY_ALIASES），但既有 292 列不改寫——篩選這一端要認得，
        # 否則畫面上看得到、篩選卻永遠濾掉，比沒有這個篩選更糟
        if severity == "warning":
            conds.append(f"severity = ANY({arg(['warning', 'warn'])})")
        else:
            conds.append(f"severity = {arg(severity)}")
    if source:
        conds.append(f"source = {arg(source)}")
    if type:
        conds.append(f"type = {arg(type)}")
    if since:
        conds.append(f"time >= {arg(since)}::text::timestamptz")
    if until:
        conds.append(f"time <= {arg(until)}::text::timestamptz")
    if before_id is not None:
        conds.append(f"id < {arg(before_id)}")
    if q and q.strip():
        needle = f"%{q.strip()}%"
        conds.append(f"(type ILIKE {arg(needle)} "
                     f"OR COALESCE(detail::text, '') ILIKE {arg(needle)})")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    # 一趟飛行正序讀、跨架次倒序讀（見 docstring）。**排序鍵一律附帶 id**：
    # 同一毫秒寫進去的兩則事件若只用 time 排序，分頁游標會在它們之間打結
    order = "time ASC, id ASC" if session_id and not before_id else "time DESC, id DESC"
    rows = await db.pool.fetch(
        f"SELECT * FROM events {where} ORDER BY {order} LIMIT {arg(min(limit, 1000))}",
        *args)
    return [dict(r) for r in rows]


@router.get("/event-types")
async def event_types(days: int = 30):
    """出現過哪些事件型別（給資訊頁的篩選下拉用）。

    **不寫死清單**：型別是後端與韌體一起長出來的，硬編一份下拉選單等於
    每加一種事件就多一個「查不到」的死角。照資料庫裡實際有的列。"""
    rows = await db.pool.fetch(
        """SELECT type, source, count(*) AS n, max(time) AS last_seen
           FROM events WHERE time >= now() - ($1 || ' days')::interval
           GROUP BY type, source ORDER BY n DESC""", str(days))
    return [dict(r) for r in rows]


@router.get("/sessions/{session_id}/commands")
async def session_commands(session_id: str):
    """這一趟下了什麼指令、哪些被擋下來。

    `/sessions/{id}/track` 也回這一段，但它同時拖著整條遙測——資訊頁只要
    指令那一列時，不該為此把幾萬筆 telemetry 拉過網路。"""
    # `params.plan_id` 補上名字：畫面上只拿得到一個 uuid 的話，
    # 「飛行中換成哪一份」就答不出來（doc/data-schema §3.4）。
    # **路徑被刪掉時 name 是 NULL**——那就顯示成「已刪除的路徑」，
    # 不是留白（留白會被讀成「沒有換」）
    rows = await db.pool.fetch(
        "SELECT c.time, c.action, c.result, c.detail, c.client, c.params, "
        "       m.name AS plan_name "
        "  FROM command_log c "
        "  LEFT JOIN plans m "
        "    ON m.id = NULLIF(c.params->>'plan_id', '')::uuid "
        " WHERE c.session_id = $1 ORDER BY c.time", session_id)
    return [dict(r) for r in rows]


# ── 任務疊圖（roadmap 3）──────────────────────────────────────────────────
# 從飛機讀回 QGC 上傳的任務。MAVLink 任務下載是**唯讀**操作，
# 不違反「backend 對 MAVLink 只讀不寫」——上傳與啟動仍由 QGC 負責。

# MAV_CMD：帶座標的導航類指令（16 WAYPOINT / 21 LAND / 22 TAKEOFF）
_NAV_CMDS = {16, 21, 22}


@router.get("/mission/current")
async def current_plan():
    if mavlink_rx.rx is None or not live.connected:
        raise HTTPException(503, "MAVLink 未連線")
    try:
        items = await asyncio.wait_for(mavlink_rx.rx.download_mission(), timeout=12)
    except Exception as e:
        raise HTTPException(502, f"任務下載失敗：{e}")
    waypoints = [
        {
            "seq": it.seq,
            "command": it.command,
            "lat": it.x / 1e7,       # MISSION_ITEM_INT 的座標是 int32 度 ×1e7
            "lon": it.y / 1e7,
            "alt": it.z,             # frame 3 = 相對起飛點高度（QGC 預設）
            "frame": it.frame,
            "p1": it.param1, "p2": it.param2, "p3": it.param3, "p4": it.param4,
        }
        for it in items
        if it.command in _NAV_CMDS and (it.x or it.y)
    ]
    return {"item_count": len(items), "waypoints": waypoints}


# ── 任務庫（路徑管理頁）───────────────────────────────────────────────────
# 儲存的路徑可標記 is_active（至多一條），即時頁優先顯示它；
# 沒有啟用中的路徑時，退回「從機上讀回目前任務」。

class WaypointIn(BaseModel):
    seq: int
    lat: float                       # DO_* 設定類（無座標）以 0 表示
    lon: float
    alt: float | None = None
    action: str | None = "waypoint"
    # MAVLink 保真度（2026-08-10）：.plan 的原始 command/frame/p1–p4 全保留，
    # 上傳到機時原樣送出——與現場工具 upload_mission.py 的行為一致。
    # 舊資料沒有這些欄位時由 action 推回（見 plan_check._cmd／command 服務）。
    command: int | None = None
    frame: int | None = None
    p1: float | None = None
    p2: float | None = None
    p3: float | None = None
    p4: float | None = None


# 叫 PlanIn 不叫 MissionIn：同名的「任務」類別定義在下面，會把這個蓋掉——
# 匯入 .plan 因此從 09-08 起一律 500（缺 waypoints）
class PlanIn(BaseModel):
    name: str
    source: str = "plan-file"        # plan-file / vehicle
    waypoints: list[WaypointIn] = Field(min_length=2, max_length=500)
    # 037：`.plan` 自報的目標機種（QGC 的 mission.firmwareType/vehicleType，
    # 值域就是 MAV_AUTOPILOT／MAV_TYPE）。**存下來是為了上傳前能比對**——
    # 航點的 frame 與 params 是照哪一家的語意寫的，只有這兩個欄位說得出來。
    # 不填＝這份任務沒說（手繪、舊資料、從機上讀回）。
    firmware_type: int | None = None
    vehicle_type: int | None = None
    #: 航線自帶的圍欄（前端從 .plan 的 geoFence 解出來）。**圍欄是每份航線
    #: 自己的事**——沒有它就只能拿系統預設值去量，而那個值只對一個場地成立
    fence: dict | None = None
    #: QGC 的 plannedHomePosition [lat, lon, alt]。RTL 沒有座標，少了它
    #: 返航那一段畫不出來；它也是距離量測該用的原點。
    #:
    #: **元素允許 null**：QGC 在沒有地形資料時把高度寫成 null
    #: （`[24.77, 121.04, null]`）。原本宣告成 list[float] 於是整份 .plan 被
    #: 422 擋下——而我們只用得到 lat/lon，高度從來沒讀過。**為了一個用不到的
    #: 欄位拒收一份合法的航線**，是驗證訂得比需求嚴。
    home: list[float | None] | None = None
    #: .plan 宣告的速度（cruiseSpeed／hoverSpeed），用來估預計時間
    cruise_speed: float | None = None
    hover_speed: float | None = None
    #: QGC 的 rallyPoints.points（[[lat, lon, alt], …]）：緊急備降點。
    #: 元素同樣允許 null（理由見 home）
    rally: list[list[float | None]] | None = None


async def _store_mission(name: str, source: str, wps: list[dict],
                         firmware_type: int | None = None,
                         vehicle_type: int | None = None,
                         fence: dict | None = None,
                         home: list[float] | None = None,
                         cruise: float | None = None,
                         hover: float | None = None,
                         rally: list | None = None,
                         policy: dict | None = None) -> str:
    async with db.pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "INSERT INTO plans (name, created_by, kind, firmware_type, "
                "vehicle_type, fence, home, cruise_speed, hover_speed, rally, "
                "policy) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id",
                name, source, "from-vehicle" if source == "vehicle" else "imported",
                firmware_type, vehicle_type,
                jdumps(fence) if fence else None,
                jdumps(home) if home else None, cruise, hover,
                jdumps(rally) if rally else None,
                jdumps(policy) if policy else None)
            await con.executemany(
                """INSERT INTO waypoints (plan_id, seq, lat, lon, alt, action, params)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                [(row["id"], w["seq"], w["lat"], w["lon"], w.get("alt"),
                  w.get("action", "waypoint"),
                  # MAVLink 保真度塞 params JSONB（閒置欄位正好承接）
                  # `h`／`alt_source` 也要留住：**逐點的 alt 是政策解出來的結果，
                  # 不是意圖**。沒有它們，重開這一頁就分不出哪個點是手動改過的
                  jdumps({k: w[k] for k in ("command", "frame", "p1", "p2", "p3",
                                            "p4", "h", "alt_source")
                              if w.get(k) is not None}) if w.get("command") is not None else None)
                 for w in wps])
    return str(row["id"])


# ── 任務（doc/mission-vs-plan-design.md §4）──────────────────────────────
# **任務 ≠ 路徑。** 路徑是一份 `.plan`（`/api/plans`），任務是「要達成的那件
# 事」——可以跨多份路徑、多個架次、多台機。這一組端點是 2026-09-08 階段 2
# 新增的；在那之前 `/api/missions` 指的是路徑（已於階段 1 改名並移除轉址）。
class MissionIn(BaseModel):
    name: str
    note: str | None = None
    #: 綁一整隊（**活的連結**：小隊改成員，任務跟著變）
    squad_id: str | None = None
    #: 直接綁的機。有效參與名單＝這個 ∪ 綁的小隊的成員
    drones: list[str] = Field(default_factory=list)
    #: 建立時就標成已結束。**補歸歷史架次時要用**：同時只能有一個「進行中」
    #: 的任務（§4.5），而回頭替以前飛過的那幾趟開一個任務，不該把現在正在
    #: 進行的那個擠掉
    ended: bool = False


class MissionPatch(BaseModel):
    name: str | None = None
    note: str | None = None
    #: 換綁小隊；空字串＝解除。**活的連結**，不是快照
    squad_id: str | None = None
    #: 直接綁的機（整份取代，與 squads 的 members 同一個約定）
    drones: list[str] | None = None
    #: 顯式收尾。**只給畫面分「進行中／已結束」，不影響任何判定**——
    #: 不做狀態機（squads 的同一條：任務不該長成第二套規劃）
    ended: bool | None = None


@router.get("/missions")
async def list_missions():
    """全部任務，附**衍生**的統計：幾趟、幾台機、幾份路徑、最近一趟。

    **統計不存欄位**：它們都是 `flight_sessions` 上一個 group by 就有的東西，
    存下來就要維護一致性，而那是第二個家。
    """
    rows = await db.pool.fetch("""
        SELECT m.*,
               (SELECT s2.name FROM squads s2 WHERE s2.id = m.squad_id) AS squad_name,
               -- 有效參與名單＝直接綁的機 ∪ 綁的小隊的成員（§4.6）。
               -- **查詢時展開，不存快照**：綁小隊的意思就是小隊改成員、任務跟著變
               (SELECT coalesce(json_agg(json_build_object(
                          'id', d.id::text, 'name', d.name)), '[]'::json)
                  FROM drones d WHERE d.id IN (
                    SELECT md.drone_id FROM mission_drones md WHERE md.mission_id = m.id
                    UNION
                    SELECT sm.drone_id FROM squad_members sm WHERE sm.squad_id = m.squad_id))
                 AS crew,
               (SELECT count(*) FROM flight_sessions s WHERE s.mission_id = m.id)
                 AS sessions,
               -- 飛過的機數（歷史）。**與 crew（現在排定要跑的）是兩件事**
               (SELECT count(DISTINCT s.drone_id) FROM flight_sessions s
                 WHERE s.mission_id = m.id) AS drones_flown,
               (SELECT count(DISTINCT s.plan_id) FROM flight_sessions s
                 WHERE s.mission_id = m.id AND s.plan_id IS NOT NULL) AS plans,
               (SELECT max(s.started_at) FROM flight_sessions s
                 WHERE s.mission_id = m.id) AS last_flight
          FROM missions m ORDER BY m.created_at DESC""")
    # asyncpg 把 json 當字串回，前端拿到會是一串引號包起來的東西
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("crew"), str):
            d["crew"] = json.loads(d["crew"])
        out.append(d)
    return out


@router.get("/missions/active")
async def active_missions(drone_id: str | None = None):
    """進行中的任務（`ended_at IS NULL`）。

    * 不帶參數＝**全部**進行中的（多組可以同時跑，§4.6）。
    * 帶 `drone_id`＝那台機參與中的那一個，沒有就回空清單。
      **恰好一個**是資料庫的不變式保證的（一台機一次只能執行一個任務）。
    """
    if drone_id:
        rows = await db.pool.fetch(
            "SELECT m.id::text, m.name, m.note, m.created_at, m.squad_id::text, m.external "
            "  FROM missions m WHERE m.ended_at IS NULL AND $1::uuid IN ("
            "    SELECT md.drone_id FROM mission_drones md WHERE md.mission_id = m.id"
            "    UNION"
            "    SELECT sm.drone_id FROM squad_members sm WHERE sm.squad_id = m.squad_id)",
            drone_id)
    else:
        rows = await db.pool.fetch(
            "SELECT id::text, name, note, created_at, squad_id::text, external "
            "FROM missions WHERE ended_at IS NULL ORDER BY created_at DESC")
    return [dict(r) for r in rows]


@router.post("/missions", status_code=201)
async def create_mission(body: MissionIn):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "任務要有名字")
    try:
        async with db.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    "INSERT INTO missions (name, note, ended_at, squad_id) "
                    "VALUES ($1, $2, CASE WHEN $3 THEN now() END, $4::uuid) "
                    "RETURNING id::text",
                    name, (body.note or "").strip() or None, body.ended,
                    body.squad_id or None)
                for did in body.drones:
                    await con.execute(
                        "INSERT INTO mission_drones (mission_id, drone_id) "
                        "VALUES ($1::uuid, $2::uuid) ON CONFLICT DO NOTHING",
                        row["id"], did)
    except asyncpg.UniqueViolationError as e:
        # 兩種撞法要分得開：撞名字、撞「一台機一次只能執行一個任務」。
        # 後者由 DB 的觸發器丟出來，訊息已經說得出是哪一台、撞到哪兩個任務
        msg = str(e)
        if "一台機一次只能執行一個任務" in msg:
            raise HTTPException(409, {"msg": msg,
                                      "how_to": ["先把那台機從另一個任務移出，或結束那個任務"]})
        # **說得出撞到哪一個**：只講「名稱重複」的話，人得自己去清單裡找
        raise HTTPException(409, f"已經有一個任務叫「{name}」")
    return {"id": row["id"], "name": name}


@router.patch("/missions/{mission_id}")
async def patch_mission(mission_id: str, body: MissionPatch):
    """改名／改備註／收尾。**改名不影響歷史**——架次上留的是當時的快照。"""
    sets, args = [], []

    def arg(v):
        args.append(v)
        return f"${len(args) + 1}"

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "任務要有名字")
        sets.append(f"name = {arg(name)}")
    if body.note is not None:
        sets.append(f"note = {arg(body.note.strip() or None)}")
    if body.ended is not None:
        sets.append(f"ended_at = {'now()' if body.ended else 'NULL'}")
    if body.squad_id is not None:
        # 空字串＝解除綁定（與 mission_id 同一個約定）
        sets.append(f"squad_id = {arg(body.squad_id.strip() or None)}::uuid")
    if not sets and body.drones is None:
        raise HTTPException(422, "沒有要改的欄位")
    if not sets:
        sets.append("name = name")          # 只改名單時也要有一個 SET
    try:
        row = await db.pool.fetchrow(
            f"UPDATE missions SET {', '.join(sets)} WHERE id = $1 "
            "RETURNING id::text, name", mission_id, *args)
    except asyncpg.UniqueViolationError as e:
        if "一台機一次只能執行一個任務" in str(e):
            raise HTTPException(409, {"msg": str(e),
                                      "how_to": ["先把那台機從另一個任務移出，或結束那個任務"]})
        raise HTTPException(409, f"已經有一個任務叫「{body.name}」")
    if row is None:
        raise HTTPException(404, "無此任務")
    if body.name is not None:
        # **改名要同步既有架次的快照**（§4.5）：不然畫面上會出現「任務叫 A，
        # 但這一趟寫著原屬 B」。快照的用途是「任務被刪之後還說得出當時叫什麼」，
        # 不是「記住每一次改名前的舊名字」——起飛時打錯字是常態
        await db.pool.execute(
            "UPDATE flight_sessions SET mission_name = $2 WHERE mission_id = $1",
            mission_id, row["name"])
    if body.drones is not None:
        # 整份取代（差異比對在前端做，避免「加一台」與「換一批」兩種語意
        # 混在同一支——與 squads 的 members 同一條）
        try:
            async with db.pool.acquire() as con:
                async with con.transaction():
                    await con.execute(
                        "DELETE FROM mission_drones WHERE mission_id = $1::uuid",
                        mission_id)
                    for did in body.drones:
                        await con.execute(
                            "INSERT INTO mission_drones (mission_id, drone_id) "
                            "VALUES ($1::uuid, $2::uuid) ON CONFLICT DO NOTHING",
                            mission_id, did)
        except asyncpg.UniqueViolationError as e:
            # **不變式的例外要在這裡也翻成人話**：它是觸發器丟的，而觸發器在
            # 這一段才被踩到——上面那個 try 只包了 UPDATE missions
            raise HTTPException(409, {"msg": str(e),
                                      "how_to": ["先把那台機從另一個任務移出，或結束那個任務"]})
    return {"id": row["id"], "name": row["name"]}


@router.delete("/missions/{mission_id}")
async def delete_mission(mission_id: str):
    """刪任務**不刪任何一趟飛行**。架次的 `mission_id` 變 NULL，
    但 `mission_name` 快照留著——歷史仍說得出當時屬於哪個任務。"""
    row = await db.pool.fetchrow(
        "DELETE FROM missions WHERE id = $1 RETURNING id::text", mission_id)
    if row is None:
        raise HTTPException(404, "無此任務")
    return {"deleted": row["id"]}


@router.get("/plans")
async def list_missions():
    # 排除群組任務地面生成的具體任務（created_by='group-gen'）——那是編隊每台的
    # materialized 任務、不是任務庫草稿，會污染一般任務清單 UI（issue 013-B 前端回報）。
    rows = await db.pool.fetch("""
        SELECT m.id, m.name, m.created_by AS source, m.created_at, m.is_active,
               m.firmware_type, m.vehicle_type, m.home,
               m.cruise_speed, m.hover_speed,
               count(w.seq) AS waypoint_count
        FROM plans m LEFT JOIN waypoints w ON w.plan_id = m.id
        WHERE m.kind IS DISTINCT FROM 'generated'
        GROUP BY m.id ORDER BY m.created_at DESC""")
    out = [dict(r) for r in rows]
    if not out:
        return out
    # 預計時間：**一次撈完所有航點再算**，不要一份一份查（N+1）。
    # 算不出來時 eta_s 是 null 並附上 eta_unknown 說明為什麼——
    # 不給預設速度，因為使用者會拿這個數字去安排電池
    ids = [r["id"] for r in out]
    wrows = await db.pool.fetch(
        "SELECT plan_id, seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = ANY($1::uuid[]) ORDER BY plan_id, seq", ids)
    by_mission: dict = {}
    for r in wrows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w.update({k: pm.get(k) for k in ("command", "frame", "p1", "p2")})
        by_mission.setdefault(w["plan_id"], []).append(w)
    for d in out:
        home = d.get("home")
        if isinstance(home, str):
            home = json.loads(home)
        d["home"] = home
        est = mission_time.estimate(
            by_mission.get(d["id"], []), d.get("cruise_speed"),
            d.get("hover_speed"), home)
        d["eta_s"] = est["seconds"]
        d["eta_unknown"] = est["unknown"]
        d["eta_assumptions"] = est["assumptions"]
    return out


@router.get("/plans/active")
async def active_mission():
    row = await db.pool.fetchrow(
        "SELECT id, name, home, fence, rally FROM plans WHERE is_active LIMIT 1")
    if row is None:
        raise HTTPException(404, "沒有啟用中的路徑")
    wps = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action FROM waypoints WHERE plan_id = $1 ORDER BY seq",
        row["id"])
    home = row["home"]
    if isinstance(home, str):
        home = json.loads(home)
    # **返航那一段要畫得出來**：RTL 沒有座標（它的意思是「回到 home」），
    # 少了 home，畫面上航線就停在最後一個航點，看起來像規劃到一半
    def _j(v):
        return json.loads(v) if isinstance(v, str) else v
    # **圍欄與備降點也要畫**：QGC 畫得出來、我們畫不出來，兩邊的圖就不一樣
    # ——而使用者是拿這張圖來確認「機會怎麼飛」的（2026-08-26 回報）
    return {"id": str(row["id"]), "name": row["name"], "home": home,
            "fence": _j(row["fence"]), "rally": _j(row["rally"]),
            "waypoints": [dict(w) for w in wps]}


@router.get("/plans/{plan_id}/waypoints")
async def mission_waypoints(plan_id: str):
    # `frame` 一起回（MAV_FRAME，躺在 params 裡）：**高度的意思寫在它上面**
    # ——3＝離起飛點、10＝離地面（地形跟隨）。同一個「4.6 m」在兩者是不同的
    # 地方，而路徑管理頁要在列上說出這件事（ui-spec §4.6）。
    wps = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, "
        "       (params->>'frame')::int   AS frame, "
        "       (params->>'command')::int AS command, "
        # 到點停留秒數（issues/062）。**縮圖與列表要標得出停留點**——
        # 沒有這一格，畫面上「停 30 秒的點」與「飛過去的點」長得一樣（066／064）
        "       (params->>'p1')::float     AS hold_s "
        "  FROM waypoints WHERE plan_id = $1 ORDER BY seq",
        plan_id)
    if not wps:
        raise HTTPException(404, "無此路徑或無航點")
    # **home 也要回**：起飛項與 RTL／LAND 在 .plan 裡可以沒有座標（意思是
    # 「從 home 起飛」「回 home」），少了它取用端畫不出起飛爬升段與返航段。
    # `/missions/active` 一直有回，回放頁走的是這條端點、於是同一份任務在
    # 即時頁與回放頁上是兩個形狀（2026-09-07）。
    home = await db.pool.fetchval("SELECT home FROM plans WHERE id = $1", plan_id)
    if isinstance(home, str):
        home = json.loads(home)
    return {"waypoints": [dict(w) for w in wps], "home": home}


@router.post("/plans")
async def save_mission(m: PlanIn):
    """存入任務庫並附上幾何預檢報告。**不因預檢失敗而拒存**——任務庫
    可放草稿；真正的擋門在 command 服務上傳到機那一步。"""
    wps = [w.model_dump() for w in m.waypoints]
    mid = await _store_mission(m.name, m.source, wps,
                               m.firmware_type, m.vehicle_type, m.fence, m.home,
                               m.cruise_speed, m.hover_speed, m.rally)
    return {"id": mid, "check": plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, fence=m.fence,
        autopilot=m.firmware_type, home=m.home, dem=terrain.shared())}


@router.post("/plans/from-vehicle")
async def import_mission_from_vehicle(name: str | None = None):
    """把機上目前的任務（QGC 上傳的）讀回並存進任務庫。唯讀 + 入庫。"""
    data = await current_plan()
    wps = data["waypoints"]
    if len(wps) < 2:
        raise HTTPException(404, "機上沒有可儲存的任務（航點少於 2）")
    stored = [{"seq": w["seq"], "lat": w["lat"], "lon": w["lon"], "alt": w["alt"],
               "action": {22: "takeoff", 21: "land"}.get(w["command"], "waypoint"),
               "command": w["command"], "frame": w.get("frame"),
               "p1": w.get("p1"), "p2": w.get("p2"), "p3": w.get("p3"), "p4": w.get("p4")}
              for w in wps]
    # 從機上讀回來的任務，機種就是**這台機**——不必猜也不該留空。
    # 這裡填的是實際偵測到的值，跟 .plan 自報的是同一組 enum。
    mid = await _store_mission(
        name or f"機上任務 {datetime.now().strftime('%m/%d %H:%M')}", "vehicle", stored,
        live.autopilot_raw, live.vehicle_type_raw)
    return {"id": mid, "waypoint_count": len(wps),
            "check": plan_check.check_waypoints(
                stored, settings.geofence_radius_m, settings.geofence_alt_m,
                settings.geofence_margin, dem=terrain.shared())}


@router.post("/plans/{plan_id}/terrain-frame")
async def make_terrain_frame(plan_id: str, name: str | None = None):
    """把一份航線改寫成**地形跟隨**（`frame 10`）並**存成新的一份**
    （issues/047 §1-A）。

    **不就地改寫、也不在上傳時偷偷轉。** 兩個理由：

    * MAVLink 保真度是這套系統的原則（見 `build_items`）——上傳時把使用者
      顯式寫的 frame 換掉，等於飛的東西跟他看的那份不是同一份。
    * 改寫之後高度的意思從「離起飛點」變成「離地面」。那是**另一份航線**，
      操作員應該先看到它、比對過縮圖，再決定要不要飛。

    新高度就是地形預檢算的離地高度；起飛、降落、RTL 不轉（理由見
    `plan_check.to_terrain_frame`）。查不到高程就整份不轉。
    """
    row = await db.pool.fetchrow(
        "SELECT name, fence, home, firmware_type, vehicle_type, "
        "cruise_speed, hover_speed, rally FROM plans WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    rows = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", plan_id)
    wps = []
    for r in rows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w["params"] = pm
        w.update({k: pm.get(k) for k in ("command", "frame", "p1", "p2", "p3", "p4")})
        wps.append(w)
    home = row["home"]
    if isinstance(home, str):
        home = json.loads(home)
    if not (home and len(home) >= 2 and (home[0] or home[1])):
        home = next(({"lat": w["lat"], "lon": w["lon"]} for w in wps
                     if w.get("lat") and w.get("lon")), None)
    else:
        home = {"lat": home[0], "lon": home[1]}
    if home is None:
        raise HTTPException(409, {"msg": "這份航線沒有起飛點座標，算不出基準高度"})

    conv = plan_check.to_terrain_frame(wps, home, dem=terrain.shared())
    if not conv["ok"]:
        raise HTTPException(409, {"msg": "無法改寫成地形跟隨", **conv})

    fence = row["fence"]
    if isinstance(fence, str):
        fence = json.loads(fence)
    rally = row["rally"]
    if isinstance(rally, str):
        rally = json.loads(rally)
    stored = [{"seq": w["seq"], "lat": w["lat"], "lon": w["lon"], "alt": w["alt"],
               "action": w.get("action") or "waypoint",
               "command": (w.get("params") or {}).get("command"),
               "frame": w.get("frame"),
               **{k: (w.get("params") or {}).get(k) for k in ("p1", "p2", "p3", "p4")}}
              for w in conv["waypoints"]]
    mid = await _store_mission(
        name or f"{row['name']}（地形跟隨）", "terrain-frame", stored,
        row["firmware_type"], row["vehicle_type"], fence,
        json.loads(row["home"]) if isinstance(row["home"], str) else row["home"],
        row["cruise_speed"], row["hover_speed"], rally)
    return {"id": mid, "from": plan_id,
            "converted": conv["converted"], "kept": conv["kept"],
            "warnings": conv["warnings"],
            # 新的那份再跑一次預檢：改寫之後 frame 10 的點不參加地形檢查
            # （那正是重點——交給飛控了），報告要能看出剩下什麼
            "check": plan_check.check_waypoints(
                stored, settings.geofence_radius_m, settings.geofence_alt_m,
                settings.geofence_margin, fence=fence,
                autopilot=row["firmware_type"], home=home and [home["lat"], home["lon"]],
                dem=terrain.shared())}


#: 產好的地形圖磚放這裡。**圖磚是純函數的產物**（同一塊 DEM ＋ 同一組 z/x/y
#: 永遠是同一張），所以快取只是省 CPU，不需要失效策略——換 DEM 的時候
#: 把這個目錄砍掉就好。
TILE_CACHE = os.environ.get("TERRAIN_TILE_CACHE", "/data/terrain-tiles")


@router.get("/terrain-rgb/{z}/{x}/{y}.png")
async def terrain_rgb(z: int, x: int, y: int):
    """給 maplibre 的地形圖磚（`raster-dem`，`encoding: "terrarium"`）。

    **就地從 `.hgt` 換算，不引進影像函式庫**——後端是會飛飛機的服務，
    為了畫圖多裝一個相依不划算，而 PNG 的最小可用編碼只有二十幾行
    （`libs/terrain._png`）。

    **這一區沒有 DEM 就回 404**，不回一張全 0 的圖磚：後者在畫面上是一片
    海平面高度的假平地，而那比沒有地形更糟——**它看起來像個答案**。
    """
    if not (0 <= z <= 20 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(400, "z/x/y 超出範圍")
    path = os.path.join(TILE_CACHE, str(z), str(x), f"{y}.png")
    if os.path.exists(path):
        with open(path, "rb") as f:
            png = f.read()
    else:
        # 一張約 0.14 秒，走執行緒池才不會擋住事件迴圈——遙測與 WebSocket
        # 都在同一條上，而地圖一次會要十幾張
        png = await asyncio.get_running_loop().run_in_executor(
            None, terrain.terrarium_tile, terrain.shared(), z, x, y)
        if png is None:
            raise HTTPException(404, "這一區沒有地形資料")
        try:
            _mkcache(os.path.dirname(path))
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(png)
            os.replace(tmp, path)      # 換名是原子的：不會讀到寫一半的圖磚
        except OSError as e:
            log.warning("地形圖磚寫不進快取（%s）——照常回應，只是下次還要再算", e)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


def _mkcache(d: str) -> None:
    """建快取目錄並開放寫入——容器是 root，而離線抓取的腳本跑在 host 上，
    兩邊要寫同一份快取。"""
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o777)
    except OSError:
        pass


ORTHO_CACHE = os.environ.get("ORTHO_CACHE", "/data/ortho")
ORTHO_UPSTREAM = ("https://wmts.nlsc.gov.tw/wmts/PHOTO2/default/"
                  "GoogleMapsCompatible/{z}/{y}/{x}")


@router.get("/ortho/{z}/{x}/{y}.jpg")
async def ortho_tile(z: int, x: int, y: int):
    """NLSC 正射影像（PHOTO2）。**先看自己的快取，沒有才上游抓。**

    現場是離線的，所以圖磚要在有網路的時候先抓好
    （`scripts/fetch-ortho.py`）。這個端點在開發機上會順手補齊快取，
    在現場則純粹是本地檔案伺服器——抓不到就 404，讓 maplibre 跳過那一格。
    """
    if not (0 <= z <= 21 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(400, "z/x/y 超出範圍")
    path = os.path.join(ORTHO_CACHE, str(z), str(x), f"{y}.jpg")
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
    else:
        url = ORTHO_UPSTREAM.format(z=z, y=y, x=x)

        def grab():
            req = urllib.request.Request(url, headers={"User-Agent": "uav-gcs"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.read()

        try:
            data = await asyncio.get_running_loop().run_in_executor(None, grab)
        except Exception as e:                                  # noqa: BLE001
            raise HTTPException(404, f"這一格沒有影像（{e}）") from e
        if not data.startswith(b"\xff\xd8"):
            raise HTTPException(404, "上游回的不是 JPEG")
        try:
            _mkcache(os.path.dirname(path))
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("正射影像寫不進快取（%s）", e)
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=604800"})


class NearIn(BaseModel):
    """航線沿線的建物。**範圍跟著線走，不是一個固定方框。**"""
    points: list[list[float]] = Field(default_factory=list, max_length=2000)
    buffer_m: float = Field(default=30.0, ge=1.0, le=500.0)


BUNDLE_DIR = os.environ.get("BUNDLE_DIR", "/data/bundles")


def _bundle_path(name: str) -> str:
    # **名字只能是一段檔名。** 這個端點會把它接進路徑，`../` 就是讀別人的檔
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "資料包名稱不合法")
    d = os.path.join(BUNDLE_DIR, name)
    if not os.path.isdir(d):
        raise HTTPException(404, f"沒有這一份資料包：{name}")
    return d


@router.get("/bundles")
async def list_bundles():
    """有哪幾份場域資料包。"""
    out = []
    if os.path.isdir(BUNDLE_DIR):
        for n in sorted(os.listdir(BUNDLE_DIR)):
            f = os.path.join(BUNDLE_DIR, n, "manifest.json")
            if not os.path.exists(f):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    m = json.load(fh)
            except (OSError, ValueError):
                continue
            out.append({"name": n, "created_at": m.get("created_at"),
                        "bbox": m.get("bbox"), "centre": m.get("centre"),
                        "complete": all(m["layers"][k].get("complete", True)
                                        for k in ("terrain", "ortho"))})
    return {"bundles": out}


@router.get("/bundles/{name}")
async def get_bundle(name: str):
    """一份資料包的 manifest——**這是給其他系統的介面**。

    裡面除了「有什麼」，還有每一種來源的 `caveat`。那不是文件，是介面的
    一部分：拿到一堆 PNG 的人不知道那是 terrarium 編碼、不知道地面線畫不出
    任何一棟樓、不知道八成建物的高度是猜的。**資料自己要說得出這些**，
    否則接收端會把一份 30 m 格子的表面當成地形圖用。
    """
    d = _bundle_path(name)
    with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    m.pop("_files", None)          # 檔案清單是打包用的，介面不必看
    return m


@router.get("/bundles/{name}/buildings.geojson")
async def bundle_buildings(name: str):
    d = _bundle_path(name)
    p = os.path.join(d, "buildings.geojson")
    if not os.path.exists(p):
        raise HTTPException(404, "這一份沒有建物資料")
    return FileResponse(p, media_type="application/geo+json")


@router.get("/bundles/{name}/archive.tar")
async def bundle_archive(name: str):
    """整包帶走：manifest ＋ 建物 ＋ 這個範圍用得到的圖磚。

    圖磚**不在資料包目錄裡**，是打包這一刻從共用快取抓出來的——複製一份
    到 `data/bundles/` 只會讓同一張圖磚在磁碟上有兩份，而且會過期。
    """
    d = _bundle_path(name)
    with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    files = man.get("_files") or {}

    def gen():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|") as tar:
            for fn in ("manifest.json", "buildings.geojson"):
                p = os.path.join(d, fn)
                if os.path.exists(p):
                    tar.add(p, arcname=f"{name}/{fn}")
            for kind, cache, ext in (("terrain", TILE_CACHE, "png"),
                                     ("ortho", ORTHO_CACHE, "jpg")):
                for rel in files.get(kind) or []:
                    p = os.path.join(cache, rel)
                    if os.path.exists(p):
                        tar.add(p, arcname=f"{name}/{kind}/{rel}")
                    if buf.tell() > 1 << 20:
                        yield buf.getvalue()
                        buf.seek(0)
                        buf.truncate()
        yield buf.getvalue()

    return StreamingResponse(gen(), media_type="application/x-tar", headers={
        "Content-Disposition": f'attachment; filename="{name}.tar"'})


@router.post("/buildings/near")
async def buildings_near(body: NearIn):
    """離這條航線 `buffer_m` 以內的每一棟樓，**帶長寬高**。

    長寬來自輪廓的最小面積外接矩形——輪廓是量出來的（OSM 足跡，公尺級）。
    **高度不是同一種東西**：多半是樓層數推算或根本沒量過，所以
    `height_source` 一定跟著出去，畫面不准只顯示一個數字
    （doc/field-3d-model-design.md §9-A）。
    """
    path = [(float(p[0]), float(p[1])) for p in body.points if len(p) >= 2]
    if not path:
        return {"type": "FeatureCollection", "features": [], "meta": {"count": 0}}
    store = buildings.shared()
    feats = []
    for b, d in store.near_path(path, body.buffer_m):
        dm = buildings.dims(b)
        feats.append({
            "type": "Feature",
            "properties": {
                "id": b.id, "name": b.name, "kind": b.kind,
                "height_m": b.height_m, "height_source": b.height_source,
                "known": b.known, "dist_m": d, **dm,
            },
            "geometry": {"type": "Polygon", "coordinates": [
                [[lo, la] for la, lo in b.ring] + [[b.ring[0][1], b.ring[0][0]]]]},
        })
    return {"type": "FeatureCollection", "features": feats,
            "meta": {"count": len(feats), "buffer_m": body.buffer_m,
                     "available": store.available,
                     "known": sum(1 for f in feats if f["properties"]["known"]),
                     "unknown": sum(1 for f in feats if not f["properties"]["known"]),
                     "assumed_default_m": buildings.ASSUMED_DEFAULT_M}}


@router.get("/plans/{plan_id}/profile")
async def mission_profile(plan_id: str, assume_m: float | None = None,
                          rtl_alt_m: float | None = None):
    """剖面圖的資料（issues/048 F1）：沿航線的地面高程與規劃高度。

    **這是那條綠線該有的樣子。** 使用者的原始問題不是沒有警告，是
    「QGC 有一條綠線而我不知道那是什麼」——圖不需要先備知識，
    線穿到地下、或兩條線貼在一起，看一眼就知道。
    """
    row = await db.pool.fetchrow(
        "SELECT home, policy, fence FROM plans WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    rows = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", plan_id)
    wps = []
    for r in rows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w["params"] = pm
        w.update({k: pm.get(k) for k in ("command", "frame", "p1", "p2")})
        wps.append(w)
    home = row["home"]
    if isinstance(home, str):
        home = json.loads(home)
    h = ({"lat": home[0], "lon": home[1]}
         if home and len(home) >= 2 and (home[0] or home[1]) else
         next(({"lat": w["lat"], "lon": w["lon"]} for w in wps
               if w.get("lat") and w.get("lon")), None))
    prof = plan_check.route_profile(wps, h, dem=terrain.shared(),
                                    assume_m=assume_m, rtl_alt_m=rtl_alt_m)
    # **晶片要說的是政策，不是 frame。** 離地面的航線寫進去也是 frame 3，
    # 只看 frame 會顯示「離起飛點」——那正是 09-07 那句誤導
    pol = row["policy"]
    prof["policy"] = json.loads(pol) if isinstance(pol, str) else pol
    fc = row["fence"]
    prof["fence"] = json.loads(fc) if isinstance(fc, str) else fc
    return prof


class SignIn(BaseModel):
    """「我看過了」。**逐條列出你決定照飛的是哪幾條**，不是一個總開關。"""
    acknowledged: list[str] = Field(default_factory=list, max_length=200)
    assume_m: float | None = None
    wp_spd: float | None = None
    wp_radius: float | None = None
    rtl_alt_m: float | None = None
    signed_by: str | None = None


async def _plan_wps(plan_id: str) -> list[dict]:
    rows = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", plan_id)
    out = []
    for r in rows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w["command"] = pm.get("command")
        w["frame"] = pm.get("frame")
        w["params"] = pm
        out.append(w)
    return out


@router.get("/plans/{plan_id}/sign")
async def get_sign(plan_id: str):
    """這一份最近一次被誰、在什麼假設下看過，以及**那次的航點還是不是這一份**。"""
    wps = await _plan_wps(plan_id)
    if not wps:
        raise HTTPException(404, "無此路徑或沒有航點")
    now = plan_check.waypoints_hash(wps)
    fc = await db.pool.fetchval("SELECT fence FROM plans WHERE id = $1", plan_id)
    fnow = plan_check.fence_hash(json.loads(fc) if isinstance(fc, str) else fc)
    row = await db.pool.fetchrow(
        "SELECT * FROM plan_checks WHERE plan_id = $1 "
        "ORDER BY checked_at DESC LIMIT 1", plan_id)
    if row is None:
        return {"signed": False, "stale": False, "hash": now,
                "why": "這一份還沒有人看過檢查結果"}
    d = dict(row)
    wp_stale = d["waypoints_hash"] != now
    fence_stale = d.get("fence_hash") != fnow
    stale = wp_stale or fence_stale
    for k in ("problems", "acknowledged", "limits"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    d["id"] = str(d["id"]); d["plan_id"] = str(d["plan_id"])
    d["checked_at"] = d["checked_at"].isoformat()
    return {"signed": True, "stale": stale, "hash": now, **d,
            "why": ("航點在簽核之後改過了，這份簽核不算數" if wp_stale else
                    "圍欄在簽核之後改過了，這份簽核不算數" if fence_stale else None)}


@router.post("/plans/{plan_id}/sign")
async def sign_plan(plan_id: str, body: SignIn):
    """簽核：**檢查由伺服器自己重跑**，不收畫面送來的結論。

    畫面能決定的只有「哪幾條我看過而且決定照飛」。
    """
    row = await db.pool.fetchrow(
        "SELECT fence, home, firmware_type FROM plans WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    wps = await _plan_wps(plan_id)
    if not wps:
        raise HTTPException(404, "這一份沒有航點")
    fence = row["fence"]
    if isinstance(fence, str):
        fence = json.loads(fence)
    home = row["home"]
    if isinstance(home, str):
        home = json.loads(home)
    chk = plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, fence=fence, autopilot=row["firmware_type"],
        home=home, dem=terrain.shared(), wp_spd=body.wp_spd,
        wp_radius=body.wp_radius, assume_m=body.assume_m,
        rtl_alt_m=body.rtl_alt_m)
    ack = [p for p in body.acknowledged if p in chk["problems"]]
    missed = [p for p in chk["problems"] if p not in ack]
    h = plan_check.waypoints_hash(wps)
    await db.pool.execute(
        "INSERT INTO plan_checks (plan_id, waypoints_hash, ok, problems, "
        "acknowledged, assumed_m, wp_spd, limits, signed_by, fence_hash) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        plan_id, h, chk["ok"], jdumps(chk["problems"]), jdumps(ack),
        body.assume_m, body.wp_spd, jdumps(chk.get("limits")), body.signed_by,
        plan_check.fence_hash(fence))
    return {"ok": chk["ok"], "hash": h, "acknowledged": ack,
            "unacknowledged": missed, "check": chk}


@router.get("/plans/{plan_id}/check")
async def check_mission(plan_id: str, wp_spd: float | None = None,
                        wp_radius: float | None = None,
                        assume_m: float | None = None,
                        rtl_alt_m: float | None = None):
    """任務庫裡某一份的幾何預檢。**檢查不該只在匯入的那一刻做一次。**

    匯入時看到的報告會隨畫面關掉就消失，而使用者是在**要飛之前**才需要它；
    再者圍欄的系統預設值可能在匯入之後被改過，那時候舊報告就是過期的。
    每次點開一份航線就重算一次，成本是一次查表。
    """
    row = await db.pool.fetchrow(
        "SELECT name, fence, home, firmware_type, vehicle_type FROM plans "
        "WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    rows = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", plan_id)
    wps = []
    for r in rows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w["command"] = pm.get("command")
        w["frame"] = pm.get("frame")
        wps.append(w)
    fence = row["fence"]
    if isinstance(fence, str):
        fence = json.loads(fence)
    return plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, fence=fence,
        # frame 規則是方言，要知道是給哪一家寫的才判得了（沒宣告時只警告）
        autopilot=row["firmware_type"],
        home=json.loads(row["home"]) if isinstance(row["home"], str) else row["home"],
        dem=terrain.shared(),
        # **速度只有飛機說得準**：規劃頁連得到機時把讀到的值帶進來，
        # 沒帶就是「沒有檢查」（`leg_profile` 會說出來），不是「通過」
        wp_spd=wp_spd, wp_radius=wp_radius,
        # 未量測建物的假設高度。**不給就不假設**——那時未知的樓是擋下，
        # 不是通過（`libs/buildings.py` 的 ASSUMED_DEFAULT_M 只是畫面的預設值）
        assume_m=assume_m,
        # 失效處置也在同一片地形上（C7）。**沒讀到就是沒判**
        rtl_alt_m=rtl_alt_m)


class PlanOverride(BaseModel):
    """把某一個航點的高度或速度換掉（試算用）。`speed` 改的是**那個航點之後**
    的 `DO_CHANGE_SPEED`——與飛控的語意一致（見 `plan_check.leg_profile`）。"""
    seq: int
    alt: float | None = None
    speed: float | None = None
    lat: float | None = None
    lon: float | None = None
    #: 到點停留（秒，issues/062）。寫進 `NAV_WAYPOINT` 的 param1；0＝不停。
    #: **只對 NAV_WAYPOINT 有效**——起飛、降落的 param1 意思不同
    hold: float | None = Field(None, ge=0, le=plan_check.HOLD_MAX_S)


class FenceIn(BaseModel):
    """畫面上畫的圍欄。**規劃端的約束，飛控不會照它擋**（見 `check_fence`）。"""
    shape: str = "none"       # none／circle／polygon
    #: circle：半徑（公尺），圓心固定是起飛點
    radius_m: float | None = None
    #: polygon：[[lat, lon], …]，少於三點視為沒畫
    points: list[list[float]] = Field(default_factory=list, max_length=200)
    alt_max_m: float | None = None


def _fence_of(f: "FenceIn | None", home: list[float] | None) -> dict | None:
    if f is None or f.shape == "none":
        return None
    if f.shape == "circle":
        if not f.radius_m or not home or len(home) < 2:
            return None
        return plan_check.fence_circle({"lat": home[0], "lon": home[1]},
                                       f.radius_m, f.alt_max_m)
    if f.shape == "polygon":
        return plan_check.fence_polygon([(p[0], p[1]) for p in f.points
                                         if len(p) >= 2], f.alt_max_m) or None
    return None


class PreviewIn(BaseModel):
    overrides: list[PlanOverride] = Field(default_factory=list, max_length=500)
    wp_spd: float | None = None
    wp_radius: float | None = None
    #: 未量測建物的假設高度（公尺）。**不給就不假設**
    assume_m: float | None = None
    #: 機上的返航高度。**沒讀到就不判返航**
    rtl_alt_m: float | None = None
    #: 給了就**存成新的一份**；不給就只算不存
    save_as: str | None = None
    #: 存回**這一份**（覆蓋航點）。與 `save_as` 互斥。
    #: **會讓那一份的人工審查失效**——簽核綁在 waypoints_hash 上
    save: bool = False
    #: 給了就用畫面上這一份圍欄（也會跟著存）；不給就用航線原本宣告的
    fence: FenceIn | None = None


def _apply_overrides(wps: list[dict], ov: list[PlanOverride]) -> list[dict]:
    """把改動套到航點上（就地不動原本那份，回一份新的 list）。"""
    alt = {o.seq: o.alt for o in ov if o.alt is not None}
    spd = {o.seq: o.speed for o in ov if o.speed is not None}
    pos = {o.seq: (o.lat, o.lon) for o in ov if o.lat is not None and o.lon is not None}
    hold = {o.seq: o.hold for o in ov if o.hold is not None}
    bad = [s for s in hold
           if not any(w.get("seq") == s and plan_check._cmd(w) == 16 for w in wps)]
    if bad:
        # **不是航點就不能停**：起飛、降落的 param1 意思不同，照寫會改到別的東西
        raise HTTPException(422, f"seq {'、'.join(map(str, bad))} 不是一般航點，"
                                 "設不了停留時間")
    out = []
    for w in wps:
        w = dict(w)
        if w.get("seq") in alt:
            w["alt"] = alt[w["seq"]]
        if w.get("seq") in hold:
            w["p1"] = float(hold[w["seq"]])
            w["params"] = {**(w.get("params") or {}), "p1": w["p1"]}
        if w.get("seq") in pos:
            w["lat"], w["lon"] = pos[w["seq"]]
        # 速度改的是**那個航點後面**的 DO_CHANGE_SPEED；航線裡沒有的話補一個
        out.append(w)
        s = spd.get(w.get("seq"))
        if s is None:
            continue
        nxt = None
        for x in wps:
            if x.get("seq") == w["seq"] + 1 and (x.get("command") == 178):
                nxt = x
                break
        if nxt is not None:
            continue        # 既有的那一項由下面的迴圈改
        out.append({"seq": w["seq"] + 0.5, "lat": 0, "lon": 0, "alt": 0,
                    "action": "do", "command": 178, "frame": 2, "p2": s})
    # 既有的 DO_CHANGE_SPEED：seq−1 有指定就換掉它的值
    for w in out:
        if w.get("command") == 178:
            s = spd.get((w.get("seq") or 0) - 1)
            if s is not None:
                w["p2"] = s
                w["params"] = {**(w.get("params") or {}), "p2": s}
    return out


@router.post("/plans/{plan_id}/preview")
async def preview_plan(plan_id: str, body: PreviewIn):
    """**試算：套上改動、跑同一套規則、不寫資料庫。**

    規劃頁的高度／速度滑桿走這條路。**規則只有一份**（`libs/plan_check`）
    ——讓前端照著門檻自己再算一次，改了後端的常數畫面不會跟著變，
    而且看起來完全正常（2026-08-26 那個「同源副本早就漂移」的同一種錯）。

    `save_as` 給了才存成**新的一份**，原本那份永遠不動——與「改成地形跟隨」
    同一條紀律：改過的航線是另一份，操作員要先看過再決定要不要飛。
    """
    row = await db.pool.fetchrow(
        "SELECT name, fence, home, firmware_type, vehicle_type, cruise_speed, "
        "hover_speed, rally FROM plans WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    rows = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", plan_id)
    wps = []
    for r in rows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w["params"] = pm
        w.update({k: pm.get(k) for k in ("command", "frame", "p1", "p2", "p3", "p4")})
        wps.append(w)
    wps = _apply_overrides(wps, body.overrides)
    fence = row["fence"]
    if isinstance(fence, str):
        fence = json.loads(fence)
    home = row["home"]
    if isinstance(home, str):
        home = json.loads(home)
    if body.fence is not None:
        fence = _fence_of(body.fence, home)
    h = ({"lat": home[0], "lon": home[1]}
         if home and len(home) >= 2 and (home[0] or home[1]) else
         next(({"lat": w["lat"], "lon": w["lon"]} for w in wps
               if w.get("lat") and w.get("lon")), None))
    check = plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, fence=fence, autopilot=row["firmware_type"],
        home=home, dem=terrain.shared(),
        wp_spd=body.wp_spd, wp_radius=body.wp_radius, assume_m=body.assume_m,
        rtl_alt_m=body.rtl_alt_m)
    profile = plan_check.route_profile(wps, h, dem=terrain.shared(),
                                       assume_m=body.assume_m,
                                       rtl_alt_m=body.rtl_alt_m)
    out: dict = {"check": check, "profile": profile, "saved_id": None}
    if body.save and body.save_as:
        raise HTTPException(422, "save 與 save_as 只能給一個")
    if body.save or body.save_as:
        stored = [{"seq": i, "lat": w.get("lat"), "lon": w.get("lon"),
                   "alt": w.get("alt"), "action": w.get("action") or "waypoint",
                   "command": w.get("command"), "frame": w.get("frame"),
                   **{k: w.get(k) for k in ("p1", "p2", "p3", "p4")}}
                  for i, w in enumerate(wps)]
        if body.save:
            # **存回原檔＝蓋掉航點。** 原本的裁定是「改動不會動到原本那份」
            # （飛過的那一份是紀錄，改它等於改歷史）；2026-09-09 使用者要
            # 加回這條路，所以把後果做成明的：航點一換 `waypoints_hash` 就變，
            # 那一份的人工審查自動失效（`GET /plans/{id}/sign` 會回 stale），
            # 上傳閘門因此會擋下——**不是靜默地放行一份沒人看過的新航點**。
            async with db.pool.acquire() as con:
                async with con.transaction():
                    await con.execute(
                        "DELETE FROM waypoints WHERE plan_id = $1", plan_id)
                    await con.executemany(
                        "INSERT INTO waypoints (plan_id, seq, lat, lon, alt, "
                        "action, params) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        [(plan_id, w["seq"], w["lat"], w["lon"], w.get("alt"),
                          w.get("action") or "waypoint",
                          jdumps({k: w[k] for k in
                                  ("command", "frame", "p1", "p2", "p3", "p4",
                                   "h", "alt_source")
                                  if w.get(k) is not None})
                          if w.get("command") is not None else None)
                         for w in stored])
                    if body.fence is not None:
                        await con.execute(
                            "UPDATE plans SET fence = $2 WHERE id = $1",
                            plan_id, jdumps(fence) if fence else None)
            out["saved_id"] = plan_id
            out["overwrote"] = True
        else:
            rally = row["rally"]
            if isinstance(rally, str):
                rally = json.loads(rally)
            out["saved_id"] = await _store_mission(
                body.save_as.strip() or f"{row['name']}（調整）", "edited", stored,
                row["firmware_type"], row["vehicle_type"], fence, home,
                row["cruise_speed"], row["hover_speed"], rally)
    return out


@router.post("/plans/{plan_id}/add-return")
async def add_return(plan_id: str):
    """**加上回程**：把去程的航點反序接在後面，另存成新的一份（issues/065／066）。

    使用者 2026-09-22 裁定「折返」是**規劃動作**（不是路徑屬性）：按下去回程點就真的
    存在，所見即所得、回程點可以個別改。

    * **另存一份，不改原本那份**：飛過的那一份是紀錄；而且原本那份的簽核綁在它的
      航點上，不能讓一份多了一倍航點的航線頂著舊簽核
    * 回程 ＝ 去程 `NAV_WAYPOINT` 去掉最後一個（那是折返點本身）再反序，插在結尾的
      降落／返航項之前。改速度等指令項不複製——回程沿用最後生效的速度
    * **回程點不帶停留**（param1＝0）：使用者裁定回程停留**可分別設定**，存好之後在
      編輯頁逐點設；照抄去程的停留會讓每個點被量兩倍久而不自知
    """
    row = await db.pool.fetchrow(
        "SELECT name, fence, home, firmware_type, vehicle_type, cruise_speed, "
        "hover_speed, rally FROM plans WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    wps = []
    for r in await db.pool.fetch(
            "SELECT seq, lat, lon, alt, action, params FROM waypoints "
            "WHERE plan_id = $1 ORDER BY seq", plan_id):
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w.update({k: pm.get(k) for k in ("command", "frame", "p1", "p2", "p3", "p4")})
        wps.append(w)
    nav = [w for w in wps if plan_check._cmd(w) == 16 and w.get("lat") and w.get("lon")]
    if len(nav) < 2:
        raise HTTPException(422, "至少要有兩個航點才有「回程」可言")
    tail = len(wps)
    while tail > 0 and plan_check._cmd(wps[tail - 1]) in (20, 21):   # RTL／LAND
        tail -= 1
    back = [{**w, "p1": 0.0} for w in reversed(nav[:-1])]
    new = wps[:tail] + back + wps[tail:]
    stored = [{"seq": i, "lat": w.get("lat"), "lon": w.get("lon"), "alt": w.get("alt"),
               "action": w.get("action") or "waypoint",
               "command": w.get("command"), "frame": w.get("frame"),
               **{k: w.get(k) for k in ("p1", "p2", "p3", "p4")}}
              for i, w in enumerate(new)]
    def _j(v):
        return json.loads(v) if isinstance(v, str) else v
    home = _j(row["home"])
    saved = await _store_mission(
        f"{row['name']}（含回程）", "edited", stored, row["firmware_type"],
        row["vehicle_type"], _j(row["fence"]), home, row["cruise_speed"],
        row["hover_speed"], _j(row["rally"]))
    return {"id": saved, "added": len(back), "from": plan_id}


class PlanPatch(BaseModel):
    name: str


@router.patch("/plans/{plan_id}")
async def patch_plan(plan_id: str, body: PlanPatch):
    """改名。**只有名字**——航點要改走 preview 那條路（它會重跑檢查）。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "名稱不可為空")
    r = await db.pool.execute(
        "UPDATE plans SET name = $1 WHERE id = $2", name, plan_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此路徑")
    return {"ok": True, "name": name}


class ResolveIn(BaseModel):
    """把一條發現變成一個具體的改動（redesign §6）。**規則在後端**。"""
    name: str                        # raise_all／raise_leg／slow_all／…
    seq: int | None = None


class DraftPoint(BaseModel):
    lat: float
    lon: float
    #: 政策單位下的高度。**只有例外才給**——不給就跟著政策走
    h: float | None = None
    #: 這個點之後的速度。同樣只有例外才給
    speed_ms: float | None = None
    #: 到點停留（秒，issues/062）。不給＝不停
    hold_s: float | None = Field(None, ge=0, le=plan_check.HOLD_MAX_S)
    alt_source: str | None = None     # policy／manual
    alt: float | None = None          # 舊欄位，仍收
    kind: str = "wp"          # wp／land


class PolicyIn(BaseModel):
    """高度與速度的**政策**——操作員的意圖。逐點的 `alt` 是它解出來的結果。"""
    mode: str = plan_check.DEFAULT_POLICY_MODE        # agl／home／amsl
    height_m: float = plan_check.DEFAULT_POLICY_HEIGHT_M
    speed_ms: float = plan_check.DEFAULT_POLICY_SPEED_MS
    #: 不給＝跟著政策算（離地 3 m 的航線就從 3 m 起飛）
    takeoff_alt_m: float | None = None
    #: **不預設回起飛點**：那條線是系統加的，畫線時看起來像自己畫錯了
    land_at_home: bool = False
    land_mode: str = "vert"   # vert（飛到定點再垂直降落）／glide（逐漸降落）


class DraftIn(BaseModel):
    """從零產生：一串點 ＋ 一個政策。**先畫線，數字後到**（§3 動作 1）。"""
    home: list[float] = Field(min_length=2, max_length=3)
    points: list[DraftPoint] = Field(default_factory=list, max_length=500)
    policy: PolicyIn = Field(default_factory=PolicyIn)
    #: 給了就先套用這個動作，再算一次。回傳會帶 `applied`
    action: ResolveIn | None = None
    wp_spd: float | None = None
    wp_radius: float | None = None
    #: 未量測建物的假設高度（公尺）。**不給就不假設**
    assume_m: float | None = None
    #: 機上的返航高度。**沒讀到就不判返航**
    rtl_alt_m: float | None = None
    #: 畫面上畫的圍欄
    fence: FenceIn | None = None
    save_as: str | None = None


@router.post("/plans/draft")
async def draft_plan(body: DraftIn):
    """**從零產生**：把點組成航線、跑同一套檢查、預設不存。

    使用者 2026-09-08：「先輸入經緯度，然後讓使用者用點位的方式規劃路線」。
    起飛項、`frame`、改速度項、降落項由系統補（見 `plan_check.build_plan`）
    ——那些正是 QGC／Mission Planner 要求使用者自己先知道的東西。

    `save_as` 給了才寫進任務庫。**沒給就只算不存**，與改既有航線那條路
    同一個規矩。
    """
    h = {"lat": body.home[0], "lon": body.home[1]}
    pol = body.policy.model_dump()
    pts = [p.model_dump() for p in body.points]
    fence = _fence_of(body.fence, body.home)
    assume = body.assume_m
    built = plan_check.build_plan(pts, pol, h, dem=terrain.shared())
    applied = None
    if body.action and body.points:
        # **先算一次才知道要改什麼**：抬多少、降到多少都是從發現算出來的
        pre = plan_check.check_waypoints(
            built["waypoints"], settings.geofence_radius_m,
            settings.geofence_alt_m, settings.geofence_margin,
            dem=terrain.shared(), home=body.home, wp_spd=body.wp_spd,
            wp_radius=body.wp_radius, assume_m=assume,
            rtl_alt_m=body.rtl_alt_m, fence=fence)
        res = plan_check.resolve(body.action.name, pre, body.action.seq)
        applied = res
        if res["kind"] == "assume":
            assume = res["assume_m"]
        elif res["kind"] in ("policy", "leg"):
            pol, pts = plan_check.apply_resolution(res, pol, pts,
                                                   built["waypoints"])
            built = plan_check.build_plan(pts, pol, h, dem=terrain.shared())
    wps, decisions = built["waypoints"], built["decisions"]
    if len(body.points) < 1:
        # **一個點都沒有時不要假裝算得出什麼**：`check` 是 None，讓畫面說
        # 「還沒放點」，而不是回一份「通過」的報告。
        #
        # **但起飛點本身要畫得出來**（使用者 2026-09-09）：放完起飛點就該
        # 看得到它、也該能設它的高度。所以剖面照給——只是那份剖面裡只有
        # 起飛點，沒有降落（還沒有航線，就沒有「飛完回來」這件事）。
        solo = plan_check.build_plan(
            [], {**pol, "land_at_home": False}, h, dem=terrain.shared())
        prof = plan_check.route_profile(solo["waypoints"], h,
                                        dem=terrain.shared(),
                                        assume_m=assume,
                                        rtl_alt_m=body.rtl_alt_m)
        prof["policy"] = pol
        return {"check": None, "profile": prof, "saved_id": None,
                "waypoints": solo["waypoints"], "decisions": solo["decisions"],
                "policy": pol, "points": pts, "assume_m": assume}
    check = plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, dem=terrain.shared(),
        home=body.home, wp_spd=body.wp_spd, wp_radius=body.wp_radius,
        assume_m=assume, rtl_alt_m=body.rtl_alt_m, fence=fence)
    profile = plan_check.route_profile(wps, h, dem=terrain.shared(),
                                       assume_m=assume,
                                       rtl_alt_m=body.rtl_alt_m)
    saved = None
    if body.save_as:
        saved = await _store_mission(body.save_as.strip() or "新航線", "drawn",
                                     wps, home=body.home, policy=pol,
                                     fence=fence)
    if profile is not None:
        profile["policy"] = pol
    return {"check": check, "profile": profile, "saved_id": saved,
            "waypoints": wps, "decisions": decisions,
            # 套用了什麼、以及套用之後的政策與點——畫面要拿它更新自己的狀態
            "applied": applied, "policy": pol, "points": pts,
            "assume_m": assume}


@router.post("/plans/{plan_id}/activate")
async def activate_mission(plan_id: str, active: bool = True):
    async with db.pool.acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE plans SET is_active = false WHERE is_active")
            if active:
                r = await con.execute(
                    "UPDATE plans SET is_active = true WHERE id = $1", plan_id)
                if r.split()[-1] == "0":
                    raise HTTPException(404, "無此路徑")
    return {"ok": True}


@router.post("/plans/{plan_id}/show")
async def show_mission(plan_id: str, why: str = "", sysid: int | None = None):
    """**這份航線就是機上現在的那份 → 畫到即時頁上，並留一筆事件。**

    與 `/activate` 的差別是**語意**，不是行為：`activate` 是人手動切換要看哪
    一份（低頻、不必留痕）；這個是系統在說「機上的航線剛剛變成這一份」。
    後者一定要留痕——上傳與改航線都是會改變飛機行為的操作，事後查案時
    「畫面上那時候畫的是哪一條」是關鍵事實。

    由指令服務在上傳成功／改航線完成後呼叫。
    """
    row = await db.pool.fetchrow("SELECT name FROM plans WHERE id = $1",
                                 plan_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    async with db.pool.acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE plans SET is_active = false WHERE is_active")
            await con.execute("UPDATE plans SET is_active = true WHERE id = $1",
                              plan_id)
    drone_id = None
    if sysid is not None:
        r = await db.pool.fetchrow(
            "SELECT id::text AS id FROM drones WHERE mav_sysid = $1", sysid)
        drone_id = r["id"] if r else None
    try:
        ev = await db.insert_event(
            drone_id, None, "info", "mission_shown",
            {"plan_id": plan_id, "mission": row["name"], "why": why})
        ev["drone"] = None
        await manager.broadcast({"type": "event", "event": ev})
    except Exception:
        log.exception("航線顯示事件寫入失敗（不影響顯示本身）")
    return {"ok": True, "mission": row["name"]}


@router.delete("/plans/{plan_id}")
async def delete_mission(plan_id: str):
    r = await db.pool.execute("DELETE FROM plans WHERE id = $1", plan_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此路徑")
    return {"ok": True}  # waypoints 由 FK CASCADE 一併刪除


# ── 機上 5G 量測回傳（真機階段）────────────────────────────────────────────
# 設計見 doc/onboard-telemetry.md。兩條通道分工：
#   live  即時通道：只送最新一筆、失敗不重試、只更新 live state 不入庫
#   batch 記錄通道：送未確認的樣本、重試到成功、唯一的入庫路徑
# 分開的理由是鏈路差時小封包還擠得過去、大批次則否；共用一條通道會讓
# 「為完整性而重試」卡住即時性，操作員看到的是舊資料卻以為是現況。

class LinkSample(BaseModel):
    """機上一次採樣的完整結果。欄位對應 RM500Q-GL 的 AT+QENG 回應。"""
    drone_id: str | None = None     # 多機：這筆樣本屬於哪台（不填＝主機）
    seq: int | None = None          # 機上單調遞增序號，只用於批次確認，不入庫
    time: datetime                  # 機上採樣時刻，須含時區
    lat: float | None = None        # 採樣當下位置，機上從 PX4 取得後綁進同一筆
    lon: float | None = None
    alt_rel: float | None = None
    rsrp: float | None = None
    rsrq: float | None = None
    sinr: float | None = None       # 干擾研究主指標，取自 AT+QENG 而非 QMI 的 SNR
    cqi: int | None = None
    pci: int | None = None
    cell_id: int | None = None      # 全域識別碼 NCI/CGI，AT+QENG 的 <cellID>
    band: str | None = None
    nr_mode: str | None = None
    rtt_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_pct: float | None = None
    throughput_up_kbps: float | None = None
    throughput_down_kbps: float | None = None
    in_interference_zone: bool | None = None
    raw: dict | None = None         # modem 原始回應，便於事後追查
    #: 機上時鐘對過了沒（2026-09-14）。False＝`time` 是錯的：Pi 換電池重開時
    #: 牆鐘停在上次存下的時間，要等對時才跳上來。不填＝舊版代理，照舊採信
    clock_synced: bool | None = None


class LinkBatch(BaseModel):
    drone_id: str | None = None     # 省略則用目前註冊的無人機（單機情境）
    samples: list[LinkSample] = Field(min_length=1, max_length=1000)


def _resolve_drone(drone_id: str | None) -> str:
    resolved = drone_id or live.drone_id
    if not resolved:
        raise HTTPException(503, "系統尚未完成初始化，無人機未註冊")
    return resolved


def _require_modem_mode() -> None:
    """simulated 模式下拒收 push，避免兩個寫入者打架。

    模擬迴圈每秒更新 live.link 並寫 link_metrics；此時若還接受外部 POST，
    live state 會被兩邊輪流覆蓋、link_metrics 出現兩套來源混雜的資料。
    實際發生過：殘留的 fake-onboard-node 讓 simulated 模式廣播出 source=modem，
    診斷了一輪才發現。拒收並講明原因，好過靜默接受後產生混料。
    """
    if settings.link_source != "modem":
        raise HTTPException(
            409, f"link_source={settings.link_source}，push 端點僅在 modem 模式開放"
            "（模擬迴圈是這個模式下唯一的鏈路資料寫入者）")


def _require_aware(ts: datetime) -> datetime:
    """拒收沒有時區的時間戳。

    機上送來的時間是資料唯一的時間依據——沒有時區就無法確定它代表哪個瞬間，
    寫進 TIMESTAMPTZ 會被當成伺服器時區而靜默偏移。寧可拒收也不要污染資料。
    """
    if ts.tzinfo is None:
        raise HTTPException(422, f"時間戳必須含時區（收到 {ts.isoformat()}）")
    return ts


#: 機上時鐘跟地面站差多少才喊（秒）。即時樣本走 5G 過來本身有延遲，單筆不準，
#: 看最近 10 筆的中位數
CLOCK_SKEW_WARN_S = 1.5
_skew: dict[str, deque] = {}
_skew_warned: dict[str, float] = {}


async def _watch_clock_skew(target, s: "LinkSample") -> None:
    """**兩邊時間要對得齊**（使用者 2026-09-14）。機上說時鐘對過了，它標的時間
    應該只比地面站收到的這一刻早一個傳輸延遲；差得比那多，就是兩邊時鐘不一致
    ——訊號、補傳、事件的時間會對不齊，而且沒有任何地方會報錯。"""
    key = s.drone_id or "primary"
    d = _skew.setdefault(key, deque(maxlen=10))
    d.append((datetime.now(timezone.utc) - s.time).total_seconds())
    if len(d) < d.maxlen:
        return
    med = sorted(d)[len(d) // 2]
    if abs(med) < CLOCK_SKEW_WARN_S or time.monotonic() - _skew_warned.get(key, -1e9) < 600:
        return
    _skew_warned[key] = time.monotonic()
    did = getattr(target, "drone_id", None)
    msg = f"機上時鐘與地面站差 {med:+.1f} 秒——訊號、補傳、事件的時間會對不齊"
    if not did:
        log.warning("%s（%s）", msg, key)
        return
    try:
        ev = await db.insert_event(did, getattr(target, "session_id", None), "warn",
                                   "clock_skew", {"skew_s": round(med, 2), "msg": msg})
        await manager.broadcast({"type": "event", "event": ev})
    except Exception:
        log.exception("時鐘偏差事件寫入失敗")


@router.post("/link-metrics/live")
async def link_metrics_live(s: LinkSample):
    """即時通道：更新 live state 並跑鏈路狀態機。**不寫資料庫。**

    **回應帶著「我最後收到你的遙測是多久以前」**（issues/047 項次 6）。
    機上唯一能知道「我送的東西有沒有到」的方法，是地面站回話——而這條
    每秒一次的通道本來就在跑，不必另開一個。回的是 `telem_age_s`
    （**後端自己的單調時鐘算的秒數**，不是時間戳），所以兩邊時鐘差多少
    都不影響；機上只要記住「這一刻我得到過確認」。

    在這之前機上是用 `gs_link_ok`（＝我聽不聽得到地面站）來決定要不要
    緩衝與暫停回傳——2026-09-08 實測那是錯的：單向中斷時它一邊每秒送出
    九十幾則遙測，一邊宣告地面站失聯。

    不入庫是為了避免與記錄通道重複寫入——live 只負責顯示，記錄通道負責留存，
    職責不重疊就不需要去重邏輯。

    機上送失敗時不該重試：下一秒的新樣本本來就會取代這一筆，重試只會佔用
    本來就不夠的頻寬。
    """
    _require_modem_mode()
    _require_aware(s.time)
    # 多機：樣本自帶 drone_id 決定更新哪台的 live state（不填＝主機）
    from .state import fleet
    target = fleet.get(s.drone_id) if s.drone_id else live
    if target is None:
        raise HTTPException(404, f"未知的 drone_id：{s.drone_id}（該機尚未註冊）")
    if s.clock_synced is False:
        # 即時樣本是當下送的，機上時鐘沒對過時改用收到的這一刻——比錯的時間準
        s = s.model_copy(update={"time": datetime.now(timezone.utc)})
    else:
        await _watch_clock_skew(target, s)
    # mode="json" 讓 datetime 變成 ISO 字串。live.link 會被 WebSocket 廣播出去，
    # 放進 datetime 物件會讓 json.dumps 拋錯而整個廣播迴圈死掉。
    m = s.model_dump(mode="json", exclude_none=False)
    m.pop("drone_id", None)
    m.pop("clock_synced", None)
    m["source"] = "modem"
    # **哨兵值先拿掉再說**：模組在受限服務下會把 SINR 回成無效標記
    # （實測 -3276），照單全收的話畫面會把它當成「最差 -3276 dB」。
    # 拿掉的值寫進 raw._dropped（見 modem_raw.drop_sentinels）
    modem_raw.drop_sentinels(m)
    # **值一直都在 raw 裡，只是沒有人解**（modem_raw.py）：pci／cell_id／band
    # 三欄今天全是 null，而畫面上的「—」會被讀成「這個場域量不到細胞資訊」。
    # 只補 null，不覆蓋機上自己填的
    modem_raw.enrich(m)
    target.link = m
    target.mark_link_seen()
    await link_transition(target, m)
    # `telem_age_s` 是 None ＝**從來沒收到過這台機的遙測**，那與「很久沒收到」
    # 不同（機上據此判斷時要當成「還沒確認過」，不是「剛確認過」）
    return {"uplink": {"telem_age_s": target.telem_age_s}}


@router.post("/link-metrics/batch")
async def link_metrics_batch(batch: LinkBatch):
    """記錄通道：唯一的入庫路徑。冪等，可安全重送。

    `session_id` 用樣本自帶的時間戳反查涵蓋它的架次，而非「當前架次」——
    補傳資料抵達時飛機可能早已上鎖。見 doc/onboard-telemetry.md。

    回應的 `accepted_seq` **包含落在架次外而被丟棄的樣本**，機上據此標記可刪除。
    若不回報這些，機上會永遠重送那些本來就不該保留的資料。
    """
    _require_modem_mode()
    drone_id = _resolve_drone(batch.drone_id)
    accepted, stored, duplicate, outside = [], 0, 0, 0

    for s in batch.samples:
        _require_aware(s.time)
        # 這裡不能用 mode="json"：time 要保持 datetime 才能寫進 TIMESTAMPTZ
        m = s.model_dump(exclude_none=False)
        m.pop("clock_synced", None)
        m["source"] = "modem"
        modem_raw.drop_sentinels(m)  # 同 live 那條路（見 modem_raw.py）
        modem_raw.enrich(m)
        session_id = await db.find_session_at(drone_id, s.time)
        if session_id is None:
            outside += 1                      # 架次外：等同 issues/004 的 gate，丟棄
        elif await db.insert_link_sample(drone_id, session_id, m):
            stored += 1
        else:
            duplicate += 1                    # 已存在，重送造成，視為成功
        if s.seq is not None:
            accepted.append(s.seq)

    return {"accepted_seq": accepted, "stored": stored,
            "duplicate": duplicate, "outside_session": outside}

# ── 地址定位（使用者裁定 2026-09-11 選 B）──────────────────────────────
# **查不到門牌就往上退一層，並說出退到哪。** OSM 的台灣門牌很稀疏（實測
# 「光復路二段101號」「中興路四段195號」都是 0 筆），精準到門牌的 TGOS
# 要申請——PoC 階段先不問。查詢是送到外部服務的：現場沒網路時查不了，
# 貼座標照樣能用（那一條不上網）。

_GEO_UA = "uav-system-poc/0.1 (ground station route planning)"
#: Nominatim 的使用規範：一秒最多一次。**不能邊打邊查**，所以是按 Enter 才查
_GEO_GAP_S = 1.1
_geo_lock = asyncio.Lock()
_geo_last = [0.0]
_geo_cache: dict[str, list] = {}
_COORD_RE = re.compile(r"(-?\d{1,3}\.\d+)\s*[,，\s]\s*(-?\d{1,3}\.\d+)")
_GEO_AREA = {"city", "town", "village", "suburb", "city_district", "district",
             "county", "state", "hamlet", "neighbourhood", "quarter",
             "municipality", "borough"}


def _geo_precision(t: str | None) -> str:
    if t in ("house", "building"):
        return "門牌"
    if t in ("road", "street"):
        return "路段"
    if t in _GEO_AREA:
        return "行政區"
    return "地標"


def _geo_fallbacks(q: str) -> list[str]:
    """門牌 → 巷弄 → 段 → 路。每退一層都是一個新的查詢。"""
    out = [q]
    for pat in (r"\d+(之\d+)?號.*$", r"\d+[巷弄].*$", r"[一二三四五六七八九十\d]+段.*$"):
        nxt = re.sub(pat, "", out[-1]).strip()
        if nxt and nxt != out[-1]:
            out.append(nxt)
    return out


def _geo_name(display: str) -> str:
    parts = [x.strip() for x in display.split(",")]
    parts = [x for x in parts if x and not x.isdigit() and x not in ("臺灣", "台灣")]
    return "・".join(parts[:3])


async def _nominatim(q: str) -> list[dict]:
    if q in _geo_cache:
        return _geo_cache[q]
    url = ("https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5"
           "&countrycodes=tw&accept-language=zh-TW&q=" + urllib.parse.quote(q))
    req = urllib.request.Request(url, headers={"User-Agent": _GEO_UA})
    async with _geo_lock:
        wait = _GEO_GAP_S - (time.monotonic() - _geo_last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=8).read())
        finally:
            _geo_last[0] = time.monotonic()
    res = [{"name": _geo_name(x.get("display_name", "")),
            "lat": float(x["lat"]), "lon": float(x["lon"]),
            "precision": _geo_precision(x.get("addresstype"))}
           for x in json.loads(raw)]
    _geo_cache[q] = res
    return res


@router.get("/geocode")
async def geocode(q: str):
    """地址、地標或座標 → 大致位置。**起飛點還是使用者自己點**，這裡只負責
    把地圖移過去。"""
    q = (q or "").strip()
    if not q:
        raise HTTPException(422, "沒有要查的東西")
    m = _COORD_RE.search(q)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if abs(a) > 90 >= abs(b):
            a, b = b, a                 # 有人會先寫經度
        if abs(a) <= 90 and abs(b) <= 180:
            return {"query": q, "used": q, "fallback": False, "source": "coord",
                    "results": [{"name": f"{a:.6f}, {b:.6f}", "lat": a, "lon": b,
                                 "precision": "座標"}]}
    for i, cand in enumerate(_geo_fallbacks(q)):
        try:
            res = await _nominatim(cand)
        except (OSError, ValueError) as e:
            raise HTTPException(503, {
                "msg": "連不上地址服務（OSM Nominatim）",
                "hint": "現場沒網路時直接貼座標，例如 24.7734, 121.0459",
                "error": str(e)}) from e
        if res:
            return {"query": q, "used": cand, "fallback": i > 0, "source": "osm",
                    "results": res}
    return {"query": q, "used": None, "fallback": False, "source": "osm",
            "results": []}
