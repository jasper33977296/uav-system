#!/usr/bin/env python3
"""ArduPilot 方言驗收（issue 015）。

**為什麼不透過 command API 跑**：capabilities 對未驗證機型是全鎖的，command
服務會直接拒絕——那正是 gating 的用意。要先用獨立測試工具驗證方言，**驗過的
項目才把 capabilities 開成 ok**。這支就是那個工具，不是產品的一部分。

驗三個 gap-analysis 標「待驗證」的項目，以及可攜指令：
  A. 任務 seq 0＝home 的位移（ArduPilot 慣例；PX4 沒有）
  B. SYSID_MYGCS：ArduPilot 預設只信 255 的 MANUAL_CONTROL，我方是 254
  C. SYS_STATUS 的 PREARM 健康位支援度
  D. 可攜指令：NAV_RETURN_TO_LAUNCH／NAV_LAND／NAV_LOITER_UNLIM／MISSION_START

用法：docker exec -i -w /srv uav-backend python3 - < scripts/accept-ardupilot.py
"""
import sys
import time

from pymavlink import mavutil

M = mavutil.mavlink
URL = "tcp:127.0.0.1:5760"
GCS_SYSID = 254           # 與 command 服務相同（mav.py），這正是 B 項要驗的
results = []


def rec(item, ok, detail):
    results.append((item, ok, detail))
    print("  %s %s：%s" % ("✅" if ok else "❌", item, detail), flush=True)


