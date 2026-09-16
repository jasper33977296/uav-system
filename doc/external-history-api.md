# 對外任務歷史：比較兩趟或多趟的訊號

> 給**外部控制端**用。2026-09-16 定案（§9）。
> 狀態：**已實作（2026-09-16）**，見 §10。即時那一半見 [`external-live-api.md`](external-live-api.md)
> （串流與輪詢兩種傳法）。

控制端要回答的是「**這次比上次好還是差**」：同一條路徑飛了兩趟以上，比較沿途的訊號。
所以這裡給的是**一個任務的完整訊號樣本**，外加每一筆「沿預計航線走了多遠、偏離多少」
——有了它，兩趟才有共同的 X 軸（§4）。

---

## 1. 定位

| | |
|---|---|
| 誰連誰 | 外部控制端呼叫地面站 backend `:38000` |
| 方向 | **唯讀**。不動飛機、不改資料 |
| 版本 | 路徑帶 `v1`，**版本號緊接在服務根之後**（`/api/v1/…`，與即時那半同一條規則）。先上線時用的 `/api/ext/v1/…` 保留為別名。欄位名與即時的 `state.link` **逐字相同** |
| 時間 | 地面站時鐘（UTC、ISO 8601） |
| 座標 | WGS84，具名欄位 `lat`／`lon` |
| 認證 | **沒有**（與其他對外端點同，靠網段隔離） |
| 資料量 | 訊號每秒一筆。實測一個任務數十到數百筆，**一次回完，不分頁、不降採樣** |

---

## 2. 兩支端點

### 2.1 有哪些任務

```
GET http://<地面站>:38000/api/v1/ext/missions?since=2026-09-01&limit=50
```

| 參數 | 說明 |
|---|---|
| `since`／`until` | ISO 時間或日期，比對任務第一趟的起飛時間 |
| `plan_id` | 只列飛過這份路徑的任務（要比同一條路徑時用它挑） |
| `external` | `true`＝只列外部控制端建立的；省略＝**全部都列**（含畫面上建立的） |
| `drone_id` | 只列這台機參與過的 |
| `limit` | 預設 50，最大 200 |

```json
{"missions": [{
  "mission_id": "8f0c2d1e-…",
  "name": "0914-square-test-v5 09-14 12:01",
  "external": true,                      // 由外部控制端用 /api/v1/start 建立
  "started_at": "2026-09-14T04:01:31.000Z",   // 第一趟解鎖
  "ended_at": "2026-09-14T04:02:35.570Z",     // null＝還在進行中
  "drones": [{"drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1}],
  "plans": [{"plan_id": "e961b301-…", "name": "0914-square-test-v5"}],
  "sessions": 1,                         // 架次數（一台機從解鎖到上鎖算一趟）
  "samples": 58                          // 訊號樣本數，0＝那次沒有量到訊號
}]}
```

**要比較的兩個任務通常用 `plan_id` 挑**：同一份路徑飛的兩趟，里程才對得起來（§4）。

### 2.2 一個任務的完整訊號

```
GET http://<地面站>:38000/api/v1/ext/missions/{mission_id}/signal
```

**一次一個任務**（2026-09-16 定案）：要比幾個就呼叫幾次，各自快取、各自失敗，
不必為了一個編號打錯而整包重來。

```json
{
  "mission": {"mission_id": "8f0c…", "name": "…", "external": true,
              "started_at": "…", "ended_at": "…"},
  "method": {"max_offset_m": 60.0, "sample_interval_s": 1},
  "drones": [{
    "drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1,
    "sessions": [{
      "session_id": "…",
      "plan_id": "e961b301-…", "plan_name": "0914-square-test-v5",
      "started_at": "2026-09-14T04:01:31.000Z",
      "ended_at": "2026-09-14T04:02:35.570Z",
      "end_reason": "disarmed",          // disarmed＝看到上鎖；telemetry_lost＝資料斷了
      "reference": "plan",               // 里程的基準：plan／null（見 §4）
      "route": {"type": "FeatureCollection", "features": [ … ]},   // 與串流的 route 同格式
      "gaps": [{"from": "…", "to": "…", "seconds": 23.4}],         // 這段沒有資料（§5）
      "samples": [{
        "time": "2026-09-14T04:01:37.120Z",
        "lat": 24.773540, "lon": 121.045880, "alt_rel": 12.3,
        "along_m": 41.2, "offset_m": 3.7,
        "rsrp": -100.0, "rsrq": -11.0, "sinr": 18.0, "cqi": null,
        "pci": 133, "cell_id": 2179073, "band": "n79", "nr_mode": "SA",
        "rtt_ms": 27.0, "jitter_ms": null,
        "packet_loss_pct": null,
        "throughput_up_kbps": null, "throughput_down_kbps": null
      }]
    }]
  }]
}
```

