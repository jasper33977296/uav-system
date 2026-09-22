# 059 · uav-agent 必須永遠擁有 UART 的最高優先權

- 狀態：in-progress（A 完成並上機 2026-09-22；**B 程式完成、未部署，暫停中**——見〈解決方式〉的 B 節）
- 嚴重度：**high**（飛安：橋的一端被別人靜默地搶走，而且很難看出來）
- 位置：`uav-agent/agent.py` 的 `_open_serial_once`、`onboard/uav-link-node.service`
  （unit 設定）、`/opt/uav-agent/systemd/`
- 建立：2026-09-21

## 現象

2026-09-21 15:12，無人機「校正一直跑不完」。真因是**兩個行程同時開著同一個
UART**：

```
2636  /home/pi/hct/qoe/.venv/bin/python get-gps.py       15:12:43 啟動
 └2637  mavsdk_server --sysid 245 --compid 190 serial:///dev/ttyAMA0:57600
2685  /opt/uav-agent/agent.py                            15:12:50 啟動
```

兩個行程輪流從同一個核心緩衝 `read()`，各自拿到**隨機片段**，
MAVLink 框架對兩邊都是破的。後果：

* 校正需要可靠的雙向往返（`COMMAND_ACK`、`MAG_CAL_PROGRESS`），封包被對方吃掉
  就永遠跑不完——**而且不會報錯，只是停在那裡**
* 代理那一側 `msgs_from_fc = 0`、`fc_heartbeat_age_s = None`
* 我們自己的 `set-fc-params.py` 也失敗：
  `device reports readiness to read but returned no data (device disconnected or
  multiple access on port?)`

停掉外部行程之後飛控立刻回來（`msgs_from_fc` 從 0 開始增加，心跳 0.7 s）。

## 原因

`/dev/ttyAMA0` 預設允許多個行程同時 `open()`，而我們從來沒有主張過獨佔。
050 需求 1 當時列了 `TIOCEXCL` 但**沒有做**——理由是「擋不住 root，而 09-16
那次是 `sudo`」。這次證明那個理由取捨錯了：**日常會踩到的是非 root 的情況**
（`mavsdk_server` 是用 `pi` 身分跑的），而 `TIOCEXCL` 正好擋得住。

## 影響

* 這是**靜默失效**：兩邊都還「活著」，沒有例外、沒有錯誤碼，只有資料莫名其妙
  不完整。今天代理有說話（050 的看門狗報了「飛控無心跳 38 秒」並重開兩次），
  但重開救不回來——問題不在埠壞了，在有人搶。
* 飛行中發生等同於失去飛控鏈路。
* 排查成本極高：要 `fuser -v /dev/ttyAMA0` 才看得見，而沒有人會第一個想到。

## 需求（2026-09-21 使用者裁定）

**代理運行期間，永遠擁有 UART 的最高優先權。**

## 修法建議

### A. `TIOCEXCL`：開埠時就主張獨佔（先做）

pyserial 的 `Serial(..., exclusive=True)`。之後任何非 `CAP_SYS_ADMIN` 的行程
`open()` 會拿到 `EBUSY`——`mavsdk_server` 這類就再也開不起來了。

> **更正（2026-09-22 實作時）：上面這句是錯的。** pyserial 的 `exclusive=True`
> 做的是 `flock(LOCK_EX|LOCK_NB)`（`serialposix.py`），那是建議鎖，只擋得住同樣
> 去 flock 的程式——`mavsdk_server`／`stty` 都不 flock，照樣開得起來。實作改成
> 直接 `ioctl(TIOCEXCL)`。原句保留，因為照它做會做出一個看起來有、實際沒有的防線。

**它擋不住 root**（root 可以無視 TIOCEXCL），所以不是唯一防線。

### B. 偵測「有別人開著」並大聲說

