# Data Schema

**現行 schema ＝ `db/init/01_schema.sql`（僅全新 volume 執行）＋ `apps/backend/app/db.py`
的 `migrate()`（冪等，每次啟動跑）＋ command 服務 DDL（`apps/command/app/main.py`）。**
本文欄位與外鍵於 2026-08-12 由執行中資料庫匯出核對。

**改 schema 的規矩**：動 `migrate()` 或 command DDL 時，**同一批 commit 更新本文件**。

---

## 1. 總覽：11 張表 ＋ 2 個彙總視圖

| # | 表 | 一列代表 | 用途 | 保留 |
|---|---|---|---|---|
| 1 | `drones` | 一台無人機 | 機隊註冊（靜態） | 永久 |
| 2 | `missions` | 一條路徑快照 | 匯入／生成的具體航線（**非任務庫**，見 §5.1） | 永久 |
| 3 | `waypoints` | 一個航點 | 屬於某條路徑 | 隨 mission |
| 4 | `flight_sessions` | 一次飛行（armed→disarmed） | 架次，所有時序資料的歸屬 | 永久 |
| 5 | `telemetry` | 一筆遙測取樣 | 飛行狀態時序（**hypertable**，1Hz） | 30 天 |
| 6 | `link_metrics` | 一筆鏈路量測 | **研究核心**：5G 品質時序（**hypertable**，1Hz） | 30 天 |
| 7 | `events` | 一則事件 | 系統推導事件＋機上 log | 永久 |
| 8 | `mission_groups` | 一次編隊任務 | 群組任務（013） | 永久 |
| 9 | `group_assignments` | 編隊中的一台機 | 該台的具體路徑與執行態 | 隨 group |
| 10 | `video_segments` | 一段影片檔 | 飛行影像（022） | **7 天** |
| 11 | `command_log` | 一筆指令 | command 服務指令留痕（含被拒／逾時） | 永久 |
| — | `link_metrics_1m` | 1 分鐘桶 | continuous aggregate | 永久 |
| — | `telemetry_1m` | 1 分鐘桶 | continuous aggregate | 永久 |

---

## 2. 關聯

```
drones ─┬─< flight_sessions ─┬─< telemetry        (無 FK)
        │        │            ├─< link_metrics     (無 FK)
        │        │            ├─< events           (無 FK)
        │        │            └─< video_segments   (CASCADE)
        │        ├── mission_id ──> missions       (SET NULL)
        │        └── group_id ────> mission_groups (無 FK)
        ├── current_mission_id ────> missions      (SET NULL)
        └─< video_segments                         (CASCADE)

missions ─┬─< waypoints                            (CASCADE)
          ├─< group_assignments.mission_id         (SET NULL)
          └─< mission_groups.base_mission_id       (SET NULL)

mission_groups ─< group_assignments                (CASCADE)
```

### 2.1 外鍵與刪除行為（從 DB 匯出）

| 外鍵 | 指向 | ON DELETE |
|---|---|---|
| `waypoints.mission_id` | missions | CASCADE |
| `flight_sessions.mission_id` | missions | SET NULL |
| `flight_sessions.drone_id` | drones | NO ACTION |
| `drones.current_mission_id` | missions | SET NULL |
| `mission_groups.base_mission_id` | missions | SET NULL |
| `group_assignments.group_id` | mission_groups | CASCADE |
| `group_assignments.mission_id` | missions | SET NULL |
| `video_segments.drone_id` | drones | CASCADE |
| `video_segments.session_id` | flight_sessions | CASCADE |

原本兩處為 NO ACTION，使「刪除被編隊引用過的路徑」直接 FK 違反（API 500，已實測
復現）。**023 已改為 SET NULL**：路徑刪得掉，而 assignment／架次那一列**留著**
（只是 mission_id 變 NULL）——對應定案「飛過的路徑可以刪、飛行紀錄永存」。
刪除後仍能說出飛的是哪條，靠 `flight_sessions.mission_name` 快照（§3.4）。

### 2.2 沒有外鍵的關聯（重要）

`telemetry`／`link_metrics`／`events` 的 `drone_id`、`session_id` **無外鍵約束**
（hypertable 與高頻事件流刻意不加，避免寫入成本）。**後果：刪架次不會連帶刪掉
它的時序資料**，清理必須由應用層顯式執行。

---

