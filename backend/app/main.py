import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import db, ingest
from .api import router
from .config import settings
from .link_sim import SimulatedLinkSource
from .state import live
from .ws import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("main")


async def _link_transition(state: str, m: dict) -> str:
    """鏈路狀態機：SINR 位準 → 離散的狀態轉換，回傳新狀態。

    SINR 是連續量、事件是離散點，直接比大小會在干擾區內每秒發一筆重複事件。
    這裡用 ok / degraded / lost 三態，只在真的跨級時發一次事件。

    每一級的回升都要多 sinr_hysteresis_db 才算數（lost→degraded 要 -2+3=1dB、
    degraded→ok 要 5+3=8dB），避免 SINR 在門檻附近抖動時來回發事件。

    設計取向是「忠實記錄事實」：每一次跨級都留紀錄（包含從 lost 回到 degraded
    這種中間轉換），detail 帶上 from 欄位，事件序列可完整還原鏈路狀態變化。
    不採用 time-to-trigger——那會讓事件時間戳晚於實際發生時刻，而「位置 ↔ 鏈路
    劣化」的時間對應正是本研究的重點。
    """
    sinr = m["sinr"]
    lost_th, deg_th = settings.sinr_lost_db, settings.sinr_degraded_db
    hyst = settings.sinr_hysteresis_db

    if sinr < lost_th and state != "lost":
        new, severity, type_ = "lost", "critical", "link_lost"
    elif lost_th + hyst <= sinr < deg_th and state == "lost":
        new, severity, type_ = "degraded", "warning", "link_degraded"
    elif sinr < deg_th and state == "ok":
        new, severity, type_ = "degraded", "warning", "link_degraded"
    elif sinr >= deg_th + hyst and state != "ok":
        new, severity, type_ = "ok", "info", "link_recovered"
    else:
        return state

    ev = await db.insert_event(
        live.drone_id, live.session_id, severity, type_,
        {"sinr": sinr, "in_zone": m["in_interference_zone"], "from": state},
    )
    await manager.broadcast({"type": "event", **ev})
    return new


async def _link_and_db_loop() -> None:
    """每秒：取樣 5G 鏈路品質 → 更新 live state →（armed 時）發鏈路事件並入庫。

    取樣一律執行，讓前端待機時也看得到即時鏈路品質；但**只在 armed 時入庫**。
    上鎖時飛機停在原地不動，那些資料是同一個座標重複上萬筆，沒有記錄的必要。
    見 issues/004。
    """
    source = SimulatedLinkSource(await db.fetch_cells(), await db.fetch_zones(enabled_only=True),
                                 handover_margin_db=settings.handover_margin_db)
    link_state = "ok"          # ok / degraded / lost
    recording = False          # 上一輪是否處於記錄狀態，用來偵測架次開始
    tick = 0
    while True:
        await asyncio.sleep(1.0 / settings.db_write_hz)
        tick += 1
        if tick % 30 == 0:  # 每 30 秒重讀干擾區設定，前端新增/刪除立即生效
            source.zones = await db.fetch_zones(enabled_only=True)
            source.cells = await db.fetch_cells()
        if live.lat is None or live.lon is None:
            continue

        m = source.sample(live.lat, live.lon, live.alt_rel)
        live.link = m          # live state 一律更新（前端待機時仍看得到鏈路品質）

        # 同時檢查 session_id：armed 由 ingest 的另一個任務設定，剛解鎖的瞬間
        # 可能還沒建好 session，此時寫入會產生 session_id NULL 的孤兒資料。
        if not (live.armed and live.session_id):
            recording = False
            continue

        if not recording:      # 架次開始：狀態機重置，避免沿用上一趟的狀態
            link_state = "ok"
            recording = True

        # 鏈路事件：link_degraded / link_lost / link_recovered
        # （不發 handover 事件——無人機等價於一台 UE，換手由 modem 與網路側處理，
        #   應用層不參與也不研究它。服務 cell 仍以 pci 欄位記錄在 link_metrics。）
        link_state = await _link_transition(link_state, m)

        await db.insert_telemetry(live)
        await db.insert_link(live)


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(1.0 / settings.broadcast_hz)
        if manager.clients:
            await manager.broadcast({"type": "telemetry", **live.telemetry_dict()})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    live.drone_id = await db.ensure_drone(settings.drone_name, settings.mavlink_url)
    log.info("drone registered: %s (%s)", settings.drone_name, live.drone_id)
    tasks = [
        asyncio.create_task(ingest.run(), name="mavlink-ingest"),
        asyncio.create_task(_link_and_db_loop(), name="link-db-loop"),
        asyncio.create_task(_broadcast_loop(), name="ws-broadcast"),
    ]
    yield
    for t in tasks:
        t.cancel()
    await db.pool.close()


app = FastAPI(title="UAV System API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev 環境；部署時改白名單
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # 目前不處理 client 訊息，僅維持連線
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mavlink_connected": live.connected}
