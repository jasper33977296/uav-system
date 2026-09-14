# 對外即時串流：任務執行中的無人機狀態

> 給**外部控制端**用。2026-09-14 與使用者逐條定案（§11）。
> 狀態：**規格草案，尚未實作**。

外部控制端自己產生一組 UUID 當**任務編號**，用它連上地面站的 WebSocket，再帶著同一組編號呼叫起飛。
**從起飛那一刻起每 0.5 秒**收到任務裡每一台機的狀態，外加預計航線、已飛過的實際軌跡與重要事件；
**最後一台上鎖 3 秒後**任務自動結束、收到 `ended`、連線關閉。
控制端拿這些資料在自己的 OSM 地圖上畫出無人機的即時情況。

---

## 1. 定位

| | |
|---|---|
| 誰連誰 | **外部控制端**連到**地面站** backend `:38000`。起飛指令仍在 command `:38001` |
| 方向 | **只讀**。串流不收指令 |
| 契約 | 有版本（路徑帶 `v1`、每則訊息帶 `v`）。**與畫面用的 `/ws/telemetry` 分開**——那一條是內部欄位原樣倒出，畫面改一次外部就壞一次 |
| 時間 | 一律**地面站時鐘**（UTC、ISO 8601）。機上 Pi 已與地面站對時（2026-09-14 實測差 5.6 ms） |
| 座標 | WGS84。GeoJSON 照規範是 **[經度, 緯度, 高度]**；`state` 裡的位置用具名欄位 `lat`／`lon`，不會搞反 |
| 認證 | **沒有**（§10） |

---

## 2. 串流綁「任務」

### 2.1 系統裡的三個名詞

| | 路徑（plan） | 任務（mission） | 架次（session） |
|---|---|---|---|
| 是什麼 | 一份航線：航點、高度、速度 | 一件要做完的事 | 一台機從解鎖到上鎖 |
| 誰建立 | 人在規劃頁畫、或匯入 `.plan` | **人宣告**（我們的畫面在起飛時問；外部控制端在呼叫起飛時帶編號） | **系統自動**：解鎖就有、上鎖就結束 |
| 範圍 | 可以飛很多次 | 可以有**多台機**、**多個架次** | 一台機、一顆電池 |
| 關聯 | — | 一台機同一時間只能在一個任務裡 | 解鎖時自動掛到這台機進行中的任務 |

**串流的鍵是任務**，不另外發明概念：任務在起飛前就存在，一個任務可以涵蓋群飛的每一台機，
而每台機的架次會自動掛上去。一條連線看的就是「這個任務底下所有機的所有架次」。

### 2.2 流程

```
控制端                                         地面站
  │ 1. 產生 UUID（任務編號）
  │ 2. 連 ws://<地面站>:38000/ws/v1/missions/<UUID> ──▶ 回 hello（phase: waiting）
  │                                                    之後每 0.5 秒送一則 waiting 保持連線
  │ 3. POST <地面站>:38001/api/start ─────────────────▶ 用這個 UUID 建立任務，開始起飛流程
  │      {"plan_id": "…", "mission_id": "<UUID>"}        ↓
  │ ◀──────────────────────────────── route（預計航線）、每 0.5 秒 state、event
  │                                                    （地面待命、上傳、解鎖、爬升都看得到）
  │ ◀──────────────────────────────── 最後一台上鎖 3 秒後：自動結束任務、送 ended、關閉連線
```

* **先連再起飛**：`/api/start` 要等飛機爬到起飛高度、切進任務模式才回應。先連上，
  上傳、解鎖、起飛那一段才看得到。
* **不帶 `mission_id` 也可以**：地面站自己產生，放在回應的 `stream` 裡——但那時飛機已經在天上了。
* **群飛**：`POST :38001/api/command/group/{group_id}/execute`，同樣帶 `{"mission_id": "<UUID>"}`，
  一條連線涵蓋群組裡的所有機。
