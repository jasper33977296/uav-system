# 對外即時資料：串流（WebSocket）與輪詢（HTTP）

> 給**外部控制端**用。2026-09-14 與使用者逐條定案，2026-09-16 補兩條（§13）。
> 狀態：**已實作**（串流 2026-09-14、輪詢 2026-09-16），**尚未實飛驗收**，見 §13。
> 事後比較兩趟或多趟的訊號見 [`external-history-api.md`](external-history-api.md)。

外部控制端自己產生一組 UUID 當**任務編號**，用它連上地面站，再帶著同一組編號呼叫起飛。
**從起飛那一刻起每 0.5 秒**收到任務裡每一台機的狀態，外加預計航線、已飛過的實際軌跡與重要事件；
**最後一台上鎖 3 秒後**任務自動結束、收到 `ended`。
控制端拿這些資料在自己的 OSM 地圖上畫出無人機的即時情況。

拿的方式有兩種，**送的是同一份訊息**：地面站推的 WebSocket 串流（§2–§7），
與控制端拉的 HTTP 輪詢（§8）。長連線方便就用串流，不方便就用輪詢，混用也可以。

---

## 1. 定位

| | |
|---|---|
| 誰連誰 | **外部控制端**連到**地面站** backend `:38000`。起飛指令仍在 command `:38001` |
| 傳法 | **串流** `ws://…:38000/ws/v1/missions/{uuid}`（推）與**輪詢** `GET http://…:38000/api/v1/ext/missions/{uuid}/live`（拉）。訊息逐字相同（§8） |
| 方向 | **只讀**。兩種都不收指令 |
| 契約 | **路徑一律帶版本**，版本號緊接在服務根之後：`/api/v1/…`、`/ws/v1/…`；每則訊息另外帶 `v`。**與畫面用的 `/ws/telemetry` 分開**——那一條是內部欄位原樣倒出，畫面改一次外部就壞一次 |
| 時間 | 一律**地面站時鐘**（UTC、ISO 8601）。機上 Pi 已與地面站對時（2026-09-14 實測差 5.6 ms） |
| 座標 | WGS84。GeoJSON 照規範是 **[經度, 緯度, 高度]**；`state` 裡的位置用具名欄位 `lat`／`lon`，不會搞反 |
| 認證 | **沒有**（§11） |

---

## 2. 即時資料綁「任務」

### 2.1 系統裡的三個名詞

| | 路徑（plan） | 任務（mission） | 架次（session） |
|---|---|---|---|
| 是什麼 | 一份航線：航點、高度、速度 | 一件要做完的事 | 一台機從解鎖到上鎖 |
| 誰建立 | 人在規劃頁畫、或匯入 `.plan` | **人宣告**（我們的畫面在起飛時問；外部控制端在呼叫起飛時帶編號） | **系統自動**：解鎖就有、上鎖就結束 |
| 範圍 | 可以飛很多次 | 可以有**多台機**、**多個架次** | 一台機、一顆電池 |
| 關聯 | — | 一台機同一時間只能在一個任務裡 | 解鎖時自動掛到這台機進行中的任務 |

**即時資料的鍵是任務**，不另外發明概念：任務在起飛前就存在，一個任務可以涵蓋群飛的每一台機，
而每台機的架次會自動掛上去。一條連線（或一支輪詢網址）看的就是「這個任務底下所有機的所有架次」。

### 2.2 流程

```
控制端                                         地面站
  │ 1. 產生 UUID（任務編號）
  │ 2. 連 ws://<地面站>:38000/ws/v1/missions/<UUID> ──▶ 回 hello（phase: waiting）
  │ 3. POST <地面站>:38001/api/v1/start ──────────────▶ 用這個 UUID 建立任務，開始起飛流程
  │      {"plan_id": "…", "mission_id": "<UUID>"}        ↓
  │ ◀──────────────────────────────── route（預計航線）、每 0.5 秒 state、event
  │                                                    （地面待命、上傳、解鎖、爬升都看得到）
  │ ◀──────────────────────────────── 最後一台上鎖 3 秒後：自動結束任務、送 ended、關閉連線
```

* **第 2 步換成輪詢也一樣**：改拉 `GET :38000/api/v1/ext/missions/<UUID>/live`，
  其餘每一步都不變（§8）。
* **先連再起飛**：`/api/v1/start` 要等飛機爬到起飛高度、切進任務模式才回應。先連上
  （輪詢的話先拉一次），上傳、解鎖、起飛那一段才看得到。
* **不帶 `mission_id` 也可以**：地面站自己產生，放在回應的 `stream` 裡——但那時飛機已經在天上了。

### 一次執行＝一個任務（2026-09-23 裁定，**契約變更**）

**每一次起飛請求都建立一個新任務**：同一條路徑飛三趟是**三件事**，疊在一個任務底下，
「比較這趟與那趟」（`ext/missions/{id}`）就拿不出來。現階段**一個任務綁定一條路徑**
（前端與外部都一樣；記在 `missions.plan_id`）。

| 情況 | 以前 | 現在 |
|---|---|---|
| 帶的 `mission_id` 對到進行中的任務 | 掛進那個任務 | **409 `mission_id_used`**——換一個新的 UUID |
| 帶的 `mission_id` 對到已結束的任務 | 409 `mission_ended` | **409 `mission_id_used`**（同一個代碼，訊息說它是進行中還是已結束）|
| 沒帶 `mission_id`、這台機還在別的任務裡 | 沿用那個任務（或 409 `mission_busy`）| **建立新任務**，並把舊的結束掉 |

**舊的任務被結束時會說出來**：回應多一個 `replaced: [{id, name, drone}]`。
資料庫的不變式是「一台機一次只能在一個任務」，所以新的一趟要成立，前一個就得收尾——
但那件事**不能默默發生**。