## 3. 各表欄位

### 3.1 `drones` — 機隊註冊

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | uuid PK | |
| `name` | text NOT NULL | |
| `model` / `serial_no` | text | `serial_no` 唯一 |
| `is_simulated` | bool NOT NULL | 模擬機與真機走同一套程式路徑，只差設定 |
| `connection_url` | text | **語意作廢**（單埠多機後，見 issues/011） |
| `video_url` | text | 即時影像串流位址（WHEP／MJPEG／video src）；空＝UI 不顯示影像入口 |
| `status` | text NOT NULL | offline／idle／in_mission／maintenance |
| `is_primary` | bool NOT NULL | MAVLink 主機，至多一台（唯一索引強制） |
| `mav_sysid` | int | **MAVLink sysid ↔ 資料列身分**，單埠多機 demux 的核心（011） |
| `current_mission_id` | uuid → missions | 上傳成功時設＝「這台現在要飛的路徑」，架次據此綁定（020） |
| `created_at` | timestamptz NOT NULL | |

### 3.2 `missions` — 路徑快照

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | uuid PK | |
| `name` | text NOT NULL | |
| `kind` | text | **分類（023）**：`imported`（使用者匯入 .plan）／`from-vehicle`（機上讀回）／`generated`（系統產生，含編隊展開）。清單過濾與群組清理都讀這欄 |
| `created_by` | text | 原始來源字串（`plan-file`／`vehicle`／`group-gen`／`command-stage2`）。**保留為歷史事實**，但不再兼差當判別欄 |
| `is_active` | bool NOT NULL | 全域「啟用中的那一條」，至多一條（單機時代遺留，仍有消費者） |
| `created_at` | timestamptz NOT NULL | |

### 3.3 `waypoints` — 航點

| 欄位 | 型別 | 說明 |
|---|---|---|
| `mission_id` + `seq` | uuid, int | 複合主鍵 |
| `lat` / `lon` / `alt` | float / real | DO_* 設定類（無座標）以 0 表示 |
| `action` | text | takeoff／waypoint／hover／photo／land／rtl |
| `params` | jsonb | **MAVLink 保真度**：原始 `command`／`frame`／`p1–p4` 全存，上傳時原樣送出 |

### 3.4 `flight_sessions` — 架次

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | uuid PK | |
| `drone_id` | uuid NOT NULL → drones | |
| `started_at` / `ended_at` | timestamptz | armed→disarmed；`ended_at` NULL＝進行中 |
| `mission_id` | uuid → missions | 這趟飛的是哪條路徑（綁定序見 §5.2）；手飛為 NULL。路徑被刪＝SET NULL |
| `mission_name` | text | **路徑名稱快照（023）**：建立架次時複製當下的名字。路徑刪除後 `mission_id` 變 NULL，靠這欄仍能說「飛的是 X（路徑已刪除）」而非一片空白——對應「飛行紀錄要永遠存在」 |
| `group_id` | uuid | 屬於哪次編隊（013）；單飛為 NULL |
| `summary` | jsonb | 落地後計算：航程、最大高度、SINR 統計等 |
| `note` | text | 使用者自訂備註（標實驗條件，如「開干擾器那趟」） |
| `origin` | text | `research`／`test`／`unknown`（NULL 視為 unknown）——見 §5.3 |
| `video_mode` | text | `on`／`off`（本趟刻意不錄）／`no_source`（該機無影像來源）——見 §5.4 |

### 3.5 `telemetry` — 飛行遙測（hypertable，1Hz）

| 欄位群 | 欄位 |
|---|---|
| 時間／歸屬 | `time` timestamptz NOT NULL（分區鍵）、`drone_id` uuid NOT NULL、`session_id` uuid |
| 位置 | `lat` `lon` float、`alt_msl` `alt_rel` real |
| 運動 | `heading` `ground_speed` `vertical_speed` real |
| 電量 | `battery_pct` `battery_voltage` real |
| GPS | `gps_fix` `satellites` smallint |
| 狀態 | `flight_mode` text、`armed` bool |
| 原始 | `raw` jsonb（不常用訊息，需求變更不必一直 migrate） |

### 3.6 `link_metrics` — 5G 鏈路品質（hypertable，1Hz）**研究核心**

