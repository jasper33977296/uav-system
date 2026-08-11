# 即時頁 Restyle 稿 v1（含 P2 路徑層合併）

- 狀態：**供實作**（UI/UX 2026-08-11；tokens 見 doc/design-tokens.md）
- 前提：版面骨架不動（map-centric＋右側欄＋浮動任務控制）；本稿是
  視覺與資訊層級重整。跨頁 IA 項（5G 摺疊、事件人話）等 PM 核可
  doc/ia-direction.md 後啟用，其餘可先做。
- 排程約束（PM）：015 capability UI 功能面優先，本稿不擋它。

## 1. 全域套用

tokens 全量替換（globals.css `:root`＋geo.ts CANVAS `#1b1a17`）。
圓角/間距/陰影照 tokens 階。頂欄 active tab 用 `--accent-bg`＋`--accent`
文字（accent 首次入場，僅此互動 chrome）。

## 2. 側欄三卡（依 IA 三層：徽章列 → 主數字 → 圖表）

### 飛行狀態卡
```
┌──────────────────────────────────┐
│ SIM-UAV-1  [PX4] [● 就緒] [MISSION] │  ← 徽章列：機名+機型+就緒+模式 chip
│ 相對高度      地速        垂直速度  │
│ 39.9 m      4.9 m/s     -0.0 m/s │  ← 主數字 2×3，tabular-nums
│ 電量 66%    GPS 3D·10   航向 0°   │  ← 電量/GPS 徽章化（IA 核可後）
└──────────────────────────────────┘
```
姿態角（roll/pitch）移出常駐版面→機名 hover tooltip（查問題資訊）。

### 訊號品質卡（研究主視覺）
```
│ 無人機訊號品質        [● 尚可]    │
│ 8.1 dB SINR                      │  ← hero 22px＋分級 chip 並列
│ ▁▂▄▂▁▆▄（sparkline）             │  ← 原始 muted 40% 1px＋平滑 2px
│ RSRP -86.9  RTT 60.5  丟包 0.0%  │  ← 次要數字一列
│ ▸ 詳細（RSRQ·PCI·頻帶·CQI·SA）    │  ← 摺疊（IA 核可後）
```
sparkline hover：crosshair tooltip（時間/原始/平滑/分級標籤）。

### 事件流卡
狀態色點＋動詞開頭人話（IA 核可後上模板；先套 tokens 密度）、
時間 `--muted`、連續同類摺疊「×N」。criticall 事件列加 `--status-danger-bg`
底——安全資訊不漸進揭露。

## 3. 任務控制面板

- tokens 化（radius 14 浮動面板＋唯一陰影層）；分區標題 11px `--muted`。
- 按鈕語意不變：危險紅系照舊；「上傳」等非危險主要動作可用 accent。
  **兩段式確認與緊急單擊、safety 鏈全部不動**（既有定案）。
- **手動 deadman 三段狀態視覺**（先前與後端定案的前端本地方案落地）：
  - 控制中（<0.4s）：`--status-ok` 圓點＋「手動控制中 · 10Hz」
  - 警告區（0.4–2s）：amber 進度環倒數＋「輸入中斷—即將自動懸停」
  - 已接管（三條件合一）：`--status-danger-bg` 橫幅「deadman 已觸發，
    機體 Hold」＋重新啟用鈕
- 僅觀察/未驗證橫幅（capability UI）套 `--status-warn` 底、圖示＋文字。

## 4. 地圖 chrome

- 圖例/比例尺/座標顯示卡片統一 tokens（surface、radius 10）。
- 檢視切換（地圖↔影像）與 sysid chips 統一 chip 樣式：
  選中＝`--surface-2`＋`--ink`，未選＝`--ink-2`；不用 accent（避免與
  互動主色搶焦點，chip 是狀態不是 CTA）。
- P2 路徑層（選型更新 2026-08-11，見 doc/route-render-tool-eval.md）：
  **deck.gl PathLayer**（jointRounded＋3D 座標＋同分級 run 分割上色＋
  Catmull-Rom 視覺平滑幾何限定）；斜向段階梯化與整源 setData 閃爍
  隨之消失。光照移出範圍（PM 定案）。機隊識別色改序列前 3 槽。
- **☐ 驗收 checklist**（遷至 deck.gl 落地驗收）：（a）瀕斷段可辨識度
  （不過→直接加亮色描邊/寬度加倍，PM 已預核）；（b）CVD 模擬四段
  分級在 **run 交界**清晰可辨；（c）5Hz 更新無閃爍；（d）幀率不低於
  現行（1200 點×多機）。

## 5. 驗收方式

實作後我用 SITL＋headless 截圖跑一輪（同 017 P1 方法學）：
tokens 對照、三卡層級、deadman 三態（手動觸發模擬）、CVD 模擬圖。
與現版截圖並列給使用者比對。
