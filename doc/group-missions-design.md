# 群組任務設計（issue 013）

- 狀態：設計定案（2026-08-11，三方討論＋PM 裁決）；實作未開工，依賴多機 SITL 環境
- 相關：`issues/013-group-missions.md`、`doc/gcs-replacement.md` §3、
  issue 011（多機接入）、issue 020／019（任務↔架次因果鏈、MCP submit_mission）

## 目標與範圍

任務控制能選單台/多台；多台時選「統一路徑」或「分別分配路徑」。研究價值：
N 台同測＝一次飛行取得鏈路品質的空間分布（垂直剖面／平面分割掃描）。

**核心安全原則**：多台不能真飛同一條線。「統一路徑」是**地面生成**概念——
一條 base 路徑展開成 N 條各自具體、含高度分層的實體任務，不做機上 runtime
offset（把安全推給 runtime、無法預檢）。

## 1. 資料模型

```
mission_groups
  id UUID PK
  name TEXT
  base_mission_id UUID       -- 「統一路徑」的來源；「分別分配」時可 NULL
  mode TEXT                  -- 'unified'（展開自 base）/ 'separate'（逐台指派）
  params JSONB               -- 展開參數（vsep_m、rtl_stagger_m…）
  created_at TIMESTAMPTZ

group_assignments
  group_id UUID FK
  drone_id UUID              -- 指派的機
  mission_id UUID            -- **具體 materialized 任務**（每台一條實體 waypoints）
  layer_index INT            -- 高度分層序（0 起）；決定 offset 與 RTL 高度
  PRIMARY KEY (group_id, drone_id)

flight_sessions
  + group_id UUID NULL       -- 架次所屬群組（群組↔架次因果鏈；接 020）
```

**每個 assignment.mission_id 是一條具體任務**，不是「共用 mission＋offset 參數」。
理由：PX4 只飛具體任務、command 只送具體 MISSION_ITEM_INT；具體化後 plan_check
能逐條驗、操作員上傳前看得到每台實際路徑。

**接因果鏈（沿用 020，零改動）**：command 群組上傳時對每台設
`drones.current_mission_id = 其 assignment.mission_id`，backend `create_session`
自動綁 `session.mission_id`；另寫 `session.group_id`。command_log 記 group 提交。
於是 group → assignments → 各機 mission → 各機 session（帶 group_id）→ telemetry/
link_metrics/events 完整可追，且與 019 MCP 的 submit_mission(N 台) 同一資料模型。

## 2. 「統一路徑」地面展開

`mode='unified'`：從 `base_mission_id` 生成 N 條具體任務，每台：
- **高度分層**：每個航點高度 += `layer_index × GROUP_VSEP_M`（避免同線同高相撞）。
  這也順帶提供**返航分離**：unified 各台在不同高度層飛行，RTL 時 PX4 於高於
  `RTL_RETURN_ALT` 時維持當前高度返航，故各台以 vsep 的間隔錯開返航（2026-08-12
  三機實飛驗證：29.9／34.9／39.9m，差 5m）。
- **RTL 高度錯開（`GROUP_RTL_STAGGER_M`）——設計項，現況未實作**：原設計要各台 RTL
  返航高度按 `layer_index × GROUP_RTL_STAGGER_M` 顯式錯開。實作現況是 `group_exec.rtl()`
  純 RTL-all（無 per-drone 返航高度、mav 層無 param-set）。unified 情境有上面的 vsep
  返航分離、rtl_stagger 冗餘；但 **separate 模式各機任務同高度時無此保護**——見 §10.2
  已知限制與操作規避。（使用者 2026-08-12 裁決：現階段不補實作、文件化限制。）
- 生成時即跑 `check_group`（§4）驗分離足夠。

`mode='separate'`：逐台指派既有任務（各自 mission_id），不展開；仍跑 check_group
互檢（同時飛的路徑要分離）。