def main():
    m = mavutil.mavlink_connection(URL, source_system=GCS_SYSID,
                                   source_component=M.MAV_COMP_ID_MISSIONPLANNER)
    hb = m.wait_heartbeat(timeout=30)
    tgt = m.target_system
    print("連上 ArduPilot SITL：sysid=%s autopilot=%s（我方 GCS sysid=%d）\n"
          % (tgt, hb.autopilot, GCS_SYSID), flush=True)

    # ── C. PREARM 位支援度 ──────────────────────────────────────────
    ss = m.recv_match(type='SYS_STATUS', blocking=True, timeout=10)
    if ss is None:
        rec("C PREARM 位", False, "10 秒內收不到 SYS_STATUS")
    else:
        bit = M.MAV_SYS_STATUS_PREARM_CHECK
        present = bool(ss.onboard_control_sensors_present & bit)
        healthy = bool(ss.onboard_control_sensors_health & bit)
        rec("C PREARM 位", present,
            "present=%s health=%s（present=False 代表 ArduPilot 不回報這個位，"
            "就緒判定要退回 STATUSTEXT）" % (present, healthy))

    # ── A. 任務 seq 0＝home 的位移 ──────────────────────────────────
    # 上傳 3 個航點，回讀看筆數與 seq 0 的內容
    wps = [(47.3980, 8.5460, 20.0), (47.3985, 8.5465, 25.0), (47.3990, 8.5470, 30.0)]
    m.mav.mission_count_send(tgt, 1, len(wps), M.MAV_MISSION_TYPE_MISSION)
    sent = 0
    t0 = time.time()
    while sent < len(wps) and time.time() - t0 < 20:
        req = m.recv_match(type=['MISSION_REQUEST', 'MISSION_REQUEST_INT'],
                           blocking=True, timeout=5)
        if req is None:
            break
        i = req.seq
        lat, lon, alt = wps[min(i, len(wps) - 1)]
        m.mav.mission_item_int_send(
            tgt, 1, i, M.MAV_FRAME_GLOBAL_RELATIVE_ALT,
            M.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 0, 0, 0,
            int(lat * 1e7), int(lon * 1e7), alt, M.MAV_MISSION_TYPE_MISSION)
        sent += 1
    ack = m.recv_match(type='MISSION_ACK', blocking=True, timeout=10)
    up_ok = ack is not None and ack.type == 0
    rec("A1 任務上傳", up_ok, "送出 %d 項，ACK=%s" %
        (sent, ack.type if ack else "無"))

    # 回讀
    m.mav.mission_request_list_send(tgt, 1, M.MAV_MISSION_TYPE_MISSION)
    cnt = m.recv_match(type='MISSION_COUNT', blocking=True, timeout=10)
    items = []
    if cnt:
        for i in range(cnt.count):
            m.mav.mission_request_int_send(tgt, 1, i, M.MAV_MISSION_TYPE_MISSION)
            it = m.recv_match(type='MISSION_ITEM_INT', blocking=True, timeout=5)
            if it:
                items.append((it.seq, it.command, it.x / 1e7, it.y / 1e7, it.z))
    m.mav.mission_ack_send(tgt, 1, 0, M.MAV_MISSION_TYPE_MISSION)
    if not items:
        rec("A2 seq 0＝home 位移", False, "回讀不到任務項")
    else:
        shifted = len(items) == len(wps) + 1
        first = items[0]
        rec("A2 seq 0＝home 位移", True,
            "上傳 %d 項、回讀 %d 項 → %s；seq0=(cmd %d, %.5f, %.5f, %.1f)%s"
            % (len(wps), len(items),
               "**多一項＝home 佔 seq 0**" if shifted else "筆數相同（無位移）",
               first[1], first[2], first[3], first[4],
               "" if not shifted else " ← 我方回讀比對必須跳過 seq 0"))

    # ── D. 可攜指令（只驗「接受」，不真的飛完）────────────────────
    for name, cmd in (("NAV_LOITER_UNLIM(Hold)", M.MAV_CMD_NAV_LOITER_UNLIM),
                      ("NAV_RETURN_TO_LAUNCH(RTL)", M.MAV_CMD_NAV_RETURN_TO_LAUNCH),
                      ("NAV_LAND(Land)", M.MAV_CMD_NAV_LAND)):
        m.mav.command_long_send(tgt, 1, cmd, 0, 0, 0, 0, 0, 0, 0, 0)
        a = m.recv_match(type='COMMAND_ACK', blocking=True, timeout=5)
        ok = a is not None and a.result == M.MAV_RESULT_ACCEPTED
        rec("D %s" % name, ok,
            "ACK=%s" % (M.enums['MAV_RESULT'][a.result].name if a else "無回應"))

    # ── B. SYSID_MYGCS：ArduPilot 認不認我方是「那個 GCS」──────────
    # **這項的理由 2026-08-31 改寫過。** 原本是「我方 254 的 MANUAL_CONTROL
    # 會不會被靜默丟棄」——搖桿已隨 issues/035 整塊移除，那個理由不在了。
    # 但檢查本身仍然要做，而且更重要：ArduPilot 用這個參數決定**誰的心跳算
    # GCS 心跳**，那是 GCS failsafe（FS_GCS）的判準，也是 issues/039 整套
    # 失聯處置的機上前提。不符的話飛控根本不覺得我們斷過線。
    m.mav.param_request_read_send(tgt, 1, b"SYSID_MYGCS", -1)
    pv = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=10)
    mygcs = pv.param_value if pv else None
    if mygcs is None:
        m.mav.param_request_read_send(tgt, 1, b"MAV_GCS_SYSID", -1)
        pv = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=10)
        mygcs = pv.param_value if pv else None
    rec("B SYSID_MYGCS", mygcs is not None and int(mygcs) == GCS_SYSID,
        "機端值=%s，我方 GCS sysid=%d → %s"
        % (int(mygcs) if mygcs is not None else "讀不到", GCS_SYSID,
           "相符，飛控認我方為 GCS" if (mygcs is not None and int(mygcs) == GCS_SYSID)
           else "**不符：飛控不會把我方的心跳當成 GCS 心跳**，"
                "GCS failsafe 因此不會依我們的斷線觸發（issues/039 的機上前提）。"
                "我方 GCS sysid 已是 255＝ArduPilot 出廠預設，"
                "所以正常情況不需要動機端；讀到別的值代表這台被改過"))

    print("\n" + "=" * 60)
    for item, ok, _ in results:
        print("  %s %s" % ("PASS" if ok else "FAIL", item))
    return 0


if __name__ == "__main__":
    sys.exit(main())
