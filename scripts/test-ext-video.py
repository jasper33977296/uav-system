#!/usr/bin/env python3
"""外部控制的影像欄位（issues/022；2026-09-23 使用者裁定走 HLS over HTTP）。

兩段，**第二段連不到服務時會說出來並跳過，不會假裝通過**：

1. **純邏輯**：`libs/video_stream` 的三態與網址組法。不連 DB、不連服務。
2. **打正在跑的 command 服務**（`:38001/api/ext/drones`）：欄位形狀、
   `ready` 才有網址、網址的主機名跟著請求的 Host 走。

跑法：`python3 scripts/test-ext-video.py`
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "libs"))
import video_stream as V                                        # noqa: E402

HOST = os.environ.get("COMMAND_HOST", "127.0.0.1:38001")
DID = "1d2f19b1-a979-4f90-ad63-3d50a9ebad11"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name} {detail}")


print("── 1. 純邏輯：三態 ──")
r = V.ext_video(DID, "rtsp://10.141.2.32:8554/cam", True, "10.141.2.21")
check("有相機＋有連線 → ready", r["state"] == "ready", r)
check("ready 給得出網址", r["hls"] == f"http://10.141.2.21:8888/uav-{DID}/index.m3u8", r)
check("ready 帶上『不保證拉得到』那句", "拉了才知道" in r.get("note", ""), r)

r = V.ext_video(DID, "rtsp://10.141.2.32:8554/cam", False, "10.141.2.21")
check("有相機＋沒連線 → offline", r["state"] == "offline", r)
check("offline 不給網址", r["hls"] is None, r)
check("offline 說得出原因", "沒有連線" in r.get("reason", ""), r)

for cam in (None, "", "   "):
    r = V.ext_video(DID, cam, True, "10.141.2.21")
    check(f"沒設相機來源（{cam!r}）→ no_camera", r["state"] == "no_camera", r)
    check("  且不給網址", r["hls"] is None, r)

r = V.ext_video(None, None, True, "10.141.2.21")
check("沒有機體記錄 → no_camera 而不是丟例外", r["state"] == "no_camera", r)

check("path 名稱綁機體身分、不是 sysid", V.path_for(DID) == f"uav-{DID}")
check("自訂埠會被帶進網址",
      V.hls_url(DID, "h", 9999) == f"http://h:9999/uav-{DID}/index.m3u8")

print("\n── 2. 打正在跑的 command 服務 ──")
try:
    req = urllib.request.Request(f"http://{HOST}/api/ext/drones",
                                 headers={"Host": HOST})
    body = json.load(urllib.request.urlopen(req, timeout=5))
except (urllib.error.URLError, TimeoutError, OSError) as e:
    print(f"  ⚠ 連不到 {HOST}（{type(e).__name__}）——**跳過這一段，不算通過**")
    print(f"\n結果：{ok} 通過、{fail} 失敗（第 2 段跳過）")
    sys.exit(1 if fail else 0)

drones = body.get("drones", [])
check("回得出機隊清單", isinstance(drones, list))
for d in drones:
    tag = f"sysid {d.get('sysid')}"
    v = d.get("video")
    check(f"{tag}：有 video 欄位", isinstance(v, dict), d)
    if not isinstance(v, dict):
        continue
    check(f"{tag}：state 是三態之一",
          v.get("state") in ("ready", "no_camera", "offline"), v)
    if v.get("state") == "ready":
        check(f"{tag}：ready 有 hls", bool(v.get("hls")), v)
        check(f"{tag}：主機名跟著請求的 Host",
              (v.get("hls") or "").startswith(f"http://{HOST.split(':')[0]}:"), v)
        # **不保證拉得到**：這裡只驗契約有把話說出來，不驗真的拉得到畫面
        check(f"{tag}：有說明 ready 不等於一定有畫面", bool(v.get("note")), v)
    else:
        check(f"{tag}：非 ready 就不給網址", v.get("hls") is None, v)
        check(f"{tag}：非 ready 說得出原因", bool(v.get("reason")), v)
    # online 與 video.state 不能自相矛盾
    if d.get("online") is False:
        check(f"{tag}：沒連線時 state 不會是 ready", v.get("state") != "ready", d)

print(f"\n結果：{ok} 通過、{fail} 失敗")
sys.exit(1 if fail else 0)
