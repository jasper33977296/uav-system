# 004 · 未 armed 時仍持續 1Hz 入庫，資料無限成長

- 狀態：closed
- 嚴重度：medium
- 位置：`backend/app/main.py:31-61`
- 建立：2026-08-03
- 關閉：2026-08-03

## 現象

只要 backend 連上 MAVLink 且拿到過座標，`_link_and_db_loop` 就每秒寫一筆
`telemetry` + 一筆 `link_metrics`，不管無人機有沒有起飛。SITL 或真機整天掛著
待命時，這些 `session_id` 為 NULL 的列會一直累積（1 Hz × 2 表 ≈ 每天 17 萬列）。

`handover` 事件同理，未 armed 時也會寫進 `events`（`session_id` NULL）。

## 原因

迴圈的守門條件只有「有沒有座標」：

```python
if live.lat is None or live.lon is None:
    continue
```

架次邊界（armed/disarmed）只用來開關 `flight_sessions`，沒有用來 gate 寫入。

## 影響

- 資料表被大量待命資料稀釋，`SELECT ... WHERE session_id IS NOT NULL` 變成每個
  分析查詢的必要條件，容易漏寫。
- hypertable 長期成長，沒有 retention policy 收斂。

## QGC 讓這題變急迫（2026-08-03 補充）

確定實務上用 QGroundControl 做感測器校正後，這題的優先序要提高：
校正（加速度計六面、磁力計旋轉、水平、遙控器）動輒數十分鐘，全程 disarmed
且飛機就放在起飛點不動。依現行邏輯這段時間會以 1Hz 持續寫入兩張表，
產生大量「同一個座標、同一個 SINR」的無意義資料——比單純待機更糟，
因為每次出勤前都會發生。

見 [doc/qgc-integration.md](../doc/qgc-integration.md) 的階段 1。

## 待決定

三個方向，取捨不同：

1. **只在有 session 時入庫**（`if live.session_id is None: continue`）。最乾淨，
   但會失去「起飛前的地面基準值」——而地面靜止時的 SINR 基準對干擾研究其實有用。
2. **照寫，加 TimescaleDB retention policy**，例如 `session_id IS NULL` 的資料
   只留 7 天。保留基準值又能收斂，但要多維護一條 policy。
3. **待命時降頻**（例如 0.1 Hz），資料量降一個數量級，仍保有基準值。

傾向 3 或 2。決定後同步更新 `doc/data-schema.md` 的「取樣頻率策略」表。

## 決定：方案 1（只在 armed 且有 session 時入庫）

理由是「上鎖狀態下飛機不會移動，沒有記錄的必要」。地面基準值的疑慮不成立——
靜止不動的資料就是同一個座標重複上萬筆，不構成有意義的對照組。

實測支持這個判斷：修正前累積的 11,007 筆待機資料裡，
**只有 2 個不同的緯度、1 個經度**，SINR 平均 11.5、標準差 1.05
（純粹是模擬器的高斯雜訊）。佔全部資料的 96.6%，內容是
「無人機停在起飛點沒動、訊號正常」重複一萬次。

## 解決方式

`_link_and_db_loop` 的守門條件從「有沒有座標」改成「armed 且有 session」：

```python
m = source.sample(live.lat, live.lon, live.alt_rel)
live.link = m          # live state 一律更新（前端待機時仍看得到鏈路品質）

if not (live.armed and live.session_id):
    recording = False
    continue
```

兩個設計細節：

1. **取樣照做、只擋入庫。** 前端待機時仍需要看到即時鏈路品質，
   所以 `live.link` 一律更新，擋的只有 `insert_telemetry` / `insert_link`
   與鏈路事件。
2. **同時檢查 `session_id` 而非只看 `armed`。** 兩者由不同協程設定，
   剛解鎖的瞬間可能 armed 已 True 但 session 還沒建好，此時寫入會產生孤兒資料。

架次開始時重置 `link_state = "ok"`，避免沿用上一趟結束時的鏈路狀態。

## 順帶修正：`_armed()` 的賦值順序競態

實作 gate 時發現的。`_armed()` 與 `_link_and_db_loop` 是同一個 event loop 內的
協程，會在 await 點交錯執行，而原本的賦值順序有兩個窗口：

- **上鎖時**：`await db.end_session(sid)` 執行期間，`live.armed` 仍是 True、
  `live.session_id` 仍是舊值 → 迴圈可能把資料寫進**已經結算完摘要的架次**。
- 解鎖時原本的順序（先建 session 再設 armed）恰好是對的，但沒有寫明理由，
  容易在後續修改時被破壞。

改成先清狀態再關 session，並在 docstring 說明順序為何是刻意的。

DB 裡原有 2 筆孤兒 `mode_change`（皆為 `HOLD → TAKEOFF`）就是這類競態的產物。
`mode_change` 本身維持不 gate——起飛前切模式是有意義的操作紀錄，
事件總量僅個位數，`session_id` NULL 正確表達「不屬於任何架次」。

## 驗證

| 檢查 | 結果 |
|---|---|
| 上鎖靜置 25 秒 | telemetry / link_metrics 各成長 **0 筆**（修正前為 25 筆）|
| 迴圈確實跑到判斷式 | WebSocket 顯示 `lat=47.3977`、`link.sinr=9.1` 持續更新，是 gate 擋下而非提前 continue |
| 解鎖後 | 20 秒內寫入 17 筆，**全部有 session_id，0 筆孤兒** |
| 完整架次 | 154 筆 telemetry + 154 筆 link_metrics，孤兒 0 |
| 落地上鎖後靜置 20 秒 | 成長 **0 筆** |
| #001 回歸 | 鏈路事件序列完整：`ok → degraded → lost → degraded → ok` |
| 架次結算 | `ended_at` 有值，摘要正確（154 samples、54 在干擾區內）|

驗證期間前後都確認 backend 存活——第一次測試時 backend 已被背景任務回收，
「0 筆成長」是假陽性，重測才成立。
