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

## 進度（2026-08-13）

**spec 對照分析完成**：`doc/032-rb5-manual-control-analysis.md`。

**主嫌：`COM_RC_IN_MODE`**，但不是 issue 原本假設的「值為 0」——PX4 1.14.3 的
**出廠預設是 3 =「RC or Joystick keep first」**（先到的來源勝出，另一個之後
一律忽略）。而 RB5 的官方設定流程**要求綁定並校正實體 Spektrum 遙控器**、
飛行模式與馬達切斷都掛在 RC 通道。遙控器若先上線，我方 MANUAL_CONTROL 就被
靜默忽略——**與症狀完全吻合**（不回錯誤、不進日誌、就是不動）。

**為什麼 SITL 驗過卻不能轉移**：本專案 SITL 的 `COM_RC_IN_MODE = 1`
（Joystick only）且環境裡沒有實體 RC，所以我方指令必然是唯一來源、必然生效。
030 的實飛驗證證明的是「我方送法對」，不是「這機制在有 RC 的機上會生效」。

**候選 3（sysid 254）可排除**：MAVLink 規格的 MANUAL_CONTROL 沒有來源身分欄位，
PX4 全部參數裡也沒有以 GCS sysid 為條件的手動控制閘門。「只信 SYSID_MYGCS」
是 ArduPilot 行為（015 實測），套到 PX4 是跨廠牌誤植。

**尚缺**：ModalAI `voxl-mavlink-server` 轉發規則文件、voxl-px4 的參數預設檔
（候選 2 完全依賴這兩份）。

**待做**：SITL 重現（把 `COM_RC_IN_MODE` 改 0 或 3 後跑 manual_stick 測項，
確認搖桿失效）——需要飛一趟。

**產品面建議已寫進分析文件**：把 `COM_RC_IN_MODE` 做成執行期前提檢查（同
ArduPilot `SYSID_MYGCS` 的機制）。值為 3 時**不得顯示成可用**——那是我們事前
查不出來的執行期事實，誠實的標記是 `unverified`。

## 與墜落事故的耦合：一個 ulog 欄位同時裁決兩案（PM 2026-08-13）

墜落候選之一是「我方送出的搖桿值構成 PX4 的解除鎖定手勢」（`MAN_ARM_GESTURE`
預設開啟，油門最低＋偏航全左＝disarm）。這條與本案主嫌**互斥耦合**：

- **若 RB5 真的是 RC-first-wins**（本案主嫌成立）→ 我方 MANUAL_CONTROL 被忽略
  → **手勢也送不進去**，那條墜落候選同時被排除。
- **若 ulog 顯示 disarm reason ＝手勢、且當時 `manual_control_setpoint` 的值
  來自我方** → 證明我方搖桿流**確實有進到飛控** → 本案的「搖桿控不了」
  **就不是「被忽略」，而是別的環節**（模式前提、mux、或值域）。

**所以這兩案不能各自查**：ulog 裡 disarm 原因與 `manual_control_setpoint` 那
兩個欄位，會同時決定本案的結論方向。

## 解決方式

（closed 時補）
