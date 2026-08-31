# 026 · 自駕儀驅動層抽象：把廠牌差異收進獨立驅動，系統只呼叫抽象動詞

- 狀態：**in-progress（架構規劃階段）**——2026-08-12 使用者拍板「要做抽象層，
  先規劃系統架構」。後端出 `doc/autopilot-driver-architecture.md` 提案
  （四個待決點各附建議方案）→ 使用者定案 → 才實作。
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
| 4 | ~~手動前置模式~~ | ~~POSCTL~~ | ~~LOITER／POSHOLD~~ | **已隨搖桿移除（035）** |
| 5 | 任務上傳 | seq 0..N-1 | **home 佔 seq 0、真航點從 1 起** | command/mav.py `job_upload_mission` |
| 6 | 遙測串流 | 預設就送 | **必須送 `REQUEST_DATA_STREAM` 且要補送** | backend/mavlink_rx.py |
| 7 | 可飛判斷 | SYS_STATUS PREARM 位 | **不支援**，原因走 STATUSTEXT | backend readiness |
| 8 | EKF 健康 | `ESTIMATOR_STATUS` | `EKF_STATUS_REPORT` | backend readiness |
| 9 | **空白參數慣例** | NaN＝「用當前值」 | **對 NaN 不回 ACK、指令靜默丟棄**；0＝當前位置 | command/mav.py `job_takeoff` |
| 10 | ~~搖桿來源檢查~~ | ~~不檢查 GCS sysid~~ | ~~只信 `SYSID_MYGCS`~~ | **已隨搖桿移除（035）** |

> 第 8 項是查第 7 項時**順帶挖到**的，而且有連鎖效應：不修它，ArduPilot 會
> 永遠顯示「遙測不足」——**看起來像我們的誠實修正，實際上是我們沒解那個訊息**。
> 「誠實回報不知道」與「資料就在那裡只是沒解」是兩回事。這種**一個差異點掩蓋
> 另一個差異點**的情況，正是分散式分支難以察覺的失效模式。

~~外加機端前提設定差異（`SYSID_MYGCS`）~~——**已隨 [035](035-remove-manual-control.md)
移除**。值得記下的是它消失的方式：**不是被抽象層吸收，是整個功能離開了系統範圍。**
縮小範圍與提高抽象是兩種不同的解法，前者更徹底。

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

1. **驅動放哪裡** — ✅ **已定案（2026-08-24）：放在機上代理。**

   使用者定案原話：「**agent 才是真正的飛行指令控制者跟自動飛行路徑的傳達者
   與監控者，不應該讓地面站記住所有無人機的操作方式**」，並同時界定地面站
   範圍為「載入／執行／中斷／中途更改飛行路徑，並絕對確保飛行過程的安全操作」。

   這是原本兩個選項（程序內模組／地面站上的獨立程序）之外的**第三個位置**：
   驅動跟著飛機走，每台機自己帶著「怎麼操作我」的知識。

   **順帶解掉待決點 2**：地面站不再持有驅動，backend 與 command 就沒有
   「兩個 build context 怎麼共用一份驅動」的問題了。

   **但它打開了新的待決點（見下方 5、6）**，而且比原本的問題大——
   不要以為定了位置就等於定了設計。
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

5. **地面站與代理之間講什麼**（待決點 1 定案後新生）。這是真正的分水嶺：
   - **甲：仍講原始 MAVLink**（現況）。代理只做轉發＋在地決策。實作最小，
     但**地面站仍需解讀方言**才能顯示模式名、判斷就緒——差異點 2、7、8
     全都留在地面站，「不持有操作方式」只做到一半。
   - **乙：講抽象協定**。代理把遙測正規化（`mode: "hold"` 而非 `custom_mode: 4`）、
     把意圖翻成方言。地面站真的不需要知道廠牌，但這會動到
     [014](014-two-tier-collection.md) 的前提——**原始層全量收集要的正是未經
     正規化的原始流**。可能的形狀是「原始流照舊送，另加一路正規化摘要」，
     等於兩種都送。
   - ✅ **裁定（2026-08-31 使用者）：乙，講抽象協定。**
     代理把遙測正規化、把意圖翻成方言，地面站真的不需要知道廠牌。
     > **這條有一部分已經是既成事實**：意圖協定的 `state` 訊息現在就在送
     > 正規化的東西（`state: "FLYING_MISSION"`、`derived`、`rc_link`、
     > `mission_seq`），而原始 MAVLink 流照舊進 backend 入庫給 014 的原始層。
     > 那正是本條當初猜的「原始流照舊送，另加一路正規化摘要」的形狀——
     > **所以搬家不是從零開始，是把已經在跑的那條路正名並補齊**。
     > 補齊的部分是：模式名與就緒判定（差異點 2、7、8）目前仍在地面站解讀。
     - 前置解除：原本寫著「在決定之前不要先寫代理端的驅動」，現在可以寫了。

