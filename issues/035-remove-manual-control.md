# 035 · 移除虛擬搖桿：地面站不再直接操控機體

- 狀態：in-progress
- 嚴重度：medium（範圍決策，非缺陷）
- 位置：`apps/command`（3 端點＋`_tick_manual`）、`libs/autopilot`（能力鍵）、
  `apps/frontend`（`ManualControl.tsx`）
- 建立：2026-08-24

## 決策

使用者定案（2026-08-24），系統範圍收斂為：

> 系統只負責管理載入各種飛行路徑檔、執行飛行路徑、中斷飛行路徑、中途更改
> 飛行路徑等等的操作，並且要**絕對確保無人機在飛行過程的安全操作**。

**虛擬搖桿不在這個範圍內，故整塊移除。**

配套的前提（同日定案）：

* **機上代理才是飛行指令與航路的下達者與監控者**，地面站不應持有各機型的
  操作方式（這同時是 [026](026-autopilot-driver-abstraction.md) 待決點 1 的答案，
  見該案）。
* **手動操控由實體遙控器承擔**，遙控器在場待命。因此
  **不得為了方便而關閉預檢的 RC 檢查**——那項檢查在確保接管手段存在。

## 為什麼虛擬搖桿本來就不該留

不是「用不到所以刪」，而是它在這個架構下站不住：

1. **延遲與鏈路**。搖桿是連續操縱，走 5G 往返（實測 RTT 22–40ms 且會抖）
   本來就勉強；鏈路一斷，操縱者手上的桿與機體狀態立刻脫節。
2. **它讓安全鏈變複雜而不是變簡單**。為了讓它安全，`_tick_manual` 要實作
   deadman → 中位 → 自動 Hold 的三段降級，而那段降級本身就出過事
   （[030](030-manual-failsafe-wrong-mode-ardupilot.md)：承諾 Hold 實送 GUIDED）。
   刪掉它，這條安全鏈連同它的失效模式一起消失。
3. **前提檢查會誤導人**。ArduPilot 只接受來源 sysid 等於 `SYSID_MYGCS` 的搖桿
   封包，不符**靜默丟棄**。能力探測因此要讀機端參數並給建議——而在「機上有
   代理代為下達」的拓樸下，那個建議會叫人去做一個**會弄壞系統的修改**
   （把 `SYSID_MYGCS` 改成 254，反而擋掉代理重送的封包）。
   這個誤導是本案的直接觸發點。

## 移除範圍

| 層 | 內容 |
|---|---|
| command 端點 | `POST /manual/start`、`POST /manual`、`POST /manual/stop`、`ManualIn` |
| command router | `self.manual` 狀態、`set_manual`／`stop_manual`、`_tick_manual`（47 行安全鏈）、手動中的 20Hz 收發切換 |
| 驅動層 | `CAP_KEYS` 的 `manual`、`Driver.manual_prepare()`（四個實作，無呼叫端）、ArduPilot 的 `SYSID_MYGCS` 探測 |
| 端點對映 | `ENDPOINT_CAP` 的 `manual` 與 `mode:position` |
| 前端 | `ManualControl.tsx`（299 行）、`CommandPanel` 的手動區塊、`store.deadman`、`SimpleHud` 的「操控中斷」句 |

### 連帶影響（刻意記下來）

* **`mode:position` 一併移除**。它唯一的用途是搖桿的前置模式（PX4 的雞生蛋：
  POSCTL 要先有手動控制串流才 engage）。沒有搖桿之後它沒有呼叫端，
  透過泛用 `mode/{mode}` 端點也不再可達。日後若要把「位置模式」當成獨立的
  飛行模式提供，是新功能、要有自己的能力鍵，不是把這條接回來。
* **[033](033-emergency-availability-design.md) 的第三層防線要改寫**。原文寫
  「手動接管層：搖桿真機可用（＝032，本案的前置依賴之一）」——那個前提已經
  不存在。該層現在指的是**實體遙控器**。
* **[032](032-joystick-cannot-control-rb5.md) 隨功能移除而關閉**，不是修好了。
  檔案保留：裡面對 `COM_RC_IN_MODE`／轉發過濾／GCS 身分的排查過程，對日後
  任何「地面站送的東西機端沒反應」的問題仍有參考價值。
