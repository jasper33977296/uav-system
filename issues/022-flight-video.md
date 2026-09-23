# 022 · 飛行影像：即時畫面＋架次錄影（mp4）＋回放同步播放

- 狀態：open
- 嚴重度：medium
- 位置：新 recorder 元件＋`apps/backend`（架次關聯/串流端點）＋前端即時頁/回放頁
- 建立：2026-08-12

## ⚠️ 2026-08-13：蘇黎世期間的驗收證據已移入備份

使用者要求「所有路徑移到台灣、台灣以外的資訊都拿掉」，故蘇黎世期間的
`flight_sessions`／`telemetry`／`events`／`video_segments`（27 筆）／`command_log`
／`link_metrics` 全數清空。

**證據沒有消滅，是搬家**——完整 dump 位於：

    backups/uav-20260813-133344-before-purge.sql

本 issue 在該日之前取得的影像驗收結論（含設計師「整份沿用不重跑」的那一批），
其底據都在上面這個檔案裡。要回溯就還原它查；**不要因為現在的庫是空的就認為
那些結論沒有根據**。

台灣的新錄影從 2026-08-13 的驗收飛行開始重新累積，設計師將據此重跑一輪，
讓結論重新建立在活資料上。


## 現象

使用者需求（2026-08-12）：無人機攝影功能三件——
1. 即時頁專注在某台機時要有對應的即時畫面；
2. 錄製路徑（架次）時一併錄影並存成 mp4；
3. 回放時可以看到影片播放。

## 現況（已有 vs 全新）

**已有**：`drones.video_url`（無人機頁「影像」設定）、即時頁地圖↔影像檢視
切換、`VideoModal`（單機／影像牆）、部署手冊 §2.4 的機上轉流建議
（RTSP→WHEP，瀏覽器不吃 RTSP）。→ 需求 1 的骨架在，缺真實相機來源。

**全新**：架次錄影（mp4）、影片與架次的關聯與時間對齊、回放同步播放。

## 使用者定案（2026-08-12）

| 題目 | 定案 |
|---|---|
| 錄影位置 | **地面站錄（從串流錄）**——架次開始自動錄、落地即可回放、時間軸天然對齊。副作用（影片品質受 5G 鏈路影響）視為研究資料而非缺陷：RF 劣化如何反映到應用層，本來就是研究主題 |
| 保留策略 | **影像 7 天**（量測資料維持 30 天，兩者脫鉤）；長期保留先匯出。**2026-08-12 修訂**：初次定案為「同 30 天」，後因實測磁碟需求（3 台×2h/天×30 天＝1080p30 407GB／720p15 150GB，而本機僅餘 128GB）由使用者改為 7 天＋720p15（≈49 GB）。以本列為準 |
| 相機來源 | **先用模擬／測試來源開發**（SITL 虛擬相機或測試串流），真相機到位再接 |
| 真機接入方向 | **2026-08-12 使用者說明：RB5 的 RTSP 不會主動推送，系統端要主動拉**——地面 MediaMTX 以 pull 模式接 RB5（path 設 source URL），網路前提隨之改為「地面站可達機端 IP」（5G 私網＋VPN 下要實測）。模擬環境的 testsrc 推流保留為測試鷹架。設計修正進行中 |

## 設計要點（待各方細化）

- **錄製觸發**＝架次生命週期（armed→disarmed），與 `flight_sessions` 綁定，
  沿用既有因果鏈（issue 020 的機制）。
- **時間對齊**：影片起始時戳必須與遙測時間軸可對齊，回放 scrub 才能同步
  （建議記錄「影片第 0 秒對應的絕對時間」而非假設同時開始）。
- **斷流是常態**（研究場景本來就在打壞鏈路）：錄製要能容忍中斷／續錄，
  且回放要**誠實呈現缺片段**（不靜默拼接假裝連續）。
- **不錄假資料**：模擬機（`is_simulated`）與測試架次（`origin='test'`）的
  錄影策略要想清楚，別讓測試影片吃掉磁碟。
- 儲存位置與磁碟預算（720p15 實流實測 ≈1.17 GB/飛行小時）、**影像 7 天**的
  自動清理（與量測資料 30 天脫鉤，見上表修訂）。保留天數**不寫死在 UI**：
  後端以中繼資料回傳 `retention_days`，前端空態句動態帶入。

## 2026-09-23：真相機接上了（Phase 3 的前半）

使用者把 **Logitech C920 PRO（046d:08e5）以 USB 接上機上的 Pi 5**，並裁定：
**拉流**、機上**獨立服務**（不塞進 uav-agent）、之後有機會換成相機模組走 Pi 的
專用介面。以下是做好並在真機上量過的。

### 機上（uav-agent repo，`camera/`，commit 7eb9563）

