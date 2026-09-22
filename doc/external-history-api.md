# 對外任務歷史：比較兩趟或多趟的訊號

> 給**外部控制端**用。2026-09-16 定案（§9），2026-09-21 補五項修正（§10.1）。
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
| 資料量 | 訊號約每秒一筆（實際間隔每趟量出來，§5.1）。實測一個任務數十到數百筆，**一次回完，不分頁、不降採樣**；清單被 `limit` 切到時 `has_more` 會說 |

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
  "state": "ended",                      // planned／flying／ended——看這個，別自己推
  "started_at": "2026-09-14T04:01:31.000Z",   // 第一趟解鎖。null＝還沒飛過
  "ended_at": "2026-09-14T04:02:35.570Z",     // 任務被結束的時間
  "drones": [{"drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1}],
  "plans": [{"plan_id": "e961b301-…", "name": "0914-square-test-v5"}],
  "sessions": 1,                         // 架次數（一台機從解鎖到上鎖算一趟）
  "samples": 58                          // 訊號樣本數，0＝那次沒有量到訊號
}],
 "total": 6,                             // 套用同一組篩選、沒套 limit 的總數
 "has_more": false}                      // true＝被 limit 切掉了，還有更多
```

**任務現在是哪一態，看 `state`**：

| `state` | 意思 | 什麼時候 |
|---|---|---|
| `planned` | 建了，**還沒飛** | 一個架次都沒有 |
| `flying` | 進行中 | 有架次，而且任務還沒結束 |
| `ended` | 結束了 | `ended_at` 有值（沒飛過就被結束的也算） |

**不要自己從時間戳推**（2026-09-21，issues/051）。`started_at` 是「第一趟解鎖」，
任務建了還沒飛時是 `null`；而「`ended_at` 是 null ＝ 進行中」這條舊規則**是錯的**
——它會把從沒飛過的任務判成正在飛。`state` 把這件事算好了。

**要比較的兩個任務通常用 `plan_id` 挑**：同一份路徑飛的兩趟，里程才對得起來（§4）。

`limit` 切掉時 `has_more` 是 `true`，`total` 仍然是全部的數目——**截斷了會說**
（issues/055）。要再往回拿請用 `since`／`until` 開時間窗，沒有 offset：
新任務一直進來時 offset 會漏。

### 2.2 一個任務的完整訊號

```
GET http://<地面站>:38000/api/v1/ext/missions/{mission_id}/signal
```

**一次一個任務**（2026-09-16 定案）：要比幾個就呼叫幾次，各自快取、各自失敗，
不必為了一個編號打錯而整包重來。

```json
{
  "mission": {"mission_id": "8f0c…", "name": "…", "external": true,
              "state": "ended", "started_at": "…", "ended_at": "…"},
  "method": {"max_offset_m": 60.0, "sample_gap_factor": 5.0},
  "drones": [{
    "drone_id": "1d2f…", "name": "pi5-sdmodelh7v2-ardu", "sysid": 1,
    "sessions": [{
      "session_id": "…",
      "plan_id": "e961b301-…", "plan_name": "0914-square-test-v5",
      "started_at": "2026-09-14T04:01:31.000Z",
      "ended_at": "2026-09-14T04:02:35.570Z",
      "end_reason": "disarmed",          // disarmed＝看到上鎖；telemetry_lost＝資料斷了
      "reference": "plan",               // 里程的基準：plan／null（見 §4）
      "sample_interval_s": 1.02,         // 這一趟量到的取樣間隔（§5）。null＝樣本不足兩筆
      "route": {"type": "FeatureCollection", "features": [ … ]},   // 與串流的 route 同格式
      "gaps": [{"from": "…", "to": "…", "seconds": 23.4}],         // 遙測失明（§5）
      "sample_gaps": [{"from": "…", "to": "…", "seconds": 22.0}],  // 沒有訊號樣本（§5）
      "samples": [{
        "time": "2026-09-14T04:01:37.120Z",
        "lat": 24.773540, "lon": 121.045880, "alt_rel": 12.3,
        "phase": "route", "along_m": 41.2, "offset_m": 3.7,
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
| `phase` | | **`route`＝在航線上；`transit`＝飛往任務起始點的那一段**（§4.1）。里程只對 `route` 有意義 |
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
* **偏離超過 `max_offset_m`（60 m）的樣本，`along_m` 給 `null`**，`offset_m` 照給。

### 4.1 `phase`：飛往起始點的那一段不在航線上

一趟任務不是從航線第一個點開始的。機起飛（或在空中接到指令）之後，會**先飛到
航線的起始點**，那一段用 GUIDED 直線飛過去，**不在航線上**。

那一段算任務的一部分——它是真的飛行，耗了電、可能失敗、也可能正好飛過你想量的
區域，所以不會從資料裡消失。但**沿航線里程對它沒有定義**，所以它的 `along_m`
是 `null`。

**沒有 `phase` 的話，`along_m: null` 會同時代表兩件完全不同的事**：

| | 意思 | 你該怎麼辦 |
|---|---|---|
| `phase: "transit"` | 航線還沒開始 | 畫訊號圖時排除；要看「飛去的路上訊號如何」時單獨拿 |
| `phase: "route"` 且 `along_m: null` | 在航線期間，但偏離超過 60 m | **要看一眼**——可能是 GPS 跳點，也可能真的偏航了 |

比較兩趟時，**用 `phase == "route"` 過濾**：起飛位置不同會讓 transit 長度差很多，
混進來就沒得比。
  硬把一個偏航 200 m 的樣本塞進某個里程，數字看起來完全正常，而且沒有任何線索說它是垃圾。
  這個門檻**不給外部調**（2026-09-21，issues/054）：它是「偏離多遠就不該再談里程」的
  方法判斷，不是查詢條件——可調的話同一趟資料在不同呼叫下會給出不同的 `along_m`。
  用了哪個值照樣寫在 `method.max_offset_m` 裡。
* **`along_m` 只在同一份路徑之間可比。** 兩個任務飛的是不同路徑時，里程不是同一條軸——
  回應裡每一趟都帶 `plan_id`，比較前先確認它們相同。

---

## 5. 取樣間隔，與兩種「沒有資料」

### 5.1 取樣間隔是量出來的

每一趟帶 `sample_interval_s`：那一趟**相鄰樣本時間差的中位數**。

**它是量到的，不是設定值**（2026-09-21，issues/052）。取樣率由機上代理的
`--modem-interval` 決定，地面站沒有管道知道它設成多少——而 2026-09-07 實測過
設定 1.0 s、實際 2.61 s（`doc/onboard-telemetry.md`）。所以這裡不回報設定，只回報量到的。
樣本少於兩筆時是 `null`：一筆樣本說不出間隔。

### 5.2 `gaps` 與 `sample_gaps` 是兩件事

| 欄位 | 是什麼 | 怎麼來的 |
|---|---|---|
| `gaps` | **遙測失明**：那段地面站看不到飛機 | 超過 10 秒沒有遙測 |
| `sample_gaps` | **沒有訊號樣本**：那段沒有量測 | 相鄰樣本差超過 `sample_interval_s × method.sample_gap_factor`（5 倍） |

**畫訊號圖要留白的是 `sample_gaps`**，不要把兩端連成一條線——那會讓中斷看起來像一段
平穩的飛行。`gaps` 另外標成「失聯」：那是飛安資訊，不是畫圖用的。

**兩者不會互相取代**（2026-09-21，issues/053）。真機上訊號樣本走機上代理的
`/batch`、每 10 秒一批而且**允許補傳**，與遙測是兩條路：

* 遙測斷了、樣本補齊了 → `gaps` 有一段而 `sample_gaps` 沒有。**那段資料是好的。**
* 數據機掛了、遙測照常 → `sample_gaps` 有一段而 `gaps` 沒有。**那段要留白。**

算不出取樣間隔時 `sample_gaps` 是 `null`，不是 `[]`——**空陣列的意思是「沒有缺口」**，
而那時我們其實是「不知道」。

**訊號差與沒有資料也是兩件事**：訊號差是量到的值難看，缺口是那段根本沒有量測。

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
  **但截斷了會說**：清單帶 `total` 與 `has_more`（issues/055）——不能分頁是一回事，
  無聲少給是另一回事。
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
const { missions, has_more } = await (await fetch(
  `http://${GS}:38000/api/v1/ext/missions?plan_id=${planId}&limit=10`)).json();
if (has_more) console.warn("還有更多，用 since／until 開時間窗往回拿");

// ② 各自抓完整訊號（一次一個）
const runs = [];
for (const m of missions.filter((m) => m.state !== "planned").slice(0, 3)) {
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
  // ④ 沒有樣本的那幾段要留白，不要把兩端連成一條線（§5.2）
  blanks: r.sessions.flatMap((s) => s.sample_gaps ?? []),
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

### 10.1 2026-09-21：五項修正（issues/051–055）

上線後逐條對過回應與實作，五個地方在說我們其實不知道的事，或該說而沒說：

| # | 問題 | 改法 |
|---|---|---|
| 051 | 清單把**建了沒飛**的任務報成「進行中」——實查當時唯一被判成進行中的任務從來沒飛過 | 清單與 `mission` 都帶 `state`（`planned`／`flying`／`ended`）。外部不必再從兩個可為 null 的時間戳推 |
| 052 | `sample_interval_s` 是**寫死的常數 1**，而取樣率在機上的旗標裡 | 改成**每一趟量出來**的中位數，擺在該趟底下；`method` 只留真的是方法的東西 |
| 053 | `gaps` 是**遙測失明**，文件卻叫控制端拿它畫訊號圖的留白 | `gaps` 維持原義，新增 `sample_gaps`（樣本缺口）。§5.2 講清楚兩者的差別與各自的用途 |
| 054 | `max_offset_m` 被外部調得動（FastAPI 自動 query 的副作用） | 拿掉參數，鎖成常數；測試改從內部呼叫 `chainage.projector()` 驗門檻 |
| 055 | 清單**截斷了不說** | 加 `total` 與 `has_more` |

**驗過的**（`scripts/test-ext-history.py`，多了 9 項）：每個任務的 `state` 與
（架次數, `ended_at`）一致、沒飛過又沒結束的是 `planned`、`limit` 切掉時 `has_more`
是 true 而 `total` 仍是全部、帶 `max_offset_m` 的回應與不帶時完全相同；
臨時架次刻意用 2 秒取樣並挖一個 22 秒的洞——**間隔量得出 2.0（不是 1）**、
`sample_gaps` 指得出那個洞而 `gaps` 仍是空的、缺口兩端就是洞兩側的樣本；
只剩一筆樣本時間隔與 `sample_gaps` 都是 `null`（不是空陣列）。
