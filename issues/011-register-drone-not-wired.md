# 011 · 「註冊無人機」表單未接線：建立資料列但系統不會連線

- 狀態：open（範圍縮小：身分管理已於 2026-08-05 系統端實作，僅剩多機 ingest）
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

## 現行正確用法（2026-08-05 更新：已系統端實作）

`DRONE_NAME` 已自 .env 移除。機的身分完全由系統端管理：

- 全新環境自動建立預設主機 `uav-1`
- 無人機頁：**改名**（PATCH /drones/{id}）、**設為主機**
  （POST /drones/{id}/primary，飛行中拒切、僚機拒設、切換即時生效不需重啟）
- MAVLink（14540）收到的遙測記在「主機」名下

本 issue 剩餘範圍：多機**同時**接入（ingest 多實例化，逐台依 connection_url
建立連線）。

## 解法（多機時一併做）

1. backend 啟動：讀 `drones` 表非模擬機清單，每台 spawn ingest（各自 LiveState）
2. 航線開啟從「主機單例」改為逐台的 armed 轉換
3. 屆時本 issue 關閉，swarm_sim（開發鷹架）退役