6. **驅動版本與機隊漂移**（待決點 1 定案後新生）。驅動跟著飛機走，意味著
   升級驅動＝逐台更新，機隊裡可能同時存在不同版本。需要：代理自報驅動版本、
   地面站看得見版本差異、以及「版本不合就降級為唯讀」之類的守則。
   這是原本放在地面站時不存在的問題。

## 與其他線的關係

- **019 MCP**：任務層的「agent 不需要無人機知識」，在多廠牌下**只有驅動層能保證**
  ——否則抽象會在「哪一種機」這裡漏。驅動層是 MCP 承諾的地基。
- **015**：本議題是 015 步驟 3（指令層抽象）的正式化與擴大。
- **capabilities 四態**：見上第 3 點，一致性測試可讓它從人工判斷變成自動判定。

## 進度

### B0 backend 端方言收斂（2026-08-12 完成）

**做了什麼**：backend 的廠牌差異原本散落在 `mavlink_rx.py`（模式表、串流補送、
EKF 訊息名）與 `state.py`（就緒原因文字），全部收進新檔 `apps/backend/app/dialect.py`
——與 command 端 `mav.py` 的 `dialect()` 對應，命名刻意對齊。

**QGC 對照帶來的分類**（§5.3）：收進去時不是收成一坨，而是**分兩類**：

| 類別 | 內容 | 去向 |
|---|---|---|
| **§1 訊息層** | 同一件事、不同訊息名（EKF：差異 8） | 未來 `Driver.adjust_incoming()` |
| **§2 解讀層** | 同一個值、不同意義（模式 2、串流 6、就緒 7） | 未來 `Driver.decode_mode()` 等 |

判準：**需要知道「值代表什麼意思」的，就不屬於訊息層。** 這一刀讓 `adjust_*`
不會退化成什麼都往裡塞的垃圾抽屜（PM 裁決：此句入 B1 規格）。

**過程中查出的實質風險（不是搬運，是新發現）**：`EKF_STATUS_REPORT` 當
`ESTIMATOR_STATUS` 讀這件事，**只在 bit 1..512 成立**：

    bit 1..512   兩邊逐位同名同義                          → 可互換
    bit 1024     ESTIMATOR_GPS_GLITCH vs EKF_UNINITIALIZED → **同位元不同意義**
    bit 32768    EKF_GPS_GLITCHING（ArduPilot 專有）

我們目前只用 1|2|16，全在安全區內，所以今天沒事。但這正是「寫的時候是對的、
用的時候沒人記得範圍」的典型——日後有人讀第 1024 位不會報錯，只會靜默給出錯的
答案。已用 `EKF_ALIAS_SAFE_BITS` 常數＋測試釘住。

**順帶修掉的方言洩漏**：`state.py` 的就緒原因寫死「PX4 預檢未過」。ArduPilot
不回報 PREARM 位、`prearm_ok` 恆為 None，所以這行今天永遠不會對 ArduPilot 觸發
——**但那是「條件剛好沒踩到」而不是「寫對了」**。改成 `dialect.prearm_label()`
依實際廠牌取名。

**零回歸驗證**（四台混機實跑，PX4 ×3 ＋ ArduPilot ×1）：

