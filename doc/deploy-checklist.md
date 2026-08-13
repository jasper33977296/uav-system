# 地面站部署前檢查清單（現場照著走）

> 用途：把系統從開發（SITL）搬到地面站接真機（RB5）之前／到現場後，**逐項打勾**。
> 每步附指令與預期結果。卡住看 §6 故障排除。完整背景見 `doc/deployment.md`。
> 埠約定：**14540＝資料唯讀通道（backend）／14541＝指令雙向通道（command）**／
> 14550＝QGC。API：backend 38000、command 38001、前端 33000。

---

## ⚠️ 幾何圍欄是逐場域決策，勿沿用模擬值

`GEOFENCE_RADIUS_M` / `GEOFENCE_ALT_M` 在本 repo 的 `.env` 是 **500m / 60m**，
那是**模擬環境**的值——選它是為了蓋過研究航線（干擾區穿越 296m / 50m）。

**真機部署必須依場地實況重設**（程式預設 50m / 15m 反映的是一般實體試飛場）。
判斷依據是場地邊界、法規限制與失控回收半徑，不是「模擬環境用多少」。

**為什麼特別標這條**：2026-08-13 之前模擬環境用 50m 跑 296m 的研究航線，
結果是**每條研究航線都紅字**。常態性存在的警告等於沒有警告——大家會學會無視它，
真正該擋的那一次也擋不住。圍欄值要能讓預檢維持「真的在把關」的狀態，
過寬與過嚴一樣有害。


## 1. 地面站環境設定（`.env`）

- [ ] `LINK_SOURCE=modem`（真機模式；`simulated` 會拒收機上 POST、機上資料一筆都進不來）
- [ ] `MAVLINK_URL=udpin://0.0.0.0:14540`（backend 收資料通道）
- [ ] `COMMAND_MAVLINK_URL=udpin://0.0.0.0:14541`（command 收指令通道；**不是** SITL 的 14550）
- [ ] `ENABLE_COMMANDS`：**先 `false`**（只觀察、不發指令、不發 GCS 心跳）→ 首飛前單機驗收 OK 後才改 `true`
- [ ] `COMPOSE_PROFILES` 不含 `sim`（生產環境不起 SITL **與測試影像流**；範本預設已無）
- [ ] `AUTOREGISTER_SIMULATED=false`（自動註冊的是真機，別標成模擬）
- [ ] `VIDEO_RETENTION_DAYS=7`（影像保留天數。**這一個變數同時餵錄製器清檔與 API 回給
      前端顯示的天數**，所以畫面說幾天就是真的清幾天。改它要同步重算磁碟，見 §1b）
- [ ] `VIDEO_RECORD_ENABLED=true`（預設開；特定實驗要關見 §4b 末）

## 1b. 磁碟（有飛行影像時要先算）

量測資料 30 天只有數 GB，**影像是磁碟的主要消耗者**（大兩三個數量級）。
現行定案：**影像保留 7 天＋720p15**，以 3 台×每天 2 飛行小時計約
**49 GB**（端到端實測；即時串流的 zerolatency 調校比離線編碼多約 40%）
——現有磁碟（≥100GB）夠用。

- [ ] 確認磁碟餘裕 ≥ 70GB（49GB 影像＋量測資料＋餘裕）
- [ ] **改設定就要重算**：1080p30＋7 天＝95GB；720p15 但保留 30 天＝150GB；
      1080p30＋30 天＝**407GB**（要加碟）。飛行時數比上述多也要按比例放大。
      換算表見 [flight-video-design.md](flight-video-design.md) §6。

## 2. 起服務＋自檢

- [ ] `docker compose up -d`（起 db／backend／command／frontend，**不含** sitl）
- [ ] backend 健康：`curl http://localhost:38000/healthz`
      → 預期 `{"ok":true,"mavlink_connected":false,"link_source":"modem"}`（未接機時 `false` 屬正常）
- [ ] command 健康：`curl http://localhost:38001/healthz` → 200、`drones` 空（未接機）
- [ ] 前端可開：瀏覽器 `http://<地面站IP>:33000`

## 3. 機上設定（每一台 RB5，逐台做）

背景與病因見 `issues/016-rb5-platform-connectivity.md`、範本在 `onboard/rb5-setup/`。

