# QGroundControl 整合設計

實務上校正與任務規劃都用 QGroundControl（以下簡稱 QGC）。本文界定
QGC 與本系統的分工、連線拓撲，以及從校正到分析的完整作業流程。

## 分工原則

**QGC 是包在本系統下的控制元件，不是外部的對等工具。** 本系統是地面站上的
無人機管理平台，QGC 在其中擔任「操作飛機」的角色；本系統負責「控制資料蒐集
與記錄事實」，對飛機**只觀測、不介入**。

> **硬規則：backend 對 MAVLink 只讀不寫。** 資料蒐集端不該有能力下達飛行指令。
> 因此任務內容是「輪詢下載」而非「上傳同步」，任務啟動也由 QGC 執行而非本系統。
> 這同時是安全考量——真機階段一個誤送的指令可能造成事故。

QGC 是成熟的地面站，感測器校正、參數調整、航線規劃、飛行監控都做得比我們好，
而且是操作員已經熟悉的工具。本系統的價值不在重做這些，而在 QGC 不做的事：
**把 5G 鏈路品質與空間位置關聯起來、長期儲存、事後分析**。

### 為什麼不把 QGC 的 UI 嵌進本系統前端（2026-08-04 討論定案）

技術上不可行：QGC 是 Qt/QML 原生桌面應用，沒有瀏覽器版本；
Qt WebAssembly 路線缺序列埠／UDP／硬體視訊解碼，社群無可用移植；
螢幕擷取串流嵌入則延遲高、操作精度差——控制介面不能隔著串流操作。

即使可行也不該做：嵌入控制介面等於在資料蒐集 UI 裡開一個飛行控制入口，
破壞「只讀不寫」的安全邊界。**按錯地方後果不同的東西，不該長在同一個介面裡。**

「包在系統下」的體驗改由三層達成，皆不碰安全邊界：

1. **視窗層**：啟動腳本一鍵拉起兩者並排定位（Ubuntu 用 `wmctrl`）
2. **資料層**：QGC 的 `.plan` 航線疊到本系統地圖；QGC 下指令產生的狀態變化
   （模式、armed、任務進度）從同一條 MAVLink 讀回顯示——只讀鏡像
3. **語意層**：本系統 UI 明示「控制權在 QGC」，避免操作員在錯的視窗找功能

| 工作 | 負責 | 理由 |
|---|---|---|
| 感測器校正（加速度計/磁力計/水平/遙控器）| QGC | 需要引導式互動 UI，且與研究無關 |
| 參數調整、韌體更新 | QGC | 同上 |
| 航線規劃、geofence、rally point | QGC | 成熟且操作員熟悉；`.plan` 是通用格式 |
| 任務上傳與啟動 | QGC | 操作員在現場的主控台 |
| 飛行安全監控 | QGC | 已有完整告警與 HUD |
| **5G 鏈路品質取樣與入庫** | **本系統** | QGC 完全不做這件事 |
| **干擾區設定與空間標注** | **本系統** | 研究場景定義 |
| **架次記錄、鏈路事件、摘要統計** | **本系統** | 研究資料 |
| **歷史回放與分析（軌跡 × SINR）** | **本系統** | 研究產出 |
| 任務內容的擷取與存檔 | 本系統（被動） | 分析時需要知道「計畫航線 vs 實際軌跡」|

### 對 roadmap 3 的影響

原 roadmap 第 3 項是「任務規劃（地圖畫航點 → MAVSDK 上傳 → SITL 執行）」。
確定用 QGC 之後，**這一項應該改寫**：自己做規劃 UI 是在重造一個成熟工具的輪子，
且會產生「兩個地方都能規劃、以誰為準」的混亂。

改為：**任務擷取與疊圖**——backend 被動抓取 QGC 上傳到飛控的任務，存進
`missions`/`waypoints`，前端把計畫航線疊在實際軌跡上。這對研究更有用
（可以看「偏離航線的地方是不是鏈路較差」），工作量也小得多。

## 連線拓撲

MAVLink 的核心限制：**一個 UDP 埠只能被一個程序綁定**。QGC 與 backend 必須
各自有一條 link，否則會搶埠。

### SITL（開發環境，目前狀態）

PX4 SITL 天生就開兩個 MAVLink instance，剛好一人一條：