## 3. 執行：兩階段提交狀態機

單執行緒 router 可落地——關鍵：router 在指令等待（`_wait()`）中**持續發心跳給
所有機**，逐台排隊**不會**餓死其他機的 datalink。

```
IDLE
 └─(execute)→ 伺服器端嚴格 gate（§5）失敗→ 回 IDLE（未動任何機）
 └─ UPLOADING：逐台 upload＋回讀比對
      └─任一失敗→ ABORT（尚未 arm，無需復原）→ IDLE
 └─ ARMING：逐台 arm
      └─任一 prearm 失敗→ GROUND_ABORT：全機 disarm（地面撤銷，零風險）→ IDLE
 └─ STARTING：全 armed 才逐台廣播 mission_start
      └─起飛前失敗→ 全機 disarm → IDLE
 └─ FLYING：
      └─單機異常→ 該台 RTL（PX4 failsafe 或指令），其他續飛（少一台＝資料缺片，非安全事件）
      └─群組 RTL-all 緊急鈕常駐
```

**起飛時間差**：逐台廣播 start，skew ＝ N × ACK。**011 驗收實測**（單執行緒
router）：每多一台約 +25ms（2 台並發 arm 端到端 49→74ms、總 75ms）。N 台
skew ≈ N×25ms（數十 ms），量測用途可接受（回放本就相對時間對齊）。
**結論：sequential 對 N=2-3 足夠，不改並行 router**。PX4 無原生 timed-start，
地面廣播是務實下限——不做 <100ms 硬同步。

## 4. 跨路徑衝突預檢

`plan_check.check_group(missions[], vsep_m, lsep_m)`：驗 N 條同時飛的路徑分離足夠
——高度分層 ≥ `vsep_m`（含 GPS 誤差裕度）或橫向 ≥ `lsep_m`。規模 >6 台時升級為
時空檢查（同時刻位置對）。這是「統一路徑」的地面安全門，生成時與 execute 前各跑一次。

## 5. Gating（PM 裁決：選擇自由、擋在行動點）

- **UI 選擇自由**：除 `mav_sysid=null`（無指令通道）外任何機都可勾選，帶狀態環
  預警（未驗證機顯示但標示）——checkbox 不預先 disabled。
- **伺服器端嚴格 gate（最終閘）**：`execute` 時逐台查 capabilities，**任一台非 "ok"
  就擋整個群組**、回逐台原因（不 silent drop——群組少一台＝研究資料缺片）。
  混機隊：ArduPilot 未驗證機在群組裡＝擋，直到可攜指令 SITL 驗過。
- capabilities 仍是伺服器端唯一真相（issue 015）；前端狀態環是提示，execute 才是門。

## 6. Config 參數（與 GEOFENCE_* 同款，ops/使用者定終值）

| 參數 | 預設 | 說明 |
|---|---|---|
| `GROUP_VSEP_M` | 5 | 相鄰分層垂直間隔（依真機尺寸/GPS 精度調） |
| `GROUP_RTL_STAGGER_M` | 4 | 各台 RTL 返航高度錯開量 |
| `GROUP_LSEP_M` | 10 | 橫向分離門檻（check_group 用） |
| `GROUP_MAX_DRONES` | 6 | 上限（對齊機隊色盤；>6 需時空檢查升級） |

## 7. API 契約

資料層（backend :38000，有 missions 表＋plan_check）：
- `POST /api/groups` — 建群組＋展開 materialized 任務＋預檢（capabilities＋check_group）。
  body: `{name, mode, base_mission_id?, drones:[{drone_id, mission_id?, layer_index?}], params?}`
  回: `{group, assignments:[{drone_id, mission_id, layer_index, capability, precheck}], conflicts}`
- `GET /api/groups/{id}` — 群組狀態（assignments、逐台 capability、預檢、執行中即時態）。

