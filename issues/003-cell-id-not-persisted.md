# 003 · `cell_id` 有產生但沒寫進 `link_metrics`

- 狀態：closed（改判：不是 bug，是 schema 語意不明）
- 嚴重度：low
- 位置：`backend/app/db.py:87-100`
- 建立：2026-08-03
- 關閉：2026-08-03

## 現象

`link_metrics.cell_id` 欄位永遠是 NULL，儘管模擬器每次取樣都算出了服務 cell。

**2026-08-03 首次實飛已確認。** 同一次取樣，WebSocket 推送有值、資料庫卻是空的：

```
WebSocket → link.cell_id = 1
DB        → SELECT cell_id FROM link_metrics ORDER BY time DESC LIMIT 1;  →  (null)
```

## 原因

`link_sim.sample()` 回傳值含 `"cell_id": best["id"]`（`link_sim.py:68`），但
`db.insert_link()` 的 INSERT 欄位清單漏了它：

```sql
INSERT INTO link_metrics (time, drone_id, session_id, lat, lon, alt_rel,
  rsrp, rsrq, sinr, cqi, pci, band, nr_mode, ...)
--                            ^^^ pci 之後直接跳到 band，沒有 cell_id
```

參數個數是對的（$1–$19），所以不會報錯，只是靜靜地少存一欄。

## 影響

回放與分析時無法直接 join `cells` 取得基地台名稱／座標，只能靠 `pci` 反查。
目前 seed 只有兩個 cell、PCI 唯一，還不痛；cell 變多或真機階段 PCI 會重複時
就會變成問題。

## 修法建議（原始判斷，已被推翻）

欄位清單補 `cell_id`、參數補 `m.get("cell_id")`，佔位符順延到 $20。
`cells.id` 是 SERIAL（int），`link_metrics.cell_id` 是 BIGINT，型別相容。

## 改判：不補，反而移除模擬器的 `cell_id` 輸出

確認研究範圍後，這題的答案反了。真實 5G 裡 modem 回報兩種 cell 識別：

| 欄位 | 意義 | 唯一性 |
|---|---|---|
| `pci` | Physical Cell ID，0–1007 | **會重複使用**，僅鄰區內唯一 |
| `cell_id` | NCI / CGI，全域 cell 識別碼 | 全域唯一 |

`cell_id` 的真正用途是**消除 PCI 重複**。但模擬器塞進去的是我們自己 `cells`
表的流水號 PK，那是完全不同的東西；而且既然網通架構不歸本專案負責、
我們不擁有 gNB 佈建資訊，`cells` 表在真機階段本來就會退化成參考資料。

把自家表的 PK 寫進一個語意為「modem 回報的全域識別碼」的欄位，
等於記錄一個虛構的事實——與「記錄事實」的原則相違。
**目前 NULL 的行為其實是對的**，錯的是模擬器產生了不該存在的值，
以及 schema 註解沒把語意講清楚。

## 解決方式

1. `link_sim.py` 的輸出移除 `cell_id`（模擬器產生不出有意義的 NCI）。
2. `db/init/01_schema.sql` 註解改為「modem 回報的全域 cell 識別碼 (NCI/CGI)，
   用來消除 PCI 重複。模擬資料為 NULL」，`pci` 註解補上「會重複使用」。
3. `doc/data-schema.md` 同步。
4. `db.insert_link()` **不動**——它原本就沒寫這欄，行為正確。

模擬階段用 `pci` 就足以識別服務 cell（seed 的兩個 gNB PCI 為 101/205）。
代價是模擬資料不能用 `cell_id` join `cells` 表，但用 `pci` join 一樣做得到。

順帶確認：`insert_link` 也沒有寫 `raw` 欄位，那是刻意保留給真機 modem 的原始
回應，現階段留空正確。
