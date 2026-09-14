# 對外即時串流：任務執行中的無人機狀態

> 給**外部控制端**用。2026-09-14 使用者定案：**1A、2C、3E、4G、5J**（見 §9）。
> 狀態：**規格草案，尚未實作**。

外部控制端呼叫起飛時拿到一條 WebSocket；連上後**每 0.5 秒**收到這次執行裡每一台機的狀態，
開始時收到航線，重要變化另外收到事件；**最後一台上鎖 10 秒後**收到 `ended`，連線關閉。
控制端拿這些資料在自己的 OSM 地圖上畫出無人機的即時情況。

---

## 1. 定位

| | |
|---|---|
| 位置 | backend `:38000`（即時狀態在那裡）。指令仍在 command `:38001` |
| 方向 | **只讀**。串流不收指令 |
| 契約 | 有版本（路徑帶 `v1`、每則訊息帶 `v`）。**與畫面用的 `/ws/telemetry` 分開**——那一條是內部欄位原樣倒出，畫面改一次外部就壞一次 |
| 時間 | 一律**地面站時鐘**（UTC、ISO 8601）。機上 Pi 已與地面站對時（2026-09-14 實測差 5.6 ms） |
| 座標 | WGS84。GeoJSON 照規範是 **[經度, 緯度]**；`state` 裡的位置用具名欄位 `lat`／`lon`，不會搞反 |
| 認證 | **沒有**（§8） |

---

## 2. 一次執行（run）

**run＝「這一次按下起飛」**：一台機（單機起飛）或一組機（群飛），各自的航線，從起飛呼叫到全部結束。

為什麼不直接用系統裡既有的東西當串流的鍵：

* **航線（plan）**是路徑本身，可以重飛很多次。
* **任務（mission）**是「要達成的那件事」，會跨很多趟。
* **架次（session）**是一台機從解鎖到上鎖——**要解鎖之後才存在**，而上傳、解鎖、起飛那一段控制端也要看得到；群飛時更是一台一個。

run 把「這一次」接起來，並記下它最後對到哪幾個架次（`ended` 裡會列出 `session_id`，事後可以拿去查回放或匯出）。

### 2.1 拿到串流

**單機**：`POST :38001/api/start`，成功的回應多一個 `stream`：

```json
{"source": "db", "plan_id": "…", "name": "0914-square-test-v5", "sysid": 1, "ok": true,
 "steps": {…},
 "stream": {"run_id": "8f0c…", "url": "ws://<地面站>:38000/ws/v1/runs/8f0c…"}}
```

⚠ **`/api/start` 會等到飛機爬到起飛高度、切進任務模式才回應**（它是一整串：上傳→解鎖→起飛→切任務）。
要從上傳那一刻就看，**控制端自己產生 `run_id`（UUID），先連上串流，再帶著它呼叫**：

```
1. 產生 run_id
2. 連 ws://<地面站>:38000/ws/v1/runs/<run_id>      ← 還沒開始時收到 phase: "waiting"
3. POST :38001/api/start  {"mission": "…", "run_id": "<run_id>"}
```

**群飛**：`POST :38001/api/command/group/{group_id}/execute`（202，立即回）多一個同樣的 `stream`。
**一條連線涵蓋群組裡的所有機**。同樣接受控制端帶 `run_id`。

### 2.2 階段（`phase`）

| phase | 什麼時候 |
|---|---|
| `waiting` | 連上了，但這個 `run_id` 還沒有人拿去起飛 |
| `starting` | 起飛流程進行中（上傳、解鎖、起飛），還沒有任何一台離地 |
| `active` | 至少一台解鎖中 |
| `ended` | 見 §2.3。送出 `ended` 後連線關閉 |

`waiting` 最多等 **10 分鐘**，逾時送 `ended`（`reason: "never_started"`）。

### 2.3 結束

**正常結束**：run 裡每一台都已上鎖，**最後一台上鎖 10 秒後**送 `ended`，伺服器以 `1000` 關閉連線。

每一台各自帶結束原因：

| `reason` | 意思 | 依據 |
|---|---|---|
| `landed` | 著地後上鎖 | `landed_state` 先到 `on_ground`，接著上鎖 |
| `crash` | **飛控判定墜機而切斷馬達** | 飛控送出 `Crash: Disarming …`。2026-09-14 11:56 那一趟就是這個，而架次列表記的是普通的「上鎖」——**控制端必須分得出落地與摔機** |
| `disarmed_in_air` | 上鎖時沒有著地訊號 | 例如飛控其他保護機制上鎖 |
| `telemetry_lost` | 解鎖中**連續 90 秒**沒有遙測，地面站收掉這一趟 | 與 backend 的 `SESSION_LOST_S` 同一個值。**這不代表飛行結束**，只代表資料在那裡斷了 |
| `start_failed` | 起飛流程被拒（上傳、解鎖、起飛任一步） | command 回傳的失敗步驟 |
| `aborted` | 群飛全撤 | `group/{id}/abort` |

