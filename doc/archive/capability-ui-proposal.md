# 機型能力差異 UI 設計提案

- 狀態：**已實作**（main 9fac7e0，2026-08-11；含下方「實作偏差」兩處，設計方已接受）
- 作者：UI/UX（2026-08-11）
- 依據：[reference/gap-analysis.md](../../reference/gap-analysis.md)、[issues/015](../../issues/015-multi-autopilot-support.md)
- 範圍：即時頁任務控制面板（CommandPanel/ManualControl）、`/drones` 頁

## 問題

UI 目前把「控制能力」當成全機隊均質的常數：按鈕清單寫死、模式語彙寫死
PX4（POSCTL、AUTO.LOITER）、失敗時的提示欄位叫 `px4_notes`。實際上
（gap-analysis 結論）能力是**每機不同**的：PX4 機全能力、ArduPilot 機在
issue 015 修好前下指令有飛安風險、非 MAVLink 機完全不可控。UI 不呈現這個
差異，操作者就無從判斷「按下去會發生什麼」——這在會飛真機的系統裡不可接受。

## 設計原則

1. **能力是資料，不是版面**：按鈕清單、模式選單由每機的 capability
   descriptor 驅動，前端不寫死。新機型接入只改資料不改版面。
2. **不可用 ≠ 不可見**：不支援的動作以 disabled＋原因呈現，不直接隱藏——
   操作者要能學到「這台為什麼不能 X」，而不是懷疑 UI 壞了。
3. **未驗證視同不可用**：gap-analysis 顯示對 ArduPilot 下 PX4 方言指令會
   **誤觸危險模式**（RTL→GUIDED、position→AUTO）。所以 gating 的判準是
   「驗證過才開」，不是「理論上支援就開」。緊急按鈕也一樣——會誤觸的
   RTL 比沒有 RTL 更危險。
4. **語彙分層**：操作按鈕用**動作語彙**（返航、懸停、降落、手動）——這是
   跨機型穩定的使用者意圖；**模式名**（POSCTL/LOITER/GUIDED）只出現在
   狀態顯示，且按 autopilot 正確解碼。既有安全設計不變式照舊：
   危險操作兩段式、緊急單擊、色彩不單獨傳達語意。

## 能力四態模型

每機在 UI 上恆屬四態之一（狀態徽章＋面板行為）：

| 態 | 判定 | 面板行為 | 徽章 |
|---|---|---|---|
| 可控 | autopilot 已識別且該機型驗收通過（目前僅 PX4） | 全功能，現行行為 | `PX4`（中性灰底） |
| 受限 | autopilot 已識別、部分動作驗證通過 | 通過的動作可用；其餘 disabled＋原因 | `ArduPilot`＋「部分支援」 |
| 未驗證 | autopilot 已識別但該機型零驗收（今日的 ArduPilot） | **整個指令區鎖定**，橫幅說明原因＋連 issue；遙測照常顯示 | `ArduPilot`＋「僅觀察」 |
| 不可控 | 非 MAVLink（HEARTBEAT 從未出現、或機型註記為 vendor SDK） | 無指令面板；`/drones` 註記「需 adapter」 | `不支援` |

「未知」（剛連上、還沒收到 HEARTBEAT）暫歸「未驗證」呈現，避免閃現全功能
面板又收回。

四態全部由 per-action capabilities **前端推導**，後端不需額外的狀態欄位
（前端確認 2026-08-11）：不可控機無 HEARTBEAT、不會出現在 healthz.drones，
「無指令面板」自然湧現；「未驗證」＝所有 action 皆非 ok，此時收斂成一張
橫幅，不逐鈕列七條重複原因。

## 版面改動（wireframe）

### 1. 面板標題列：加機型徽章

```
┌─ 任務控制 ─────────────────────────────┐
│ sysid 1 · PX4 · 已解鎖              ▾ │   ← 徽章插在 sysid 與 armed 之間
└────────────────────────────────────────┘
```

多機 chip 列同步帶徽章：`[sysid 1 ·PX4] [sysid 7 ·Ardu]`——選機前就能
預期能力差異。

### 2. 未驗證機型：安全鎖定橫幅（取代指令區）

```
┌─ 任務控制 ─────────────────────────────┐
│ sysid 7 · ArduPilot · 僅觀察        ▾ │
│ ● 就緒 · LOITER · GPS 3D·14顆 · 87%   │   ← 狀態列照常（模式名已正確解碼）
│ ⚠ 此機型（ArduCopter）控制尚未驗證，   │
│   指令已鎖定——現行指令集對本機型可能   │
│   誤觸危險模式（詳 issues/015）。      │
│   遙測與紀錄不受影響。                 │
└────────────────────────────────────────┘
```

橫幅用警告色底、非紅色（不是故障，是刻意保護）；一句話講清楚
「為什麼＋影響範圍＋去哪看」。

### 3. 受限機型：逐鈕 gating

