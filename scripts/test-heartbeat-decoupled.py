#!/usr/bin/env python3
"""GCS 心跳與指令服務解耦的驗證（issues/033 §4.2.1）。

**要證明的只有一件事：指令服務重啟時，心跳不會停。** 那是把心跳搬出去的
全部理由——原本每一次重啟（含只是存檔觸發 `--reload`）都是一次真實的 GCS
心跳中斷，而那可能超過飛控的 `FS_GCS_TIMEOUT`。

做法：扮一台機（往 command 的 MAVLink 埠送 HEARTBEAT），量回程 sysid 255
心跳的間隔，中間把 command 重啟一次，比較重啟前後的最大間隔。

**反向驗證同樣重要**：如果心跳其實沒在發，這支測試也會「通過」（沒有間隔可
量）。所以先斷言收得到，再斷言重啟時沒斷。

用法：python3 scripts/test-heartbeat-decoupled.py
"""
import json
import socket
import subprocess
import sys
import threading
import time

from pymavlink import mavutil

M = mavutil.mavlink
CWD = "/home/k200/uav-system"
CMD_PORT = 14541          # 生產埠；SITL 覆寫時改這裡
FAKE_SYSID = 77           # 不與現場任何機重疊
GCS_SYSID = 255

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 0))
sock.settimeout(0.5)
mav = M.MAVLink(None, srcSystem=FAKE_SYSID, srcComponent=1)
hb = mav.heartbeat_encode(M.MAV_TYPE_QUADROTOR, M.MAV_AUTOPILOT_ARDUPILOTMEGA,
                          0, 0, M.MAV_STATE_STANDBY).pack(mav)

stamps, stop = [], threading.Event()


def beat():
    """扮機：1 Hz 送 HEARTBEAT，讓 command 學到我們的來源位址。"""
    while not stop.is_set():
        try:
            sock.sendto(hb, ("127.0.0.1", CMD_PORT))
        except OSError:
            pass
        stop.wait(1.0)


def listen():
    """收回程，只記 sysid 255 的心跳到達時刻。"""
    parser = M.MAVLink(None)
    while not stop.is_set():
        try:
            data, _ = sock.recvfrom(65535)
        except (socket.timeout, OSError):
            continue
        try:
            for m in parser.parse_buffer(data) or []:
                if (m.get_type() == "HEARTBEAT"
                        and m.get_srcSystem() == GCS_SYSID):
                    stamps.append(time.time())
        except Exception:
            pass


threading.Thread(target=beat, daemon=True).start()
threading.Thread(target=listen, daemon=True).start()

print("── 暖機：讓 command 學到位址、心跳行程讀到它 ──────────")
time.sleep(8)

peers = subprocess.run(
    ["docker", "compose", "exec", "-T", "uav-command", "cat", "/state/peers.json"],
    capture_output=True, text=True, cwd=CWD).stdout.strip()
chk("command 把來源位址寫進位址表", str(FAKE_SYSID) in peers, peers[:160])

n0 = len(stamps)
chk("**先確認真的收得到心跳**（否則後面的『沒斷』是假通過）", n0 >= 3,
    f"{n0} 則")

print("\n── 重啟 command，量心跳的最大間隔 ──────────────────")
base = len(stamps)
t_restart = time.time()
subprocess.run(["docker", "compose", "restart", "uav-command"],
               capture_output=True, cwd=CWD)
time.sleep(12)

after = stamps[base:]
gaps = [b - a for a, b in zip(stamps[base - 1:], stamps[base:])] if base else []
worst = max(gaps) if gaps else None
chk("重啟期間心跳沒有停", len(after) >= 8, f"重啟後收到 {len(after)} 則")
chk("**最大間隔 < 3 秒**（遠低於 ArduPilot FS_GCS_TIMEOUT 的 5 秒預設）",
    worst is not None and worst < 3.0,
    f"最大間隔 {worst:.2f}s" if worst else "沒有量到間隔")

print("\n── 反向驗證：位址過期後應該停發（不是對著空氣喊）────")
stop.set()          # 假機不再送 HEARTBEAT → 位址表的時戳開始老化
time.sleep(1)
stale_stop = threading.Event()
late = []


def listen_late():
    parser = M.MAVLink(None)
    while not stale_stop.is_set():
        try:
            data, _ = sock.recvfrom(65535)
        except (socket.timeout, OSError):
            continue
        try:
            for m in parser.parse_buffer(data) or []:
                if (m.get_type() == "HEARTBEAT"
                        and m.get_srcSystem() == GCS_SYSID):
                    late.append(time.time())
        except Exception:
            pass


threading.Thread(target=listen_late, daemon=True).start()
print("   （等位址過期，約 35 秒）")
time.sleep(35)
n_before = len(late)
time.sleep(6)
stale_stop.set()
chk("位址過期後停止發送", len(late) == n_before,
    f"過期後又收到 {len(late) - n_before} 則")

sock.close()
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
