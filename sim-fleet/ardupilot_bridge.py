#!/usr/bin/env python3
"""ArduPilot SITL ↔ 機隊 fanout 的橋接（issue 015 驗收用）。

為什麼需要它：ArduPilot SITL（`sim_vehicle.py --no-mavproxy`）只開 **TCP 5760**，
而機隊的 fanout 收的是 UDP（14545）。這支就是純位元組轉送，不解析、不改內容
——保真度優先：驗收要驗的是**我們的程式怎麼處理 ArduPilot 方言**，橋接若動了
內容就等於在驗一個被我們自己修飾過的東西。

接進 fanout（而不是讓 backend 直連）是刻意的：這樣 ArduPilot 走的是與 PX4
**完全相同的單埠 demux 路徑**，驗到的才是「多廠牌混機能不能共存」，
而不只是「ArduPilot 單獨能不能跑」。

用法（在有 python3 的容器內，host network）：
    python3 ardupilot_bridge.py            # 預設 tcp 5760 ↔ udp 14545
環境變數：ARDU_TCP（預設 127.0.0.1:5760）、FANOUT（預設 127.0.0.1:14545）
"""
import os
import socket
import sys
import threading
import time

TCP_HOST, TCP_PORT = (os.environ.get("ARDU_TCP", "127.0.0.1:5760").split(":"))
FAN_HOST, FAN_PORT = (os.environ.get("FANOUT", "127.0.0.1:14545").split(":"))
FANOUT = (FAN_HOST, int(FAN_PORT))


def main():
    while True:                                   # SITL 重啟時自動重連
        try:
            tcp = socket.create_connection((TCP_HOST, int(TCP_PORT)), timeout=10)
            tcp.settimeout(None)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(("0.0.0.0", 0))
            print("bridge: tcp %s:%s ↔ udp → %s:%s" %
                  (TCP_HOST, TCP_PORT, FAN_HOST, FAN_PORT), flush=True)

            def up():                             # 飛機 → fanout（遙測）
                while True:
                    d = tcp.recv(4096)
                    if not d:
                        raise ConnectionError("SITL 關閉了 TCP")
                    udp.sendto(d, FANOUT)

            def down():                           # fanout → 飛機（指令回程）
                while True:
                    d, _ = udp.recvfrom(65535)
                    tcp.sendall(d)

            t = threading.Thread(target=down, daemon=True)
            t.start()
            up()
        except Exception as e:
            print("bridge: 中斷（%s: %s），3 秒後重連" % (type(e).__name__, e),
                  flush=True)
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
