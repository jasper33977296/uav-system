# 006 · `battery_pct` 多乘了 100，實際值是 10000

- 狀態：closed
- 嚴重度：medium
- 關閉：2026-08-03
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

## 解決方式

拿掉乘法，`live.battery_pct = b.remaining_percent`。

**刻意不採用上面建議的 `if pct <= 1.0: pct *= 100` 自動判斷**——1% 是真實的
低電量值，把它誤判成 100% 正好發生在最需要正確的時候。改為在程式碼註記
版本差異，升降版時人工確認。

實證確認值域：修正前 DB 裡 `battery_pct` 的 min=5000、max=10000，
即 `remaining_percent` 回傳 50.0–100.0，確為 0–100 值域。

## 驗證

實飛一趟（157 筆遙測）：`battery_pct` 範圍 **55.0 – 100.0**，數值合理，
且隨飛行遞減。WebSocket 推送同步正確。
