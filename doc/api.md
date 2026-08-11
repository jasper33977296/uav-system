# API 文件（以起飛程序為主軸）

本文把兩個服務的 HTTP/WS 端點，依**一次量測飛行的實際操作順序**排列：
前置檢查 → 任務入庫 → 上傳到機 → 起飛執行 → 空中監看 → 返航 → 落地後歸檔。
每一步都標明「呼叫什麼、成功判準是什麼、失敗怎麼讀」。

**外部系統要一次呼叫跑完起飛的，直接看 [§2 一鍵起飛](#2-一鍵起飛外部觸發)**——
下面 §1 那串是「一個動作一支 API」，給 UI 與人工排查用。

權威來源：`apps/backend/app/api.py`（ingest，唯讀）、
`apps/command/app/main.py`＋`mav.py`（指令）。設計理由見
[gcs-replacement.md](gcs-replacement.md)，這裡只講介面。

## 0. 服務、埠、職責分離

| 服務 | 埠 | MAVLink | 職責 |
|---|---|---|---|
| backend | `38000` | `udpin://0.0.0.0:14540`（**唯讀**） | 遙測入庫/廣播、任務庫、架次、事件、鏈路量測回收 |
| command | `38001` | `udpin://0.0.0.0:14541`（雙向，開發 SITL 用 14550） | 解鎖/模式/任務上傳/任務啟動、航線檔查詢、外部一鍵起飛，指令留痕 |
| frontend | `33000` | — | UI |

分離是刻意的：**backend 永遠不對 MAVLink 寫**。所有會動到飛機的動作只在
command 服務，並吃 `ENABLE_COMMANDS` gate（預設 `false`，端點回 403）。

定址：backend 用 `drone_id`（UUID，資料庫身分）；command 用 **`sysid`**
（MAVLink 身分，單埠多機 demux）。兩者的對應欄位是 `drones.mav_sysid`。

範例中的 `$B=http://localhost:38000`、`$C=http://localhost:38001`。

---

## 1. 起飛程序對照總表

| # | 步驟 | 端點 | 成功判準 | 失敗處置 |
|---|---|---|---|---|
| 1 | 指令能力就緒 | `GET $C/healthz` | `enabled=true` 且 `drones` 至少一個 sysid、`age_s` < 3 | `enabled=false` → `.env` 設 `ENABLE_COMMANDS=true`；`drones` 空 → 14541/14550 未通 |
| 2 | 遙測與機況就緒 | `GET $B/healthz`、`GET $B/api/live` | `mavlink_connected=true`；`gps_fix` ≥ 3、`battery_pct` 足夠、`armed=false` | 無 GPS 就上傳任務只是浪費一輪，先等定位 |
| 3 | 任務進任務庫 | `POST $B/api/missions`（或 `POST /api/missions/from-vehicle`） | 回 `id`；`check.ok` 為幾何預檢**參考**值 | 預檢不過**不擋存檔**，任務庫允許草稿 |
| 4 | 上傳到機 | `POST $C/api/command/{sysid}/mission/upload` | `verified=true` 且 `uploaded` = 航點數 | 502＝握手/回讀失敗（鏈路或機端）；409＝機端拒絕或預檢擋門 |
| 5 | 起飛執行 | `POST $C/api/command/{sysid}/mission/start` | `accepted=true`（`result=MAV_RESULT_ACCEPTED`） | 409 帶 `hint`＋`px4_notes`（PX4 的拒絕原因原文） |
| 5' | 分開解鎖（多機兩階段提交用） | `POST .../arm` → `POST .../mode/mission` → `POST .../mission/start` | 每步 `accepted=true` | 任一台 arm 失敗 → 全組 `disarm`，地面撤銷 |
| 6 | 空中監看 | `WS $B/ws/telemetry`（UI）／`GET $B/api/live`（腳本） | `armed=true`、`flight_mode` 進入 mission、`alt_rel` 上升 | 鏈路狀態看 `link_state` |
| 7 | 返航／中止 | `POST $C/api/command/{sysid}/mode/rtl｜hold｜land` | `accepted=true` | RC 永遠是最終手段（見底線） |
| 8 | 落地歸檔 | `GET $B/api/sessions?limit=1` → `/track`、`/export` | 架次 `ended_at` 有值、`summary` 有統計 | — |

參考實作：`scripts/test-flight.py`（步驟 3→8 全走一遍）、
`scripts/fly-mission.py`（`.plan` 匯入版）、共用客戶端 `scripts/gcs_client.py`。

**步驟 3–5 可以用一支 `POST /api/start` 取代**（見 §2）——外部觸發不必自己
串三支 API。前置檢查（1、2）與監看歸檔（6–8）仍是分開的端點。

底線不變：**RC 是唯一手動通道**，本 API 只做監督式控制（任務、模式切換）；
failsafe 邏輯留在 PX4，地面站只負責觸發與顯示。

---

## 2. 一鍵起飛（外部觸發）

給外部系統的端點，全部在 command 服務（`:38001`）。設計意圖：外部先用
**總表**知道有哪些航線可挑，需要細節時看**內容**，然後打 **`/api/start`**
一次跑完「取航線 → 預檢 → 上傳回讀 → 起飛執行」。

> **啟動流程（2026-08-11 併入現版時更新）**：`/api/start` **不再是「地面直接
> MISSION_START」**——那個真機會失敗（PX4 地面直接啟動任務踩過的雷）。現版
> 內部委派 `POST /api/command/{sysid}/mission/fly`＝**上傳回讀 → arm → NAV_TAKEOFF
> → 等實際到達高度 → 切 AUTO.MISSION**（真 SITL 驗過）。對外介面完全不變，
> 回應多帶 `steps`（upload/arm/takeoff/alt_reached/mission）＋`sysid`＋`source`。
> 起飛高度可用 `takeoff_alt`（預設 10 m）帶。能力 gating／逐台 audit／X-Client 歸因
> 全部自動繼承現版。

**航線來源以任務庫（DB）為主**（2026-08-11 決定）：總表與內容都讀
`missions`/`waypoints` 表，跟前端路徑管理頁看到的是同一份。`missions/` 目錄的
`.plan` 檔是**次要來源**，兩者不同步、內容也不一樣——`/api/start` 仍可直接吃
檔名（會先匯入任務庫再飛）。

> **沒有身分驗證**（2026-08-11 決定）。擋門只有 `ENABLE_COMMANDS` 與網路
> 隔離——**任何連得到 `:38001` 的人都能讓飛機起飛**。部署時務必確認這個埠
> 不對外曝露。

### 2a. 總表 — `GET /api/missions`

任務庫裡所有航線。唯讀，不吃 `ENABLE_COMMANDS` gate。

```json
{ "source": "db",
  "missions": [
    { "id": "d43d75e9-851d-43ce-aa46-7b77e17b8dc8", "name": "test_flight_plan1",
      "source": "plan-file", "created_at": "2026-08-10T08:07:49+00:00",
      "is_active": false, "waypoint_count": 4, "nav_count": 3 } ] }
```

| 欄位 | 說明 |
|---|---|
| `id` / `name` | 兩個都能直接餵給 `/api/start` 的 `mission` |
| `waypoint_count` / `nav_count` | 全部航點數／帶座標的（`DO_*` 這種無座標的不計） |
| `source` | 這條怎麼來的：`plan-file`（匯入）／`vehicle`（從機上讀回） |
| `is_active` | 前端即時頁顯示中的那一條（至多一條） |

**任務庫允許同名**——測試腳本重跑就會產生一堆同名任務。要精確指定就用 `id`。

### 2b. 內容 — `GET /api/missions/{id 或 name}`

單一航線的完整航點，含 `command`/`frame`/`p1`–`p4` 保真欄位，外加 `check`
幾何預檢報告——與 `/api/start` 上傳前跑的是同一份檢查，外部可以先看過再
決定要不要觸發。

用名稱查且有多筆同名時，取**最新建立**的那筆，並在 `same_name_count` 回報
共有幾筆；回應一律帶解析出來的 `id`，看得到自己拿到的是哪一筆。

### 2c. 次要來源：`.plan` 檔 — `GET /api/plans`、`GET /api/plans/{name}`

`missions/` 目錄下的 `.plan` 檔，**不在任務庫裡**。總表帶檔名、項目數、
`plannedHomePosition`、檔案大小與 mtime；解析失敗的檔一樣列出來（帶 `error`），
不讓壞檔在總表上消失。

單檔端點預設回解析後的航點＋預檢，`?raw=true` 回 QGC 原始 JSON。
副檔名可省略。檔名只認 `missions/` **這一層**：含 `/`、`..`、絕對路徑一律 404。

### 2d. 一鍵起飛 — `POST /api/start`

```
POST $C/api/start
{ "mission": "test_flight_plan1", "sysid": 1 }
```

| 欄位 | 必填 | 說明 |
|---|---|---|
| `mission` | 二選一 | 任務庫的 **id 或名稱**（主要來源） |
| `plan` | 二選一 | `missions/` 的 `.plan` 檔名（次要來源；會先匯入任務庫再飛） |
| `sysid` | — | 省略＝**唯一在線的那台**；線上有多台時省略會回 409（不猜——猜錯是讓錯的飛機起飛） |
| `store` | — | 只對 `plan` 來源有意義，預設 `true`。內容相同會**重用既有那筆**，反覆觸發不會把任務庫洗版 |

`mission` 與 `plan` **必須恰好給一個**，兩個都給或都不給回 422。

成功（HTTP 200）：

```json
{ "source": "db", "name": "test_flight_plan1",
  "mission_id": "d43d75e9-…", "sysid": 1,
  "waypoints": 4, "skipped": [],
  "uploaded": 4, "verified": true, "started": true,
  "result": "MAV_RESULT_ACCEPTED",
  "check": { "ok": true, "problems": [], "warnings": [], "max_dist_m": 21.3 },
  "px4_notes": [], "elapsed_s": 12.4 }
```

**同步回應**：上傳含逐項回讀比對，實測 10–40 秒（長航線更久）。
呼叫端的 timeout 請設 **60 秒以上**。

伺服器內部依序做的事，與失敗時的 `step` 值：

| `step` | 動作 | 失敗碼 |
|---|---|---|
| `mission` | 從任務庫取航線（id/名稱查無、或該任務沒有航點） | 404 |
| `plan` | 解析 `.plan`（檔名檢查、格式、導航航點至少 2 個） | 404 |
| — | 解析 sysid（未連線／心跳過期／多台未指定） | 404 / 409 / 503 |
| `precheck` | 幾何預檢。**預設不擋**，`GEOFENCE_ENFORCE=true` 才回 409 | 409 |
| — | 入庫（`plan` 來源且 `store=true` 時） | 500 |
| `upload` | `MISSION_COUNT` 握手 → 逐項送出 → **回讀比對** | 502 / 504 |
| `start` | `MAV_CMD_MISSION_START`（300） | 409 / 502 / 504 |

失敗回應（HTTP 4xx/5xx），`detail` 一定帶 `step`：

```json
{ "detail": { "step": "start",
              "msg": "機端拒絕（MAV_RESULT_TEMPORARILY_REJECTED）",
              "hint": "暫時拒絕——EKF/GPS 暖機中，稍等 30–60 秒再試",
              "px4_notes": ["Arming denied: Preflight checks failed"] } }
```

成功的定義沿用單步端點：`verified=true`（機上任務與送出內容一致）**且**
`started=true`（機端 ACK 為 ACCEPTED）。任一不成立都是非 2xx，不會回 200。

全程留痕：`command_log` 會有 `start`、`start:upload`、`start:mission_start`
三筆（失敗也留）。

---

## 3. 各步驟詳解

### 步驟 1 — 指令能力就緒

```
GET $C/healthz
```

```json
{ "ok": true, "enabled": true, "gcs_sysid": 254,
  "drones": { "1": { "age_s": 0.4, "armed": false, "custom_mode": 50593792 } } }
```

- `enabled`：`ENABLE_COMMANDS` 的值。**false 時所有指令端點回 403，且不發
  GCS 心跳**——不發心跳＝不進入 PX4 的 datalink-loss 安全鏈，純觀察不擔責。
- `drones`：心跳建檔的機。key 就是後續路徑上的 `{sysid}`。
  只有「自駕儀的心跳」會建檔，其他 GCS（QGC 255）的訊息不會混進來。
- `age_s` 持續變大＝心跳斷了，別下指令。

心跳一旦開始發，**服務存活就屬飛安相關**：`COM_DL_LOSS_T` 逾時未收到心跳，
PX4 會執行 `NAV_DCL_ACT`（設定為 RTL）。

### 步驟 2 — 遙測與機況就緒

```
GET $B/healthz        → {"ok":true,"mavlink_connected":true,"link_source":"simulated"}
GET $B/api/live       → 主機的即時狀態快照（與 WS 廣播同一份資料）
```

`/api/live` 欄位（`app/state.py::telemetry_dict`）：

| 欄位 | 說明 |
|---|---|
| `drone_id` / `drone_name` / `session_id` | 身分與當前架次 |
| `connected` | MAVLink 連線 |
| `lat` / `lon` / `alt_msl` / `alt_rel` | 位置；`alt_rel` 是相對起飛點 |
| `heading` / `roll` / `pitch` / `ground_speed` / `vertical_speed` | 姿態與速度 |
| `battery_pct` / `battery_voltage` | 電量 |
| `gps_fix` / `satellites` | 定位品質（起飛前看這個） |
| `flight_mode` / `armed` | 飛行模式與解鎖狀態 |
| `link` | 最新一筆 5G 量測（`sinr`/`rsrp`/`rtt_ms`/`cell_id`…） |
| `link_state` / `link_age_s` | 鏈路狀態機；`link_age_s=null` 表示從未收到 |

即時顯示請走 WebSocket，**輪詢頻率不要超過 `broadcast_hz`**。

### 步驟 3 — 任務進任務庫

```
POST $B/api/missions
{ "name": "test-flight", "source": "plan-file",
  "waypoints": [ {"seq":0,"lat":47.397742,"lon":8.545594,"alt":30,
                  "action":"takeoff","command":22,"frame":3}, … ] }
→ { "id": "…", "check": {"ok": true, "problems": [], "warnings": [],
                          "max_dist_m": 210.4, "fence_r": 500, "fence_alt": 100} }
```

航點欄位（2–500 筆）：`seq`、`lat`、`lon` 必填（`DO_*` 設定類以 0 表示），
`alt`、`action` 選填；**MAVLink 保真度欄位** `command`/`frame`/`p1`–`p4`
會原樣保留並在上傳時原樣送出。

- `frame=3`＝`GLOBAL_RELATIVE_ALT`（帶座標的導航項，QGC 預設）
- `frame=2`＝`MISSION`（RTL/`DO_*` 這類無座標指令；**給錯 frame，PX4 會回
  `MAV_MISSION_UNSUPPORTED`**）

`check` 是幾何預檢報告：第一個導航項是否為起飛、最後是否返航/降落、各點離
起飛點距離與高度是否超過 `GEOFENCE_*`。**存檔不因預檢失敗而拒絕**——任務庫
可以放草稿。

其他入庫路徑：

```
POST $B/api/missions/from-vehicle?name=…   # 把機上目前任務（QGC 上傳的）讀回入庫
POST $B/api/missions/{id}/activate?active=true   # 標記啟用（即時頁優先顯示，至多一條）
```

`.plan` 檔沒有專用端點：由客戶端解析成 waypoints 再 POST（做法見
`scripts/fly-mission.py::import_plan`，與前端解析邏輯一致）。

### 步驟 4 — 上傳到機

```
POST $C/api/command/{sysid}/mission/upload
{ "mission_id": "…" }
→ { "uploaded": 4, "verified": true, "px4_notes": [], "check": {…} }
```

伺服器端做的事，按順序：

1. 讀該任務的航點；沒有 → **404**。
2. 幾何預檢。報告一律附在回應的 `check` 與 `command_log` 留痕。
   **預設不擋**（`GEOFENCE_ENFORCE=false`，2026-08-10 決定）；
   設為 `true` 時預檢不過回 **409** `{"msg":"任務未通過幾何預檢，未上傳", …}`。
   空中真正的防線是 PX4 自己的 Geofence。
3. `MISSION_COUNT` → 逐項 `MISSION_ITEM_INT` 握手（MAVLink 2）。
   `COUNT` 每 2 秒重送直到機端開始請求，總期限 30 秒。
4. **回讀比對**：下載回來逐項比對 command 與座標（容差：經緯 2e-7 度、高度 0.5 m）。
5. 聽 3 秒 PX4 的 `STATUSTEXT`，有就放進 `px4_notes`。

**`verified=true` 才算上傳成功**——收到 ACK 不算，機上內容與送出內容一致才算。
比對不符、回讀逾時、30 秒未完成握手，一律 **502**，訊息可直接給操作員看。

### 步驟 5 — 起飛執行

```
POST $C/api/command/{sysid}/mission/start     # MAV_CMD_MISSION_START (300)
→ { "result": "MAV_RESULT_ACCEPTED", "accepted": true, "attempts": 1 }
```

PX4 在此會自動解鎖並起飛（`scripts/test-flight.py` 的驗收路徑）。
需要地面可撤銷的兩階段提交時（多機同時起飛），改用分解版：

```
POST $C/api/command/{sysid}/arm               # MAV_CMD_COMPONENT_ARM_DISARM (400) param1=1
POST $C/api/command/{sysid}/mode/mission      # DO_SET_MODE → AUTO.MISSION
POST $C/api/command/{sysid}/mission/start
POST $C/api/command/{sysid}/disarm            # 撤銷用（地面上才安全）
```

`mode` 只接受 `mission` / `hold` / `rtl` / `land`，其他回 **422**。

指令契約：`COMMAND_LONG` → 等 ACK（2 秒）→ 最多重送 3 次。
**沒有 ACK 一律視為失敗**（502），絕不當成功。

被拒時回 **409**，body 是結構化的三件套：

```json
{ "detail": { "msg": "機端拒絕（MAV_RESULT_TEMPORARILY_REJECTED）",
              "hint": "暫時拒絕——EKF/GPS 暖機中，稍等 30–60 秒再試",
              "px4_notes": ["Arming denied: Preflight checks failed"] } }
```

`px4_notes` 是拒絕前後幾秒 PX4 廣播的 `STATUSTEXT` 原文。實戰教訓：
只給 result code，操作員無從排查——那行 `Arming denied: …` 才是答案。
（PX4 1.14 多走 Events 協定，`px4_notes` 可能為空。）

### 步驟 6 — 空中監看

```
WS  $B/ws/telemetry
```

伺服器每 1/`broadcast_hz` 秒對每台機推一則：

```json
{ "type": "telemetry", "primary": true, "drone_id": "…", "armed": true,
  "flight_mode": "AUTO.MISSION", "alt_rel": 29.8, "link_state": "ok", … }
```

`primary=true` 標記 MAVLink 主機（僚機訊息先到時不會被誤認）。
用戶端送來的訊息目前不處理，連線保持即可。

腳本端沒必要開 WS 就輪詢 `GET $B/api/live`（見
`gcs_client.monitor_until_disarm`：每 2 秒印模式/高度/SINR/鏈路，直到上鎖）。

任務進度與機上目前任務：`GET $B/api/mission/current`（MAVLink 任務下載是唯讀
操作，不違反 backend 唯讀原則）。未連線回 503、下載失敗回 502。

### 步驟 7 — 返航／中止

| 動作 | 端點 |
|---|---|
| 返航 | `POST $C/api/command/{sysid}/mode/rtl` |
| 空中暫停（定點盤旋） | `POST $C/api/command/{sysid}/mode/hold` |
| 就地降落 | `POST $C/api/command/{sysid}/mode/land` |
| 地面上鎖 | `POST $C/api/command/{sysid}/disarm` |

多機空中失敗政策：單台異常＝該台自行 failsafe，**其他台繼續**；
要全體中止就對每台並行送 `mode/rtl`。

### 步驟 8 — 落地歸檔

```
GET    $B/api/sessions?limit=50&mission_id=…      # 架次列表（含 summary 統計）
GET    $B/api/sessions/{id}/track                 # 回放：軌跡＋鏈路時序＋關聯任務
GET    $B/api/sessions/{id}/export                # 單一 JSON 匯出（lossless，附下載檔名）
DELETE $B/api/sessions/{id}                       # 刪除架次與其時序資料（飛行中拒刪）
GET    $B/api/events?limit=100&session_id=…       # 事件流（link_degraded/lost/recovered…）
```

資料庫有 30 天 retention：要長期保留原始資料就先 `export` 再 `DELETE`
（UI 的「匯出並移除」流程）。指令留痕在 `command_log` 資料表，目前沒有讀取
端點，用 SQL 查。

---

## 4. 非起飛路徑的端點

### 機隊管理（backend）

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/drones` | 全機列表 |
| POST | `/api/drones` | 註冊一台（真機階段用；模擬機自動註冊）。名稱重複回 409 |
| PATCH | `/api/drones/{id}` | 改名／設定 `video_url`（空字串＝清除） |
| POST | `/api/drones/{id}/primary` | 指定 MAVLink 主機。**飛行中拒切**（409），切換立即生效不需重啟 |
| DELETE | `/api/drones/{id}` | 刪機與其全部架次/遙測/鏈路/事件。連線中的拒刪；關聯任務只解除關聯不陪葬 |

### 任務庫其餘端點（backend）

`GET /api/missions`、`GET /api/missions/active`、
`GET /api/missions/{id}/waypoints`、`DELETE /api/missions/{id}`（waypoints 由
FK CASCADE 一併刪）。

### 機上 5G 量測回傳（backend，兩條通道）

| 方法 | 路徑 | 語義 |
|---|---|---|
| POST | `/api/link-metrics/live` | 即時通道：只更新 live state 與鏈路狀態機，**不入庫**。回 204。送失敗不該重試 |
| POST | `/api/link-metrics/batch` | 記錄通道：**唯一入庫路徑**，冪等可重送。回 `{accepted_seq, stored, duplicate, outside_session}` |

兩者都只在 `LINK_SOURCE=modem` 開放（simulated 模式回 409，避免兩個寫入者
打架）；時間戳**必須含時區**，否則 422。細節見
[onboard-telemetry.md](onboard-telemetry.md)。

### 群飛模擬（backend，開發鷹架）

`POST /api/swarm/start?count=3&mission_id=…`、`POST /api/swarm/stop`、
`GET /api/swarm/status`。僅 `simulated` 模式可用，最多 3 台。

---

## 5. 錯誤碼對照

| 碼 | 服務 | 意義 | 處置 |
|---|---|---|---|
| 403 | command | `ENABLE_COMMANDS=false` | 刻意的安全 gate，部署時顯式開啟 |
| 404 | 兩者 | 任務/航線/無人機不存在，或任務沒有航點 | 檢查 id |
| 409 | command | 機端拒絕（body 有 `msg`/`hint`/`px4_notes`）或預檢擋門 | 讀 `px4_notes` |
| 409 | backend | 狀態衝突：飛行中切主機、刪連線中的機、名稱重複、模式不符 | 訊息即原因 |
| 422 | 兩者 | 參數不合法（未知 mode、時間戳無時區、沒有要更新的欄位） | 修正請求 |
| 502 | command | MAVLink 層失敗：無 ACK、上傳逾時、回讀比對不符 | 訊息可直接給操作員；查鏈路 |
| 502 | backend | 任務下載失敗 | 同上 |
| 503 | 兩者 | 沒有任何機在線／MAVLink 未連線／系統未初始化 | 等連線 |
| 504 | command | router 執行緒未在期限內回覆（指令 30 秒、上傳 180 秒） | 機端或鏈路無回應 |
| 500 | command | 內部錯誤 | 看服務日誌 |

`/api/start` 的錯誤 `detail` 一律是物件並帶 `step`（見 §2c）；其餘端點的
`detail` 多為字串，只有機端拒絕那種是帶 `msg`/`hint`/`px4_notes` 的物件。

**所有指令結果（成功與失敗）都寫入 `command_log`**：誰、何時、參數、ACK
結果、細節。指令史是實驗記錄的一部分，失敗一樣留痕。

---

## 6. 最小可跑的一次起飛（curl 版）

```bash
B=http://localhost:38000; C=http://localhost:38001
curl -s $C/healthz | jq '{enabled, drones}'          # 1. gate 開了嗎、看到機了嗎
curl -s $B/api/live | jq '{gps_fix, battery_pct, armed}'   # 2. 機況
MID=$(curl -s -X POST $B/api/missions -H 'Content-Type: application/json' \
      -d @mission.json | jq -r .id)                  # 3. 入庫
curl -s -X POST $C/api/command/1/mission/upload \
     -H 'Content-Type: application/json' -d "{\"mission_id\":\"$MID\"}" \
     | jq '{uploaded, verified}'                     # 4. 上傳＋回讀比對
curl -s -X POST $C/api/command/1/mission/start       # 5. 起飛
watch -n2 "curl -s $B/api/live | jq '{flight_mode, alt_rel, armed, link_state}'"   # 6. 監看
curl -s -X POST $C/api/command/1/mode/rtl            # 7. 返航
curl -s "$B/api/sessions?limit=1" | jq '.[0].summary'  # 8. 歸檔
```

腳本版直接跑 `python3 scripts/test-flight.py`（純標準庫，不需 venv）。

**外部觸發版**——步驟 3–5 縮成一支：

```bash
curl -s $C/api/missions | jq '.missions[] | {id, name, waypoint_count}'  # 有哪些航線
curl -s $C/api/missions/test_flight_plan1 | jq .check                    # 先看預檢
curl -s --max-time 90 -X POST $C/api/start \
     -H 'Content-Type: application/json' \
     -d '{"mission":"test_flight_plan1"}' \
     | jq '{source, name, uploaded, verified, started, elapsed_s}'
```

改吃 `missions/` 目錄的 `.plan` 檔就把最後一行換成
`-d '{"plan":"interference-survey.plan"}'`，清單改看 `$C/api/plans`。
