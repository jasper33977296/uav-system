# UAV Onboard Link Node — 機上 5G 量測節點

跑在無人機 companion computer（RB5）上的獨立程式：每秒讀 modem 的
RF 指標（`AT+QENG="servingcell"`，含 SINR/RSRP/PCI）、從機上 PX4 取
當下位置與 GPS 時間、ping 地面站量 RTT——先寫入本地 SQLite 緩衝，
再以雙通道送往地面站（即時顯示 + 斷點補傳入庫）。

**鏈路劣化正是資料傳不回去的時刻**：緩衝與補傳是資料正確性的前提。
完整設計見主系統 repo 的 `doc/onboard-telemetry.md`。

本資料夾**自包含**（無主 repo 依賴），可直接作為獨立 repo 推到機上。

## 需求

- Python **3.6+**、**零第三方依賴（純標準庫）**——不需要 venv、不需要 pip，
  RB5 原廠 Ubuntu 18.04 映像 clone 完直接跑。serial 用 termios 直開 tty；
  PX4 位置用內建迷你 MAVLink 解析器被動監聽（只認三種訊息，X.25 CRC 驗證）
- modem AT 埠可讀（Quectel RM50xQ 通常是 `/dev/ttyUSB2`；`ls /dev/ttyUSB*` 確認）
- 機上 PX4 有一條 mavlink 實例送到**本機** 14540（見下節）

## 機上 MAVLink 前置（PX4 原生多實例，無 router／無 QGC）

PX4 的 MAVLink 輸出共三個消費者，全部用 PX4 原生實例
（voxl 平台在 `/usr/bin/voxl-px4-start` 加行；`-u` 各用不同空埠）：

```
mavlink start -x -u 14560 -o 14540 -t <地面站IP> -m onboard -r 50000      # 資料 → 地面站
mavlink start -x -u 14561 -o 14541 -t <地面站IP> -m minimal -r 20000     # 指令 → 地面站
mavlink start -x -u 14562 -o 14540 -t 127.0.0.1 -m onboard -n lo -r 100000  # 機內 → 本 node
```

注意：`-m normal` 不是合法模式名（normal 是預設，不可指定——會靜默
啟動失敗）；PX4 實例有上限（通常 4 條），超額時把沒觀眾的既有實例
（如餵 voxl-mavlink-server 的）註解掉。改完 `systemctl restart voxl-px4`，
用 `ss -ulnp | grep 1456` 確認每條都有 bind。

## 一次性安裝（SSH 進機上，之後不再需要）

clone 在哪個路徑都行，`install.sh` 會依實際位置生成 systemd 服務：

```bash
git clone git@github.com:jasper33977296/uav-onboard.git && cd uav-onboard
cp .env.example .env      # 編輯 GROUND_API（零依賴，不需要 venv/pip）
sudo ./install.sh         # selftest → 生成服務檔 → 開機自啟 → 立即啟動
```

**手動 `python3 onboard_node.py` 只當測試用**——機身重啟手動程序就沒了
（實際發生過：量測靜默中斷一小時）。部署一律走 systemd。

## 第一步永遠是 --probe（上機首驗）

**不要假設 AT 解析一次就對**——韌體版本間欄位有差異：

```bash
python3 onboard_node.py --probe        # AT_PORT 非預設時在 .env 設定
```

把整段輸出貼回開發端校準 `parse_qeng()`。
（2026-08-10 已對實機 RM502Q-AE R11A04 首驗：SA 格式逐欄命中、與
AT+QNWINFO 交叉驗證一致，該樣本已固化進 `--selftest`。）
另一項首驗仍待做：
在已知干擾源附近確認 **SINR 值會隨干擾下降**（Quectel 論壇有 RM500Q
SINR 回報異常的前例）。

## 正式運行＝systemd（`install.sh` 已裝好）

```bash
journalctl -u uav-link-node -f        # 看運行日誌（每秒取樣、HTTP 錯誤帶原因）
sudo systemctl restart uav-link-node  # 改 .env 後重啟生效
```

驗收在**地面站**做：`python3 scripts/check-onboard.py`（六項完整性檢查，
每個失敗附排查指引）；前端「無人機訊號品質」卡應有數值。

## 設定（`.env`，真環境變數優先）

| 變數 | 預設 | 說明 |
|---|---|---|
| `GROUND_API` | （必填）| 地面站 API，如 `http://192.168.55.10:38000` |
| `AT_PORT` | `/dev/ttyUSB2` | modem AT 埠 |
| `AT_BAUD` | `115200` | |
| `SAMPLE_HZ` | `1.0` | AT 查詢延遲 100–500ms，1Hz 是務實上限 |
| `BUFFER_PATH` | `/var/lib/uav-link/buffer.sqlite3` | 持久緩衝，放斷電保留的分割區 |
| `PX4_URL` | `udpin://0.0.0.0:14540` | 機上 PX4 MAVLink |
| `PING_HOST` | 取自 GROUND_API | RTT 目標 |
| `DRONE_ID` | （不填）| 不填＝記在地面站的「主機」名下（單機正確預設）|

## 常見拒絕原因

| 機上日誌 | 原因與解法 |
|---|---|
| `伺服器拒絕 HTTP 409：link_source=simulated...` | 地面站 `.env` 改 `LINK_SOURCE=modem` 後 `docker compose up -d` |
| `伺服器拒絕 HTTP 422：時間戳必須含時區...` | 不應發生（本程式一律送含時區時間），回報開發端 |
| `送出失敗（URLError: ...）` | 連不到地面站：確認 GROUND_API 的 IP/埠與 5G 路由 |

## 驗證行為

- 斷線容忍：地面站斷線期間樣本累積於緩衝，恢復後自動補傳、冪等去重
  （地面站以 `(drone_id, time)` 去重，重送安全）
- PX4 連不上時照常採樣（樣本無座標，時序仍完整）
- 網路逾時不拖慢取樣：送出在獨立執行緒，主迴圈只管「讀 modem＋落盤」
  的穩定節奏——斷線期間（最需要資料的時刻）採樣解析度不下降
- 已送達樣本保留 7 天後清除（地面站資料庫重建時可救援）
