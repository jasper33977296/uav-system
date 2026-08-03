# 系統架構

## 研究目標

無人機經 **5G 通訊**，研究**飛入高干擾場域時對訊號品質的影響**。
系統的核心工作是記錄並視覺化「飛行遙測 × 5G 鏈路品質 × 空間位置」的關聯。

## 整體架構

```
PX4 SITL (Gazebo)          Backend (FastAPI)               Frontend (Next.js)
┌────────────┐  MAVLink  ┌─────────────────────┐  WS/REST  ┌──────────────┐
│  模擬飛控   │ ────────→ │ MAVSDK ingest        │ ────────→ │ 地圖監控 UI   │
│ udp:14540  │           │ 5G link source       │           │ SINR 上色軌跡 │
└────────────┘           │ WS broadcast (5Hz)   │           │ 5G 儀表板     │
                         │ DB writer (1Hz)      │           └──────────────┘
                         └────────┬────────────┘
                         PostgreSQL + TimescaleDB
```

## 核心設計原則

**即時資料流與業務資料分離。**

- Telemetry 流：無人機 → backend → WebSocket 直接推前端（5Hz），同時非同步入庫（1Hz）。
  前端顯示不等資料庫。
- 業務資料（任務、機隊、干擾區設定）走 REST + 關聯式資料表。

## 關鍵決策與理由

### 單機起步、保留多機擴充

- 所有資料表從第一天就帶 `drone_id`。
- MAVSDK ingest 目前是 FastAPI 內的 asyncio 背景任務（`backend/app/ingest.py`），
  **不架 MQTT broker**。
- 多機化路徑：把 ingest 抽成獨立 gateway process、中間插 MQTT、
  `app/state.py` 的單例 `LiveState` 換成 `dict[drone_id, LiveState]`。
  Schema 與前端不用動。

### 5G 鏈路來源可抽換（SITL ↔ 真機）

SITL 沒有真的 5G modem，因此鏈路品質來源抽象成同一個 `sample(lat, lon, alt) -> dict` 介面：

| 階段 | 實作 | 說明 |
|---|---|---|
| 模擬 | `SimulatedLinkSource`（`link_sim.py`）| gNB 距離 → path loss → RSRP → SINR，落在干擾區內額外扣 `severity_db`（區緣漸變）；RTT/丟包/吞吐由 SINR 推導 |
| 真機 | `ModemLinkSource`（未實作）| modem AT command / QMI 讀 RF 指標 + ping 實測 RTT/丟包 |

模擬模型刻意簡單——重點是讓「位置 → 訊號品質」的因果關係**可控、可重現**，
方便開發與驗證前端／分析流程，不是要精確模擬電波傳播。

### 架次（session）為資料組織單位

armed → disarmed 為一個 `flight_session`。所有 telemetry 與 link_metrics 掛在
session 下，「回放某次飛行」「比較兩次實驗」都是一個 `session_id` 條件。
架次結束時自動統計鏈路摘要（avg/min SINR、干擾區內取樣數等）寫入 `summary`。

### 鏈路事件門檻（`backend/app/config.py`）

| 事件 | 條件 | severity |
|---|---|---|
| `link_degraded` | SINR < 5 dB | warning |
| `link_lost` | SINR < -2 dB | critical |
| `link_recovered` | SINR ≥ 8 dB（+3dB 遲滯防抖）| info |
| `handover` | 服務 cell PCI 改變 | info |

### 影像串流（第一版不做）

前端已預留 16:9 視窗。之後接 **MediaMTX + WebRTC (WHEP)**，
前端只需把 placeholder 換成 `<video>`，版面不動。

## 部署形態

- 開發：`docker compose up`（TimescaleDB + PX4 SITL headless + backend），
  前端 `npm run dev`。
- SITL 與 backend 用 host network，避免 MAVLink UDP 過 NAT 的問題。
- 真機階段：無人機側 companion computer 經 5G 回傳 MAVLink
  （backend 的 `MAVLINK_URL` 換掉即可），`LINK_SOURCE=modem`。
