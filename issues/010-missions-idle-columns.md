# 010 · missions 表的 drone_id 與 status 欄位閒置

- 狀態：open
- 嚴重度：low
- 位置：`db/init/01_schema.sql`（missions 表）
- 建立：2026-08-04

## 現象

`missions.drone_id`（任務綁定機台）與 `missions.status`
（draft/uploaded/running/completed/aborted 生命週期）自 schema 初版設計後
從未被任何程式讀寫。

## 原因

初版 schema 設想本系統負責任務的完整生命週期。後續範圍定案
（doc/qgc-integration.md）：任務上傳與執行由 QGC 負責，本系統只讀——
因此我們永遠不會知道 uploaded/running 這些狀態的轉換時機，
欄位失去了資料來源。

`drone_id` 同理：路徑庫的路徑不綁定機台（任何機都能飛同一條），
綁定關係實際上記在 `flight_sessions.mission_id`（哪台機哪次飛了哪條）。

## 影響

無功能影響。風險是誤導：未來的人看到欄位可能以為有東西在維護它。

## 處理

決定（2026-08-04）：記錄在案即可，暫不動 schema。
下次需要動 missions 表時一併移除這兩欄；在那之前以本 issue 為準——
**這兩欄沒有資料來源，讀到的永遠是預設值**。