* **看從我們畫面啟動的任務**：`GET :38000/api/missions/active` 拿到任務編號，連同一個網址。
  這種任務由操作員在畫面上結束（§2.5）。

### 2.3 `/api/start` 的參數

| 欄位 | 必填 | 說明 |
|---|---|---|
| `plan_id` | 與 `plan` 二選一 | **路徑**：路徑庫的 id 或名稱 |
| `plan` | 與 `plan_id` 二選一 | `missions/` 目錄裡的 `.plan` 檔名（會先存進路徑庫再飛） |
| `mission_id` | 選填 | **任務**編號（UUID）。不給就由地面站產生 |
| `mission_name` | 選填 | 任務名稱，不可與既有任務同名。不給時用「`<路徑名> <MM-DD HH:MM>`」 |
| `sysid` | 選填 | 飛哪一台（不給＝主機） |
| `takeoff_alt` | 選填 | 起飛高度 |
| ~~`mission`~~ | 舊名 | **就是 `plan_id`**（2026-09-08 改名前的叫法，那時「任務」指的是路徑）。暫時保留相容，下一版移除 |

成功的回應多一個 `stream`：

```json
{"source": "db", "plan_id": "…", "name": "0914-square-test-v5", "sysid": 1, "ok": true,
 "steps": {…},
 "stream": {"mission_id": "8f0c…", "url": "ws://<地面站>:38000/ws/v1/missions/8f0c…"}}
```

會被擋下的情況：

| HTTP | 原因 |
|---|---|
| `422` | `mission_id` 不是 UUID |
| `409` | `mission_id` 對到的任務已經結束 |
| `409` | 這台機已經在**另一個**進行中的任務裡（一台機同一時間只能在一個任務） |
| `409` | `mission_name` 與既有任務同名 |

`mission_id` 對到一個**進行中**的任務時，這次起飛就掛到那個任務底下（例如群飛裡補飛一台）。

### 2.4 階段（`phase`）

| phase | 什麼時候 |
|---|---|
| `waiting` | 控制端已經用這組 UUID 連上，但還沒有人用它呼叫起飛 |
| `starting` | 起飛流程進行中（上傳、解鎖、起飛），還沒有任何一台離地 |
| `active` | 至少一台解鎖中 |
| `ended` | 見 §2.5。送出 `ended` 後連線關閉 |

`waiting` 最多等 **10 分鐘**，逾時送 `ended`（`reason: "never_started"`）並關閉。

### 2.5 結束

**用 `/api/start`（或群飛執行）建立的任務**：任務裡每一台都已上鎖，**最後一台上鎖 3 秒後**，
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

### 3.1 `hello`：連上時第一則

```json
{"v": 1, "type": "hello", "mission_id": "…", "seq": 1840, "ts": "…",
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

* 點的 `kind`：`takeoff`／`waypoint`／`land`。
* 第三個座標是**離起飛點高度**（公尺）。

### 3.3 `track`：已經飛過的實際軌跡

**連上或重連時送一次**（在 `route` 之後），內容是這個任務從開始到現在每台機實際飛過的路徑。
之後控制端用每則 `state` 的位置接著往下畫。群飛時每台機各一則。

```json
{"v": 1, "type": "track", "mission_id": "…", "seq": 1842, "ts": "…",
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
   "drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu",
   "freshness": "live", "age_s": 0.3, "connected": true,
   "position": {"lat": 24.773540, "lon": 121.045880, "alt_rel": 12.3, "alt_msl": 135.6},
   "last_known": null,
   "heading": 87.0, "ground_speed": 2.0, "vertical_speed": 0.1,
   "flight_mode": "AUTO", "mode_verb": "mission",
   "armed": true, "landed": "in_air",
   "mission_progress": {"current": 3, "total": 8, "state": "active"},
   "battery": {"pct": 76, "voltage": 15.9},
   "gps": {"fix": 3, "sats": 22},
   "link": {"sinr": 18.0, "rsrp": -100, "rtt_ms": 27, "state": "ok", "age_s": 0.4}}]}