- [ ] **voxl-mavlink-server 版本 ≥ 1.4.12**（<1.4.12 有「全機 sysid 被重設為 1」的 bug，
      會打掉單埠 sysid demux、多機全被當同一台）：在 RB5 上查版本
- [ ] **套用兩通道 MAVLink 設定**（出廠是廣播 :14550，5G 打不到我方埠 → 不改＝連不上）：
      ```bash
      # 在 RB5 上（root）。先 dry-run 看要做什麼：
      sudo ./configure-mavlink.sh --gs-ip <地面站IP>
      # 確認後真的寫入，並指定這台的唯一 sysid：
      sudo ./configure-mavlink.sh --gs-ip <地面站IP> --sysid <N> --apply
      ```
- [ ] **每台 `MAV_SYS_ID` 唯一**（單埠多機靠 sysid demux；重號＝混料，backend 會發
      `sysid_addr_change` 告警）。逐台設不同 N，套用後**實測唯一**（見 §4 第 1 步）
- [ ] 三條 mavlink 實例都起得來（PX4 實例有上限，通常 4；出廠已佔幾個，超額會靜默失敗
      ——`configure-mavlink.sh` 會先數、位子不夠會中止提示）
- [ ] **5G 量測節點 `.env`（每台各設，`onboard/.env`）**：
      | 變數 | 每台要填什麼 |
      |---|---|
      | `GROUND_API` | `http://<地面站IP>:38000`——**每台都填同一個**（＝§4 的地面站 IP）|
      | `MAV_SYSID` | **這台的 sysid，與上一步設的 `MAV_SYS_ID` 相同**（逐台不同：1、2、3…）|
      多機**必設 `MAV_SYSID`**：不設的話每台送回的即時訊號都會被記到「主機」名下
      （靜默混料、僚機的即時卡空白）。節點啟動時用它向地面站解出 drone_id——drone_id 是
      地面站 UUID，要該機首次連上被自動註冊才存在，所以機上填 sysid 不填 UUID。
- [ ] 裝成服務並看到解析成功：`sudo ./install.sh`（會先 preflight 檢查地面站可達性與
      AT 埠）→ `journalctl -u uav-link-node -f` 應出現
      `[identity] sysid N → drone_id <uuid>（樣本開始送出）`
      - 若持續印「地面站還沒有 mav_sysid=N 的機」：該機還沒被地面站自動註冊
        （先確認 §3 的 MAVLink 設定生效、前端無人機頁看得到這台）。此時節點**刻意不送**
        樣本、留在緩衝，解出後自動補傳，不會掉資料。

## 4. 5G 連線可達性（機↔地面站）

- [ ] RB5 能路由到地面站 IP：在 RB5 上 `ping <地面站IP>` 通
- [ ] 機上是 **unicast static IP**（`-t <地面站IP>`），不靠廣播（廣播不過 5G）
- [ ] 地面站防火牆放行 UDP 14540／14541（機主動打進來）
- [ ] （QGC 若要用）我方需**加聽 14550**（voxl 埠寫死），或機上架 mavlink-router 轉埠

### 4.1 影像相關埠（**8189 UDP 最容易被忽略**）

| 埠 | 方向 | 用途 | 不通的症狀 |
|---|---|---|---|
| **554/TCP**（機端）| 地面站 → 機 | **拉流：地面站去連相機 RTSP**（RB5 不主動推）| `paths/list` 的 path 一直 not ready |
| **8554/TCP** | 機 → 地面站 | 僅「退回推流」備案時才需要 | 用推流備案時前端沒有影像入口 |
| **8889/TCP** | 瀏覽器 → 地面站 | WHEP 訊令（建立播放連線）| 影像窗轉圈或報錯 |
| **8189/UDP** | 瀏覽器 ↔ 地面站 | **WebRTC 媒體本身** | ⚠️ **頁面一切正常、畫面全黑** |

- [ ] 放行 8554/TCP、8889/TCP、**8189/UDP**
- [ ] **跨 VPN／跨網段時特別確認 8189 UDP 通**——訊令走 TCP 會成功建立連線，媒體走
      UDP 卻過不去，於是**頁面沒有任何錯誤、就是不出畫面**。這個症狀最容易被誤判成
      「影像壞了」，實際是網路。

