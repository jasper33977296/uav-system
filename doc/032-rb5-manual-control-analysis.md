# 032 · 搖桿無法控制 RB5：spec 對照分析

- 日期：2026-08-13
- 方法：**逐項對照文件，不猜**。每條結論標明出處與證據等級。
- 結論分三類：**文件確認**（規格白紙黑字）／**SITL 可驗**（本地能重現）／
  **只能真機驗證**（本地無法判定，需 RB5 到場）

---

## 主假設（文件確認）

> **RB5 上 `COM_RC_IN_MODE` 的生效值讓實體遙控器「先到先得」，於是我方送出的
> `MANUAL_CONTROL` 被靜默忽略。**

### 證據一：PX4 參數規範

出處：PX4 1.14.3 build 的 `parameters.json`
（`/root/Firmware/build/px4_sitl_default/parameters.json`，本專案 SITL 映像內）

    COM_RC_IN_MODE  預設 = 3
      0 = RC Transmitter only     ← 只吃實體遙控器，MANUAL_CONTROL 完全不看
      1 = Joystick only
      2 = RC and Joystick with fallback
      3 = RC or Joystick keep first   ← **出廠預設：先到的那個來源勝出**
      4 = Stick input disabled

**`3` 的語意是關鍵**：不是「兩個都能用」，而是**哪個先來就用哪個、另一個之後
一律忽略**。

### 證據二：RB5 的官方設定流程要求綁實體遙控器

出處：`reference/rb5/Qualcomm-Flight-RB5-user-guide-pre-flight-setup.html`

> Once bound, power cycle the vehicle and restart QGroundControl, otherwise the
> radio channels will fail to show up. Now follow the on-screen instructions to
> calibrate the range and trims of your radio.
>
> Confirm RC Settings … “Flap Gyro” switch … Channel 6 Up: Manual Flight Mode /
> Middle: Position / Down: Offboard … “Aux2 Gov” switch … Channel 7 … Motor Kill Switch

也就是說**一台照官方流程設定好的 RB5，本來就有一支綁定並校正過的 Spektrum 遙控器**，
而且飛行模式與馬達切斷都掛在 RC 通道上。

### 證據三：為什麼 SITL 驗過卻不能轉移

本專案 SITL 的實際值（2026-08-13 參數快照）：

    COM_RC_IN_MODE = 1     ← Joystick only
    COM_RC_LOSS_T  = 0.5
    COM_RC_OVERRIDE = 1

**SITL 被設成「只吃 Joystick」，而且環境裡根本沒有實體 RC**——所以我方的
`MANUAL_CONTROL` 必然是唯一來源、必然生效。030 的實飛驗證（位移 37m）證明的是
「我方的送法對」，**不是「這個機制在有 RC 的機上會生效」**。

> 這正是 015 以來反覆出現的形狀：**SITL 驗證的邊界是「環境相同的部分」**。
> 一個在 SITL 恆為 1、在真機恆為 3 的參數，SITL 永遠測不出它的後果。

### 症狀吻合度

`COM_RC_IN_MODE` 造成的失敗是**完全靜默的**：MANUAL_CONTROL 照收、不回錯誤、
不進日誌，機就是不動。與使用者回報的「搖桿沒辦法真正控制」完全一致——
**如果是協定或路由問題，通常會有別的症狀**（連不上、其他指令也失效）。

---

## 逐項候選

| # | 候選 | 結論 | 等級 |
|---|---|---|---|
| 1 | `COM_RC_IN_MODE` | **主嫌**（見上） | 文件確認＋SITL 可驗 |
| 2 | voxl-mavlink-server 轉發範圍 | **無法判定**——本地無 ModalAI 該服務的文件 | 需補文件 |
| 3 | 我方 sysid 254 非 QGC 慣例 255 | **可排除**（PX4） | 文件確認 |
| 4 | 5G 抖動觸發 `COM_RC_LOSS_T` | 有可能但**非主嫌** | 只能真機驗證 |
| 5 | POSCTL 模式前提 | 與 backlog B-case 同源，**獨立問題** | SITL 可驗 |

### 候選 3 可排除的理由（文件確認）

MAVLink 規格（`reference/mavlink/common.xml` msg 69）：`MANUAL_CONTROL` 只有
`target`（要控制哪台），**沒有任何「來源必須是特定 sysid」的欄位或語意**。

