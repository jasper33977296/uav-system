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
* **機上代理的手動重送路徑也成為死碼**。`uav-agent` 的 `_reissue()` 存在的
  唯一理由是把地面站的搖桿封包以代理身分重新編碼；地面站不再送搖桿之後，
  該段要一併移除（另一個 repo，另開分支）。

## 驗證

* command `/healthz` 的 capabilities 不再有 `manual` 鍵，reasons 只剩
  `mission_start`／`mission_fly`。
* 舊端點回 404。
* command 服務重載無例外、`router_alive: true`。
* 前端 `✓ Compiled successfully`。

## 解決方式

（closed 時補：commit hash）