## 4b. 飛行影像（issue 022）

### 4b.1 服務組成（哪個要起、哪個絕對不能起）

- [ ] **`uav-video`（MediaMTX）要起**——它是**正式元件**（收流／錄影／瀏覽器播放），
      沒有掛 profile，`docker compose up -d` 會自動帶起。確認：
      ```bash
      docker ps --filter name=uav-video --format '{{.Names}} {{.Status}}'
      ```
- [ ] ⚠️ **`uav-video-testsrc-*` 絕對不能在真機環境跑**——它們推的測試彩條會與真相機
      **搶同一條 path 名**（`uav-N`），結果是畫面變成測試圖樣或兩邊互踢。它們掛在
      `sim` profile，只要 `.env` 沒有 `COMPOSE_PROFILES=sim` 就不會起。**動手確認**：
      ```bash
      docker ps --filter name=testsrc --format '{{.Names}}'   # 應該完全沒有輸出
      ```

### 4b.2 相機接入：**地面站主動去拉**（RB5 的 RTSP 不會自己推）

⚠️ **方向與 MAVLink／量測資料相反，這是本節最需要現場確認的前提。** RB5 的
RTSP 伺服器不會主動推送，要地面站當 client 去拉；而系統其餘部分都是「機端主動
外連」（因為 5G 機端多在 NAT 後）。**拉得到拉不到，取決於你的私有 5G＋VPN 拓撲，
請實測。**

- [ ] **先確認地面站連得到機端**（這一步不過，後面都不用做）：
      ```bash
      ping <機IP>                                   # 路由通不通
      nc -vz <機IP> 554                             # RTSP 埠開不開（或相機用的埠）
      ```
      不通就跳到本節末的「退回推流」。
- [ ] **用 ffmpeg 先確認拉得到影像**（排除 MediaMTX 變因）：
      ```bash
      ffmpeg -rtsp_transport tcp -i rtsp://<機IP>:554/<路徑> -t 5 -f null -
      ```
      看得到 `Stream #0:0: Video: h264` 就對了；**編碼必須是 H.264**
      （H.265 錄得下來但瀏覽器播不出）。
- [ ] **設定 MediaMTX 逐台拉流**（`video/mediamtx.yml` 的 `paths`，`uav-N` 對應該機 sysid）：
      ```yaml
      paths:
        uav-1:
          source: rtsp://<機1IP>:554/<路徑>
          sourceOnDemand: yes      # 沒人看也沒在錄就不拉——閒置不佔 5G 上行
        uav-2:
          source: rtsp://<機2IP>:554/<路徑>
          sourceOnDemand: yes
      ```
      改完 `docker compose up -d uav-video`（改設定檔要重建，見 deployment.md）。
- [ ] **驗流真的進來了**（地面站）：
      ```bash
      curl -s http://localhost:9997/v3/paths/list | python3 -m json.tool
      ```
      每台應有 `"name": "uav-N"`、`"ready": true`、`"tracks": ["H264"]`。
      （`sourceOnDemand: yes` 時要先有人看或正在錄，`ready` 才會是 true。）
- [ ] 建議相機設 **720p15、2 Mbps 上限**：磁碟與 5G 上行都吃得消（見 §1b）。

**退回推流（拉不到時）**：在 RB5 上跑一支推流程序把相機 RTSP 推到地面站——
MediaMTX 兩種都吃、不必改架構，代價是機端多一個要顧的常駐程序：
```bash
# 在機上執行（<地面站IP> 與 uav-N 換成實際值）
ffmpeg -rtsp_transport tcp -i rtsp://<相機IP>:554/<路徑> \
  -c copy -f rtsp -rtsp_transport tcp rtsp://<地面站IP>:8554/uav-1
```
此時 `mediamtx.yml` 的該 path 維持 `source: publisher`（預設）。

### 4b.3 每台機的 `video_url`

- [ ] 到前端「無人機」頁逐台設定「影像」：
      `http://<地面站IP>:8889/uav-<sysid>/whep`
