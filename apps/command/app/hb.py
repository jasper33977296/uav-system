"""GCS 心跳發送器——**獨立行程，不隨指令服務重啟**（issues/033 §4.2.1）。

## 為什麼要獨立出來

飛控的 GCS failsafe 盯的是我方 `sysid 255` 的心跳，而那個心跳**是地面發的**
（2026-08-31 查證推翻了原本「機上代理在發」的說法）。心跳原本住在
`MavRouter._tick` 裡，於是：

* 指令服務每一次重啟——`docker compose up -d`、crash、**甚至只是存一個檔案觸發
  `--reload`**——都是一次真實的 GCS 心跳中斷；
* graceful shutdown 上限 3 秒加上啟動時間，**很可能超過 ArduPilot `FS_GCS_TIMEOUT`
  的 5 秒預設**，也就是說我方的一次存檔就可能讓飛行中的機體觸發 failsafe。

使用者 2026-08-31 裁定把它搬出來。理由比原本的建議（靠 deploy guard 擋住重啟）
更強：**解耦之後，「我們自己的部署動作會不會變成飛控的失聯事件」整類消失**，
不是靠人記得別在飛行中重啟去迴避。

本 repo 已有同一個理由的先例：錄影不放進 backend，因為 backend 掛 `--reload`、
改一行程式就重啟＝錄影中斷（`docker-compose.yml` 的 `uav-video`）。

## 最大的約束不是「怎麼發」，是「發給誰」

心跳要送到每台機的**來源位址**，而那是 UDP 收包時學到的：機上代理的送出 socket
沒有 bind，來源埠由作業系統給，代理重啟或 5G 重連就換一個。**只有正在收包的
那個行程知道它。**

所以本行程不自己發現飛機，而是讀指令服務寫下的位址表；而且讀到之後**自己留著**
——否則指令服務一重啟本行程也跟著瞎掉，等於沒有解耦。

    指令服務（收包、學位址）──寫入──▶ peers.json ──讀取──▶ 本行程（只發心跳）

## 三條紀律（設計文件 §4.2.1）

1. 位址表要能跨指令服務重啟存活 → 放共用 volume 的檔案。
2. **本行程不得掛原始碼、不得有 `--reload`**：它存在的唯一理由就是不隨我們的
   開發動作重啟。做成跟 command 一樣的形狀等於白做。
3. **位址過期要停發，而且要說得出現在在對誰發**——否則我們會有一個「看起來在
   運作」卻對著空氣喊的元件，那正是 issues/034 那個殭屍 router 的形狀。

跑法（容器內）：`python -m app.hb`
"""
import json
import logging
import os
import socket
import time

from pymavlink import mavutil

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("gcs-hb")

M = mavutil.mavlink

#: 與 `mav.GCS_SYSID` 同值。**兩處各寫一份數字就是會漂移**，所以啟動時自檢：
#: 讀得到指令服務那份就比對，不同就拒絕啟動——一個發錯 sysid 的心跳，
#: 對飛控而言等於沒有心跳，而畫面上完全看不出來
GCS_SYSID = int(os.environ.get("GCS_SYSID", "255"))

#: 位址表：指令服務寫、本行程讀
PEERS_PATH = os.environ.get("GCS_PEERS_PATH", "/state/peers.json")
#: 幾秒沒更新就當那台機已經不在，停止對它發
PEER_STALE_S = float(os.environ.get("GCS_PEER_STALE_S", "30"))
HB_INTERVAL_S = 1.0
#: 多久印一次「現在在對誰發」。**這行不能省**（紀律 3）：沒有它，一個對著死位址
#: 猛發心跳的行程與一個正常工作的行程在外面看起來完全一樣
STATUS_EVERY_S = 60.0


