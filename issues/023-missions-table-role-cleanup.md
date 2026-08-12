# 023 · missions 表正名與瘦身：它是「路徑快照庫」不是「任務庫」

- 狀態：**closed（2026-08-12 實作完成）**——死欄位 status／geometry／drone_id 已移除
  （移除前以資料驗證三者全為預設值／NULL）；新增 `kind`（imported/from-vehicle/generated）
  並自 created_by 回填，created_by 保留為歷史事實；`group_assignments.mission_id` 與
  `mission_groups.base_mission_id` 兩處外鍵補上 ON DELETE SET NULL（「飛過的路徑可以刪、
  飛行紀錄永存」現在真的成立，實測刪除後 assignment 列仍在、只是 mission_id 變 NULL）；
  新增 `flight_sessions.mission_name` 快照並回填。遷移前後各表筆數一致。
- 嚴重度：medium
- 位置：`db/init/01_schema.sql`（missions）、`apps/backend/app/db.py` migrate、`apps/backend/app/api.py`
- 建立：2026-08-12（PM 與使用者設計討論）

## 現象

`missions` 有四個欄位從建表至今**從未被寫入或讀取**（`status`、`drone_id`、
`geometry`；`drone_id` 唯一用途是刪無人機時清成 NULL），且一張表混了兩種
本質不同的列——匯入的路徑 vs 編隊地面展開的一次性路徑——後者曾灌爆任務
清單 UI，目前靠 `WHERE created_by IS DISTINCT FROM 'group-gen'` 在 API 層
掩蓋（issue 013-B 前端回報）。閒置欄位部分即長期未結的 [issues/010](010-missions-idle-columns.md)。

## 原因（角色與欄位不符）

討論釐清：**`.plan` 檔＝作者原稿**（在 QGC／使用者檔案系統，本系統管不到、
不保證還在或沒被改）；**`missions` 那一列＝匯入當下的不可變快照**
（系統實際上傳／飛的那條路徑）。已查證 missions **沒有任何編輯路徑**
（只有建立／啟用／刪除，無 PATCH、無 UPDATE waypoints）——所以它天生就是
快照，不需要另外在架次存副本。

它存在的三個理由都與「模板」無關：
1. 給穩定 id，讓架次能指向「這趟飛的是哪條」（MCP 的「照計畫飛了嗎」）；
2. 讓同一條路徑的多次飛行可被 group 起來比較（比較頁的地基）；
3. command 服務上傳與回讀比對需要具體航點。

而 `status`（draft/uploaded/running/completed/aborted）、`drone_id`、
`geometry` 是照「任務規劃工具」的模型設計的——但本系統**刻意不做規劃**
（規劃留給 QGC，見 doc/gcs-replacement.md 範圍定案），任務生命週期實際長在
`flight_sessions` 與 `group_assignments.phase` 上。欄位與真實角色不符，
才會有死欄位與「生成物污染任務庫」兩個症狀。

## 影響

資料模型誤導（新進者會以為系統有任務生命週期管理）；API 過濾是掩蓋而非
分類；MCP 目標層（區域→自動規劃路徑）落地後會產生更多一次性路徑，
症狀會加重。**現在做比之後做便宜。**

## 修法建議（使用者 2026-08-12 定案）

### 1. 砍死欄位（結掉 010）
- 移除 `missions.status`、`missions.drone_id`、`missions.geometry`。
- `is_active`：**先評估不急著砍**——它仍有活的消費者（`/missions/active`、
  即時頁預計路徑、`create_session` 的最後後備），多機化後語意才會真正退場。

### 2. 加 `kind` 判別欄，取代 `created_by` 兼差
- `kind`：`imported`（.plan 匯入）／`from-vehicle`（機上讀回）／
  `generated`（編隊展開）。回填自現有 `created_by`。
- 清單 API 改用 `kind` 過濾——從「掩蓋」變成正當分類查詢；
  `generated` 的列可跟著它的群組一起清理。

### 3. 刪除語意（使用者定案）
**飛過的任務可以刪**；代價是那些架次最多失去「對應到哪條 plan」，
**但飛行紀錄本身必須永遠存在**。所以：
- `flight_sessions.mission_id` 維持 `ON DELETE SET NULL`（架次列不受影響）——
  現行行為正確，不改。
- **要補的洞**：`group_assignments.mission_id REFERENCES missions(id)`
  **沒有 ON DELETE 政策**（NO ACTION）——刪一條被編隊引用的任務會 FK 違反
  而 500（separate 模式下 assignment 指向的正是使用者匯入的任務）。
  補成 `ON DELETE SET NULL`，讓「可以刪」真的刪得掉、不是丟 500。
- **建議（可選，成本低）**：架次留一份 `mission_name` 快照，刪除後歷史仍能
  說「飛的是 complex-survey（路徑已刪除）」而不是一片空白——更貼合
  「飛行紀錄要一直存在」的意圖。做不做由實作者評估。

schema 改動照慣例同批更新 `doc/data-schema.md`（並把「路徑快照庫」的角色
寫清楚，取代現行「任務庫」的說法）。

## 解決方式

（closed 時補）