> 重用同一個 UUID 的呼叫端會開始收到 409。訊息與 `how_to` 說得出要做什麼
> （產生新的 UUID，或乾脆不給）。
  這台機若已經在一個進行中的任務裡，就直接掛進那個任務。
* **群飛**：`POST :38001/api/v1/command/group/{group_id}/execute`，同樣帶 `{"mission_id": "<UUID>"}`，
  一條連線涵蓋群組裡的所有機。**不帶 `mission_id` 也不帶 `mission_name` 時照舊不建任務**（畫面按的群飛就是這樣）。
* **看從我們畫面啟動的任務**：`GET :38000/api/v1/missions/active` 拿到任務編號，連同一個網址。
  這種任務由操作員在畫面上結束（§2.5）。

### 2.3 `/api/v1/start` 的參數

| 欄位 | 必填 | 說明 |
|---|---|---|
| `plan_id` | 與 `plan` 二選一 | **路徑**：路徑庫的 id 或名稱 |
| `plan` | 與 `plan_id` 二選一 | `missions/` 目錄裡的 `.plan` 檔名（會先存進路徑庫再飛） |
| `mission_id` | 選填 | **這個新任務要用的編號**（UUID）。不給就由地面站產生。**必須沒被用過**——見下面的「一次執行＝一個任務」 |
| `mission_name` | 選填 | 任務名稱，不可與既有任務同名。不給時用「`<路徑名> <MM-DD HH:MM>`」（同分鐘再撞就加秒數、再撞加 `#2`）|
| `sysid` | 選填 | 飛哪一台（不給＝主機） |
| `takeoff_alt` | 選填 | 起飛高度 |
| ~~`mission`~~ | 舊名 | **就是 `plan_id`**（2026-09-08 改名前的叫法，那時「任務」指的是路徑）。暫時保留相容，下一版移除 |

成功的回應多一個 `stream`：

```json
{"source": "db", "plan_id": "…", "name": "0914-square-test-v5", "sysid": 1, "ok": true,
 "steps": {…},
 "mission_id": "8f0c…", "mission_name": "0914-square-test-v5 09-14 12:01",
 "stream": {"mission_id": "8f0c…", "url": "ws://<地面站>:38000/ws/v1/missions/8f0c…"}}
```

被擋下時回 HTTP 錯誤。**每一個錯誤都同時帶機器讀的 `code` 與人讀的 `msg`**，
呼叫端自己排除得了的再附 `how_to`——只給代碼，對方就得來問我們那是什麼意思：

```json
{"detail": {"code": "mission_id_used",
            "msg": "mission_id 8f0c… 已經是任務「0914 巡檢」（還在進行中）。每一次執行都是新的一個任務——同一條路徑飛多趟，那是多件事",
            "how_to": ["用新的 UUID（crypto.randomUUID()／uuid.uuid4()）再送一次",
                       "或不給 mission_id，讓地面站產生"]}}
```

| HTTP | `code` | 什麼時候 | `msg` 說明什麼 |
|---|---|---|---|
| `422` | `mission_id_invalid` | `mission_id` 不是 UUID | 收到的值是什麼、要的格式（例如 `crypto.randomUUID()` 產生的） |
| `409` | `mission_id_used` | `mission_id` 已經被用過（進行中或已結束）| 那個任務的名稱與狀態；**一次執行＝一個任務**，要用新的 UUID 或不給 |
| `409` | `mission_busy` | **只剩競態**：兩個請求同時替同一台機建任務時，資料庫的不變式擋下慢的那一個（以前是常態，現在舊任務會被結束，見上）| 撞到哪兩個任務 |
| `409` | `mission_name_taken` | `mission_name` 與既有任務同名（不分大小寫） | 撞名的是哪一個任務 |
| `422` | `plan_required` | `plan_id` 與 `plan` 都沒給或都給了 | 兩者的差別 |
| `409` | `drone_unknown` | 這個 sysid 在地面站還沒有機體記錄 | 等收到心跳再試 |

起飛流程本身的失敗（預檢不過、解鎖被拒、沒離地…）**也是同一個格式**：沒有專屬代碼的依 HTTP 狀態給通用代碼
（`forbidden`、`not_found`、`conflict`、`vehicle_rejected`、`timeout`…），起飛後判定不到離地是 `not_airborne`。
群飛執行帶 `mission_id` 時同理。其他指令端點（上傳、切模式）的錯誤形狀照舊。

`mission_id` 對到一個**進行中**的任務時，這次起飛就掛到那個任務底下（例如群飛裡補飛一台）。

### 2.4 階段（`phase`）

| phase | 什麼時候 |
|---|---|
| `waiting` | 控制端已經用這組 UUID 連上，但還沒有人用它呼叫起飛 |
| `starting` | 起飛流程進行中（上傳、解鎖、起飛），還沒有任何一台離地 |
| `active` | 至少一台解鎖中 |
| `ended` | 見 §2.5。送出 `ended` 後連線關閉 |

`waiting` 最多等 **30 秒**：連上之後 30 秒內沒有人用這個編號呼叫起飛，送 `ended`
（`reason: "never_started"`，附 `msg`）並關閉。**收到** `/api/v1/start` 那一刻就進 `starting`，
不是等它回應——它要等飛機爬到起飛高度才回，那可能超過 30 秒。
正常用法是連上後馬上呼叫起飛；打錯編號的連線也不會一直掛著。

### 2.5 結束

**用 `/api/v1/start`（或群飛執行）建立的任務**：任務裡每一台都已上鎖，**最後一台上鎖 3 秒後**，
地面站自動結束任務，送 `ended`，以 `1000` 關閉連線。**這 3 秒內任何一台又解鎖，就不結束**。
要換電池再飛，開一個新任務（新的 UUID）。

