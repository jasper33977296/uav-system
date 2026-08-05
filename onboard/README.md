# UAV Onboard Link Node — 機上 5G 量測節點

跑在無人機 companion computer（RB5）上的獨立程式：每秒讀 modem 的
RF 指標（`AT+QENG="servingcell"`，含 SINR/RSRP/PCI）、從機上 PX4 取
當下位置與 GPS 時間、ping 地面站量 RTT——先寫入本地 SQLite 緩衝，
再以雙通道送往地面站（即時顯示 + 斷點補傳入庫）。

**鏈路劣化正是資料傳不回去的時刻**：緩衝與補傳是資料正確性的前提。
完整設計見主系統 repo 的 `doc/onboard-telemetry.md`。

本資料夾**自包含**（無主 repo 依賴），可直接作為獨立 repo 推到機上。

## 需求

- Python **3.6+**（RB5 原廠 Ubuntu 18.04 映像可直接跑，零環境改動）。
  全程同步式、無 asyncio；PX4 位置用 pymavlink 被動監聽（無 mavsdk_server
  副程序）。未安裝 pymavlink 時自動退化為無座標採樣
- modem AT 埠可讀（RM500Q 通常是 `/dev/ttyUSB2`；`ls /dev/ttyUSB*` 確認）
- 機上 PX4 的 MAVLink 可達（預設 `udpin://0.0.0.0:14540`，依機上路由設定調整）

## 一次性安裝（SSH 進機上，之後不再需要）

```bash
sudo mkdir -p /opt/uav-onboard && sudo chown $USER /opt/uav-onboard
cd /opt/uav-onboard
# 把本資料夾內容放進來（git clone 你的機上 repo，或 scp）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 第一步永遠是 --probe（上機首驗）

**不要假設 AT 解析一次就對**——韌體版本間欄位有差異：

```bash
AT_PORT=/dev/ttyUSB2 .venv/bin/python onboard_node.py --probe
```

把整段輸出貼回開發端校準 `parse_qeng()`。同時做設計文件要求的首驗：
在已知干擾源附近確認 **SINR 值會隨干擾下降**（Quectel 論壇有 RM500Q
SINR 回報異常的前例）。

## 正式運行

```bash
GROUND_API=http://<地面站IP>:38000 .venv/bin/python onboard_node.py
```

確認地面站前端「無人機訊號品質」卡開始有數值後，裝成開機自啟：
編輯 `uav-link-node.service` 的路徑與 `GROUND_API`，然後

```bash
sudo cp uav-link-node.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now uav-link-node
journalctl -u uav-link-node -f     # 看運行日誌
```

## 設定（環境變數）

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

## 驗證行為

- 斷線容忍：地面站斷線期間樣本累積於緩衝，恢復後自動補傳、冪等去重
  （地面站以 `(drone_id, time)` 去重，重送安全）
- PX4 連不上時照常採樣（樣本無座標，時序仍完整）；pymavlink 未安裝同理
- 網路逾時不拖慢取樣：送出在獨立執行緒，主迴圈只管「讀 modem＋落盤」
  的穩定節奏——斷線期間（最需要資料的時刻）採樣解析度不下降
- 已送達樣本保留 7 天後清除（地面站資料庫重建時可救援）
