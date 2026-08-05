# 開發進度

專案目前走到哪、下一步做什麼。**這份是現況快照**（改動時直接覆寫），
逐次的開發紀錄放在 [log/](log/)。

最後更新：2026-08-04

## Roadmap 狀態

對應 [README.md](../README.md) 的 roadmap：

| # | 階段 | 狀態 | 備註 |
|---|---|---|---|
| 1 | 資料流骨架：SITL → backend → DB + WS → 前端即時地圖 | ✅ 完成並實測 | 2026-08-03 首飛跑通全鏈；2026-08-04 前端 UI 已目視確認（headless Chromium 截圖，尾跡四段上色與事件流均正常） |
| 2 | 歷史回放頁（軌跡上色 + SINR/RTT 時序圖）| ✅ 完成並實測 | /replay/[id]：3D 懸浮絲帶＋雙圖時間軸＋scrubber |
| 3 | ~~任務規劃 UI~~ → **QGC 任務擷取與疊圖** | ✅ 完成並實測 | GET /api/mission/current 自機上唯讀讀回；灰色懸浮絲帶＋地面虛線疊圖 |
| 4 | ~~前端干擾區編輯~~ | ✂️ 已移除 | 場域物件不存在於系統認知（2026-08-04 釐清），干擾分布由實測軌跡揭露 |
| 5 | 真機：機上量測回傳、影像 WebRTC | 🔶 地面站側完成 | 機上 ROS node 待硬體（RB5）；上機首驗：AT+QENG 的 SINR 是否隨已知干擾下降 |

## 各模組現況

| 模組 | 完成度 | 說明 |
|---|---|---|
| `apps/backend/app/ingest.py` | 可用 | MAVSDK 七路訂閱、armed↔disarmed 切分架次、模組層共享 System |
| `apps/backend/app/link_events.py` | 可用 | 三態鏈路狀態機，模擬／真機共用 |
| `apps/backend/app/link_sim.py` | 可用 | 開發鷹架（不擬真）；換手邊際防抖 |
| `apps/backend/app/api.py` | 可用 | 場景/架次/事件 + 無人機生命週期 + 任務庫 + 機上量測 push + 任務讀回 |
| `apps/backend/app/main.py` | 可用 | armed gate＋雙模式分工（simulated 取樣／modem 只寫 telemetry）|
| `db/init/01_schema.sql` | 可用 | 兩個 hypertable + 任務庫 + 冪等去重索引；尚無 retention |
| 前端 `/` 即時監控 | 可用 | 3D 懸浮絲帶＋球體機體＋任務疊圖＋地面網格；失聯 UI 未做 |
| 前端 `/drones` | 可用 | 註冊／刪除／架次統計，點列進回放 |
| 前端 `/replay/[id]` | 可用 | 3D 絲帶＋方向箭頭＋雙圖時間軸＋球體游標 |
| 前端 `/missions` | 可用 | .plan 上傳／機上讀回／顯示於即時頁切換 |
| `apps/frontend/lib/geo.ts`、`components/droneLayer.ts` | 可用 | 3D 幾何與 three.js 球體層（多機結構就緒）|
| `scripts/` | 可用 | setup／dev／test-flight／fly-mission／fake-onboard-node |

## 環境現況

**已安裝並實測跑通。**

| 項目 | 狀態 |
|---|---|
| Docker Engine 29.7.1 + compose v5.3.1 | ✅ 已裝，daemon 運行中 |
| TimescaleDB 容器 | ✅ `:35432`，9 張表 + seed（2 gNB、1 干擾區）|
| PX4 SITL 容器 | ✅ MAVLink → 127.0.0.1:14540（需 [#005](../issues/005-sitl-mavlink-target-ip.md) 的修正）|
| `apps/backend/.venv` | ✅ Python 3.12.3、mavsdk 3.17.2（host 端腳本用；服務本體改跑 container）|
| backend / frontend containers | ✅ 掛原始碼 + reload，restart: unless-stopped，綁 0.0.0.0（區網可達）|
| `apps/frontend/node_modules` | ✅ 已安裝 |
| 前端 UI 目視確認 | ⬜ 尚未做（backend 資料已驗證正確）|

連接埠慣例：自家服務一律 30000 以上（DB 35432 / backend 38000 / frontend 33000），
MAVLink 的 14540/14550 例外（PX4 固定慣例）。細節見
[scripts/README.md](../scripts/README.md)。

注意：`docker` 群組變更需重新登入才生效，未登出時用 `sg docker -c '...'` 代替。

## 下一步

1. 修 [#001](../issues/001-link-lost-event-never-fires.md)（已實測確認，擋到示範場景），
   一併處理 [#006](../issues/006-battery-pct-x100.md)、[#007](../issues/007-heading-never-populated.md)、
   [#003](../issues/003-cell-id-not-persisted.md)——都是小改動且都有實測佐證。
2. 開前端目視確認一次（`./scripts/dev-frontend.sh` → :33000），
   看軌跡上色、干擾區圈、事件流是否如 `doc/frontend.md` 設計。
3. 決定 [#004](../issues/004-writes-while-disarmed.md) 的方向後再進 roadmap 2。

## 待決事項

| 項目 | 說明 |
|---|---|
| 待命時的入庫策略 | 見 [#004](../issues/004-writes-while-disarmed.md) |
| 底圖來源 | 現用 OSM raster；台灣場域是否換 NLSC WMTS 正射影像（見 `doc/frontend.md`）|
| 真機時程 | 影響 `ModemLinkSource` 與影像串流的優先序 |
