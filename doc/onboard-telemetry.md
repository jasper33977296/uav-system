# 機上 5G 量測回傳設計

真機階段，5G 鏈路指標的採集點在**機上**（companion computer 上的 ROS node
讀 modem），本文定義它如何把資料送回地面站的本系統。

狀態：**設計定案，硬體已確認，尚未實作。**

## 硬體

| 項目 | 型號 | 對本設計的意義 |
|---|---|---|
| 機上運算 | Qualcomm Flight RB5 5G Platform（QRB5165，ModalAI 參考設計）| — |
| 5G modem | **Quectel RM500Q-GL**（Snapdragon X55）| 我們的量測儀器 |
| 作業系統 | Ubuntu 18.04（Yocto Dunfell）、kernel 4.19 | 版本偏舊，見下方注意事項 |
| 中介軟體 | **ROS 2**（平台預裝）| 機上 node 用 ROS 2 撰寫 |
| 飛控 | **PX4**（平台預裝，同一台機器）| 位置可在機上直接取得，不需跨裝置 |

三件事因此確定下來：

1. **ROS 版本是 ROS 2**，不需再議。
2. **PX4 與 modem 在同一台機器上**——這正好支撐了「位置在機上就綁進同一筆」
   的設計，取位置是本機呼叫，沒有跨裝置的時鐘與延遲問題。
3. **SINR 拿得到**（見下節），原本標記為唯一外部相依的風險解除。

> **注意事項：平台軟體堆疊偏舊。** Ubuntu 18.04 已於 2023 年結束標準支援，
> 搭配的 ROS 2 版本也屬早期。實作前需確認機上可用的 Python 版本與 ROS 2 發行版，
> 這會影響機上 node 能用哪些套件。這不影響本文的介面設計——
> HTTP POST 對執行環境幾乎沒有要求，這正是選它的好處之一。

相關：[architecture.md](architecture.md) 的系統定位、
[data-schema.md](data-schema.md) 的 `link_metrics` 欄位定義。

## 為什麼不能只是「即時送過來就好」

**量測通道就是被量測的通道。** 無人機的 5G 鏈路同時是研究對象與資料回傳路徑。

鏈路劣化時，回傳量測的管道也一起劣化。SINR 掉到 -15 dB、封包開始遺失——
那正是研究最想看的時刻，也正是資料傳不回來的時刻。

任何「即時送、送不到就算了」的設計，都會在最關鍵的地方留下空洞，
而且空洞的分布與研究結論直接相關：**你會系統性地少掉最差的那些樣本，
統計上是有偏的**。畫出來的「干擾區內 SINR 分布」會比真實情況樂觀。

因此**機上必須有持久化緩衝與斷點續傳**。這不是優化，是正確性的前提。
這個結論先於協定選擇——確立之後，MQTT 相對 HTTP 的主要優勢
（broker 管理的 QoS）就被機上緩衝抵銷掉大半，卻多一個服務要維運。

## 為什麼位置要在機上就綁上去

`link_metrics` 已經反正規化了 `lat / lon / alt_rel`。當初的理由是「查詢方便」，
在真機階段這會變成**必要**：

- 位置來自 MAVLink（地面站收到的時間）、RF 指標來自 modem（機上採樣的時間），
  兩邊時鐘有偏差就直接污染「位置 ↔ 鏈路劣化」的對應——那是本研究的核心。
- 補傳的資料抵達時飛機早已飛離，用「當下位置」配對會完全錯亂。

**機上 ROS node 在採樣的當下就從 PX4 取位置，寫進同一筆記錄。**
位置與 RF 來自同一個時鐘、同一個瞬間，補傳多久都不影響正確性。schema 不需修改。

### 時鐘來源

機上時間戳是唯一權威。建議**從 PX4 的 GPS 時間取得絕對時間**，
而非依賴 NTP——飛行中鏈路可能中斷，NTP 校時不可靠，而 GPS 時間機上本來就有。

