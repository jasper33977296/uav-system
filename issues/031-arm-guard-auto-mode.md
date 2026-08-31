# 031 · arm 防護：自動模式＋機上有任務時，裸 arm ＝ 立即自主起飛

- 狀態：**closed**（2026-08-31 對帳收案；實作在 `be00220`）
- 嚴重度：**high**（真機上等於 fly-away；SITL 已實際發生一次）
- 位置：`apps/command`（arm 端點／能力 gating）
- 建立：2026-08-13（PM 記錄當日事故，配號 031）

## 事故（2026-08-13，SITL，無損害）

後端 session 為產生測試架次，對 sysid 3 下裸 `arm`（意圖：地面 arm/disarm 最小
驗證）。該機**仍停在 MISSION 模式且機上載有任務**——arm 後立即自主起飛執行
任務，爬到 50m 巡航才被發現，RTL 收回。全程約 2 分鐘。

**這正是 issue 028 危害描述的人工重演**（「把一台還在地面的機切進
AUTO.MISSION」）——028 修掉了程式會犯的版本，這次證明**人（與未來的 agent）
也會犯**，而系統明知機上模式卻沒有阻攔。

## 為什麼要系統防護而不是只靠紀律

- 操作者的紀律（「arm 前先讀模式」）該有，但**單靠紀律的防線今天已被
  同一位資深操作者穿過三次**（gzclient、import 相依、本次）——疲勞與慣性
  是常態不是例外。
- **MCP 之後 agent 也會下 arm**——agent 不會累，但也不會「想到要先看模式」，
  除非系統把這件事變成不可繞過的檢查。
- 系統**已經知道**判斷所需的一切：當前 flight_mode（HEARTBEAT）、機上是否
  有任務（current_mission_id／回讀）。知情不阻攔，與本專案一路的誠實/防護
  哲學不符。

## 修法方向（後端設計，PM 先記大意）

`arm` 指令在偵測到「當前模式為自動執行類（AUTO.MISSION 等）且機上載有任務」
時**拒絕裸 arm**，回明確原因與兩條出路：
1. 先切安全模式（hold）再 arm；或
2. 帶顯式意圖旗標（如 `intent=start_mission`）表明「我就是要 arm 後立即飛」
   ——那其實該走 `mission_fly` 序列而不是裸 arm。

拒絕訊息要人話（沿用 capability_reasons 風格）：說明機在什麼模式、arm 會
發生什麼、該怎麼做。

## 同族第二條：搖桿值可構成 disarm 手勢（2026-08-13 查出，待實作）

`MAN_ARM_GESTURE` 預設開啟，PX4 參數文件原文：「moving the left stick to the
lower right arms and **to the lower left disarms the vehicle**」。左搖桿＝
油門(z)＋偏航(r)，左下＝油門最低＋偏航全左。

我方編碼（`mav.py:_tick_manual`）：`z_wire = 500 + z*500`、`r_wire = r*1000`，
所以前端送 `z=-1.0, r=-1.0` 時線上就是 `z=0, r=-1000`——**正是那個手勢**。
而 `_tick_manual` 對搖桿值**沒有任何過濾**。

**與 031 本體同一個形狀**：系統知道那個組合會停馬達，卻照送。

**待查證（SITL 可安全驗，比讀碼可信）**：PX4 對空中的手勢 disarm 是否有前置
條件（是否檢查 land-detected 或限特定模式）。**若有，這條與「空中誤判著陸」
就是串聯而非獨立的兩個候選**。驗法：空中送 `z=0, r=-1000` 看會不會掉。

**待實作**：飛行中不得送出構成 disarm 手勢的組合（或 z 極低時夾住 r）。

## 解決方式

`be00220` 實作 `_guard_bare_arm`（`apps/command/app/main.py:226`），接在
`POST /api/command/{sysid}/arm`。2026-08-31 逐項對帳確認：

* **判準用模式動詞不用模式名**（`_AUTO_EXEC_VERBS = {mission, rtl, land}`）——
  PX4 叫 MISSION、ArduPilot 叫 AUTO，比字串會漏（030 的教訓）。
  `guided` 刻意不在此列：那是「解鎖後等指令」的正常起飛前置。
* 拒絕訊息給兩條出路與 `intent=start_mission` override，照修法方向。
* **收不到心跳時不擋**：不知道模式就不亂擋（能力 gating 另有把關）。
* **只擋這個 HTTP 端點**：`_do_takeoff` 與群組執行器直接呼叫 `job_command`，
  那些 arm 是起飛序列的一步、有意圖，不該被擋。

### 與原始描述的一個差異（刻意）

標題寫的是「自動模式**＋機上有任務**」，實作只看模式、不查機上任務。
**這是更嚴的一邊**：AUTO 模式下就算機上任務是空的，解鎖後的行為也不是
「安靜地待在地上」——不值得為了放行一個沒人需要的情境去多讀一次任務。

### 同族第二條已隨功能消滅

「搖桿值可構成 disarm 手勢」那一條的前提是 `_tick_manual` 會把搖桿值送給
飛控。虛擬搖桿已隨 [035](035-remove-manual-control.md) 整塊移除，
`_tick_manual` 不存在，**該路徑不再可達**——是消滅不是修好，記在這裡免得
日後有人以為驗過。
