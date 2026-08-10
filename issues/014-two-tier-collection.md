# 014 · 兩層收集：機上傳出的資訊全數收集（原始層＋結構層）

- 狀態：in-progress（原始層 2026-08-10 已實作；結構層待做）
- 嚴重度：medium（資料完整性；GCS 取代階段 1 的擴充）
- 位置：`apps/backend/app/capture.py`；設計 `doc/gcs-replacement.md` §2
- 建立：2026-08-10

## 需求（討論定案）

任務上傳可能失敗且要看得到原因 → 換位：**任何無人機傳出的資訊都要
在系統中收集到**。盤點（SITL 實測）：待機 27 種週期訊息 ~368 則/s
~16.5 KB/s；事件/回應型 ~12 種（STATUSTEXT/COMMAND_ACK/MISSION_ACK⋯，
上傳失敗原因碼在此）；另有 modem ~10 項、companion 健康 ~5 項、
ulog 100+ topic（事後回收）。

## 已完成：原始層

透明 tee：backend 綁 MAVLINK_URL 埠，每框架落盤（tlog，工具鏈相容）
後轉發內部埠給 mavsdk；回程原路轉回（任務讀回實測通過）。UTC 日切檔、
`CAPTURE_KEEP_DAYS=30` 滾動清理、錄製失敗不拖垮資料路徑、啟動失敗
退回直連。設定：`CAPTURE_ENABLED`/`CAPTURE_DIR`/`CAPTURE_KEEP_DAYS`。

## 待做：結構層（issue 012 階段 1 的具體清單）

1. STATUSTEXT → 事件流；COMMAND_ACK/MISSION_ACK →（含原因碼）
2. MISSION_CURRENT、SYS_STATUS 健康旗標、RC 狀態
3. modem 擴充（鄰區/QTEMP/流量計數）、companion 健康、ulog 事後回收
4. 錄製檔的系統內可見性（列表/下載 API 或文件化取用方式）
