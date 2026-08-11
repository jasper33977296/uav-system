# 019 · 架次未綁任務：新飛的資料在比較頁整個用不了

- 狀態：open（2026-08-11 已診斷；PM 排程於「任務層資料模型」批次、多機驗收之後——**現在不動工**）
- 嚴重度：high（打斷使用者主研究流程：比較頁「同任務前後比較」）
- 位置：`apps/backend/app/db.py:create_session`、`apps/command/app/main.py`、
  `scripts/fly-mission.py`
- 建立：2026-08-11

## 現象

今天所有架次（含 13:03 complex-survey 18 分鐘完整任務飛行）在 /drones 架次表
「任務」欄都是「—」，compare 頁任務下拉每個任務掛 0 架次；只有 8/5 舊架次有
關聯。compare 頁的「同一任務前後比較」對新飛的資料整個用不了。

## 根因（兩個獨立問題疊加）

1. **架次未綁任務**：架次由 backend 在 `armed` 轉換時開
   （`mavlink_rx.py:287` → `create_session(drone_id)`），`create_session` 只會綁
   「當下 `is_active` 的任務」。但 command 服務飛任務（mission_fly/upload/start）
   **從不設 `is_active`**——兩者無溝通，`session.mission_id` 就是 NULL。
   （8/5 舊架次有關聯，是因為當時在路徑頁手動「顯示於即時頁」設過 is_active。）
2. **重複任務記錄**：`fly-mission.py` 每次以 .plan 上傳都**新建一筆 mission**，
   所以下拉出現多筆重複 complex-survey/test-flight。就算架次綁上了，每次飛
   綁的是**不同**mission → compare 頁按 mission 分組後每個 mission 仍只有 1 架次，
   無法「多次飛行比較」。

## 修法（多機正確版；排程：任務層資料模型批次，非現在）

> 註：此 issue 是 MCP 終局「任務↔架次↔指令↔事件因果鏈」的一環
> （doc/agent-mcp-goals.md）。per-drone 綁定與多機驗收的 mav_sysid 對應共用
> 同一份身分基礎，故排在多機驗收之後由 PM 派工。

1. **per-drone 任務綁定**（取代單一全域 is_active，多機安全）：
   - `drones.active_mission_id` 欄位（backend migrate）。
   - command 上傳任務時 `UPDATE drones SET active_mission_id WHERE mav_sysid=sysid`。
   - `create_session` 綁定序：明示 > 該機 active_mission_id > is_active（後備）。
2. **重複任務去重**：`fly-mission.py` import 以名稱 upsert（同名重用既有 mission），
   同一 .plan 反覆飛＝多架次綁同一 mission → compare 可用。
3. is_active 保留原用途（即時頁疊圖顯示），與架次綁定解耦。

## 解決方式

（closed 時補；含 SITL 驗證：飛任務→架次綁上 mission、再飛→重用同 mission）
