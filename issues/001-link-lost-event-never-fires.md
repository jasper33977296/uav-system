# 001 · `link_lost` 事件永遠不會觸發

- 狀態：closed
- 嚴重度：high
- 位置：`backend/app/main.py:44-51`
- 建立：2026-08-03
- 關閉：2026-08-03

## 現象

無人機飛進干擾區、SINR 從正常一路掉到 -2 dB 以下時，只會收到一個
`link_degraded`（warning），不會收到 `link_lost`（critical）。

重現：照 README「測試場景：飛進干擾區」起飛往北飛進 `sim-jammer-A`
（severity 25 dB），觀察前端事件流或 `SELECT type FROM events ORDER BY time`。

**2026-08-03 首次實飛已確認。** SINR 逐秒穿越兩個門檻並在 -15 dB（模擬下限）
停留十餘秒，全程只有一筆 warning，沒有任何 critical：

```
 時間  | SINR  | zone        events 表：
-------+-------+------       12:23 | warning | link_degraded | {"sinr": 3.4, "in_zone": true}
 12:22 |   6.9 | t           （之後 SINR 一路到 -15，再無任何事件）
 12:23 |   3.4 | t   ← link_degraded 在這裡發出，degraded=True
 12:24 |  -0.9 | t
 12:25 |  -2.7 | t   ← 已跌破 sinr_lost_db(-2)，link_lost 未發出
 12:26 |  -6.7 | t
 12:27 | -11.1 | t
 12:28 | -15.0 | t   ← 觸底，持續 10+ 秒仍無 critical 事件
```

## 原因

`degraded` 是單一 bool，兩個門檻共用同一個旗標：

```python
if m["sinr"] < settings.sinr_lost_db and not degraded:      # < -2 dB
    degraded = True; ... link_lost
elif m["sinr"] < settings.sinr_degraded_db and not degraded: # < 5 dB
    degraded = True; ... link_degraded
```

SINR 連續下降時必定先穿過 5 dB → 觸發 `link_degraded` 並把 `degraded` 設為
True。之後即使跌破 -2 dB，第一個分支的 `not degraded` 已為 False，`link_lost`
不可能發出。只有「單一取樣直接從 ≥5 dB 跳到 < -2 dB」才會走到 lost 分支，
但 1 Hz 取樣下實際飛行幾乎不會發生。

## 影響

- `doc/architecture.md` 的事件門檻表宣告了四種鏈路事件，實際上 critical 級別的
  那一種不會產生 → 前端事件流、`events` 表都缺這筆資料。
- 直接打到 README 的開箱示範場景（宣稱可觀察 `link_lost`）。
- 之後做「干擾區內外鏈路品質比較」時，斷線次數這個指標會恆為 0。

## 修法建議

把 bool 換成三態，允許 degraded → lost 升級，並保留回復遲滯：

```python
link_state = "ok"  # ok / degraded / lost

if m["sinr"] < settings.sinr_lost_db and link_state != "lost":
    link_state = "lost"      # 發 link_lost (critical)
elif settings.sinr_lost_db <= m["sinr"] < settings.sinr_degraded_db and link_state == "ok":
    link_state = "degraded"  # 發 link_degraded (warning)
elif m["sinr"] >= settings.sinr_degraded_db + 3.0 and link_state != "ok":
    link_state = "ok"        # 發 link_recovered (info)
```

取捨：上式的 lost → ok 要等 SINR 回到 8 dB，中途不會補一個 degraded。這是刻意
的——避免飛在區緣時 lost/degraded 來回跳。若之後分析需要知道「脫離 lost 的時
間點」，再考慮加 lost → degraded 的降級事件。

## 解決方式

採「三態 + 每級各自遲滯」（討論時的方案 B），依「先記錄事實」的原則決定：
不選單一回復門檻（會漏記 lost→degraded 這個事實），也不選 time-to-trigger
（會讓事件時間戳晚於實際發生時刻，扭曲「位置 ↔ 鏈路劣化」的時間對應，
而那正是本研究的重點）。

實作為 `backend/app/main.py:_link_transition()`，門檻與遲滯全部進 `config.py`
（新增 `sinr_hysteresis_db`，原本 `+ 3.0` 是寫死在程式裡的）。
事件 detail 新增 `from` 欄位記錄來源狀態。

| 轉換 | 條件 | 事件 |
|---|---|---|
| → `lost` | SINR < -2 | `link_lost` (critical) |
| `ok` → `degraded` | SINR < 5 | `link_degraded` (warning) |
| `lost` → `degraded` | 1 ≤ SINR < 5 | `link_degraded` (warning) |
| → `ok` | SINR ≥ 8 | `link_recovered` (info) |

驗證（邏輯層 + 實飛）：

- 用本 issue 記錄的實測 SINR 序列（6.9 → 3.4 → -0.9 → -2.7 → … → -15.0）重跑，
  正確產生 `link_degraded` → `link_lost`。修正前只有前者。
- 回升序列 -15 → 12 產生 `link_degraded`（在 1 dB）→ `link_recovered`（在 8 dB），
  兩段轉換都留下紀錄。
- 在 5 dB 上下抖動 8 次只發 1 筆事件（無遲滯會發 8 筆）。
- 持續 -10 dB 20 秒只發 1 筆（不重複洗版）。
