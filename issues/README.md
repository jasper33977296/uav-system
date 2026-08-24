# Issues

專案的已知問題、待修項目與設計待決事項。一個問題一個檔案，方便在 commit 訊息或
討論中直接引用編號（例：`fix: link_lost 永不觸發 (#001)`）。

## 慣例

- 檔名：`NNN-短標題-用連字號.md`，編號遞增不重用。
- **配號請找 PM**：多 session 並行編輯本索引，自行取號會撞號（實際發生過：
  018 被兩案同時取用）。要開新 issue 先向 PM 要號，或由 PM 代開。
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
| [014](014-two-tier-collection.md) | 兩層收集：機上傳出資訊全數收集（原始層已實作）| medium | in-progress | `apps/backend/app/capture.py` |
| [015](015-multi-autopilot-support.md) | 跨自駕儀支援：硬編碼 PX4 方言，非 PX4 機不可控（部分指令有飛安風險）| high | open | `reference/gap-analysis.md` |
| [016](016-rb5-platform-connectivity.md) | RB5 平台連線層：廣播 :14550／埠寫死／sysid bug，兩通道設計不會自動成立 | high | open | `reference/gap-analysis.md` §0 |
| [017](017-live-3d-visual-quality.md) | 即時頁 3D 品質：P1 join／P2 deck.gl／P3 底圖＋圖示全數收官（3D 機模另案）| medium | **closed** | `apps/frontend/lib/geo.ts` |
| [018](018-event-detail-plain-language.md) | 事件 detail 人話化＋新增 serving cell 變更事件 | low | open | 前端事件流＋backend 事件結構 |
| [019](019-agent-mcp-interface.md) | MCP agent 介面：任務層工具＋因果鏈紀錄＋分析 API（終局目標定案）| medium | open | `doc/agent-mcp-goals.md` |
| [020](020-session-mission-association-broken.md) | 架次未綁任務：新飛資料比較頁用不了 ✔回填驗證 | high | **closed** | `db.py:create_session` |
| [021](021-vehicle-data-suite.md) | 機上資料：QGC 式全量即時資訊（Inspector/參數快照/ulog 回收，分四期）| medium | open | `issues/021` PM scope 定案 |
| [022](022-flight-video.md) | 飛行影像：即時畫面＋架次錄影 mp4＋回放同步播放（地面錄製定案）| medium | open | `issues/022` |
| [023](023-missions-table-role-cleanup.md) | missions 表正名瘦身：死欄位＋生成物污染＋刪除語意（含 010）| medium | **closed** | `db/init/01_schema.sql` |
| [024](024-video-anchor-offset.md) | 影像時間錨點早 0.41s：暫緩修正，待真機實測（含重啟觸發條件）| low | deferred | `doc/flight-video-design.md` §9 |
| [025](025-group-rtl-stagger-not-implemented.md) | 編隊 RTL 高度錯開未實作：separate 同高任務緊急返航無分離保證 | low | deferred | `doc/group-missions-design.md` §10.2 |
| [026](026-autopilot-driver-abstraction.md) | 自駕儀驅動層抽象：廠牌差異收進獨立驅動，系統只呼叫抽象動詞 | medium | in-progress | 跨 backend／command |
| [027](027-arclength-projection-endpoint.md) | 弧長投影後端端點（§6b 共用查詢層供 MCP；含自適應格寬三要求）| low | open | `apps/backend` |
| [028](028-primary-drone-assumption-in-takeoff.md) | 起飛序列讀「主機」高度：非主機判斷全錯，反向會把地面機切進 AUTO.MISSION ✔實飛驗證 | high | **closed** | `apps/command/app/main.py` |
| [029](029-mission-frame-default-breaks-rtl.md) | 含 RTL 的任務一律上不去：無座標項 frame 預設錯（附 PX4 實測值域表）✔實測 | high | **closed** | `build_items`／`plan_check` |
| [030](030-manual-failsafe-wrong-mode-ardupilot.md) | 搖桿失聯自動懸停在 ArduPilot 切錯模式（承諾 Hold 實送 GUIDED）；附搖桿實飛驗證 ✔實飛 | high | **closed** | `mav.py:_tick_manual` |
| [031](031-arm-guard-auto-mode.md) | arm 防護：自動模式＋機上有任務時裸 arm＝立即自主起飛（SITL 實際發生）| high | open | `apps/command` |
| [032](032-joystick-cannot-control-rb5.md) | 搖桿無法真正控制 RB5 → **隨功能移除而結案（035），根因未確認**；排查過程對「靜默丟棄」類故障仍可參考 | high | **closed** | `apps/command`＋`reference/` |
| [033](033-emergency-availability-design.md) | 意外狀況下的可用性保障：分層防線設計（機上 failsafe 核定／緊急通道／手動接管／deploy guard／心跳解耦）| high | open | 跨服務＋部署流程 |
| [034](034-healthz-hides-zombie-router.md) | `/healthz` 不反映 router 死活：殭屍服務照回 ok（心跳停發近一小時無人察覺）| high | in-progress | `apps/command/app/main.py:184` |
| [035](035-remove-manual-control.md) | 移除虛擬搖桿：系統範圍收斂為航路管理＋飛行安全，連續操縱交給實體遙控器（含 026 待決點 1 定案）| medium | in-progress | `apps/command`／`libs/autopilot`／`apps/frontend` |
| [036](036-live-page-display-honesty.md) | 即時頁把「沒有資料」畫成「有資料」：斷線／從未連上／無定位三者同形，0,0 哨兵被當成座標畫在幾內亞灣 | medium | in-progress | `mavlink_rx.py`／`main.py`／`MapView.tsx` |

「✔實測確認」= 2026-08-03 首次實飛（SITL 起飛 → 進干擾區 → RTL）取得的實際資料佐證，
不只是讀碼推論。詳見 [progress/log/2026-08-03.md](../progress/log/2026-08-03.md)。
