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
2. ~~COMMAND_ACK/MISSION_ACK 入流~~ → **2026-09-02 done**。

   **為什麼 `command_log` 已經留痕了還要收**：那張表記的是**我們送出去的**
   指令與它拿到的回應。而飛控會回應**任何人**送的指令——QGC、機上代理
   （失聯處置的 RTL）、驗收 rig、直接打端點的腳本。那些回應原本完全看不到，
   於是事後查「這台機為什麼突然回家」時，**證據鏈斷在「誰下的令」那一格**。

   記成 `vehicle_ack`，帶解出來的枚舉名（`MAV_CMD_*`／`MAV_RESULT_*`／
   `MAV_MISSION_RESULT_*`）；認不得的枚舉值回 `MAV_CMD_400` 這種字串——
   **比空白有用，它至少查得到**。`IN_PROGRESS` 不入流（長動作每秒重複回報、
   不帶新資訊，淹掉事件流的話真正的失敗就沒人看得見）。重複折疊沿用 STATUSTEXT
   那套。文案照老規矩寫成「飛控**收下**了 X」——**ACK 是我收到了，不是我做到了**。

   實測：重啟 command 服務觸發它問 `AUTOPILOT_VERSION`，真機回的
   `MAV_RESULT_ACCEPTED` 確實入流。
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
4. modem 擴充（鄰區/QTEMP/流量計數）、companion 健康、
   ~~ulog 事後回收~~ → **裁定前不做，見下方的算術**
5. ~~錄製檔的系統內可見性~~ → **2026-09-02 done**：
   `GET /api/captures`（列表）與 `GET /api/captures/{name}`（下載，支援 Range）。

   **「全數收集」如果拿不到，就只是一個宣稱。** 原始層從 2026-08-10 就在錄
   （實測 5.6 GB），但系統裡沒有任何地方看得到它——要知道有沒有錄到、
   錄了多少、能不能取用，得 ssh 進地面站 `ls` 一個容器裡的目錄。
   **那等於資料只對知道路徑的人存在。**

   tlog 與 QGC 回放、`pymavlink` 的 `mavlogdump.py` 相容，所以「取得檔案」
   就是取得全部，不需要我們再做一套檢視器。下載的檔名**逐字比對既有清單**
   ——`../` 這種東西不該靠字串檢查擋，該靠「它必須是我們列得出來的那些檔案
   之一」擋（**白名單而不是黑名單**）。實測路徑穿越回 404。

## ⛔ ulog／dataflash 事後回收：算術先於實作（2026-09-02）

**兩個事實讓這件事在現行鏈路上做不成，而它們與程式寫得好不好無關。**

**一、平台不對。** `ulog` 是 PX4 的格式；**本專案的真機是 ArduPilot**，
它寫的是 dataflash `.BIN`，走的是另一套協定（`LOG_REQUEST_LIST`／
`LOG_REQUEST_DATA`），不是 MAVLink FTP。所以「ulog 回收」這個標題本身就要改。

**二、頻寬。** 飛控↔伴飛電腦是 **57600 8N1 UART**（實測確認機上 `.env` 沒改
預設），理論上限 **5.6 KB/s**，而這條線**同時要跑遙測**：

| 可用頻寬 | 下載 2 MB | 5 MB | 20 MB |
|---|---|---|---|
| 100%（餓死遙測）| 5.8 分 | 14.5 分 | 58 分 |
| 50% | 11.6 分 | 29 分 | 116 分 |
| 30% | 19.3 分 | 48 分 | **193 分** |

**一趟十分鐘的飛行，日誌動輒數 MB。** 也就是說：在現行鏈路上回收一份日誌，
要花掉比飛行本身更長的時間，而且期間會排擠遙測——**而遙測正是我們真正在做的
研究資料**。

### 所以真正的選項是這三個，都要先裁

1. **不做**，改用地面站已有的 tlog（上面第 5 項）。tlog 缺的是機上高頻資料
   （IMU 原始、控制器內部量），但它有我們鏈路研究要的一切。
2. **落地後用別的路徑取**——把飛控的 SD 卡拔出來讀，或接 USB。
   **零頻寬成本、零程式**，代價是人要動手。
3. **提高 UART 速率**（921600 是 ArduPilot 常見值，快 16 倍 → 5 MB 約 1 分鐘）。
   但那要改飛控參數與 Pi 的設定，而且**要重驗整條鏈路**——57600 是目前唯一
   實測過的設定。

**建議 2**：它現在就能做、不花頻寬、不動任何飛安路徑。**1 是預設值**
（我們已經有 tlog）。**3 值得另案評估**，因為它同時會讓遙測與任務上傳都變快，
好處不只在日誌。

> **這一節的價值是那張表，不是結論。** 沒有它，「回收日誌」聽起來像一個
> 兩天的功能；有了它，它是一個「要嘛換路徑、要嘛換速率」的決定。
