# 012 · command 服務：自製 GCS 的指令能力（取代 QGC 作業流程）

- 狀態：**closed（2026-08-13 索引對帳時正式收案）**——command 服務早已是系統核心
  （單機/多機指令、編隊執行器、capabilities gating、驅動層皆建於其上）。
  惟一未竟項「真機心跳 failsafe 實測」已列入 `doc/deploy-checklist.md`
  首飛驗收順序，於使用者上機時執行，不再由本 issue 追蹤。
- 嚴重度：medium（新能力，非缺陷；GCS 取代計畫的承重結構）
- 位置：`apps/command/`；設計見 `doc/gcs-replacement.md` §1
- 建立：2026-08-10

## 已落地（SITL 全流程驗收通過）

`apps/command/`（獨立容器 uav-command，port 38001）：sysid 254、單埠
多機路由（自管來源位址表，mavutil udpin 不記來源故接管 recv/send）、
1Hz 心跳（enable 時才發）、指令 ACK 契約（重送 3 次、無 ACK 即失敗）、
任務上傳（MISSION_ITEM_INT 握手＋**回讀逐項比對**）、模式切換
（mission/hold/rtl/land）、`command_log` 留痕、`ENABLE_COMMANDS`
預設關（403）。驗收：上傳 4 航點 verified → arm → mission start →
飛行中 RTL → 降落自動上鎖，留痕 4 筆，架次自動結算。

## 待做（階段 3）

前端控制 UI（滑動確認解鎖、緊急鈕常駐、拒絕原因就地顯示）、
拔心跳觸發 `COM_DL_LOSS_T` failsafe 實測、drones.mav_sysid 對應 UI。

## 需求

例行量測飛行完全不開 QGC：任務上傳、起飛/RTL/Hold、解鎖，由自製系統執行。
「backend 對 MAVLink 唯讀」改為模組邊界——ingest 不動，指令走獨立服務。

## 定案要點（詳見設計文件）

- 獨立服務、sysid 254、1Hz GCS 心跳（= PX4 datalink-loss failsafe 觸發源，
  服務存活屬飛安相關）
- **從第一天就是多機**：**單埠＋sysid 路由**（2026-08-10 定案，取代
  connection_url 連線池）——14541 一個埠收發所有機，指令依
  `drones.mav_sysid` 定址、回程依「sysid → 來源位址」路由；
  所有 API 帶 drone_id（issue 011 的 ingest demux 用同一份 sysid 對應）
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
