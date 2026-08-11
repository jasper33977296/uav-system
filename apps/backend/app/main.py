import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import db, mavlink_rx, msg_registry
from .api import router
from .config import settings
from .link_events import transition as link_transition
from .link_sim import SimulatedLinkSource
from .state import fleet, live
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
    # 每台機各自一份 link source：SimulatedLinkSource 內帶 handover 遲滯狀態
    # （_serving_pci），多機若共用一份，位置不同的機會互相污染服務 cell、噴假換手。
    # 場景（gNB/干擾區）是模擬器內建常數（link_sim.DEFAULT_*），不讀 DB。
    sources: dict[str, SimulatedLinkSource] = {}
    if not simulated:
        log.info("link_source=%s：鏈路資料改由機上 POST 進來，本迴圈只寫 telemetry",
                 settings.link_source)

    def _source_for(drone_id: str) -> SimulatedLinkSource:
        src = sources.get(drone_id)
        if src is None:
            src = sources[drone_id] = SimulatedLinkSource(
                handover_margin_db=settings.handover_margin_db)
        return src

    recording: dict[str, bool] = {}   # 各機上一輪是否記錄中，用來偵測架次開始
    while True:
        await asyncio.sleep(1.0 / settings.db_write_hz)
        if mavlink_rx.rx:
            mavlink_rx.rx.refresh_connected()     # 逾時未見訊息 → 失聯標記
        for st in list(fleet.values()):
            if st.lat is None or st.lon is None:
                continue

            if simulated:
                # 每台依自己的位置取樣——in_interference_zone、服務 cell、SINR 全是
                # 各機自算（多機干擾研究的核心：不同機在不同位置量到不同鏈路品質）。
                st.link = _source_for(st.drone_id).sample(st.lat, st.lon, st.alt_rel)
                st.mark_link_seen()   # 前端待機時仍看得到鏈路品質

            # 同時檢查 session_id：armed 由 rx worker 設定，剛解鎖的瞬間可能
            # 還沒建好 session，此時寫入會產生 session_id NULL 的孤兒資料。
            if not (st.armed and st.session_id):
                recording[st.drone_id] = False
                continue

            if not recording.get(st.drone_id):   # 架次開始：狀態機重置
                st.link_state = "ok"
                recording[st.drone_id] = True

            if simulated:
                # 鏈路事件：link_degraded / link_lost / link_recovered（各機自己的
                # 狀態機，st.link_state 是 per-drone；link_transition 帶 st 進去）
                await link_transition(st, st.link)
                await db.insert_link(st)

            await db.insert_telemetry(st)


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
                for st in list(fleet.values()):
                    await manager.broadcast({"type": "telemetry",
                                             "primary": st is live,
                                             **st.telemetry_dict()})
        except Exception:
            log.exception("broadcast 失敗，略過這一輪")


async def _msg_registry_loop() -> None:
    """014 Phase B：per-drone 泛型訊息登錄表廣播（msg_registry_hz，低於遙測 5Hz——
    量大、研究/除錯用，不必高頻）。序列化例外只吞這一輪，不拖垮其他廣播。"""
    while True:
        await asyncio.sleep(1.0 / settings.msg_registry_hz)
        try:
            if manager.clients:
                for st in list(fleet.values()):
                    if not st.connected:
                        continue
                    await manager.broadcast({"type": "msg_registry",
                                             "drone_id": st.drone_id,
                                             **msg_registry.snapshot(st)})
        except Exception:
            log.exception("msg_registry 廣播失敗，略過這一輪")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    await db.migrate()
    # 主機身分由系統端決定（無人機頁註冊/設為主機/改名），不再走環境變數
    primary = await db.get_primary_drone()
    if primary is None:
        primary = await db.create_default_primary(
            settings.link_source == "simulated", settings.mavlink_url)
        log.info("無主機設定，自動建立預設主機 uav-1（可在無人機頁改名）")
    live.drone_id, live.drone_name = primary["id"], primary["name"]
    fleet[live.drone_id] = live      # 主機進機隊註冊表（rx 依 sysid 對回同一物件）
    recovered = await db.recover_orphan_sessions()
    if recovered:
        log.info("補結算 %d 條孤兒航線（上次執行期間中斷的飛行）", recovered)
    log.info("primary drone: %s (%s)", live.drone_name, live.drone_id)
    # 路線 B（issues/011）：pymavlink 單迴圈＝原始層錄製＋解碼＋多機 demux，
    # mavsdk 退役、零副程序
    rx_task = await mavlink_rx.start()
    tasks = [
        rx_task,
        asyncio.create_task(_link_and_db_loop(), name="link-db-loop"),
        asyncio.create_task(_broadcast_loop(), name="ws-broadcast"),
        asyncio.create_task(_msg_registry_loop(), name="ws-msg-registry"),
    ]
    yield
    for t in tasks:
        t.cancel()
    if mavlink_rx.rx and mavlink_rx.rx.rec:
        mavlink_rx.rx.rec.close()
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
