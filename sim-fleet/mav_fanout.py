#!/usr/bin/env python3
"""多機模擬環境的 MAVLink 合流路由——**listen-model**（issue 013／multi-sim-env.md pivot A）。

PX4 SITL 多實例（sitl_multiple_run）各實例 GCS mavlink 經 edit_rcS 導到本程式的
IN 埠（14545）；本程式合流轉發給 backend(14540)＋command(14550)、並把指令按
target_system 路由回各實例。這樣 backend/command 都拿到全艦隊（單埠 demux），且不必改埠。

第一次 bring-up 實測：PX4 GCS mavlink 不回「連線者」而是送固定 remote，故用 listen-model
（我方聽艦隊送來的、非我方連上去）。sysid 天然唯一（-i 0/1/2 → 1/2/3）。

拓撲：
  各實例 GCS(18570+i, 送 14545) ──▶ [S_in 14545] ──┬─▶ backend 14540（雙向，見下）
                                                     └─▶ command 14550（雙向；指令依 sysid 送回實例 18570+i）

backend 這條原本設計成 forward-only（讀寫分離），但那讓**真機做得到的事在模擬環境
做不到**：正式部署裡 backend 直接綁 14540、機器直接送它，它回覆走機器的來源位址是通的
（任務讀回、參數表請求都靠這條）。模擬環境若不回送，backend 的白名單查詢會靜默無回應
——不是產品缺陷，是模擬器不夠像真機。改成雙向後與正式部署一致。
（read-only 邊界仍由 backend 自己的 SEND_WHITELIST 管制，不靠這裡的拓撲擋。）
  各實例 GCS 監聽 18570+i 收指令；本程式記 sysid→(addr) 從遙測學。
"""
import os
import select
import time
import socket

from pymavlink import mavutil

M = mavutil.mavlink
HOST = os.environ.get("FLEET_HOST", "127.0.0.1")
IN_PORT = int(os.environ.get("FANOUT_IN_PORT", "14545"))       # 艦隊 GCS 送這裡
BACKEND = (HOST, int(os.environ.get("BACKEND_PORT", "14540"))) # forward-only
COMMAND = (HOST, int(os.environ.get("COMMAND_PORT", "14550"))) # 雙向
ROUTER_SYSID = 255

s_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)        # 收艦隊遙測＋回送指令給實例
s_in.bind(("0.0.0.0", IN_PORT))
s_be = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)        # ↔ backend（雙向，見下）
s_be.bind(("0.0.0.0", 0))
s_cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # ↔ command（送遙測、收指令）
s_cmd.bind(("0.0.0.0", 0))

parser = M.MAVLink(None)
parser.robust_parsing = True
sysid_addr = {}     # sysid → 該實例來源位址（指令回程；從遙測學）
_warn_t = {}        # 轉發失敗告警節流（per 目的地）
print(f"mav-fanout(listen)：艦隊→:{IN_PORT} ⇒ backend {BACKEND[1]}(fwd-only)／command {COMMAND[1]}",
      flush=True)


def _route_to_fleet(data):
    """command 的指令 → 依 target_system 送回對應實例（未知/廣播→全發已學到的）。"""
    dest = None
    try:
        for msg in (parser.parse_buffer(data) or []):
            ts = getattr(msg, "target_system", 0)
            if ts and ts in sysid_addr:
                dest = [sysid_addr[ts]]
            break
    except Exception:
        pass
    for addr in (dest or list(sysid_addr.values())):
        try:
            s_in.sendto(data, addr)
        except OSError:
            pass


while True:
    r, _, _ = select.select([s_in, s_cmd, s_be], [], [], 0.5)
    for s in r:
        try:
            data, src = s.recvfrom(65535)
        except OSError:
            continue
        if s is s_in:
            # 艦隊遙測：學 sysid→addr、原樣 fan-out 給 backend＋command
            if len(data) >= 6 and data[0] in (0xFD, 0xFE):
                sysid = data[5] if data[0] == 0xFD else data[3]
                if sysid and sysid != ROUTER_SYSID:
                    sysid_addr[sysid] = src           # 該實例 GCS 來源位址（回指令用）
            # **兩條腿各自 try**：合用一個 try 的話，前面那條丟例外會讓後面
            # 那條整包跳過——一條腿的暫時性錯誤會靜默餓死另一條。
            #
            # 而且**失敗要看得見**。2026-08-13 事故：backend 容器 recreate 之後，
            # 這裡對 backend 的轉發停止生效（command 那條照常），前端看到「連得上
            # 但 0 筆資料」，查了一輪才定位到 fanout。原本的 `except OSError: pass`
            # 讓它完全無聲——UDP 送到當時沒人聽的埠會回 ICMP port unreachable，
            # 而未連線的 UDP socket 上這個錯誤是延後回報的，正是「本地看起來
            # 沒事、下一次呼叫才炸」那一類。
            #
            # ⚠️ **確切觸發機制未完全查明**（重起 fanout 即恢復，現場已消失）。
            # 這裡修的是**結構**：一條腿壞掉不得拖累另一條、且不得無聲。
            for sock, dest, who in ((s_be, BACKEND, "backend"),
                                    (s_cmd, COMMAND, "command")):
                try:
                    sock.sendto(data, dest)
                except OSError as e:
                    now = time.monotonic()
                    if now - _warn_t.get(who, 0.0) > 10.0:
                        _warn_t[who] = now
                        print(f"mav-fanout：轉發給 {who}{dest} 失敗（{e}）"
                              "——該端可能重啟中；本訊息每 10 秒最多一則",
                              flush=True)
        else:
            # command／backend 回來的訊息 → 依 target_system 路由回艦隊
            _route_to_fleet(data)
