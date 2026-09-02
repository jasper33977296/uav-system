# 019 · MCP agent 介面：任務層工具＋因果鏈紀錄＋分析 API

- 狀態：open
- 嚴重度：medium
- 位置：`doc/agent-mcp-goals.md`
- 建立：2026-08-11

## ⏸ 2026-09-02：MCP 先不做，改為只提供三個任務層 API

使用者裁定：**MCP 先不做**，提供「選擇任務／上傳任務到無人機／無人機開始執行
任務」三個 API 就好，並整理成 OpenAPI 格式的文件。

**那三個端點本來就存在**（散在 command 服務裡），所以這一批做的不是新建，
是**收斂成一個說得清楚的介面**：

* `GET /api/missions`（選）、`POST .../mission/upload`（上傳）、
  `POST .../mission/start`（執行）加上 OpenAPI 分組與說明；
  其餘端點分到「操作／群組／一鍵／健康」，**讓「外面該用哪些」一眼看得出來**，
  而不是把 19 個端點倒給對方自己挑。
* [`doc/mission-api.md`](../doc/mission-api.md)：三步流程、
  **呼叫前一定要知道的兩件事**（`start` 不會讓地面上的機起飛；`upload` 在空中
  是立即改道）、以及**三種 4xx 的差別**（入列 403／能力 501／守門 409），
  含「被擋下不等於沒有退路」那句。
* [`doc/openapi.json`](../doc/openapi.json) 進 git，
  `scripts/export-openapi.py` 匯出並可 `--check` 偵測漂移
  ——**介面變更因此看得到 diff**，那是口頭約定做不到的事。

**本案其餘部分（任務層工具的 MCP 皮層、因果鏈紀錄、分析 API）維持 open**，
但排序退到三個 API 之後。027（弧長投影後端端點）仍然是分析 API 的前置。

## 現象

終局目標定案（2026-08-11）：系統將作為 MCP 供不具無人機知識的 agent 使用
（「讓 XX 台無人機飛 XX 路徑」層級的意圖）。現有系統是給人操作的 UI＋
過程式 API，缺 agent 可用的任務層介面。

## 原因

見 `doc/agent-mcp-goals.md` 全文。三個組成：

1. 任務層工具：submit_mission／get_mission_status／abort（實機要人確認、
   SITL 直飛；操作層不暴露）。
2. 紀錄補齊：任務↔架次↔指令↔事件↔資料因果鏈（010 欄位復活）、
   架次機器可讀摘要、指令留痕加 agent 身分。
3. 分析 API：get_flight_summary／query_signal_map／compare_flights
   （與 /compare 共用查詢層；聚合附樣本數、不插值）。

## 影響

研究自動化的核心路徑：agent 批次做量測實驗與分析的前提。

## 修法建議

照 `doc/agent-mcp-goals.md` §4 落地順序：多機驗收先行 → 任務層資料模型
（010 復活）→ 分析 API → MCP 皮層 → 目標層（013 合流）。

## 解決方式

（closed 時補）
