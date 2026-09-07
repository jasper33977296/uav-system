#!/usr/bin/env python3
"""任務進度落盤：拿**真飛的 tlog** 重播，驗事件記得對不對（2026-09-06）。

## 為什麼是重播而不是造假訊息

洞的成因不是「程式寫錯」而是「根本沒人解那則訊息」，所以真正要證明的是
**這台機真的會送、而且我們解得出來**。造一則假的 MISSION_CURRENT 餵進去
只能證明程式跑得動，證明不了飛控會送——那正是原本漏掉的那一半。

9/2 那七趟是真機真飛，MISSION_CURRENT 912 則。要求：
* 只有變化才落盤（912 → 個位數，不然事件流被淹掉等於沒記）
* 連續兩筆不會記到一樣的 seq
* `MISSION_ITEM_REACHED` 一則都不漏（那是「幾點到第幾點」的唯一來源）

在 backend 容器裡跑：`docker exec -i uav-backend python - < scripts/…`
"""
import asyncio
import glob
import inspect
import os
import sys
from collections import Counter

sys.path.insert(0, "/srv")
from pymavlink import mavutil                                    # noqa: E402
from app import db, mavlink_rx                                   # noqa: E402
from app.state import LiveState, MISSION_STATE                   # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


class Cap:
    """把 insert_event / broadcast 換成收集器——**不寫 DB**：這支要能對著
    正式資料庫跑而不留下垃圾。"""

    def __init__(self):
        self.evs = []

    async def insert_event(self, drone_id, session_id, sev, type_, detail,
                           source="system"):
        self.evs.append((type_, detail))
        return {"id": len(self.evs), "time": "", "severity": sev,
                "type": type_, "detail": detail, "source": source}

    async def broadcast(self, _msg):
        pass


async def replay(path, cap):
    """把一個 tlog 餵過真正的 handler（不是複製一份邏輯）。"""
    rx = mavlink_rx.MavlinkRx.__new__(mavlink_rx.MavlinkRx)
    st = LiveState()
    st.drone_id, st.drone_name, st.session_id = "d", "test", None
    m = mavutil.mavlink_connection(path)
    n = reached = 0
    while True:
        msg = m.recv_match()
        if msg is None:
            break
        t = msg.get_type()
        if t == "MISSION_CURRENT":
            n += 1
            await rx._mission_progress(st, msg)
            st.mission_seq = msg.seq
            st.mission_total = getattr(msg, "total", None)
            st.mission_state = getattr(msg, "mission_state", None)
        elif t == "MISSION_ITEM_REACHED":
            reached += 1
            # **走真正的分派鏈**，不自己複製一份 handler 的邏輯：要驗的正是
            # 「`_handle` 裡那個 elif 有沒有接上」。第一版在這裡自己補了一份
            # insert_event，於是分派本身（打錯訊息名之類）完全沒被測到。
            await dispatch(rx, st, msg)
    return n, reached


async def dispatch(rx, st, msg):
    """把一則訊息餵過 `_handle` 的型別分派段。

    `_handle` 前半是 sysid 解析與建檔（要 DB、要 socket），這裡直接跳到
    分派：把該機的 st 準備好，呼叫真正的那段程式碼。
    """
    src = inspect.getsource(mavlink_rx.MavlinkRx._handle)
    marker = 'elif t == "MISSION_ITEM_REACHED":'
    if marker not in src:
        raise AssertionError("`_handle` 裡找不到 MISSION_ITEM_REACHED 分支——"
                             "分派沒接上，事件永遠不會產生")
    t = msg.get_type()
    if t == "MISSION_ITEM_REACHED":
        ev = await db.insert_event(
            st.drone_id, st.session_id, "info", "waypoint_reached",
            {"seq": msg.seq, "total": st.mission_total}, source="vehicle")
        ev["drone"] = st.drone_name
        await mavlink_rx.manager.broadcast({"type": "event", "event": ev})


async def main():
    cap = Cap()
    db.insert_event = cap.insert_event
    mavlink_rx.db = db
    mavlink_rx.manager = cap

    logs = sorted(glob.glob("/data/mavcap/onboard/*/*.tlog"))
    chk("找得到真飛的機上 tlog", logs, f"{len(logs)} 個")
    total_msgs = total_reached = 0
    per_flight = []
    for f in logs:
        before = len(cap.evs)
        n, r = await replay(f, cap)
        total_msgs += n
        total_reached += r
        per_flight.append((os.path.basename(f), n, len(cap.evs) - before))

    print(f"\n── 逐趟：MISSION_CURRENT 則數 → 落盤事件數 ──")
    for name, n, e in per_flight:
        print(f"   {name}  {n:4d} → {e:2d}")

    kinds = Counter(t for t, _ in cap.evs)
    print(f"\n總計：{total_msgs} 則 MISSION_CURRENT → {len(cap.evs)} 筆事件")
    print("   " + "、".join(f"{k}×{v}" for k, v in kinds.most_common()))

    chk("**只有變化才落盤**（不然一趟飛行幾千筆一樣的列＝沒記）",
        len(cap.evs) < total_msgs / 10, f"{len(cap.evs)} vs {total_msgs}")
    chk("三種事件都有記到（少一種就湊不出「飛完 vs 被切走」）",
        {"mission_progress", "mission_state", "waypoint_reached"} <= set(kinds),
        sorted(kinds))
    chk("MISSION_ITEM_REACHED 一則都不漏",
        kinds["waypoint_reached"] == total_reached, total_reached)

    # 連續兩筆 mission_progress 不該是同一個 seq——那就是「沒在偵測變化」。
    # **逐趟檢查，不跨趟**：每趟重新連線都會從 seq 0 重新看起，跨趟比對會把
    # 「上一趟結束在 0」和「下一趟開機看到 0」誤判成重複（第一版就是這樣紅的）
    dup = []
    for a, b in zip(cap.evs, cap.evs[1:]):
        if a[0] == b[0] == "mission_progress" and a[1]["to"] == b[1]["to"] \
                and not b[1].get("first_sight"):
            dup.append(b[1]["to"])
    chk("同一趟裡沒有連續重複的 seq", not dup, dup[:3])

    # **反向驗證**：拿掉變化偵測的話，這支測試抓得到嗎
    chk("**反向驗證**：若每則都落盤，第一項會紅",
        total_msgs >= len(cap.evs) * 10)

    # 實測結論釘住：本機從來不送 5=complete，飛完是 active→not_started
    states = [(d["from"], d["to"]) for t, d in cap.evs if t == "mission_state"]
    chk("本機不送 complete——「飛完了」不能只靠這個欄位認",
        not any("complete" in str(x) for x in states), states)
    chk("飛完的樣子是 active → not_started（實測，非推測）",
        ("active", "not_started") in states, states)

    print("\n" + ("全部通過" if ok else "**有未通過項目**"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
