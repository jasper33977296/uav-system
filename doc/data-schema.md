# Data Schema 設計

完整 DDL 見 `db/init/01_schema.sql`（TimescaleDB image 啟動時自動執行）。

## 設計思路

資料分四類：**靜態註冊、架次、時序量測、事件**。

```
drones ──┬── flight_sessions ──┬── telemetry     (hypertable, 1Hz)
         │                     ├── link_metrics  (hypertable, 1Hz) ← 研究核心
         │                     └── events
         └── missions ── waypoints
cells                （gNB 基地台，模擬訊號源）
interference_zones   （干擾區標注，研究場景設定）
```

## 各表要點

### `link_metrics` — 5G 鏈路品質（研究核心）

| 欄位群 | 欄位 | 說明 |
|---|---|---|
| RF 層 | `rsrp` `rsrq` `sinr` `cqi` | SINR 是干擾研究主指標 |
| Cell | `pci` `cell_id` `band` `nr_mode` | PCI 變化 = handover |
| 端到端 | `rtt_ms` `jitter_ms` `packet_loss_pct` `throughput_up/down_kbps` | RF 劣化如何反映到應用層 |
| 空間 | `lat` `lon` `alt_rel` | **刻意反正規化**：空間分析／熱度圖不必與 telemetry 做時間 join |
| 標注 | `in_interference_zone` `source` | source = simulated / modem，模擬與實測資料可共存、可過濾 |

### `telemetry` — 飛行遙測

MAVSDK telemetry API 一對一對映：`position()` → lat/lon/alt、`battery()` → 電量、
`gps_info()` → fix/衛星數、`flight_mode()`。保留 `raw JSONB` 放不常用的原始訊息，
需求變更不必一直 migrate。

### `flight_sessions` — 架次

armed→disarmed 自動切分。`summary JSONB` 在架次結束時計算：
max_alt、avg/min SINR、avg RTT、干擾區內取樣數／總取樣數。
「比較干擾區內外的鏈路品質」這類分析可以直接從 summary 起手。

### `drones`

`is_simulated` 與 `connection_url` 讓模擬機和真機走同一套程式路徑，只差設定。

### `cells` / `interference_zones`

研究場景設定表。模擬器每 30 秒重載，前端新增/刪除干擾區即時生效。
真機階段 `interference_zones` 轉為「已知干擾源」的 ground truth 標注，
`cells` 存實網量到的 cell 資訊。

### `events`

`link_degraded` / `link_lost` / `link_recovered` / `handover` / `mode_change` /
`low_battery`…，帶 `severity` 與 `acked_at`（操作員確認）。

## 取樣頻率策略

| 路徑 | 頻率 | 理由 |
|---|---|---|
| 前端即時顯示（WebSocket）| 5 Hz | 順暢的位置更新，不落地 |
| telemetry / link_metrics 入庫 | 1 Hz | 回放與分析足夠，資料量可控 |
| 長期彙總 | （未做）| 之後用 TimescaleDB continuous aggregate 產 1 分鐘級別 |

## 已知取捨

- `link_metrics` 反正規化位置欄位：多寫一份 lat/lon，換取分析查詢不用 join。
  1 Hz 的量級下空間成本可忽略。
- telemetry 與 link_metrics 分兩張表而非合一：兩者未來頻率可能不同
  （真機 modem 讀取可能只有 0.2–1 Hz），且 link 欄位在真機階段會擴充。
