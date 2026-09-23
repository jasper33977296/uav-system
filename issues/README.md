# Issues

專案的已知問題、待修項目與設計待決事項。一個問題一個檔案，方便在 commit 訊息或
討論中直接引用編號（例：`fix: link_lost 永不觸發 (#001)`）。

## 慣例

- 檔名：`NNN-短標題-用連字號.md`，編號遞增不重用。
- 新問題從 [TEMPLATE.md](TEMPLATE.md) 複製。
- 狀態寫在檔案開頭的欄位，同時更新下方索引表：

| 狀態 | 意義 |
|---|---|
| `open` | 已確認、待處理 |
| `in-progress` | 修改中 |
| `needs-decision` | 卡在設計取捨，需要先決定方向 |
| `deferred` | **知情暫緩**：已查明、決定現階段不修，檔案內必須寫明**重啟觸發條件**（什麼情況要回來做）|
| `closed` | 已修並驗證（在檔案末尾補「解決方式」與 commit）|

慣例：設計文件裡的「已知限制／暫緩項」要在本索引留一條 `deferred` 入口——
只寫在設計文件裡，等於只有讀那份文件的人看得到。

> **整份設計被裁定不做時同理**：狀態寫在該案的 issue 裡，
> 而**文件開頭要有「不做」的橫幅與重啟觸發條件**——否則下一個讀到它的人
> 會以為那是待辦。例：`doc/mavlink-signing-design.md`（040 A5，2026-09-02）。

- 嚴重度：`high`（擋到主要研究流程／示範）、`medium`（資料正確性或體驗受損）、
  `low`（清理、體感問題）。

## 索引