**從我們畫面建立的任務**：畫面在落地時問操作員「任務結束了嗎？」，**操作員結束時**才送 `ended`。
兩趟之間照常每 0.5 秒送狀態。

每一台各自帶結束原因：

| `reason` | 意思 | 依據 |
|---|---|---|
| `landed` | 著地後上鎖 | 著地狀態先到 `on_ground`，接著上鎖 |
| `crash` | **飛控判定墜機而切斷馬達** | 飛控送出 `Crash: Disarming …`。2026-09-14 11:56 那一趟就是這個，而架次列表記的是普通的「上鎖」——**控制端必須分得出落地與摔機** |
| `disarmed_in_air` | 上鎖時沒有著地訊號 | 例如飛控其他保護機制上鎖 |
| `telemetry_lost` | 解鎖中**連續 90 秒**沒有遙測，地面站收掉這一趟 | 與 backend 的 `SESSION_LOST_S` 同一個值。**這不代表飛行結束**，只代表資料在那裡斷了 |
| `start_failed` | 起飛流程被拒（上傳、解鎖、起飛任一步） | command 回傳的失敗步驟 |
| `aborted` | 群飛全撤 | `group/{id}/abort` |

**不會結束的情況**（照實送，不自作主張）：

* **著地了但一直沒上鎖**（例如手飛落地、油門沒拉到底）：繼續送，該機 `landed: "on_ground"`、`armed: true`。
* **航線最後沒有降落項**（例如從 QGC 匯入的）：飛機會在最後一點懸停，串流一直開著，直到有人讓它降落。
  本系統規劃頁產生的航線一定帶降落項。
* **控制端斷線**：不影響飛行，也不影響任務何時結束。

**3 秒夠嗎——自動上鎖是實測過的**（這台 ArduCopter 4.7，2026-09-14 的五次降落）：任務的降落項或 LAND 模式降落，
著地後 **0.4～0.8 秒**就上鎖，所以「上鎖後 3 秒」離著地大約 4 秒。
**沒有樣本、未驗證**：LOITER／STABILIZE 手飛直接落地（依 ArduPilot 規則是油門最低後 10 秒才上鎖）、RTL 降落。

---

## 3. 訊息

全部是 JSON 文字訊息。每一則都有：

```json
{"v": 1, "type": "…", "mission_id": "…", "seq": 1842, "ts": "2026-09-14T07:30:12.500Z"}
```

`seq` 在同一個任務內**所有型別共用、嚴格遞增**，補送就靠它（§5）。
**只屬於一條連線的訊息不帶 `seq`**：`hello`，以及連上時補的 `route`、`track`——它們不進補送緩衝。
`seq` 不從 1 開始（以毫秒時間起算，地面站重啟後仍比重啟前的大），只保證遞增。

### 3.1 `hello`：連上時第一則

```json
{"v": 1, "type": "hello", "mission_id": "…", "ts": "…",
 "phase": "active", "mission_name": "0914-square-test-v5 09-14 12:01",
 "started_at": "2026-09-14T04:01:31.000Z",
 "drones": [{"drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1}],
 "replay": {"from_seq": 1700, "gap": null}}
```

`waiting` 時 `drones` 是空的、`started_at` 是 `null`。

### 3.2 `route`：預計航線

起飛流程開始時送；連上或重連時再送一次；**飛行中改航線時也送**（`reason: "change_route"`）。
群飛時每台機各一則。

```json
{"v": 1, "type": "route", "mission_id": "…", "seq": 1841, "ts": "…", "reason": "initial",
 "drone_id": "1d2f…", "plan_id": "…", "plan_name": "0914-square-test-v5",
 "home": {"lat": 24.773548, "lon": 121.045883, "alt_msl": 129.0},
 "geojson": {"type": "FeatureCollection", "features": [
   {"type": "Feature",
    "geometry": {"type": "LineString",
                 "coordinates": [[121.045883, 24.773548, 0], [121.045911, 24.773812, 3.0]]},
    "properties": {"role": "planned_path"}},
   {"type": "Feature",
    "geometry": {"type": "Point", "coordinates": [121.045911, 24.773812, 3.0]},
    "properties": {"role": "waypoint", "seq": 3, "kind": "waypoint"}}]}}
```

* 點的 `kind`：`takeoff`／`waypoint`／`land`／`rtl`。`seq` 是**路徑自己的項目序號**（0 起，含沒有座標的指令項），
  與 `state.mission_progress.current`、`waypoint_reached` 的序號對得上（ArduPilot 機上的序號多一格 home，地面站換算好了）。
* 第三個座標是**離起飛點高度**（公尺）。高度基準是離地（frame 10）而換不出來時只給 [經度, 緯度]。
* `reason`：`initial`（起飛流程開始、或第一次連上）／`reconnect`（帶 `after_seq` 重連）／`change_route`（飛行中改航線）。

### 3.3 `track`：已經飛過的實際軌跡

**連上或重連時送一次**（在 `route` 之後），內容是這個任務從開始到現在每台機實際飛過的路徑。
之後控制端用每則 `state` 的位置接著往下畫。群飛時每台機各一則。

```json
{"v": 1, "type": "track", "mission_id": "…", "ts": "…",
 "drone_id": "1d2f…",
 "geojson": {"type": "Feature",
   "geometry": {"type": "MultiLineString", "coordinates": [
     [[121.045883, 24.773548, 0.1], [121.045890, 24.773600, 2.4]],
     [[121.045930, 24.773900, 3.1], [121.045950, 24.774000, 3.0]]]},
   "properties": {"from": "2026-09-14T04:01:37Z", "to": "2026-09-14T04:02:31Z",
                  "points": 55, "interval_s": 1}}}
```

