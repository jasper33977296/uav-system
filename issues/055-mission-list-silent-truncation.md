# 055 · 任務清單會無聲截斷

- 狀態：open
- 嚴重度：low
- 位置：`apps/backend/app/ext_history.py:21,93`
- 建立：2026-09-21

## 現象

`GET /api/v1/ext/missions` 只有 `limit`（預設 50、上限 `MAX_LIMIT = 200`），
回應是 `{"missions": [...]}`。**沒有 `total`、沒有 `has_more`、沒有 cursor。**

拿到 200 筆時，呼叫端無法判斷是剛好有 200 個任務，還是被切掉了。

## 原因

```python
ORDER BY coalesce(first.started_at, m.created_at) DESC
LIMIT {arg(max(1, min(limit, MAX_LIMIT)))}
```

`limit` 被夾到 200 之後就直接截斷，而回應沒有帶任何「還有更多」的訊號。

## 影響

**現在不會咬人**：資料庫裡總共 6 個任務，離 200 還很遠。這是潛在問題。

真正的毛病不是「會截斷」——資料多了本來就該截斷——而是**截斷了不說**。
控制端問「這條路徑飛過的全部任務」時，拿到的答案可能是錯的，
而且沒有任何線索。`doc/external-history-api.md` §7 寫的「不分頁」講的是
訊號樣本（一次回完），清單這邊其實也沒有分頁，但理由不同。

## 修法建議

**不做分頁，但要說實話。** 回應加兩個欄位：

```json
{"missions": [...], "total": 6, "has_more": false}
```

`total` 是套用同一組篩選條件、不套 `limit` 的 count（多一次查詢；
現在的量級成本可忽略）。`has_more` ＝ `total > len(missions)`。

文件寫明：**超過 `limit` 時用 `since`／`until` 開時間窗往回拿**，
而不是加 offset——時間窗對這份資料是天然的切法，而 offset 在新任務
一直進來時會漏。

真的需要 cursor 時再開回來，而那時要先量過資料量，不是先猜。
