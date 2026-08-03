# 002 · 服務 cell 無遲滯，handover 事件會抖動狂噴

- 狀態：closed
- 嚴重度：low（原評 medium，確認模擬器是開發鷹架後降級）
- 位置：`backend/app/link_sim.py:36-43`、`backend/app/main.py:38-42`
- 建立：2026-08-03
- 關閉：2026-08-03

## 現象

飛在兩個 gNB 距離相近的位置時，`handover` 事件可能每秒都發一次，前端事件流被
洗版，`events` 表也塞滿無意義的 PCI 來回切換紀錄。

seed 的兩個 gNB（`sim-gnb-1` @47.3970,8.5450 與 `sim-gnb-2` @47.4010,8.5490）
中間正好是干擾區與示範航線經過的區域，很容易踩到。

**2026-08-03 首次實飛已確認。** 單趟飛行就抓到一組來回換手，間隔 2 秒：

```
 12:42 | info | handover | {"to_pci": 205, "from_pci": 101}
 12:44 | info | handover | {"to_pci": 101, "from_pci": 205}
```

這趟只飛了 2 分 47 秒且大部分時間在干擾區內定點，實際任務飛行時數量會更多。

## 原因

`SimulatedLinkSource.sample()` 每次取樣獨立重算，選 RSRP 最大者為服務 cell：

```python
rsrp = -55.0 - 25.0 * log10(...) + (tx_power - 40.0) + random.gauss(0, 1.5)
if rsrp > best_rsrp: best, best_rsrp = c, rsrp
```

每個 cell 各自加了 σ=1.5 dB 的高斯雜訊，兩台差距在 ~3 dB 以內時，勝負基本由雜訊
決定。選擇過程沒有任何遲滯或時間遲延，`main.py` 又是「PCI 一變就發事件」。

真實網路的 handover 有 A3 offset 與 time-to-trigger，不會這樣抖。

## 影響

- 事件流可用性下降，真正重要的 `link_degraded` / `link_lost` 被淹沒。
- `handover` 次數這個指標失去意義（之後若要分析「干擾區是否誘發更多換手」會被雜訊蓋掉）。

## 修法建議

在模擬器內維持目前服務 cell，套 3GPP A3 式的門檻：候選 cell 必須比目前服務 cell
強超過 `handover_margin_db`（預設 3 dB），且連續成立 N 次取樣（time-to-trigger，
1 Hz 下取 2–3 次）才換手。

```python
self.serving = None          # 目前服務 cell
self._candidate_ticks = 0    # 候選連續勝出次數
```

另一個（可疊加的）作法是對每個 cell 的 RSRP 做 EMA 平滑再比較，讓雜訊不直接參與
選擇；門檻與 TTT 參數放進 `config.py` 方便實驗時調整。

## 範圍認定：換手不在研究範圍內

一台無人機等價於**一台 UE**，機上跑 ROS。換手由 modem 與網路側自行處理，
應用層（ROS、我們的資料蒐集）既不參與決策也不控制它。因此換手不是本研究要
探討的現象，只是 modem 回報的一項狀態。

這決定了本 issue 的處理上限：讓假象不干擾開發即可，不在建模上投資。
`pci` 仍然記錄在 `link_metrics`（那是 modem 回報的事實），
但沒有必要為它建立精緻的事件邏輯。

## 為什麼不做 3GPP 等級的建模

討論時確認了本系統的職責是**控制資料蒐集與記錄事實，不做通道模擬**。
`link_sim.py` 只是開發鷹架——SITL 沒有 5G modem，需要合理輸入才能開發前端與
驗證資料流。真機階段的 `ModemLinkSource` 會直接從 modem 讀 PCI，
換手是實際觀測到的事實，本模組再怎麼精修都不會更接近真實網路行為。

因此判準只有一個：**這個假象會不會妨礙開發**。做到「不礙事」即可，
不做 A3 offset + time-to-trigger + L3 濾波那一整套。

## 解決方式（二）：移除 handover 事件

依上述範圍認定，`handover` 事件類型整個移除——`main.py` 不再比對 PCI 變化發事件，
`doc/architecture.md`、`doc/data-schema.md`、`db/init/01_schema.sql` 的事件類型
清單同步更新。

`pci` 仍記錄在 `link_metrics`，需要時可從 1Hz 時序資料看出服務 cell 的變化。
這符合「事件是衍生資料、時序表才是原始事實」的原則——換手既然不是研究關注點，
就不需要為它維護即時事件邏輯。

## 解決方式（一）：換手邊際

`SimulatedLinkSource` 記住現任服務 cell，候選要強過 `handover_margin_db` 才換手。
約 5 行，不引入額外狀態機。

參數值是實測選出來的，不是拍腦袋：

| 設定 | 定點停留 300 秒的換手次數 | 飛越兩 gNB |
|---|---|---|
| 現況（margin=0）| 136–161 次 | — |
| margin=3 dB | 14–20 次（**仍不夠**，約每 15 秒抖一次）| 正常 |
| **margin=6 dB** | **0–1 次** | 正常換手 1 次 |
| EMA(α=0.3) + margin=3 | 0 次 | 正常換手 1 次 |

最後選 **margin=6**：與 EMA 方案效果相同，但只是一個參數值、不需要在鷹架程式碼
裡多維護一份濾波狀態。EMA 雖然更貼近真實 UE 的 L3 濾波行為，但那是在為一段
會被丟掉的程式碼投資擬真度。

實測位置就是本 issue 記錄的抖動發生點（47.3995, 8.5456，兩個 gNB 的 RSRP 只差
0.88 dB，而雜訊差值 σ=2.12 dB → 較弱者有 34% 機率靠雜訊勝出）。