```
│ 飛行                                   │
│ [解鎖] [起飛]                          │
│ [RTL 返航] [Hold 懸停] [降落]          │   ← 降落＝現有 disabled 樣式（opacity 0.4）
│ · 降落：本機型待 SITL 驗證（015-4）    │   ← 原因行，同 not_ready_reasons 樣式
│ 手動（停用：本機型手動控制待驗證）     │
```

disabled 的視覺＝現有樣式（降透明度）＋原因行，**不在按鈕文字內加符號**
（12px 下 ⊘ 類字符易讀性差，且「符號＋透明度＋原因行」三重編碼過度）。
原因沿用 `hint-line` 樣式（未就緒原因逐條列的同一視覺語言），
不用 tooltip——戶外觸控環境 hover 不可靠。

### 4. `/drones` 頁：機型呈現

原稿為「機型欄」，實作改為每機卡片 drone-head 的**機型 chip**（該頁是
卡片不是表格；chip 與既有「主機/模擬」徽章同語言）。

**實作偏差（2026-08-11，設計方接受）**：

- `/drones` 只標機型名，**不標「（僅觀察）」**——能力是 capabilities 的
  即時狀態（可攜 RTL/Hold 驗證通過後會變成受限態），/api/drones 無
  capabilities 資料，寫死後綴會過時。能力狀態集中在即時頁面板呈現
  （單一真相）。若日後盤點需要，讓 /api/drones 帶 capabilities 摘要再補。
- 從未見心跳顯示「**未見 MAVLink 心跳**」而非「不支援（非 MAVLink）」——
  資料上分不出「非 MAVLink」與「還沒連線」，不斷言（誠實原則）。
  issue 016 的連線診斷落地後，此文案可再引導操作員區分
  「通道未通（去修設定）」與「協定不支援（等 adapter）」。

## 資料前置需求（後端／command）

| # | 需求 | 建議形狀 | 去處 |
|---|---|---|---|
| A | 自駕儀識別 | HEARTBEAT 的 `autopilot`＋`type` 解出 `autopilot: "px4"\|"ardupilot"\|…`、`vehicle_type` | `/healthz` 每機、`/api/live` 每機、**WS 遙測**（模式名解碼 `flight_mode` 依賴此，別只做 healthz）、drones 表（對應落地順序第 2 步） |
| B | 能力描述 | 每機 `capabilities: { arm|takeoff|land|rtl|hold|mission_start|manual: "ok"\|"unverified"\|"unsupported", reason?: string }` | **command 服務**生成（單一事實源＝實際會執行指令的服務；短期按 autopilot 查靜態表，長期改讀 AVAILABLE_MODES，UI 無感） |
| C | 中性欄位名 | `px4_notes` → `autopilot_notes`；**過渡**：前端先雙讀（`autopilot_notes ?? px4_notes`）一版後才移除舊名 | 錯誤回報結構（gap-analysis §7 已建議） |

B 放 command 服務而非前端寫死，理由：能力判定會隨驗收進度更新（「受限」
的邊界移動），該資訊屬於指令執行方；前端寫死會與 command 實際行為漂移。

**部署順序守則（前端回饋，最重要）**：前端 gating 必須 feature-detect——
healthz **沒有** `capabilities` 欄位時退回現行全功能行為，不得把「缺欄位」
當「未驗證」鎖死。否則前端先上線或後端回滾時，整個 PX4 機隊會被誤鎖。
今日機隊全 PX4，此 fallback 安全。

## 已定案（PM，2026-08-11）

1. **未驗證機型的緊急按鈕：全鎖**（含 RTL/Hold）——現行 RTL 對 Copter 是
   誤切 GUIDED，比不動危險。配套：「RTL/Hold 改可攜指令
   （NAV_RETURN_TO_LAUNCH／NAV_LOITER_UNLIM）」列為落地順序第 3 步優先項，
   驗證後開放緊急出口。
2. **非 MAVLink 機允許註冊**並標「不支援」——機隊盤點有研究管理價值，
   且未來 adapter 落地時資料列已在。

## 追加需求：連線狀態與協定能力分開呈現（PM 輸入，2026-08-11）

實測機 RB5 的問題在**平台連線層**（出廠廣播 :14550、埠寫死、sysid bug、
廣播不過 5G——issue 016、gap-analysis §0），不是方言。這揭示「不可控」
其實有兩種，操作員必須能分辨，因為處置完全不同：

| 種類 | 原因 | 操作員的下一步 |
|---|---|---|
| 通道未通 | 機上設定未配好（埠/sysid/路由） | 去修機上設定（可解） |
| 協定不支援 | 非 MAVLink／機型未驗證 | 等 adapter／等驗收（不可當場解） |

設計方向（下一版 wireframe 補）：機型徽章表達「協定能力」；連線健康另用
既有的失聯 UI 訊號源（healthz age_s、link_age_s）表達「通道狀態」——
兩軸正交，不混進同一個徽章。「看得到機但通道異常」與「根本看不到機」
在 /drones 頁需要不同的註記文案。