`TIOCEXCL` 擋不住 root，而 root 的情況今天也發生過（09-16 的 `sudo uart-probe.sh`）。
代理應該定期看一眼還有誰開著這個裝置（掃 `/proc/*/fd` 找指向同一個裝置節點的），
發現第二個就**報出行程與命令列**——今天這個資訊得手動 `fuser` 才拿得到，
而它就是答案本身。

搭配 050 已經做好的那兩個：termios 被改（`port_tampered`）與心跳失聯（`fc_link_ok`）。

> **裁定（2026-09-21 使用者）：B 偵測到的結果要報到地面站**，讓操作員在畫面上
> 就看得到「機上有別的程式在搶飛控」。今天這個資訊得 ssh 上去 `fuser` 才拿得到，
> 而它就是答案本身；放在畫面上，下次五秒就解決。走 [057](057-agent-events-never-consumed.md)
> 的事件管道——**那條管道要先接起來**，否則事件丟進去就消失。
>
> 附帶裁定：**啟動時發現埠已被別人開著，不拒絕啟動**。一個被干擾的橋仍然
> 比沒有橋好，它該照跑並持續抱怨。

### C. 給其他程式一條**正確**的路，並寫進文件

搶埠往往不是惡意，是不知道還有別條路。代理已經把 MAVLink 轉發成
UDP（遙測 14540、指令 14541），那條路本來就是給其他程式用的、不會搶位元組。
**`uav-agent/README.md` 要明寫**：機上任何要讀飛控的程式一律走 UDP，
不准直接開 `/dev/ttyAMA0`。

### D.（待評估）unit 層的隔離

systemd 的 `DeviceAllow`／`PrivateDevices` 能不能讓別的服務根本看不到這個節點，
要看實際部署（`get-gps.py` 是人手跑的，不是 service，unit 層管不到它）。
**先做 A＋B＋C**。

## 相關

* 050 需求 1——`TIOCEXCL` 原本列在那裡但沒做，本條把它獨立出來並加上偵測與文件
* 058（參數被外部改動不提示）——同一天的另一半

## 解決方式

### A（2026-09-22，uav-agent `e3500ee`，已部署）

* `_open_serial_once` 拿到 fd 後呼叫 `_claim_exclusive`：`ioctl(fd, TIOCEXCL)`。
  非 tty（`--dev udpin:`）跳過。下不成功只 `log.error`、**不擋開機**（本案裁定）。
  結果放進狀態列 `port_exclusive`：`true`／`false`／`null`（不是 tty）。
* **實作時翻出的第二件事**：獨佔旗標只在 tty 被**最後**釋放時才由核心清除。
  只要別人（例如 root 的 `stty`）還開著，代理關掉自己的 fd 並不是最後一次，
  050 需求 2 的「先關再開」會被**自己上次的獨佔**擋在外面。所以關舊埠之前先
  `TIOCNXCL`（`_release_exclusive`）。pty 上重現過這個自鎖。

驗證：

| | 結果 |
|---|---|
| `tools/uart-exclusive.py`（pty、驗行為）| ✅ 全過；含兩個反例：pyserial `exclusive=True` 擋不住、只關不放會自鎖 |
| `tools/fc-link-watchdog.py` | ✅ 全過（fake 綁的是真的 `_claim`／`_release`）|
| 上機 Pi 5，`pi` 身分 `open("/dev/ttyAMA0")` | ✅ `EBUSY`；`stty -F` 也是 busy |
| 上機，root 開 | 仍開得起來——**已知缺口，由 B 處理** |
| 上機，重啟後鏈路 | ✅ `port_exclusive=true`、心跳 0.8 s、`msgs_from_fc` 持續增加、`port_tampered=0` |

**沒驗到的**：在真的 UART 上跑「程式內先放再關再開」那條路（要讓飛控心跳斷 15 秒
才會觸發）。pty 上驗過；真機只驗到行程重啟那種釋放。

**A 擋不住的兩種**仍然成立，都是 B 的範圍：root，以及**早於代理開埠的人**
（09-21 那次 `get-gps.py` 比代理早 7 秒——TIOCEXCL 只擋之後的 `open()`）。

