# 020 · 架次未綁任務：新飛的資料在比較頁整個用不了

- 狀態：closed（2026-08-11 修復並驗證；PM 插隊到佇列最前面因它擋研究主流程且孤兒架次持續累積）
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

## 解決方式（2026-08-11）

**前向綁定（因果鏈事實源＝command 上傳）**：
- `drones.current_mission_id` 欄位（backend `migrate()`；順帶把 PM 2a 的
  `mav_sysid` 遷移也移進 backend migrate，解耦部署順序）。
- command `mission_upload` 成功後 `UPDATE drones SET current_mission_id`
  （sysid→drone 靠 `drones.mav_sysid`）。
- `create_session` 綁定序：明示 mission_id > 該機 current_mission_id >
  is_active 後備。is_active 保留原用途（即時頁疊圖），與架次綁定解耦。
- **去重**：`fly-mission.py` import 同名任務重用既有記錄，不每飛新建。

**回填**：`scripts/backfill-session-mission.sql`（一次性，冪等）——以
command_log 的 mission_upload 留痕為事實源，把孤兒架次綁回實際飛的任務。
本機實測：55 孤兒中回填 21 筆（sim-uav-1 有對應留痕者）；complex-survey
從 0 → **18 架次**，比較頁「同任務前後比較」恢復可用。綁不上的 34 筆＝
swarm 模擬機（17，無 command_log）＋手動解鎖/command 服務前的舊架次
＝資料斷代，維持 NULL 不強綁。

**SITL 驗證**：上傳 complex-survey → arm → 新架次 mission_id 正確綁到該
mission（032fe74f→b74aa544）；fly-mission 再飛同名 → 重用同一 mission。

殘留（非本 issue，記錄）：既有 2 筆同名 complex-survey（duplicate）其一
0 架次，是修復前 fly-mission 新建的 clutter；UI 側下拉附架次數即可自明
（前端，設計師已註記）。