| 欄位群 | 欄位 | 說明 |
|---|---|---|
| 時間／歸屬 | `time`（分區鍵）`drone_id` `session_id` | |
| 空間 | `lat` `lon` `alt_rel` | **刻意反正規化**：空間分析不必與 telemetry 做時間 join |
| RF | `rsrp` `rsrq` `sinr` `cqi` | **SINR 是干擾研究主指標** |
| Cell | `pci` `cell_id` `band` `nr_mode` | `pci` 僅鄰區內唯一；`cell_id`＝全域 NCI/CGI（模擬為 NULL）；`nr_mode`＝SA／NSA／LTE |
| 端到端 | `rtt_ms` `jitter_ms` `packet_loss_pct` `throughput_up_kbps` `throughput_down_kbps` | RF 劣化如何反映到應用層 |
| 標注 | `in_interference_zone` bool | **模擬專用**；真機階段為 NULL（干擾分布是產出不是輸入） |
| 來源 | `source` text NOT NULL | `simulated`／`modem`，可共存可過濾 |
| 原始 | `raw` jsonb | modem 原始回應 |

唯一索引 `(drone_id, time)`：機上補傳是 at-least-once，靠它冪等去重。

### 3.7 `events` — 事件

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | bigserial PK | |
| `time` | timestamptz NOT NULL | |
| `drone_id` / `session_id` | uuid | 無 FK |
| `severity` | text NOT NULL | info／warning／critical |
| `type` | text NOT NULL | link_degraded／link_lost／link_recovered／mode_change／low_battery／sysid_addr_change／vehicle_event… |
| `source` | text NOT NULL | `system`＝backend 推導；`vehicle`＝自駕儀自己吐的 log（STATUSTEXT／PX4 EVENT） |
| `detail` | jsonb | vehicle 事件帶 `{text,count}`／`{event_id,args,count}` |
| `acked_at` | timestamptz | 操作員確認 |

### 3.8 `mission_groups` — 編隊任務（013）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | uuid PK | |
| `name` | text NOT NULL | |
| `mode` | text NOT NULL | `unified`（一條 base 展開成 N 條）／`separate`（各飛各的） |
| `base_mission_id` | uuid → missions | unified 的展開來源 |
| `params` | jsonb | `vsep_m`／`rtl_stagger_m` 等 |
| `status` | text NOT NULL | 生命週期見 [group-missions-design.md](group-missions-design.md) §7.1（預設 `draft`） |
| `created_at` | timestamptz NOT NULL | |

### 3.9 `group_assignments` — 編隊中的每台機

| 欄位 | 型別 | 說明 |
|---|---|---|
| `group_id` + `drone_id` | uuid | 複合主鍵 |
| `mission_id` | uuid → missions | **地面展開後的具體路徑**（不是共用 base） |
| `layer_index` | int NOT NULL | 高度分層序（× `vsep_m`） |
| `phase` | text NOT NULL | 執行期即時態，見 group-missions-design §7.1（預設 `idle`） |
| `error` | jsonb | 異常態 `{msg, hint, autopilot_notes}` |
| `updated_at` | timestamptz | 前端 1s 輪詢看新鮮度 |

### 3.10 `video_segments` — 飛行影像（022）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | uuid PK | |
| `drone_id` / `session_id` | uuid（FK, CASCADE） | `session_id` 由**時間區間比對**得出，不靠開錄／收錄事件配對；不在任何架次內錄的段為 NULL |
| `started_at` | timestamptz NOT NULL | **影片第 0 秒的絕對時間**＝回放 seek 的錨點。**逐段獨立、不假設段段相接** |
| `duration_s` | float | |
| `path` | text NOT NULL | 檔案路徑（**錨點事實源是本表不是檔名**） |
| `codec` / `width` / `height` / `fps` / `bytes` | | 相容性判斷用（非 H.264 瀏覽器播不了）。**Phase 1 為 NULL**——錄製器只給起訖，補這些要另外探測檔案，排 Phase 2/3 |
| `source` | text NOT NULL | `ground`（地面站從串流錄） |

唯一索引 `(drone_id, started_at)`。

### 3.11 `command_log` — 指令留痕（command 服務）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | bigserial PK | |
| `time` | timestamptz NOT NULL | |
| `sysid` | int | MAVLink sysid（**不是** drone_id） |
| `action` | text NOT NULL | arm／mode:hold／takeoff／mission_upload… |
| `params` | jsonb | |
| `result` | text NOT NULL | **含被拒與逾時**（失敗也留痕） |
| `detail` | text | 拒絕原因原文 |
| `client` | text | 誰下的：`frontend`／`acceptance-rig`／…（MCP 落地後加 agent 身分，019） |

