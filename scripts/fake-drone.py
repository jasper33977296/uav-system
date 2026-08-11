#!/usr/bin/env python3
"""輕量假回應機——多機驗收用（issue 011 混合路徑，PM 2026-08-11 定案）。

真 SITL 太重、跑 N 個 Gazebo 不划算；demux/指令路由/逐台架次是**我們的**
程式，假機足以驗。本腳本＝最小回應式假 autopilot：
  - 以指定 sysid 發 HEARTBEAT（帶 armed/custom_mode）＋GLOBAL_POSITION_INT
    ＋SYS_STATUS 到地面站的 14540（資料）與 14541（指令）兩通道。
  - 收指令通道回來的 COMMAND_LONG 並回應：ARM/DISARM→改 armed＋ACK、
    DO_SET_MODE→改 custom_mode＋ACK、NAV_TAKEOFF→ACK＋模擬爬升、
    MISSION_START→ACK；MANUAL_CONTROL→接受（不跑物理）。
  - 心跳反映狀態，所以 backend 逐台架次（armed 轉換）、command 的模式
    驗證（custom_mode 比對）、demux 分流都能測。

**非**真 PX4 行為（無物理、無 EKF、無 failsafe）——「多台同時真實移動的
手動控制」與「兩階段真實起飛時序」仍需真 PX4（013-B 用 2 台真機）。

用法（地面站；真 SITL 已在 sysid 1 時，補假機）：
    python3 scripts/fake-drone.py --sysid 2 [--gs 127.0.0.1] [--lat 47.398 --lon 8.546]
    # 多開幾台：--sysid 3、4…（各自唯一）
"""
import argparse
import math
import socket
import sys
import threading
import time

from pymavlink.dialects.v20 import common as mav

M = mav
ap = argparse.ArgumentParser()
ap.add_argument("--sysid", type=int, required=True)
ap.add_argument("--gs", default="127.0.0.1", help="地面站 IP")
ap.add_argument("--data-port", type=int, default=14540)
ap.add_argument("--cmd-port", type=int, default=14541)
ap.add_argument("--lat", type=float, default=47.3978)
ap.add_argument("--lon", type=float, default=8.5456)
args = ap.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", 0))
sock.settimeout(0.1)
targets = [(args.gs, args.data_port), (args.gs, args.cmd_port)]
enc = mav.MAVLink(None, srcSystem=args.sysid, srcComponent=1)

# 起手 HOLD（(main4,sub3)<<）——像真機一樣有模式，不顯 MODE_0
st = {"armed": False, "custom_mode": (4 << 16) | (3 << 24), "alt_rel": 0.0,
      "alt_target": 0.0, "lat": args.lat, "lon": args.lon}
lock = threading.Lock()


def send_all(msg):
    buf = msg.pack(enc)
    for t in targets:
        sock.sendto(buf, t)
    enc.seq = (enc.seq + 1) % 256


def sender():
    """2Hz：HEARTBEAT＋位置＋SYS_STATUS 到兩通道。"""
    while True:
        with lock:
            base = M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            if st["armed"]:
                base |= M.MAV_MODE_FLAG_SAFETY_ARMED
            cm = st["custom_mode"]
            # 模擬爬升/降落：alt_rel 往 target 靠
            d = st["alt_target"] - st["alt_rel"]
            st["alt_rel"] += max(-1.0, min(1.0, d))
            alt_rel, lat, lon = st["alt_rel"], st["lat"], st["lon"]
        send_all(enc.heartbeat_encode(M.MAV_TYPE_QUADROTOR, M.MAV_AUTOPILOT_PX4,
                                      base, cm, M.MAV_STATE_ACTIVE if st["armed"]
                                      else M.MAV_STATE_STANDBY))
        send_all(enc.global_position_int_encode(
            int(time.time() * 1000) % 4294967295,
            int(lat * 1e7), int(lon * 1e7), int((500 + alt_rel) * 1000),
            int(alt_rel * 1000), 0, 0, 0, 65535))
        # SYS_STATUS：電量＋PREARM 健康位（就緒顯示用），全感測器健康
        allb = 0x1 | 0x2 | 0x4 | 0x8 | 0x20 | 0x10000000
        send_all(enc.sys_status_encode(allb, allb, allb, 200, 16000, -1, 100,
                                       0, 0, 0, 0, 0, 0))
        time.sleep(0.5)


def ack(target, cmd, result=0):
    sock.sendto(enc.command_ack_encode(cmd, result).pack(enc), target)
    enc.seq = (enc.seq + 1) % 256


threading.Thread(target=sender, daemon=True).start()
print(f"假機 sysid={args.sysid} 上線：→ {args.gs}:{args.data_port}/{args.cmd_port}",
      flush=True)

while True:
    try:
        data, addr = sock.recvfrom(4096)
    except socket.timeout:
        continue
    try:
        msgs = enc.parse_buffer(data) or []
    except Exception:
        continue
    for m in msgs:
        t = m.get_type()
        if t == "COMMAND_LONG" and m.target_system == args.sysid:
            c = m.command
            with lock:
                if c == M.MAV_CMD_COMPONENT_ARM_DISARM:
                    st["armed"] = m.param1 >= 0.5
                    if not st["armed"]:
                        st["alt_target"] = 0.0
                    print(f"  sysid{args.sysid} {'ARM' if st['armed'] else 'DISARM'}", flush=True)
                elif c == M.MAV_CMD_DO_SET_MODE:
                    st["custom_mode"] = (int(m.param2) << 16) | (int(m.param3) << 24)
                    print(f"  sysid{args.sysid} SET_MODE main={int(m.param2)} sub={int(m.param3)}", flush=True)
                elif c == M.MAV_CMD_NAV_TAKEOFF:
                    st["armed"] = True
                    st["alt_target"] = (m.param7 - 500.0) if m.param7 > 100 else 10.0
                    print(f"  sysid{args.sysid} TAKEOFF → {st['alt_target']:.0f}m", flush=True)
            ack(addr, c, M.MAV_RESULT_ACCEPTED)
        elif t == "MANUAL_CONTROL":
            pass   # 接受，不跑物理（並發手動控制的路由測試足矣）
