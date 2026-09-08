#!/usr/bin/env python3
"""錄製起訖條件的離線驗證（flight-video-design §8c）。

**不需要服務、不需要飛機、不需要資料庫**：把 `_landed_transition` 的資料庫
呼叫換成記錄用的假物件，直接餵一串 `landed_state` 進去，檢查

  * 曾離地的記憶（三個非 on_ground 的值都算）；
  * 落地滿 N 秒才停錄，而且**只停一次**；
  * arm 之後、起飛之前不會停錄——那是這條規則最容易寫錯的地方；
  * 從沒收到過 landed_state 時，**不得**被判成「沒飛過」。

跑法：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-record-window.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/srv")

from app import video_rec                      # noqa: E402
from app.config import settings                # noqa: E402
from app.mavlink_rx import MavlinkRx            # noqa: E402
from app.state import LiveState                # noqa: E402

CALLS: list[tuple] = []


class FakeDB:
    @staticmethod
    async def mark_airborne(sid, first):
        CALLS.append(("airborne_from" if first else "airborne_to", sid))

    @staticmethod
    async def mark_landed_seen(sid):
        CALLS.append(("seen", sid))


async def feed(st: LiveState, seq: list[str]) -> None:
    """把一串 landed_state 餵進真正的 `_landed_transition`。"""
    for ls in seq:
        st.landed_state = ls
        await MavlinkRx._landed_transition(None, st)    # self 用不到


def new_state() -> LiveState:
    st = LiveState()
    st.drone_id = "d"
    st.session_id = "s"
    st.sysid = 1
    return st


def main() -> int:
    video_rec.db = FakeDB                    # type: ignore[assignment]
    import app.mavlink_rx as rx
    rx.db = FakeDB                           # type: ignore[assignment]
    N = settings.video_landed_stop_s
    ok = True

    def chk(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"{'✓' if good else '✗'} {label}: {got}" + ("" if good else f"（期望 {want}）"))

    # ① arm 之後、起飛之前：一直在地上，**不准停錄**
    st = new_state()
    asyncio.run(feed(st, ["on_ground"] * 40))
    chk("起飛前不停錄（就算在地上很久）",
        video_rec.should_stop_for_landing(st, (st.on_ground_since or 0) + N + 99), False)
    chk("起飛前 airborne_seen 仍是 False", st.airborne_seen, False)
    chk("收到過 landed_state", st.landed_state_seen, True)

    # ② 完整一趟：on_ground → takeoff → in_air → landing → on_ground
    st = new_state(); CALLS.clear()
    asyncio.run(feed(st, ["on_ground", "takeoff", "in_air", "in_air", "landing"]))
    chk("離地被記住", st.airborne_seen, True)
    chk("起點只寫一次", [c[0] for c in CALLS].count("airborne_from"), 1)
    asyncio.run(feed(st, ["on_ground"]))
    chk("落地寫終點", [c[0] for c in CALLS].count("airborne_to"), 1)
    t = st.on_ground_since or 0
    chk(f"落地未滿 {N:.0f} 秒不停", video_rec.should_stop_for_landing(st, t + N - 0.1), False)
    chk(f"落地滿 {N:.0f} 秒才停", video_rec.should_stop_for_landing(st, t + N), True)
    st.landed_stopped = True
    chk("停過就不再停", video_rec.should_stop_for_landing(st, t + N + 60), False)

    # ③ 短跳：只有 takeoff，沒有 in_air（實測 20260902-081800.tlog）
    st = new_state()
    asyncio.run(feed(st, ["on_ground", "takeoff", "on_ground"]))
    chk("短跳也算飛過（只有 takeoff）", st.airborne_seen, True)

    # ④ 落地後又起飛：終點要更新，而且錄影還沒停就不該被當成停過
    st = new_state(); CALLS.clear()
    asyncio.run(feed(st, ["in_air", "on_ground", "in_air", "on_ground"]))
    chk("多次起降記兩次終點", [c[0] for c in CALLS].count("airborne_to"), 2)

    # ⑤ 完全沒收到 landed_state：不知道，**不得**被當成沒飛過
    st = new_state()
    chk("沒收到過就是不知道", (st.landed_state_seen, st.airborne_seen), (False, False))
    chk("不知道時也不會停錄", video_rec.should_stop_for_landing(st, 1e9), False)

    print("全部通過" if ok else "有項目不通過")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
