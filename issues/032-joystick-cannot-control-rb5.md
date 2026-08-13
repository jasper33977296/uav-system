# 032 · 搖桿無法真正控制 AI Model RB5：對照 spec 找根因

- 狀態：open
- 嚴重度：**high**（手動接管是意外狀況下的救命層；030 已證明這條路徑的缺陷會在最需要時才現形）
- 位置：`apps/command`（`mav.py:_tick_manual`）＋ `reference/`（PX4/ModalAI 文件）
- 建立：2026-08-13（使用者回報，PM 配號）

## 現象

使用者回報：搖桿沒辦法真正控制 AI Model RB5。系統的手動控制在 SITL
（PX4＋ArduPilot）已實測可用（見 030 的驗飛），但對真機 RB5（voxl-px4）不生效。

## 待驗證的候選根因（照 spec 逐一對照，不猜）

依廠牌差異分析的既有經驗，優先排查：

1. **PX4 `COM_RC_IN_MODE`**：值為 0（僅實體 RC）時 MANUAL_CONTROL 會被忽略
   ——搖桿流照送、機照收、就是不動，且無任何錯誤回報。RB5 出廠值要查。
2. **voxl-mavlink-server 的訊息轉發範圍**：MANUAL_CONTROL 是否會從我方
   14541 指令通道轉進 autopilot？（該服務對非 QGC 端點可能有訊息類過濾。）
3. **GCS 身分**：我方 sysid 254 非 QGC 慣例 255——PX4 的 manual control
   來源選擇邏輯是否綁定特定 sysid／component？
4. **串流節奏與逾時**：MANUAL_CONTROL 需持續高頻流，PX4 逾時（`COM_RC_LOSS_T`）
   即判 RC loss；經 5G 的抖動是否讓流被判定中斷？
5. **模式前提**：手動接管需先進 POSCTL/MANUAL 類模式——與 backlog 既有的
   「POSCTL B-case」調查可能同源。

## 驗收定義

在 SITL 重現「RB5 等效設定」下逐項排除；產出「真機到場後 10 分鐘可驗完」
的現場檢查步驟（含每步預期結果），併入 deploy-checklist。

## 解決方式

（closed 時補）
