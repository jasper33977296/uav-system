# 070 · 代理每次啟動前要先做時間同步

- 狀態：open（**裁定 2026-09-22 使用者**，待實作）
- 嚴重度：medium（時間錯了，機上記下的每一筆都對不齊：補傳樣本、錄製、notice）
- 位置：`uav-agent/systemd/uav-agent.service`（啟動前置）、`uav-agent/clock.py`
- 建立：2026-09-22

## 現象

2026-09-22 Pi 重開後，代理跑了約 8 分鐘，**整段牆鐘比地面站慢約 21 分鐘**：
057 的 notice 帶 `at_unsynced: true`、晚到 1271 秒。`systemd-timesyncd` 設定是對的
（`NTP=10.141.2.21`、無 fallback），但**第一次對時成功是在開機後約 18 分鐘**
（地面站時間 16:09），那時代理已經被停掉了。

代理有誠實標出「時鐘沒對過」（`clock.synced()`、notice 的 `at_unsynced`），
但標出來不等於資料可用——那 8 分鐘裡機上記下的時間全部要事後猜偏移。

## 需求（2026-09-22 使用者）

**agent 每次啟動前的前置作業要包含時間同步。**

## 要決定／實作時注意

* 前置同步**擋多久**。一直對不上時（5G 還沒起來、地面站 NTP 沒開），代理不起來＝
  沒有橋，比時間錯更糟（059 同一條原則：被干擾的橋仍然比沒有橋好）。建議：主動觸發
  一次同步並等一個上限（例如 60 秒），逾時照樣啟動、**大聲說**並把 `clock_synced`
  維持在狀態裡。
* 觸發方式：`ExecStartPre` 重啟 `systemd-timesyncd` 並等 `/run/systemd/timesync/synchronized`，
  或 `After=time-sync.target`＋`systemd-time-wait-sync`（後者沒有上限，要加 `TimeoutStartSec`）。
* 對上之後**才**開始記時間戳的東西，對不上期間的照 `clock.py` 既有規則（記單調、送出時換算）。
* 驗收：Pi 重開 → 代理日誌第一行之前有一行「已對時／對時逾時」，notice 不再帶 `at_unsynced`。

## 相關

* 047 項次 6（Pi 的時間來源）、`clock.py`、057（`at_unsynced` 就是這次抓到它的）

## 解決方式

（closed 時補）
