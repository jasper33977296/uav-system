#!/usr/bin/env python3
"""對外即時串流的判斷邏輯（doc/external-live-api.md）。不連資料庫、不碰飛機。

用法（用 backend 映像、掛上工作樹的原始碼）：
  docker run --rm -i -v "$PWD/apps/backend/app:/srv/app:ro" <backend 映像> python - < scripts/test-ext-stream.py
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

from app import ext_stream as es
from app.state import LiveState, fleet

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


# ── 預計航線：0914-square-test-v5 的實際航點 ───────────────────────
rows = [
    {"seq": 0, "lat": 0, "lon": 0, "alt": 3.1, "action": "takeoff", "params": {"frame": 3}},
    {"seq": 1, "lat": 0, "lon": 0, "alt": 0, "action": "do", "params": {"frame": 2}},
    {"seq": 2, "lat": 24.773281, "lon": 121.046003, "alt": 3.5, "action": "waypoint", "params": {"frame": 3}},
    {"seq": 3, "lat": 0, "lon": 0, "alt": 0, "action": "do", "params": {"frame": 2}},
    {"seq": 4, "lat": 24.773269, "lon": 121.045950, "alt": 4, "action": "waypoint", "params": {"frame": 3}},
    {"seq": 5, "lat": 0, "lon": 0, "alt": 0, "action": "do", "params": {"frame": 2}},
    {"seq": 6, "lat": 24.773324, "lon": 121.045854, "alt": 0, "action": "land", "params": {"frame": 3}},
]
h, gj = es.route_geojson(rows, [24.773359, 121.045863, 119.0])
line = gj["features"][0]["geometry"]["coordinates"]
chk("home 帶海拔", h == {"lat": 24.773359, "lon": 121.045863, "alt_msl": 119.0}, h)
chk("航線從起飛點地面開始", line[0] == [121.045863, 24.773359, 0.0], line[0])
chk("起飛項畫在起飛點正上方", line[1] == [121.045863, 24.773359, 3.1], line[1])
chk("降落先平飛到那一點再垂直往下",
    line[-2] == [121.045854, 24.773324, 4.0] and line[-1] == [121.045854, 24.773324, 0.0], line[-2:])
pts = gj["features"][1:]
chk("點的種類", [f["properties"]["kind"] for f in pts] == ["takeoff", "waypoint", "waypoint", "land"])
chk("點用路徑自己的序號（含非導航項）", [f["properties"]["seq"] for f in pts] == [0, 2, 4, 6])
_, gj2 = es.route_geojson(
    [{"seq": 0, "lat": 1.0, "lon": 2.0, "alt": 5, "action": "waypoint", "params": {"frame": 10}}],
    [1.0, 2.0, 0])
chk("離地高度（frame 10）換不出離起飛點高度就不給", gj2["features"][-1]["geometry"]["coordinates"] == [2.0, 1.0])

# ── 實際軌跡 ─────────────────────────────────────────────────────
t0 = datetime(2026, 9, 14, tzinfo=timezone.utc)
lines = es.track_lines([{"time": t0 + timedelta(seconds=i), "lat": 1.0, "lon": 2.0, "alt_rel": 1.0}
                        for i in (0, 1, 2, 20, 21, 40)])
chk("斷超過 10 秒分段，單點畫不成線就不送", [len(x) for x in lines] == [3, 2], [len(x) for x in lines])


# ── 資料新舊 ─────────────────────────────────────────────────────
def mk(age, **kw):
    st = LiveState(drone_id="d1", drone_name="x", sysid=1, lat=24.7, lon=121.0,
                   alt_rel=5.0, alt_msl=130.0, armed=True, landed_state="in_air",
                   flight_mode="AUTO", autopilot_raw=3, battery_pct=70.0, **kw)
    st.telem_seen_mono = None if age is None else time.monotonic() - age
    st.connected = age is not None and age < 5
    return st


d = es.Drone("d1")
for age, want in ((0.5, "live"), (5, "stale"), (15, "old"), (None, "never")):
    got = es.drone_state(d, mk(age))["freshness"]
    chk(f"遙測 {age} 秒前 → {want}", got == want, got)
old = es.drone_state(d, mk(15))
chk("old：位置、解鎖、電量都不給數字",
    old["position"] is None and old["armed"] is None and old["battery"] is None)
chk("old：最後已知位置放 last_known", old["last_known"]["lat"] == 24.7 and old["last_known"]["at"])
chk("never：link 仍是固定形狀", {"state", "age_s", "rsrp", "throughput_down_kbps"} <= set(es.drone_state(d, None)["link"]))

# ── 任務進度 ─────────────────────────────────────────────────────
d, st = es.Drone("d1"), mk(0.5)
st.mission_seq, st.mission_total, st.mission_state = 7, 8, 3
p = es.drone_state(d, st)["mission_progress"]
chk("ArduPilot 的序號減一（home 佔 seq 0）", p == {"current": 6, "total": 7, "state": "active"}, p)
st.mission_seq, st.mission_state = 1, 2
p = es.drone_state(d, st)["mission_progress"]
chk("飛完回報第 1 項時停在最後一項", p["current"] == 6 and p["state"] == "not_started", p)


# ── 事件與結束原因 ─────────────────────────────────────────────────
def stream(external=True, phase="active"):
    s = es.Stream("11111111-1111-1111-1111-111111111111")
    s.phase, s.external = phase, external
    s.drone("d1").name = "x"
    es.streams[s.id] = s
    es._by_drone.clear()
    es._index(s)
    return s


def events(s, kind):
    return [m for _, m in s.buf if m["type"] == "event" and m["kind"] == kind]


s, st = stream(), mk(0.5)
fleet["d1"] = st
es.on_statustext(st, "critical", "Crash: Disarming: AngErr=151>30")
st.armed = False
es.on_disarmed(st)
chk("墜機文字之後上鎖 → crash", s.drones["d1"].reason == "crash" and events(s, "crash"))
s, st = stream(), mk(0.5, )
st.landed_state, st.armed = "on_ground", False
es.on_disarmed(st)
chk("著地後上鎖 → landed", s.drones["d1"].reason == "landed")
s, st = stream(), mk(0.5)
es.on_disarmed(st)
chk("上鎖時沒有著地訊號 → disarmed_in_air", s.drones["d1"].reason == "disarmed_in_air")

s, st = stream(), mk(0.5)
for _ in range(3):
    es.on_statustext(st, "warning", "PreArm: Battery 1 low voltage failsafe")
chk("同一句 30 秒內只送一則", len(events(s, "vehicle_text")) == 1)
s.drones["d1"].texts["PreArm: Battery 1 low voltage failsafe"][0] -= 31
es.on_statustext(st, "warning", "PreArm: Battery 1 low voltage failsafe")
ev = events(s, "vehicle_text")
chk("過了 30 秒再送，count 帶上累積次數", len(ev) == 2 and ev[-1]["detail"]["count"] == 3, ev[-1]["detail"])
es.on_statustext(st, "info", "Mission: 2 WP")
chk("info 等級的文字不轉", len(events(s, "vehicle_text")) == 2)

s, st = stream(), mk(0.5)
fleet["d1"] = st
es.on_db_event("d1", "info", "waypoint_reached", {"seq": 3, "total": 8})
ev = events(s, "waypoint_reached")
chk("到達航點換成路徑序號", ev and ev[-1]["detail"] == {"seq": 2, "total": 7} and "第 2 項" in ev[-1]["text"])
es.on_db_event("d1", "info", "statustext", {"text": "x"})
chk("不在清單裡的事件不轉", len([m for _, m in s.buf if m["type"] == "event"]) == 1)

s, st = stream(), mk(0.5)
st.landed_state = "on_ground"
es.on_landed(st, "landing")
st.landed_state = "takeoff"
es.on_landed(st, "on_ground")
chk("著地與離地事件", events(s, "landed") and events(s, "takeoff") and s.drones["d1"].landed_at)

s, st = stream(phase="starting"), mk(0.5)
es.on_armed(st)
chk("解鎖就進 active", s.phase == "active" and s.started_at and events(s, "armed"))


# ── 結束規則 ─────────────────────────────────────────────────────
class FakePool:
    def __init__(self):
        self.executed = []

    async def execute(self, q, *a):
        self.executed.append(q)

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None


es.db.pool = FakePool()


async def end_rule():
    s, st = stream(), mk(0.5)
    s.in_db, st.armed = True, False
    fleet["d1"] = st
    s.drones["d1"].reason = "landed"
    t = time.monotonic()
    await es.tick(s, t)
    await es.tick(s, t + 2.0)
    a = s.phase
    st.armed = True
    await es.tick(s, t + 2.5)
    b = (s.phase, s.end_at)
    st.armed = False
    await es.tick(s, t + 3.0)
    await es.tick(s, t + 5.9)
    c = s.phase
    await es.tick(s, t + 6.1)
    return a, b, c, s


a, b, c, s = asyncio.run(end_rule())
chk("上鎖 2 秒還不結束", a == "active")
chk("3 秒內又解鎖就取消", b == ("active", None), b)
chk("再上鎖滿 3 秒才結束", c == "active" and s.phase == "ended", (c, s.phase))
chk("外部任務結束時寫回 ended_at", any("ended_at = now()" in q for q in es.db.pool.executed))
ended = [m for _, m in s.buf if m["type"] == "ended"]
chk("ended 帶每一台的原因", ended and ended[0]["drones"][0]["reason"] == "landed")


async def hold(external, starting):
    s, st = stream(external=external), mk(0.5)
    s.in_db, st.armed = True, False
    fleet["d1"] = st
    s.drones["d1"].reason = "landed"
    if starting:
        s.drones["d1"].starting_since = time.monotonic()
    t = time.monotonic()
    for k in range(10):
        await es.tick(s, t + k)
    return s.phase


chk("起飛流程還在跑就不結束", asyncio.run(hold(True, True)) == "active")
chk("畫面建立的任務不自動結束", asyncio.run(hold(False, False)) == "active")


async def waiting():
    s = es.Stream("22222222-2222-2222-2222-222222222222")
    es.streams[s.id] = s
    await es.tick(s, s.created + 0.5)
    first = [m for _, m in s.buf if m["type"] == "state"]
    await es.tick(s, s.created + 30.5)
    return first, s


first, s = asyncio.run(waiting())
chk("等待中也每拍送 state", first and first[0]["phase"] == "waiting" and first[0]["drones"] == [])
e = [m for _, m in s.buf if m["type"] == "ended"]
chk("30 秒沒人起飛 → never_started 並附說明", s.phase == "ended" and e and e[0]["reason"] == "never_started" and e[0]["msg"])

# ── 連線佇列與序號 ─────────────────────────────────────────────────
c = es.Client(None)
for i in range(es.CLIENT_MAX):
    c.push({"type": "state", "seq": i})
c.push({"type": "event", "seq": 900})
c.push({"type": "state", "seq": 901})
chk("佇列滿了丟最舊的 state、事件不丟",
    c.q[0]["seq"] == 1 and c.q[-1].get("dropped") == 1 and any(m["seq"] == 900 for m in c.q))
s1 = es.Stream("33333333-3333-3333-3333-333333333333")
s1.publish("state", {})
time.sleep(0.003)
s2 = es.Stream(s1.id)
chk("重建的串流序號仍大於之前送出的", s2.seq > s1.seq, (s1.seq, s2.seq))

print("\n全部通過" if ok else "\n有項目沒過")
raise SystemExit(0 if ok else 1)