* **一秒一點**（與地面站存檔的解析度相同）。
* **遙測斷掉超過 10 秒的地方分成兩段**（`MultiLineString`）——不畫成一條直線，
  否則會被讀成飛機直直飛過去。
* 中途才連上、或斷線超過補送範圍（§5）的控制端，靠這一則把軌跡補齊。

### 3.4 `state`：每 0.5 秒

**從起飛流程開始，固定每 0.5 秒送一次，沒有變化也送。**

```json
{"v": 1, "type": "state", "mission_id": "…", "seq": 1843, "ts": "2026-09-14T07:30:12.500Z",
 "phase": "active",
 "drones": [{
   "drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1,
   "freshness": "live", "age_s": 0.3, "connected": true,
   "position": {"lat": 24.773540, "lon": 121.045880, "alt_rel": 12.3, "alt_msl": 135.6},
   "last_known": null,
   "heading": 87.0, "ground_speed": 2.0, "vertical_speed": 0.1,
   "flight_mode": "AUTO", "mode_verb": "mission",
   "armed": true, "landed": "in_air",
   "mission_progress": {"current": 3, "total": 8, "state": "active"},
   "battery": {"pct": 76, "voltage": 15.9},
   "gps": {"fix": 3, "sats": 22},
   "link": {"state": "ok", "age_s": 0.4, "time": "2026-09-14T07:30:12.130Z",
            "rsrp": -100.0, "rsrq": -11.0, "sinr": 18.0, "cqi": null,
            "pci": 133, "cell_id": 2179073, "band": "n79", "nr_mode": "SA",
            "rtt_ms": 27.0, "jitter_ms": null, "packet_loss_pct": null,
            "throughput_up_kbps": null, "throughput_down_kbps": null}}]}
```

| 欄位 | 單位／值域 | 說明 |
|---|---|---|
| `freshness` | `live`／`stale`／`old`／`never` | 見 §6 |
| `sysid` | 整數 | 下指令用的號碼 |
| `age_s` | 秒 | 距離最後一次收到這台機的遙測 |
| `position.alt_rel` | 公尺 | **離起飛點** |
| `position.alt_msl` | 公尺 | 海拔 |
| `heading` | 度，0～360 | 機頭朝向，以北為 0、順時針。飛控沒給時 `null` |
| `ground_speed` | m/s | |
| `vertical_speed` | m/s | **向上為正** |
| `flight_mode` | 原廠模式名 | ArduPilot：`AUTO`、`LOITER`、`RTL`、`LAND`、`STABILIZE`… |
| `mode_verb` | `mission`／`hold`／`rtl`／`land`／`position`／`guided`／`null` | **廠牌無關**的動作。手動類模式（STABILIZE 等）是 `null`。判斷用這個，顯示用 `flight_mode` |
| `landed` | `on_ground`／`takeoff`／`in_air`／`landing`／`null` | 飛控的著地狀態 |
| `mission_progress.current` | 路徑序號 | **跑完後維持在最後一項**。飛控跑完 Land 會立刻回報「第 1 項」，那不是任務重來，這裡不照送 |
| `mission_progress.state` | `not_started`／`active`／`paused`／`complete`／`null` | 飛控上那份航線的執行狀態 |
| `gps.fix` | 0～6 | 3＝3D，≥4＝差分／RTK |
| `link.state` | `ok`／`degraded`／`stale`／`lost`／`unknown` | 5G 鏈路。`link.age_s` 超過 5 秒是 `stale`、超過 30 秒是 `lost` |
| `link.age_s` | 秒 | 訊號量測幾秒前收到的。太大代表**量測送不回來**，這與「訊號很差」是兩件事 |
| `link.time` | ISO 8601 | 這筆量測的採樣時刻 |
| `link.rsrp` | dBm | 參考訊號接收功率 |
| `link.rsrq` | dB | 參考訊號接收品質 |
| `link.sinr` | dB | 訊號干擾雜訊比 |
| `link.cqi` | 0～15 | 通道品質指示 |
| `link.pci` | — | Physical Cell ID（十進位；只在鄰區內唯一） |
| `link.cell_id` | — | 全域 cell 識別碼（NCI/CGI） |
| `link.band` | — | 頻段，如 `n79` |
| `link.nr_mode` | `SA`／`NSA`／`LTE` | |
| `link.rtt_ms` | ms | 來回時間 |
| `link.jitter_ms` | ms | 抖動 |
| `link.packet_loss_pct` | % | 封包遺失率 |
| `link.throughput_up_kbps`／`link.throughput_down_kbps` | kbps | 上行／下行吞吐 |

訊號指標量測不到的是 `null`，不補假值。

`waiting` 時送的是 `{"type": "state", "phase": "waiting", "drones": []}`——只為了讓控制端知道連線還活著（§4）。

**原本輪詢快照 `/api/ext/live` 的欄位全部在 `state` 裡**，那支端點移除（2026-09-14 定案）。
**要輪詢請用 §8**：它綁任務、拿的是同一份 `state`，而 `/api/ext/live` 是全機隊的、與任務無關——
取代它的不是「不給輪詢」，是「輪詢也綁任務」（2026-09-16 定案）。

| `/api/ext/live` | `state.drones[]` |
|---|---|
| `drone_name`／`mav_sysid` | `name`／`sysid` |
| `connected`／`armed` | 同名 |
| `lat`／`lon`／`alt_rel`／`alt_msl` | `position.*`（`old` 時改看 `last_known`） |
| `ground_speed`／`vertical_speed`／`heading` | 同名 |
| `telem_age_s` | `age_s`，另有 `freshness` |
| `link_state`／`link_age_s` | `link.state`／`link.age_s` |
| `link.*`（14 項訊號指標） | `link.*`，名稱不變 |

### 3.5 `event`：重要變化

