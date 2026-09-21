#!/usr/bin/env python3
"""只有 5G 訊號、沒有遙測的機也要上即時頁（issues/049）。

2026-09-16：飛控串列被外部改掉 baud 而啞掉，後端又在那期間重啟了一次。
`ever_connected` 是 process 內的記憶體狀態，重啟就歸零，而飛控已經不講話了，
再也沒有 MAVLink 把它翻回 True——於是廣播迴圈整台跳過，即時頁空白 21 分鐘。

**而那段期間後端手上的 5G 訊號全程是新鮮的**（`/api/live` 查得到，機上回報
hz=1.0）。訊號走的是機上代理直接 POST 的路，與 MAVLink 無關，卻因為它的值是
搭遙測的便車送出去的，跟著一起消失。

所以這支測的是**那道閘的判準**，而重點在兩個方向都要成立：

  * 放行：只有訊號的機要上得了畫面（否則就是 09-16 那次）
  * 擋下：**幽靈機不能回來**（issues/036 的 B：從來沒人講話的機佔著畫面，
    而且長得跟有資料的機一樣）。判準用的是訊號的**新鮮度**，不是「曾經有過」
    ——一小時前來過一筆的機代表機上沒有人在講話。

跑法（不需要服務、不需要資料庫）：
    python3 scripts/test-link-only-broadcast.py
"""
import sys
import time

sys.path.insert(0, "/home/k200/uav-system/apps/backend")
sys.path.insert(0, "/home/k200/uav-system/libs")   # autopilot 驅動（dialect 會載）

from app.state import LiveState  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def drone(*, ever=False, link_age=None):
    """link_age=None＝從來沒收過訊號樣本；數字＝幾秒前收到最後一筆。"""
    st = LiveState(drone_id="d1", drone_name="測試機")
    st.ever_connected = ever
    if link_age is not None:
        st.link_seen_mono = time.monotonic() - link_age
    return st


print(__doc__.split("跑法")[0].strip().splitlines()[0])
print()

print("— 擋下（否則就是 036 的幽靈機）—")
chk("兩邊都沒有＝不廣播", not drone().broadcastable,
    "後端一啟動主機就進 fleet，不擋的話它從那一刻起就佔著畫面")
chk("訊號是一小時前的那一筆＝不廣播",
    not drone(link_age=3600).broadcastable,
    "機上沒有人在講話——「曾經有過」不算數")
chk("訊號剛好過期＝不廣播",
    not drone(link_age=LiveState.LINK_PRESENT_S + 1).broadcastable,
    f"門檻 {LiveState.LINK_PRESENT_S:.0f}s＝1Hz 取樣連掉三十筆")

print("\n— 放行 —")
chk("有過遙測＝廣播（既有行為不變）", drone(ever=True).broadcastable)
chk("斷線但曾連上＝照樣廣播", drone(ever=True).broadcastable,
    "最後已知位置要繼續送（使用者定案），擋的不是 connected")
chk("**只有新鮮訊號、從沒收過遙測＝廣播**", drone(link_age=1).broadcastable,
    "09-16 空白 21 分鐘的那一種")
chk("訊號在門檻上＝廣播",
    drone(link_age=LiveState.LINK_PRESENT_S).broadcastable)

print("\n— 放行之後送出去的東西要誠實（036 的規矩）—")
st = drone(link_age=1)
st.link = {"sinr": 30.0, "rsrp": -60.0, "pci": 133, "source": "modem"}
d = st.telemetry_dict()
chk("connected 是 False", d["connected"] is False, "飛控確實沒連上")
chk("ever_connected 是 False", d["ever_connected"] is False,
    "**前端據此說「沒有遙測」而不是「最後已知位置」**——後者根本不存在")
chk("位置是 None 不是 0", d["lat"] is None and d["lon"] is None,
    "0,0 會被畫在幾內亞灣（036）")
chk("telem_age_s 是 None＝從未收到", d["telem_age_s"] is None,
    '前端 staleLevel 的 never 分支')
chk("**訊號有送出去**", (d.get("link") or {}).get("sinr") == 30.0,
    "這才是整條 issue 的重點")
chk("link_age_s 是新鮮的", (d["link_age_s"] or 99) <= 2)

print("\n" + ("✓ 全部通過" if ok else "✗ 有失敗"))
sys.exit(0 if ok else 1)