**不會結束的情況**（照實送，不自作主張）：

* **著地了但一直沒上鎖**（例如手飛落地、油門沒拉到底）：繼續送，該機 `landed: "on_ground"`、`armed: true`。
* **航線最後沒有降落項**（例如從 QGC 匯入的）：飛機會在最後一點懸停，串流一直開著，直到有人讓它降落。
  本系統規劃頁產生的航線一定帶降落項。
* **控制端斷線**：不影響飛行，也不影響 run 何時結束。

**自動上鎖是實測過的**（這台 ArduCopter 4.7，2026-09-14 的五次降落）：任務的降落項或 LAND 模式降落，
著地後 **0.4～0.8 秒**上鎖；解鎖後沒起飛、油門在最低，10 秒上鎖（`DISARM_DELAY=10`）。
**沒有樣本、未驗證**：LOITER／STABILIZE 手飛直接落地（依 ArduPilot 規則是油門最低後 10 秒）、RTL 降落。

---

## 3. 訊息

全部是 JSON 文字訊息。每一則都有：

```json
{"v": 1, "type": "…", "run_id": "…", "seq": 1842, "ts": "2026-09-14T07:30:12.500Z"}
```

`seq` 在同一個 run 內**所有型別共用、嚴格遞增**，補送就靠它（§4）。

### 3.1 `hello`：連上時第一則

```json
{"v": 1, "type": "hello", "run_id": "…", "seq": 1840, "ts": "…",
 "phase": "active", "started_at": "2026-09-14T07:29:01.000Z",
 "drones": [{"drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1}],
 "replay": {"from_seq": 1700, "gap": null}}
```

### 3.2 `route`：航線

連上時送一次（在 `hello` 之後）；**飛行中改航線時再送一次**（`reason: "change_route"`）。

```json
{"v": 1, "type": "route", "run_id": "…", "seq": 1841, "ts": "…", "reason": "initial",
 "drone_id": "1d2f…", "plan_id": "…", "plan_name": "0914-square-test-v5",
 "home": {"lat": 24.773548, "lon": 121.045883, "alt_msl": 129.0},
 "geojson": {"type": "FeatureCollection", "features": [
   {"type": "Feature", "geometry": {"type": "LineString",
      "coordinates": [[121.045883, 24.773548, 0], [121.045911, 24.773812, 3.0], …]},
    "properties": {"role": "planned_path", "alt": "alt_rel"}},
   {"type": "Feature", "geometry": {"type": "Point", "coordinates": [121.045911, 24.773812, 3.0]},
    "properties": {"role": "waypoint", "seq": 3, "kind": "waypoint"}},
   …]},
 "fence": {"geojson": {…}, "alt_max": 30, "enforced_by_fc": false}}
```

* 點的 `kind`：`takeoff`／`waypoint`／`land`。第三個座標是**離起飛點高度**（公尺）。
* `fence` 是規劃端的圍欄，**飛控不照它擋**（`enforced_by_fc: false`）。沒有圍欄時是 `null`。
* 群飛時每台機各一則 `route`。

### 3.3 `state`：每 0.5 秒

**固定每 0.5 秒送一次，沒有變化也送**——控制端據此知道連線還活著（超過 3 秒沒收到任何訊息就當成斷線）。

```json
{"v": 1, "type": "state", "run_id": "…", "seq": 1842, "ts": "2026-09-14T07:30:12.500Z",
 "phase": "active",
 "drones": [{
   "drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu",
   "freshness": "live", "age_s": 0.3, "connected": true,
   "position": {"lat": 24.773540, "lon": 121.045880, "alt_rel": 12.3, "alt_msl": 135.6},
   "last_known": null,
   "heading": 87.0, "ground_speed": 2.0, "vertical_speed": 0.1,
   "flight_mode": "AUTO", "mode_verb": "mission",
   "armed": true, "landed": "in_air",
   "mission": {"current": 3, "total": 8, "state": "active"},
   "battery": {"pct": 76, "voltage": 15.9},
   "gps": {"fix": 3, "sats": 22},
   "link": {"sinr": 18.0, "rsrp": -100, "rtt_ms": 27, "state": "ok", "age_s": 0.4}}]}
```

