# 021 · 機上資料（Vehicle Data）：QGC 式機上全量即時資訊蒐集與檢視

- 狀態：open
- 嚴重度：medium
- 位置：`apps/backend/app/mavlink_rx.py`（解讀層）＋前端新「機上資料」檢視
- 建立：2026-08-12

## 現象

使用者需求：「QGC 能蒐集無人機本身的即時資訊，我要在我們系統上也做一套」——
不只訊號事件，是機上全量即時資訊。

## 原因（現況差距）

QGC 的能力＝遙測串流＋tlog 側錄＋MAVLink Inspector（全訊息型別即時清單）＋
參數檢視＋感測健康＋機上 ulog 下載。我們的底子：原始層全量收集（014 capture）
與遙測入庫已有——缺**解讀層**（通用解碼）與**呈現層**（Inspector 式檢視），
以及參數/ulog 兩條協定。

## 修法建議（PM scope 定案 2026-08-12，分四期）

1. **Phase 1（本期）**：014 Phase B 通用解碼——單埠流每型訊息 generic decode，
   per-機登錄表（型別/頻率/最新欄位值）以 1–2Hz 廣播；UI 三層（儀表 IMU→
   事件 log→Inspector 原始）；SYS_STATUS 感測健康位併入。誠實原則：欄位名/
   單位照方言原樣、未知型別顯示 id。
2. **Phase 2**：每架次參數快照（PARAM_REQUEST_LIST 唯讀存檔可查）；
   **只讀不改**，參數編輯維持 QGC 分工。
3. **Phase 3**：機上 ulog 落地自動回收歸檔到架次（MAVLink FTP/log 協定）。
4. **Phase 4**：視需求再議（欄位歷史曲線等）。

排序：多機模擬環境 → IMU 驗收/事件 modal → 本案 Phase 1。
UI 規格由設計師出、走使用者核准流程。

## 解決方式

（closed 時補）
