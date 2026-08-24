# 037 · `.plan` 自報的目標機種被完全忽略

- 狀態：in-progress
- 嚴重度：**high**（會讓錯的航線靜默上到機上，且事後查不出原因）
- 位置：`apps/frontend/app/missions/page.tsx`（匯入）、`apps/backend/app/api.py`
  （入庫）、`apps/command/app/main.py`（上傳前比對）、`plans.py`（外部觸發）
- 建立：2026-08-24

## 現象

使用者問：「現在的 plan 檔應該會根據不同飛控或不同機型而不同吧，這樣要標示
相關資訊吧，不然使用者永遠都不知道問題出在哪。」

查證結果：**`.plan` 檔本來就帶著這個資訊，而我們一個字都沒讀。**

    complex-survey.plan        firmwareType=12  vehicleType=2
    interference-survey.plan   firmwareType=12  vehicleType=2

`firmwareType=12` 是 `MAV_AUTOPILOT_PX4`。而 repo 裡這兩份任務檔目前唯一會飛到的
機是 **ArduPilot**。全庫 grep `firmwareType|vehicleType` 的結果是 **0 個讀取端**。

## 為什麼這件事會靜默出錯

危險的地方在於它跟一個**正確的設計原則**撞在一起。`build_items()` 的註解寫著：

> 明給的 frame 一律照送（MAVLink 保真度）；沒給才依指令推。**不覆寫使用者/`.plan`
> 的顯式值**——那是保真度的核心

這個原則對。但它的後果是：**照 A 家語意寫出來的顯式 `frame` 與 `params`，
會原封不動送給 B 家的機。** 兩家的差異見 [026](026-autopilot-driver-abstraction.md)
的差異點表，與任務直接相關的至少三項：

| 差異 | PX4 | ArduPilot |
|---|---|---|
| 任務線序 | seq 0..N-1 | **home 佔 seq 0**，真航點從 1 起 |
| 無座標項的 frame 預設 | — | 錯了會讓含 RTL 的任務整份上不去（[029](029-mission-frame-default-breaks-rtl.md)）|
| 空白參數慣例 | NaN＝用當前值 | **對 NaN 不回 ACK、指令靜默丟棄**；0＝當前位置 |

[029](029-mission-frame-default-breaks-rtl.md) 就是這個家族的問題，只是那次是
**我方推錯**；這次是**來源本來就不同**，而且我們連「來源不同」都看不見。

使用者那句「不然使用者永遠都不知道問題出在哪」正是重點：任務上傳失敗或飛出
奇怪的行為時，**沒有任何線索指向「這份檔案是給另一種飛控寫的」**。

## 修法：示警但放行（使用者定案 2026-08-24）

**不硬擋**——多數航點跨家其實飛得動，硬擋會逼人去改檔案繞過，反而更糟。
但必須**在上傳前就講**，而不是讓他用失敗去發現（ui-spec §0.2c 條款 6）。

1. **存下來**：`missions` 加 `firmware_type`／`vehicle_type` 兩欄（值域就是
   `MAV_AUTOPILOT`／`MAV_TYPE`，**與 HEARTBEAT 同源**，所以可以直接比對）。
   `NULL`＝這份任務沒說（手繪、舊資料）。
2. **三條入庫路徑都帶上**：UI 匯入 `.plan`、外部觸發（`POST /api/start` 走
   `plans.py`）、從機上讀回（`from-vehicle`——機種就是那台機，不必猜也不該留空）。
   **外部觸發那條特別重要**：漏掉它，自動化流程會是唯一沒有警示的入口。
3. **選檔時就看得到**：任務卡片顯示目標機種 chip。
4. **上傳前比對**：不符時把警告**併進既有的 `check.warnings`**，不自成欄位——
   前端已經會顯示那份報告，多開一個欄位就多一個可能沒人接的顯示點。
   併進 `warnings` 也不影響 `ok`，不會意外觸發 `GEOFENCE_ENFORCE` 的擋門。

### 呈現上的一個細節

認不得的 enum 值**原樣顯示 id**（`firmware 7`），不寫「未知」——「未知」會讓
**「檔案沒說」**與**「說了但我們沒收錄這個型號」**看起來一樣，那是 ui-spec §0.2e
禁止的同一件事。檔案沒說時**不顯示 chip**：空白代表「沒說」，不是「通用」。

## 驗證（實跑）

建立一份宣告 `firmware_type=12`（PX4）的任務，上傳到 ArduPilot 機：

    上傳結果 : 5 verified=True          ← 照常上傳（示警不擋）
    warnings : 這份航線宣告是給 PX4 寫的，這台機是 ArduPilot
               ——航點的 frame 與參數語意可能不同（見 issues/037）

`plans.py` 對現有兩份檔案解析出 `firmware_type=12 / vehicle_type=2`。

> 驗證過程**實際把一份 5 項任務寫進了機上飛控**（機體上鎖、無 GPS、
> RC failsafe 中，無法解鎖）。飛控原本的 `mission_seq` 是 `None`。

## 尚未做

- **機種資訊只到「宣告」層級**：我們比對的是檔案自己說的，不是實際分析航點內容
  推斷出來的。手繪或來源不明的任務仍然沒有機種資訊，也就沒有警示。
- 差異點本身**沒有被翻譯**——只警示不轉換。真正的解法是 026 的驅動層
  （已定案放在機上代理），那才會把 A 家語意翻成 B 家。

## 解決方式

（closed 時補：commit hash）