* ~~**機上代理的手動重送路徑也成為死碼**~~ → **已完成**（`uav-agent` `7eb5f4b`，
  2026-08-24，與地面站移除虛擬搖桿同一天）。2026-08-31 對帳確認該 repo 已無
  `MANUAL_CONTROL`／`RC_CHANNELS_OVERRIDE` 殘骸。
  > 但**代理的 `gs_parser` 不是死碼**，雖然它的註解一度讓它看起來像：那段的
  > 說明寫著「要認出搖桿封包才能改寫身分」，而它實際做的是（一）解析＝驗 CRC，
  > 擋掉壞封包不讓它進飛控的解析器；（二）模式溯源，也就是「這個 LOITER 是
  > 地面站按的暫停還是飛手切的手飛」——那是失聯處置選項 C 最關鍵的一格。
  > **又一次「照理由刪會刪掉還在保護東西的程式」**，與本檔 `SYSID_MYGCS` 那條同形。

## 驗證

* command `/healthz` 的 capabilities 不再有 `manual` 鍵，reasons 只剩
  `mission_start`／`mission_fly`。
* 舊端點回 404。
* command 服務重載無例外、`router_alive: true`。
* 前端 `✓ Compiled successfully`。

## 對帳補漏：`SYSID_MYGCS` 探測（2026-08-31）

移除範圍表列了「ArduPilot 的 `SYSID_MYGCS` 探測」，但**實際沒有拔掉**——
`mav.py` 每 30 秒對每台 ArduPilot 機重讀一次那個參數（兩個新舊參數名都問），
而它存在的唯一理由是註解自己寫的那句：「ArduPilot 只信 SYSID_MYGCS 指定來源的
**MANUAL_CONTROL**」。搖桿移除之後 `d["sysid_mygcs"]` 沒有任何判斷在讀它，
兩家驅動的 `capabilities(ctx)` 也都不再看 `ctx`。

**死流量加上一段會誤導的註解**：下一個讀到那段的人會以為系統還在為搖桿做前提
檢查。已移除探測、`MYGCS_REREAD_S`、`PARAM_VALUE` 分支與那段說明，並改寫
`capabilities_for` 的 docstring（不再點名一個沒人填的欄位）。

`GCS_SYSID = 255` **留著**，它的理由沒有變：ArduPilot 只信該參數指定來源的部分
指令，而 255 是出廠預設，改我方常數比要求每台實機改參數可靠。

### 但 `scripts/accept-ardupilot.py` 的 B 項不刪，改寫理由

那項檢查的**理由**死了（搖桿），**檢查本身沒死**：ArduPilot 用 `SYSID_MYGCS`
決定誰的心跳算 GCS 心跳，那是 GCS failsafe（`FS_GCS`）的判準，也是
[039](039-autonomous-flight-state-machine.md) 整套失聯處置的機上前提——不符的話
飛控根本不覺得我們斷過線。順帶修掉裡面那句過期的「須設 SYSID_MYGCS=254」
（我方早已改為 255）。

**判斷方式值得記下來**：清死碼時要分「這段程式為什麼存在」與「這段程式做了
什麼」。理由消失不等於行為無用——照理由刪會刪掉還在保護東西的檢查。

### 連帶查出兩支永遠紅的測試（同一天修）

清理時跑回歸才發現，有兩支測試**在改動之前就已經是紅的**，而且都紅在
「刻意改變的行為」上——它們把過去的行為當成正確答案：

* `scripts/test-driver-equivalence.py`：拿 git 基準點 `41471cc` 的舊實作比對
  今天的驅動。035 移除 `manual` 能力鍵、015 在 08-24 把 ArduPilot 的
  `mission_start`／`mission_fly` 由 unverified 開為 ok——這三筆刻意的改變讓它
  **永遠紅**。改成具名的分歧表：表上有的放行（每筆寫得出為什麼）、
  表上沒有的照樣失敗、**表上有卻已經不再分歧的也失敗**（過期的豁免要清掉，
  留著它會在日後真的回歸時默默放行）。已反向驗證：拿掉任一筆豁免，測試會擋下。
* `scripts/test-dialect-boundary.py`：斷言 `mode_name(0, ardupilot) == "—"`——
  那正是 `a92b8e1` 修掉的 bug（ArduPilot 的 0 是 **STABILIZE**，一個合法的
  手飛模式，不是「沒有模式」）。**測試在替一個已修的 bug 背書。**

**一支永遠紅的測試等於沒有測試**：真正的回歸會淹在固定的雜訊裡，而且沒有人
會第二次去看它。這兩支都不是本案造成的，但它們是同一種帳沒對完的痕跡。

## 解決方式

（closed 時補：commit hash）
