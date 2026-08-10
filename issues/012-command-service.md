# 012 · command 服務：自製 GCS 的指令能力（取代 QGC 作業流程）

- 狀態：open
- 嚴重度：medium（新能力，非缺陷；GCS 取代計畫的承重結構）
- 位置：新服務（`apps/command/`，未建）；設計見 `doc/gcs-replacement.md` §1
- 建立：2026-08-10

## 需求

例行量測飛行完全不開 QGC：任務上傳、起飛/RTL/Hold、解鎖，由自製系統執行。
「backend 對 MAVLink 唯讀」改為模組邊界——ingest 不動，指令走獨立服務。

## 定案要點（詳見設計文件）

- 獨立服務、sysid 254、1Hz GCS 心跳（= PX4 datalink-loss failsafe 觸發源，
  服務存活屬飛安相關）
- **從第一天就是多機**：連線池依 `drones` 表 connection_url 逐台建立，
  所有 API 帶 drone_id（issue 011 的 ingest 多實例化用同一份註冊表，一併解）
- 指令佇列＋ACK 追蹤＋重送；無 ACK 不得顯示成功；緊急指令獨立路徑
- 任務上傳：MAVLink 2 握手＋回讀比對
- 指令留痕入庫；`ENABLE_COMMANDS` 預設關
- 機上 mavlink-router 常設端點兩個（14540 唯讀 / 14541 雙向），
  QGC 緊急接入走動態 TCP 5760

## 驗收

SITL：拔心跳→PX4 依 `COM_DL_LOSS_T` 執行 RTL（實測）；ACK 遺失注入→
UI 顯示失敗而非靜默；任務上傳中斷→機上任務保持原樣。

## 前置

階段 1（監看補齊：STATUSTEXT/MISSION_CURRENT/SYS_STATUS，backend 唯讀，
可先行）完成後動工。
