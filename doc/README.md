# 文件索引

三類：**系統文件**（長期維護，描述系統現況）、**方向定案**（目標與計畫）、
**現行設計規範**（實作依據）。已完結的提案/評估在 [archive/](archive/)。

新文件慣例：一次性提案、評估、restyle 稿→定案或實作完成後移入 archive/；
只有「描述系統長期現況」的文件留在本層。每份文件開頭寫狀態與日期。

## 系統文件（讀懂系統從這裡開始）

| 文件 | 內容 |
|---|---|
| [architecture.md](architecture.md) | 系統架構總覽：研究目標、三服務分工（backend/command/frontend）、資料流 |
| [data-schema.md](data-schema.md) | 資料庫設計：靜態註冊/架次/時序量測/事件四類，DDL 在 db/init/ |
| [deployment.md](deployment.md) | 真機部署完整手冊（RB5＋地面站全設定），SITL 開發環境見附錄 |
| [drone-registration.md](drone-registration.md) | **無人機註冊流程**：接一台新機的完整步驟、換機／退役、名字對不上的排查、以及系統靠什麼認出哪台是哪台 |
| [autonomous-flight-state-machine.md](autonomous-flight-state-machine.md) | **全自動飛行狀態機**：狀態判定式、允許/禁止的轉移、每條守門對應的實際危害 |
| [frontend.md](frontend.md) | 前端設計：四頁結構、map-centric、圖層誠實原則、視覺平滑邊界 |
| [onboard-telemetry.md](onboard-telemetry.md) | 機上 5G 量測回傳：companion 上的 node 如何送資料回地面站 |
| [qgc-integration.md](qgc-integration.md) | QGC 分工與連線拓撲（QGC＝板凳工具＋緊急備援） |

## 方向定案（為什麼做、做到哪）

| 文件 | 內容 |
|---|---|
| [gcs-replacement.md](gcs-replacement.md) | GCS 取代計畫：command 服務、兩層收集、群組任務、階段驗收表 |
| [agent-mcp-goals.md](agent-mcp-goals.md) | 終局目標：系統作為 MCP 供 agent 使用——任務層工具/紀錄完備標準/分析 API（issue 019） |

## 現行設計規範（實作照這裡）

| 文件 | 內容 |
|---|---|
| [ui-spec.md](ui-spec.md) | **UI 完整規格書（使用者核准，全頁面配色/內容/動作/文字圖）——UI 實作唯一依據**；變更走「修訂→使用者核准→實作」流程 |
| [design-tokens.md](design-tokens.md) | 視覺基準 tokens：色盤/字階/間距/圖表樣式（色盤驗證數據在文末） |
| [event-stream-design.md](event-stream-design.md) | 事件流納入無人機 vehicle log（STATUSTEXT/PX4 Events）的設計，實作中 |

## archive/ — 已完結的提案與評估（歷程紀錄，勿當現行規範）

| 文件 | 結局 |
|---|---|
| [capability-ui-proposal.md](archive/capability-ui-proposal.md) | 機型能力四態 UI——已實作（main 9fac7e0） |
| [compare-drones-restyle.md](archive/compare-drones-restyle.md) | 比較頁 3D＋機隊頁摺疊——已實作 |
| [3d-view-quality-proposal.md](archive/3d-view-quality-proposal.md) | 3D 品質分級提案（P1/P2/P3）——P1 完成；P2 被 deck.gl 選型吸收；P3 底圖定案見 frontend.md |
| [route-render-tool-eval.md](archive/route-render-tool-eval.md) | 路線渲染工具評估——定案 deck.gl（附四條件） |
| [live-restyle-spec.md](archive/live-restyle-spec.md) | 即時頁 restyle v1——被 ui-spec.md 取代 |
| [ia-direction.md](archive/ia-direction.md) | 資訊架構三分法——定案 2 被簡約重設計推翻，其餘併入 ui-spec.md |
| [simple-first-redesign.md](archive/simple-first-redesign.md) | 簡約重設計概念稿——被 ui-spec.md（使用者核准版）取代 |
