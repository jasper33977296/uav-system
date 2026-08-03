# 006 · `battery_pct` 多乘了 100，實際值是 10000

- 狀態：open
- 嚴重度：medium
- 位置：`backend/app/ingest.py:51`
- 建立：2026-08-03

## 現象

WebSocket 推送與 `telemetry` 資料表裡的 `battery_pct` 都是 `10000.0`，
側欄「電量」顯示 `10000 %`。

實測（2026-08-03 首次飛行）：

```sql
SELECT max(battery_pct) FROM telemetry WHERE session_id IS NOT NULL;
-- 10000
```

## 原因

```python
live.battery_pct = b.remaining_percent * 100.0
```

MAVSDK 舊版的 `remaining_percent` 是 0.0–1.0 的比例，需要乘 100。
但本專案裝的是 **mavsdk 3.17.2**，該欄位已改成直接給百分比（0–100），
再乘 100 就變成 10000。

## 影響

- 側欄電量數字錯誤（顯示 10000%）。
- `low_battery` 事件（`events` 表的 type 之一，目前還沒實作發送邏輯）
  之後若用百分比門檻判斷，會永遠不觸發。

## 修法建議

拿掉乘法：

```python
live.battery_pct = b.remaining_percent
```

因為這個欄位的語意在 MAVSDK 版本間變過，建議加一行防呆註解或做範圍判斷
（`if pct <= 1.0: pct *= 100`），避免之後升降版又踩到。
