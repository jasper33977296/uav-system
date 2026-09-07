# Data Schema

**現行 schema ＝ `db/init/01_schema.sql`（僅全新 volume 執行）＋ `apps/backend/app/db.py`
的 `migrate()`（冪等，每次啟動跑）＋ command 服務 DDL（`apps/command/app/main.py`）。**
本文欄位與外鍵於 2026-08-12 由執行中資料庫匯出核對。

**改 schema 的規矩**：動 `migrate()` 或 command DDL 時，**同一批 commit 更新本文件**。

---

## 1. 總覽：12 張表 ＋ 2 個彙總視圖

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
| 12 | `captures` | 一份錄製檔的 metadata | **內容在磁碟，這裡記路徑**（014，兩層 tier） | ground 30 天／onboard 90 天 |
| — | `link_metrics_1m` | 1 分鐘桶 | continuous aggregate | 永久 |
| — | `telemetry_1m` | 1 分鐘桶 | continuous aggregate | 永久 |

---

## 2. 關聯

**`drones` 是事實來源那張 metadata 表**（2026-09-02 使用者裁定）：
**所有資料都靠 DB 存，其他人透過 UID 外鍵指回去查。**

```
drones ─┬─< flight_sessions ─┬─< telemetry        (CASCADE)
        │        │            ├─< link_metrics     (CASCADE)
        │        │            ├─< events           (CASCADE)
        │        │            └─< video_segments   (CASCADE)
        │        ├── mission_id ──> missions       (SET NULL)
        │        └── group_id ────> mission_groups (無 FK)
        ├── current_mission_id ────> missions      (SET NULL)
        ├─< telemetry / link_metrics / events      (CASCADE)
        ├─< blackouts                              (CASCADE)
        ├─< captures                               (CASCADE)
        ├─< video_segments                         (CASCADE)
        └─< command_log.drone_id                   (SET NULL)

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
| `telemetry.drone_id` | drones | **CASCADE**（09-02 補） |
| `link_metrics.drone_id` | drones | **CASCADE**（09-02 補） |
| `events.drone_id` | drones | **CASCADE**（09-02 補） |
| `blackouts.drone_id` | drones | CASCADE |
| `captures.drone_id` | drones | **CASCADE**（09-02 新增） |
| `command_log.drone_id` | drones | **SET NULL**（09-02 補） |

`flight_sessions.drone_id` 09-02 由 `NO ACTION` 改成 `CASCADE`：原本刪一台機會被
它擋下，而**擋下的方式是靜靜失敗**——用裸 SQL 收拾的腳本因此把測試機留在機隊裡
好幾個星期，還出現在即時頁上（見 §5.6）。

原本兩處為 NO ACTION，使「刪除被編隊引用過的路徑」直接 FK 違反（API 500，已實測
復現）。**023 已改為 SET NULL**：路徑刪得掉，而 assignment／架次那一列**留著**
（只是 mission_id 變 NULL）——對應定案「飛過的路徑可以刪、飛行紀錄永存」。
刪除後仍能說出飛的是哪條，靠 `flight_sessions.mission_name` 快照（§3.4）。

### 2.2 `drone_id` 全數掛上外鍵（2026-09-02 改）

原本 `telemetry`／`link_metrics`／`events` 的 `drone_id` **沒有外鍵**，理由是
「hypertable 不能加」與「避免寫入成本」。**兩個理由都不成立**：

* **TimescaleDB 2.29 實測，hypertable 指出去的外鍵是支援的**（不支援的是反過來
  指進 hypertable）。
* 寫入成本是每列一次 PK 查找，而本專案的寫入是 1 Hz／機——量級差得太遠。

而代價是真的：清理只能靠應用層記得，**漏一張就長孤兒，孤兒不會叫**。
2026-09-02 實測清出 **284 筆指向 22 台已不存在的機的事件**。

現在 `drone_id` 一律 `ON DELETE CASCADE`，`delete_drone` 只剩兩件事：
**刪之前先數**（外鍵清完就查不到了，而「刪掉多少」是那個端點唯一的回執），
以及**刪磁碟上的檔案**（外鍵清的是列，不是檔案）。

> `session_id` 仍然無外鍵，**這一格是刻意的**：架次可以被刪掉而時序資料留著
> （歸屬變成未知），與「飛過的路徑可以刪、飛行紀錄永存」同一條原則。

### 2.3 `captures`：大東西記路徑，不進資料庫

> **資料本身很大的時候，SQL 欄位記路徑，要內容再到那個路徑下去看。**
> （2026-09-02 使用者裁定）

`captures` 一列＝一份錄製檔的 metadata，`path` 指向磁碟上的內容。與
`video_segments` 完全同一個形狀——影片一開始就是這樣做的，錄製檔則是
**2026-09-02 才改過來**：在那之前它的 metadata 是寫在磁碟上的 `.meta` JSON，
清單靠 glob 目錄。那等於把事實來源放在檔案系統裡：查不了、關聯不了、
刪機時也不會連帶清。

| 欄 | 意義 |
|---|---|
| `tier` | `ground`＝地面站錄的「送到地面站的東西」／`onboard`＝機上錄的「飛控送出的東西」 |
| `path` | **內容在這裡。** `status='partial'` 時內容住在 `path + '.part'`（路徑是這一列的身分，不因傳到一半而變） |
| `status` | `partial`／`complete`／**`lost`**（機上未回傳即被滾動刪除的墓碑） |
| `covers_from/to` | 這份錄製涵蓋的時間，收尾驗 sha256 那一遍順手掃出來的 |

**兩層放同一張表而不是兩張**：兩者相差的正是 5G 斷線那一段，要能用一句 SQL
把兩層對起來（`/api/onboard-captures/coverage`）。畫面上仍分開列。

> **唯一鍵是 `(tier, drone_id, name) NULLS NOT DISTINCT`。** 地面站那一層沒有
> `drone_id`，而普通唯一約束裡 **NULL ≠ NULL**——`ON CONFLICT` 永遠不成立，
> 對帳一次就多一份重複列（實測 11 個檔案兩次對帳變 22 列）。

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

**電池三欄的分工**：`battery_voltage` 是量出來的，`battery_pct` 與
`battery_consumed_mah` 都是飛控**算**的——`pct = (BATT_CAPACITY − consumed)
/ BATT_CAPACITY`，而 consumed 是用 `BATT_AMP_PERVLT` 對電流積分得來。
2026-09-07 補上 `battery_current` 與 `battery_consumed_mah`：原本只存結論
（pct）不存推導過程，於是「那個百分比可不可信」在資料庫裡答不出來。
**`consumed_mah` 是從上電起算的累加器**，斷電歸零，只在同一段供電裡有意義。

> **⚠ 這張表只在 armed 且有架次時才寫**（`main.py` 的記錄迴圈）。
> 停在地面待機時**一筆都不會寫**——那段時間唯一完整的紀錄是 014 的原始層
> tlog（逐框架落盤，含 `BATTERY_STATUS`）。做地面耗電測試要看 tlog：
> `mavlogdump.py --types BATTERY_STATUS /data/mavcap/<日期>.tlog`

### 3.6 `link_metrics` — 5G 鏈路品質（hypertable，取樣率見下）**研究核心**

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

### `telemetry` 有兩個來源，而它們曾經互相矛盾（2026-09-07）

`telemetry.backfilled` 分辨這一列是誰寫的：`false`＝即時串流（直接來自飛控的
封包），`true`＝機上補傳（代理在斷線期間緩衝、恢復後補送）。

補傳端點一直有去重，但它比對的是 `round(t, 2)`——而兩條路的百分秒天生不同
（即時是「收到封包的時刻」、逐筆漂移；補傳是機上 1Hz 取樣的整齊刻度），
**永遠不會落在同一個百分秒，所以每一筆補傳都被當成新資料插進去**。
已改成 ±0.6 秒的時間窗、**即時優先**（`DEDUP_WINDOW_S`，commit d6dea0b）。

**修法不會回頭改已經寫進去的列。** 實測受影響：6 趟、127 筆補傳列落在即時
資料已覆蓋的秒數上，最遠位置差 32.8 m，其中多數連 `flight_mode` 都不同
（補傳說 LOITER、即時說 AUTO——飛機正在 3 m 空中的那 20 秒被插進「機在地上」
的樣本）。另有 335 筆補傳列落在真正的斷線缺口裡，**那些是唯一的紀錄，不動**。

處置分三層，**都不自動發生**：

| 層 | 東西 | 做什麼 |
|---|---|---|
| 量測 | `GET /api/sessions/{id}/telemetry-quality` | 用現行那把尺去量歷史：幾筆即時、幾筆補傳、幾筆撞在一起、位置差多遠 |
| 呈現 | 資訊頁 → 架次 → 第①段 | 有補傳就說一句；撞在一起就轉警告底。**判讀與匯出這一趟之前看得到** |
| 清理 | `scripts/clean-backfill-conflicts.py` | 預設乾跑；`--apply` 才刪，刪前先把要刪的列匯出成 JSON |

**地圖不受影響**：即時頁、回放頁、比較頁畫的軌跡都來自 `link_metrics`
（另一條路，不經過補傳）。實測那一趟的 link 座標與即時遙測差 0.1–2.9 m，
就是取樣落差——**研究主資料沒有被污染**。

**什麼時候有資料**：只有**離地期間**（2026-09-07 使用者裁定）。閘門在機上，
判準是 `landed_state`（退回高度＋地速），**不知道就記**——飛行中的量測補不
回來。停在地面上不入庫，即時畫面照樣看得到。見 `doc/onboard-telemetry.md`。

**取樣率**：由機上 `--modem-interval` 決定（預設 1.0s）。**這裡原本寫死
「1Hz」，而實際是 0.38 Hz**——2026-09-07 修掉兩個計時錯誤之後才真的是設定值
（見 `onboard-telemetry.md`）。取樣與即時傳送已分家：**取樣可以更密，
`/live` 仍是每秒一次**，所以這張表的密度不再被畫面的需求綁住。

### 3.7 `events` — 事件

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | bigserial PK | |
| `time` | timestamptz NOT NULL | |
| `drone_id` / `session_id` | uuid | 無 FK |
| `severity` | text NOT NULL | info／warning／critical |
| `type` | text NOT NULL | link_degraded／link_lost／link_recovered／mode_change／low_battery／sysid_addr_change／vehicle_event／**mission_progress／waypoint_reached／mission_state**… |
| `source` | text NOT NULL | `system`＝backend 推導；`vehicle`＝自駕儀自己吐的 log（STATUSTEXT／PX4 EVENT） |
| `detail` | jsonb | vehicle 事件帶 `{text,count}`／`{event_id,args,count}` |
| `acked_at` | timestamptz | 操作員確認 |

**任務進度三型別**（2026-09-06 補；原本任務執行過程在系統裡完全沒有紀錄）：

| type | 來源 | 回答的問題 |
|---|---|---|
| `mission_progress` | `MISSION_CURRENT.seq` 變化 | 現在飛向第幾項、什麼時候換的 |
| `waypoint_reached` | `MISSION_ITEM_REACHED` | **每一項幾點到的** |
| `mission_state` | `MISSION_CURRENT.mission_state` 變化 | 未開始／執行中／暫停 |

`seq` 存的是**機端的編號**，不換算成我方航點索引（ArduPilot 把 home 算成
seq 0，兩者差 1）——換算是驅動層的職責，在入庫就換會讓原始事實消失。

**只有變化才落盤**：實測 9/2 七趟真飛共 1060 則 `MISSION_CURRENT` → 36 筆
事件。每則都寫的話一趟會多出幾千筆一模一樣的列，把事件流淹掉＝等於沒記。

**舊韌體不送的欄位要記成 NULL 不是 0**：`total` 與 `mission_state` 是
MAVLink 擴充欄位（ArduPilot 4.5+ 才送），而 **pymavlink 對缺席的擴充欄位
填 0 不是 None**。照收就會把「韌體沒說」記成「共 0 項」，畫面上寫出
「共 0 項」——2026-09-07 用 ArduPilot 4.0.3 的 SITL 飛一趟五項任務時抓到。

**「飛完了」認不出來是靠單一欄位**：實測本機韌體從來不送
`mission_state=5 (complete)`，飛完的樣子是 `active → not_started` ＋最後一項
有 `waypoint_reached`；中途被切走則是 active 之後沒有那一則。三種事件缺一
就湊不出這個判斷——這是三種都要記的原因，不是為了完整而完整。

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
| `drone_id` | uuid FK→drones ON DELETE SET NULL | 寫入當下由 sysid 解出 |
| `session_id` | uuid FK→flight_sessions ON DELETE SET NULL | **哪一趟飛行**；當時沒有進行中的架次＝NULL |

**`drone_id`／`session_id` 在寫入當下解，不事後推**（2026-09-06）：只靠
sysid＋時間戳回推得假設「sysid 從那時到現在沒被重新配過號」，而 sysid 正是
會被重新配號的那個東西（[040](../issues/040-sysid-must-be-assigned.md)）。

**歷史 307 筆一律留空，不回填。** 空著代表「不知道」——那是實話；用今天的
sysid 猜出當時是哪一台，正是這兩個欄位要防的錯誤。

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
