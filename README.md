# UAV System — 5G 鏈路品質研究平台

無人機經 5G 通訊，研究飛入高干擾場域時對訊號品質的影響。
系統記錄「飛行遙測 × 5G 鏈路品質 × 空間位置」的時序資料，並即時視覺化。

## 專案結構

```
apps/backend/    FastAPI + MAVSDK：遙測接收、5G 鏈路模擬、WebSocket 廣播、API
apps/frontend/   Next.js + MapLibre + three.js：3D 即時監控、架次回放、機隊與路徑管理
db/init/    TimescaleDB schema（容器首次啟動自動執行）
doc/        系統設計文件（架構、schema、前端設計、QGC 整合、機上量測回傳）
missions/   QGroundControl .plan 航線檔（研究用固定航線）
scripts/    安裝與啟動腳本
issues/     已知問題與待決事項（一問題一檔，編號引用）
progress/   開發進度現況 + 逐次開發紀錄 log/
```

| 文件 | 內容 |
|---|---|
| [doc/architecture.md](doc/architecture.md) | 系統架構、關鍵決策（單機→多機、模擬→真機的擴充路徑）|
| [doc/data-schema.md](doc/data-schema.md) | 資料表設計與理由、取樣頻率策略 |
| [doc/frontend.md](doc/frontend.md) | 前端版面、軌跡上色規則、即時資料流 |
| [doc/qgc-integration.md](doc/qgc-integration.md) | QGroundControl 分工、連線拓撲、完整作業流程 |
| [doc/onboard-telemetry.md](doc/onboard-telemetry.md) | 真機階段：機上 ROS 如何把 5G 量測送回地面站 |
| [doc/deployment.md](doc/deployment.md) | **部署手冊**：地面站安裝、機上一次性設定、網路、驗收、維運 |
| [progress/README.md](progress/README.md) | **目前進度、環境現況、下一步** |
| [issues/README.md](issues/README.md) | 問題索引與狀態 |

## 快速啟動

```bash
./scripts/setup.sh          # 裝 Docker、backend venv、frontend 套件、挑選 port
docker compose up -d        # 全部服務：DB + SITL + backend + frontend
```

**開發也是這樣跑**（2026-08-04 起）：apps/backend/frontend 容器掛載原始碼並
熱重載，改檔案即生效；`restart: unless-stopped` 讓服務不依賴任何終端
session，機器重開自動復活。前端 `http://<主機IP>:33000`（區網可直連，
API 位址自動推導）。

host 端腳本（`scripts/dev-*.sh`）保留給需要單獨跑某個服務除錯時用。

測試飛行：

```bash
apps/backend/.venv/bin/python scripts/test-flight.py    # 直線穿越干擾區
apps/backend/.venv/bin/python scripts/fly-mission.py    # 以 QGC 身分上傳並執行 .plan 任務
apps/backend/.venv/bin/python scripts/fly-mission.py missions/complex-survey.plan  # 複雜航線
```

### 連接埠慣例

自家服務一律使用 **30000 以上**的 port，避開系統服務與同機其他專案：

| 服務 | Port |
|---|---|
| TimescaleDB | 35432 |
| Backend (FastAPI) | 38000 |
| Frontend (Next.js) | 33000 |
| MAVLink UDP | 14540 / 14550（PX4 與 QGroundControl 固定慣例，不改）|

實際使用的 port 由 `scripts/setup.sh` 偵測衝突後寫進根目錄 `.env`，
`docker-compose.yml` 與各 dev 腳本都讀它。

## 測試場景：飛進干擾區

Seed 資料在 PX4 SITL 預設起飛點（蘇黎世）旁放了兩個 gNB 和一個干擾區，
起飛往北約 200m 進入干擾區，可觀察 SINR 驟降、`link_lost` 事件與軌跡變色：

```bash
apps/backend/.venv/bin/python scripts/test-flight.py
```

腳本連 **14550** 而非 14540——backend 的 mavsdk_server 已經綁住 14540，
同一個 UDP 埠不能兩個程序同時用（見 [issues/008](issues/008-readme-test-script-port-conflict.md)）。
PX4 SITL 對兩個埠都會送 MAVLink，控制指令走哪個都可以。

（或用 QGroundControl 連 `udp:14550` 手動規劃任務——實務上校正與規劃都走 QGC，
分工與完整流程見 [doc/qgc-integration.md](doc/qgc-integration.md)。
現成航線在 [missions/](missions/)。）

## Roadmap

1. ✅ 資料流骨架：SITL → backend → DB + WebSocket → 前端即時地圖
2. 歷史回放頁（軌跡上色 + SINR/RTT 時序圖表）
3. 任務規劃（地圖畫航點 → MAVSDK 上傳 → SITL 執行）
4. 前端干擾區編輯
5. 真機：ModemLinkSource（AT/QMI + ping 實測）、影像 WebRTC