掃過 PX4 1.14.3 的全部參數，**沒有任何一個以 GCS sysid 為條件閘門手動控制**
（找到的 GCS 相關參數是 `COM_DL_LOSS_T`＝連線遺失門檻 10s、`NAV_DLL_ACT`＝
遺失後動作，都與「誰有資格控制」無關）。

**「只信 `SYSID_MYGCS`」是 ArduPilot 的行為（issue 015 實測），不是 PX4 的。**
把它套到 PX4 上是跨廠牌誤植——這正是 026 驅動層要防的那類錯誤。

### 候選 4：不是主嫌，但要量

`COM_RC_LOSS_T = 0.5s`（出廠預設，文件確認）。我方送 20Hz（50ms 一則），
餘裕 10 倍。**但 5G 的尾端延遲不是常態分佈**——單次 >500ms 的抖動就會判 RC loss。

若這是主因，症狀應是**斷續可用**（動一下停一下），而不是完全不動。
使用者描述比較像後者，所以列為次要。**真機上量 MANUAL_CONTROL 的到達間隔分佈**
才能判定。

### 候選 5：與主線無關，但要一起修

PX4 在空中拒絕進 POSCTL（`Switching to POSCTL is currently not available`），
本專案 SITL 實測、持續送設定點 25s 仍被拒。**這是獨立問題**：候選 1 說的是
「進了手動模式但搖桿無效」，候選 5 說的是「根本進不了手動模式」。兩者都會讓
「手動接管」這條救命路徑失效，**但成因與修法不同**。

---

## 需要補抓的文件（本地沒有，無法判定）

1. **ModalAI `voxl-mavlink-server` 文件**——它的訊息轉發規則／是否對非 QGC
   端點過濾訊息類。候選 2 完全依賴這份。
2. **ModalAI 對 `COM_RC_IN_MODE` 的出廠設定**（voxl-px4 的 param 預設檔）——
   確認 RB5 到底是 3 還是別的值。
3. PX4 官方 *Manual Control / Joystick* 說明頁（`docs.px4.io`）——本地只有
   參數 metadata，沒有敘述性文件。

---

## 真機到場 10 分鐘檢查步驟

**不需要起飛，全程地面。** 每步都有預期結果，對不上就停在那一步。

| # | 動作 | 預期 | 對不上代表 |
|---|---|---|---|
| 1 | 連上機、等參數快照抓完，讀 `COM_RC_IN_MODE` | 看得到值 | 參數表沒抓到（021 Phase 2 的路徑有問題） |
| 2 | 若值為 **0** | — | **確定就是它**：改 1 或 2 |
| 3 | 若值為 **3**，且遙控器有開 | — | **確定就是它**：RC 先到、我方被忽略。關掉遙控器重開機再試，或改 2 |
| 4 | 若值為 **1 或 2** | 搖桿應該要能動 | 排除候選 1，往候選 2（轉發）查 |
| 5 | 在地面送搖桿，用 `msg_registry` 看機端有沒有回 `MANUAL_CONTROL` 的效果（姿態設定點變化） | 有反應 | 訊息沒到機端 → 候選 2 |
| 6 | 量 `MANUAL_CONTROL` 到達間隔（機端 log 或我方送出節奏對照） | p99 < 500ms | 超過 → 候選 4（`COM_RC_LOSS_T`） |

---

## 產品面建議（不只是查案）

**把 `COM_RC_IN_MODE` 做成執行期前提檢查**，與 ArduPilot 的 `SYSID_MYGCS`
同一個機制（`ui-spec` §0.2c 條款 6：**前提可事前查證時，就不要讓使用者用失敗
去發現它**）。

我們**已經在抓全機參數表**（021 Phase 2），所以這個值連線時就在手上：

- `COM_RC_IN_MODE ∈ {1, 2}` → `manual` 能力 `ok`
- `= 0` → 鎖住，原因寫「機端設為僅接受實體遙控器（`COM_RC_IN_MODE=0`），
  搖桿指令會被忽略；改為 1 或 2 才能用」
- `= 3` → **這個要特別處理**：能不能用取決於「開機時誰先來」，是我們**事前
  查不出來的執行期事實**。誠實的做法是標 `unverified` 並說明原因，
  **不是猜它可以用**
- `= 4` → 鎖住（Stick input disabled）

**第三種情況正是本專案一路在處理的「不知道」**：`3` 不代表可用也不代表不可用，
而 UI 若把它顯示成可用，操作者會在最需要手動接管時才發現按鈕沒反應——
**與 030 的失聯降級同一種失效時機**。
