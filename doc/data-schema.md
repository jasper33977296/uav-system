# Data Schema 設計

基準 DDL 見 `db/init/01_schema.sql`（只在**全新 volume** 執行）；之後的增量
變更在 `apps/backend/app/db.py` 的 `migrate()`（冪等，backend 啟動時跑）——
**看現行 schema 以兩者相加為準**。command 服務另建 `command_log`（防序也補
drones 欄位）。

## 設計思路

資料分五類：**靜態註冊、架次、時序量測、事件、群組/指令留痕**。

```
drones ──┬── flight_sessions ──┬── telemetry     (hypertable, 1Hz)
         │        │            ├── link_metrics  (hypertable, 1Hz) ← 研究核心
         │        │            └── events (source: system/vehicle)
         │        └── group_id ─┐
         └── missions ── waypoints │
mission_groups ── group_assignments ┘   (013 群組任務)
command_log                             (command 服務指令留痕)
```

（模擬場景表 `cells`／`interference_zones` 已於 2026-08-10 拆除，場景改為
`link_sim.py` 內建常數；完整 ERD 見 `doc/architecture.md`「資料模型」。）

## 各表要點

### `link_metrics` — 5G 鏈路品質（研究核心）

| 欄位群 | 欄位 | 說明 |
|---|---|---|
| RF 層 | `rsrp` `rsrq` `sinr` `cqi` | SINR 是干擾研究主指標 |
| Cell | `pci` `cell_id` `band` `nr_mode` | `pci` 是 Physical Cell ID（會重複使用）；`cell_id` 是 modem 回報的全域識別碼 NCI/CGI，模擬資料為 NULL。換手不在研究範圍（無人機 = 一台 UE，由 modem 與網路側處理），只記錄不發事件 |
| 干擾標注 | `in_interference_zone` | **模擬專用**：模擬器對照內建干擾場景（`link_sim.DEFAULT_ZONES`，它自己的輸入）標的。真機階段系統對干擾無先驗知識，此欄為 NULL；干擾的空間分布由實測 SINR 軌跡揭露，是產出不是輸入 |
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
後補欄位：
- `note TEXT`（比較頁 v3）：架次自訂備註，使用者標實驗條件（如「開干擾器那趟」）；
  `PATCH /api/sessions/{id}` 設定（空字串／null＝清除），`GET /api/sessions` 帶回。

### `drones`

`is_simulated` 與 `connection_url` 讓模擬機和真機走同一套程式路徑，只差設定
（單埠多機後 `connection_url` 語意作廢，見 issues/011）。後補欄位：
- `mav_sysid`（011）：MAVLink sysid ↔ 資料列身分對應，單埠多機 demux 的核心；
  自動註冊時寫入。
- `current_mission_id`（020）：command 上傳任務成功時設＝「這台現在要飛的
  任務」，create_session 據此綁架次（任務↔架次因果鏈）。
- `video_url`：即時影像串流位址（無則 UI 不顯示影像入口）。

### ~~`cells` / `interference_zones`~~（已拆除，2026-08-10）

模擬場景是模擬器的內部細節，不佔正式 schema——改為 `link_sim.py` 內建
常數。真機的 cell 資訊記錄在每筆 `link_metrics`（`pci`/`cell_id`/`band`），
已知干擾源由實測資料歸因，不需要預先標注表。

### `events`

`link_degraded` / `link_lost` / `link_recovered` / `mode_change` /
`low_battery` / `sysid_addr_change`…，帶 `severity` 與 `acked_at`（操作員確認）。

`source` 欄（014 Phase A）分兩流：`system`＝backend 推導的事件（預設，
舊資料已回填）；`vehicle`＝自駕儀自己吐的 log（STATUSTEXT，QGC
vehicle-messages 同源；分段重組成整句、15s 窗重複折疊帶 count 於
`detail`）。PX4 1.14 實測多走 Events 協定（EVENT 410）而非 STATUSTEXT，
解碼見 issue 014 Phase A.2。