```json
{"v": 1, "type": "event", "mission_id": "…", "seq": 1850, "ts": "…",
 "drone_id": "1d2f…", "kind": "waypoint_reached", "severity": "info",
 "text": "到達第 4 項（共 8 項）", "detail": {"seq": 4, "total": 8}}
```

| `kind` | 什麼時候 |
|---|---|
| `start_step` | 起飛流程每一步的結果（上傳、解鎖、起飛、切任務），`detail.ok` |
| `armed`／`disarmed` | 解鎖／上鎖。`disarmed` 帶 `detail.reason`（同 §2.5） |
| `takeoff`／`landed` | 著地狀態轉到 `in_air`／`on_ground` |
| `mode_change` | 模式變了，`detail.from`／`detail.to`（原廠名與 verb 都帶） |
| `waypoint_reached` | 到達航點 |
| `mission_progress` | 飛控上航線的執行狀態變了 |
| `route_changed` | 飛行中改航線（接著會送新的 `route`） |
| `link_degraded`／`link_lost`／`link_recovered` | 5G 鏈路 |
| `telemetry_lost`／`telemetry_resumed` | 這台機的遙測斷了 10 秒以上／恢復 |
| `failsafe` | 飛控回報進入 CRITICAL／EMERGENCY 狀態 |
| `crash` | 飛控送出 `Crash: Disarming` |
| `vehicle_text` | 飛控的文字訊息，**只轉警告以上**；同一句 30 秒內重複的不另外送，下一次送出時 `detail.count` 帶上累積次數——實測真機每分鐘會重複送同一句預檢失敗，不過濾會把其他事件淹掉 |

`severity`：`info`／`warning`／`critical`。

### 3.6 `ended`

```json
{"v": 1, "type": "ended", "mission_id": "…", "seq": 2201, "ts": "…",
 "drones": [{"drone_id": "1d2f…", "reason": "landed", "session_ids": ["…"],
             "landed_at": "2026-09-14T04:02:32.060Z", "disarmed_at": "2026-09-14T04:02:32.570Z"}]}
```

`session_ids` 是這台機在這個任務裡的架次，事後可以拿去查回放或匯出。之後伺服器以 `1000` 關閉。

---

## 4. 控制端怎麼判斷斷線：3 秒

> 這一節講的是**串流**。輪詢沒有這個問題——一次呼叫失敗就是失敗，下一次重試即可（§8.5）。

**地面站保證每 0.5 秒一定送一則訊息**——`waiting` 時送保持連線用的 `state`，起飛後送真正的 `state`。
所以控制端的規則很簡單：

> **超過 3 秒沒有收到任何一則訊息，就當成連線斷了**：關掉這條連線，帶著最後收到的 `seq` 重連（§5）。

為什麼不能只等 WebSocket 自己報錯：網路中間斷掉時（例如 5G 或跨網段的路由掉包），
TCP 可能要**好幾十秒**才發現對方不在了。那段時間連線看起來還開著，地圖上的飛機卻停在原地——
**看起來像飛機懸停，其實是資料沒有進來**。3 秒是正常間隔的 6 倍，一般的網路抖動不會誤判。

判斷斷線後，控制端應該把地圖上的機體標成「資料中斷」，而不是讓它停在最後的位置看起來正常。

---

## 5. 斷線重連與補送

* 伺服器為每個任務保留**最近 60 秒**的訊息。
* 重連時帶上最後收到的序號：

  ```
  ws://<地面站>:38000/ws/v1/missions/<UUID>?after_seq=1842
  ```

* 伺服器依序送：`hello` → 當下的 `route` → `track`（完整實際軌跡）→
  **序號大於 1842 的緩衝訊息**（每則帶 `"replay": true`）→ 接回即時。
* 漏掉的比緩衝還舊時，`hello.replay.gap` 說明缺了哪一段：`{"from_seq": 1843, "to_seq": 1990}`。
  **軌跡不會缺**（`track` 補齊），**缺的是那段期間的事件與逐則狀態**——照實說，不假裝沒缺。
* `ended` 送出後緩衝再保留 **30 秒**——在最後幾秒斷線的控制端，重連還拿得到 `ended`；
  超過 30 秒才重連會收到 `4410`。
* 更早的完整資料不走串流：用 `GET :38000/api/v1/sessions/{session_id}/export`。

---

## 6. 過期資料

**與本系統自己的畫面同一套規則**（`apps/frontend/lib/staleness.ts`）。理由：2026-08-26 畫面顯示
`armed=true / LAND / 高度 1.07 m`，那是**兩個半小時前**的殘影——一個照常顯示的舊數字比沒有數字更危險。

| `freshness` | `age_s` | 送什麼 |
|---|---|---|
| `live` | < 2 | 全部照送 |
| `stale` | 2～10 | 全部照送，由 `freshness` 標明。控制端應該把那台機畫淡 |
| `old` | > 10 | 除了身分、`freshness`、`age_s`、`connected`、`link` 以外全部是 `null`（位置、朝向、速度、模式、解鎖、著地、任務進度、電量、GPS）；**最後已知位置放在 `last_known`**：`{"lat", "lon", "alt_rel", "at"}` |
| `never` | 從未收到 | 除身分外全部 `null` |

`old` 時把位置拿掉而不是只標記，是為了**讓人讀不到那個數字**——一個標著「舊」的座標仍然會被畫成「飛機在這裡」。

§4 的「連線斷了」與這裡的「資料舊了」是兩件事：連線好好的，飛機的遙測也可能斷了（`old`）。

---

## 7. 連線與流量

* **一個任務可以同時有多個控制端**連上。
* 每個連線**各自排隊**：送不出去時丟掉最舊的 `state`（`route`、`track`、`event`、`ended` 不丟），
  下一則 `state` 帶 `"dropped": N`。**一個慢的控制端不准拖慢其他人**——包括本系統自己的畫面
  （現有的 `/ws/telemetry` 是一個送完才送下一個，實作時不能沿用）。