`404`＝沒有這個任務；任務存在但沒有任何架次時 `drones` 是空的，**不是錯誤**
（任務建立了、還沒飛）。

---

## 3. 樣本欄位

| 欄位 | 單位 | 說明 |
|---|---|---|
| `time` | ISO 8601 | 這筆量測的採樣時刻 |
| `lat`／`lon` | 十進位度 | 量到這筆訊號時機在哪裡 |
| `alt_rel` | m | 離起飛點高度 |
| `along_m` | m | **沿預計航線走了多遠**（§4）。`null`＝算不出來 |
| `offset_m` | m | 離預計航線多遠。`null`＝沒有參考路徑 |
| `rsrp` | dBm | 參考訊號接收功率 |
| `rsrq` | dB | 參考訊號接收品質 |
| `sinr` | dB | 訊號干擾雜訊比 |
| `cqi` | 0–15 | 通道品質指示 |
| `pci` | — | Physical Cell ID（十進位） |
| `cell_id` | — | 全域 cell 識別碼（NCI/CGI） |
| `band` | — | 頻段，如 `n79` |
| `nr_mode` | — | `SA`／`NSA`／`LTE` |
| `rtt_ms` | ms | 來回時間 |
| `jitter_ms` | ms | 抖動 |
| `packet_loss_pct` | % | 封包遺失率 |
| `throughput_up_kbps`／`throughput_down_kbps` | kbps | 上行／下行吞吐 |

**量不到的欄位是 `null`，不補假值**——欄位名與即時串流 `state.link` 逐字相同，
即時看到的與事後拿到的是同一個東西。

---

## 4. 沿航線里程：兩趟怎麼對齊

**時間對不齊。** 兩趟的速度不同，用時間當 X 軸會把「同一個地點」錯位。所以每一筆樣本
都投影到**那一趟自己的預計航線**上，給出走了多遠（`along_m`）與偏離多少（`offset_m`）；
控制端拿 `along_m` 當共同的 X 軸，兩趟的同一段路就對得起來。

演算法與畫面上的「沿路徑對照」**是同一份**（`app/chainage.py`，前端 `lib/chainage.ts` 的後端版）。

三件必須講清楚的事：

* **基準是計畫航點，不是任一趟的實飛軌跡。** 拿其中一趟當基準，那一趟的偏航就變成零誤差，
  比較失去意義。所以**這支端點不替控制端選基準**：那一趟沒有綁路徑時
  `reference` 是 `null`、`along_m` 全部是 `null`，不退回用軌跡。
* **偏離超過 `max_offset_m`（預設 60 m）的樣本，`along_m` 給 `null`**，`offset_m` 照給。
  硬把一個偏航 200 m 的樣本塞進某個里程，數字看起來完全正常，而且沒有任何線索說它是垃圾。
* **`along_m` 只在同一份路徑之間可比。** 兩個任務飛的是不同路徑時，里程不是同一條軸——
  回應裡每一趟都帶 `plan_id`，比較前先確認它們相同。

---

## 5. 沒有資料的那幾段

`gaps` 是這一趟**超過 10 秒沒有任何資料**的區間（地面站的失明記錄）。

**它與「訊號差」是兩件事**：訊號差是量到的值難看，失明是那段根本沒有量測送回來。
畫成圖時這幾段要留白，不要把兩端連成一條線——那會讓中斷看起來像一段平穩的飛行。

---

## 6. 拿得到與拿不到的

* **只有掛在任務底下的架次拿得到。** 用 `/api/v1/start` 帶 `mission_id` 起飛的一定有；
  更早以前的飛行多半沒有掛任務，要先在畫面的資訊頁補歸。