```
                    ┌─────────────────────────────┐
  PX4 SITL          │  :14550 (Normal 模式)  ──────┼──→ QGroundControl
  (Gazebo)          │  :14540 (Onboard 模式) ──────┼──→ backend (MAVSDK)
                    └─────────────────────────────┘
```

不需要任何額外設定，QGC 直接開就會連上 14550。
（前提是 `docker-compose.yml` 的 `command: ["127.0.0.1", "127.0.0.1"]`，見
[issues/005](../issues/005-sitl-mavlink-target-ip.md)。）

### 真機經 5G（部署形態）

真機只有一條實體鏈路（companion computer 經 5G 回傳），**必須用 MAVLink router
把單一串流複製給多個消費者**：

```
  Pixhawk ──UART──→ Companion Computer ──5G──→ ┌─ QGroundControl (操作員)
                    (mavlink-router)            └─ backend (本系統)
```

`mavlink-router` 設定範例（放在 companion computer）：

```ini
[UartEndpoint fc]
Device = /dev/ttyTHS1
Baud = 921600

[UdpEndpoint backend]
Mode = Normal
Address = <backend 主機 IP>
Port = 34540

[UdpEndpoint qgc]
Mode = Normal
Address = <操作員電腦 IP>
Port = 14550
```

備選方案：PX4 本身支援三個 MAVLink instance（`MAV_0/1/2_CONFIG`），
若 companion computer 與操作員各走不同實體埠（例如 TELEM1 給電台、TELEM2 給
companion），可以不架 router。但 5G 情境下兩者共用同一條網路鏈路，router 較合適。

> **注意**：這一段是依文件設計、**尚未實測**。SITL 的部分才是下面實測驗證過的。
> 真機階段要先驗證 router 的延遲與封包遺失對 1Hz 取樣的影響。

## 實測驗證（2026-08-03，SITL）

整合可行性的關鍵問題已用雙連線實測回答：

| 問題 | 結果 | 說明 |
|---|---|---|
| backend 能否解析 QGC 的 `.plan` 檔？ | ✅ | `MissionRaw.import_qgroundcontrol_mission()`（MAVSDK 3.17.2）正確解析 5 個 item |
| QGC 上傳的任務，backend 從另一條 link 能否取得？ | ✅ | `download_mission()` 拿到完整航線，座標無誤 |
| `mission_changed()` 會通知旁觀的 link 嗎？ | ❌ | 15 秒內未觸發，**不能當偵測訊號** |
| 輪詢下載的成本？ | ✅ 8–13 ms | 空任務 8ms、5 item 13ms，可安心每 5–10 秒輪詢 |
| 任務執行中能否即時追蹤進度？ | ✅ | backend 在自己的 link 上收到 `mission_progress` 0/5 → 4/5 |

實測時 QGC 角色在 14550、backend 角色在 14540，互不干擾。

**設計結論**：任務變更用**輪詢 + 內容指紋比對**偵測（不是事件驅動），
執行進度則可以直接訂閱 `mission_progress()`。

## 完整作業流程

### 階段 0 · 場域設定（本系統，飛行前）

在本系統前端標注干擾區、確認 gNB 位置。這是研究場景的定義，與 QGC 無關。

### 階段 1 · 校正（QGC）

操作員用 QGC 完成感測器校正、參數檢查。

**backend 該做什麼：什麼都不做，但要正確地不做。** 校正期間飛機是 disarmed，
依現行邏輯不會建立 `flight_session`——這是對的。但目前 backend 仍會 1Hz 寫入
`telemetry`/`link_metrics`（`session_id` 為 NULL），校正可能持續數十分鐘，
會累積大量無意義資料。這正是 [issues/004](../issues/004-writes-while-disarmed.md)
要決定的事，**QGC 的加入讓這題更急迫**（校正時間遠長於待機時間）。

註：加速度計校正後飛控需要重開機，MAVLink 會斷線重連。backend 的 MAVSDK
會自動重連，但 `live` state 的殘留值要確認不會污染資料。

### 階段 2 · 任務規劃（QGC）→ 擷取（本系統）

1. 操作員在 QGC 畫航線，存成 `.plan`，上傳到飛控。
2. backend 的 mission watcher 每 5–10 秒 `download_mission()`，
   對 item 內容算指紋（`sha256(seq,command,frame,x,y,z)`）。
3. 指紋改變 → 寫入 `missions` + `waypoints`，發 `mission_uploaded` 事件，
   前端地圖立刻疊上計畫航線。