```

| 欄位 | 單位／值域 | 說明 |
|---|---|---|
| `freshness` | `live`／`stale`／`old`／`never` | 見 §6 |
| `age_s` | 秒 | 距離最後一次收到這台機的遙測 |
| `position.alt_rel` | 公尺 | **離起飛點** |
| `position.alt_msl` | 公尺 | 海拔 |
| `heading` | 度，0～360 | 機頭朝向，以北為 0、順時針。飛控沒給時 `null` |
| `ground_speed` | m/s | |
| `vertical_speed` | m/s | **向上為正** |
| `flight_mode` | 原廠模式名 | ArduPilot：`AUTO`、`LOITER`、`RTL`、`LAND`、`STABILIZE`… |
| `mode_verb` | `mission`／`hold`／`rtl`／`land`／`position`／`guided`／`null` | **廠牌無關**的動作。手動類模式（STABILIZE 等）是 `null`。判斷用這個，顯示用 `flight_mode` |
| `landed` | `on_ground`／`takeoff`／`in_air`／`landing`／`null` | 飛控的著地狀態 |
| `mission_progress.current` | 航點序號 | **跑完後維持在最後一項**。飛控跑完 Land 會立刻回報「第 1 項」，那不是任務重來，這裡不照送 |
| `mission_progress.state` | `not_started`／`active`／`paused`／`complete`／`null` | 飛控上那份航線的執行狀態 |
| `gps.fix` | 0～6 | 3＝3D，≥4＝差分／RTK |
| `link.state` | `ok`／`degraded`／`stale`／`lost`／`unknown` | 5G 鏈路。`link.age_s` 超過 5 秒是 `stale`、超過 30 秒是 `lost` |

`waiting` 時送的是 `{"type": "state", "phase": "waiting", "drones": []}`——只為了讓控制端知道連線還活著（§4）。

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
| `vehicle_text` | 飛控的文字訊息，**只轉警告以上**；同一句 30 秒內重複的折成一則（`detail.count`）——實測真機每分鐘會重複送同一句預檢失敗，不過濾會把其他事件淹掉 |

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
* `ended` 送出後緩衝再保留 60 秒——在最後幾秒斷線的控制端，重連還拿得到 `ended`。
* 更早的完整資料不走串流：用 `GET :38000/api/sessions/{session_id}/export`。

---

## 6. 過期資料

**與本系統自己的畫面同一套規則**（`apps/frontend/lib/staleness.ts`）。理由：2026-08-26 畫面顯示
`armed=true / LAND / 高度 1.07 m`，那是**兩個半小時前**的殘影——一個照常顯示的舊數字比沒有數字更危險。

| `freshness` | `age_s` | 送什麼 |
|---|---|---|
| `live` | < 2 | 全部照送 |
| `stale` | 2～10 | 全部照送，由 `freshness` 標明。控制端應該把那台機畫淡 |
| `old` | > 10 | `position`、`heading`、速度、`armed`、`landed`、`flight_mode` 改成 `null`；**最後已知位置放在 `last_known`**：`{"lat", "lon", "alt_rel", "at"}` |
| `never` | 從未收到 | 除身分外全部 `null` |

`old` 時把位置拿掉而不是只標記，是為了**讓人讀不到那個數字**——一個標著「舊」的座標仍然會被畫成「飛機在這裡」。

§4 的「連線斷了」與這裡的「資料舊了」是兩件事：連線好好的，飛機的遙測也可能斷了（`old`）。

---

## 7. 連線與流量

* **一個任務可以同時有多個控制端**連上。
* 每個連線**各自排隊**：送不出去時丟掉最舊的 `state`（`route`、`track`、`event`、`ended` 不丟），
  下一則 `state` 帶 `"dropped": N`。**一個慢的控制端不准拖慢其他人**——包括本系統自己的畫面
  （現有的 `/ws/telemetry` 是一個送完才送下一個，實作時不能沿用）。
* 流量：每台機的 `state` 約 0.7 KB，一秒兩則；`track` 一分鐘的飛行約 3 KB。
* 關閉碼：

| code | 意思 |
|---|---|
| `1000` | 正常結束（`ended` 之後） |
| `4400` | 網址裡的編號不是 UUID |
| `4410` | 任務已結束，補送緩衝也過期了 |
| `1011` | 伺服器錯誤 |

---

## 8. 最小的控制端（Leaflet）

```js
const missionId = crypto.randomUUID();
let lastSeq = null, ws = null, watchdog = null;
const markers = {}, tracks = {};