- [ ] ⚠️ **`<地面站IP>` 必須是「操作端瀏覽器連得到」的位址**，不是 `localhost`，也不一定
      是地面站自己看到的網卡 IP。判斷方法：**就用你在網址列開前端的那個位址**。
      填錯的症狀是「其他功能都正常、只有影像空白」，很容易被誤判成影像壞掉。
      （長期修法已列 backlog：`video_url` 只存 path、host 由前端動態帶，換位址永不失效。）

### 4b.4 錄影行為與開關

- [ ] 錄影**只在架次期間**（armed→disarmed）自動進行，待機不寫檔（可用 §1b 的磁碟估算）。
- [ ] 落地後**多錄約 3 秒**是刻意的（避免切掉落地瞬間），不是異常。
- [ ] **要在特定實驗關閉錄影**（例如影像上行會干擾被測鏈路時）：`.env` 設
      `VIDEO_RECORD_ENABLED=false` → `docker compose up -d uav-backend`。
      關閉時該架次會標記 `video_mode=off` 留痕，回放頁顯示「本趟未啟用錄影」——
      **與「錄了但斷流」在資料上分得開**，不會事後分不清是沒錄還是錄失敗。

## 5. 首飛驗收順序（**單機先 → 編隊後**）

### 5a. 單機（每台各驗一次）
- [ ] 機上線後 backend 自動註冊該機，前端「無人機」頁看得到、`mav_sysid` 正確且**唯一**
- [ ] **機上資料完整性**（真機資料回傳的唯一自檢工具）：
      ```bash
      python3 scripts/check-onboard.py http://<地面站IP>:38000 30
      ```
      → 逐項綠：source=modem、~1Hz、RF 欄位（sinr/rsrp/pci/band…）非空、位置綁定、時鐘偏差 <5s
- [ ] 設 `ENABLE_COMMANDS=true` 並重起 command（`docker compose up -d uav-command`）後，
      單機 arm→takeoff→RTL 走一遍（前端或 `POST :38001/api/command/<sysid>/takeoff`），落地正常
- [ ] armed 時 DB 真的有新 `telemetry`／`link_metrics` 列（check-onboard 第 6 項會抽查）

- [ ] **路徑管理頁「從機上讀回」實際點一次**，確認任務入庫。
      ⚠️ 這條路徑**在多機模擬環境曾長期不通**（fanout 的 backend 腿是單向的、
      backend 送不出查詢，2026-08-12 才修），沒人發現只因為沒人用過——**真機是
      第一次真正驗證它**，請務必實際點一次而不是假設它會動。
- [ ] **看讀回後的預檢報告內容**，不只確認「鈕會動」。那份報告會告訴你**機上此刻
      實際掛著什麼任務、合不合現行安全設定**——那正是這顆鈕的價值。
      模擬環境實測讀回的任務有 5 項違規（高度 30m 超過上限 15m、離起飛點 196/307m
      超過圍欄 50m、最後一個導航項不是返航/降落），是先前實驗殘留。**真機若也長
      這樣，先弄清楚機上掛的是什麼再飛。**

### 5b. 編隊（≥2 台，單機都過之後）
- [ ] 前端建群組任務（unified 或 separate）→ 執行 → 各機 phase 逐台推進到 flying
- [ ] 緊急鈕：群組 RTL-all／abort 能全撤、各機安全返航落地

## 6. 安全注意事項

> ⚠️ **separate 模式編隊：各機任務請規劃「不同飛行高度」。**
> 顯式 RTL 返航高度錯開（`GROUP_RTL_STAGGER_M`）**尚未實作**（設計已知限制，
> 見 `doc/group-missions-design.md` §10.2）。unified 模式各台自動高度分層（vsep）、
> 返航會自然錯開；但 **separate 模式若各機任務高度相同，緊急 RTL-all 的返航高度分離
> 無保證**——各機可能以同高度往同一 home 上空匯合。**操作規避＝separate 編隊任務
> 規劃不同高度**，直到未來版本補上顯式錯開。

- [ ] 首飛在開闊、可視、低風場地，Home 點淨空
- [ ] 每台在前端確認 `ready=True`（PX4 預檢過）才 arm
- [ ] 已知 sysid 唯一（§3、§4）——重號會讓指令送錯機

## 7. 故障排除速查