| 欄位 | 單位／值域 | 說明 |
|---|---|---|
| `freshness` | `live`／`stale`／`old`／`never` | 見 §5 |
| `age_s` | 秒 | 距離最後一次收到這台機的遙測 |
| `position.alt_rel` | 公尺 | **離起飛點** |
| `position.alt_msl` | 公尺 | 海拔 |
| `heading` | 度，0～360 | 機頭朝向，以北為 0、順時針。飛控沒給時 `null` |
| `ground_speed` | m/s | |
| `vertical_speed` | m/s | **向上為正** |
| `flight_mode` | 原廠模式名 | ArduPilot：`AUTO`、`LOITER`、`RTL`、`LAND`、`STABILIZE`… |
| `mode_verb` | `mission`／`hold`／`rtl`／`land`／`position`／`guided`／`null` | **廠牌無關**的動作。手動類模式（STABILIZE 等）是 `null`。判斷用這個，顯示用 `flight_mode` |
| `landed` | `on_ground`／`takeoff`／`in_air`／`landing`／`null` | 飛控的著地狀態 |
| `mission.current` | 航點序號 | **任務跑完後維持在最後一項**。飛控跑完 Land 會立刻回報「第 1 項」，那不是任務重來，這裡不照送 |
| `mission.state` | `not_started`／`active`／`paused`／`complete`／`null` | |
| `gps.fix` | 0～6 | 3＝3D，≥4＝差分／RTK |
| `link.state` | `ok`／`degraded`／`stale`／`lost`／`unknown` | 5G 鏈路。`link.age_s` 超過 5 秒是 `stale`、超過 30 秒是 `lost` |

### 3.4 `event`：重要變化

```json
{"v": 1, "type": "event", "run_id": "…", "seq": 1850, "ts": "…",
 "drone_id": "1d2f…", "kind": "waypoint_reached", "severity": "info",
 "text": "到達第 4 項（共 8 項）", "detail": {"seq": 4, "total": 8}}
```

| `kind` | 什麼時候 |
|---|---|
| `start_step` | 起飛流程每一步的結果（上傳、解鎖、起飛、切任務），`detail.ok` |
| `armed`／`disarmed` | 解鎖／上鎖。`disarmed` 帶 `detail.reason`（同 §2.3） |
| `takeoff`／`landed` | 著地狀態轉到 `in_air`／`on_ground` |
| `mode_change` | 模式變了，`detail.from`／`detail.to`（原廠名與 verb 都帶） |
| `waypoint_reached` | 到達航點 |
| `mission_state` | 任務狀態變了 |
| `route_changed` | 飛行中改航線（接著會送新的 `route`） |
| `link_degraded`／`link_lost`／`link_recovered` | 5G 鏈路 |
| `telemetry_lost`／`telemetry_resumed` | 這台機的遙測斷了 10 秒以上／恢復 |
| `failsafe` | 飛控回報進入 CRITICAL／EMERGENCY 狀態 |
| `crash` | 飛控送出 `Crash: Disarming` |
| `vehicle_text` | 飛控的文字訊息，**只轉 warning 以上**；同一句 30 秒內重複的折成一則（`detail.count`）——實測真機每分鐘會噴同一句預檢失敗 |

`severity`：`info`／`warning`／`critical`。

### 3.5 `ended`

```json
{"v": 1, "type": "ended", "run_id": "…", "seq": 2201, "ts": "…",
 "drones": [{"drone_id": "1d2f…", "reason": "landed", "session_id": "…",
             "landed_at": "2026-09-14T04:02:32.060Z", "disarmed_at": "2026-09-14T04:02:32.570Z"}]}
```

之後伺服器以 `1000` 關閉。

---

## 4. 斷線重連與補送（4G）

* 伺服器為每個 run 保留**最近 60 秒**的訊息。
* 重連時帶上最後收到的序號：

  ```
  ws://<地面站>:38000/ws/v1/runs/<run_id>?after_seq=1842
  ```

* 伺服器依序送：`hello` → 當下的 `route` → **序號大於 1842 的緩衝訊息**（每則帶 `"replay": true`）→ 接回即時。
* 漏掉的比緩衝還舊時，`hello.replay.gap` 說明缺了哪一段：`{"from_seq": 1843, "to_seq": 1990}`。**不會假裝沒缺**。
* `ended` 送出後緩衝再保留 60 秒——在最後幾秒斷線的控制端，重連還拿得到 `ended`。
* 更早的歷史不走串流：用 `GET :38000/api/sessions/{session_id}/export`（完整 JSON）。

---

## 5. 過期資料（3E）

**與本系統自己的畫面同一套規則**（`apps/frontend/lib/staleness.ts`）。理由：2026-08-26 畫面顯示
`armed=true / LAND / 高度 1.07 m`，那是**兩個半小時前**的殘影——一個照常顯示的舊數字比沒有數字更危險。

| `freshness` | `age_s` | 送什麼 |
|---|---|---|
| `live` | < 2 | 全部照送 |
| `stale` | 2～10 | 全部照送，由 `freshness` 標明。控制端應該把那台機畫淡 |
| `old` | > 10 | `position`、`heading`、速度、`armed`、`landed`、`flight_mode` 改成 `null`；**最後已知位置放在 `last_known`**：`{"lat", "lon", "alt_rel", "at"}` |
| `never` | 從未收到 | 除身分外全部 `null` |

