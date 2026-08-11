# 019 · MCP agent 介面：任務層工具＋因果鏈紀錄＋分析 API

- 狀態：open
- 嚴重度：medium
- 位置：`doc/agent-mcp-goals.md`
- 建立：2026-08-11

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
