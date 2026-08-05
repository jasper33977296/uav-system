# 部署文件：地面站安裝與完整設定

本文是**真機部署**（RB5 無人機 ＋ 地面站）的完整操作手冊，含所有設定項。
模擬環境（SITL 開發）見文末附錄。設計依據：
[onboard-telemetry.md](onboard-telemetry.md)（部署拓撲）、
[architecture.md](architecture.md)（系統定位）。

```
  無人機（RB5：PX4 + ROS 2 + RM500Q-GL modem）
  │ ① MAVLink/UDP  → 地面站:14550（QGC）＋ 地面站:14540（本系統，只讀）
  │ ② HTTP over 5G → 地面站:38000 /api/link-metrics/live 與 /batch
  ▼
  地面站（Ubuntu + Docker：本系統四容器 ＋ QGC 桌面程式）
```

三條流全部由**機上主動外連**——SSH 只在安裝日用一次，運行期間零人為連線。

---

## 0. 前提清單

| 項目 | 需求 |
|---|---|
| 地面站 | Ubuntu 22.04+，4 核 8GB RAM 起，磁碟 ≥ 100GB（原始資料 30 天約數 GB，餘量給匯出檔）|
| 網路 | 地面站與無人機的 5G 網路互通（私有 5G 直達；公網電信見 §3.3）|
| 地面站 IP | **必須固定**（DHCP 保留或靜態）——機上兩條流都寫死這個位址 |
| 無人機 | RB5 平台，PX4 與 ROS 2 可開機自啟，modem 已能上網 |
| 本文佔位符 | `<GS_IP>`＝地面站固定 IP（下文範例用 `192.168.55.10`）|

---

## 1. 地面站安裝

### 1.1 取得專案並初始化

```bash
git clone <repo> uav-system && cd uav-system
./scripts/setup.sh        # 裝 Docker、backend venv（腳本用）、frontend 套件、產生 .env
```

`setup.sh` 產生根目錄 `.env`（連接埠一律 30000 以上，避開系統服務）：

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
# 並刪除 COMPOSE_PROFILES=sim 這行——模擬器（開發鷹架）自此不存在於本環境
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

## 2. 機上（RB5）一次性安裝

> SSH 進機上只做這一節，做完之後運行期間不再需要任何人為連線。

### 2.1 MAVLink 路由（流 ①）

> **為什麼 QGC 連上了還不夠**：需求不是「QGC 能連」，是「同一條 MAVLink
> 流要餵兩個消費者」——QGC（控制）與本系統 backend（14540，只讀記錄）。
>
> **快速路徑（不動機上）**：QGC 設定 → MAVLink → 啟用 Forwarding，
> 目標 `localhost:14540`（QGC 與本系統同一台地面站）。立即可用，
> 適合驗證期。代價：**記錄依賴 QGC 存活**——QGC 關閉或當掉，
> 本系統即斷流停錄。
>
> **正式部署用本節作法**：機上路由雙端點，控制與記錄從源頭各自獨立、
> 故障互不牽連。QGC 連得上代表機上已有路由服務在送 MAVLink——
> 這裡只是**加一個端點**，不是裝新軟體。

RB5／ModalAI 平台內建 MAVLink 路由（依 SDK 版本為 `mavlink-router` 或
voxl-vision 設定檔，上機確認用哪套）。目標：把 PX4 的 MAVLink 同時送到
地面站兩個埠。`mavlink-router` 設定範例（`/etc/mavlink-router/main.conf`）：

```ini
[UdpEndpoint qgc]
Mode = Normal
Address = 192.168.55.10     # <GS_IP>
Port = 14550

[UdpEndpoint uav-system]
Mode = Normal
Address = 192.168.55.10     # <GS_IP>
Port = 14540
```

改完 `systemctl restart mavlink-router`。驗證：地面站 `curl .../healthz`
的 `mavlink_connected` 轉 `true`、QGC 自動出現載具。

### 2.2 5G 量測 node（流 ②）

機上 ROS node 目前**待實作**——`scripts/fake-onboard-node.py` 是完整原型
（SQLite 緩衝、先落盤再送、雙通道、斷線補傳、批次確認，全部實測過），
移植工作＝把「讀模擬器」換成讀 modem：

- RF 指標：`AT+QENG="servingcell"`（直接給 SINR，欄位對應見
  [onboard-telemetry.md](onboard-telemetry.md)）
- 位置與時間：機上 PX4（MAVSDK 連 localhost），採樣當下綁進同一筆
- RTT／丟包：`ping <GS_IP>` 實測

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

### 2.3 上機首驗（第一個里程碑）

在已知干擾源附近做一次可控測試，確認 `AT+QENG` 的 **SINR 會隨干擾下降**
——Quectel 論壇有 RM500Q SINR 回報異常的前例，不要假設數值正確。
這一測同時驗證整條鏈路：modem → node → HTTP → DB → 前端。

---

## 3. 網路設定

### 3.1 連接埠總表（地面站）

| 埠 | 協定 | 用途 | 誰連進來 |
|---|---|---|---|
| 33000 | TCP | 前端 | 操作員瀏覽器（區網）|
| 38000 | TCP | API/WS ＋機上 push | 瀏覽器、機上 node |
| 14550 | UDP | MAVLink → QGC | 機上 mavlink-router |
| 14540 | UDP | MAVLink → 本系統 | 機上 mavlink-router |
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
| 備份 | `docker exec uav-system-db-1 pg_dump -U uav uav > backup-$(date +%F).sql`（排程丟遠端）|
| 更新版本 | `git pull && docker compose up -d --build` |
| 看日誌 | `docker compose logs -f backend`（log 已設 50MB×3 上限，不會寫爆磁碟）|
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

- 測試飛行：`apps/backend/.venv/bin/python scripts/test-flight.py`（直線穿越）、
  `scripts/fly-mission.py [plan檔]`（上傳並執行任務）
- 群飛模擬：`POST /api/swarm/start?count=3[&mission_id=...]`
- 讓別台電腦的 QGC 連 SITL（臨時環境變數，不進 .env）：
  `SITL_QGC_HOST=<那台IP> docker compose up -d sitl`

注意：SITL 是共用的一台——測試腳本上傳任務會**覆蓋**QGC 上傳的
（MAVLink 任務是整包替換），且 SITL 容器重啟機上任務即歸零。
用「路徑管理 → 從機上讀回」確認機上現況。

前端容器跑的是 dev server（熱重載，開發即部署）；研究用地面站可接受。
要正式 build：`frontend` 容器 command 改
`sh -c "npm run build && npm run start -- -p 33000 -H 0.0.0.0"`。
