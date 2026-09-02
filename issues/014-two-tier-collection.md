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

1. ~~**PX4 新版 Events 協定解碼**（msgid 410）~~（Phase A.2）；
   ~~**文字翻譯**~~、~~**411 掉包偵測**~~ → **2026-09-02 done**：

   * **韌體版本比對接上了。** `fw_match` 的三態早就實作，但**呼叫端一直沒把
     版本傳進去**，所以它恆為 `unknown`——**一個接了一半的守門**。
     那段程式的註解寫著阻塞是「機上韌體版本我們拿不到」，而**那條阻塞已經被
     [038](038-board-identity.md) 走完了**（command 服務問 `AUTOPILOT_VERSION`
     → `drones.flight_sw_version` → 回填）。
     > **一個因為別的工作而消失的阻塞，不會自己去通知被它擋住的那段程式碼。**
   * 比對只取 `x.y.z`：機上字串帶 `(official)` 之類的後綴，而**建置型別不改變
     事件 id**——拿它去比會把相符的判成不符。
   * **版本不符時不給 `text`**（README 的規則）：事件 id 是名稱的雜湊，
     跨版本翻出來的是**看起來合理但完全錯誤**的句子，比顯示原始 id 危險得多。
     但事件本身不丟，且回傳 `no_text_reason` 讓 UI 說得出為什麼沒有翻譯。
   * **411 `CURRENT_EVENT_SEQUENCE` 掉包偵測**：410 自己的序號與 411 機端主動
     報的序號都餵進同一個判定。**「沒有事件」與「事件掉了」在畫面上完全同形**
     ——一段安靜的事件流可能代表飛得很順，也可能代表我們瞎了那一段，
     序號是唯一分得出來的東西。缺口記成 `vehicle_events_missed`（warning）。
     處理了 u16 迴繞、序號倒退（不當掉包）、以及 `RESET` 旗標
     （機端歸零不是我們漏了）。
   * **只偵測不補請求**：補請求要送 `MAV_CMD_REQUEST_EVENT`＝`COMMAND_LONG`，
     而 backend 的唯讀邊界是**依訊息型別**擋的——放行它等於同時放行 arm 與
     切模式。要補請求該由 command 服務做，與 038 同一條路。

   驗證 `scripts/test-px4-events.py`（17 項，容器內跑）。
   **注意：真機是 ArduPilot，不走 EVENT 協定**（它用 STATUSTEXT），
   所以這一批只在 PX4 上會被實際執行——實測近 10 分鐘真機 0 筆，符合預期。
2. COMMAND_ACK/MISSION_ACK 入流（command 服務已留痕 command_log，
   backend 側可從 tlog 重放）
3. ~~EKF／感測器健康旗標~~（2026-08-11 done）；~~MISSION_CURRENT~~（done）；
   ~~**RC 狀態**~~ → **2026-09-02 done，但實測發現它在真機上還沒有訊號來源**：

   * `st.rc_link` 三態（True／False／**None＝不知道**），判準與機上代理
     **逐字相同**：`SYS_STATUS` 的 `present` 位元決定「知不知道」、`health`
     決定真假，**不看 `enabled`**（那是「壞了沒」不是「在不在」），
     **不用 `RC_CHANNELS.rssi`**（rssi 沒有「不知道」這一態，255 是無效值
     不是滿格）。判準不同的話 crosscheck 會噴一堆假的不一致，
     **而真的不一致就會淹在裡面**。
   * 轉態時發事件，**含「不知道 → 掉線」那一格**——那是最該被看見的一次
     轉換，只比對 True/False 的寫法會讓它安靜地過去。
   * 併進 026 §9 的執行期 crosscheck（`rc_link` 在協定裡是頂層欄位不在
     `derived` 裡，比對前攤平——**位置不同不代表它不該被比對**）。

   > ### ⚠ 真機實測：ArduPilot 4.7 **不回報這個位元**
   >
   > 2026-09-02 從 `msg_registry` 讀真機的 `SYS_STATUS`：**回報 18 個感測器，
   > RC 接收機不在其中**。所以這台機的 `rc_link` 恆為 `None`，
   > 兩邊一致（實測：地面站 `None`、代理 `None`，crosscheck 0 筆不一致）。
   >
   > **後果：[039](039-autonomous-flight-state-machine.md) 複裁 A 的 RC 守門
   > 在這台真機上從未生效。** `None` 不擋是對的（把「不知道」當成「沒有 RC」
   > 會讓所有還沒回報這個位元的機都起飛不了），所以**要修的不是那條政策，
   > 是「這個廠牌的訊號來源」**——見 039 的待查項。
4. modem 擴充（鄰區/QTEMP/流量計數）、companion 健康、ulog 事後回收
5. 錄製檔的系統內可見性（列表/下載 API 或文件化取用方式）
