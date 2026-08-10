#!/usr/bin/env python3
"""測試飛行：起飛 → 穿越模擬干擾區 → RTL。觀察完整資料流：
SINR 驟降 → link_degraded/link_lost 事件 → 回升 link_recovered → 架次摘要。

2026-08-10 改版：不再自己講 MAVLink（mavsdk 退役）——任務經 API 入庫、
經 command 服務上傳與啟動（吃 gate/預檢/留痕），監看走 /api/live。
純標準庫，直接 python3 執行，不需要 venv。

前置（開發 .env）：ENABLE_COMMANDS=true、COMMAND_MAVLINK_URL=udpin://0.0.0.0:14550、
GEOFENCE_RADIUS_M=500、GEOFENCE_ALT_M=100（本航線 200m/30m，超出真機圍欄
是刻意的——它是模擬場景的展示航線；預檢擋下時會印出可讀原因）。

用法：python3 scripts/test-flight.py
"""
from gcs_client import BACKEND, COMMAND, call, monitor_until_disarm, pick_sysid, print_last_session

# 模擬干擾區中心（link_sim.DEFAULT_ZONES）：起飛點以北約 200m
HOME = (47.3977420, 8.5455940)
ZONE = (47.3995, 8.5456)
BEYOND = (47.4005, 8.5456)
ALT = 30.0


def main():
    sysid = pick_sysid()
    print(f"🎯 目標機 sysid={sysid}")

    # frame 3＝全域相對高度（帶座標的導航項）；frame 2＝MISSION（RTL/DO_*
    # 這類無座標指令——給錯 frame PX4 會回 MAV_MISSION_UNSUPPORTED）
    wp = lambda seq, latlon, alt, cmd, action, frame=3: {
        "seq": seq, "lat": latlon[0], "lon": latlon[1], "alt": alt,
        "action": action, "command": cmd, "frame": frame}
    mission = {
        "name": "test-flight（干擾區穿越）", "source": "plan-file",
        "waypoints": [
            wp(0, HOME, ALT, 22, "takeoff"),
            wp(1, ZONE, ALT, 16, "waypoint"),
            wp(2, BEYOND, ALT, 16, "waypoint"),
            wp(3, (0, 0), None, 20, "rtl", frame=2),
        ]}
    res = call(f"{BACKEND}/api/missions", mission)
    mid = res["id"]
    chk = res.get("check") or {}
    print(f"📄 任務入庫 {mid}（預檢 {'✅' if chk.get('ok') else '❌ ' + '；'.join(chk.get('problems', []))}）")

    up = call(f"{COMMAND}/api/command/{sysid}/mission/upload", {"mission_id": mid})
    print(f"⬆️  上傳：{up.get('uploaded')} 項，回讀比對 {'✅' if up.get('verified') else '❌'}")
    for note in up.get("px4_notes") or []:
        print(f"   📢 PX4: {note}")

    call(f"{COMMAND}/api/command/{sysid}/mission/start", {})
    print("🚀 任務啟動（PX4 會自動解鎖起飛）")
    monitor_until_disarm("——預期穿越干擾區時 SINR 驟降、link 轉 degraded/lost")
    print_last_session()
    print("\n完成。事件流見前端即時頁；指令留痕：command_log 資料表。")


if __name__ == "__main__":
    main()
