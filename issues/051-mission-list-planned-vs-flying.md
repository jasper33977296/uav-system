# 051 · 任務歷史清單把「建了沒飛」報成「進行中」

- 狀態：open
- 嚴重度：medium
- 位置：`apps/backend/app/ext_history.py:74,102`＋`doc/external-history-api.md` §2.1
- 建立：2026-09-21

## 現象

`GET /api/v1/ext/missions` 的文件說「`ended_at` 是 `null` ＝ 還在進行中」。
照這條規則讀，今天打實際的 API 拿到的是：

```
名稱                    started_at   ended_at              sessions
0914-7                  None         2026-09-14T04:05:34   0
第一次穩定完成任務        None         None                  0   ← 被判成「還在進行中」
```

**資料庫裡真正進行中的任務是 0 個**（有架次且未結束）。
所以唯一會被外部判成「進行中」的那一筆，其實是一個從來沒飛過的任務。

```sql
-- 沒架次且未結束（started_at 與 ended_at 都會是 null）：1
-- 進行中（有架次、未結束）：0
```

## 原因

清單的 `started_at` 是**第一趟解鎖時間**，直接取 `min(flight_sessions.started_at)`：

```python
LEFT JOIN LATERAL (SELECT min(started_at) AS started_at FROM flight_sessions x
                    WHERE x.mission_id = m.id) first ON true
...
"started_at": _iso(d["started_at"]), "ended_at": _iso(d["ended_at"]),
```

任務建了還沒飛時沒有任何架次，所以 `started_at` 是 `null`。
而 `missions.ended_at` 只有在任務**被結束**時才寫，沒飛過也沒人結束的任務兩個都是 `null`。

`0914-7` 是另一種形狀：建了、沒飛、但被結束了（`started_at` null、`ended_at` 有值）。
**兩種 null 的意思不一樣，而回應裡沒有任何欄位說得出差別。**

## 影響

* 外部控制端會把一個從沒飛過的任務畫成「正在飛」。這正是 036／049 那一族的毛病：
  **把「沒有資料」呈現成「有資料」**。
* 排序也受影響：`ORDER BY coalesce(first.started_at, m.created_at) DESC` 用建立時間
  頂替，所以沒飛過的任務會混在飛過的中間，而清單上看不出它憑什麼排在那裡。
* 文件寫的那條規則本身是錯的，照著寫的控制端一定會錯。

## 修法建議

**不要只改文件。** 要控制端從兩個可為 `null` 的時間戳推導狀態，等於把我方的內部
結構丟給對方去猜；`started_at` 是不是 null 這件事，本來就不該是外部要知道的事。

清單直接給一個狀態欄位：

| `state` | 意思 | 判準 |
|---|---|---|
| `planned` | 建了，還沒飛 | 沒有任何架次 |
| `flying` | 進行中 | 有架次且 `ended_at IS NULL` |
| `ended` | 結束了 | `ended_at IS NOT NULL` |

`started_at`／`ended_at` 照給（它們是時間，不是狀態），文件改成
「**看 `state`**，不要自己從時間戳推」。

取捨：多一個欄位，但少一條要在文件裡解釋、而且現在解釋錯了的規則。
