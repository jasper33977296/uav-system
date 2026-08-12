# 015 · 跨自駕儀支援：command/backend 硬編碼 PX4 方言，非 PX4 機無法控制

- 狀態：open
- 嚴重度：high
- 位置：`apps/command/app/mav.py:30`、`apps/command/app/main.py`（_do_takeoff）、`apps/backend/app/mavlink_rx.py:52`
- 建立：2026-08-11

## 現象

2026-08-10 實測：能控制的無人機非常有限——實際上只有跑 PX4 的機（RB5）。
專案目標是控制各種不同規格型號的無人機。

## 原因

實作綁定 PX4 私有慣例，完整分析見 `reference/gap-analysis.md`（規格文件快照
在 `reference/`）。三個關鍵硬編碼：

1. `PX4_MODES`（mav.py:30）把 DO_SET_MODE 的 param2/3 編成 PX4 main/sub；
   ArduPilot 的 param2 是自家模式號 → 我方 hold/rtl/land 在 Copter 上全變
   GUIDED、position 變 AUTO（**誤觸危險模式，不只是失效**）。
2. 起飛序列（main.py）＝arm→NAV_TAKEOFF（param7 絕對海拔）；Copter 須
   GUIDED→arm→takeoff 且 param7 是相對高度。
3. 模式解讀（mavlink_rx.py:52）以 custom_mode>>16 拆 PX4 union；
   ArduPilot 的 custom_mode 是整數模式號 → UI 模式名全錯。

另有：deadman 失聯降級送 PX4 Hold（Copter 收到變 GUIDED）、ArduPilot 預設
只信 SYSID_MYGCS=255 的 MANUAL_CONTROL（我方 254 會被靜默丟棄，待驗證）、
ArduPilot 任務 seq 0＝home 的回讀位移（待驗證）。

## 影響

- 擋住「多機型機隊」的核心研究目標；非 PX4 機完全不可控。
- 部分指令在 ArduPilot 機上是**飛安風險**（誤切 GUIDED/AUTO），在修好前
  不應對 ArduPilot 實機下模式指令。

## 修法建議

照 `reference/gap-analysis.md` §建議的落地順序：

1. 機型盤點（等使用者提供 2026-08-10 失敗機型清單，分 PX4／ArduPilot／非 MAVLink）。
2. HEARTBEAT autopilot+type 偵測入庫，全鏈路帶著走。
3. 指令層抽象：優先用可攜指令（NAV_RETURN_TO_LAUNCH/NAV_LAND/
   NAV_LOITER_UNLIM/MISSION_START），剩餘按 autopilot 分表
   （起飛序列、手動前置模式、deadman 降級）。
4. ArduPilot SITL 過同一套驗收（含三個「待驗證」項）。
5. 非 MAVLink 機型另立 adapter 設計。

## 解決方式

（closed 時補）

## ArduPilot SITL 驗收結果（2026-08-12 實測）

環境：ArduCopter SITL（sysid 10）**接進機隊的單埠 fanout**，與 3 台 PX4 混跑
——驗的是「多廠牌混機共存」，不只是「ArduPilot 單獨能跑」。混機的 sysid demux、
逐台 autopilot 判定、逐台 capabilities 全部正常。

### 🔴 最大的發現（原本不在待驗證清單裡）

**ArduPilot 預設幾乎不送遙測。** 我方只收到 4 種訊息：`HEARTBEAT`／`PARAM_VALUE`
／`STATUSTEXT`／`TIMESYNC`——沒有位置、GPS、電量、SYS_STATUS。送一次
`REQUEST_DATA_STREAM` 之後變 **32 種**。也就是說，接一台真的 ArduPilot 機，在修正
前會是「**連得上但等於瞎的**」（心跳有、清單看得到、但沒有任何遙測）。

PX4 預設就串流，所以這件事在只測 PX4 的時候永遠不會暴露——**這是「只測過一種
自駕儀就以為理解了協定」的典型代價**。

修正：`SEND_WHITELIST` 加 `REQUEST_DATA_STREAM`（唯讀請求），註冊後送出並**每 30
秒補送**（串流率設在自駕儀端，機端重開／換通道／我方重連之後就沒了，只送一次會
靜默失去全部遙測）。實測：重啟 ArduPilot 後遙測**自動恢復**（34 種訊息）。

### 三個待驗證項的答案