* **進行中的任務也給**（2026-09-16 定案）：資料到目前為止，`ended_at` 是 `null`。
  要即時看請用串流或輪詢（[`external-live-api.md`](external-live-api.md)），這支是事後比較用的。
* **原始資料不設保留期限**，所以舊任務照樣查得到。

---

## 7. 明確不做

* **不做 CSV、不做降採樣、不分頁**（2026-09-16 定案）。資料量小，一次回完最單純；
  日後真的變大再談，而那時要先量過，不是先猜。
* **不做伺服器端的比較結論**（勝負、改善多少）。地面站給的是樣本與共同的 X 軸；
  **怎麼比是控制端的事**——我方不替它定義什麼叫「比較好」。
* **不含飛行遙測**（姿態、速度、電量）。要那些請用架次匯出
  `GET :38000/api/v1/sessions/{session_id}/export`。
* **沒有認證。** 與其他對外端點同一條：任何連得到地面站的人都拿得到這些資料。

---

## 8. 控制端的最小做法

```js
const GS = "10.141.2.21";
// ① 挑出飛同一份路徑的任務
const { missions } = await (await fetch(
  `http://${GS}:38000/api/v1/ext/missions?plan_id=${planId}&limit=10`)).json();

// ② 各自抓完整訊號（一次一個）
const runs = [];
for (const m of missions.slice(0, 3)) {
  const d = await (await fetch(
    `http://${GS}:38000/api/v1/ext/missions/${m.mission_id}/signal`)).json();
  runs.push({ name: m.name, sessions: d.drones.flatMap((x) => x.sessions) });
}

// ③ 用 along_m 當共同 X 軸；算不出里程的樣本跳過
const series = runs.map((r) => ({
  name: r.name,
  points: r.sessions.flatMap((s) => s.samples)
    .filter((p) => p.along_m != null)
    .map((p) => ({ x: p.along_m, y: p.sinr })),
}));
```

---

## 9. 定案紀錄（2026-09-16）

| # | 題目 | 定案 |
|---|---|---|
| 1 | 列哪些任務 | **全部**，附 `external` 標記，可用 `external=true` 篩 |
| 2 | 多個任務怎麼拿 | **一次一個**，控制端要比幾個就呼叫幾次 |
| 3 | 比較由誰算 | **控制端算**；地面站每筆多給 `along_m`／`offset_m`，省掉幾何計算 |
| 4 | 還沒結束的任務 | **也給**，資料到目前為止 |
| 5 | 格式 | **只給 JSON** |

---

## 10. 實作（2026-09-16）

| 地方 | 做了什麼 |
|---|---|
| `apps/backend/app/chainage.py` | 投影拆出 `projector()`：`(lat, lon) → (里程, 偏離)`。畫面上的沿路徑對照改用同一份，**同一套投影只留一份** |
| `apps/backend/app/ext_history.py` | 兩支端點。預計航線用 `ext_stream.route_geojson`、訊號欄位用 `ext_stream.LINK_KEYS`——即時與事後同一份 |
| `apps/backend/app/main.py` | 掛上路由；`_api_version` middleware 讓 `/api/v1/ext/…` 與先上線的 `/api/ext/v1/…` 走同一支 |

統計（架次數、樣本數、第一趟起飛時間）是查詢時算的，沒有新增欄位。

**驗過的**（`scripts/test-ext-history.py`，打正在跑的 backend，只讀既有資料＋一組跑完就刪的臨時資料）：
清單的架次數與樣本數與資料庫一致、樣本欄位就是 §3 那一組、綁路徑的架次 `reference` 是 `plan` 且
87/87 筆算得出里程、把 `max_offset_m` 縮到 0.01 時里程全部變 `null` 而偏離照給、
沒有綁路徑的架次 `reference` 是 `null` 且不退回用軌跡、`plan_id`／`drone_id`／`external`／時間窗四種篩選、
不合法編號回 422、找不到回 404。投影重構另外比對過既有的沿路徑對照，行為沒變。

**還沒做**：舊架次的補歸要在畫面上做（§6）；沒有補歸的飛行不會出現在任務歷史裡。
