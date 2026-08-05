import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import db, ingest
from .api import router
from .config import settings
from .link_events import transition as link_transition
from .link_sim import SimulatedLinkSource
from .state import live
from .ws import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("main")


async def _link_and_db_loop() -> None:
    """每秒把 telemetry 入庫；模擬模式下另外負責取樣鏈路品質與發事件。

    **只在 armed 且已建立架次時入庫**——上鎖時飛機停在原地不動，那些資料是同一個
    座標重複上萬筆，沒有記錄的必要。見 issues/004。

    兩種 link_source 的分工（見 doc/onboard-telemetry.md）：

    - `simulated`：本迴圈取樣、更新 live state、發事件、寫 link_metrics。
    - `modem`：鏈路資料由機上 ROS node push 進來——即時通道
      （POST /api/link-metrics/live）更新 live state 並發事件，記錄通道
      （POST .../batch）負責寫 link_metrics。本迴圈只管 telemetry。
      telemetry 仍走這裡，因為它來自地面站收到的 MAVLink，不是機上送的。
    """
    simulated = settings.link_source == "simulated"
    source = None
    if simulated:
        source = SimulatedLinkSource(
            await db.fetch_cells(), await db.fetch_zones(enabled_only=True),
            handover_margin_db=settings.handover_margin_db)
    else:
        log.info("link_source=%s：鏈路資料改由機上 POST 進來，本迴圈只寫 telemetry",
                 settings.link_source)

    recording = False          # 上一輪是否處於記錄狀態，用來偵測架次開始
    tick = 0
    while True:
        await asyncio.sleep(1.0 / settings.db_write_hz)
        tick += 1
        if simulated and tick % 30 == 0:  # 每 30 秒重讀干擾區，前端新增/刪除立即生效
            source.zones = await db.fetch_zones(enabled_only=True)
            source.cells = await db.fetch_cells()
        if live.lat is None or live.lon is None:
            continue

        if simulated:
            live.link = source.sample(live.lat, live.lon, live.alt_rel)
            live.mark_link_seen()   # 前端待機時仍看得到鏈路品質

        # 同時檢查 session_id：armed 由 ingest 的另一個任務設定，剛解鎖的瞬間
        # 可能還沒建好 session，此時寫入會產生 session_id NULL 的孤兒資料。
        if not (live.armed and live.session_id):
            recording = False
            continue

        if not recording:      # 架次開始：狀態機重置，避免沿用上一趟的狀態
            live.link_state = "ok"
            recording = True

        if simulated:
            # 鏈路事件：link_degraded / link_lost / link_recovered
            # （不發 handover——無人機等價於一台 UE，換手由 modem 與網路側處理。）
            await link_transition(live, live.link)
            await db.insert_link(live)

        await db.insert_telemetry(live)


async def _broadcast_loop() -> None:
    """定時把 live state 推給前端。

    整個迴圈包 try/except 是必要的：這個 task 的參照被 lifespan 的 tasks list
    持有，asyncio 因此永遠不會 GC 它，「Task exception was never retrieved」
    也就永遠不會印出來——任何未捕捉的例外都會讓廣播無聲無息地停止。
    實際踩過：live.link 裡混進 datetime 物件導致 json.dumps 拋錯，
    前端與所有 WebSocket client 就此再也收不到資料，日誌卻乾乾淨淨。
    """
    while True:
        await asyncio.sleep(1.0 / settings.broadcast_hz)
        try:
            if manager.clients:
                # primary 旗標：多機廣播中標記「MAVLink 主機」，前端側欄鎖定它
                # （否則僚機的訊息先到會被誤認成主機）
                await manager.broadcast({"type": "telemetry", "primary": True,
                                         **live.telemetry_dict()})
        except Exception:
            log.exception("broadcast 失敗，略過這一輪")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    # 主機身分由系統端決定（無人機頁註冊/設為主機/改名），不再走環境變數
    primary = await db.get_primary_drone()
    if primary is None:
        primary = await db.create_default_primary(
            settings.link_source == "simulated", settings.mavlink_url)
        log.info("無主機設定，自動建立預設主機 uav-1（可在無人機頁改名）")
    live.drone_id, live.drone_name = primary["id"], primary["name"]
    recovered = await db.recover_orphan_sessions()
    if recovered:
        log.info("補結算 %d 條孤兒航線（上次執行期間中斷的飛行）", recovered)
    log.info("primary drone: %s (%s)", live.drone_name, live.drone_id)
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
    # link_source 讓前端決定要不要畫模擬專用圖層（干擾區、gNB）——
    # 真機模式下系統對干擾無先驗知識，畫出來就是撒謊。見 doc/architecture.md。
    return {"ok": True, "mavlink_connected": live.connected,
            "link_source": settings.link_source}