指令層（command :38001，有 router＋gating）：
- `POST /api/command/group/{id}/execute` — 兩階段提交（§3）。**非同步啟動**：
  嚴格 gate 通過後**立即回 202**＋群組 handle，背景 task 跑序列（逐台 upload
  回讀→全 arm→廣播 start），過程逐步寫入群組即時態。gate 失敗則同步回 409＋
  逐台原因（未啟動序列）。
- `POST /api/command/group/{id}/abort` — **操作員主動撤銷**（緊急全撤鈕）。
  冪等、單擊、依當前 phase 自動選動作：UPLOADING→停序列；ARMING/已 arm 未 start
  →全 disarm；FLYING→RTL-all。與 §3「伺服器偵測失敗自動全撤」是兩條路徑
  （自動 vs 操作員主動），終態一致。
- `POST /api/command/group/{id}/rtl` — 群組 RTL-all（空中緊急）。冪等。

**執行語意（013 定案，明寫）**：
- **排程原子性全在伺服器端**。execute 非同步啟動後**前端斷線/關頁不影響**——
  不會因 HTTP 斷掉卡半途留半 arm 機隊；序列由伺服器跑到終態或自動全撤（§3），
  中止只能透過 abort 端點，不是斷 HTTP。
- **進度**：不需 WS——`GET /api/groups/{id}` 即時態逐步推進，前端 **1s 輪詢**
  看逐台 phase 即可。
- **逐台錯誤形狀**：execute 逐步結果與 GET 即時態的 per-member 錯誤，沿用既有
  結構化 `{msg, hint, autopilot_notes}`（同單機拒絕），UI 原文顯示、不猜。

> 這組即 019 MCP `submit_mission(N 台)` 的地基：execute 是多機版 submit_mission，
> 實機時進 `pending_approval`→操作員前端確認才飛，SITL 直飛（019 定案）。
> §7 契約已與前端四端點提案收斂（2026-08-11）。

### 7.1 狀態枚舉（前端照此畫狀態視圖，不猜字串）

**群組 `group.status`**：`draft`（建好未執行）→ `pending_approval`（實機待審批）→
`executing`（序列進行中）→ `flying`（全機起飛）→ `completed`（全落地上鎖）；
終態/中止：`gate_rejected`（嚴格 gate 擋）、`aborting`→`aborted`、
`partial`（空中單機 RTL、其他續飛）。

**每台 `assignment.phase`**：`idle`→`uploading`→`uploaded`→`arming`→`armed`→
`starting`→`flying`→`landed`；異常：`upload_failed`、`prearm_failed`、
`rejected`（capability 非 ok）、`rtl`（該台返航中）。每個異常態帶
`error?: {msg, hint, autopilot_notes}`。

## 8. 模式入口（PM 裁決：漸進顯示）

編隊/群組入口採**漸進顯示**——**≥2 機連線才出現**，非面板頂常駐切換。單機時
UI 維持現狀（單機指令面板），不增認知負擔。

## 9. 切批（實作順序，013 依賴 011 先過）

| 批 | 內容 | 前提 |
|---|---|---|
| 0 | 多機 PX4 SITL 環境探路 | — |
| 1 | issue 011 多機驗收（demux/逐台架次/並發指令/撞號告警） | 批0 |
| 2 | 013-A：資料模型＋統一路徑地面生成＋check_group | 批1 |
| 3 | 013-B：兩階段執行器＋狀態機＋群組 RTL | 批2＋**≥2 台真 PX4 SITL**（驗 arm ACK 時序與同時起飛真實行為；假機不跑物理不夠）|
| 4 | 013-C：submit_mission（併 019 MCP，實機 pending_approval） | 批3＋019 |

**013 不建議跳過 011**（單埠多機路由基礎未驗，群組執行無意義）。

## 10. 收官（2026-08-11）

**013 宣告收官**。交付與驗證狀態：

