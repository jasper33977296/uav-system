from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from . import db
from .config import settings
from .link_events import transition as link_transition
from .state import live

router = APIRouter(prefix="/api")


@router.get("/drones")
async def list_drones():
    return [dict(r) for r in await db.pool.fetch("SELECT * FROM drones ORDER BY created_at")]


@router.get("/cells")
async def list_cells():
    return await db.fetch_cells()


@router.get("/zones")
async def list_zones():
    return await db.fetch_zones()


class ZoneIn(BaseModel):
    name: str
    center_lat: float
    center_lon: float
    radius_m: float
    severity_db: float
    note: str | None = None


@router.post("/zones")
async def create_zone(z: ZoneIn):
    row = await db.pool.fetchrow(
        """
        INSERT INTO interference_zones (name, center_lat, center_lon, radius_m, severity_db, note)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
        """,
        z.name, z.center_lat, z.center_lon, z.radius_m, z.severity_db, z.note,
    )
    return dict(row)


@router.delete("/zones/{zone_id}")
async def delete_zone(zone_id: int):
    await db.pool.execute("DELETE FROM interference_zones WHERE id = $1", zone_id)
    return {"ok": True}


@router.get("/sessions")
async def list_sessions(limit: int = 50):
    rows = await db.pool.fetch(
        """
        SELECT s.*, d.name AS drone_name FROM flight_sessions s
        JOIN drones d ON d.id = s.drone_id
        ORDER BY s.started_at DESC LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


@router.get("/sessions/{session_id}/track")
async def session_track(session_id: str):
    """回放用：一個架次的飛行軌跡 + 鏈路品質時序，前端據此畫上色軌跡與圖表。"""
    telemetry = await db.pool.fetch(
        "SELECT * FROM telemetry WHERE session_id = $1 ORDER BY time", session_id)
    link = await db.pool.fetch(
        "SELECT * FROM link_metrics WHERE session_id = $1 ORDER BY time", session_id)
    return {"telemetry": [dict(r) for r in telemetry],
            "link": [dict(r) for r in link]}


@router.get("/events")
async def list_events(limit: int = 100):
    rows = await db.pool.fetch("SELECT * FROM events ORDER BY time DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# ── 機上 5G 量測回傳（真機階段）────────────────────────────────────────────
# 設計見 doc/onboard-telemetry.md。兩條通道分工：
#   live  即時通道：只送最新一筆、失敗不重試、只更新 live state 不入庫
#   batch 記錄通道：送未確認的樣本、重試到成功、唯一的入庫路徑
# 分開的理由是鏈路差時小封包還擠得過去、大批次則否；共用一條通道會讓
# 「為完整性而重試」卡住即時性，操作員看到的是舊資料卻以為是現況。

class LinkSample(BaseModel):
    """機上一次採樣的完整結果。欄位對應 RM500Q-GL 的 AT+QENG 回應。"""
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
    # mode="json" 讓 datetime 變成 ISO 字串。live.link 會被 WebSocket 廣播出去，
    # 放進 datetime 物件會讓 json.dumps 拋錯而整個廣播迴圈死掉。
    m = s.model_dump(mode="json", exclude_none=False)
    m["source"] = "modem"
    live.link = m
    live.mark_link_seen()
    await link_transition(m)
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
