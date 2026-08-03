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


async def _link_and_db_loop() -> None:
    """每秒：取樣 5G 鏈路品質 → 更新 live state → 發鏈路事件 → 入庫。"""
    source = SimulatedLinkSource(await db.fetch_cells(), await db.fetch_zones(enabled_only=True))
    prev_pci: int | None = None
    degraded = False
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
        live.link = m

        # 鏈路事件：handover / link_degraded / link_lost
        if prev_pci is not None and m["pci"] != prev_pci:
            ev = await db.insert_event(live.drone_id, live.session_id, "info", "handover",
                                       {"from_pci": prev_pci, "to_pci": m["pci"]})
            await manager.broadcast({"type": "event", **ev})
        prev_pci = m["pci"]

        if m["sinr"] < settings.sinr_lost_db and not degraded:
            degraded = True
            ev = await db.insert_event(live.drone_id, live.session_id, "critical", "link_lost",
                                       {"sinr": m["sinr"], "in_zone": m["in_interference_zone"]})
            await manager.broadcast({"type": "event", **ev})
        elif m["sinr"] < settings.sinr_degraded_db and not degraded:
            degraded = True
            ev = await db.insert_event(live.drone_id, live.session_id, "warning", "link_degraded",
                                       {"sinr": m["sinr"], "in_zone": m["in_interference_zone"]})
            await manager.broadcast({"type": "event", **ev})
        elif m["sinr"] >= settings.sinr_degraded_db + 3.0 and degraded:  # +3dB 遲滯避免抖動
            degraded = False
            ev = await db.insert_event(live.drone_id, live.session_id, "info", "link_recovered",
                                       {"sinr": m["sinr"]})
            await manager.broadcast({"type": "event", **ev})

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