### B（**暫停中**，2026-09-22）——程式與測試完成、**未部署到機上**

> **暫停原因**：部署當下地面站的 5G 斷線約 4 分鐘（13:58 起，連地面站預設閘道
> `10.141.2.22` 都不通，不是 Pi 的問題），使用者決定先暫停、改做與代理無關的功能。
> **機上仍是 057 那一版**（`agent.py` md5 `e12bb3b…`），沒有 `holders.py`，unit 沒動——
> 已上機確認。**repo 比機上新**，恢復時照下面「剩下的」做完即可。

與 050 需求 3 同一批做（兩者共用 `state` 的 `fc_link` 區塊）。

**已完成（commit 訊息開頭「059 B／050 需求 3（未部署）」）：**

* `uav-agent/holders.py`：掃 `/proc/*/fd` 找開著同一裝置的**別的**行程，回傳
  pid／使用者／命令列；**讀不到 fd 表的行程數另外回報**（`unreadable`）——
  不是 0 時「沒找到」不等於「沒有」。裝置名先解符號連結（`/dev/serial0`）。
* `agent.py` `_check_port_holders`：每 5 秒掃一次（開發機實測 9 ms）。
  名單**變了**才發 `notice`（`fc_port`；有人搶＝critical、走了＝info）；
  看不完整時說一次「代理看不到所有程式的開檔」。名單本身一直在 `state.fc_link.holders`
  與狀態列 `port_holders`。
* unit 加 `AmbientCapabilities=CAP_SYS_PTRACE`：沒有它，root 行程的 fd 表全是
  `EACCES`，而 root 正是 TIOCEXCL 擋不住的那種。`pi` 本來就有免密碼 sudo，
  沒有多給權力。
* 地面站：`agent_link.as_dict` 多轉 `fc_link`（**已部署**；舊代理不送＝null，無影響）；
  CommandPanel 在代理回報有人搶埠時**常駐**一條警告（事件會被捲走，搶埠是持續狀態），
  完整命令列在 tooltip。前端**已建置上線**，但機上代理還沒送這一格，所以看不到。
* `tools/port-holders.py`：另開一個真的行程去開同一個 pty，跑真的
  `_check_port_holders`——找得到、帶得出命令列、只在名單變時喊、走了也說、
  別名找得到、非 tty 跳過、看不完整時要說。全過。

**實作時另外發現：059 C 的前提不成立。** C 要文件寫「機上程式一律走 UDP」，
但代理的 UDP（14540／14541）是送**地面站**的，機上**沒有給本機程式用的
MAVLink 出口**。照寫就是指一條不存在的路。搶埠的 notice 因此只說「停掉它」，
不叫人改走哪裡。**C 要先決定要不要做本機出口**（那條路若能上行，等於讓
機上任何程式對飛控下指令，要一起想守門）。

**剩下的（恢復時）：**

1. 部署到 Pi：`agent.py`、`holders.py`、`tools/port-holders.py`、`tools/fc-link-watchdog.py`；
   unit 用 `sudo install` 裝上並 `daemon-reload`（**Pi 上的 unit 與 repo 有一處既有差異**：
   repo 多 `Environment=FC_SYSID=1`，程式預設本來就是 1，無影響）。舊檔先備份。
2. 確認 `/proc/<pid>/status` 的 `CapAmb` 帶 ptrace、`fc_link.holders_unreadable` 是 0。
3. 上機實測：`sudo python3 -c` 開著 `/dev/ttyAMA0` **只開不讀**（不會搶走位元組）
   約 15 秒 → 事件流出現 critical 的 `agent_notice`、CommandPanel 出現警告、
   關掉後出現「已沒有別的程式」。
4. 用 `sudo stty -F /dev/ttyAMA0 1500000` 驗「序列埠設定被外部改掉」那則也送得上來。
