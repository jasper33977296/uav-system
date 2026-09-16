"""對外即時串流（doc/external-live-api.md）：一個任務一條 WebSocket，每 0.5 秒送狀態。

控制端用自己產生的 UUID 連上 `/ws/v1/missions/{uuid}`，再帶著同一個編號呼叫
command 的 `/api/start`；command 建立任務並把起飛流程每一步 POST 到
`/api/ext/missions/{id}/notify`。即時狀態讀 `fleet`，事件由 mavlink_rx／main
的鉤子與 `db.insert_event` 的旁聽者送進來。
"""
import asyncio
import functools
import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import db
from .dialect import get_driver
from .jsonsafe import json_safe
from .state import MISSION_STATE, LiveState, fleet

log = logging.getLogger("ext_stream")
router = APIRouter(prefix="/api")

TICK_S = 0.5
SYNC_S = 1.0
WAIT_S = 30.0
END_AFTER_S = 3.0
BUFFER_S = 60.0
KEEP_ENDED_S = 30.0
#: 畫面建立的任務不會自己結束，沒有人連的串流放著只是佔記憶體
IDLE_DROP_S = 60.0
TRACK_GAP_S = 10.0
TEXT_FOLD_S = 30.0
#: 飛控先送 `Crash: Disarming` 再上鎖，兩者相隔不到一秒；留寬一點
CRASH_WINDOW_S = 10.0
#: command 沒送完成通知（例如服務重啟）時，不能讓「起飛中」永遠擋住結束
START_STALE_S = 300.0
CLIENT_MAX = 240

TW = timezone(timedelta(hours=8))
LINK_KEYS = ("time", "rsrp", "rsrq", "sinr", "cqi", "pci", "cell_id",
             "band", "nr_mode", "rtt_ms", "jitter_ms", "packet_loss_pct",
             "throughput_up_kbps", "throughput_down_kbps")
_PROGRESS_STATES = ("not_started", "active", "paused", "complete")
_BLANK = {"position": None, "last_known": None, "heading": None,
          "ground_speed": None, "vertical_speed": None, "flight_mode": None,
          "mode_verb": None, "armed": None, "landed": None,
          "mission_progress": None, "battery": None, "gps": None}
REASON_TEXT = {"landed": "著地後上鎖", "crash": "飛控判定墜機而切斷馬達",
               "disarmed_in_air": "上鎖時沒有著地訊號", "telemetry_lost": "遙測中斷 90 秒",
               "start_failed": "起飛流程被拒", "aborted": "群飛全撤"}
STEP_TEXT = {"begin": "起飛流程開始", "upload": "上傳完成", "takeoff": "已送出起飛",
             "airborne": "已離地", "mission": "開始飛路徑",
             "uploading": "上傳中", "uploaded": "上傳完成", "arming": "解鎖中",
             "armed": "已解鎖", "starting": "起飛中", "flying": "開始飛路徑",
             "rtl": "返航", "abort": "群飛全撤"}


