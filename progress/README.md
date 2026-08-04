# 開發進度

專案目前走到哪、下一步做什麼。**這份是現況快照**（改動時直接覆寫），
逐次的開發紀錄放在 [log/](log/)。

最後更新：2026-08-04

## Roadmap 狀態

對應 [README.md](../README.md) 的 roadmap：

| # | 階段 | 狀態 | 備註 |
|---|---|---|---|
| 1 | 資料流骨架：SITL → backend → DB + WS → 前端即時地圖 | ✅ 完成並實測 | 2026-08-03 首飛跑通全鏈；前端 UI 尚未目視確認 |
| 2 | 歷史回放頁（軌跡上色 + SINR/RTT 時序圖）| ⬜ 未開始 | 後端 `GET /api/sessions/{id}/track` 已就緒 |
| 3 | ~~任務規劃 UI~~ → **QGC 任務擷取與疊圖** | ⬜ 未開始 | 已改寫，見 [doc/qgc-integration.md](../doc/qgc-integration.md)；整合機制已實測驗證 |
| 4 | 前端干擾區編輯 | ⬜ 未開始 | `POST/DELETE /api/zones` 已就緒，缺前端畫圈 UI |
| 5 | 真機：機上量測回傳、影像 WebRTC | 🔶 地面站側完成 | push 端點＋補傳＋失聯偵測已實測；機上 ROS node 待硬體（RB5）到位，見 [doc/onboard-telemetry.md](../doc/onboard-telemetry.md) |

## 各模組現況

| 模組 | 完成度 | 說明 |
|---|---|---|
| `backend/app/ingest.py` | 可用 | MAVSDK 六路訂閱、armed↔disarmed 切分 session |
| `backend/app/link_sim.py` | 可用 | 開發鷹架（不擬真）；換手邊際防抖，見 [#002](../issues/002-handover-event-flapping.md) |
| `backend/app/main.py` | 可用 | armed gate＋雙模式分工（simulated 取樣／modem 只寫 telemetry）|
| `backend/app/link_events.py` | 可用 | 三態鏈路狀態機，模擬與真機兩路共用 |
| `backend/app/api.py` | 部分 | 另有機上量測 push 端點（live/batch，冪等＋架次反查）；missions 未做 |
| `db/init/01_schema.sql` | 可用 | 兩個 hypertable + seed 場景，尚無 retention / continuous aggregate |
| `frontend` 即時監控頁 | 可用 | 地圖 + 側欄；`/missions`、`/flights/[id]` 未建 |

## 環境現況

**已安裝並實測跑通。**

| 項目 | 狀態 |
|---|---|
| Docker Engine 29.7.1 + compose v5.3.1 | ✅ 已裝，daemon 運行中 |
| TimescaleDB 容器 | ✅ `:35432`，9 張表 + seed（2 gNB、1 干擾區）|
| PX4 SITL 容器 | ✅ MAVLink → 127.0.0.1:14540（需 [#005](../issues/005-sitl-mavlink-target-ip.md) 的修正）|
| `backend/.venv` | ✅ Python 3.12.3、mavsdk 3.17.2 |
| `frontend/node_modules` | ✅ 已安裝 |
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
