# 030 搖桿失聯自動懸停在 ArduPilot 上切錯模式（安全鏈裡的方言洩漏）

- 嚴重度：**high**（位於安全鏈）
- 狀態：**closed**（2026-08-12 修復並實飛驗證）
- 位置：`apps/command/app/mav.py` `_tick_manual()`
- 發現：ArduPilot 搖桿實飛驗證途中，模式無故從 POSHOLD 跳回 GUIDED

## 症狀

驗證 ArduPilot 搖桿時，`manual/start` 成功切到 POSHOLD（讀回 HEARTBEAT 確認），
但緊接著機體**自己跳回 GUIDED**。指令稽核表只有三筆（takeoff／manual_start／
manual_stop），**沒有任何一筆送過 GUIDED**。

## 根因

`_tick_manual()` 的「操作者失聯 → 自動懸停」那段寫死了 PX4 的模式編碼：

```python
main, sub = PX4_MODES["hold"]        # (4, 3)
self._sendto(sysid, ... MAV_CMD_DO_SET_MODE, ..., main, sub, ...)
```

PX4 的 `DO_SET_MODE` 是 `param2=main_mode, param3=sub_mode`，`(4,3)`＝AUTO.LOITER。
**ArduPilot 的 `param2` 直接是模式號**——`4` 是 **GUIDED**，不是 LOITER(`5`)。

而這段是 fire-and-forget（不等 ACK，理由正當：等 ACK 會阻塞 router 執行緒、害
其他機的心跳斷掉），所以它**不進稽核表**，從指令紀錄上完全看不出來。

## 為什麼這是安全問題

這段程式的職責是：**操作者失聯時讓飛機自主安全懸停**。

- 承諾：切到 Hold（ArduPilot 的 LOITER＝定點停懸）
- 實際（ArduPilot）：切到 **GUIDED**

GUIDED 是「等待地面站下達目標」的模式。在失聯情境下切進 GUIDED——也就是切進
一個**預期地面站會繼續給指令**的模式——與這段程式存在的目的正好相反。

程式碼原本的註解也是 PX4 視角：「中位搖桿的 POSCTL 本來就在原地懸停，且停送
MANUAL_CONTROL 時 **PX4 自身的** manual-control-loss failsafe 也會接管」。
**推理對 PX4 成立，對 ArduPilot 不成立，但程式碼對兩家都執行。**

## 為什麼 B0／B1／B2 三輪抽象都沒抓到

B0 把 backend 方言收斂、B1 定義驅動介面、B2 把兩端實作搬進共用驅動——**這一處
從頭到尾沒被碰到**，因為它**直接讀模組層的 `PX4_MODES` 表，沒有經過
`dialect()`**。

> **收斂到單一位置，只收斂了「會經過那個位置的呼叫」。** 直接讀表的旁路不會在
> 搬遷過程中現形——搬遷看的是「誰呼叫了 dialect()」，而它沒有。

這也是為什麼它是被**實飛**抓到而不是被三輪重構抓到：靜態上它看起來只是取一個
常數，動態上它送出了錯的模式號。

## 修法

改走驅動的模式編碼，未知廠牌則不送（停送搖桿本身已足夠安全）：

```python
drv = _autopilot.get_driver((self.drones.get(sysid) or {}).get("autopilot"))
try:
    main, sub = drv.encode_mode("hold")
except KeyError:            # 未知廠牌：不亂送模式
    continue
```

驗證編碼：PX4 `(4,3)`／ArduPilot `(5,0)`——修正前對 ArduPilot 送的是 `4`＝GUIDED。

## 防再犯（已驗證會擋）

`scripts/test-driver-equivalence.py` 第 10 項：**原始碼層面禁止直接索引
`PX4_MODES[...]`／`ARDU_COPTER_MODES[...]`**。這兩個名字現在只准用於 API 參數
驗證（`main.py` 檢查使用者給的 mode 字串是否合法），不准用於決定要送出的值。

反向驗證做過：把 `PX4_MODES["hold"]` 放回去，測試如實失敗並指出
`mav.py:209`；還原後通過。**沒有反向驗證過的測試，不算有測試。**

## 附帶成果：ArduPilot 搖桿實飛驗證通過

本 bug 是在做這件事時發現的（issue 015 的 `manual` 鍵證據強度補齊）。

**方法**：`MANUAL_CONTROL` 沒有 ACK，「送出成功」不構成證據，唯一的證據是機體
真的照指令動——且必須排除「本來就在飄」。

| 階段 | 6 秒位移 | 期間模式 |
|---|---|---|
| 前進 pitch `x=0.6` | **37.18 m** | POSHOLD |
| 鬆手回中位（緊接著） | 9.38 m | POSHOLD |
| 從靜止、全程中位 | 0.03 m | — |

**結論：有效。** 37 m 的定向位移出現在 POSHOLD——而現場**沒有任何實體遙控器**，
唯一的控制輸入來源就是我方的 `MANUAL_CONTROL`。

**兩個誠實邊界**：

1. 第二段的 9.38 m 是**減速滑行**（鬆手時機體正以約 6 m/s 前進，POSHOLD 讓它煞停），
   不是漂移。
2. 第三段「從靜止中位 0.03 m」**是在機體已落地時量到的**（腳本執行前它已自行
   下降並上鎖），所以它**不構成有效的空中漂移對照**。真正支撐結論的是第 1 點與
   「POSHOLD 下無其他輸入源」這個事實，不是第三段。

**前提**：該 SITL 的 `SYSID_MYGCS` 已被手動改為 254（非出廠值 255）。出廠機此鍵
應為鎖住＋原因文字。

**未解**：第二段結束後機體在 LOITER 下自行下降並上鎖（事件流可見 "Potential
Thrust Loss"、稍早有 "Battery unhealthy"），推測是 SITL 電池失效保護，**未查證**。
不影響本結論，但列出來免得下一個人以為它已經被解釋過。

## 相關

- 015 跨自駕儀支援（`manual` 鍵的證據強度）
- 026 驅動層抽象——本案是「旁路不會被搬遷發現」的實例
- 028／029 同屬「單一廠牌假設殘留」家族