def _iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_ago(seconds: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _severity(s: str | None) -> str:
    if s in ("critical", "alert", "emergency"):
        return "critical"
    if s in ("warning", "warn", "error"):
        return "warning"
    return "info"


def _safe(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            log.exception("對外串流 %s 失敗（不影響資料路徑）", fn.__name__)
    return wrap


class Client:
    """一條連線自己的送出佇列。慢的控制端只丟自己的 state，不拖慢別人。"""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.q: deque = deque()
        self.wake = asyncio.Event()
        self.dropped = 0

    def push(self, msg: dict) -> None:
        if msg.get("type") == "state":
            if len(self.q) >= CLIENT_MAX:
                for i, m in enumerate(self.q):
                    if m.get("type") == "state":
                        del self.q[i]
                        self.dropped += 1
                        break
            if self.dropped:
                msg = {**msg, "dropped": self.dropped}
                self.dropped = 0
        self.q.append(msg)
        self.wake.set()

    def close(self, code: int) -> None:
        self.q.append({"_close": code})
        self.wake.set()

    async def run(self) -> None:
        try:
            while True:
                while self.q:
                    m = self.q.popleft()
                    if "_close" in m:
                        await self.ws.close(code=m["_close"])
                        return
                    await self.ws.send_text(
                        json.dumps(json_safe(m), ensure_ascii=False, default=str))
                self.wake.clear()
                await self.wake.wait()
        except Exception:
            pass


class Drone:
    def __init__(self, drone_id: str):
        self.drone_id = drone_id
        self.name: str | None = None
        self.sysid: int | None = None
        self.plan_id: str | None = None
        self.db_plan: str | None = None
        self.db_seen = False
        self.route_plan: str | None = None
        self.starting_since: float | None = None
        self.reason: str | None = None
        self.aborted = False
        self.landed_at: str | None = None
        self.disarmed_at: str | None = None
        self.crash_at: float | None = None
        self.progress: int | None = None
        self.texts: dict = {}


class Stream:
    def __init__(self, mission_id: str):
        self.id = mission_id
        self.name: str | None = None
        self.external = False
        self.in_db = False
        self.phase = "waiting"
        self.started_at: str | None = None
        self.created = time.monotonic()
        self.ended_mono: float | None = None
        self.end_at: float | None = None
        self.last_client = time.monotonic()
        # 以毫秒時間起算：backend 重啟後重建的串流，序號仍大於重啟前送出去的
        self.seq = int(time.time() * 1000)
        self.buf: deque = deque()
        self.clients: set[Client] = set()
        self.drones: dict[str, Drone] = {}

    def drone(self, drone_id: str) -> Drone:
        d = self.drones.get(drone_id)
        if d is None:
            d = self.drones[drone_id] = Drone(drone_id)
        return d

    def publish(self, type_: str, body: dict) -> dict:
        self.seq += 1
        msg = {"v": 1, "type": type_, "mission_id": self.id, "seq": self.seq,
               "ts": _iso(), **body}
        now = time.monotonic()
        self.buf.append((now, msg))
        if self.phase != "ended":
            while self.buf and now - self.buf[0][0] > BUFFER_S:
                self.buf.popleft()
        for c in list(self.clients):
            c.push(msg)
        return msg

    def envelope(self, type_: str, body: dict) -> dict:
        """只屬於一條連線的訊息（hello、連線時補的 route／track）：不佔序號、不進緩衝。"""
        return {"v": 1, "type": type_, "mission_id": self.id, "ts": _iso(), **body}


streams: dict[str, Stream] = {}
_by_drone: dict[str, Stream] = {}


def _index(s: Stream) -> None:
    if s.phase in ("starting", "active"):
        for did in s.drones:
            _by_drone[did] = s


def _hit(drone_id: str | None) -> tuple[Stream, Drone] | None:
    s = _by_drone.get(drone_id) if drone_id else None
    if s is None or s.phase not in ("starting", "active") or drone_id not in s.drones:
        return None
    return s, s.drones[drone_id]


def _event(s: Stream, d: Drone, kind: str, severity: str, text: str,
           detail: dict | None = None) -> None:
    s.publish("event", {"drone_id": d.drone_id, "kind": kind, "severity": severity,
                        "text": text, "detail": detail or {}})


# ── 序號換算：route 用路徑自己的序號，飛控的序號要對回來 ─────────────
def _offset(st: LiveState | None) -> int:
    return 1 if st is not None and get_driver(st.autopilot_raw).home_at_seq0 else 0


def _plan_seq(st: LiveState | None, v) -> int | None:
    if v is None:
        return None
    r = int(v) - _offset(st)
    return r if r >= 0 else None


def _plan_total(st: LiveState | None, v) -> int | None:
    if not v:
        return None
    return max(int(v) - _offset(st), 0)


# ── 每 0.5 秒的狀態 ────────────────────────────────────────────────
def _link(st: LiveState | None) -> dict:
    if st is None:
        return {"state": "unknown", "age_s": None, **{k: None for k in LINK_KEYS}}
    lk = st.link or {}
    return {"state": st._link_state_now(), "age_s": st.link_age_s,
            **{k: lk.get(k) for k in LINK_KEYS}}


def _progress(d: Drone, st: LiveState) -> dict:
    cur = _plan_seq(st, st.mission_seq)
    state = MISSION_STATE.get(st.mission_state)
    state = state if state in _PROGRESS_STATES else None
    # 飛控跑完降落會回報第 1 項；不是重來，停在最後一項
    if cur is not None and (state == "active" or d.progress is None or cur > d.progress):
        d.progress = cur
    return {"current": d.progress, "total": _plan_total(st, st.mission_total), "state": state}


def drone_state(d: Drone, st: LiveState | None) -> dict:
    out = {"drone_id": d.drone_id, "name": d.name or (st.drone_name if st else None),
           "sysid": d.sysid if d.sysid is not None else (st.sysid if st else None)}
    age = st.telem_age_s if st is not None else None
    if age is None:
        return {**out, "freshness": "never", "age_s": None, "connected": False,
                **_BLANK, "link": _link(st)}
    if age > 10:
        # 舊到這個程度就不給數字：標著「舊」的座標仍然會被畫成「飛機在這裡」
        return {**out, "freshness": "old", "age_s": age, "connected": st.connected,
                **_BLANK,
                "last_known": {"lat": st.lat, "lon": st.lon, "alt_rel": st.alt_rel,
                               "at": _iso_ago(age)},
                "link": _link(st)}
    return {**out, "freshness": "live" if age < 2 else "stale", "age_s": age,
            "connected": st.connected,
            "position": {"lat": st.lat, "lon": st.lon,
                         "alt_rel": st.alt_rel, "alt_msl": st.alt_msl},
            "last_known": None,
            "heading": st.heading, "ground_speed": st.ground_speed,
            "vertical_speed": st.vertical_speed,
            "flight_mode": st.flight_mode, "mode_verb": st.mode_verb,
            "armed": st.armed, "landed": st.landed_state,
            "mission_progress": _progress(d, st),
            "battery": {"pct": st.battery_pct, "voltage": st.battery_voltage},
            "gps": {"fix": st.gps_fix, "sats": st.satellites},
            "link": _link(st)}


# ── 預計航線與實際軌跡 ────────────────────────────────────────────
def _alt_rel(alt, frame, home_msl) -> float | None:
    if alt is None:
        return None
    if frame in (None, 3, 6):
        return float(alt)
    if frame in (0, 5) and home_msl is not None:
        return float(alt) - float(home_msl)
    return None          # 離地高度（frame 10）要地形資料才換得出來


def _pos(lon, lat, alt) -> list:
    return [lon, lat] if alt is None else [lon, lat, round(alt, 2)]


def _point(lon, lat, alt, seq, kind) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": _pos(lon, lat, alt)},
            "properties": {"role": "waypoint", "seq": seq, "kind": kind}}


def route_geojson(rows: list[dict], home) -> tuple[dict | None, dict]:
    h = None
    if isinstance(home, (list, tuple)) and len(home) >= 2 and (home[0] or home[1]):
        h = {"lat": home[0], "lon": home[1], "alt_msl": home[2] if len(home) > 2 else None}
    elif isinstance(home, dict) and home.get("lat") is not None:
        h = {"lat": home["lat"], "lon": home["lon"], "alt_msl": home.get("alt")}
    if h is None:
        f = next((w for w in rows if w.get("lat") or w.get("lon")), None)
        if f:
            h = {"lat": f["lat"], "lon": f["lon"], "alt_msl": None}
    line, feats, prev = [], [], 0.0
    if h:
        line.append(_pos(h["lon"], h["lat"], 0.0))
    for w in rows:
        p = w.get("params") or {}
        if isinstance(p, str):
            p = json.loads(p)
        act = w.get("action") or "waypoint"
        has = bool(w.get("lat") or w.get("lon"))
        alt = _alt_rel(w.get("alt"), p.get("frame"), h["alt_msl"] if h else None)
        if act == "rtl":
            if h:
                line += [_pos(h["lon"], h["lat"], prev), _pos(h["lon"], h["lat"], 0.0)]
                feats.append(_point(h["lon"], h["lat"], 0.0, w["seq"], "rtl"))
                prev = 0.0
            continue
        if act == "takeoff":
            lat, lon = ((w["lat"], w["lon"]) if has else
                        ((h["lat"], h["lon"]) if h else (None, None)))
            if lat is None:
                continue
            line.append(_pos(lon, lat, alt))
            feats.append(_point(lon, lat, alt, w["seq"], "takeoff"))
        elif not has:
            continue
        elif act == "land":
            # 降落項：先平飛到那一點，再垂直往下
            line += [_pos(w["lon"], w["lat"], prev), _pos(w["lon"], w["lat"], 0.0)]
            feats.append(_point(w["lon"], w["lat"], 0.0, w["seq"], "land"))
            prev = 0.0
            continue
        else:
            line.append(_pos(w["lon"], w["lat"], alt))
            feats.append(_point(w["lon"], w["lat"], alt, w["seq"], "waypoint"))
        if alt is not None:
            prev = alt
    path = ([{"type": "Feature", "geometry": {"type": "LineString", "coordinates": line},
              "properties": {"role": "planned_path"}}] if len(line) >= 2 else [])
    return h, {"type": "FeatureCollection", "features": path + feats}


def track_lines(rows: list[dict]) -> list[list]:
    lines, cur, last = [], [], None
    for r in rows:
        if last is not None and (r["time"] - last).total_seconds() > TRACK_GAP_S and cur:
            lines.append(cur)
            cur = []
        cur.append(_pos(r["lon"], r["lat"], r["alt_rel"]))
        last = r["time"]
    if cur:
        lines.append(cur)
    return [ln for ln in lines if len(ln) >= 2]


async def _route_body(d: Drone, reason: str) -> dict | None:
    if not d.plan_id:
        return None
    prow = await db.pool.fetchrow("SELECT name, home FROM plans WHERE id = $1::uuid", d.plan_id)
    if prow is None:
        return None
    rows = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1::uuid ORDER BY seq", d.plan_id)
    home = prow["home"]
    if isinstance(home, str):
        home = json.loads(home)
    h, gj = route_geojson([dict(r) for r in rows], home)
    return {"reason": reason, "drone_id": d.drone_id, "plan_id": d.plan_id,
            "plan_name": prow["name"], "home": h, "geojson": gj}


async def _track_body(s: Stream, d: Drone) -> dict:
    rows = await db.pool.fetch(
        """SELECT t.time, t.lat, t.lon, t.alt_rel FROM telemetry t
            WHERE t.drone_id = $2::uuid AND t.lat IS NOT NULL
              AND t.time >= (SELECT min(started_at) FROM flight_sessions WHERE mission_id = $1::uuid)
              AND t.session_id IN (SELECT id FROM flight_sessions
                                    WHERE mission_id = $1::uuid AND drone_id = $2::uuid)
            ORDER BY t.time""", s.id, d.drone_id)
    return {"drone_id": d.drone_id,
            "geojson": {"type": "Feature",
                        "geometry": {"type": "MultiLineString", "coordinates": track_lines(rows)},
                        "properties": {"from": _iso(rows[0]["time"]) if rows else None,
                                       "to": _iso(rows[-1]["time"]) if rows else None,
                                       "points": len(rows), "interval_s": 1}}}


async def _send_route(s: Stream, d: Drone, reason: str) -> None:
    body = await _route_body(d, reason)
    if body is None:
        return
    if reason == "change_route":
        _event(s, d, "route_changed", "warning", f"飛行中改航線：{body['plan_name']}",
               {"plan_id": d.plan_id})
    d.route_plan, d.progress = d.plan_id, None
    s.publish("route", body)


# ── 與資料庫對齊：任務是否存在、成員、機上路徑 ─────────────────────
async def _sync(s: Stream) -> None:
    row = await db.pool.fetchrow(
        "SELECT name, external, ended_at FROM missions WHERE id = $1::uuid", s.id)
    if row is None:
        return
    s.in_db, s.name, s.external = True, row["name"], row["external"]
    if s.phase == "waiting":
        s.phase = "starting"
    crew = await db.pool.fetch(
        """SELECT d.id::text AS id, d.name, d.mav_sysid, d.current_plan_id::text AS plan_id
             FROM drones d
            WHERE d.id IN (SELECT md.drone_id FROM mission_drones md WHERE md.mission_id = $1::uuid
                           UNION
                           SELECT sm.drone_id FROM squad_members sm
                             JOIN missions m ON m.squad_id = sm.squad_id WHERE m.id = $1::uuid
                           UNION
                           SELECT fs.drone_id FROM flight_sessions fs WHERE fs.mission_id = $1::uuid)""",
        s.id)
    flying = any((fleet.get(r["id"]) or LiveState()).armed for r in crew)
    for r in crew:
        d = s.drone(r["id"])
        d.name, d.sysid = r["name"], r["mav_sysid"]
        # 只看機上路徑有沒有「變」：起飛流程通知給的新路徑，上傳完成前資料庫還是舊的
        if not d.db_seen:
            d.db_seen, d.db_plan = True, r["plan_id"]
            if d.plan_id is None and r["plan_id"] and s.phase != "ended":
                d.plan_id = r["plan_id"]
                await _send_route(s, d, "initial")
        elif r["plan_id"] != d.db_plan:
            d.db_plan = r["plan_id"]
            if r["plan_id"] and r["plan_id"] != d.plan_id and s.phase != "ended":
                d.plan_id = r["plan_id"]
                await _send_route(s, d, "change_route" if flying and d.route_plan else "initial")
    if row["ended_at"] is not None and s.phase != "ended":
        await _finish(s)


async def _sync_all() -> None:
    for r in await db.pool.fetch(
            "SELECT id::text AS id FROM missions WHERE external AND ended_at IS NULL"):
        if r["id"] not in streams:
            streams[r["id"]] = Stream(r["id"])
    for s in list(streams.values()):
        if s.phase != "ended":
            await _sync(s)
    _by_drone.clear()
    for s in streams.values():
        _index(s)


async def _finish(s: Stream, reason: str | None = None, msg: str | None = None) -> None:
    if s.phase == "ended":
        return
    s.phase, s.ended_mono, s.end_at = "ended", time.monotonic(), None
    body: dict = {"drones": []}
    if s.in_db:
        sess: dict = {}
        try:
            if s.external:
                await db.pool.execute(
                    "UPDATE missions SET ended_at = now() WHERE id = $1::uuid AND ended_at IS NULL",
                    s.id)
            for r in await db.pool.fetch(
                    "SELECT drone_id::text AS drone_id, array_agg(id::text ORDER BY started_at) AS ids "
                    "FROM flight_sessions WHERE mission_id = $1::uuid GROUP BY drone_id", s.id):
                sess[r["drone_id"]] = list(r["ids"])
        except Exception:
            log.exception("任務 %s 收尾寫入失敗（照樣送 ended）", s.id)
        body["drones"] = [{"drone_id": d.drone_id, "reason": d.reason,
                           "session_ids": sess.get(d.drone_id, []),
                           "landed_at": d.landed_at, "disarmed_at": d.disarmed_at}
                          for d in s.drones.values()]
    if reason:
        body.update(reason=reason, msg=msg)
    s.publish("ended", body)
    for c in list(s.clients):
        c.close(1000)
    log.info("對外串流結束：%s（%s）", s.name or s.id, reason or "任務結束")


async def tick(s: Stream, now: float) -> None:
    if s.phase == "ended":
        if now - s.ended_mono > KEEP_ENDED_S:
            streams.pop(s.id, None)
        return
    if s.phase == "waiting":
        if now - s.created > WAIT_S:
            await _finish(s, "never_started",
                          f"連上後 {WAIT_S:.0f} 秒內沒有人用這個編號呼叫起飛")
            return
        s.publish("state", {"phase": "waiting", "drones": []})
        return
    if s.clients:
        s.last_client = now
    elif not s.external and now - s.last_client > IDLE_DROP_S:
        streams.pop(s.id, None)
        return
    armed, out = False, []
    for d in s.drones.values():
        st = fleet.get(d.drone_id)
        armed |= bool(st is not None and st.armed)
        if d.starting_since is not None and now - d.starting_since > START_STALE_S:
            d.starting_since = None
        out.append(drone_state(d, st))
    if armed and s.phase == "starting":
        s.phase, s.started_at = "active", _iso()
    s.publish("state", {"phase": s.phase, "drones": out})
    if not s.external:
        return
    ds = s.drones.values()
    done = (not armed and not any(d.starting_since is not None for d in ds)
            and any(d.reason for d in ds))
    if not done:
        s.end_at = None
    elif s.end_at is None:
        s.end_at = now + END_AFTER_S
    elif now >= s.end_at:
        await _finish(s)


async def run() -> None:
    last_sync = 0.0
    while True:
        await asyncio.sleep(TICK_S)
        now = time.monotonic()
        if now - last_sync >= SYNC_S:
            last_sync = now
            try:
                await _sync_all()
            except Exception:
                log.exception("對外串流同步失敗，下一輪再試")
        for s in list(streams.values()):
            try:
                await tick(s, now)
            except Exception:
                log.exception("對外串流 %s 這一輪失敗", s.id)


# ── 鉤子：mavlink_rx／main／db 在事情發生的當下呼叫 ─────────────────
@_safe
def on_armed(st: LiveState) -> None:
    hit = _hit(st.drone_id)
    if not hit:
        return
    s, d = hit
    d.reason = d.landed_at = d.disarmed_at = d.crash_at = None
    s.end_at = None
    if s.phase == "starting":
        s.phase, s.started_at = "active", _iso()
    _event(s, d, "armed", "info", "解鎖")


@_safe
def on_disarmed(st: LiveState) -> None:
    hit = _hit(st.drone_id)
    if not hit:
        return
    s, d = hit
    if d.crash_at is not None and time.monotonic() - d.crash_at < CRASH_WINDOW_S:
        reason = "crash"
    elif d.aborted:
        reason = "aborted"
    elif st.landed_state == "on_ground":
        reason = "landed"
    else:
        reason = "disarmed_in_air"
    d.reason, d.disarmed_at = reason, _iso()
    _event(s, d, "disarmed", "info" if reason in ("landed", "aborted") else "critical",
           f"上鎖（{REASON_TEXT[reason]}）", {"reason": reason})


@_safe
def on_landed(st: LiveState, prev: str | None) -> None:
    new = st.landed_state
    if prev is None or new is None or new == prev:
        return
    hit = _hit(st.drone_id)
    if not hit:
        return
    s, d = hit
    if new == "on_ground":
        d.landed_at = _iso()
        _event(s, d, "landed", "info", "著地")
    elif prev == "on_ground":
        _event(s, d, "takeoff", "info", "離地")


@_safe
def on_statustext(st: LiveState, severity: str, text: str) -> None:
    hit = _hit(st.drone_id)
    if not hit:
        return
    s, d = hit
    now = time.monotonic()
    if text.startswith("Crash") and "Disarm" in text:
        d.crash_at = now
        _event(s, d, "crash", "critical", text, {"text": text})
        return
    sv = _severity(severity)
    if sv == "info":
        return
    last = d.texts.get(text)
    if last and now - last[0] < TEXT_FOLD_S:
        last[1] += 1
        return
    count = 1 + (last[1] if last else 0)
    d.texts[text] = [now, 0]
    if len(d.texts) > 200:
        for k in [k for k, v in d.texts.items() if now - v[0] > TEXT_FOLD_S]:
            del d.texts[k]
    _event(s, d, "vehicle_text", sv, text, {"text": text, "count": count})


@_safe
def on_telemetry_lost(st: LiveState) -> None:
    hit = _hit(st.drone_id)
    if hit:
        _event(*hit, "telemetry_lost", "warning", "遙測中斷超過 10 秒")


@_safe
def on_telemetry_resumed(st: LiveState) -> None:
    hit = _hit(st.drone_id)
    if hit:
        _event(*hit, "telemetry_resumed", "info", "遙測恢復")


@_safe
def on_session_lost(st: LiveState) -> None:
    hit = _hit(st.drone_id)
    if hit:
        hit[1].reason = "telemetry_lost"


_LINK_TEXT = {"link_degraded": "5G 鏈路變差", "link_lost": "5G 鏈路中斷",
              "link_recovered": "5G 鏈路恢復"}


@_safe
def on_db_event(drone_id: str | None, severity: str, type_: str, detail: dict) -> None:
    if type_ not in ("mode_change", "waypoint_reached", "mission_state", "failsafe",
                     *_LINK_TEXT):
        return
    hit = _hit(drone_id)
    if not hit:
        return
    s, d = hit
    st = fleet.get(drone_id)
    detail = detail or {}
    if type_ == "mode_change":
        _event(s, d, "mode_change", "info", f"模式 {detail.get('from')} → {detail.get('to')}",
               {k: detail.get(k) for k in ("from", "to", "from_verb", "to_verb")})
    elif type_ == "waypoint_reached":
        seq, total = _plan_seq(st, detail.get("seq")), _plan_total(st, detail.get("total"))
        if seq is None:
            return
        _event(s, d, "waypoint_reached", "info",
               f"到達第 {seq} 項" + (f"（共 {total} 項）" if total else ""),
               {"seq": seq, "total": total})
    elif type_ == "mission_state":
        _event(s, d, "mission_progress", "info",
               f"路徑執行狀態 {detail.get('from')} → {detail.get('to')}",
               {"from": detail.get("from"), "to": detail.get("to"),
                "seq": _plan_seq(st, detail.get("seq")),
                "total": _plan_total(st, detail.get("total"))})
    elif type_ == "failsafe":
        _event(s, d, "failsafe", "critical", f"飛控進入 {detail.get('state')} 狀態", detail)
    else:
        _event(s, d, type_, _severity(severity), _LINK_TEXT[type_], detail)


# ── command 送來的起飛流程進度 ─────────────────────────────────────
class NotifyIn(BaseModel):
    kind: str
    drone_id: str | None = None
    sysid: int | None = None
    plan_id: str | None = None
    step: str | None = None
    ok: bool | None = None
    msg: str | None = None


@router.post("/ext/missions/{mission_id}/notify", include_in_schema=False)
async def notify(mission_id: str, body: NotifyIn):
    mid = str(uuid.UUID(mission_id))
    s = streams.get(mid)
    if s is None:
        s = streams[mid] = Stream(mid)
    did = body.drone_id or next(
        (k for k, st in fleet.items() if body.sysid is not None and st.sysid == body.sysid), None)
    d = s.drone(did) if did else None
    if d is not None and body.kind == "start_begin":
        d.plan_id = body.plan_id or d.plan_id
        d.starting_since, d.reason, d.aborted = time.monotonic(), None, False
    await _sync(s)
    _index(s)
    if d is None or s.phase == "ended":
        return {"ok": False}
    st = fleet.get(d.drone_id)
    armed = bool(st is not None and st.armed)
    if body.kind == "start_begin":
        await _send_route(s, d, "initial")
        _event(s, d, "start_step", "info", STEP_TEXT["begin"], {"step": "begin", "ok": True})
    elif body.kind == "start_step":
        ok = body.ok is not False
        text = STEP_TEXT.get(body.step, body.step or "")
        _event(s, d, "start_step", "info" if ok else "warning",
               text if ok else f"{text}失敗：{body.msg}",
               {"step": body.step, "ok": ok, "msg": body.msg})
    elif body.kind == "start_done":
        d.starting_since = None
    elif body.kind == "start_failed":
        d.starting_since = None
        if not armed:
            d.reason = "start_failed"
        _event(s, d, "start_step", "critical", f"起飛流程失敗：{body.msg}",
               {"step": body.step, "ok": False, "msg": body.msg})
    elif body.kind == "aborted":
        d.starting_since, d.aborted = None, True
        if not armed and d.reason is None:
            d.reason = "aborted"
        _event(s, d, "start_step", "critical", STEP_TEXT["abort"],
               {"step": "abort", "ok": False, "msg": body.msg})
    return {"ok": True}


# ── HTTP 輪詢：與 WebSocket 同一份資料，兩種傳法 ───────────────────
async def _open(mid: str) -> Stream:
    """拿到（必要時建立）這個編號的串流。**與 WS 進場走同一條路**——
    輪詢的控制端一樣可以先用自己的 UUID 開場、再帶著它呼叫起飛。
    """
    s = streams.get(mid)
    if s is not None:
        return s
    row = await db.pool.fetchrow("SELECT name, ended_at FROM missions WHERE id = $1::uuid", mid)
    if row is not None and row["ended_at"] is not None:
        raise HTTPException(410, {
            "code": "mission_gone",
            "msg": f"任務「{row['name']}」已在 {row['ended_at'].astimezone(TW):%m-%d %H:%M:%S} "
                   f"結束，結束後 {KEEP_ENDED_S:.0f} 秒的補送也過期了",
            "how_to": ["要看這次飛行的完整訊號：GET :38000/api/v1/ext/missions/{mission_id}/signal",
                       "要看完整遙測：GET :38000/api/v1/sessions/{session_id}/export"]})
    s = streams[mid] = Stream(mid)
    if row is not None:
        await _sync(s)
        _index(s)
    return s


@router.get("/ext/missions/{mission_id}/live")
async def live(mission_id: str, after_seq: int | None = None):
    """即時資料的**輪詢版**：與 `/ws/v1/missions/{id}` 同一份訊息、同一組序號。

    一推一拉，但 `messages` 裡的每一則與 WS 送出去的**逐字相同**——
    控制端換一種傳法不必改訊息處理，兩種也可以混用（WS 斷線期間先用輪詢頂著）。

    * **不帶 `after_seq`＝快照**：現在的 `route`、`track` 與最新一則 `state`
      （結束了就再附 `ended`）。第一次呼叫用這個，不必等下一個 tick。
    * **帶 `after_seq`＝補送**：那之後的每一則，語意同 WS 的 `?after_seq=`。
      回應的 `seq` 就是下一次要帶的值；緩衝只留 60 秒，斷太久就會有 `gap`。

    **輪詢不會讓任務活得比較久**：結束條件與 WS 完全一樣（最後一台上鎖
    3 秒後），這支端點只是把同一份訊息換個方式交出去。
    """
    try:
        mid = str(uuid.UUID(mission_id))
    except ValueError:
        raise HTTPException(422, {
            "code": "mission_id_invalid",
            "msg": f"網址裡的任務編號要是 UUID，收到的是「{mission_id}」",
            "how_to": ["用 crypto.randomUUID() 或 uuid.uuid4() 產生"]})
    s = await _open(mid)
    # 輪詢的人也是「有人在看」。少了這一句，非外部任務會在 IDLE_DROP_S 之後
    # 被當成沒人看而回收，而輪詢端根本沒有連線可以證明自己還在
    s.last_client = time.monotonic()

    oldest = s.buf[0][1]["seq"] if s.buf else None
    msgs: list[dict] = []
    gap = None
    if after_seq is None:
        for d in list(s.drones.values()):
            body = await _route_body(d, "initial")
            if body:
                msgs.append(s.envelope("route", body))
            if s.in_db:
                msgs.append(s.envelope("track", await _track_body(s, d)))
        if s.phase == "ended":
            for kind in ("state", "ended"):
                m = next((m for _, m in reversed(s.buf) if m["type"] == kind), None)
                if m is not None:
                    msgs.append(m)
        else:
            # **現算，不撿緩衝裡最後一則**：DB 裡剛出現的任務要到下一個 tick 才有
            # state，而快照的用途就是「不必等下一個 tick」。內容與 tick 送的同一份
            # （`ext_stream.tick`），只是不佔序號、不進緩衝——與 route／track 一樣
            # 是只屬於這一次呼叫的訊息
            msgs.append(s.envelope("state", {
                "phase": s.phase,
                "drones": [drone_state(d, fleet.get(d.drone_id)) for d in s.drones.values()]}))
    else:
        if after_seq < s.seq:
            first = oldest if oldest is not None else s.seq + 1
            if after_seq + 1 < first:
                gap = {"from_seq": after_seq + 1, "to_seq": first - 1}
        msgs = [m for _, m in s.buf if m["seq"] > after_seq]
    return {"v": 1, "mission_id": s.id, "ts": _iso(), "phase": s.phase,
            "mission_name": s.name, "started_at": s.started_at,
            "drones": [{"drone_id": d.drone_id, "name": d.name, "sysid": d.sysid}
                       for d in s.drones.values()],
            "seq": s.seq, "replay": {"from_seq": oldest, "gap": gap},
            "poll_after_s": TICK_S,
            "messages": json_safe(msgs)}


# ── WebSocket ─────────────────────────────────────────────────────
async def _refuse(ws: WebSocket, mid: str, close_code: int, code: str, msg: str,
                  how_to: list[str]) -> None:
    # 關閉原因最多 123 bytes，放不下一句中文解釋，先送一則 error
    try:
        await ws.send_text(json.dumps(
            {"v": 1, "type": "error", "mission_id": mid, "ts": _iso(),
             "code": code, "msg": msg, "how_to": how_to}, ensure_ascii=False))
        await ws.close(code=close_code)
    except Exception:
        pass


async def serve(ws: WebSocket, mission_id: str, after_seq: int | None) -> None:
    await ws.accept()
    try:
        mid = str(uuid.UUID(mission_id))
    except ValueError:
        await _refuse(ws, mission_id, 4400, "mission_id_invalid",
                      f"網址裡的任務編號要是 UUID，收到的是「{mission_id}」",
                      ["用 crypto.randomUUID() 或 uuid.uuid4() 產生"])
        return
    try:
        s = await _open(mid)
    except HTTPException as e:
        # 進場的判斷與輪詢端點共用一份（連同那句話）；這裡只換成 WS 的關閉碼
        d = e.detail
        await _refuse(ws, mid, 4410, d["code"], d["msg"], d["how_to"])
        return
    mark = s.seq
    bodies = []
    for d in list(s.drones.values()):
        r = await _route_body(d, "reconnect" if after_seq is not None else "initial")
        if r:
            bodies.append(("route", r))
        if s.in_db:
            bodies.append(("track", await _track_body(s, d)))
    c = Client(ws)
    oldest = s.buf[0][1]["seq"] if s.buf else None
    gap = None
    if after_seq is not None and after_seq < mark:
        first = oldest if oldest is not None else mark + 1
        if after_seq + 1 < first:
            gap = {"from_seq": after_seq + 1, "to_seq": first - 1}
    c.push(s.envelope("hello", {
        "phase": s.phase, "mission_name": s.name, "started_at": s.started_at,
        "drones": [{"drone_id": d.drone_id, "name": d.name, "sysid": d.sysid}
                   for d in s.drones.values()],
        "replay": {"from_seq": oldest, "gap": gap}}))
    for t, b in bodies:
        c.push(s.envelope(t, b))
    floor = after_seq if after_seq is not None else mark
    for _, m in s.buf:
        if m["seq"] > floor:
            c.push({**m, "replay": True} if m["seq"] <= mark else m)
        elif after_seq is None and m["type"] == "ended":
            c.push({**m, "replay": True})
    s.clients.add(c)
    s.last_client = time.monotonic()
    if s.phase == "ended":
        c.close(1000)
    writer = asyncio.create_task(c.run())
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("對外串流連線例外")
    finally:
        s.clients.discard(c)
        writer.cancel()