| 項目 | 結果 |
|---|---|
| **A. 任務 seq 0＝home** | **確認。**<br>**協定層原始行為**（用獨立腳本繞過產品程式觀察）：上傳 3 個航點，回讀**筆數仍是 3**，但 seq 0 被機端 home 覆蓋（frame=0 絕對高度）、**我方第一個航點消失**——筆數不變，所以只比筆數的檢查抓不到。<br>**產品層實際症狀（更正 2026-08-12）**：`job_upload_mission` 的回讀比對**本來就是逐項比座標**（不是只比筆數），所以它會在 seq 0 比對不符時 raise「回讀比對不符」→ **ArduPilot 上傳一律大聲失敗，不是靜默吃掉航點**。訊息看起來像機上內容錯亂，仍然不能用，但飛安意義與「靜默遺失」完全不同。<br>**已修（`de339c0`）**：`mission_dialect()` 方言分支——home 佔 seq 0、真航點從 seq 1 起（線上 count=N+1），回讀跳過 seq 0 的內容比對、其餘對齊偏移後照常逐項比。PX4 路徑一個位元組未變。實測 PX4 `wire_items=4`／ArduPilot `wire_items=5`，兩者 verified=true。 |
| **B. `SYSID_MYGCS`** | **確認不相容**。機端值 255，我方 GCS sysid 254 → 我方 `MANUAL_CONTROL` 被**靜默丟棄**（無錯誤、搖桿純粹沒反應）。解法：機端設 `SYSID_MYGCS=254`（機端前提設定），或我方 sysid 做成每機設定。 |
| **C. PREARM 位** | **確認不支援**。ArduPilot 的 SYS_STATUS `PREARM_CHECK present=False`（PX4 是 True）→ **不能用 SYS_STATUS 對 ArduPilot 做可飛判斷**，它的預檢原因走 STATUSTEXT。 |
| **D. 可攜指令** | **全部 ACCEPTED**：`NAV_LOITER_UNLIM`（Hold）／`NAV_RETURN_TO_LAUNCH`（RTL）／`NAV_LAND`（Land）。任務上傳握手本身也正常（ACK=0）。 |

### 第四個方言差異（查 C 時順帶挖到）

**EKF 健康回報的訊息名不同**：PX4 發 `ESTIMATOR_STATUS`、**ArduPilot 發
`EKF_STATUS_REPORT`**。我們原本只解 PX4 那個，所以 ArduPilot 的 `ekf_ok` 永遠是
None——加上它不回報 PREARM 位，readiness 就永遠判不出來。補上解析後，ArduPilot
才有可飛判斷的依據（實測 ready 由 null 變成有依據的 true）。

### readiness 的連帶修正

原本「沒有反對證據」就回 `ready=true`，包括**什麼權威訊號都還沒收到**的情況
（ArduPilot 驗收機實測：prearm/ekf 皆 null 卻宣告就緒）。改為無權威依據時回
`null`＋理由。**GPS 好不算數**——GPS 定位良好但預檢未過完全可能。

### 尚未完成（capabilities 維持全鎖）

PM 判準：**「驗證過」要指「整條路徑可用」，不是「這三個指令會被 ACK」**。
順序：`REQUEST_DATA_STREAM`（✅ 已修）→ **ArduPilot 任務上傳修正（未做）** →
重跑驗收 → 才逐鍵開 `rtl`／`hold`／`land`。


## 2026-08-12 收盤狀態

**ArduPilot 能力：7 項 ok**（`mission_upload`／`hold`／`rtl`／`land`／`arm`／
`takeoff`／`manual`），皆為 SITL 實測驗過：
- 三個模式讀回 HEARTBEAT 確認 `mode_engaged=True`（不只 ACK）
- 起飛 GUIDED→arm→NAV_TAKEOFF 實測爬到 15.0m（相對高度語意正確）
- `manual` 走**事前讀 `SYSID_MYGCS`**（見下），非人工判定

**維持 unverified**：`mission_start`／`mission_fly`——AUTO 任務執行整段尚未驗。

**混機編隊**：ArduPilot 的 arm 已開，但編隊執行序列第 3 步要切 MISSION、
會用到 `mission_start`，**所以必須先驗 mission_start 才跑得動混機編隊**。

### `manual` 的解法可能是「機端前提」類問題的通用解

ArduPilot 只接受 `SYSID_MYGCS` 指定來源的 MANUAL_CONTROL，不符**靜默丟棄**；
而 `MANUAL_CONTROL` 沒有 ACK，事後完全偵測不到。原本要標「受限」，但那承諾了
我們兌現不了的失敗偵測。改為**連上時讀回該參數**（每 30 秒重讀）：等於 254 就開
`ok`，不等於就鎖住並在 reason 給出**現值與該改成什麼**。

已升為 ui-spec §0.2c 條款 6：**前提可事前查證時，就不要讓使用者用失敗去發現它。**

⚠️ **注意目前 SITL 的 `SYSID_MYGCS` 已被我改成 254**（為了驗證正向路徑），
所以現在看到 `manual=ok` 反映的是**被修改過的 SITL**，不是 ArduPilot 出廠預設
（預設是 255＝鎖住）。真機接入時仍須依部署文件設定該參數。