## 架構

```
無人機（輕量 Linux + ROS）                        地面站（Ubuntu）
┌──────────────────────────────┐                ┌────────────────────────┐
│ ROS node（1 Hz）              │                │ FastAPI backend        │
│  ├ 讀 modem RF 指標           │                │                        │
│  ├ 從 PX4 取當下位置與時間     │   即時通道 ──→ │ POST .../live          │
│  ├ 量 RTT / 丟包              │   （最新一筆） │   → 只更新 live state   │
│  ├ 寫入本地持久佇列 (SQLite)  │                │                        │
│  └ 兩條通道送出               │   記錄通道 ──→ │ POST .../batch         │
└──────────────────────────────┘   （未確認的） │   → 唯一的入庫路徑      │
                                                 └────────────────────────┘
```

## 兩條通道

需求是「可以事後補，但要儘量看到即時資訊」。這兩件事該用**獨立的通道**滿足，
不能用一條兼顧。

| | 即時通道 | 記錄通道 |
|---|---|---|
| 送什麼 | 只送最新一筆 | 所有尚未被確認接收的樣本，批次送 |
| 失敗處理 | **放棄**，不重試 | 重試到成功 |
| 職責 | 前端顯示 | 完整性，**唯一寫入資料庫的路徑** |
| 延遲要求 | 越低越好 | 可以慢 |

**為什麼分開**：鏈路很差時，一個小封包還有機會擠過去，一個大批次則否。
若只有一條通道，為了完整性而重試就會卡住即時性——操作員看到十秒前的舊資料，
那比沒有資料更糟，因為他會以為那是現況。

**為什麼即時通道不重試**：下一秒的新樣本本來就會取代它，重試舊資料沒有意義，
只會佔用本來就不夠的頻寬。

**為什麼即時通道不入庫**：避免與記錄通道重複寫入。live 只負責顯示，
記錄通道負責留存，兩者職責不重疊，不需要去重邏輯。
鏈路正常時記錄通道也是秒級送達，資料庫並不會落後多少。

## API 規格

### `POST /api/link-metrics/live`

單一樣本，fire-and-forget。只更新 live state 供前端顯示，**不寫資料庫**。

Body 即單一樣本物件（格式見下）。回應 `204 No Content`。

### `POST /api/link-metrics/batch`

樣本陣列。**唯一的入庫路徑。**

```json
{ "drone_id": "...", "samples": [ {...}, {...} ] }
```

回應告知哪些已確實接收，機上據此標記可刪除：

```json
{ "accepted_seq": [12340, 12341, 12342], "rejected": [] }
```

**必須是冪等的。** 重試代表 at-least-once 投遞，同一批可能送達兩次。
以 `(drone_id, time)` 為天然鍵搭配 `ON CONFLICT DO NOTHING`。

> 需要新增 schema：`link_metrics` 目前沒有唯一約束。
> hypertable 的唯一索引必須包含分區欄位，`(drone_id, time)` 已滿足。

### 樣本格式

```json
{
  "seq": 12345,
  "time": "2026-08-03T14:22:31.502Z",
  "lat": 47.3995, "lon": 8.5456, "alt_rel": 50.1,
  "rsrp": -91.2, "rsrq": -11.0, "sinr": -3.5, "cqi": 4,
  "pci": 101, "cell_id": 12345678, "band": "n78", "nr_mode": "SA",
  "rtt_ms": 95.2, "jitter_ms": 12.0, "packet_loss_pct": 3.0,
  "throughput_up_kbps": null, "throughput_down_kbps": null,
  "raw": { "at_response": "..." }
}
```

`seq` 是機上單調遞增序號，只用於批次確認，不入庫。
`source` 由地面站填 `'modem'`。`raw` 存 modem 原始回應，便於事後追查。

## `session_id` 用時間戳反查

**這是 [issues/004](../issues/004-writes-while-disarmed.md) 的 armed gate
在 push 模式下的正確版本。**

