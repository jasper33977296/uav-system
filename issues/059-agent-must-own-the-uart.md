# 059 · uav-agent 必須永遠擁有 UART 的最高優先權

- 狀態：open（**裁定完成 2026-09-21**，待實作）
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

（closed 時補）
