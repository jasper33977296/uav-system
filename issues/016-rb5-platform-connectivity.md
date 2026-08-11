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

## 連線層設計定案（2026-08-11 最終，使用者澄清後）

**現行兩通道設計即為正解**——使用者澄清：機端**已設定開兩個 port 分別做
資料與控制**（即我們先前改好的那台 RB5 三實例設定就是現況）。所以：

- 「14550 出廠合流」問題**在本專案不存在**：機端主動 unicast 打我方
  14540（資料唯讀）／14541（指令雙向）。
- **地面 relay/demux 方案取消**（曾在誤以為需接 14550 合流時評估過，
  使用者澄清後停止，不投入）。
- **「機上不裝 router」定案維持，不破例**。
- **機端兩 port 設定＝前提條件**：`onboard/rb5-setup/`（設定範本＋部署腳本，
  以已驗證的三實例 RB5 為 known-good）是新機/其他單元接入時的正式套用工具，
  不是 fallback。

### 收斂後的剩餘風險項

1. **新機/其他 RB5 單元接入的機端設定套用**：用 `onboard/rb5-setup/`
   範本＋腳本，避免每台手改（腳本待真機驗證）。
2. **多機 sysid 唯一性**：voxl-mavlink-server 升 ≥1.4.12（<1.4.12 有全機
   sysid 重設為 1 的 bug，打掉單埠 demux）＋逐台設唯一 `MAV_SYS_ID`、實測。
3. **5G unicast 路由**：機端 GCS IP 可達性（VPN/公網路由），不依賴廣播
   （廣播不過 5G）。這是 5G 場景唯一繞不開的機端前提。

### 附：已撤銷方案（歷史記錄）

曾在**誤以為**機端只有 14550 出廠合流時，評估過「地面接 14550 relay/demux」
（含 mavlink-router vs 自寫輕量 relay 選型，結論傾向自寫以建構上保住 backend
零出向）。使用者澄清機端本來就兩 port 分流後**整條撤銷、不投入**。
**重啟觸發條件**：若未來出現真正改不了機端設定的單元（例如 voxl-mavlink-server
代且無法加 PX4 mavlink 實例），才重議地面 relay。

## 解決方式

（closed 時補）
