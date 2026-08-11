import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from . import db, groups, mavlink_rx, plan_check, swarm_sim
from .config import settings
from .link_events import transition as link_transition
from .state import live

router = APIRouter(prefix="/api")


@router.get("/drones")
async def list_drones():
    from .state import fleet
    from .mavlink_rx import autopilot_name
    rows = await db.pool.fetch("SELECT * FROM drones ORDER BY created_at")
    out = []
    for r in rows:
        d = dict(r)
        # autopilot：從即時 fleet 帶（runtime，非 DB 欄位）；沒連過 MAVLink＝null
        st = fleet.get(str(d["id"]))
        d["autopilot"] = (autopilot_name(st.autopilot_raw)
                          if st and st.autopilot_raw is not None else None)
        out.append(d)
    return out


class DroneIn(BaseModel):
    name: str
    connection_url: str | None = None
    note: str | None = None


@router.post("/drones")
async def register_drone(d: DroneIn):
    """註冊一台無人機（真機階段用；模擬機由 backend 啟動時自動註冊）。"""
    row = await db.pool.fetchrow(
        """
        INSERT INTO drones (name, serial_no, is_simulated, connection_url, status)
        VALUES ($1, $1, false, $2, 'idle')
        ON CONFLICT (serial_no) DO NOTHING
        RETURNING *
        """,
        d.name, d.connection_url,
    )
    if row is None:
        raise HTTPException(409, f"名稱 {d.name} 已存在")
    return dict(row)