`old` 時把位置拿掉而不是只標記，是為了**讓人讀不到那個數字**——一個標著「舊」的座標仍然會被畫成「飛機在這裡」。

---

## 6. 連線與流量

* **一個 run 可以同時有多個控制端**連上。
* 每個連線**各自排隊**：送不出去時丟掉最舊的 `state`（`route`、`event`、`ended` 不丟），
  下一則 `state` 帶 `"dropped": N`。**一個慢的控制端不准拖慢其他人**——包括本系統自己的畫面
  （現有的 `/ws/telemetry` 是一個送完才送下一個，實作時不能沿用）。
* 流量：每台機的 `state` 約 0.7 KB，一秒兩則。
* 關閉碼：

| code | 意思 |
|---|---|
| `1000` | 正常結束（`ended` 之後） |
| `4404` | 沒有這個 `run_id`（而且不是剛產生、等待中的） |
| `4410` | run 已結束，補送緩衝也過期了 |
| `1011` | 伺服器錯誤 |

---

## 7. 最小的控制端（Leaflet）

```js
const runId = crypto.randomUUID();
let lastSeq = null, markers = {};

function connect() {
  const q = lastSeq == null ? "" : `?after_seq=${lastSeq}`;
  const ws = new WebSocket(`ws://GS:38000/ws/v1/runs/${runId}${q}`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    lastSeq = m.seq;
    if (m.type === "route") L.geoJSON(m.geojson).addTo(map);     // GeoJSON 是 [經度, 緯度]
    if (m.type === "state") for (const d of m.drones) {
      const p = d.position ?? d.last_known;                       // old 時位置在 last_known
      if (!p) continue;
      markers[d.drone_id] ??= L.marker([p.lat, p.lon]).addTo(map); // Leaflet 是 [緯度, 經度]
      markers[d.drone_id].setLatLng([p.lat, p.lon])
        .setOpacity(d.freshness === "live" ? 1 : 0.4);
    }
    if (m.type === "ended") ws.onclose = null;
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

connect();
await fetch("http://GS:38001/api/start", {method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({mission: "0914-square-test-v5", run_id: runId})});
```

---

## 8. 明確不做

* **沒有認證**（5J，2026-09-14 定案）。任何連得到地面站的人都能連上看任何一次執行的即時狀態，
  也能用 `/api/start` 指揮飛機（[mission-api.md](mission-api.md) §4 同一件事）。**安全靠網段隔離**——
  這件事要寫在這裡，不能靠「大家都知道」。
* **串流不收指令**。暫停、返航、改航線照舊走 command 服務。
* 不送影像、不送原始 MAVLink、不送 IMU／驅動診斷這類內部欄位。
* 補送只保留 60 秒；更早的走匯出。

---

## 9. 定案紀錄（2026-09-14）

| # | 題目 | 定案 |
|---|---|---|
| 1 | 串流何時開始、結束 | **A**：起飛呼叫時取得（或控制端先帶 `run_id` 連上）；最後一台上鎖 10 秒後 `ended`。前提「降落後會自動上鎖」先用真機資料確認過（§2.3） |
| 2 | 多機 | **C**：一條連線涵蓋整個 run 的所有機 |
| 3 | 過期資料 | **E**：與畫面同一套規則，超過 10 秒位置改 `null`＋`last_known` |
| 4 | 斷線重連 | **G**：序號＋補送最近 60 秒 |
| 5 | 認證 | **J**：不做 |

---

## 10. 實作要動的地方

* **command**：`/api/start` 與 `group/{id}/execute` 接受選填的 `run_id`、回傳 `stream`；
  run 開始、每一步結果、失敗、全撤通知 backend。
* **backend**
  * run 登記：**寫進資料庫**（run ↔ 機 ↔ 航線 ↔ 架次）。backend 重啟時補送緩衝會不見，
    但 run 本身不能不見——重連時要能用 `hello.replay.gap` 誠實說出缺了一段。
  * `/ws/v1/runs/{run_id}`：每連線一個送出佇列、0.5 秒的節拍、60 秒環形緩衝。
  * `route`：由航線的航點與起飛點組（與規劃頁同一份資料）。
  * `event`：接在既有事件寫入的地方轉出；**`Crash: Disarming` 目前沒有任何地方特別處理**，要新接。
  * 結束判定：架次結束（`disarmed`／`telemetry_lost`）＋墜機文字＋最後一台上鎖後 10 秒。
  * 任務進度：跑完後飛控跳回第 1 項的那一下不照送。
* **驗收**：用 SITL 跑一次單機、一次群飛；中途切斷控制端再重連看補送；拔掉遙測看 `old`／`telemetry_lost`。
