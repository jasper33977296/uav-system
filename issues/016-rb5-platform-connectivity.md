# 016 · RB5 平台連線層：出廠廣播 :14550／埠寫死／sysid bug，兩通道設計不會自動成立

- 狀態：open
- 嚴重度：high
- 位置：`reference/gap-analysis.md` §0、`reference/rb5/`
- 建立：2026-08-11

## 現象

2026-08-10 實測（機型：高通 AI Model RB5）能控制的無人機非常有限。
RB5 跑 PX4，不是自駕儀方言問題（issue 015 是另一題）——是平台連線層。

## 原因

RB5 的 MAVLink 對外通道由 ModalAI 平台層管理（來源快照與出處：`reference/rb5/`）：

1. 出廠行為＝**廣播 heartbeat 到 :14550** 等 GCS 回應（QGC「自動連線」的原理）；
   新版 voxl-mavlink-server 則走 conf 的 static GCS IP，**目的埠 14550 寫死**。
   我們的架構假設「機上配兩條 PX4 mavlink 實例主動打我方 14540/14541」——
   出廠機不會這樣做，沒逐台改機上設定就是連不上。
2. **廣播不過 5G 網段**（官方對 LTE/5G 建議 VPN＋static IP）——WiFi 測通、
   上 5G 就失聯的典型病因，而 5G 量測正是本專案主場景。
3. voxl-mavlink-server **<1.4.12 的 sysid bug：全部機被重設為 sysid 1**，
   直接打掉單埠 sysid demux 多機架構（全機被當同一台＋同 sysid 異位址告警）。

## 影響

- RB5 機隊「有時能控、有時不能」不可重現；5G 場景系統性失聯。
- 多機接入時 demux 失效，資料歸屬錯亂。

## 修法建議

按 `reference/gap-analysis.md` §0 的上機檢查清單：

1. 判代：`voxl-inspect-services`＋`ls /etc/modalai/`（full-m0052.config vs
   voxl-mavlink-server.conf）。
2. m0052 代：`param set MAV_BROADCAST 0`＋逐台加實例指向我方 14540/14541；
   server 代：conf 設 static_gcs_ip＋我方加聽 14550，或機上 mavlink-router 轉埠
   （「機上不裝 router」定案可能要對 server 代破例，需重議）。
3. voxl-mavlink-server 升 ≥1.4.12，逐台設 MAV_SYS_ID 並實測 sysid 唯一。
4. 5G 走 VPN/公網路由，不依賴廣播。
5. 機上設定納入 uav-onboard repo 管理（設定檔範本＋部署腳本），
   不要每台手改。

## 連線層設計定案（2026-08-11，使用者拍板）

使用者明確表示「**RB5 規格比較死、難改 config**」，故翻轉先前評估：

- **主路線＝地面接 14550 relay/demux**（等同把 tee 搬回地面，使用者知情取捨）。
- **機上設定管理化＝fallback**：`onboard/rb5-setup/`（已改好的三實例 RB5）
  保留為 fallback 路徑，不是主路線。
- **5G 唯一繞不開的機上設定**：機端至少要能指定 GCS IP（unicast 目標，
  廣播不過 5G）——範圍最小化到「填一個 IP」。

### relay 選型：mavlink-router vs 自寫輕量 relay → **選自寫輕量 relay**

需求（使用者定）：backend 零出向要在 relay **設定層**保住（backend 端點
forward-only/sniffer、command 端點雙向）；與 QGC 備援的 14550 搶埠要互斥。

| | mavlink-router | 自寫輕量 relay（建議） |
|---|---|---|
| backend 零出向 | 路由本質雙向，**無法逐端點強制 forward-only**——零出向只能靠 backend 自身不送，relay 層保不住 | **建構上保證**：relay 只把 command 端點的出向送回機，backend 端點永不回送 |
| 既有基礎 | 需另裝服務、config | **已寫過並驗證**這個模式（退役的 capture tee：綁埠、收 datagram、last_address 回程）——約 100 行、純標準庫 |
| 埠互斥/QGC | 需 config | relay 擁 14550，QGC 走別埠或由 relay 轉一份（程式碼可控） |
| 相依 | 外部二進位 | 零相依，跟 command/backend 同棧 |

決定性理由：使用者的硬需求「backend 零出向在 relay 設定層保住」，
mavlink-router 做不到逐端點方向強制，自寫 relay 可**建構上保證**。且這個
tee/demux 模式我已實作驗證過（capture.py 的前身）。

### relay 設計（地面站，待實作）

```
RB5 ──14550 合流──▶ uav-relay（綁 14550）─┬─ 單向 ──▶ backend ingest（唯讀，永不回送）
        ◀── 指令回程（last_address）──────┴─ 雙向 ──▶ command 服務
```

- relay 綁 14550、學 RB5 來源位址（last_address）；每框架扇出給 backend 與
  command 兩內部埠。
- **backend 方向強制單向**：relay 對 RB5 的回送**只**來自 command 端點；
  backend 端點的任何東西都不回送（零出向由 relay 建構保證，不靠 backend 自律）。
- QGC 互斥：relay 擁 14550；要並用 QGC 時由 relay 多轉一份給 QGC 端點
  （指令來源要標明，避免雙指揮）。
- sysid demux 與現行 command 一致（單埠多機）；voxl-mavlink-server <1.4.12
  的 sysid=1 bug 仍需逐台 MAV_SYS_ID＋升版把關（本 issue 上段）。

## 解決方式

（closed 時補）