現行邏輯是「backend 看到 armed 才寫」。改成 push 之後，補傳的樣本抵達時
飛機可能已經上鎖，用當下 armed 判斷會把它整批丟掉。

正確作法是**用樣本自帶的時間戳反查涵蓋它的架次**：

```sql
SELECT id FROM flight_sessions
WHERE drone_id = $1 AND started_at <= $2
  AND (ended_at IS NULL OR ended_at >= $2)
```

落在某架次區間內就歸給它；落在區間外就丟棄——語意等同原本的 armed gate，
但對補傳資料成立。

順帶好處：**機上不需要知道 armed 狀態**，一律採樣一律送，
判斷邏輯集中在地面站一處。

> 需要新增索引：`flight_sessions (drone_id, started_at)`。

## 機上緩衝

建議 **SQLite**，一張表加一個 `sent` 旗標。相較 append-only 檔案的好處是
原子更新、可直接查詢未送出的樣本、不需自行維護游標檔。

- 採樣後**先落盤再嘗試送出**，順序不可顛倒——先送後存的話，
  程式在送出後、寫入前被中斷就會遺失。
- 已確認送達的樣本保留一段時間再刪（例如 7 天），
  留給「地面站資料庫需要重建」這類情況。
- 磁碟寫爆的防護：1 Hz × 每筆數百 bytes ≈ 每天數十 MB，
  但仍應設上限——參考 [issues/009](../issues/009-sitl-log-fills-disk.md)
  的教訓，沒有上限的寫入遲早會出事。

## 前端要能區分即時與失聯

即時通道會靜默失敗，前端若只顯示「最後收到的值」，操作員無法判斷那是不是現況。

- live state 需附 `link_last_seen` 時間戳。
- 超過門檻（例如 3 秒）未更新，明確顯示「已失聯 N 秒」，
  而不是靜靜停在最後一筆數值。
- 這與 `link_lost` 事件是不同的東西：`link_lost` 是「量到 SINR 很差」，
  失聯是「量測本身送不回來」。兩者都要能看見，語意不可混用。

## 對現有程式碼的影響

| 位置 | 改動 |
|---|---|
| `main.py:_link_and_db_loop` | 只在 `LINK_SOURCE=simulated` 時運作；modem 模式下入庫改由 API endpoint 負責 |
| `api.py` | 新增上述兩個 endpoint |
| `db.py` | `insert_link` 支援指定時間戳與 `ON CONFLICT DO NOTHING`；新增架次反查 |
| `01_schema.sql` | `link_metrics` 加唯一索引 `(drone_id, time)`；`flight_sessions` 加索引 |
| `state.py` | 加 `link_last_seen` |
| 前端 | 失聯狀態顯示 |

`link_metrics` 的 `source`（`simulated`/`modem`）與 `raw` JSONB 欄位
當初就是為此保留的，不需新增。

## 從 modem 讀什麼、怎麼讀

**modem 是本研究唯一的量測儀器。** RSRP、RSRQ、SINR、PCI 不是我們算出來的，
是向 modem 查詢得來。RM500Q-GL 有兩條存取路徑，取捨不同。

### 路徑一：AT 指令（建議主用）

`AT+QENG="servingcell"` 在 NR5G-SA 模式的回應格式：

```
+QENG: "servingcell",<state>,"NR5G-SA",<duplex_mode>,<MCC>,<MNC>,
       <cellID>,<PCID>,<TAC>,<ARFCN>,<band>,<NR_DL_bandwidth>,
       <RSRP>,<RSRQ>,<SINR>,<scs>,<srxlev>
```

**這條路徑直接給 SINR**（5G NR 模式下範圍 -20 ~ 30 dB），
而且欄位與 `link_metrics` 幾乎一對一對應：

