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
