# 070 · 代理每次啟動前要先做時間同步

- 狀態：in-progress（**已實作並上機驗證 2026-09-22**，uav-agent `8b58a2c`；剩「開機時 5G 比 60 秒晚起來」那一格）
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

uav-agent `systemd/wait-time-sync.sh`，由 unit 的 `ExecStartPre=+` 以 root 執行（代理本身仍是 pi）：

* 已對過（`/run/systemd/timesync/synchronized` 存在）→ 立刻放行
* 沒對過 → `systemctl --no-block restart systemd-timesyncd` 催它馬上試，每 20 秒再催，最多 60 秒
* **逾時照樣放行**（exit 0）；代理本來就會在狀態列與 notice 標 `clock_synced`／`at_unsynced`
* `TimeoutStartSec=120`

### 驗證

| | 結果 |
|---|---|
| 本機（`SYNC_FLAG` 指向暫存檔）| ✅ 已對過立刻過；等待中對上 → 成功；逾時 → exit 0 並說明 |
| **真機**（09-22 17:08，Pi 重開後沒對時、Pi 顯示 16:56、地面站 17:08）| ✅ 經 deploy.sh 重啟時：「開機後還沒對過——催 systemd-timesyncd」→ **約 1 秒就對上**，`Offset: +12min 13s`，接著代理以正確時間啟動 |

**這也說明了當初為什麼 18 分鐘才對上**：不是地面站的 NTP 不通，是 timesyncd 自己
退避得太長——催一下 1 秒就好。

### 還沒驗／還沒做

- [ ] **真的開機**那一次：開機時 5G 可能比代理晚起來，60 秒內等不到就會帶著錯的時間啟動，
      之後又要等 timesyncd 自己的退避。09-22 那兩次重開都在部署這個之前，還沒有看到
      開機當下的行為。若真的常發生，補一個「對時還沒成、而地面站已經連得到」時再催一次的機制
      （代理是 pi，不能自己重啟 timesyncd——要一個 root 的 timer 或 path unit）
