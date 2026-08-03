# 007 · `heading` 從未訂閱，地圖機頭朝向永遠指北

- 狀態：closed
- 嚴重度：medium
- 關閉：2026-08-03
- 位置：`backend/app/ingest.py`（缺少 heading 訂閱）
- 建立：2026-08-03

## 現象

`telemetry.heading` 全部是 NULL，WebSocket 推送的 `heading` 也是 `null`。

實測（2026-08-03 首次飛行，124 筆遙測）：

```sql
SELECT count(heading) AS notnull, count(*) AS total FROM telemetry WHERE session_id IS NOT NULL;
--  notnull | total
--        0 |   124
```

## 原因

`ingest.py` 有 `_position` / `_velocity` / `_battery` / `_gps` / `_mode` / `_armed`
六個訂閱任務，**沒有任何一個填 `live.heading`**。
`LiveState.heading` 從初始值 `None` 開始就沒被寫過。

## 影響

`MapView.tsx` 的無人機 marker：

```ts
markerRef.current.setLngLat([t.lon, t.lat]).setRotation(t.heading ?? 0);
```

`?? 0` 讓機頭三角形永遠指向正北，與實際航向無關。
`doc/frontend.md` 明載「無人機即時位置（機頭朝向三角形）」，目前這個功能是壞的。

## 修法建議

在 `ingest.run()` 的 `asyncio.gather` 加一個訂閱：

```python
async def _heading(drone: System) -> None:
    async for h in drone.telemetry.heading():
        live.heading = h.heading_deg
```

替代來源是 `drone.telemetry.attitude_euler()` 的 `yaw_deg`（-180~180，需轉成 0~360）。
`telemetry.heading()` 語意較直接，優先用它。

順帶一提：`_velocity` 只算了地速大小，沒有保留 NED 分量。若之後分析需要航向與
速度向量的關係，可考慮把 `north_m_s` / `east_m_s` 存進 `raw` JSONB。

## 解決方式

`ingest.py` 新增 `_heading()` 訂閱並加進 `asyncio.gather`：

```python
async def _heading(drone: System) -> None:
    async for h in drone.telemetry.heading():
        live.heading = h.heading_deg
```

用 `telemetry.heading()`（0–360）而非 `attitude_euler().yaw_deg`（-180–180），
語意直接對應地圖上的機頭朝向，不需轉換。已確認 mavsdk 3.17.2 有此 API，
欄位名為 `heading_deg`。

## 驗證

實飛一趟：**157/157 筆都有值**（修正前 0/124），範圍 0–360。
航向確實隨飛行變化，不是固定值：

| 時間 | 航向 | 緯度 | 說明 |
|---|---|---|---|
| 20:01 | 93° | 47.39775 | 停機朝東 |
| 20:21 | 36° | 47.39776 | 起飛後轉向 |
| 20:41 | 0° | 47.39862 | 向北飛 |
| 21:01 | 360° | 47.39948 | 抵達干擾區 |
| 22:01 | 93° | 47.39775 | 落地回原朝向 |

註：0° 與 360° 同義，heading 在正北附近會在兩者間跳。前端 `setRotation()`
吃這兩個值的行為相同，不需另外處理。