class DronePatch(BaseModel):
    name: str | None = None
    video_url: str | None = None      # 空字串＝清除


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
    if "video_url" in fields:
        fields["video_url"] = (fields["video_url"] or "").strip() or None
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    r = await db.pool.execute(
        f"UPDATE drones SET {sets} WHERE id = $1", drone_id, *fields.values())
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此無人機")
    if live.drone_id == drone_id and "name" in fields:
        live.drone_name = fields["name"]   # 主機改名即時反映（事件標籤、側欄）
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
    async with db.pool.acquire() as con:
        async with con.transaction():
            counts = {}
            for table in ("telemetry", "link_metrics", "events", "flight_sessions"):
                r = await con.execute(f"DELETE FROM {table} WHERE drone_id = $1", drone_id)
                counts[table] = int(r.split()[-1])
            # 任務不陪葬：路徑不綁機（issue 010），只解除關聯。
            # 不處理會踩 missions.drone_id 的 FK → 整筆交易 500。
            r = await con.execute(
                "UPDATE missions SET drone_id = NULL WHERE drone_id = $1", drone_id)
            counts["missions_unlinked"] = int(r.split()[-1])
            r = await con.execute("DELETE FROM drones WHERE id = $1", drone_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此無人機")
    return {"deleted": counts}


@router.get("/live")
async def live_snapshot():
    """主機的即時狀態快照（與 WS 廣播同一份資料）。

    給不方便開 WebSocket 的取用端：驗收腳本（scripts/check-onboard.py）、
    curl 排查。輪詢頻率不宜超過 broadcast_hz，即時顯示請走 WS。
    """
    return live.telemetry_dict()


class GroupDroneIn(BaseModel):
    drone_id: str
    layer_index: int | None = None
    mission_id: str | None = None     # separate 模式各自任務


class GroupIn(BaseModel):
    name: str
    mode: str = "unified"             # unified / separate
    base_mission_id: str | None = None
    drones: list[GroupDroneIn] = Field(min_length=1, max_length=8)
    params: dict | None = None


@router.post("/groups")
async def create_group(g: GroupIn):
    """建群組任務（issue 013-A）：unified 從 base 展開 per-drone 具體任務、
    separate 用各自任務。回 assignments＋跨路徑衝突預檢（capability 嚴格
    gate 在 execute／command 服務，此處只做幾何互檢＋materialize）。"""
    if g.mode == "unified" and not g.base_mission_id:
        raise HTTPException(422, "unified 模式需要 base_mission_id")
    if g.mode == "separate" and any(d.mission_id is None for d in g.drones):
        raise HTTPException(422, "separate 模式每台需要 mission_id")
    try:
        return await groups.create_group(
            g.name, g.mode, g.base_mission_id,
            [d.model_dump() for d in g.drones], g.params)
    except groups.GroupError as e:
        raise HTTPException(422, str(e))


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
async def list_sessions(limit: int = 50, mission_id: str | None = None,
                        since: str | None = None, min_samples: int | None = None,
                        include_test: bool = False):
    """架次清單。可選 mission_id（綁定任務）／since（ISO 時間窗）／min_samples／include_test。

    `min_samples`：只回鏈路樣本數 ≥ 此值的架次（場域訊號頁用，避免空/測試殘留架次
    佔滿載入窗——空架次不是「飛行」，誠實原則）。用架次結束時算好的 summary.samples_total
    篩，不掃 link_metrics；未結束（summary NULL）視為 0、min_samples≥1 時自然排除。

    `include_test`：預設 False＝隱藏 origin='test' 的架次（測試殘留混研究庫的治理；
    見 backfill-session-origin.sql）；True 顯示全部。'unknown'／'research'／NULL 一律顯示。"""
    conds, args = [], []
    if not include_test:
        conds.append("COALESCE(s.origin, 'unknown') <> 'test'")
    if mission_id:
        args.append(mission_id)
        conds.append(f"s.mission_id = ${len(args) + 1}")
    if since:
        args.append(since)
        conds.append(f"s.started_at >= ${len(args) + 1}::text::timestamptz")
    if min_samples is not None:
        args.append(min_samples)
        conds.append(
            f"COALESCE((s.summary->>'samples_total')::int, 0) >= ${len(args) + 1}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    q = f"""
        SELECT s.*, d.name AS drone_name, m.name AS mission_name
        FROM flight_sessions s
        JOIN drones d ON d.id = s.drone_id
        LEFT JOIN missions m ON m.id = s.mission_id
        {where}
        ORDER BY s.started_at DESC LIMIT $1
        """
    rows = await db.pool.fetch(q, limit, *args)
    return [dict(r) for r in rows]


class SessionPatch(BaseModel):
    note: str | None = None            # 自訂備註（實驗條件標註）；空字串/None＝清除


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: str, body: SessionPatch):
    """更新架次自訂備註（使用者標實驗條件，如「開干擾器那趟」）。"""
    note = (body.note or "").strip() or None
    row = await db.pool.fetchrow(
        "UPDATE flight_sessions SET note = $2 WHERE id = $1 RETURNING id::text, note",
        session_id, note)
    if row is None:
        raise HTTPException(404, "無此架次")
    return {"id": row["id"], "note": row["note"]}


@router.get("/sessions/{session_id}/track")
async def session_track(session_id: str):
    """回放用：一條航線的軌跡 + 鏈路時序 + 關聯任務（供疊預計路徑）。"""
    sess = await db.pool.fetchrow(
        """SELECT s.id, s.mission_id, s.started_at, s.ended_at, m.name AS mission_name
           FROM flight_sessions s LEFT JOIN missions m ON m.id = s.mission_id
           WHERE s.id = $1""", session_id)
    telemetry = await db.pool.fetch(
        "SELECT * FROM telemetry WHERE session_id = $1 ORDER BY time", session_id)
    link = await db.pool.fetch(
        "SELECT * FROM link_metrics WHERE session_id = $1 ORDER BY time", session_id)
    return {"session": dict(sess) if sess else None,
            "telemetry": [dict(r) for r in telemetry],
            "link": [dict(r) for r in link]}


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str):
    """整條航線匯出成單一 JSON（lossless，可離線分析或封存）。
    配合 30 天 retention：要長期保留原始資料就先匯出；
    匯出後可呼叫 DELETE 移除 DB 內的資料（UI 的「匯出並移除」流程）。"""
    sess = await db.pool.fetchrow(
        """SELECT s.*, d.name AS drone_name, m.name AS mission_name
           FROM flight_sessions s
           JOIN drones d ON d.id = s.drone_id
           LEFT JOIN missions m ON m.id = s.mission_id WHERE s.id = $1""", session_id)
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


@router.get("/events")
async def list_events(limit: int = 100, session_id: str | None = None):
    if session_id:
        rows = await db.pool.fetch(
            "SELECT * FROM events WHERE session_id = $1 ORDER BY time LIMIT $2", session_id, limit)
    else:
        rows = await db.pool.fetch("SELECT * FROM events ORDER BY time DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# ── 任務疊圖（roadmap 3）──────────────────────────────────────────────────
# 從飛機讀回 QGC 上傳的任務。MAVLink 任務下載是**唯讀**操作，
# 不違反「backend 對 MAVLink 只讀不寫」——上傳與啟動仍由 QGC 負責。

# MAV_CMD：帶座標的導航類指令（16 WAYPOINT / 21 LAND / 22 TAKEOFF）
_NAV_CMDS = {16, 21, 22}


@router.get("/mission/current")
async def current_mission():
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


class MissionIn(BaseModel):
    name: str
    source: str = "plan-file"        # plan-file / vehicle
    waypoints: list[WaypointIn] = Field(min_length=2, max_length=500)


async def _store_mission(name: str, source: str, wps: list[dict]) -> str:
    async with db.pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "INSERT INTO missions (name, created_by) VALUES ($1, $2) RETURNING id",
                name, source)
            await con.executemany(
                """INSERT INTO waypoints (mission_id, seq, lat, lon, alt, action, params)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                [(row["id"], w["seq"], w["lat"], w["lon"], w.get("alt"),
                  w.get("action", "waypoint"),
                  # MAVLink 保真度塞 params JSONB（閒置欄位正好承接）
                  json.dumps({k: w[k] for k in ("command", "frame", "p1", "p2", "p3", "p4")
                              if w.get(k) is not None}) if w.get("command") is not None else None)
                 for w in wps])
    return str(row["id"])


@router.get("/missions")
async def list_missions():
    # 排除群組任務地面生成的具體任務（created_by='group-gen'）——那是編隊每台的
    # materialized 任務、不是任務庫草稿，會污染一般任務清單 UI（issue 013-B 前端回報）。
    rows = await db.pool.fetch("""
        SELECT m.id, m.name, m.created_by AS source, m.created_at, m.is_active,
               count(w.seq) AS waypoint_count
        FROM missions m LEFT JOIN waypoints w ON w.mission_id = m.id
        WHERE m.created_by IS DISTINCT FROM 'group-gen'
        GROUP BY m.id ORDER BY m.created_at DESC""")
    return [dict(r) for r in rows]


@router.get("/missions/active")
async def active_mission():
    row = await db.pool.fetchrow("SELECT id, name FROM missions WHERE is_active LIMIT 1")
    if row is None:
        raise HTTPException(404, "沒有啟用中的路徑")
    wps = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action FROM waypoints WHERE mission_id = $1 ORDER BY seq",
        row["id"])
    return {"id": str(row["id"]), "name": row["name"], "waypoints": [dict(w) for w in wps]}


