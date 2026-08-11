# 事件流設計稿：納入無人機 vehicle log（STATUSTEXT／PX4 Events）

- 狀態：**供實作**（UI/UX 2026-08-11；使用者指示參考 QGC 做法，PM 派案）
- 關聯：issue 018（人話化，同波）、doc/design-tokens.md（severity 用色）、
  後端資料面進行中（source='vehicle'、MAV_SEVERITY、重複計數、arming 逐項）
- 範圍：事件流卡（即時頁為主，回放頁事件標記沿用 severity 對應）

## 1. 來源維度（篩選 chips）

事件分三個來源，卡片標題列加篩選 chips`[全部] [訊號] [無人機] [地面站]`，
預設全部；chip 樣式同站（selected=surface-2，非 accent）：

| 來源 | 內容 | 現有 type |
|---|---|---|
| 訊號 | 鏈路品質與網路 | link 劣化/瀕斷/恢復、cell_change |
| 無人機 | 機體自身狀態與 log | mode、failsafe、arming、STATUSTEXT、PX4 Events |
| 地面站 | GCS 側操作與服務 | 指令執行、架次切分、服務連線 |

篩選狀態不記憶（篩選是臨時查詢動作，非工作區配置）。

## 2. Severity 視覺分級（MAV_SEVERITY 0–7 → tokens）

| MAV_SEVERITY | 呈現 |
|---|---|
| 0 EMERGENCY / 1 ALERT / 2 CRITICAL | `--status-danger-bg` 整列底＋danger 圓點＋severity 文字標籤（沿用現有 critical 慣例）；**同時觸發頂部 toast**（見 §4） |
| 3 ERROR | danger 圓點＋標籤，無整列底 |
| 4 WARNING | `--status-warn` 圓點＋標籤 |
| 5 NOTICE / 6 INFO | `--muted` 圓點，常規密度 |
| 7 DEBUG | **預設不顯示**——卡片底部「顯示 DEBUG（N）」摺疊列，展開才見（教學文判準：不記憶） |

顏色永不單獨傳達：severity 以文字標籤（CRITICAL/WARNING…）伴隨圓點。
STATUSTEXT／Events 的訊息**原文呈現不翻譯**（忠實原則——那是機上韌體
的話，翻譯反而引入失真；018 人話模板只管我們自產的結構化事件）。

## 3. 重複摺疊

沿用既有「×N」慣例：後端帶重複計數的直接顯示；無計數的由前端摺疊
「連續同來源同文字」。摺疊列展開可看逐條時間戳。

## 4. 嚴重訊息 toast（QGC 對應）

severity ≤ 2 時，即時頁頂部出**橫幅式 toast**（非遮擋地圖中央、
不擋任務控制面板）：danger 底＋原文＋時間，10s 自動收合，
點擊跳到事件流該列。安全資訊不漸進揭露原則的延伸——事件流在側欄
可能被捲走，嚴重訊息必須主動浮出。同時最多一條（新的頂掉舊的，
被頂掉的仍在事件流）。

## 5. Arming check 逐項原因（除錯主場景）

就緒徽章（未就緒時）點擊展開，合併呈現兩層資訊：

```
● 未就緒                        ← 現有 not_ready_reasons（持續狀態）
· EKF 未收斂
· GPS 衛星不足
上次解鎖嘗試（13:42:07）被拒：   ← 最近一次 arming 失敗事件（時點快照）
· Preflight: GPS horizontal accuracy too low
```

兩層語意不同：not_ready_reasons 是「現在為什麼不行」、arming 事件是
「上次嘗試時機上實際說了什麼」——除錯時後者常帶韌體端的具體數值，
是 QGC 使用者習慣的資訊。無 arming 失敗紀錄時第二段不出現。

## 6. 音量控制（vehicle log 流量大的防淹沒）

- DEBUG 預設隱藏（§2）；INFO/NOTICE 不觸發任何醒目樣式。
- 事件流卡維持現有容量上限；來源篩選讓「只看訊號」的研究情境不被
  機上雜訊淹沒。
- 回放頁事件標記（三角）只畫 severity ≤ 4（WARNING 以上）＋訊號事件，
  避免時間軸被 INFO 淹沒。

## 驗收

SITL 飛行：STATUSTEXT 各 severity 呈現正確、arming 失敗展開兩層原因、
toast 觸發與自動收合、來源篩選、DEBUG 摺疊、×N 沿用。
