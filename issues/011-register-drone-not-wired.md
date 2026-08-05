# 011 · 「註冊無人機」表單未接線：建立資料列但系統不會連線

- 狀態：open（設計超前於實作，非 bug）
- 嚴重度：low
- 位置：`apps/frontend/app/drones/page.tsx`（表單）、`apps/backend/app/api.py`（POST /drones）
- 建立：2026-08-05

## 現象

無人機頁可註冊新機（名稱＋connection_url），但註冊後：

1. backend **不會**依 `connection_url` 建立 MAVLink 連線——它只有一條接收，
   由 `.env` 的 `MAVLINK_URL`／`DRONE_NAME` 設定，啟動時自動註冊主機。
2. 機上 node 對註冊機 push 鏈路量測也**全數被拒**：航線只由主機 ingest 的
   armed 轉換開啟，註冊機永遠沒有航線 → `find_session_at` 反查不到 →
   批次樣本判為「架次外」丟棄。

`connection_url` 目前是描述性欄位，不是操作指令。

## 為什麼保留這個設計

`drones` 表與 drone_id 鍵是承重牆（群飛渲染、歸屬、生命週期都靠它）。
表單是多機未來的正確入口：**ingest 多實例化時，應改為讀 `drones` 表的
註冊清單＋connection_url 逐台建立連線**——屆時表單從「存資料」升級為
真正的「接入一台機」，欄位語意當初即為此設計。

## 現行正確用法

單機部署「加入無人機」＝改 `.env` 的 `DRONE_NAME`（自動註冊＋綁定），
見 doc/deployment.md §1.2。表單已加誠實標注。

## 解法（多機時一併做）

1. backend 啟動：讀 `drones` 表非模擬機清單，每台 spawn ingest（各自 LiveState）
2. 航線開啟從「主機單例」改為逐台的 armed 轉換
3. 屆時本 issue 關閉，swarm_sim（開發鷹架）退役