* 流量：每台機的 `state` 約 1 KB，一秒兩則；`track` 一分鐘的飛行約 3 KB。
* 關閉碼：

| code | 意思 |
|---|---|
| `1000` | 正常結束（`ended` 之後） |
| `4400` | 網址裡的編號不是 UUID |
| `4410` | 任務已結束，補送緩衝也過期了 |
| `1011` | 伺服器錯誤 |

**以錯誤關閉之前，先送一則 `error`**，理由與 HTTP 錯誤一樣要帶解釋——WebSocket 的關閉原因最多 123 bytes，
中文放不下一句完整的話：

```json
{"v": 1, "type": "error", "mission_id": "8f0c…", "ts": "…",
 "code": "mission_gone",
 "msg": "任務「0914 巡檢」已在 07:42:45 結束，結束後 30 秒的補送也過期了",
 "how_to": ["要看這次飛行的完整資料：GET :38000/api/v1/sessions/{session_id}/export"]}
```

| 關閉碼 | `code` |
|---|---|
| `4400` | `mission_id_invalid` |
| `4410` | `mission_gone` |
| `1011` | `server_error` |

---

## 8. 另一種傳法：HTTP 輪詢

有些控制端不方便維持長連線——走 HTTP proxy、跑在無狀態的後端、或就是想每秒拉一次。
同一份即時資料另外開一支輪詢端點：**推與拉只差在管道，`messages` 裡的每一則與串流送出去的逐字相同**。
所以兩種可以混用：WebSocket 斷線的那段先用輪詢頂著，接回來再換回去。

```
GET http://<地面站>:38000/api/v1/ext/missions/{mission_id}/live[?after_seq=1842]
```

### 8.1 兩種用法

| 帶不帶 `after_seq` | 回什麼 | 什麼時候用 |
|---|---|---|
| **不帶＝快照** | 現在的 `route`、`track` 與**現算的** `state`（任務已結束就再附 `ended`） | 第一次呼叫。不必等下一拍就有完整畫面 |
| **帶＝補送** | 序號大於它的每一則，語意與串流重連的 `?after_seq=` 完全相同 | 之後每一次，帶上次回應的 `seq` |

```json
{
  "v": 1,
  "mission_id": "8f0c…",
  "ts": "2026-09-16T07:30:12.500Z",
  "phase": "active",
  "mission_name": "0914-square-test-v5 09-16 15:30",
  "started_at": "2026-09-16T07:29:40.120Z",
  "drones": [{"drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1}],
  "seq": 1789538218542,          // 下一次帶 after_seq=這個
  "replay": {"from_seq": 1789538218400, "gap": null},
  "poll_after_s": 0.5,           // 地面站的節拍。拉得比它快只會拿到空的 messages
  "messages": [ … ]              // 與串流逐字相同的 state／event／route／track／ended
}
```

外層那幾個欄位就是串流 `hello` 的內容（`phase`、`mission_name`、`started_at`、`drones`、`replay`），
而且**每一次都給**——輪詢沒有「連上的那一刻」，所以不另外分出一則 `hello`。

### 8.2 兩個不一樣的地方

* **快照的 `state` 沒有 `seq`。** 它與 `route`、`track` 一樣是「只屬於這一次呼叫」的訊息
  （串流那邊的 `hello` 與連線時補的 `route`／`track` 也都不帶序號、不進緩衝，§3）。
  **要接續請用外層的 `seq`**，不要從 `messages` 裡挑。
  這一則是**現算的**，不是緩衝裡最後一則：剛掛上來的任務要到下一拍才有 `state`，
  而快照的用途正是「不必等下一拍」。
* **緩衝一樣只有 60 秒。** 拉得太慢會掉東西，`replay.gap` 會說缺了哪一段（與 §5 同一套）。
  不想漏就照 `poll_after_s` 的節拍拉。

### 8.3 與串流一模一樣的地方

* **進場**：沒見過的編號一樣是「開場」而不是錯誤——輪詢的控制端也可以先用自己的 UUID
  拉一次、再帶著它呼叫起飛（§2.2 的「先連再起飛」）。
* **結束**：結束條件完全相同（最後一台上鎖 3 秒後，§2.5）。
  **輪詢不會讓任務活得比較久，也不會讓它提早結束**——這支端點只是把同一份訊息換個方式交出去。
* **過期資料**：`state` 的新舊分級同 §6。
* **錯誤**：編號不是 UUID 回 `422` `mission_id_invalid`；任務已結束且補送也過期回 `410` `mission_gone`。
  `code`／`msg`／`how_to` 與串流關閉前那則 `error` **是同一份**（同一段程式產生，§13）。

### 8.4 最小的輪詢控制端

```js
const missionId = crypto.randomUUID();
let seq = null, timer = null;
const url = () => `http://GS:38000/api/v1/ext/missions/${missionId}/live`
                + (seq == null ? "" : `?after_seq=${seq}`);

async function poll() {
  const r = await fetch(url());
  if (r.status === 410) return stop();                    // 任務結束且補送過期
  const d = await r.json();
  seq = d.seq;
  if (d.replay.gap) markGap(d.replay.gap);                // 缺了就照實標，不把兩端連成一條線
  for (const m of d.messages) draw(m);                    // 與串流版是同一個函式
  if (d.messages.some((m) => m.type === "ended")) return stop();
  timer = setTimeout(poll, d.poll_after_s * 1000);
}
function stop() { clearTimeout(timer); }

