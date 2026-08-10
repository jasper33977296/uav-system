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

## HTTP 側（5G 樣本）身分現況（2026-08-10 盤點）

「這筆樣本是哪台機的」不靠來源 IP（5G/NAT 不可靠），靠樣本自帶
`drone_id`（各機 onboard node 的 `.env` 設 `DRONE_ID=<drones 表 UUID>`；
不填＝記在主機名下，單機正確預設）。盤點：

- ✅ batch 通道帶 `drone_id`、入庫冪等鍵 `(drone_id, time)`——協定就緒
- ❌ live 通道**沒有** `drone_id`：更新的是單例 live state，多機同時
  POST 會互相覆蓋（畫面錯，資料不受影響）
- ❌ 架次歸戶只認主機：非主機的 batch 樣本反查不到架次被丟棄
- 兩條流身分平行：MAVLink＝sysid→`drones.mav_sysid`、
  HTTP＝`DRONE_ID`→`drones.id`，都收斂到 drones 表

## 解法（多機時一併做；模型 2026-08-10 定案）

**單埠＋sysid demux**（取代原案「逐台依 connection_url 建連線」）：

1. 所有機的 mavlink-router 都打地面站**同一個埠**（14540）——機上設定
   完全相同（同一映像），只差 PX4 參數 `MAV_SYS_ID`（裝機時設唯一值）
2. capture tee 升級為 demux：逐框架讀 sysid（v2 表頭第 5 byte），
   per-sysid 轉發到各自內部埠與 mavsdk 實例（各自 LiveState）；
   回程依「sysid → 最後來源位址」原路送回。原始層 tlog 天然多機安全
   （每則訊息帶 sysid，重放時可按機拆分）
3. `drones` 表加 `mav_sysid` 欄位承擔身分對應；「設為主機」概念退役
   （單連線時代的過渡設計）。`connection_url` 欄位語意作廢
4. 防呆必要：偵測「同 sysid 來自兩個不同來源位址」→ 告警
   （撞號會靜默混料，比連不上更糟）
5. 航線開啟從「主機單例」改為逐台的 armed 轉換
6. 屆時本 issue 關閉，swarm_sim（開發鷹架）退役