| 檢查 | 結果 |
|---|---|
| 模式解讀 | sysid 1/2/3 = LAND/LAND/HOLD、sysid 10 = LAND（ArduPilot Copter 9）✔ |
| ArduPilot `ekf_ok` | `True`——**證明合併後的 EKF 分支仍吃 `EKF_STATUS_REPORT`** ✔ |
| capabilities | PX4 9/9、ArduPilot 7/9，與 B0 前逐鍵相同 ✔ |
| `/api/drones` autopilot | 四台正確（走改過的 import）✔ |
| `test-readonly-boundary` / `test-json-safe` | 均 OK（未被波及）✔ |
| `test-dialect-boundary`（新） | OK |

新增測試 `scripts/test-dialect-boundary.py`，沿用 read-only 白名單的同一手法。
它有一條**反向檢查**：若安全區外哪天不再有分歧位元，測試會失敗要求放寬常數
——留著一個過度保守又沒人知道為什麼的常數，比沒有還糟。

`mavlink_rx.py` 不再持有任何廠牌表，**也不轉出** `dialect` 的名字（留轉出等於
留下第二個看似權威的位置），測試第 8 項釘住這點。

### B1／B2 完成（2026-08-12）

- **B1**：介面定義 13 成員（`libs/autopilot/driver.py` ＋ `doc/autopilot-driver-interface.md`）。
  兩條教訓做成**型別**而非註解：`MessageEquivalence` 強制帶適用範圍、`Limit` 在
  建構期擋掉「confidence=unverified 卻有數字」。
- **B2**：兩端切到共用驅動層，`libs/` 進 repo 根 build context。零回歸——
  capabilities 逐鍵相同（含 reasons 逐字）、ready／mode 逐台相同，**設計師獨立
  跑他自己的基準線 diff 也全綠**。等價測試改為對照 git 基準點 `41471cc`，
  從一次性搬遷驗證變成長期回歸護欄。

### B3 進行中（2026-08-12 停在此）

**已完成**：測試套骨架（`scripts/conformance/`）＋六條測項。

| 測項 | px4 | ardupilot | 覆蓋的能力鍵 |
|---|---|---|---|
| `mode_set` | pass | pass | hold／rtl／land |
| `mission_upload` | pass | pass | mission_upload |
| `manual_failsafe` | pass | pass | manual（之一） |
| `takeoff` | pass | pass | arm／takeoff |
| `manual_stick` | **skip** | pass | manual（之一） |
| `mission_fly` | pass | **skip** | mission_start／mission_fly |

`dryrun.py` 對照「現況 vs 改由測試推導」：**缺口從 8 降到 1**。
**gating 尚未切換**（規則：清單為空才切）。

跑測逼出三個測試套自身的缺陷，都已修——其中第一條最重要：

> **機端拒絕 ≠ 方言錯誤。** 電池不健康而拒絕解鎖被記成 `fail`，那是**誣賴驅動**，
> 而在能力值改由測試推導之後會讓一個好好的動詞被鎖住。這正是本套測試開宗明義
> 宣告要分清的「一致性測試 vs 執行期前提檢查」——**設計時寫下的區分，實跑才
> 發現自己沒做到**。

另外兩條：`skip` 不得覆蓋既有 `pass`（skip 是「這次沒新證據」，不是「舊證據作廢」）；
能力 gating 擋下要單獨分類（記成 fail 等於宣稱「驗過而且壞了」）。

### 兩個未決項（下一段的起點）

**(A) 能力 gating 的 bootstrap 雞生蛋。** 動詞是 `unverified` → API 拒發 →
一致性測試跑不了 → 永遠拿不到證據。**刻意不繞過**（繞過等於測一條產品上不存在
的路徑）。使用者定案：做一個「**一次性人工放行**」機制——每次 trial 都要明確
放行（不是給測試身分常設權限）、**只對 `is_simulated` 機**、全程 `command_log`
留痕標 trial、結果落地即失效。設計先過目再實作。

**(B) PX4 airborne 進不了 POSCTL。** 空中持續回
`Switching to POSCTL is currently not available`，持續送設定點 25s 仍被拒；地面
切則正常。線索：產品的 `manual_start` 在 2026-08-11 已 SITL 驗證可用，而
`mav.py` 註解明寫「POSCTL 需要**已存在的手動控制串流**才會接受切換」——產品的
順序是**先建立串流、再切模式**。待比對測試序列與該順序的差異。
**「測試進不去」與「能力不存在」是兩回事**，未查明前不接受降級。

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