poll();
await fetch("http://GS:38001/api/v1/start", {method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({plan_id: "0914-square-test-v5", mission_id: missionId})});
```

**`draw(m)` 與 §9 那支串流版共用**——兩種傳法送同一份訊息，用意就在這裡。

### 8.5 該選哪一個

| | 串流（§2–§7） | 輪詢（§8） |
|---|---|---|
| 延遲 | 事情發生就送到 | 最多差一個輪詢間隔 |
| 斷線 | 要自己做 3 秒看門狗（§4）——TCP 可能幾十秒才發現對方不在 | 一次呼叫失敗就是失敗，下一次重試即可 |
| 連線 | 要維持長連線 | 無狀態，過 proxy 不必特別設定 |
| 流量 | 只送變化 | 每次都帶外層欄位（約 0.5 KB）＋那段期間的訊息 |

**畫即時地圖建議用串流**。輪詢是為了長連線不方便的場合，不是為了少寫幾行。

---

## 9. 最小的串流控制端（Leaflet）

```js
const missionId = crypto.randomUUID();
let lastSeq = null, ws = null, watchdog = null;
const markers = {}, tracks = {};

function connect() {
  const q = lastSeq == null ? "" : `?after_seq=${lastSeq}`;
  ws = new WebSocket(`ws://GS:38000/ws/v1/missions/${missionId}${q}`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.seq != null) lastSeq = m.seq;
    resetWatchdog();
    if (m.type === "route") L.geoJSON(m.geojson).addTo(map);            // GeoJSON：[經度, 緯度]
    if (m.type === "track") {
      tracks[m.drone_id]?.remove();
      tracks[m.drone_id] = L.geoJSON(m.geojson, {style: {color: "red"}}).addTo(map);
    }
    if (m.type === "state") for (const d of m.drones) {
      const p = d.position ?? d.last_known;                              // old 時位置在 last_known
      if (!p) continue;
      markers[d.drone_id] ??= L.marker([p.lat, p.lon]).addTo(map);      // Leaflet：[緯度, 經度]
      markers[d.drone_id].setLatLng([p.lat, p.lon])
        .setOpacity(d.freshness === "live" ? 1 : 0.4);
    }
    if (m.type === "ended") { clearTimeout(watchdog); ws.onclose = null; }
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

// §4：3 秒沒收到任何訊息＝連線斷了。不等 WebSocket 自己發現
function resetWatchdog() {
  clearTimeout(watchdog);
  watchdog = setTimeout(() => {
    for (const mk of Object.values(markers)) mk.setOpacity(0.2);        // 標成「資料中斷」
    ws.close();                                                          // onclose 會帶 after_seq 重連
  }, 3000);
}

connect();
await fetch("http://GS:38001/api/v1/start", {method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({plan_id: "0914-square-test-v5", mission_id: missionId})});
```

---

## 10. 實際軌跡從哪裡來

地面站在飛機**解鎖期間**每秒存一筆位置（`telemetry` 表，不設保留期限）。`track` 就是把這個任務底下
各架次的那些點依時間串起來，遙測斷超過 10 秒的地方切段。即時 `state` 的位置與它是同一個來源。

---

## 11. 明確不做

* **沒有認證**（2026-09-14 定案）。任何連得到地面站的人都能連上看任何一個任務的即時狀態，
  也能用 `/api/v1/start` 指揮飛機（[mission-api.md](mission-api.md) §4 同一件事）。**安全靠網段隔離**——
  這件事要寫在這裡，不能靠「大家都知道」。
* **兩種傳法都不收指令**。暫停、返航、改航線照舊走 command 服務。
* **不送圍欄**。控制端只需要預計航線與實際軌跡。
* **影像改成給了**（2026-09-23 使用者裁定，本文件同日修訂）：走 **RTSP**，網址在 command 服務的 `GET /api/v1/ext/drones` 每台機的 `video` 欄位裡。這條**不在**本文件的串流訊息裡——即時訊息是共用的（WS 與輪詢逐字相同），而網址裡的主機名要依每個呼叫端自己連進來的位址決定。
* 仍然不送原始 MAVLink、不送 IMU／驅動診斷這類內部欄位。
* 逐則訊息只補送 60 秒；軌跡以外更早的資料走匯出。
* **不再提供全機隊的輪詢快照** `/api/ext/live`：要輪詢即時資料請用綁任務的 §8，
  它與串流是同一份訊息。**不是不給輪詢，是輪詢也要綁任務**。

---

## 12. 定案紀錄

| # | 題目 | 定案 |
|---|---|---|
| 1 | 串流何時開始、結束 | **從起飛流程開始送**（不等切進任務模式）；**最後一台上鎖 3 秒後**任務自動結束、送 `ended`。前提「降落後會自動上鎖」先用真機資料確認過（§2.5） |
| 2 | 多機 | 一條連線涵蓋任務裡的所有機 |
| 3 | 過期資料 | 與畫面同一套規則，超過 10 秒位置改 `null`＋`last_known` |
| 4 | 斷線重連 | 序號＋補送最近 60 秒；實際軌跡另以 `track` 補齊 |
| 5 | 認證 | 不做 |
| 6 | 串流的鍵 | **任務**（不另外發明「一次執行」的概念）。控制端產生的 UUID 就是任務編號，也是連線的依據 |
| 7 | 任務何時結束 | **A**：外部建立的任務在最後一台上鎖 3 秒後自動結束；換電池再飛開新任務。畫面建立的任務照舊由操作員結束 |
| 8 | 飛控文字訊息 | 只轉警告以上，30 秒內重複的折成一則 |
| 9 | 航線資訊 | 不送圍欄；只送預計航線（`route`）與實際軌跡（`track`） |
| 10 | 斷線判斷 | 地面站保證每 0.5 秒一則；控制端 3 秒沒收到就當斷線（§4） |
| 11 | 畫面建立的任務 | 不自動結束，照舊由操作員在落地時結束 |
| 12 | 錯誤 | HTTP 與 WebSocket 的錯誤都帶 `code`＋`msg`（＋`how_to`），不只給代碼 |
| 13 | 連上後等多久 | 30 秒內沒有人用這個編號起飛就關閉 |
| 14 | 結束後保留多久 | `ended` 之後補送緩衝再留 30 秒 |
| 15 | 輪詢快照 | `/api/ext/live` 的欄位全部併進 `state`（含完整訊號指標），快照端點在串流上線時移除 |

### 12.1 2026-09-16 補的兩條

| # | 題目 | 定案 |
|---|---|---|
| 16 | 即時資料的傳法 | **串流與輪詢都提供**，`messages` 裡的每一則與串流逐字相同（§8）。這修正了 #15 的「只走串流」：移除的是**全機隊、與任務無關**的 `/api/ext/live`，取代它的是**綁任務**的 `…/missions/{id}/live` |
| 17 | 路徑版本 | 對外端點的路徑**一律帶版本**，版本號緊接在服務根之後：`/api/v1/…`、`/ws/v1/…`。舊的無版本路徑保留為別名。09-08 把上傳欄位 `mission_id` 改名成 `plan_id` 之所以會**無聲**打斷外部，就是因為指令那一組沒有版本可以並存——只能靠文件通知，而文件到不了已經寫死的程式 |

---

## 13. 實作

| 地方 | 做了什麼 |
|---|---|
| `apps/backend/app/ext_stream.py` | 串流本體：一個任務一個串流、0.5 秒一拍、60 秒補送緩衝、每條連線各自的送出佇列、結束判定、`route`／`track` 的組法 |
| `apps/backend/app/main.py` | `/ws/v1/missions/{uuid}`；失明開始／結束、架次因失聯收尾時通知串流 |
| `apps/backend/app/mavlink_rx.py` | 解鎖／上鎖、著地狀態轉換、STATUSTEXT（墜機判斷與 `vehicle_text`）通知串流 |
| `apps/backend/app/db.py` | `missions.external`；`insert_event` 寫完通知旁聽者——模式、航點、任務狀態、failsafe、鏈路事件由這裡轉出 |
| `apps/command/app/missions.py` | `/api/v1/start` 與群飛的任務建立／掛上，以及 422／409 |
| `apps/command/app/main.py` | `/api/v1/start` 新參數與 `stream`、錯誤補成 `{code, msg}`、起飛流程每一步通知 backend（`POST /api/ext/missions/{id}/notify`，內部用） |
| `apps/command/app/group_exec.py` | 群飛帶 `mission_id` 時建任務、逐台進度與失敗／全撤通知 |
| `apps/frontend/components/MissionPrompt.tsx` | 外部建立的任務落地時不問「結束了嗎」 |

**驗過的**（都沒有動到飛機）：
* `scripts/test-ext-stream.py`：判斷邏輯——航線與軌跡的組法、新舊分級、序號換算與「飛完回報第 1 項」、結束原因、文字折疊、3 秒結束與取消、等待逾時、佇列丟棄。
* `scripts/test-ext-missions.py`：任務建立，接真資料庫、只動臨時機與臨時任務。
* `scripts/test-ext-stream-live.py`：打正在跑的 backend，起飛流程用 notify 模擬——4400、4410、等待中每 0.5 秒一則、30 秒 `never_started`、結束後重連拿得到 `ended`、失敗後約 3 秒自動結束並寫回 `ended_at`、`after_seq` 補送接得上、缺口回報。

**還沒驗的**（要真的飛）：解鎖／離地／著地／上鎖事件的時序、墜機判定、`track` 的實際內容、群飛。先用 SITL 飛一趟單機與一趟群飛。

**還沒做**：串流實飛驗收後移除 `/api/ext/live`（`api.py` 的端點與 `EXT_LIVE_KEYS`／`EXT_LINK_KEYS`）。

### 13.1 2026-09-16 補的

| 地方 | 做了什麼 |
|---|---|
| `apps/backend/app/ext_stream.py` | `GET /api/ext/missions/{id}/live`：快照與補送共用串流的緩衝與訊息。**進場判斷抽成 `_open()`，WebSocket 與輪詢共用一份**——建串流、410 與那句話只留一份 |
| `apps/backend/app/main.py`、`apps/command/app/main.py` | `_api_version` middleware：把 `/api/v1/…` 的版本前綴剝掉再路由。**版本是路徑的前綴，不是每一支端點各自的事**——逐支加會漏掉新端點 |
| `apps/backend/app/ext_history.py` | 路由前綴改成剝完版本的 `/api/ext`；先上線時用過的 `/api/ext/v1/…` 由 middleware 一併收下 |

快照的 `state` 是**現算的**，不是撿緩衝裡最後一則：剛掛上來的任務要到下一拍才有 `state`，
而快照的用途正是「不必等下一拍」。它與 `route`／`track` 一樣不佔序號、不進緩衝。

**驗過的**（`scripts/test-ext-live-poll.py`，打正在跑的兩個服務，只讀既有資料＋一組跑完就刪的臨時資料）：
五組端點的 `/api/v1/…` 與無版本路徑走的是同一支、舊拼法 `/api/ext/v1/…` 仍通、沒有的版本不會被吞掉（`/api/v9/…` 回 404）；
輪詢的 422／410／沒見過的編號是開場／快照只給一則不帶序號的 state／`after_seq` 補送沒有缺號／太舊時報得出缺口；
**同一個任務同時開 WS 與輪詢，重疊的七則訊息逐欄相同**；臨時任務的快照帶得出 `route` 與 `track`，
而且快照現算的 `state` 與串流下一拍送的逐欄相同。歷史 API 的 20 項回歸照跑。

**還沒驗的**：輪詢在真飛時的表現（與串流同一批，要等實飛驗收）。
