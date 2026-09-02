# 部署文件：地面站安裝與完整設定

本文是**真機部署**（伴飛電腦 ＋ 飛控 ＋ 地面站）的完整操作手冊，含所有設定項。
模擬環境（SITL 開發）見文末附錄。設計依據：
[onboard-telemetry.md](onboard-telemetry.md)（部署拓撲）、
[architecture.md](architecture.md)（系統定位）。

> ## ⚠️ 2026-09-02 改寫：本文原本寫的是 **RB5 平台**，那與現況不符
>
> 舊版的 §2 描述的是「RB5 跑 PX4、用 UDP 開多個 mavlink 實例、5G 量測由獨立的
> `onboard/` node 負責」。**現役機上平台完全不是那個形狀**：
>
> | | 舊文件（RB5） | 現況 |
> |---|---|---|
> | 伴飛電腦 | Qualcomm RB5 | **Raspberry Pi 5** |
> | 飛控 | PX4（機上同一塊板）| **ArduPilot 4.7**，獨立飛控板 |
> | 飛控↔伴飛 | UDP（同機內） | **UART 57600（GPIO14/15）** |
> | 對地通道 | PX4 開三個 mavlink 實例 | **uav-agent** 轉發 |
> | 5G 量測 | 獨立的 `onboard/` node | **併進 uav-agent**（`modem.py`）|
>
> **照舊版做會做出一台不存在的機。** 這正是 [issues/016](../issues/016-rb5-platform-connectivity.md)
> 記的那些 RB5 平台問題（廣播 14550、廣播不過 5G、voxl 的 sysid=1 bug）
> **不再適用的原因——它們是 RB5 專屬的**。
>
> RB5 時代的內容移到 [附錄 A](#附錄-a-rb5-平台備查已非現役)，**沒有刪除**：
> 若日後回頭用 RB5，那些實測過的坑仍然成立。

```
  無人機
  ├─ 飛控（ArduPilot 4.7）
  │    ↕ UART 57600（GPIO14/15）
  └─ 伴飛電腦（Raspberry Pi 5）跑 uav-agent
       │ ① MAVLink/UDP  → 地面站:14540（遙測，唯讀）＋ :14541（指令，雙向）
       │ ② WebSocket    → 地面站:38000 /ws/agent（意圖通道）
       │ ③ HTTP over 5G → 地面站:38000 /api/link-metrics/live（5G 量測）
       ▼
  地面站（Ubuntu + Docker：本系統五容器）
```

全部由**機上主動外連**——SSH 只在安裝日用一次，運行期間零人為連線。
（代理主動撥出是刻意的：換到電信商 APN 後飛機在 carrier NAT 後面，
地面站連不進去；撥出在兩種拓撲都成立。）

> 🚀 **到現場照著打勾的精簡版**見 [`deploy-checklist.md`](deploy-checklist.md)——env／埠、
> 服務起法、每台機的機上設定驗證、5G 可達性、首飛驗收順序（單機先→編隊後）、安全注意
> 事項。本文是完整手冊與背景；清單是現場操作面。

---

## 0. 前提清單

| 項目 | 需求 |
|---|---|
| 地面站 | Ubuntu 22.04+，4 核 8GB RAM 起，磁碟 ≥ 100GB（原始資料 30 天約數 GB，餘量給匯出檔）|
| 網路 | 地面站與無人機的 5G 網路互通（私有 5G 直達；公網電信見 §3.3）|
| 地面站 IP | **必須固定**（DHCP 保留或靜態）——機上兩條流都寫死這個位址 |
| 無人機 | 伴飛電腦（Raspberry Pi 5）＋ 飛控（ArduPilot 4.7，UART 相接）；modem 已能上網。**飛控必須拿得出 `AUTOPILOT_VERSION.uid2`**——沒有板號就沒有身分，也就不可被指揮（issues/040）|
| 機上代理 | `uav-agent` 已安裝並設為開機自啟。**本系統只指揮有代理的機**（2026-09-02 裁定）|
| 本文佔位符 | `<GS_IP>`＝地面站固定 IP（下文範例用 `192.168.55.10`）|

---

## 1. 地面站安裝

### 1.1 取得專案並初始化（純 Docker，不需 setup.sh）

前提只有 Docker 本身（含 compose plugin）：

```bash
sudo apt install -y docker.io docker-compose-v2   # 或官方安裝腳本
sudo usermod -aG docker $USER && newgrp docker
```

然後：

```bash
git clone <repo> uav-system && cd uav-system
cp .env.example .env      # 範本即部署形（不含模擬器 profile）
```

其餘一切都在容器裡：backend 依賴在映像內；**frontend 是 production
build**——`next build` 於映像建構時完成（含型別檢查），執行的是
`next start` 服務預編譯產物，不是開發伺服器。
`scripts/setup.sh` 是**開發環境**工具（SITL 測試腳本的 venv、埠偵測、
啟用 sim profile 與前端熱重載覆寫），部署不需要。

`.env` 內容（連接埠一律 30000 以上，避開系統服務）：

```bash
DB_PORT=35432        # TimescaleDB
BACKEND_PORT=38000   # FastAPI（HTTP API + WebSocket + 機上 push 端點）
FRONTEND_PORT=33000  # 前端
```

### 1.2 設定真機模式

**網路層設定集中在根目錄 `.env`**（`setup.sh` 會產生；範本見 `.env.example`）。
真機部署改一行：

```bash
LINK_SOURCE=modem        # ★ 真機模式：鏈路量測由機上 POST 進來
# （範本預設已無 COMPOSE_PROFILES=sim，模擬器不存在於本環境）
```

改完 `docker compose up -d` 重載即生效。

**機的身分在系統端設定**（不走 .env）：全新環境會自動建立預設主機
`uav-1`，開站後到前端「無人機」頁**改名**成你的機名（如 rb5-uav-1）即可；
之後註冊其他機、切換「主機」（MAVLink 遙測記在哪台名下）也都在該頁操作。

`LINK_SOURCE` 是模擬／真機的總開關：

| 值 | 行為 |
|---|---|
| `simulated` | backend 自己模擬 5G 鏈路（開發用）；push 端點回 409 |
| `modem` | 鏈路資料只接受機上 push；地圖不畫任何模擬假設圖層 |

SITL 模擬器屬 `sim` profile，部署 .env 沒有 `COMPOSE_PROFILES=sim` 就
**永遠不會啟動**——不需要記得排除它：

```bash
docker compose up -d
```

四個服務都是 `restart: unless-stopped`——地面站重開機自動復活。

### 1.3 backend 全部設定項（環境變數）

`apps/backend/app/config.py`，皆可用環境變數覆寫（大寫同名）：

| 變數 | 預設 | 說明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://uav:uav@localhost:35432/uav` | DB 連線 |
| `MAVLINK_URL` | `udpin://0.0.0.0:14540` | MAVLink 監聽位址 |
| `LINK_SOURCE` | `simulated` | `simulated` / `modem` |
| `BROADCAST_HZ` | `5.0` | WebSocket 推送頻率 |
| `DB_WRITE_HZ` | `1.0` | 遙測入庫頻率（armed 時）|
| `SINR_DEGRADED_DB` | `5.0` | 鏈路事件門檻：低於此值→劣化 |
| `SINR_LOST_DB` | `-2.0` | 低於此值→瀕斷 |
| `SINR_HYSTERESIS_DB` | `3.0` | 回升遲滯（防門檻抖動）|
| `HANDOVER_MARGIN_DB` | `6.0` | 模擬器專用，真機不適用 |
| `CAPTURE_ENABLED` | `true` | 原始層錄製：MAVLink 每框架無損落盤（tlog）|
| `CAPTURE_DIR` | `/data/mavcap` | 錄製目錄（compose 的 `mavcap` volume）|
| `CAPTURE_KEEP_DAYS` | `30` | 錄製檔滾動保留天數（~61 MB/hr）|
| `GEOFENCE_RADIUS_M` | `50` | 任務幾何預檢：圍欄半徑（與 QGC Geofence 一致）|
| `GEOFENCE_ALT_M` | `15` | 同上：高度上限 |
| `GEOFENCE_ENFORCE` | `false` | 預檢預設純參考不擋；true 時上傳超標任務回 409 |

> 門檻只影響**事件通知**；研究分析以 1Hz `link_metrics` 原始資料為準，
> 事後可用任何門檻重新計算（事件是衍生資料）。

### 1.4 前端設定

預設**零設定**：API 位址由瀏覽器網址自動推導（開 `http://<GS_IP>:33000`
就會連 `<GS_IP>:38000`）。只有前後端分屬不同主機才需要
`apps/frontend/.env.local` 指定 `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL`。

### 1.5 QGC

地面站桌面安裝 [QGroundControl](https://qgroundcontrol.com/)。不需設定連線
——機上 mavlink-router 會主動送到 `<GS_IP>:14550`，QGC 預設監聽即自動連上。
分工與完整作業流程見 [qgc-integration.md](qgc-integration.md)。

### 1.6 安裝驗證（無人機還沒到也能做）

```bash
curl http://localhost:38000/healthz
# {"ok":true,"mavlink_connected":false,"link_source":"modem"}  ← 未連機屬正常
```

瀏覽器開 `http://<GS_IP>:33000`：三顆狀態圓點應為 綠（後端）／紅（MAVLink
等待中）／空心（待機）。

---

## 2. 機上一次性安裝（伴飛電腦 ＋ 飛控）

> SSH 進機上只做這一節，做完之後運行期間不再需要任何人為連線。
>
> **RB5 時代的做法見 [附錄 A](#附錄-a-rb5-平台備查已非現役)。**

### 2.1 接線與飛控參數

飛控用 **UART 接到 Pi 的 GPIO14/15，57600**。Pi 這一側要先關掉序列埠的
console，否則系統會跟飛控搶那條線。

飛控參數逐項核定見 [`verification-checklist.md`](verification-checklist.md)，
其中兩項與本系統直接相關、**現場一定要對**：

| 參數 | 值 | 為什麼 |
|---|---|---|
| `SYSID_MYGCS` | **255** | 飛控用它決定「誰的心跳算 GCS 心跳」，那是 `FS_GCS` 的判準。我方 GCS sysid 是 255（＝ArduPilot 出廠預設，所以正常不用動）|
| `FS_GCS_ENABLE` / `FS_GCS_TIMEOUT` | **1 / 45** | 飛控的失聯處置當代理的**後備**。45 必須大於代理的 `LINK_LOSS_MAX_SOLO_S`（30s），否則飛控會搶在代理的分狀態處置之前動作（issues/039） |

> **勾的是「關係成立」不是那兩個數字**：`FS_GCS_TIMEOUT` 在飛控、
> `LINK_LOSS_MAX_SOLO_S` 在代理——**分屬兩邊的關係正是最會漂移的那種**。
> 代理開機時會自己讀回來比對並在不成立時大聲說。

### 2.2 安裝 uav-agent

代理是**飛控與地面站之間的橋**，也是本系統的入列前提：**沒有代理的機只能看，
不能指揮**（2026-09-02 裁定）。它同時承擔了舊架構裡 `onboard/` 那個 5G 量測
node 的工作（`modem.py`），所以**不需要再另外裝一個量測程式**。

安裝與設定見 uav-agent 的 README。與地面站有關的設定項：

| 變數 | 範例 | 說明 |
|---|---|---|
| `GS_HOST` | `10.141.2.21` | 地面站固定 IP。**三條流都寫死這個位址** |
| `FC_DEV` / `FC_BAUD` | `/dev/ttyAMA0` / `57600` | 飛控的序列埠 |
| `TELEMETRY_PORT` / `COMMAND_PORT` | `14540` / `14541` | 遙測（唯讀）／指令（雙向）|
| `BACKEND_PORT` | `38000` | 5G 量測與註冊走的 HTTP |
| `MODEM_PORT` | `/dev/ttyUSB2` | 5G 模組的 AT 埠 |

systemd：`uav-agent.service`（repo 內附）。`systemctl enable --now uav-agent`。

> **改代理的程式要重啟服務才生效**，而重啟會中斷意圖通道幾秒。
> 機在地面且已上鎖時無害（失聯處置只在 armed 時介入）；**飛行中不要做**。

### 2.3 5G 量測（流 ③）

由代理的 `modem.py` 負責，不需要另外安裝。要點：

- RF 指標走 `AT+QENG="servingcell"`（SINR/RSRP/PCI）。**第一次接一顆新模組時
  先用 `tools/modem-probe.py` 印出原始回應**再校準解析——不要假設一次就對。
- 位置與時間由代理從飛控遙測綁定（**沒有定位就不填位置**：`0,0` 是哨兵不是座標）。
- 斷線期間的樣本**留在機上環形緩衝**，恢復後補傳（issues/039 C 層）。

### 2.4 上機首驗（第一個里程碑）

在已知干擾源附近做一次可控測試，確認 SINR 會隨干擾下降——Quectel 論壇有
RM500Q SINR 回報異常的前例，**不要假設數值正確**。這一測同時驗證整條鏈路：
modem → 代理 → HTTP → DB → 前端。

同時要看到的三件事：

1. `curl :38000/healthz` 的 `mavlink_connected` 轉 `true`
2. `curl :38001/healthz` 的 `drones` 出現 sysid
3. `curl :38000/api/admission/<sysid>` 回 **`admitted`**——**這一項是新的**：
   板號、配號、代理連線三者相符才算數，前兩項通過但這項不過的話，
   **看得到但指不動**（issues/040）

### 2.5 即時影像（選配）

前端地圖點擊機體會開啟即時畫面 modal，來源是每台機的影像串流位址
（無人機頁 →「影像」設定，存在系統端，換瀏覽器不用重設）。

瀏覽器不支援 RTSP，機上相機串流需先轉成瀏覽器吃的格式。建議機上跑
[MediaMTX](https://github.com/bluenviron/mediamtx)（單一執行檔）把 RTSP 轉 WHEP：

```bash
# 機上：mediamtx.yml 指定 source: rtsp://<相機>，跑起來後
# 無人機頁「影像」填：http://<機IP>:8889/<路徑名>/whep
```

MJPEG（IP cam 常見）與 MP4/WebM 位址也可直接填，前端依 URL 自動選播放器。
注意：影像走 5G 會與量測流量搶頻寬——研究量測時建議降碼率或只在需要時開啟。

---

## 3. 網路設定

### 3.1 連接埠總表（地面站）

| 埠 | 協定 | 用途 | 誰連進來 |
|---|---|---|---|
| 33000 | TCP | 前端 | 操作員瀏覽器（區網）|
| 38000 | TCP | API/WS ＋機上 push | 瀏覽器、機上 node |
| 14550 | UDP | MAVLink → QGC（**SITL 下：command 服務改聽此埠**，見下註） | 機上 mavlink-router／PX4 SITL 廣播 |
| 14540 | UDP | MAVLink → 本系統（ingest，唯讀） | 機上 mavlink-router／SITL |
| 14541 | UDP | MAVLink ↔ command 服務（雙向，**生產**，`ENABLE_COMMANDS=true` 時） | 機上 mavlink-router |

> **command 埠的生產 vs SITL 分歧**：上表 14541 是**生產**（onboard router 打 GS:14541）。
> **SITL 開發**沒有 onboard router，PX4 只廣播到 14550，故 SITL 的 command 服務改聽
> **14550**（本機 `.env` 設 `COMMAND_MAVLINK_URL=udpin://0.0.0.0:14550`）——此時 command
> 與 QGC 同搶 14550、**同機互斥**（見 issues/016）。`config.py` 預設值仍是 14541（生產）。
| 38001 | TCP | command 服務 API | 瀏覽器（階段 3 UI）、curl |
| 35432 | TCP | TimescaleDB | 僅本機（分析工具可直連）|

防火牆有開的話（`ufw`）：

```bash
ufw allow 33000/tcp && ufw allow 38000/tcp
ufw allow 14540/udp && ufw allow 14550/udp
```

### 3.2 私有 5G（實驗室情境）

機上與地面站同網段或可路由即可，無需其他設定。確認一次
`ping <GS_IP>`（從機上）雙向通。

### 3.3 公網電信（如日後需要）

機上在電信 NAT 後面。三條流都是機上主動外連，所以只要地面站有機上可達的
位址即可——最乾淨是 WireGuard：地面站起 wg 伺服器（開一個 UDP 埠），
機上開機撥入，所有流量（MAVLink、HTTP）走隧道內網 IP，設定檔裡的
`<GS_IP>` 換成隧道位址，其餘完全不變。

### 3.4 安全性（誠實聲明）

目前 **API 無認證、CORS 全開**——設計前提是部署在隔離的實驗網段
（地面站＋無人機＋操作員）。任何能連到 38000 的人都能刪資料。
若地面站會接入更大的網路，需先補：API token、CORS 白名單、
DB 密碼改掉預設值。這是已知邊界，不是疏忽。

---

## 4. 運行驗收清單

開機順序不拘（各元件互相重試）。全部就緒的判準：

1. `healthz`：`ok:true`、`mavlink_connected:true`、`link_source:"modem"`
2. 前端三顆圓點：綠／綠／（解鎖後）紅點「記錄中」
3. 側欄遙測跳動（高度、姿態、電量），5G 卡有數值且 `source` 為 modem
4. QGC 看得到載具、能上傳任務；上傳後在本系統**路徑管理 → 從機上讀回**
   驗證機上真的有（這是任務是否上傳成功的客觀判準）
5. 解鎖起飛 → 無人機頁自動出現新航線（進行中）；落地 → 摘要結算
6. 拔掉 5G 天線 10 秒（或遮蔽）→ 前端應顯示失聯、恢復後資料補齊無空洞
   （驗證機上緩衝與補傳）

---

## 5. 維運

| 事項 | 作法 |
|---|---|
| 資料生命週期 | 原始 1Hz 資料 **30 天自動清除**；1 分鐘彙總永久保留。要長期保留原始資料：無人機頁逐航線「匯出」（單一 JSON）後可「移除」 |
| MAVLink 原始錄製 | 機上傳出的**每一個 MAVLink 框架**無損保留於 `mavcap` volume（tlog、UTC 日切檔、30 天滾動）。取用：`docker cp uav-backend:/data/mavcap/<日期>.tlog .`，可用 QGC 回放或 pymavlink `mavlogdump.py` 解析（兩層收集設計見 `doc/gcs-replacement.md` §2）|
| 備份 | `docker exec uav-db pg_dump -U uav uav > backup-$(date +%F).sql`（排程丟遠端）|
| 更新版本 | `git pull && docker compose up -d --build --renew-anon-volumes` |
| 看日誌 | `docker compose logs -f uav-backend`（log 已設 50MB×3 上限，不會寫爆磁碟）|
| 服務狀態 | `docker compose ps`；全部 `restart: unless-stopped`，重開機自動復活 |

**已知注意事項**：backend 若在飛行中重啟，啟動時會自動補結算中斷的航線
（`recover_orphan_sessions`），資料不遺失但該航線摘要以中斷點為終點。

---

## 附錄：模擬環境（SITL 開發）

與真機部署的差異只有 .env 兩行：`LINK_SOURCE=simulated`＋`COMPOSE_PROFILES=sim`
（setup.sh 產生的預設即開發模式）：

```bash
docker compose up -d        # 含 sitl（由 sim profile 帶起）
```

- 測試飛行（純標準庫，經 command 服務——需 `ENABLE_COMMANDS=true`）：
  `python3 scripts/test-flight.py`（干擾區穿越）、
  `python3 scripts/fly-mission.py <任務名稱|mission_id|plan檔>`（上傳並執行）
- 群飛模擬：`POST /api/swarm/start?count=3[&mission_id=...]`
- 讓別台電腦的 QGC 連 SITL（臨時環境變數，不進 .env）：
  `SITL_QGC_HOST=<那台IP> docker compose up -d uav-sitl`

注意：SITL 是共用的一台——測試腳本上傳任務會**覆蓋**QGC 上傳的
（MAVLink 任務是整包替換），且 SITL 容器重啟機上任務即歸零。
用「路徑管理 → 從機上讀回」確認機上現況。

前端模式由 .env 的 `COMPOSE_FILE` 決定：部署（基底檔）＝production
build；開發加 `docker-compose.dev.yml` 覆寫＝bind mount + 熱重載。
前端程式碼變更後，部署環境要 `docker compose up -d --build` 重建映像
（build 產物在映像內）；開發環境改檔即生效。

## 接 ArduPilot 機的機端前提

> **標題原本是「與 RB5 兩 port 設定同一類」**（2026-09-02 拿掉）：ArduPilot
> 現在是**主要平台**，不是需要對照 RB5 才說得清楚的特例。

2026-08-12 以 ArduCopter SITL 實測（見 [issues/015](../issues/015-multi-autopilot-support.md)）。
接 ArduPilot 機時**必須**先處理下面三件，否則會遇到「看起來連上了但不對勁」：

1. ~~**機端 `SYSID_MYGCS` 要設成 254**~~ ——**2026-08-24 起不再需要**：
   該參數只管 `MANUAL_CONTROL`／RC override 的來源檢查，而虛擬搖桿已移除
   （[issues/035](../issues/035-remove-manual-control.md)）。arm、切模式、
   任務上傳走的 `COMMAND_LONG` **不受這個參數限制**。
   （若日後又需要從地面送連續操縱，記得它的失敗模式是**靜默丟棄、沒有任何
   錯誤訊息**；新版韌體該參數改名為 `MAV_GCS_SYSID`。）
2. **遙測要靠我方主動要求**：ArduPilot 預設幾乎不送（只有心跳等 4 種訊息）。
   backend 會在註冊後送 `REQUEST_DATA_STREAM` 並每 30 秒補送——**這是預期行為，
   不是多餘流量**；沒有它整台機是瞎的。
3. **不能用 SYS_STATUS 判斷 ArduPilot 可不可飛**：它不回報 `PREARM_CHECK` 位
   （實測 present=False）。就緒判定改看 EKF（`EKF_STATUS_REPORT`）與感測器健康位，
   預檢失敗的具體原因走 STATUSTEXT。

> ⚠️ **任務上傳目前不支援 ArduPilot**：ArduPilot 把 home 當 seq 0，直接送
> 0..N-1 會讓**第一個航點被 home 覆蓋而消失**（且回讀筆數相同、比對抓不到）。
> 修正前請勿用本系統對 ArduPilot 機上傳任務。

### ⚠ `--reload` 現在是開發環境限定（2026-09-01）

`--reload` 與原始碼掛載**已從基底 compose 移到 `docker-compose.dev.yml`**
（issues/033 §4.1 裁定）。在那之前它們寫死在基底檔，於是**部署環境編輯一個
檔案就會重啟服務**——不需要有人下 restart 指令。而 command 重啟會中斷指令通道，
在心跳解耦之前還會一併中斷 GCS 心跳、可能超過飛控的 `FS_GCS_TIMEOUT`。
**換句話說：在部署環境存一個檔案，就可能讓飛行中的機體觸發 failsafe。**

* **開發**：`.env` 加 `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml`
  （`scripts/setup.sh` 會自動設）。下面那條 WebSocket 卡死的注意事項只在這個
  模式下適用。
* **部署**：不設那一行。改原始碼**不會有任何效果**，要
  `docker compose up -d --build`。那個「不便」正是要的——
  **部署環境不該有一條靠人記得別踩的路**。

### 開發注意事項：backend `--reload` 會被常駐 WebSocket 卡死

開發環境 backend 掛 bind mount + `uvicorn --reload`，改 `apps/backend/app/`
下任何 `.py` 即熱重載。**坑（2026-08-12 實測）**：uvicorn 的 graceful
shutdown 會等所有連線關閉才重啟，而前端 `/ws/telemetry` 是**常駐 WS、不會
自己關** → reload 無限卡在 `Waiting for connections to close`，backend 直接
down（不是幾秒 blip，是無限期，直到手動介入）。「沒人在飛」擋不住這個——
WS 與 arming 無關、前端一開著就一直連著。

- **緩解**：`docker-compose.yml` backend 的 uvicorn command 加
  `--timeout-graceful-shutdown 3`，reload 最多卡 3s 自救。
- **沒加之前**：backend 改完寧可 `docker restart uav-backend`（可預期 ~8s、
  乾淨載新碼、SITL 機隊自動重連），不要賭 `--reload` 不卡。
- **多檔一次改**：先 `python3 -m py_compile <檔...>` 確認全部能編譯，再一次
  改完、一次 restart，避免中途 reload 載到半套。
- 使用者/機隊實測進行中時，backend 改動挑安靜窗口（`GET :38001/healthz`
  三台持續 disarmed）＋部署前複驗，並知會協作者會有一次短重啟。

### 開發注意事項：加前端相依要重裝 volume，**重建映像沒有用**

開發模式的前端由 `docker-compose.dev.yml` 覆寫，關鍵是它掛了一個 **named
volume 在 `/app/node_modules`**，而啟動指令是：

```
[ -x node_modules/.bin/next ] || npm ci; exec npm run dev ...
```

三件事疊起來造成一個很難看出的坑：

1. **volume 遮住映像**——`docker compose build` 把新套件裝進映像了，但執行時
   `/app/node_modules` 是 volume 的內容，映像那份根本沒被用到；
2. **`npm ci` 只在 `next` 不存在時才跑**——它存在，所以永遠不跑；
3. 連 bind mount 進去的主機 `node_modules`（可能已經有新套件）**也一併被遮住**。

於是「重建映像」這個直覺動作會**成功但無效**：建置沒有錯誤、容器正常起來、
頁面 200，只有那個新套件不存在。

實際踩過（2026-08-13，§2.4d 3D 機模）：前端把 `@deck.gl/mesh-layers` 寫進
`package.json`，後端重建映像後宣告完成——實況是容器內 `find -name mesh-layers`
零結果。**是另一個 session 的容器內抽查攔下來的。**

正確做法（dev 模式）：

```bash
# volume 是 root 所有、容器以 uid 1000 跑 → 要 -u 0 才裝得動
docker exec -u 0 uav-frontend sh -c 'cd /app && npm ci'
docker restart uav-frontend
# **驗容器內實況**，不是看 package.json：
docker exec uav-frontend node -e 'require.resolve("@deck.gl/mesh-layers")'
```

> 這是本節第三條「看起來成功、實際沒生效」的坑（另兩條見上下文）。三條的共同
> 形狀都是**同一件事**：改的東西與跑的東西之間隔了一層（reload／compose／
> volume），而那一層不會告訴你它擋住了什麼。**驗收一律以容器內實況為準。**

### 開發注意事項：改 `docker-compose.yml` 一定要 recreate 才生效

`--reload` 只重跑 **Python 程式碼**，它看不到 compose 的改動。**新增掛載
（volumes）或環境變數（environment）後，容器不重建就完全不會生效**，而且
失敗方式很隱晦——程式讀得到設定的「預設值」、掛載點則整個不存在。

實際踩過（2026-08-12，022 影像）：幫 backend 加了 `videorec:/rec` 掛載與
`VIDEO_*` 環境變數，只存檔沒重建 → 取影片端點一路回 410（`/rec` 在容器裡
不存在），而 `settings.video_retention_days` 讀到的是程式預設值 7，**剛好與
`.env` 相同所以看起來正常**，差點誤判成設定已生效。

```bash
docker compose up -d uav-backend      # 改 compose 後必須這一步（約 8s 中斷）
docker exec uav-backend ls -d /rec    # 驗掛載真的進去了
docker exec -w /srv uav-backend python3 -c "import os; print(os.environ.get('VIDEO_RETENTION_DAYS'))"
```

**驗收要看容器內的實際狀態，不要只看程式印出來的值**——預設值會假裝一切正常。

---

## 附錄 A：RB5 平台（備查，已非現役）

> **2026-09-02 從本文 §2 移到這裡。** RB5 不是現役平台（見文首的對照表），
> 但下面這些是**實測過的坑**——若日後回頭用 RB5，它們仍然成立，
> 重新查一次要花的時間比留著這段多得多。
>
> 相關的平台層問題記在
> [issues/016](../issues/016-rb5-platform-connectivity.md)：出廠廣播 `:14550`、
> 廣播不過 5G 網段、voxl-mavlink-server < 1.4.12 把全部機重設為 sysid 1。

### A.1 機上（RB5）一次性安裝

> SSH 進機上只做這一節，做完之後運行期間不再需要任何人為連線。

#### A.1.1 MAVLink 路由（流 ①）

> **為什麼 QGC 連上了還不夠**：需求不是「QGC 能連」，是「同一條 MAVLink
> 流要餵兩個消費者」——QGC（控制）與本系統 backend（14540，只讀記錄）。
>
> **快速路徑（不動機上）**：QGC 設定 → MAVLink → 啟用 Forwarding，
> 目標 `localhost:14540`（QGC 與本系統同一台地面站）。立即可用，
> 適合驗證期。代價：**記錄依賴 QGC 存活**——QGC 關閉或當掉，
> 本系統即斷流停錄。
>
> **正式部署（2026-08-10 定案：PX4 多實例，不裝 router）**：
> RB5 上 PX4 走 UDP，原生就能開多個 mavlink 實例——通道固定為兩條
> （資料/指令）時不需要額外路由軟體，符合原廠映像零改動原則。

PX4 的 MAVLink 輸出共三個消費者（前兩條對地、第三條機內），
在 PX4 啟動設定裡各開一個實例：

```
mavlink start -x -u <空埠1> -o 14540 -t <GS_IP>   -m onboard   # 資料（ingest，唯讀）
mavlink start -x -u <空埠2> -o 14541 -t <GS_IP>   -m normal    # 指令（ENABLE_COMMANDS 時）
mavlink start -x -u <空埠3> -o 14540 -t 127.0.0.1 -m onboard   # 機內：onboard node 綁座標用
```

驗證：地面站 `curl :38000/healthz` 的 `mavlink_connected` 轉 `true`（資料）、
`curl :38001/healthz` 的 `drones` 出現 sysid（指令）、
`scripts/check-onboard.py` 第 4 項樣本帶座標（機內）。

> mavlink-router 何時才值得裝：PX4 走**串列埠**（單程序獨佔，必須有人
> 分流）、消費者常變動、或需要動態 TCP 口讓 QGC 臨時接入。
> 目前部署皆不適用；日後需要時把上述實例目標改回本機、由 router 分流即可。

#### A.1.2 5G 量測 node（流 ②）

機上程式在主 repo 的 **`onboard/`**，並鏡像為獨立 repo 供機上 clone：
`git@github.com:jasper33977296/uav-onboard.git`（主 repo 的 onboard/ 為
單一事實來源，修正後同步過去）。安裝與設定見其 README。摘要：

- RF 指標：`AT+QENG="servingcell"`（SINR/RSRP/PCI），**第一步必為
  `--probe`**——印出 modem 原始回應貼回校準解析，勿假設一次就對
- 位置與時間：機上 PX4（MAVSDK 連 localhost）＋ GPS 時間，採樣當下綁定
- RTT／丟包：`ping <GS_IP>` 實測
- SQLite 緩衝先落盤再送、雙通道、斷點補傳（沿用已實測的原型邏輯）

設定項（環境變數）：

| 變數 | 範例 | 說明 |
|---|---|---|
| `GROUND_API` | `http://192.168.55.10:38000` | 地面站 API |
| `SAMPLE_HZ` | `1.0` | 採樣頻率（AT 查詢延遲 100–500ms，1Hz 是務實上限）|
| `BUFFER_PATH` | `/data/uav-link-buffer.sqlite3` | 持久緩衝（要落在斷電保留的分割區）|

systemd unit（`/etc/systemd/system/uav-link-node.service`）：

```ini
[Unit]
Description=UAV 5G link telemetry node
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/uav-system/onboard_node.py
Environment=GROUND_API=http://192.168.55.10:38000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now uav-link-node
```

#### A.1.3 上機首驗（第一個里程碑）

在已知干擾源附近做一次可控測試，確認 `AT+QENG` 的 **SINR 會隨干擾下降**
——Quectel 論壇有 RM500Q SINR 回報異常的前例，不要假設數值正確。
這一測同時驗證整條鏈路：modem → node → HTTP → DB → 前端。

#### A.1.4 即時影像（選配）

前端地圖點擊機體會開啟即時畫面 modal，來源是每台機的影像串流位址
（無人機頁 →「影像」設定，存在系統端，換瀏覽器不用重設）。

瀏覽器不支援 RTSP，機上相機串流需先轉成瀏覽器吃的格式。建議機上跑
[MediaMTX](https://github.com/bluenviron/mediamtx)（單一執行檔）把 RTSP 轉 WHEP（WebRTC，延遲最低，適合 FPV）：

```bash
# 機上：mediamtx.yml 指定 source: rtsp://<相機>，跑起來後
# 無人機頁「影像」填：http://<機IP>:8889/<路徑名>/whep
```

MJPEG（IP cam 常見）與 MP4/WebM 位址也可直接填，前端依 URL 自動選播放器。
注意：影像走 5G 會與量測流量搶頻寬——研究量測時建議降碼率或只在需要時開啟。

---


