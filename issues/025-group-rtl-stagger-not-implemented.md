# 025 · 編隊 RTL 高度錯開未實作（separate 同高任務的緊急返航無分離保證）

- 狀態：**deferred**（2026-08-12 使用者裁定「現階段不補實作，文件記錄即可」）
- 嚴重度：low（有操作面規避；unified 模式不受影響）
- 位置：`doc/group-missions-design.md` §10.2、`apps/command/app/group_exec.py`
- 建立：2026-08-12（補開——原僅記於設計文件，索引上無入口）

## 現象

設計文件原寫「各台按 `layer_index × GROUP_RTL_STAGGER_M` 錯開返航高度」，
但**顯式的 rtl_stagger 從未實作**：`group_exec.rtl()` 是純 RTL-all（逐台
切 RTL 模式，無 per-drone 返航高度設定），也沒有 param-set 的工作函式。

013 收官實飛觀察到三台確實在不同高度返航（29.9／34.9／39.9m，差 5m、安全
不撞），但**那個分離來自 vsep 高度分層＋PX4「高於 RTL_RETURN_ALT 時維持當前
高度返航」的特性，不是 rtl_stagger**。

## 影響

- **unified 模式**：vsep 分層已提供返航分離，實飛驗證過，無問題。
- **separate 模式**：各機飛各自的任務，若使用者把多台的任務規劃在**相同高度**，
  緊急 RTL-all 時全部往同一個 home 收斂，**分離無保證**。
- 深層問題：緊急路徑的安全性依賴 PX4 的**突現行為**（「高於 return alt 就維持
  高度」是特性不是保證，且低於 return alt 時就失效），而非確定性的機制。

## 現階段的處置（使用者裁定）

**不補實作，改為文件化限制＋操作面規避**：
- `doc/group-missions-design.md` §10.2 記載已知限制；
- `doc/deploy-checklist.md` 安全注意事項寫明：**separate 編隊請為各機規劃
  不同高度**（同高度時緊急全機返航的分離無保證）。

## 重啟觸發條件

- 實機（非 SITL）要跑 separate 模式編隊——**實機碰撞的代價與模擬完全不同**；
- 或出現任一次 RTL-all 時機間距離低於安全閾值的實例。

## 修法（重啟時）

RTL 前逐台 param-set `RTL_RETURN_ALT = base + layer_index × GROUP_RTL_STAGGER_M`
（需在 command 服務新增 param-set 工作函式），並重測 separate 情境：
兩台同高度任務 → RTL-all → 確認返航高度分離。工作量估 30–45 分鐘＋重測。

## 解決方式

（closed 時補）