### `mission_groups` / `group_assignments`（013 群組任務）

`mission_groups`：一次編隊任務（`mode`＝unified/separate、`base_mission_id`
＝unified 的展開來源、`params` 存 vsep 等、`status` 生命週期見
doc/group-missions-design.md §7.1）。`group_assignments`：每台一列——
`mission_id` 指向**地面展開後的具體 materialized 任務**（不是共用
base）、`layer_index` 高度分層、`phase`/`error`/`updated_at` 為 013-B
執行期即時態。`flight_sessions.group_id` 讓群組↔架次可追。

### `command_log`（command 服務）

指令留痕：`sysid`/`action`/`params`/`result`/`detail`，每筆指令（含被拒與
逾時）都入庫——020 的孤兒架次回填就是靠它。MCP 落地時將加主體欄
（操作員 vs agent 身分，issue 019）。

## 取樣頻率策略

| 路徑 | 頻率 | 理由 |
|---|---|---|
| 前端即時顯示（WebSocket）| 5 Hz | 順暢的位置更新，不落地 |
| telemetry / link_metrics 入庫 | 1 Hz，**僅 armed 時** | 回放與分析足夠，資料量可控 |
| 長期彙總 | （未做）| 之後用 TimescaleDB continuous aggregate 產 1 分鐘級別 |

**只在 armed 且已建立架次時入庫**（見 [issues/004](../issues/004-writes-while-disarmed.md)）。
上鎖狀態下飛機停在原地不動，那些資料是同一座標重複上萬筆，不構成有意義的
對照組——修正前累積的 11,007 筆待機資料裡只有 2 個不同緯度、1 個經度。
取樣本身照常執行，`live` state 持續更新，因此前端待機時仍看得到即時鏈路品質。

## 已知取捨

- `link_metrics` 反正規化位置欄位：多寫一份 lat/lon，換取分析查詢不用 join。
  1 Hz 的量級下空間成本可忽略。
- telemetry 與 link_metrics 分兩張表而非合一：兩者未來頻率可能不同
  （真機 modem 讀取可能只有 0.2–1 Hz），且 link 欄位在真機階段會擴充。

## 資料生命週期（2026-08-04 定案）

| 層 | 保留 | 說明 |
|---|---|---|
| 原始 1Hz（telemetry / link_metrics）| **30 天** | TimescaleDB retention policy 自動清除 |
| 1 分鐘彙總（`link_metrics_1m` / `telemetry_1m`）| 永久 | continuous aggregate，每 10 分鐘自動刷新 |
| 匯出檔 | 使用者自管 | `GET /api/sessions/{id}/export` 單一 JSON（lossless）|

**要長期保留原始資料就先匯出**。UI 流程：無人機頁每條航線的「匯出」下載
完整 JSON → 確認後「移除」從 DB 刪除。匯出格式含 session/telemetry/
link_metrics/events 四段，可離線分析或之後寫匯入工具還原。

## 航線 ↔ 任務（2026-08-04 新增；2026-08-11 issue 020 改版）

`flight_sessions.mission_id` 綁定序（020 定案）：**明示指定 >
`drones.current_mission_id`（command 上傳時設，「操作員宣告要飛這條」的
意圖點）> `missions.is_active` 後備**。回放頁據此疊出預計路徑做預計 vs
實際比對；手飛／未宣告為 NULL。舊孤兒架次的回填見
`scripts/backfill-session-mission.sql`（事實源＝command_log，冪等）。

用詞約定：UI 稱一次飛行紀錄為「**航線**」（資料表名維持 flight_sessions，
程式識別字不動，只有使用者可見文字用航線）。

閒置欄位：`missions.drone_id` 與 `missions.status` 無資料來源，
見 [issues/010](../issues/010-missions-idle-columns.md)。
