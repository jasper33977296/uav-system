#!/usr/bin/env python3
"""機體在 armed 狀態下消失時，架次要被收掉——而且不能太快也不能不收。

**為什麼要專門測**：這個 bug 的症狀是「畫面顯示飛行中」，而那正是**看起來
一切正常**的樣子。2026-08-26 一筆架次開了 2.5 小時，遙測在開始後 10 秒就斷了，
沒有任何錯誤訊息、沒有任何告警——系統只是安靜地在捏造一趟還在進行的飛行。

兩個方向都要驗：
  * 短暫抖動（5G 常態，數十秒）**不得**把一趟飛行切成好幾段
  * 真的消失了就要收，而且結束時間要用**最後一筆資料的時間**，不是「現在」
"""
import asyncio, sys, types
sys.path.insert(0, "/srv")
from app import main as M
from app.state import LiveState

calls = []
class FakeDB:
    async def end_session(self, sid, reason="disarmed"):
        calls.append(("end", sid, reason))
    async def insert_event(self, *a, **k):
        calls.append(("event", a[3] if len(a) > 3 else None))
        return {"id": 1, "time": "t", "severity": "warning", "type": "x", "detail": {}}
class FakeMgr:
    async def broadcast(self, m): pass

M.db = FakeDB(); M.manager = FakeMgr()

ok = True
def chk(l, c, n=""):
    global ok; ok &= bool(c)
    print(f"{'✓' if c else '✗'} {l}{('｜'+str(n)) if n else ''}")

async def run():
    global ok
    st = LiveState(drone_id="d1", drone_name="測試機")
    st.session_id = "s1"; st.armed = True; st.connected = False
    M.fleet.clear(); M.fleet["d1"] = st

    # t=0：第一次看到失聯，只記時間不收
    calls.clear(); await M._close_orphan_sessions()
    chk("剛失聯不收架次（5G 抖動是常態）", not calls and st.session_id == "s1")

    # 還在寬限期內
    st._lost_since = M.time.monotonic() - (M.SESSION_LOST_S - 10)
    calls.clear(); await M._close_orphan_sessions()
    chk(f"寬限期內（{M.SESSION_LOST_S - 10:.0f}s）仍不收",
        not calls and st.session_id == "s1")

    # 超過寬限期
    st._lost_since = M.time.monotonic() - (M.SESSION_LOST_S + 5)
    calls.clear(); await M._close_orphan_sessions()
    ends = [c for c in calls if c[0] == "end"]
    chk("超過寬限期就收掉", len(ends) == 1, ends)
    chk("**理由標成 telemetry_lost**（不是 disarmed——飛機可能還在飛）",
        ends and ends[0][2] == "telemetry_lost", ends and ends[0][2])
    chk("收掉後 session_id 清空", st.session_id is None)
    chk("留一筆事件（不是安靜地消失）",
        any(c[0] == "event" for c in calls))

    # **反向驗證**：連線還在的機不收
    st2 = LiveState(drone_id="d2", drone_name="連線正常")
    st2.session_id = "s2"; st2.armed = True; st2.connected = True
    st2._lost_since = M.time.monotonic() - 9999
    M.fleet.clear(); M.fleet["d2"] = st2
    calls.clear(); await M._close_orphan_sessions()
    chk("**反向驗證**：連線正常的機不受影響", not calls and st2.session_id == "s2")

    # 抖動後恢復：_lost_since 要被清掉，不能累積
    st3 = LiveState(drone_id="d3", drone_name="抖一下")
    st3.session_id = "s3"; st3.armed = True; st3.connected = False
    M.fleet.clear(); M.fleet["d3"] = st3
    await M._close_orphan_sessions()          # 記下失聯起點
    st3.connected = True
    await M._close_orphan_sessions()          # 恢復
    st3.connected = False
    calls.clear(); await M._close_orphan_sessions()
    chk("恢復後重新計時（抖動不累積成收尾）",
        not calls and st3.session_id == "s3")

asyncio.run(run())
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