@router.get("/missions/{mission_id}/waypoints")
async def mission_waypoints(mission_id: str):
    wps = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action FROM waypoints WHERE mission_id = $1 ORDER BY seq",
        mission_id)
    if not wps:
        raise HTTPException(404, "無此路徑或無航點")
    return {"waypoints": [dict(w) for w in wps]}


@router.post("/missions")
async def save_mission(m: MissionIn):
    """存入任務庫並附上幾何預檢報告。**不因預檢失敗而拒存**——任務庫
    可放草稿；真正的擋門在 command 服務上傳到機那一步。"""
    wps = [w.model_dump() for w in m.waypoints]
    mid = await _store_mission(m.name, m.source, wps)
    return {"id": mid, "check": plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin)}


@router.post("/missions/from-vehicle")
async def import_mission_from_vehicle(name: str | None = None):
    """把機上目前的任務（QGC 上傳的）讀回並存進任務庫。唯讀 + 入庫。"""
    data = await current_mission()
    wps = data["waypoints"]
    if len(wps) < 2:
        raise HTTPException(404, "機上沒有可儲存的任務（航點少於 2）")
    stored = [{"seq": w["seq"], "lat": w["lat"], "lon": w["lon"], "alt": w["alt"],
               "action": {22: "takeoff", 21: "land"}.get(w["command"], "waypoint"),
               "command": w["command"], "frame": w.get("frame"),
               "p1": w.get("p1"), "p2": w.get("p2"), "p3": w.get("p3"), "p4": w.get("p4")}
              for w in wps]
    mid = await _store_mission(
        name or f"機上任務 {datetime.now().strftime('%m/%d %H:%M')}", "vehicle", stored)
    return {"id": mid, "waypoint_count": len(wps),
            "check": plan_check.check_waypoints(
                stored, settings.geofence_radius_m, settings.geofence_alt_m,
                settings.geofence_margin)}


@router.post("/missions/{mission_id}/activate")
async def activate_mission(mission_id: str, active: bool = True):
    async with db.pool.acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE missions SET is_active = false WHERE is_active")
            if active:
                r = await con.execute(
                    "UPDATE missions SET is_active = true WHERE id = $1", mission_id)
                if r.split()[-1] == "0":
                    raise HTTPException(404, "無此路徑")
    return {"ok": True}


@router.delete("/missions/{mission_id}")
async def delete_mission(mission_id: str):
    r = await db.pool.execute("DELETE FROM missions WHERE id = $1", mission_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此路徑")
    return {"ok": True}  # waypoints 由 FK CASCADE 一併刪除


# ── 群飛模擬（開發鷹架，僅 simulated 模式）─────────────────────────────────

@router.post("/swarm/start")
async def swarm_start(count: int = 3, mission_id: str | None = None):
    """啟動 N 台運動學僚機。mission_id 指定時多機飛同一條路徑（梯次起飛，
    航線關聯該任務——真實情境的多機同任務）；否則各飛自己的幾何。"""
    if settings.link_source == "modem":
        raise HTTPException(409, "群飛模擬僅在 simulated 模式可用")
    try:
        names = await swarm_sim.start(max(1, min(count, 3)), mission_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"drones": names, "mission_id": mission_id}


@router.post("/swarm/stop")
async def swarm_stop():
    await swarm_sim.stop()
    return {"ok": True}


@router.get("/swarm/status")
async def swarm_status():
    return swarm_sim.status()


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


@router.post("/link-metrics/live", status_code=204)
async def link_metrics_live(s: LinkSample):
    """即時通道：更新 live state 並跑鏈路狀態機。**不寫資料庫。**

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
    # mode="json" 讓 datetime 變成 ISO 字串。live.link 會被 WebSocket 廣播出去，
    # 放進 datetime 物件會讓 json.dumps 拋錯而整個廣播迴圈死掉。
    m = s.model_dump(mode="json", exclude_none=False)
    m.pop("drone_id", None)
    m["source"] = "modem"
    target.link = m
    target.mark_link_seen()
    await link_transition(target, m)
    return Response(status_code=204)


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
        m["source"] = "modem"
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