| 件 | 說明 |
|---|---|
| `uav-camera.service` | MediaMTX v1.21.1（arm64），User=pi、`SupplementaryGroups=video`、**Nice=5**——代理是 -5，相機永遠讓飛控資料先走 |
| `mediamtx.yml` | 只開 RTSP 8554；`runOnDemand` ＋ `runOnDemandCloseAfter: 10s` |
| `camera-source.sh` | 先 `v4l2-ctl --list-formats` 探格式：有 H.264 就 `-c copy`，沒有才軟編。`CAM_TEST=1` 給合成畫面，沒相機也能測整條鏈路 |

**MediaMTX 的執行檔不在 git 裡**（arm64 binary），安裝步驟寫在 `camera/README.md`。

**量到的事實**：C920 PRO 只給 `YUYV` 與 `MJPG`，**沒有 H.264**；Pi 5 也**沒有**
硬體 H.264 編碼器。所以目前走 `libx264 ultrafast`：**一顆核的 54.5%**、約
**2.7 Mbps**（8 秒 2.76 MB），load 0.08。換成相機模組或自帶 H.264 的 USB 相機時，
`camera-source.sh` 會自動改走 `-c copy`，這段 CPU 就不見了。

### 地面站（本 repo，commit 9c89ad6）

* **path 改綁機體身分**：`uav-<drones.id>`，不再是 `uav-<sysid>`。sysid 會被重新
  指派（040），舊寫法一旦相機通了，這台的架次會錄到**另一台的畫面**，而
  `sync_segments` 用時間區間歸屬會照樣把它記在這台名下——事後幾乎救不回來。
  改名的時機是 `video_segments` 還是 0 列的時候，沒有歷史要搬。
  `path_of()` **每次重查不快取**：快取一個會被重新指派的號碼正是那個坑本身。
* **`drones.camera_url`（新欄）**＝地面站要去拉的 `rtsp://<機IP>:8554/cam`；
  `video_url` ＝瀏覽器播放位址（WHEP），設前者時自動填。**兩件事不共用一欄。**
* `set_source()` 把 path 設成 `source: <camera_url>` ＋ `sourceOnDemand: yes`——
  沒人看也沒在錄的時候**完全不拉**。這不只是省頻寬：影像與 5G 量測共用同一條
  上行，一直傳等於量到的不再是原本那條鏈路的品質。
* 無人機管理頁多一顆「相機來源」。
* MapView：DB 裡的播放位址可能指著 `localhost`（在地面站自己設的），在別台電腦的
  瀏覽器上那是**它自己**——播放時換成這個瀏覽器正在用的主機名。

### 實作時才發現的坑：**錄影不算讀者**

`sourceOnDemand` 只在「有讀者在看」時才去拉，而**錄影不算讀者**。實測只設
`record: yes`：Pi 上根本不起 ffmpeg，`ready` 一直是 false、`bytesReceived` 是 0。
架次開始時通常沒有人開著即時頁，於是**整趟一段都錄不到**。

兩處修掉：

* `set_record(on)` 一併送 `sourceOnDemand: not on`——開錄就長駐拉流，收錄就回到
  on-demand。`set_source()` 在**正在錄**的時候不把 on-demand 設回來（飛行中換來源
  等於當場把錄影的來源關掉）。
* `has_source()` 與 `stream_ready()` 分家：拉流之後「有沒有相機」與「現在有沒有在
  傳」是兩件事。舊的 `decide_video_mode` 看後者，會把**每一趟**都判成 `no_source`。

### 驗證（2026-09-23，真機 uav-1）

| 項 | 結果 |
|---|---|
| 機上 on-demand | ✅ 沒人看時沒有 ffmpeg；有人拉才起、離開 10s 後自己收 |
| 地面站拉流 | ✅ `ready: true`、`tracks: ['H264']`、8 秒 2.76 MB |
| **開錄觸發拉流** | ✅ 修正後 **2 秒內** `ready: true`、`bytesReceived` 2.12 MB |
| 片段入庫 | ✅ playback `/list` 給 `start` ＋ `duration: 8.266`，`sync_segments` 寫進 `video_segments` |
| 收錄後回復 | ✅ `record: false`、`sourceOnDemand: true` |
| WHEP | ✅ headless Chrome 實測：`POST 201`→`ice=connected`→`ontrack video`，1280×720、**30 fps**、407 幀已解碼，codec `profile-level-id=42e01f`（H.264 baseline） |
| **即時頁實際看到畫面** | ✅ 即時頁右下小窗播出機上相機的實景（`readyState: 4`、`currentTime` 持續前進、1280×720）——不是黑畫面，是真的那顆鏡頭拍到的東西 |

`.env` 的 `VIDEO_RECORD_ENABLED` 從 false 改回 **true**——那行的註解原本寫的條件
（「先修 path↔機的綁定，再把這行改回 true」）就是這次做完的事。測試留下的那段
影像已從檔案與 DB 一併刪掉。

**驗證時順手踩到的**：從 `file://` 開的測試頁 WHEP 會 `Failed to fetch`（Origin 是
`null`），改成用 http 供這個頁就通了。**不是服務的問題**，MediaMTX 的
`Access-Control-Allow-Origin` 本來就是 `*`；記在這裡是免得下次又懷疑錯對象。