function connect() {
  const q = lastSeq == null ? "" : `?after_seq=${lastSeq}`;
  ws = new WebSocket(`ws://GS:38000/ws/v1/missions/${missionId}${q}`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    lastSeq = m.seq;
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
await fetch("http://GS:38001/api/start", {method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({plan_id: "0914-square-test-v5", mission_id: missionId})});
```

---

## 9. 實際軌跡從哪裡來

地面站在飛機**解鎖期間**每秒存一筆位置（`telemetry` 表，不設保留期限）。`track` 就是把這個任務底下
各架次的那些點依時間串起來，遙測斷超過 10 秒的地方切段。即時 `state` 的位置與它是同一個來源。

---

## 10. 明確不做

* **沒有認證**（2026-09-14 定案）。任何連得到地面站的人都能連上看任何一個任務的即時狀態，
  也能用 `/api/start` 指揮飛機（[mission-api.md](mission-api.md) §4 同一件事）。**安全靠網段隔離**——
  這件事要寫在這裡，不能靠「大家都知道」。
* **串流不收指令**。暫停、返航、改航線照舊走 command 服務。
* **不送圍欄**。控制端只需要預計航線與實際軌跡。
* 不送影像、不送原始 MAVLink、不送 IMU／驅動診斷這類內部欄位。
* 逐則訊息只補送 60 秒；軌跡以外更早的資料走匯出。

---

## 11. 定案紀錄（2026-09-14）

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

---

## 12. 實作要動的地方

* **command**
  * `/api/start`：新參數 `plan_id`／`mission_id`／`mission_name`，舊的 `mission` 當 `plan_id` 的別名；
    以 `mission_id` 建立（或掛到進行中的）任務，並把這台機加進任務；回傳 `stream`。
  * `group/{id}/execute`：同樣接受 `mission_id`。
  * 起飛流程每一步的結果、失敗、全撤通知 backend。
* **backend**
  * `missions` 加一欄標記「外部建立」，**只有這種任務會自動結束**。畫面上落地時問「任務結束了嗎？」
    的提示，碰到已經自動結束的任務不要再問。
  * `/ws/v1/missions/{uuid}`：未知的 UUID 進 `waiting`；每連線一個送出佇列、0.5 秒的節拍、
    60 秒環形緩衝。backend 重啟時補送緩衝會不見——重連時用 `hello.replay.gap` 誠實說出缺了一段。
  * `route`：由路徑的航點與起飛點組（與規劃頁同一份資料）。`track`：由 `telemetry` 表組，斷 10 秒切段。
  * `event`：接在既有事件寫入的地方轉出；**`Crash: Disarming` 目前沒有任何地方特別處理**，要新接。
  * 結束判定：架次結束（`disarmed`／`telemetry_lost`）＋墜機文字＋最後一台上鎖後 3 秒（期間又解鎖則取消）。
  * 任務進度：跑完後飛控跳回第 1 項的那一下不照送。
* **驗收**：用 SITL 跑一次單機、一次群飛；先連再起飛看得到解鎖與爬升；中途切斷控制端，
  分別在 60 秒內與超過 60 秒重連，看補送與 `track`；拔掉遙測看 `old`／`telemetry_lost`；
  上鎖後 3 秒內再解鎖，任務不應結束。