- **013-A**（資料模型＋統一路徑地面生成＋check_group）：完成。
- **013-B**（兩階段執行器＋狀態機＋execute/abort/rtl）：完成。骨架 in-process 14/14；
  前端**真機確認輪通過**（1真+2假：execute→逐台 phase→空中 abort→三台 RTL→全撤）；
  **單真機時序驗收**：alt-reached gating 真爬升驗過（starting 期實際 0→10m、到 80% 才切
  MISSION）、arm-ACK 細粒度（真 PX4 arm 16ms／takeoff 8ms／mode 12–16ms 單次 ACK）、
  真機 prearm 失敗路徑（TEMPORARILY_REJECTED→prearm_failed→自動全撤）、RTL 落地。

**兩項時序驗收於多機模擬環境完成（2026-08-12，3 台真 PX4 SITL）**：
- **起飛 skew — PASS**：建群→執行端到端跑通，takeoff 指令 skew 實測 0／+12／+27ms
  （~13ms/台、總 27ms），優於 011 的 +25ms/台預期、落在「數十 ms、量測用途可接受」內；
  arm skew 同量級（0／15／41ms）。同時起飛三步式（全體 takeoff→全體到高度→全體 MISSION）
  真機行為正確。
- **群組 RTL 分離 — 以 vsep 驗證通過、rtl_stagger 列已知限制（§10.2）**：三機 RTL-all
  實飛返航高度 29.9／34.9／39.9m（差 5m，安全不匯合），分離來自 vsep 分層＋PX4 高於
  return alt 維持高度。顯式 `GROUP_RTL_STAGGER_M` 未實作。

**013 全案收官（2026-08-12）**。多機模擬環境已交付（3×PX4 SITL＋單埠 demux＋逐台
link_sim），swarm_sim 開發鷹架退役（011 close）。

### 10.1 多機模擬環境（方向，scope 討論中）

使用者 2026-08-11 提出方向：「模擬多台無人機**同時操控與資料蒐集**」，將成為下一條主線。
**規模/保真度/路徑（N 台上限、bridge net＋埠重映 vs PX4 原生 multi-vehicle、逐台鏈路資料
生成）由 PM 與使用者討論定案後正式派工**——此處僅記方向，未定稿。已知要點：各實例唯一
MAV_SYS_ID、單埠 demux 接現有 backend/command；link_sim 需擴成逐台生成（否則僚機無研究
資料）；起停腳本化。**此環境已於 2026-08-12 交付**（3×PX4 SITL＋單埠 demux＋逐台
link_sim＋起停腳本 `sim-fleet/fleet.sh`），013-B 剩兩項時序（skew、RTL 分離）已在其上收（見上）。

> **011 收尾（swarm_sim 運動學僚機退役）——2026-08-12 執行**：多機 SITL 環境交付＝退役
> 條件達成。前端無 /swarm 入口（僅 backend 端點），故直接拆 `swarm_sim.py`＋`/api/swarm/*` 端點。

## 10.2 已知限制：separate 模式緊急 RTL-all 返航高度分離

`GROUP_RTL_STAGGER_M`（顯式逐台錯開 RTL 返航高度）**未實作**（`group_exec.rtl()` 純
RTL-all、mav 層無 param-set job）。影響僅限 **separate 模式且各機任務規劃為相同高度**：
緊急 RTL-all 時各機以相同高度往同一 home 匯合，返航高度分離無保證——PX4「高於 return alt
維持高度」是特性非保證，且低於 return alt 即失效。**unified 模式不受影響**（vsep 分層已
提供返航分離，實飛驗證 29.9／34.9／39.9m）。

- **操作規避（已入部署檢查清單）**：separate 編隊任務請規劃**不同飛行高度**。
- **未來項**：RTL 前逐台 param-set `RTL_RETURN_ALT`＝base＋layer×`GROUP_RTL_STAGGER_M`
  （需 mav 層加 param-set job；使用者 2026-08-12 裁決現階段不補、文件化限制）。
