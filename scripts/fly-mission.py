#!/usr/bin/env python3
"""上傳並執行任務庫的任務（或先匯入 .plan 檔）。

2026-08-10 改版：經 command 服務（gate/預檢/留痕），不再自己講 MAVLink。
純標準庫，直接 python3 執行。

用法：
    python3 scripts/fly-mission.py <任務名稱或 mission_id>
    python3 scripts/fly-mission.py <檔案.plan>          # 先匯入任務庫再飛
"""
import json
import sys

from gcs_client import BACKEND, COMMAND, call, monitor_until_disarm, pick_sysid, print_last_session

NAV_CMDS = {16, 17, 18, 19, 20, 21, 22}


def import_plan(path: str) -> str:
    """QGC .plan → 任務庫（全部 SimpleItem 保真，與前端解析一致）。"""
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    if plan.get("fileType") != "Plan":
        sys.exit("❌ 不是 QGC 的 .plan 檔")
    wps = []
    for it in plan["mission"]["items"]:
        if it.get("type") != "SimpleItem":
            print(f"⚠️  跳過複雜項目 type={it.get('type')}")
            continue
        p = (it.get("params") or []) + [None] * 7
        cmd = it["command"]
        wps.append({
            "seq": len(wps), "lat": p[4] or 0, "lon": p[5] or 0,
            "alt": it.get("Altitude") or p[6],
            "action": {22: "takeoff", 21: "land", 20: "rtl"}.get(
                cmd, "waypoint" if cmd in NAV_CMDS else "do"),
            "command": cmd, "frame": it.get("frame"),
            "p1": p[0], "p2": p[1], "p3": p[2], "p4": p[3]})
    nav = [w for w in wps if w["command"] in NAV_CMDS and w["lat"] and w["lon"]]
    if len(nav) < 2:
        sys.exit("❌ 檔案內找不到足夠的導航航點")
    name = path.rsplit("/", 1)[-1]
    name = name[:-5] if name.endswith(".plan") else name
    res = call(f"{BACKEND}/api/missions",
               {"name": name, "source": "plan-file", "waypoints": wps})
    chk = res.get("check") or {}
    print(f"📄 已匯入任務庫「{name}」（{len(wps)} 項，預檢 "
          f"{'✅' if chk.get('ok') else '❌ ' + '；'.join(chk.get('problems', []))}）")
    for w in chk.get("warnings") or []:
        print(f"   ⚠️ {w}")
    return res["id"]


def resolve_mission(arg: str) -> str:
    missions = call(f"{BACKEND}/api/missions")
    for m in missions:
        if m["id"] == arg or m["name"] == arg:
            return m["id"]
    names = "、".join(m["name"] for m in missions[:10])
    sys.exit(f"❌ 任務庫找不到「{arg}」。現有：{names}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    arg = sys.argv[1]
    mid = import_plan(arg) if arg.endswith(".plan") else resolve_mission(arg)

    sysid = pick_sysid()
    up = call(f"{COMMAND}/api/command/{sysid}/mission/upload", {"mission_id": mid})
    print(f"⬆️  上傳 sysid={sysid}：{up.get('uploaded')} 項，"
          f"回讀比對 {'✅' if up.get('verified') else '❌'}")
    for note in up.get("px4_notes") or []:
        print(f"   📢 PX4: {note}")

    call(f"{COMMAND}/api/command/{sysid}/mission/start", {})
    print("🚀 任務啟動")
    monitor_until_disarm()
    print_last_session()


if __name__ == "__main__":
    main()