| # | 標題 | 嚴重度 | 狀態 | 位置 |
|---|---|---|---|---|
| [001](001-link-lost-event-never-fires.md) | `link_lost` 事件永遠不會觸發 ✔實測確認 | high | **closed** | `backend/app/main.py:44-51` |
| [002](002-handover-event-flapping.md) | handover 事件抖動狂噴 → 已移除該事件類型 ✔實測確認 | low | **closed** | `backend/app/link_sim.py:36-43` |
| [003](003-cell-id-not-persisted.md) | `cell_id` 沒寫進 DB → 改判非 bug，是 schema 語意不明 | low | **closed** | `backend/app/db.py:87-100` |
| [004](004-writes-while-disarmed.md) | 未 armed 時仍持續 1Hz 入庫，資料無限成長 ✔實測確認 | medium | **closed** | `backend/app/main.py:31-61` |
| [005](005-sitl-mavlink-target-ip.md) | SITL 在 host network 下把 MAVLink 送到區網閘道 | high | **closed** | `docker-compose.yml` |
| [006](006-battery-pct-x100.md) | `battery_pct` 多乘 100，實際值 10000 ✔實測確認 | medium | **closed** | `backend/app/ingest.py:51` |
| [007](007-heading-never-populated.md) | `heading` 從未訂閱，地圖機頭永遠指北 ✔實測確認 | medium | **closed** | `backend/app/ingest.py` |
| [008](008-readme-test-script-port-conflict.md) | README 測試腳本用 14540，與 backend 搶埠 | low | **closed** | `README.md:65` |
| [009](009-sitl-log-fills-disk.md) | SITL 沒掛 TTY，log 以 4.9GB/hr 寫爆磁碟 ✔實測確認 | critical | **closed** | `docker-compose.yml` |
| [010](010-missions-idle-columns.md) | missions.drone_id / status 欄位閒置 → 併入 023 一起結掉 | low | **closed** | `db/init/01_schema.sql` |
| [011](011-register-drone-not-wired.md) | 「註冊無人機」表單未接線 → 單埠多機自動註冊取代；多機實測過 | low | **closed** | `apps/frontend/app/drones/page.tsx` |
| [012](012-command-service.md) | command 服務：自製 GCS 指令能力——已為系統核心，階段交付完成（真機 failsafe 實測列部署清單）| medium | **closed** | `doc/gcs-replacement.md` §1 |
| [013](013-group-missions.md) | 群組任務：V1 全案收官（skew/RTL 實測）；V2/V3 自動指派歸 019 目標層 | medium | **closed** | `doc/gcs-replacement.md` §3 |
| [014](014-two-tier-collection.md) | 兩層收集：原始層＋結構層。事件翻譯／411 掉包／ACK 入流／錄製檔可見性完成；**機上錄製自動回傳已上機**（只在地面傳、一解鎖立刻停、可續傳驗 sha256）；**ulog 回收算術上做不成**（57600 上 5 MB 要 29 分鐘），剩 modem 擴充與 companion 健康 | medium | in-progress | `capture.py`＋`onboard_capture.py`＋`mavlink_rx.py` |
| [015](015-multi-autopilot-support.md) | 跨自駕儀支援：硬編碼 PX4 方言，非 PX4 機不可控（部分指令有飛安風險）| high | open | `reference/gap-analysis.md` |
| [016](016-rb5-platform-connectivity.md) | RB5 平台連線層：三條病因全是 RB5 專屬，而現役是 Pi 5＋ArduPilot——**前提已不成立**；部署文件已改寫（含重啟觸發條件）| high | deferred | `doc/deployment.md` 附錄 A |
| [017](017-live-3d-visual-quality.md) | 即時頁 3D 品質：P1 join／P2 deck.gl／P3 底圖＋圖示全數收官（3D 機模另案）| medium | **closed** | `apps/frontend/lib/geo.ts` |
| [018](018-event-detail-plain-language.md) | 事件 detail 人話化＋新增 serving cell 變更事件 | low | open | 前端事件流＋backend 事件結構 |
| [019](019-agent-mcp-interface.md) | MCP agent 介面：**MCP 先不做**（2026-09-02），改提供三個任務層 API＋OpenAPI（已交付）；因果鏈與分析 API 仍 open | medium | open | `doc/mission-api.md`＋`doc/agent-mcp-goals.md` |
| [020](020-session-mission-association-broken.md) | 架次未綁任務：新飛資料比較頁用不了 ✔回填驗證 | high | **closed** | `db.py:create_session` |
| [021](021-vehicle-data-suite.md) | 機上資料：QGC 式全量即時資訊（Inspector/參數快照/ulog 回收，分四期）；**09-02 發現訊號記錄通道機上那半從來沒實作**——`link_metrics` 真機一筆都沒存過，已接上 | medium | open | `issues/021` PM scope 定案 |
| [022](022-flight-video.md) | 飛行影像：即時畫面＋架次錄影 mp4＋回放同步播放（地面錄製定案）| medium | open | `issues/022` |
| [023](023-missions-table-role-cleanup.md) | missions 表正名瘦身：死欄位＋生成物污染＋刪除語意（含 010）| medium | **closed** | `db/init/01_schema.sql` |
| [024](024-video-anchor-offset.md) | 影像時間錨點早 0.41s：暫緩修正，待真機實測（含重啟觸發條件）| low | deferred | `doc/flight-video-design.md` §9 |
| [025](025-group-rtl-stagger-not-implemented.md) | 編隊 RTL 高度錯開未實作：separate 同高任務緊急返航無分離保證 | low | deferred | `doc/group-missions-design.md` §10.2 |
| [026](026-autopilot-driver-abstraction.md) | 自駕儀驅動層抽象：B1–B3 完成；**B4 搬家進行中**（協定契約＋執行期不一致護欄已上線，機端移植待做）| medium | in-progress | `libs/autopilot`＋uav-agent |
| [027](027-arclength-projection-endpoint.md) | 弧長投影後端端點（§6b 共用查詢層；自適應格寬＋偏航捨棄計數＋方法參數可見）✔實測 | low | **closed** | `apps/backend/app/chainage.py` |
| [028](028-primary-drone-assumption-in-takeoff.md) | 起飛序列讀「主機」高度：非主機判斷全錯，反向會把地面機切進 AUTO.MISSION ✔實飛驗證 | high | **closed** | `apps/command/app/main.py` |
| [029](029-mission-frame-default-breaks-rtl.md) | 含 RTL 的任務一律上不去：無座標項 frame 預設錯（附 PX4 實測值域表）✔實測 | high | **closed** | `build_items`／`plan_check` |
| [030](030-manual-failsafe-wrong-mode-ardupilot.md) | 搖桿失聯自動懸停在 ArduPilot 切錯模式（承諾 Hold 實送 GUIDED）；附搖桿實飛驗證 ✔實飛 | high | **closed** | `mav.py:_tick_manual` |
| [031](031-arm-guard-auto-mode.md) | arm 防護：自動模式下裸 arm＝立即自主起飛（SITL 實際發生）；判準用模式動詞、附 intent override ✔對帳 | high | **closed** | `apps/command/app/main.py:226` |
| [032](032-joystick-cannot-control-rb5.md) | 搖桿無法真正控制 RB5 → **隨功能移除而結案（035），根因未確認**；排查過程對「靜默丟棄」類故障仍可參考 | high | **closed** | `apps/command`＋`reference/` |
| [033](033-emergency-availability-design.md) | 意外狀況下的可用性保障：分層防線設計已交付（`doc/emergency-availability-design.md`）；**四條裁定全數完成並實作**：第 2 層取消、心跳解耦、`FS_GCS` 開（逾時 45s＞代理 30s）、生產拿掉 `--reload`；剩實機參數覆核與呈現層 | high | in-progress | 跨服務＋部署流程 |
| [034](034-healthz-hides-zombie-router.md) | `/healthz` 不反映 router 死活：殭屍服務照回 ok（心跳停發近一小時無人察覺）；偵測＋503＋前端告示已落地，**只剩自動重啟待裁** | high | in-progress | `apps/command/app/main.py:198`＋`CommandPanel.tsx` |
| [035](035-remove-manual-control.md) | 移除虛擬搖桿：系統範圍收斂為航路管理＋飛行安全，連續操縱交給實體遙控器（含 026 待決點 1 定案）| medium | in-progress | `apps/command`／`libs/autopilot`／`apps/frontend` |
| [036](036-live-page-display-honesty.md) | 即時頁把「沒有資料」畫成「有資料」：斷線／從未連上／無定位三者同形，0,0 哨兵被畫在幾內亞灣 ✔對帳；**09-02 同族第五處：刪掉的機還在即時頁上**（刪除只動資料庫，執行期照樣廣播）| medium | **closed** | `mavlink_rx.py`／`main.py`／`MapView.tsx` |
| [037](037-plan-autopilot-mismatch.md) | `.plan` 自報的 firmwareType／vehicleType 被完全忽略：PX4 寫的航線靜默上到 ArduPilot 機 ✔三處對帳，示警放行 | **high** | **closed** | 匯入／入庫／上傳三處 |
| [038](038-board-identity.md) | 系統不知道哪台是哪台：本階段請求並記錄飛控板 UID；**比對與告警 09-02 實作**（撞號的 PX4 SITL 曾寫 46 筆假事件進真機記錄）| medium | in-progress | `mavlink_rx.py`＋uav-agent |
| [039](039-autonomous-flight-state-machine.md) | 全自動飛行的狀態機與安全守門：**飛行中上傳任務會立刻改道且無任何守門**（SITL 實測）。飛安裁定全數完成（08-31 複裁七條），A／C／E／G 待實作 | **high** | in-progress | `doc/autonomous-flight-state-machine.md` |
| [040](040-sysid-must-be-assigned.md) | **sysid 由系統指派＋入列驗證協定**：驗證完成前不得指派任務或控制。唯一鍵值＝板號、撞號自動重新配號、代理強制；**A1–A4 完成**；A5 簽章**裁定不做**（設計留存，含重啟觸發條件）| **high** | in-progress | `mavlink_rx.py`＋`command`＋`drones` 表＋uav-agent |
| [046](046-crash-20260907-lowspeed-test.md) | 摔機 lowspeed-test-260907：機上 tlog 找到，**死因是機械**——與設定、與高度估計、與本系統改過的參數都無關（當天寫進飛控的參數全部讀回比對過）| **high** | **closed** | `issues/evidence/`＋機上 tlog |
| [047](047-terrain-and-link-recovery.md) | 地形檢查與斷線重連：四項裁定分批實作。地形 A／B 兩條都做、DEM 上傳前與飛控核對、斷線恢復做到 L2、斷線畫面已實作；**補傳去重的根因是「從來沒有生效過」**；項次 6 的緩衝觸發條件仍是錯的 | **high** | in-progress | `libs/`＋`apps/backend`＋uav-agent |
| [048](048-plan-safe-altitude.md) | 航線的「最低安全高度」與系統自行規劃：**現有檢查問錯了問題**——有效速度沒照飛控實際執行的語意算、安全高度該是兩個下限取大。起因是 046 那次摔機 | **high** | open | 航線檢查＋規劃流程 |
| [049](049-link-display-gated-on-mavlink.md) | 訊號面板被 MAVLink 綁架：飛控不在、5G 訊號就整塊消失（**036 的鏡像**：把「有資料」畫成「沒有資料」）；後端手上樣本新鮮，卻被廣播閘 `ever_connected` 擋掉。**閘改成「有遙測或機上代理正在送訊號」**（看新鮮度，幽靈機仍擋住）；端到端重現驗證 0 則 → 41 則 | medium | **closed** | `backend/app/main.py:207`＋`useTelemetry.ts:40`／`SidePanel.tsx:381` |
| [050](050-agent-fc-link-watchdog.md) | **飛控串列斷了，代理不知道、不出聲、也不試著救**：25 分鐘零告警，靠人工重啟才恢復（而重啟有效只是 pyserial 重設 termios 的副作用）。**需求 1 偵測（a02f7d3）與 2 取回（17e5765）已完成並上機驗證**——真機重現原鏈路一秒未斷；需求 3「失聯期間持續出聲」**上機 09-22**（序列埠被改 3 秒內改回並報到地面站；真的讓飛控失聯還沒做）（走代理通道的 notice＋`fc_link`，畫面說得出斷在哪一段） | **high** | in-progress | `uav-agent/agent.py`＋`tools/fc-link-watchdog.py` |
| [051](051-mission-list-planned-vs-flying.md) | 對外任務歷史清單把**「建了沒飛」報成「進行中」**：`started_at`／`ended_at` 兩個 null 意思不同而回應說不出差別；實查現在唯一被判成「進行中」的任務從來沒飛過（036／049 同族）| medium | **closed** | `backend/app/ext_history.py:74,102` |
| [052](052-sample-interval-hardcoded.md) | 對外訊號的 `sample_interval_s` 是**寫死的常數 1**，而取樣率由機上 `--modem-interval` 決定、backend 沒有管道知道；同類錯誤已發生過（實測 2.61 s vs 宣稱 1 s）| medium | **closed** | `backend/app/ext_history.py:192` |
| [053](053-gaps-telemetry-vs-samples.md) | `gaps` 是**遙測失明**不是訊號樣本缺口，而文件叫控制端拿它畫留白：真機上樣本可補傳、遙測與樣本各自會斷，兩個方向都會畫錯（模擬時重合所以看不出來）| medium | **closed** | `backend/app/ext_history.py:170-176` |
| [054](054-max-offset-m-tunable-by-caller.md) | `max_offset_m` 被外部調得動（FastAPI 自動 query 的副作用），同一趟資料在不同呼叫下給出不同 `along_m`；文件沒寫這個參數 | low | **closed** | `backend/app/ext_history.py:129` |
| [055](055-mission-list-silent-truncation.md) | 任務清單**截斷了不說**：只有 `limit`（上限 200），沒有 `total`／`has_more`／cursor，拿到滿額時分不出是剛好還是被切掉 | low | **closed** | `backend/app/ext_history.py:21,93` |
| [056](056-mission-fly-skips-start-point.md) | 一鍵起飛**跳過航線第一個點**：ArduCopter 的 NAV_TAKEOFF 不看經緯度，而序列是先離地才切 AUTO，所以機直接飛往第二個點。改成離地後先 GUIDED 飛到起始點；超過 100 m 要確認。✔SITL；群飛與 PX4 尚未改 | high | **closed** | `command/app/main.py:mission_fly` |
| [057](057-agent-events-never-consumed.md) | 代理的事件清單沒有人讀（18 處寫、0 處讀）→ **接上去**：只有代理知道的 11 處改走新的 `notice` 型別（斷線緩衝、逐則回執、重連重送、缺號看得到、`gs_link` 節流但彙總說出來），地面站看得到的 7 處刪掉；事件時間改用發生時刻 ✔端到端＋上機（真實失聯未重現）| medium | **closed** | `uav-agent/outbox.py`＋`/ws/agent` |
| [058](058-external-param-change-not-surfaced.md) | **別人改了飛控參數，畫面不說**：2026-09-21 排查花了數小時。我們不留存 `PARAM_VALUE`，所以說不出「它變了」；預檢字串原樣轉出，少了「哪個圍欄、我離它多遠」。**A 完成 09-22**：`drone_params` 基準＋`param_changed` 事件（重啟後也比得出、沒有基準先借上一趟快照、說得出是不是經由指令服務改的）；真機那一輪待代理恢復後看；B、C 未做 | **high** | in-progress | `mavlink_rx.py`＋即時頁預檢呈現 |
| [059](059-agent-must-own-the-uart.md) | **uav-agent 必須永遠擁有 UART 最高優先權**：`get-gps.py`／`mavsdk_server` 與代理同開 `/dev/ttyAMA0`，兩邊各拿隨機片段——校正永遠跑不完且不報錯。050 需求 1 列過 `TIOCEXCL` 但沒做，理由取捨錯了（日常踩到的是非 root）。**09-21 裁定：搶埠要報到地面站**（走 057 的管道），啟動時發現被搶不拒絕啟動。**A 完成並上機 09-22**（`TIOCEXCL`；issue 寫的 pyserial `exclusive=True` 其實是 flock、擋不住，已更正）；**B 上機驗證 09-22**：root 佔埠 5 秒內報出 pid／命令列；翻出 unit 要 `CAP_SYS_PTRACE`＋`CAP_DAC_READ_SEARCH` 兩個（只給一個時 158 個行程看不到）（09-22 部署時 5G 斷線；另發現 C 的前提不成立：機上沒有本機 MAVLink 出口）| **high** | in-progress | `agent.py` 開埠＋unit＋README |
| [060](060-fly-to-start-only-on-takeoff.md) | **重複執行同一路徑仍不會先飛到起始點**：`_fly_to_start` 全檔只有一個呼叫點（`mission_fly`），已在空中重跑完全不經過。056 只修了起飛那一條；航線第一段永遠沒飛到＝A/B 比較的基準被破壞。**09-21 三項裁定**：重新／繼續分成兩個動作、高度以 plan 起始點為絕對主導、先做單機。**主體已實作上線 `59380c9`**（順手修掉擋住診斷的 audit 截斷）；**SITL 行為驗證未跑** | **high** | in-progress | `apps/command/app/main.py:958` |
| [061](061-external-repeat-is-a-new-mission.md) | 外部控制重複執行同一路徑要算成不同任務，名稱以時間自動產生——否則多趟資料疊在同一個任務下，「比較兩趟」拿不出來。**09-23 裁定並完成**：每一次請求都是新任務（重用 `mission_id` 改回 409 `mission_id_used`）、舊任務會被結束並在 `replaced` 說出來、`missions.plan_id` 綁一條路徑、畫面名稱預先填好。**對外契約變更** | medium | **closed** | `apps/command`＋對外契約 |
| [062](062-waypoint-hold-time-not-settable.md) | 規劃時設不了航點停留秒數。**資料模型其實已經支援**——`mission_time.py` 讀 `NAV_WAYPOINT` param1 算進飛行時間，缺的只是編輯端。**09-21 裁定：停留期間的樣本要標記**，任務歷史要說得出「靜止量測 N 秒、M 筆」。**09-22 兩半都完成**：規劃端設得出來（p1，起飛／降落擋 422）、剖面看得出來（翻出既有航線本來就有停留卻看不到）；任務歷史的 `holds`／`hold_seq` 是**量出來的**——實飛推翻了「到點事件＝到達」（ArduCopter 在停留結束才報）；09-21 實飛 10／20／10 → 量到 11.1／19.2／11.1 s。瀏覽器點選與縮圖標示未做 | medium | in-progress | 規劃 UI |
| [063](063-waypoint-hidden-under-3d-building.md) | 點位落在建築物上被 3D 建物蓋住就再也選不到，只能整條路徑重來。**09-23 完成**：選本來就沒壞（命中測試看螢幕距離），壞的是看不到——航點改成永遠畫在最上層，加上航點列表保底 | medium | **closed** | `MapView.tsx` 圖層順序／命中測試 |
| [064](064-2d-route-preview.md) | 控制端要看得到每條路徑的 2D 預覽圖。現有縮圖是等距 3D，同場地幾條路徑在斜角下形狀相似又可各自轉向，彼此比不了。**09-23 完成**：`MissionThumb2D` 北方朝上、同場地同比例、有方向箭頭與停留環，列表預設 2D | low | **closed** | `MissionThumb3D.tsx` |
| [065](065-round-trip-overlapping-waypoints.md) | **來回路徑的重疊點位選不到——規劃流程要重新設計**。表示法與選取是兩個問題；建議「折返」變成路徑屬性（源頭消滅重疊）＋航點列表保底。**spiderfy 散開顯示確定不做**：飛行規劃介面不該把點畫在假座標上。**2026-09-21 定案走 D＋A**（折返變路徑屬性＋航點列表保底），三個細節待定。**09-22 裁定折返改成「規劃動作」**（回程點真的加進去、可個別改；回程不帶停留、可分別設定），N 趟不做在路徑上；既有航線走 `add-return` 另存一份 | medium | **closed** | 規劃 UI＋航線資料結構 |
| [066](066-plan-table-to-map-editing.md) | 規劃頁：點路徑表格列直接跳到地圖上那條路徑線就地編輯每個參數。**這是 065A／062／063／064 的共同編輯面**——分開做會做出三套不一樣的介面。**09-23 完成**：點一列就進編輯頁（飛行紀錄改 ▸ 展開）、側欄最上面是航點列表（選取與地圖雙向同步、顯示停留、可刪點、可加回程） | medium | **closed** | `app/plans/page.tsx`＋`MapView.tsx` |
| [067](067-link-lost-ui-copy-and-buttons.md) | 失聯呈現兩件：時間不要寫「7.4 小時前」要寫「7 小時 25 分前」；**失聯時那兩顆返航／降落按鈕拿掉**（按了也沒作用，推翻 09-07 裁定）。實作要順便決定：下行斷但上行還通時要不要保留。**09-22 結案**：時間改「H 小時 M 分前」；按鈕依**指令通道的事實**（指令服務 5 秒內還收得到這台機）決定——通就留、不通就拿掉並說明；真畫面翻出第二塊矛盾的失聯區（`admitted_offline`）一併處理 | low | **closed** | `lib/staleness.ts`＋`CommandPanel.tsx` |
| [068](068-inflight-param-adjust.md) | **意外情況下即時調整飛控參數**。能力已經在了（09-07 裁定，白名單 20 筆已含 `FENCE_*`），擋住的只有「解鎖中一律 409」那一道。主張：不改成開關，另開更窄的飛行中清單並逼出 `inflight_effect`；**飛行中的範圍檢查是狀態相依的**（降天花板會立刻 breach）；一次一個、要限速；058 是前置 | **high** | needs-decision | `command/main.py:538`＋`params.py`＋`guard.py` |
| [069](069-agent-one-shot-deploy.md) | **uav-agent 沒有一鍵部署腳本**：每次手動 scp＋手動 md5 核對單一檔；機上說不出跑的是哪個 commit，解鎖中不准重啟也全靠人記得。建議開發機跑的 `deploy.sh`：乾淨工作目錄、看 `state.json` 新鮮度的解鎖閘、rsync 全部執行檔、寫 `DEPLOYED`、重啟後驗收、可回滾。**09-22 完成**：`deploy.sh` 真機部署／回滾／再部署都走過；第一次 dry-run 就看到機上 18 個檔與 repo 不同 | medium | **closed** | uav-agent（新腳本）|
| [070](070-agent-start-time-sync.md) | **代理每次啟動前要先做時間同步**（09-22 使用者裁定）：Pi 重開後約 18 分鐘才對上時，代理那 8 分鐘的時間全慢 21 分鐘（057 的 notice 帶 `at_unsynced` 抓到）。擋多久要有上限——沒有橋比時間錯更糟。**09-22 實作並上機**：ExecStartPre 催 timesyncd，真機 1 秒對上（偏移 +12 分 13 秒）；剩開機時 5G 晚起來那一格 | medium | in-progress | `uav-agent` unit 前置＋`clock.py` |
| [071](071-drone-param-config-page.md) | 無人機管理頁的「配置無人機參數」子頁（09-22 使用者提出，方向記錄）：058 抓到 `FS_GCS_ENABLE` 1→0，裁定先不改回、日後在這個子頁改 | medium | open | `app/drones/`＋指令服務 `set_params` |
| [072](072-pi-unexpected-reboots.md) | **機上 Pi 無預警重開**：09-22 八分鐘內兩次、都沒有關機程序（像斷電），第一次同時地面站閘道也斷；原因未明。飛行中發生＝代理、失聯處置、緩衝全部歸零 | **high** | open | Pi 供電／5G 模組 |
| [073](073-terrain-frame-action-deferred.md) | **「改成地形跟隨」暫緩**（09-23 裁定）：用過 0 次；本場域航線 53–67 m、起伏 1.1–1.5 m，而飛控地形格 100 m、無測距儀——它不改變任何事，卻讓畫面多一套高度語意。收按鈕、留端點與上傳閘門；**重啟條件**：起伏 > 10 m 的場地／裝測距儀／飛控沒圖磚的新場地 | low | deferred | `app/plans/page.tsx`＋`terrain-frame` 端點 |

「✔實測確認」= 2026-08-03 首次實飛（SITL 起飛 → 進干擾區 → RTL）取得的實際資料佐證，
不只是讀碼推論。詳見 [progress/log/2026-08-03.md](../progress/log/2026-08-03.md)。
