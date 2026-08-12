# 026 · 自駕儀驅動層抽象：把廠牌差異收進獨立驅動，系統只呼叫抽象動詞

- 狀態：**needs-decision**（2026-08-12 使用者提議，先議不實作）
- 嚴重度：medium（架構債，隨支援機型數線性惡化）
- 位置：跨 `apps/backend/app/mavlink_rx.py`、`apps/command/app/mav.py`／`main.py`
- 建立：2026-08-12

## 提議（使用者原話大意）

ArduPilot 與 PX4 的差異是「實作面考量」，不該散在系統各處——**把相關功能
獨立成不同的工具／驅動，系統本身用抽象邏輯呼叫**。

## 為什麼現在值得議：差異點正在快速增生

015 驗收前我們以為只有 3 處方言差異；一天之內變成 **10 處**，而且分佈在兩個服務：

| # | 差異點 | PX4 | ArduPilot | 位置 |
|---|---|---|---|---|
| 1 | 設定模式 | DO_SET_MODE param2/3＝main/sub | param2＝模式號 | command/mav.py `PX4_MODES` |
| 2 | 解讀模式 | custom_mode 位元拆解 | custom_mode＝整數模式號 | backend/mavlink_rx.py `_mode_name` |
| 3 | 起飛序列 | arm→TAKEOFF（param7 絕對海拔） | 先進 GUIDED→arm→TAKEOFF（相對高度） | command/main.py `_do_takeoff` |
| 4 | 手動前置模式 | POSCTL | LOITER／POSHOLD | command/main.py manual |
| 5 | 任務上傳 | seq 0..N-1 | **home 佔 seq 0、真航點從 1 起** | command/mav.py `job_upload_mission` |
| 6 | 遙測串流 | 預設就送 | **必須送 `REQUEST_DATA_STREAM` 且要補送** | backend/mavlink_rx.py |
| 7 | 可飛判斷 | SYS_STATUS PREARM 位 | **不支援**，原因走 STATUSTEXT | backend readiness |
| 8 | EKF 健康 | `ESTIMATOR_STATUS` | `EKF_STATUS_REPORT` | backend readiness |
| 9 | **空白參數慣例** | NaN＝「用當前值」 | **對 NaN 不回 ACK、指令靜默丟棄**；0＝當前位置 | command/mav.py `job_takeoff` |
| 10 | 搖桿來源檢查 | 不檢查 GCS sysid | 只信 `SYSID_MYGCS`（預設 255），不符**靜默丟棄且無 ACK** | command/capabilities.py |

> 第 8 項是查第 7 項時**順帶挖到**的，而且有連鎖效應：不修它，ArduPilot 會
> 永遠顯示「遙測不足」——**看起來像我們的誠實修正，實際上是我們沒解那個訊息**。
> 「誠實回報不知道」與「資料就在那裡只是沒解」是兩回事。這種**一個差異點掩蓋
> 另一個差異點**的情況，正是分散式分支難以察覺的失效模式。

外加**機端前提設定**差異（ArduPilot 的 `SYSID_MYGCS` 要設 254，否則
`MANUAL_CONTROL` 被靜默丟棄）。

**每支援一個新廠牌，這張表就多一欄、每一處分支都要改。** 而專案目標明寫
「控制各種不同規格型號」（見 [multi-autopilot 目標](015-multi-autopilot-support.md)），
所以這是會持續惡化的債，不是一次性的。

## 提議的形狀（待議，非定案）

**抽象動詞層**——系統只呼叫「意圖」，不知道廠牌：

```
set_mode(logical)      # hold / mission / rtl / land / position
takeoff(alt)           # 含各家的前置序列
upload_mission(wps)    # 含各家的 seq／frame 慣例
start_mission()
rtl() / land() / hold()
manual_control(sp)     # 含各家的前置模式與 sysid 前提
on_connect()           # 各家的串流請求等初始化
decode_mode(custom)    # → 人話模式名
readiness(state)       # → ok / not_ready+原因 / null(依據不足)
capabilities()         # 本驅動宣告支援哪些動詞
```

驅動實作：`Px4Driver`、`ArduPilotDriver`、（未來）其他。
**系統其餘部分不得出現 `if autopilot == ...`。**

## 要先決定的四件事

1. **驅動放哪裡**：程序內模組 vs 使用者說的「獨立工具（獨立程序）」。
   - 程序內模組：最簡單，抽象效益已經拿到。
   - 獨立程序：對**非 MAVLink 機型**（DJI 等需要 vendor SDK）幾乎是必然——
     那類無法共用 MAVLink 程式碼。可能的結論是**混合**：MAVLink 家族走
     程序內驅動，非 MAVLink 走獨立 adapter 程序（原 015 步驟 5 的設計）。
2. **跨服務怎麼共用**：backend（解碼）與 command（編碼）是兩個服務、
   **build context 各自獨立**（先前模式表就撞過這道牆，當時的決議是
   「canonical 表＋副本＋drift-guard 斷言測試」）。驅動層會讓這個問題變大，
   選項：共用 package／repo 根 build context／維持副本＋一致性測試。
3. **一致性測試套（conformance suite）**：把 015 的驗收清單變成**每個驅動都要通過的
   同一套測試**。這會讓 capabilities 的「四態」有客觀依據——
   **某動詞的能力標成 `ok`，當且僅當該驅動的對應測試通過**，而不是人工判斷。
4. **時機**：現在抽 vs 先修完 ArduPilot 再抽。
   - 現在抽：只有 2 個實作，抽象容易抽錯（過早抽象）。
   - 先修再抽：ArduPilot 的修正（任務上傳 seq、串流請求）會再增加分散的分支，
     之後要搬。
   - **建議折衷**：先修 ArduPilot，但**把每個服務的分支收斂到單一位置**
     （不再散落），抽驅動時就是把那一處提取出來，不是全域搜捕。

## 與其他線的關係

- **019 MCP**：任務層的「agent 不需要無人機知識」，在多廠牌下**只有驅動層能保證**
  ——否則抽象會在「哪一種機」這裡漏。驅動層是 MCP 承諾的地基。
- **015**：本議題是 015 步驟 3（指令層抽象）的正式化與擴大。
- **capabilities 四態**：見上第 3 點，一致性測試可讓它從人工判斷變成自動判定。

## 解決方式

（closed 時補）


## 附記：NaN 在每一層邊界都有不同的爆法（2026-08-12 一天內踩三次）

同一個 NaN，跨不同邊界的失敗方式完全不同——**而且沒有一次是在產生它的地方爆的**：

| 邊界 | 症狀 | 發現方式 |
|---|---|---|
| **瀏覽器 JSON.parse** | 裸 `NaN` 讓**整包** JSON 解析 throw，前端每則訊息都解不開 | 前端回報 |
| **PostgreSQL JSONB** | `Token "NaN" is invalid` 直接拒收——**參數快照從頭到尾沒寫進去過一次** | 端到端補驗才發現（若只看「程式路徑都在」會漏掉） |
| **MAVLink 指令參數** | ArduPilot 對 NaN 參數**連 ACK 都不回**，指令靜默丟棄 | SITL 實測 |

前兩者已由 `app/jsonsafe.py` 一處收斂＋測試釘住（見 `scripts/test-json-safe.py`）；
第三者屬方言差異（表中第 9 項）。**教訓：NaN 是「本地看起來沒事、到邊界才炸」的
典型，而每層邊界的爆法不一樣——所以要在每個跨界點都當它是不合法值處理，不能
靠「上游應該不會給 NaN」。**