| AT 回應欄位 | `link_metrics` 欄位 |
|---|---|
| `<SINR>` | `sinr` ← **干擾研究主指標** |
| `<RSRP>` / `<RSRQ>` | `rsrp` / `rsrq` |
| `<PCID>` | `pci` |
| `<cellID>` | `cell_id` ← 全域識別碼，正是 [issues/003](../issues/003-cell-id-not-persisted.md) 定義的語意 |
| `<band>` | `band` |
| `"NR5G-SA"` | `nr_mode` |
| `<duplex_mode>` `<ARFCN>` `<scs>` `<srxlev>` `<TAC>` | 存進 `raw` JSONB |

schema 不需要為真機階段做任何修改——當初的欄位設計與 modem 實際回報的內容吻合。

### 路徑二：QMI（libqmi / `qmicli`）

RM500Q-GL 是 Qualcomm X55 平台，支援 QMI。
`qmicli --nas-get-signal-info` 回報 5G NR 的 RSRP、RSRQ 與 **SNR**。

**注意這裡是 SNR 不是 SINR。** 兩者名稱相近但定義不同——SINR 把干擾算進分母，
SNR 只算雜訊。實務上接收端難以區分兩者（量到的雜訊底噪本就含其他 cell 的干擾），
但既然 AT 路徑明確提供 SINR，**主指標應以 AT 路徑為準**，
避免在論文裡把兩個定義不同的量混用。

QMI 適合當輔助：介面穩定、解析比 AT 字串容易，可用來取連線狀態等非主指標資訊。

### 上機後要做的第一個驗證

Quectel 論壇上有 RM500Q SINR 回報異常的討論串，因此**不要假設數值一定正確**。
建議的驗證方式：在已知干擾源附近做一次可控測試，確認 `<SINR>` 會隨干擾出現而下降。

這個測試同時驗證了整條量測鏈路（modem → ROS node → HTTP → 資料庫），
是上機後的第一個里程碑。若 SINR 對干擾無反應，主指標就要改用其他方式取得——
那是設計層級的變更，越早發現越好。

## 量測本身會干擾量測

- **吞吐量的主動測試（iperf 之類）會消耗正在被量測的頻寬**，
  且會與飛控的 MAVLink 流量競爭。建議優先用被動計數器；
  若必須主動測，頻率要低並在資料中標記，分析時排除那些時段。
- RTT / 丟包用 ping 對頻寬影響極小，1 Hz 可接受。
- modem 的 AT 查詢本身有延遲（約 100–500 ms），1 Hz 是務實的上限，
  更高的採樣率不一定拿得到。

## 未定事項

原本列為未定的三項（ROS 版本、運算平台、modem 型號）都已因硬體確認而解決，
**沒有外部相依擋住實作**。剩下的是實作階段自然會遇到的事：

| 項目 | 何時處理 |
|---|---|
| 機上可用的 Python 版本與 ROS 2 發行版（平台為 Ubuntu 18.04）| 開始寫機上 node 前確認 |
| `<SINR>` 是否確實隨干擾變化 | 上機後第一個驗證，見上節 |
| 吞吐量要用被動計數器還是低頻主動測試 | 機上 node 實作時決定 |

地面站側（兩個 API endpoint、schema 索引、前端失聯顯示）不依賴以上任何一項，
**可以現在就實作**，並用一支模擬機上 node 的腳本打進去驗證完整流程。

## 參考資料

- [Qualcomm Flight RB5 5G Platform（ModalAI）](https://www.modalai.com/pages/qualcomm-flight-rb5-5g-platform)
- [Qualcomm Dragonwing QRB5165 產品頁](https://www.qualcomm.com/internet-of-things/products/q5-series/qrb5165)
- [Quectel RG50xQ & RM5xxQ 系列 AT 指令手冊](https://quectel.com/content/uploads/2024/05/Quectel_RG50xQRM5xxQ_Series_AT_Commands_Manual_V1.2.pdf)
- [libqmi NAS Get Signal Info 參考文件](https://www.freedesktop.org/software/libqmi/libqmi-glib/latest/libqmi-glib-NAS-Get-Signal-Info-response.html)