| 症狀 | 最可能原因 | 查 |
|---|---|---|
| 前端看不到機 | 機上沒改設定（還在廣播 :14550）／5G 打不到 14540 | §3、§4；`ping`、`ss -ulnp` 看 14540 有無封包 |
| 多機互相干擾、`sysid_addr_change` 告警洪水 | sysid 重號（voxl <1.4.12 bug 或沒逐台設） | §3 版本＋逐台唯一 sysid |
| 有遙測、`check-onboard` 說位置空 | 機上 router 沒把 MAVLink 餵給 onboard node 的 PX4_URL | §3 第三條 lo 實例 |
| 指令送出無反應 | `ENABLE_COMMANDS=false`／command 收在 14541 但機打 14540 | §1、§5a |
| `check-onboard` 收 409 / source 非 modem | 地面站 `.env` 還是 `simulated` | §1 `LINK_SOURCE=modem` |
| **頁面一切正常、影像窗全黑**（無錯誤訊息）| **8189/UDP 不通**：WHEP 訊令走 TCP 建得起連線，媒體走 UDP 過不去 | §4.1；跨 VPN／跨網段特別容易 |
| 其他功能都好、**只有影像空白** | `video_url` 的 host 不是操作端瀏覽器連得到的位址 | §4b.3——用你網址列開前端的那個位址 |
| 影像出現**測試彩條**而非相機畫面 | `uav-video-testsrc-*` 在跑，搶了同名 path | §4b.1：`docker ps --filter name=testsrc` 應為空 |
| `paths/list` 有流但影像播不出 | 相機送 H265（錄得下來、瀏覽器播不出）| §4b.2：相機改 H.264 + yuv420p，看 `tracks` |
| 架次回放**沒有影像**（`video_status`）| `off`＝本趟刻意沒錄／`no_source`＝該機沒來源／`expired`＝過保留期已清／`missing`＝**該錄卻沒錄到（故障）** | 只有 `missing` 要查：看 `uav-video` 是否活著、該 path 當時 ready |
| 錄影一直沒有檔 | 錄影只在 armed 期間進行；待機不寫檔是正常 | §4b.4 |

## 7b. 現場順手量兩個數字（只有真機量得到）

以下兩項**模擬環境量不出真值**，現場跑清單時順手記一筆就好，日後判斷都靠它們：

- [ ] **影片時間 vs 遙測時間的偏差**：用畫面上看得出來的瞬間（**離地那一刻**、
      或飛進干擾區時畫面開始糊的那一刻），比對影片時刻與遙測事件時刻，記下差幾秒。
      - 模擬環境基線＝ **0.41s**（影片比遙測晚，即定位到 T 會看到 T+0.41s）
      - **建議好鏈路、劣化鏈路各量一次**：若兩者差很多，代表延遲會隨鏈路品質變動
        ——那會讓「畫質劣化 vs SINR」的對照被系統性拖移，屬研究效度問題
      - **> 1s 請回報**（達到重議門檻，見 flight-video-design.md）
- [ ] **參數抓取耗時**（021 Phase 2 上線後）：模擬環境是 851 個參數約 3 秒；
      真機 5G 下記一筆實際秒數。

## 8. 從舊版升級（既有資料庫）

- [ ] **先備份再啟動新版**：`docker exec uav-db pg_dump -U uav -d uav | gzip > backup.sql.gz`
      （backend 啟動時會自動跑 schema 遷移，冪等但不可逆）
- [ ] 023 遷移會**移除 `missions` 的 `status`／`geometry`／`drone_id`** 三個從未使用的欄位、
      新增 `kind` 並自 `created_by` 回填、新增 `flight_sessions.mission_name` 快照並回填、
      兩處外鍵改 `ON DELETE SET NULL`。**現有資料不受影響**（開發環境實測遷移前後
      7 張表筆數完全一致）。若你有自己寫的查詢或腳本讀那三個欄位，要先改。
- [ ] 022 遷移會新增 `video_segments` 表與 `flight_sessions.video_mode`。
      **升級前的舊架次會被回填成 `video_mode='off'`**（它們本來就沒有錄影，
      標成「本趟未啟用錄影」是事實；不回填的話會被判讀成「該錄卻沒錄到」的故障）。
