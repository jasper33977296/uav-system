#!/usr/bin/env python3
"""機上 IMU／感測器健康檢查：讀原始層 tlog，或現場聽 MAVLink UDP。

原始層（app/capture.py）錄下的 SYS_STATUS / VIBRATION / ESTIMATOR_STATUS /
STATUSTEXT 就包含 IMU 健康的完整資訊——本工具把位元旗標解成人話。
這是 issue 014 結構層的前哨：確認判讀正確後，同樣邏輯進 backend 上 UI。

用法（地面站或開發機，需 pip install pymavlink）：
  docker cp uav-backend:/data/mavcap/$(date -u +%Y%m%d).tlog /tmp/cap.tlog
  python3 scripts/imu-health.py /tmp/cap.tlog            # 分析錄製檔
  python3 scripts/imu-health.py udpin:0.0.0.0:14550 30   # 現場聽 30 秒

判讀基準：
  SYS_STATUS      present/enabled/health 三個位元遮罩，IMU 相關位元逐一比對
  VIBRATION       PX4 手冊經驗值 <30 佳、>60 有問題；clipping 累計數應為 0
  ESTIMATOR_STATUS ratio 值 <1 為健康（EKF 創新一致性）；旗標解碼
  STATUSTEXT      抓含 IMU/accel/gyro/mag/EKF 關鍵字的警告
"""
import sys

from pymavlink import mavutil

mav = mavutil.mavlink

IMU_BITS = [
    ("陀螺儀 gyro",   mav.MAV_SYS_STATUS_SENSOR_3D_GYRO),
    ("加速度計 accel", mav.MAV_SYS_STATUS_SENSOR_3D_ACCEL),
    ("磁力計 mag",    mav.MAV_SYS_STATUS_SENSOR_3D_MAG),
    ("gyro #2",       mav.MAV_SYS_STATUS_SENSOR_3D_GYRO2),
    ("accel #2",      mav.MAV_SYS_STATUS_SENSOR_3D_ACCEL2),
    ("mag #2",        mav.MAV_SYS_STATUS_SENSOR_3D_MAG2),
    ("AHRS 姿態解算", mav.MAV_SYS_STATUS_AHRS),
]

EKF_BITS = [
    ("姿態", mav.ESTIMATOR_ATTITUDE),
    ("水平速度", mav.ESTIMATOR_VELOCITY_HORIZ),
    ("垂直速度", mav.ESTIMATOR_VELOCITY_VERT),
    ("水平位置(絕對)", mav.ESTIMATOR_POS_HORIZ_ABS),
    ("垂直位置(絕對)", mav.ESTIMATOR_POS_VERT_ABS),
    ("GPS 異常", mav.ESTIMATOR_GPS_GLITCH),
    ("加速度計誤差", mav.ESTIMATOR_ACCEL_ERROR),
]

KEYWORDS = ("imu", "accel", "gyro", "mag", "ekf", "vibrat", "clip", "sensor")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    conn = mavutil.mavlink_connection(src)
    is_file = not src.startswith(("udp", "tcp"))

    sys_status = est = None
    vib_last = None
    vib_max = [0.0, 0.0, 0.0]
    clip = [0, 0, 0]
    texts = []
    import time
    deadline = time.monotonic() + seconds
    while True:
        if not is_file and time.monotonic() > deadline:
            break
        msg = conn.recv_match(
            type=["SYS_STATUS", "VIBRATION", "ESTIMATOR_STATUS", "STATUSTEXT"],
            blocking=not is_file, timeout=2)
        if msg is None:
            if is_file:
                break
            continue
        t = msg.get_type()
        if t == "SYS_STATUS":
            sys_status = msg
        elif t == "ESTIMATOR_STATUS":
            est = msg
        elif t == "VIBRATION":
            vib_last = msg
            v = (msg.vibration_x, msg.vibration_y, msg.vibration_z)
            vib_max = [max(a, b) for a, b in zip(vib_max, v)]
            clip = [msg.clipping_0, msg.clipping_1, msg.clipping_2]
        elif t == "STATUSTEXT":
            if any(k in msg.text.lower() for k in KEYWORDS):
                texts.append(msg.text)

    if sys_status is None:
        sys.exit("沒收到 SYS_STATUS——來源不對，或飛控未連線")

    print("══ IMU／感測器（SYS_STATUS 位元遮罩）══")
    p, e, h = (sys_status.onboard_control_sensors_present,
               sys_status.onboard_control_sensors_enabled,
               sys_status.onboard_control_sensors_health)
    for name, bit in IMU_BITS:
        if not p & bit:
            print("  {:14s} 未安裝".format(name))
        elif not e & bit:
            print("  {:14s} 安裝但停用".format(name))
        else:
            print("  {:14s} {}".format(name, "✅ 健康" if h & bit else "❌ 異常"))

    print("══ 震動（VIBRATION，佳<30 / 問題>60 m/s²）══")
    if vib_last is None:
        print("  （此串流 profile 未含 VIBRATION）")
    else:
        print("  最大 x/y/z = {:.2f} / {:.2f} / {:.2f}".format(*vib_max))
        print("  加速度計 clipping 累計 = {} / {} / {}  {}".format(
            *clip, "✅" if not any(clip) else "❌ 有削頂＝震動超過量測範圍"))

    print("══ EKF（ESTIMATOR_STATUS，ratio<1 為健康）══")
    if est is None:
        print("  （此串流 profile 未含 ESTIMATOR_STATUS）")
    else:
        for name, bit in EKF_BITS:
            on = bool(est.flags & bit)
            bad_flag = bit in (mav.ESTIMATOR_GPS_GLITCH, mav.ESTIMATOR_ACCEL_ERROR)
            mark = ("❌" if on else "✅") if bad_flag else ("✅" if on else "—")
            print("  {:14s} {}".format(name, mark))
        print("  創新比 vel/posH/posV/mag = {:.3f} / {:.3f} / {:.3f} / {:.3f}".format(
            est.vel_ratio, est.pos_horiz_ratio, est.pos_vert_ratio, est.mag_ratio))

    if texts:
        print("══ 相關警告（STATUSTEXT）══")
        for t in texts[-10:]:
            print("  ", t)


if __name__ == "__main__":
    main()
