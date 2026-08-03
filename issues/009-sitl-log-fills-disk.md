# 009 · SITL 容器沒掛 TTY，log 以 4.9GB/hr 寫爆磁碟

- 狀態：closed
- 嚴重度：critical
- 位置：`docker-compose.yml`（sitl service）
- 建立：2026-08-03
- 關閉：2026-08-03

## 現象

SITL 容器啟動後約 2.5 小時，247GB 的根分割區從 94GB 用量漲到 **100% 滿**，
整台機器無法寫入任何檔案。`/tmp` 在 `/` 上，所以連暫存檔都寫不了。

```
/dev/vda1  247G  247G  0  100% /
/var/lib/docker  →  146G
```

沒有任何徵兆——不是變慢，是突然所有寫入都 ENOSPC。

## 原因

**PX4 的互動式 shell 在沒有 TTY 時進入 busy loop。**

容器內的 PX4 啟動後會開一個互動式 console（提示字元 `pxh>`）。
`docker-compose.yml` 沒有設 `tty: true`，PX4 讀 stdin 立刻拿到 EOF，
於是不斷重畫提示字元，log 內容長這樣（無換行、無限重複）：

```
pxh> ESC[K pxh> ESC[K pxh> ESC[K ...
```

`ESC[K` 是「清除該行」的終端控制碼。這不是在記錄任何資訊，是純粹的空轉輸出。

第二個因素：**Docker 的 json-file logging driver 預設沒有大小上限**，
所以每一個 byte 都落到 `/var/lib/docker/containers/<id>/<id>-json.log`。

兩者相乘 = 磁碟被寫爆。

## 實測數據

| 狀態 | 20 秒 log 成長 | 換算 |
|---|---|---|
| 修正前（無 TTY）| 28,476,416 bytes | **4,888 MB/hr** |
| 修正後（`tty: true`）| **0 bytes** | 0 |

閒置就 4.9 GB/hr；實際觀察到 2.5 小時累積約 146GB（平均 ~58 GB/hr），
表示有 MAVLink 連線與飛行活動時速率更高。

## 影響

任何人照 README 把 SITL 掛著跑實驗都會踩到，而且是整台機器層級的故障
（本次同時影響到同機的其他專案）。這是本專案目前最嚴重的環境問題。

## 解決方式

`docker-compose.yml` 的 sitl service 加上兩層防護：

```yaml
    stdin_open: true      # 根治：讓 PX4 shell 有 TTY，不再空轉
    tty: true
    logging:              # 第二道防線：即使未來有其他來源狂寫也不會爆
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
```

清理已產生的 log（容器停掉後執行，glob 要在 root 下展開）：

```bash
sudo docker stop uav-system-sitl-1
sudo sh -c 'truncate -s 0 /var/lib/docker/containers/*/*-json.log'
```

驗證：重建容器後 20 秒內 log 成長 0 bytes，PX4 功能正常
（`14540 will be associated to 127.0.0.1`、MAVLink 照常連線）。

## 待辦：PX4 的 .ulg 飛行日誌

PX4 另外會寫 `.ulg` 飛行日誌到容器內（開機 log 可見
`Opened full log file: ./log/2026-08-03/03_06_52.ulg`）。目前它躺在容器的
writable layer，沒有掛出來也沒有上限，`--force-recreate` 就會消失。

兩個方向，未定：

1. 若不需要 → 現況可接受，但要留意長期執行的累積。
2. 若之後想用 PX4 原始飛行日誌與我們的 `telemetry` 表交叉比對驗證 →
   應該掛一個 volume 出來保存。

考量到本專案的研究資料以自己的 DB 為準，傾向 1，需要時再補。