---

## 4. 取樣頻率與生命週期

| 路徑 | 頻率／保留 | 說明 |
|---|---|---|
| WebSocket 即時顯示 | 5 Hz | 不落地 |
| `telemetry`／`link_metrics` 入庫 | 1 Hz，**僅 armed 時** | 上鎖時同座標重複萬筆無分析價值（issues/004） |
| 原始 1Hz 資料 | **30 天** | TimescaleDB retention policy 自動清除 |
| 1 分鐘彙總 | 永久 | continuous aggregate，每 10 分鐘刷新 |
| **飛行影像** | **7 天** | 與量測資料脫鉤：約 **1.17 GB/飛行小時**（720p15 端到端實流實測；0.83 GB 是離線編碼參考值，zerolatency 約多 40%）。見 [flight-video-design.md](flight-video-design.md) §6 |
| 匯出檔 | 使用者自管 | `GET /api/sessions/{id}/export`（lossless JSON） |

**要長期保留原始資料就先匯出**：無人機頁每條航線「匯出」下載完整 JSON
（含 session／telemetry／link_metrics／events 四段）→ 確認後「移除」從 DB 刪除。

---

## 5. 設計註記（為什麼這樣）

### 5.1 `missions` 是「路徑快照」不是「任務庫」

`.plan` 檔才是作者原稿（在 QGC／使用者檔案系統，本系統管不到、不保證還在）；
`missions` 那一列是**匯入當下的不可變快照**——全庫沒有任何改航點的路徑
（只有建立／啟用／刪除）。它存在只為三件事：給穩定 id 讓架次指向、讓同一條
路徑的多次飛行可比較、提供上傳與回讀比對的具體航點。
`status`／`drone_id`／`geometry` 是照「任務規劃工具」設計的欄位，但本系統刻意不做
規劃（規劃留 QGC）——三欄從建表至今從未被寫入或讀取，**已於 023 移除**（移除前以
資料驗證全為預設值／NULL，不是只信程式碼推論）。分類改用明確的 `kind` 欄，
`created_by` 保留為歷史事實。見 [issues/023](../issues/023-missions-table-role-cleanup.md)。

### 5.2 架次 ↔ 路徑的綁定序（020）

**明示指定 > `drones.current_mission_id`（上傳時設＝操作員宣告要飛這條）>
`missions.is_active`（後備）**。回放頁據此疊出「預計 vs 實際」。手飛為 NULL。
舊架次回填見 `scripts/backfill-session-mission.sql`（事實源＝command_log，冪等）。

### 5.3 `origin`：測試殘留治理

測試架次曾佔研究庫 97%。回填信號優先序：`command_log.client` 為測試類 >
假機架次 > 零樣本；**不明留 `unknown` 不強標**（不確定就說不確定）。
API 預設隱藏 `test`（`include_test=true` 顯示全部）。**標記不刪除**——
刪除由使用者審過分布後自行決定。回填見 `scripts/backfill-session-origin.sql`。

### 5.4 `video_mode`：零片段有三種意思

「**本趟刻意不錄**」（實驗設定）與「**錄了但鏈路斷光**」（實驗結果）對研究的
意義相反，事後無從推測，所以在架次建立時就記下。`on` 且零片段＝該錄卻整趟
沒收到流＝異常。NULL 視為 `off`（影像功能上線前的舊架次已回填）。

### 5.5 其他取捨

- `link_metrics` 反正規化位置欄位：多寫一份 lat/lon，換分析查詢不用 join。
- `telemetry` 與 `link_metrics` 不合併：頻率未來會不同（真機 modem 可能 0.2–1 Hz），
  且 link 欄位在真機階段會擴充。
- 模擬場景表（`cells`／`interference_zones`）已於 2026-08-10 拆除——模擬器的
  內部細節不佔正式 schema，改為 `link_sim.py` 內建常數。真機的 cell 資訊記在
  每筆 `link_metrics`，已知干擾源由實測資料歸因。

### 5.6 用詞

UI 稱一次飛行紀錄為「**航線**」；資料表名維持 `flight_sessions`，程式識別字不動。