`.plan` 檔本身也建議存檔（`missions.plan_json`），因為它含有 backend 下載不到的
資訊：`plannedHomePosition`、`cruiseSpeed`/`hoverSpeed`、複雜航線（Survey /
CorridorScan）的原始參數。飛控端只看得到展開後的 waypoint 序列。

### 階段 3 · 執行（QGC 啟動 → 本系統記錄）

1. 操作員在 QGC 按下開始，飛機 arm → **backend 現有邏輯自動開 `flight_session`**。
2. backend 訂閱 `mission_progress()`，把「目前第幾個航點」寫進 live state 並經
   WebSocket 推給前端；每次 item 推進發一筆 `mission_item_reached` 事件。
3. 1Hz 的 telemetry / link_metrics 照常入庫，鏈路事件照常發。
4. 落地 disarm → 現有邏輯自動關閉架次並計算摘要。

把 `mission_id` 記進 `flight_sessions`，就能回答「這個架次執行的是哪條航線」。

### 階段 4 · 分析（本系統）

回放頁把**計畫航線**（來自 QGC）與**實際軌跡 + SINR 上色**（來自我們的記錄）
疊在同一張圖上，配合 SINR/RTT 時序圖。這是整條流程的研究產出，
也是 QGC 完全不提供的能力。

## 需要新增的實作

### backend

| 項目 | 位置 | 說明 |
|---|---|---|
| mission watcher 背景任務 | `app/mission.py`（新）| 5–10 秒輪詢 `download_mission()`，指紋比對，變更時入庫 + 廣播 |
| `mission_progress` 訂閱 | `app/ingest.py` | 加一路訂閱，寫入 `live.mission_current` / `mission_total` |
| `.plan` 匯入 API | `app/api.py` | `POST /api/missions/import`，用 `import_qgroundcontrol_mission_from_string()` |
| live state 欄位 | `app/state.py` | `mission_id` / `mission_current` / `mission_total` |

### DB schema

```sql
ALTER TABLE missions
  ADD COLUMN source      TEXT NOT NULL DEFAULT 'qgc',   -- qgc / internal
  ADD COLUMN plan_json   JSONB,                          -- 原始 .plan（含 QGC 專屬資訊）
  ADD COLUMN fingerprint TEXT,                           -- 變更偵測用
  ADD COLUMN uploaded_at TIMESTAMPTZ;

ALTER TABLE flight_sessions
  ADD COLUMN mission_id UUID REFERENCES missions(id);     -- 這趟飛的是哪條航線
```

`events.type` 新增：`mission_uploaded` / `mission_started` / `mission_item_reached` /
`mission_finished`。

### 前端

- 地圖新增「計畫航線」圖層（虛線 + 航點編號），與實際軌跡的四段上色並存。
- 側欄顯示任務進度（第 N/M 個航點）。
- 回放頁的計畫 vs 實際對照。

## 待決事項

| 項目 | 說明 |
|---|---|
| 校正期間的入庫策略 | [issues/004](../issues/004-writes-while-disarmed.md)，QGC 讓這題更急迫 |
| 輪詢間隔 | 5 秒（反應快）vs 10 秒（更省）。實測單次 13ms，傾向 5 秒 |
| 複雜航線（Survey/CorridorScan）| 飛控端只有展開後的 waypoint。要不要解析 `.plan` 還原原始形狀？初期可先不做 |
| `.plan` 檔的取得方式 | backend 下載不到原始 `.plan`。要嘛操作員手動上傳到我們的 API，要嘛只存展開後的 waypoint。初期建議後者，需要時再補匯入介面 |
| 真機 router 方案 | mavlink-router vs PX4 多 instance，待真機階段實測 |

## 參考

- [Plan File Format · QGC Dev Guide](https://docs.qgroundcontrol.com/master/en/qgc-dev-guide/file_formats/plan.html)
- [MissionRaw · MAVSDK-Python](http://mavsdk-python-docs.s3-website.eu-central-1.amazonaws.com/plugins/mission_raw.html)
- [mavlink-router](https://github.com/mavlink-router/mavlink-router)
- [MAVLink Peripherals (GCS/OSD/Companion) · PX4 Guide](https://docs.px4.io/main/en/peripherals/mavlink_peripherals)
- [Sensor Setup (PX4) · QGC Guide](https://docs.qgroundcontrol.com/master/en/qgc-user-guide/setup_view/sensors_px4.html)
