# 010 · missions 表的 drone_id 與 status 欄位閒置

- 狀態：**closed（2026-08-12，隨 023 一併結掉）**（**已併入 [023](023-missions-table-role-cleanup.md)**：
  閒置欄位的根因是 missions 的角色與欄位不符，隨 023 的正名瘦身一併結掉）
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

## 設計論證：路徑為什麼不該綁定機台（2026-08-04 討論定案）

1. **路徑是地理工件**：waypoints 只有座標／高度／動作，沒有任何機台屬性。
   研究語意上它是「穿越干擾場域的實驗設計」，換機執行不改變其意義。
2. **執行關係已有正確的家**：`flight_sessions (drone_id, mission_id, started_at)`
   事實上就是 drones↔missions 的多對多關聯表，附帶時間維度。
   把 drone_id 放在 missions 是把「執行關係」誤植為「擁有關係」。
3. **綁定傷害研究**：同一路徑被不同機、不同時間重複飛＝可重複量測，
   是多機比對分析（同路徑不同機的 SINR 分布差異）的前提。
   綁死一台機會在資料模型層面封死這種比對。
4. **異質機隊的正確解法**：若未來混入定翼機等（部分路徑物理上飛不了），
   應加「能力需求」中繼資料（如 `vehicle_class`，描述什麼**類型**能飛），
   而非綁定單一機台。QGC .plan 本身帶 `vehicleType` 欄位可作匯入來源。
   目前機隊單一類型（RB5 多旋翼），到異質化那天再做。

## 影響

無功能影響。風險是誤導：未來的人看到欄位可能以為有東西在維護它。

## 處理

決定（2026-08-04）：記錄在案即可，暫不動 schema。
下次需要動 missions 表時一併移除這兩欄；在那之前以本 issue 為準——
**這兩欄沒有資料來源，讀到的永遠是預設值**。
