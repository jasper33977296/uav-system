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

## 已完成（2026-08-10，隨路線 B 落地）

- STATUSTEXT → 事件流（`mavlink_rx.py`；嚴重度對映 MAV_SEVERITY）
- mode_change／sysid 撞號告警入事件流
- SYS_STATUS 電量已入結構層

## 已完成：Phase A（2026-08-11）— STATUSTEXT 韌性

`mavlink_rx.py`：STATUSTEXT 進事件流三件事——**分段重組**（MAVLink2 長訊息
切 50 字一段、同 id/chunk_seq 遞增、末段<50；末段掉包殘段逾時丟棄）、
**重複折疊**（同句連續重複 15s 窗內折成一筆帶 count、就地更新＋原地重播
`fold:true`）、**source 分類**（events 加 `source` 欄，STATUSTEXT 標 `'vehicle'`、
backend 推導的標 `'system'`；前端分「機上訊息／系統事件」）。

## 已完成：Phase A.2（2026-08-11）— PX4 Events 協定解碼（本表待做 #1）

**實測關鍵事實**：抓當天 tlog（含一次完整起飛 mode MISSION→RTL），
**STATUSTEXT=0 筆、EVENT(410)=65 筆**——證實 PX4 1.14 的 vehicle 通知
（Armed/Takeoff detected…）**全走 Events 協定、不走 STATUSTEXT**。故 Phase A
（只收 STATUSTEXT）在真機上抓到零；這解釋了為什麼它空轉。

`mavlink_rx.py::_decode_event`：當前 pymavlink 方言（v10.ardupilotmega，v20
common/all 也一樣）**未定義 410**，故從裸 frame 手工解（欄位線序 id(u32)/
event_time_boot_ms(u32)/sequence(u16)/dest_comp/dest_sys/log_levels/args[40]；
MAVLink2 尾零截斷補回；crc_extra=160 實算並對過真機 frame）。發
`type='vehicle_event'`、`source='vehicle'`、`detail={event_id,args,count}`，
severity 取 log_levels 外層 nibble（8=Protocol 等落表外→丟）；折疊同 STATUSTEXT。
驗收：`scripts/inject-event.py`（注入）＋`scripts/test-phase-a2.py`（單元 7/7 過：
解碼／severity／折疊 count／protocol 丟棄／source／fold 廣播／args 帶入）。

**本階段不含文字翻譯**：event 人話要逐韌體 event metadata（component metadata
JSON，QGC 那套）才翻得出——排 013-B 之後。前端先顯示「機上事件 #id（severity）」
骨架，metadata 落地時同列升級全文、UI 結構不變。原始層 tlog 全錄不丟。

## 待做：結構層剩餘

1. ~~**PX4 新版 Events 協定解碼**（msgid 410）~~（2026-08-11 Phase A.2 done：
   410 手工解、severity＋event id＋args 入流；見上）。**剩**：event metadata
   文字翻譯（逐韌體 JSON）＋411 CURRENT_EVENT_SEQUENCE 掉包偵測／補請求——排 013-B 後
2. COMMAND_ACK/MISSION_ACK 入流（command 服務已留痕 command_log，
   backend 側可從 tlog 重放）
3. ~~EKF／感測器健康旗標~~（2026-08-11 done：SYS_STATUS 位元、PREARM_CHECK、ESTIMATOR_STATUS、EXTENDED_SYS_STATE、MAV_STATE→failsafe 事件、前端 Ready to fly 橫幅）；MISSION_CURRENT、RC 狀態待做
4. modem 擴充（鄰區/QTEMP/流量計數）、companion 健康、ulog 事後回收
5. 錄製檔的系統內可見性（列表/下載 API 或文件化取用方式）