### 2026-09-23 傍晚：換成 CSI 相機模組

使用者把 USB 的 C920 拔掉、改接 CSI 模組到 CAM/DISP 接口。過程與結論：

**接上的是什麼**：使用者原以為是 OV5647，**實際是 IMX219（Camera Module v2）**，接在
**CAM/DISP1**。怎麼查出來的——`camera_auto_detect` 兩個接口都探不到，強制載入
`ov5647` 的 overlay 得到 `i2c read error … -121`（EREMOTEIO＝沒有人 ACK），
而且整條 I2C 掃下來全空。**全空這件事本身是線索**：如果只是型號猜錯，那顆感光元件
仍會在自己的位址上回應。後來逐一載入 14 種相機 overlay 再掃，才在 `i2c-11` 上看到
**0x10 有回應**——那是 IMX219 的位址。

**為什麼要逐一載入才看得到**：有些感光元件**沒有時脈與電源就不會回應 I2C**，而那是由
overlay 開的。只掃「沒載 overlay 的匯流排」會得到「什麼都沒有」的假結論。

機上 `config.txt` 已改（原檔備份為 `config.txt.bak-20260923-141445`）：

```
camera_auto_detect=0
dtoverlay=imx219
```

**自動偵測探不到這顆**，所以不能只靠 `camera_auto_detect=1`。

**`camera-source.sh` 的 CSI 分支**（uav-agent `a31e286`／`50090d3`／`17788e6`）：

* 先試 libcamera、沒有才用 USB（`CAM_SRC` 可強制）
* **不是用 `rpicam-vid -o -` 管給 ffmpeg**：rpicam-apps v1.12 的 `-o -` 實測**吐 0 bytes**
  （寫成檔案正常），ffmpeg 收到空輸入報 `unspecified size`。改用 rpicam-vid 內建的
  `--codec libav --libav-format rtsp` 直接推，連 ffmpeg 都不需要，而且只剩一個行程可以
  `exec`——MediaMTX 收掉它就真的乾淨，不會留下 rpicam-vid 把相機佔住
* 改寫時**漏掉 `--bitrate`**，實際跑 3.95 Mbps 而日誌照印 `2M`。補上後實測 2.12 Mbps。
  這條上行與 5G 量測共用，多出來的近 2 Mbps 會污染量到的鏈路品質

**量到的事實（720p30）**：

| | CSI IMX219 | USB C920 |
|---|---|---|
| CPU | **一顆核 36–46%** | 一顆核 55% |
| 碼率 | 2.12 Mbps | ~2.7 Mbps |
| 溫度 | 44–47°C，`throttled=0x0` | — |

**差在哪不是硬體編碼器**——Pi 5 兩條路都是軟編。C920 送的是 MJPEG，ffmpeg 要先解碼
再編碼；CSI 是 ISP 直接給 YUV，只編一次。（實作中我一度寫成「模組能吐 H.264 所以省
CPU」，**那是錯的**，已更正。另外先前記的「27%」是啟動後 2 秒的單一取樣，穩態是 36–46%。）

**驗證**：開機自動認到 ✅／地面站拉流 H.264 1280×720 ✅／開錄 2 秒內觸發拉流、9.7 秒
片段入庫、收錄後 on-demand 回復 ✅／即時頁小窗播出實景 ✅／最後一個讀者離開後**準時
10 秒**收掉 rpicam-vid ✅。

**還沒決定的**：模組的安裝方向。目前畫面是側躺的，裝上機時要用 `--rotation` 或實體轉正。

**注意**：測試期間 Pi 無預警重開三次（見 072）。時間點與串流重疊，但 `get_throttled`
全程 `0x0`，而且 09-22 那兩次發生時機上還沒有相機——**相關不等於原因**，072 記了對照
測試的做法。

### 還沒做完的（Phase 3 的後半）

* **真的飛一趟時的錄影沒驗過**：上面的開錄是手動呼叫 `set_record`，不是
  armed 觸發的。要在真架次上確認 `video_mode=on`→整趟有片段→落地停錄→
  未離地的那一趟會被刪掉。
* 回放頁的同步播放（Phase 2）本來就還沒做。
* **上行的代價還沒在飛行中量**：2.7 Mbps 與 5G 量測共用一條上行，`sourceOnDemand`
  是為此設計的，但「開著即時頁飛」對量測的實際影響還沒量過。

## 修法建議

分三階段：
1. **Phase 1**：地面錄製元件（架次觸發、mp4 落檔、與 session 關聯、時間錨點）
   ＋測試來源接通。
2. **Phase 2**：回放頁影片同步播放（時間軸 scrub、缺片段誠實呈現）。
3. **Phase 3**：即時畫面體驗收尾（選中機＝畫面來源，與現有檢視切換整合）
   ＋真相機接入驗證。

## 解決方式

（closed 時補）
