# 069 · uav-agent 沒有一鍵部署腳本：每次都是手動 scp

- 狀態：open
- 嚴重度：medium（還沒出過事，但「Pi 上跑的和 repo 裡的不一樣」已經差點發生過一次）
- 位置：uav-agent repo（新腳本，建議 `deploy.sh`）；相關 `install-service.sh`、`tools/relocate-to-opt.sh`
- 建立：2026-09-22

## 現象

2026-09-22 使用者問：「現在有 uav agent 的一鍵部署腳本嗎？」**沒有。**

uav-agent 現有的兩支腳本都不是在做這件事：

| 腳本 | 在哪裡跑 | 做什麼 |
|---|---|---|
| `install-service.sh` | **Pi 上**、sudo、一次性 | UART overlay、裝 systemd unit、對時 |
| `tools/relocate-to-opt.sh` | Pi 上、sudo、一次性 | 2026-09 從家目錄搬到 `/opt` |

「從開發機把改好的程式推上 Pi、重啟、確認跑起來的就是這一版」沒有腳本，
每次都靠手動。059 A 部署（2026-09-22）實際做的步驟：

1. ssh 讀 `/opt/uav-agent/state.json` 確認 `armed=False`
2. `fuser -v /dev/ttyAMA0` 看有沒有別人開著
3. scp 到 `/tmp`，備份原檔成 `agent.py.bak-<tag>`，再搬進 `/opt/uav-agent`
4. `sudo systemctl restart uav-agent`，等幾秒
5. 兩邊 `md5sum agent.py` 比對
6. 從 journald 撈狀態列，看 `fc_link_ok`／`msgs_from_fc`／這次新增的欄位

每一步都是在鍵盤上現打的，漏掉哪一步也不會有任何東西提醒。

## 原因

部署一直是 `scp`，不是 `git pull`（Pi 上的 `/opt/uav-agent` 不是 git 工作副本）。
一開始只有 `agent.py` 一個檔，手動還算合理；現在機上已經有 `autopilot/`、
`intent.py`、`guard.py`、`modem.py`、`recorder.py`、`uploader.py`、`backfill.py`、
`clock.py`、`route.py`、`normalize.py` 加上 `tools/`，手動挑檔一定會漏。

## 影響

* **版本漂移**：只 scp 了 `agent.py`、忘了它 import 的模組，或反過來改了 repo
  沒部署。2026-08-24 差點發生過（見 git 流程的筆記：「Pi 上跑的是好的、repo 裡是壞的」）。
  目前唯一的核對方式是手動 `md5sum`，而且只核對了一個檔。
* **說不出機上跑的是哪一版**：Pi 上沒有任何東西記著「這是哪個 commit」。
  事後查問題時（例如 050／059 這種要比對「那時候的程式長什麼樣」的），只能猜。
* **飛安閘門靠人記得**：重啟代理＝橋斷幾秒。解鎖中重啟是不能做的事，現在全靠
  部署的人記得先看 `state.json`。

## 修法建議

在 uav-agent repo 加一支從**開發機**跑的 `deploy.sh`（或 `.py`），大致：

1. **前置**
   * 工作目錄不乾淨就拒絕（或要 `--dirty` 明說）——否則部署出去的東西沒有 commit 可以對應
   * 連得到 Pi（兩個位址都試：`10.141.2.32` 5G 側、`10.101.129.121` eth0）
   * **解鎖中就中止**。讀 `state.json`，而且要看它的**新鮮度**——代理死了的話
     那個檔會停在最後一刻，`armed=False` 可能是舊的。讀不到或太舊就當作
     「不知道」而中止，不是當作「沒解鎖」
2. **傳送**：`rsync` 整個執行期需要的檔（清單寫在腳本裡或用排除清單），
   不是挑單一檔案。先傳到暫存目錄，再原子地換上
3. **留痕**：在 Pi 上寫 `DEPLOYED`（commit hash、時間、部署者），
   並讓代理開機時把它 log 出來、放進狀態列——「機上跑的是哪一版」從此有答案
4. **備份與回滾**：保留上一版，`--rollback` 一行切回去
5. **重啟後驗收**：等狀態列出現，確認 `fc_link_ok`、`msgs_from_fc` 在增加；
   不過就報錯（要不要自動回滾另議）
6. **核對**：逐檔比對兩邊 hash，不只核對 `agent.py`

### 要決定的

* **systemd unit 有改時怎麼辦**：`install-service.sh` 要 sudo 且會動 `config.txt`。
  是部署腳本偵測到 unit 有差異就停下來叫人跑 install，還是自己裝 unit
  （只裝 unit，不碰 overlay）？
* **venv 相依有變時**：`requirements.txt` 改了要不要順便 `pip install`？
  機上現場不一定有網路（預設路由是黑洞，見 Pi 的筆記）。
* **驗收失敗要不要自動回滾**：自動回滾比較安全，但會把「新版哪裡不對」的現場蓋掉。

## 相關

* 059 A 的部署（2026-09-22）就是上面那六個手動步驟的實例
* `tools/relocate-to-opt.sh`——已經有「解鎖中就中止」的寫法可以借

## 解決方式

（closed 時補：實際改法 + commit hash）
