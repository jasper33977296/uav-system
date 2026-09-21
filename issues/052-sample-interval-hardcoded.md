# 052 · 對外訊號的 `sample_interval_s` 是寫死的常數

- 狀態：closed
- 嚴重度：medium
- 位置：`apps/backend/app/ext_history.py:192`
- 建立：2026-09-21

## 現象

`GET /api/v1/ext/missions/{id}/signal` 的回應帶一個 `method` 區塊：

```json
"method": {"max_offset_m": 60.0, "sample_interval_s": 1}
```

`sample_interval_s` 是**字面常數 `1`**，不是從那一趟的資料算出來的：

```python
"method": {"max_offset_m": max_offset_m, "sample_interval_s": 1},
```

它放在叫 `method`（方法）的區塊裡，讀起來就是「這一趟的實際取樣率」。

## 原因

取樣率其實由**另一台機器上的旗標**決定，而 backend 手上沒有任何管道知道它設成多少：

| 路 | 誰決定 | 預設 |
|---|---|---|
| 真機 | 機上代理的 `--modem-interval` | 1.0 s |
| 模擬 | backend 的 `settings.db_write_hz`（`main.py:146`） | 1.0 Hz |

兩個都可調。`agent_link.py` 沒有任何欄位載運機上的取樣率或 `hz`，
所以這個 `1` 是在**替另一台機器的預設值背書**。

## 影響

**同一類錯誤在這個專案已經發生過一次**，而且有實測紀錄
（`doc/onboard-telemetry.md`〈取樣率：設定多少就要真的是多少〉，2026-09-07）：

> `--modem-interval 1.0` 實際量到 **2.61 秒一筆**（0.38 Hz），而 §3.6 寫的是 1 Hz。

當時兩個獨立的錯疊在一起（睡錯地方＋等太久），而那份文件自己寫下了結論：

> **唯一看得出來的地方是 `link_metrics` 的時間戳差，那張表當時沒有真資料。**
> **能揭露問題的資料，正好就是沒被記錄的那份。**

那個 bug 修好了，但**結構沒變**：現在這支對外端點又在回報一個沒有量過的數字。
外部拿它做時間軸內插或密度補正時，錯的量不會有任何線索。

另外即使旗標真的是 1.0 s，樣本也不必然等距——機上是 `/batch` 每 10 秒一批、
允許補傳，而且「數據機讀數靜止實測約 1–4 秒才變一次」。

## 修法建議

**量出來，而且分趟給。** 取樣率是每一趟自己的性質，不是全域常數：

* 在每一個 `session` 底下回報從 `samples` 時間戳算出的間隔中位數
  （例如 `sample_interval_s: 1.02`），樣本少於兩筆就給 `null`。
* `method` 區塊只留真正是「方法」的東西（`max_offset_m`，見 054）。
* 文件寫明它是**量到的**，不是設定值。

取捨：查詢時多算一次中位數（樣本本來就都在手上，成本可以忽略）。
不要的做法：讓機上把旗標值回報上來——那還是「設定值」，而這個 issue 就是在講
設定值與實際值可以差 30 倍。

## 解決方式

`method.sample_interval_s` 拿掉，改成每一趟底下的 `sample_interval_s`＝該趟相鄰樣本
時間差的**中位數**（`ext_history._interval_s()`），不足兩筆給 `null`。
`method` 只留真的是方法的東西（`max_offset_m`、`sample_gap_factor`）。
文件寫明它是量到的，不是設定值。

2026-09-21，commit `0d404fb`。
