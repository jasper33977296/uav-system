# 018 · 事件 detail 人話化：JSON 直出改事件語句模板

- 狀態：open
- 嚴重度：low
- 位置：`apps/frontend`（事件流渲染）、可能涉及 backend 事件結構
- 建立：2026-08-11（doc/ia-direction.md 定案 3 拆出）

## 現象

事件流的 detail 以 JSON 直出＋省略號截斷（doc/frontend.md 待做 2），
如 `failsafe {"state":"CRITICAL"}`。操作者要自行解讀欄位。

## 方向

每個事件 type 對應一個動詞開頭的中文句式模板（例：failsafe →
「機體進入 CRITICAL——failsafe 觸發」；mode → 「模式 HOLD → MISSION」
已可读，保留）。跨端工作：句式需要的欄位若 detail 沒有，動 backend
事件結構。連續同類事件在 UI 摺疊計數（×N）。

新事件類型「serving cell/PCI 變更」已由後端落地（`cell_change`，
main 4ebc8ab）：detail 帶 `{from_pci, to_pci, from_band, to_band, sinr}`
人話句式欄位，三層防抖（link_sim 6dB 邊際／真機硬體滯後／事件層連續
2 次取樣確認），單元測試四情境過。前端句式建議：
「serving cell 換手：PCI {from_pci}（{from_band}）→ PCI {to_pci}（{to_band}）」。
端到端驗證（實飛跨 gNB）：2026-08-11 UI/UX 以 test-flight.py 穿越飛行
驗證中。

## 排程

PM 定案：restyle 主波之後；restyle 過程順手能改的簡單案例先改。

## 解決方式

（closed 時補）