def _check_sysid() -> None:
    """開機自檢：本行程的 sysid 必須與指令服務的一致。

    **不一致的後果是靜默的**：飛控只認 `SYSID_MYGCS` 指定的那個來源，
    發錯號的心跳會被當成別人的，GCS failsafe 照樣觸發——而我們這邊
    log 顯示「心跳正常發送中」。所以寧可拒絕啟動。
    """
    try:
        from .mav import GCS_SYSID as router_sysid
    except Exception as e:                      # 映像裡沒有 command 的模組時
        log.warning("讀不到指令服務的 GCS_SYSID（%s）——跳過自檢", e)
        return
    if router_sysid != GCS_SYSID:
        raise SystemExit(
            f"GCS sysid 不一致：本行程 {GCS_SYSID}、指令服務 {router_sysid}。"
            "兩者必須相同，否則飛控不會把我方的心跳當成 GCS 心跳。")


def load_peers(path: str) -> dict:
    """讀位址表。**讀不到就沿用上一次的**（見 main 的 last_good）。

    指令服務重啟的那幾秒檔案可能正在被覆寫（或還沒建出來），而那正是本行程
    最不該停下來的時刻——它存在的全部理由就是撐過那幾秒。
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for sysid, p in (raw.get("peers") or {}).items():
        try:
            out[int(sysid)] = (str(p["ip"]), int(p["port"]), float(p["t"]))
        except (KeyError, TypeError, ValueError):
            log.warning("位址表裡有一筆讀不懂的資料（sysid=%s）：%r", sysid, p)
    return out


def main() -> None:
    _check_sysid()
    mav = mavutil.mavlink.MAVLink(
        None, srcSystem=GCS_SYSID, srcComponent=M.MAV_COMP_ID_MISSIONPLANNER)
    buf = mav.heartbeat_encode(
        M.MAV_TYPE_GCS, M.MAV_AUTOPILOT_INVALID, 0, 0, 0).pack(mav)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    log.info("GCS 心跳發送器啟動：sysid=%d，%.0f Hz，位址表 %s（過期門檻 %.0fs）",
             GCS_SYSID, 1.0 / HB_INTERVAL_S, PEERS_PATH, PEER_STALE_S)

    last_good: dict = {}
    last_status = 0.0
    warned_missing = False
    while True:
        now = time.time()
        try:
            last_good = load_peers(PEERS_PATH)
            warned_missing = False
        except FileNotFoundError:
            if not warned_missing:
                warned_missing = True
                log.warning("還沒有位址表（%s）——指令服務尚未見過任何機。"
                            "**沒有機的時候不發心跳是對的**：對著不存在的位址發"
                            "只會讓 log 看起來很正常", PEERS_PATH)
        except Exception as e:
            # 沿用上一次讀到的：這幾秒可能正是指令服務在重啟、檔案寫到一半
            log.warning("位址表讀取失敗（%s）——沿用上一次的 %d 筆",
                        e, len(last_good))

        live, stale = [], []
        for sysid, (ip, port, t) in sorted(last_good.items()):
            (stale if now - t > PEER_STALE_S else live).append((sysid, ip, port, t))
        for sysid, ip, port, _t in live:
            try:
                sock.sendto(buf, (ip, port))
            except OSError as e:
                # 網路瞬斷是常態不是異常（5G）。**不能讓例外殺掉這個迴圈**：
                # 它死了就等於飛控失聯，而且外面看不出來
                log.warning("心跳送出失敗 sysid=%d → %s:%d（%s）",
                            sysid, ip, port, e)

        if now - last_status >= STATUS_EVERY_S:
            last_status = now
            if live:
                log.info("心跳中：%s%s",
                         "、".join(f"sysid {s}→{ip}:{p}" for s, ip, p, _ in live),
                         f"；另有 {len(stale)} 台位址過期已停發" if stale else "")
            else:
                log.info("目前沒有對象可發（位址表 %d 筆，全部過期或空）",
                         len(last_good))
        time.sleep(HB_INTERVAL_S)


if __name__ == "__main__":
    main()
