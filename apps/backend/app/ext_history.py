"""對外任務歷史（doc/external-history-api.md）：一個任務的完整訊號，供控制端比較兩趟或多趟。

即時那一半在 `ext_stream`；訊號欄位與預計航線的組法**共用同一份**，
即時看到的與事後拿到的是同一個東西。
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from . import chainage, db
from .ext_stream import LINK_KEYS, route_geojson

log = logging.getLogger("ext_history")
router = APIRouter(prefix="/api/ext/v1")

MAX_LIMIT = 200
#: 訊號欄位（LINK_KEYS 的第一項是 time，樣本裡另外擺在最前面）
METRIC_KEYS = tuple(k for k in LINK_KEYS if k != "time")
SAMPLE_COLS = ", ".join(("time", "lat", "lon", "alt_rel", *METRIC_KEYS))


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid(v: str, field: str) -> str:
    try:
        return str(uuid.UUID(v))
    except ValueError:
        raise HTTPException(422, {"code": f"{field}_invalid",
                                  "msg": f"{field} 要是 UUID，收到的是「{v}」"})


@router.get("/missions")
async def list_missions(since: str | None = None, until: str | None = None,
                        plan_id: str | None = None, external: bool | None = None,
                        drone_id: str | None = None, limit: int = 50):
    """歷史任務清單。要比較的兩個任務通常用 `plan_id` 挑——同一份路徑飛的兩趟里程才對得起來。

    統計（架次數、樣本數、第一趟起飛時間）都是查詢時算的，**不存欄位**：
    存下來就要維護一致性，而那是第二個家。
    """
    args: list = []

    def arg(v) -> str:
        args.append(v)
        return f"${len(args)}"

    conds = []
    if plan_id:
        conds.append("EXISTS (SELECT 1 FROM flight_sessions x WHERE x.mission_id = m.id "
                     f"AND x.plan_id = {arg(_uuid(plan_id, 'plan_id'))}::uuid)")
    if drone_id:
        did = _uuid(drone_id, "drone_id")
        conds.append(f"({arg(did)}::uuid IN (SELECT drone_id FROM mission_drones WHERE mission_id = m.id) "
                     f"OR EXISTS (SELECT 1 FROM flight_sessions x WHERE x.mission_id = m.id "
                     f"AND x.drone_id = {arg(did)}::uuid))")
    if external is not None:
        conds.append(f"m.external = {arg(external)}")
    if since:
        conds.append(f"coalesce(first.started_at, m.created_at) >= {arg(since)}::text::timestamptz")
    if until:
        conds.append(f"coalesce(first.started_at, m.created_at) <= {arg(until)}::text::timestamptz")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = await db.pool.fetch(f"""
        SELECT m.id::text AS mission_id, m.name, m.external, m.ended_at,
               first.started_at,
               (SELECT count(*) FROM flight_sessions x WHERE x.mission_id = m.id) AS sessions,
               (SELECT count(*) FROM link_metrics l JOIN flight_sessions x ON x.id = l.session_id
                 WHERE x.mission_id = m.id) AS samples,
               (SELECT coalesce(json_agg(json_build_object(
                          'drone_id', d.id::text, 'name', d.name, 'sysid', d.mav_sysid)), '[]'::json)
                  FROM drones d WHERE d.id IN (
                    SELECT drone_id FROM mission_drones WHERE mission_id = m.id
                    UNION SELECT drone_id FROM flight_sessions WHERE mission_id = m.id)) AS drones,
               (SELECT coalesce(json_agg(json_build_object(
                          'plan_id', p.id::text, 'name', p.name)), '[]'::json)
                  FROM plans p WHERE p.id IN (
                    SELECT plan_id FROM flight_sessions
                     WHERE mission_id = m.id AND plan_id IS NOT NULL)) AS plans
          FROM missions m
          LEFT JOIN LATERAL (SELECT min(started_at) AS started_at FROM flight_sessions x
                              WHERE x.mission_id = m.id) first ON true
          {where}
         ORDER BY coalesce(first.started_at, m.created_at) DESC
         LIMIT {arg(max(1, min(limit, MAX_LIMIT)))}""", *args)
    out = []
    for r in rows:
        d = dict(r)
        for k in ("drones", "plans"):
            if isinstance(d[k], str):
                d[k] = json.loads(d[k])
        out.append({"mission_id": d["mission_id"], "name": d["name"],
                    "external": d["external"],
                    "started_at": _iso(d["started_at"]), "ended_at": _iso(d["ended_at"]),
                    "drones": d["drones"], "plans": d["plans"],
                    "sessions": d["sessions"], "samples": d["samples"]})
    return {"missions": out}


async def _route_of(plan_id: str | None) -> tuple[dict | None, list[dict]]:
    """路徑 →（GeoJSON, 投影用的參考點）。沒有路徑就兩個都沒有。"""
    if not plan_id:
        return None, []
    prow = await db.pool.fetchrow("SELECT home FROM plans WHERE id = $1::uuid", plan_id)
    if prow is None:
        return None, []
    wps = [dict(r) for r in await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1::uuid ORDER BY seq", plan_id)]
    home = prow["home"]
    if isinstance(home, str):
        home = json.loads(home)
    _, gj = route_geojson(wps, home)
    # 投影的參考點＝帶座標的航點本身（與畫面上的沿路徑對照同一個來源）
    ref = [{"lat": w["lat"], "lon": w["lon"]} for w in wps if w["lat"] or w["lon"]]
    return gj, ref


@router.get("/missions/{mission_id}/signal")
async def mission_signal(mission_id: str,
                         max_offset_m: float = chainage.DEFAULT_MAX_OFFSET_M):
    """一個任務的完整訊號，依「機 → 架次」分組。一次一個任務。

    每一筆樣本帶 `along_m`（沿預計航線走了多遠）與 `offset_m`（偏離多少）——
    兩趟的速度不同，時間對不齊，里程才是共同的 X 軸（doc/external-history-api.md §4）。
    """
    mid = _uuid(mission_id, "mission_id")
    m = await db.pool.fetchrow(
        "SELECT id::text AS mission_id, name, external, ended_at FROM missions WHERE id = $1::uuid", mid)
    if m is None:
        raise HTTPException(404, {"code": "mission_not_found",
                                  "msg": f"沒有這個任務：{mid}"})
    rows = await db.pool.fetch("""
        SELECT s.id::text AS session_id, s.drone_id::text AS drone_id, d.name AS drone_name,
               d.mav_sysid, s.plan_id::text AS plan_id,
               coalesce(p.name, s.plan_name) AS plan_name,
               s.started_at, s.ended_at, s.end_reason
          FROM flight_sessions s
          JOIN drones d ON d.id = s.drone_id
          LEFT JOIN plans p ON p.id = s.plan_id
         WHERE s.mission_id = $1::uuid
         ORDER BY d.name, s.started_at""", mid)
    drones: dict[str, dict] = {}
    routes: dict[str, tuple] = {}
    for r in rows:
        if r["plan_id"] not in routes:
            routes[r["plan_id"]] = await _route_of(r["plan_id"])
        gj, ref = routes[r["plan_id"]]
        project = chainage.projector(ref, max_offset_m)
        samples = []
        for x in await db.pool.fetch(
                f"SELECT {SAMPLE_COLS} FROM link_metrics WHERE session_id = $1::uuid ORDER BY time",
                r["session_id"]):
            along = off = None
            if project and x["lat"] is not None and x["lon"] is not None:
                along, off = project(x["lat"], x["lon"])
            samples.append({"time": _iso(x["time"]), "lat": x["lat"], "lon": x["lon"],
                            "alt_rel": x["alt_rel"],
                            "along_m": round(along, 2) if along is not None else None,
                            "offset_m": round(off, 2) if off is not None else None,
                            **{k: x[k] for k in METRIC_KEYS}})
        gaps = [{"from": _iso(b["started_at"]), "to": _iso(b["ended_at"]),
                 "seconds": (round((b["ended_at"] - b["started_at"]).total_seconds(), 1)
                             if b["ended_at"] else None)}
                for b in await db.pool.fetch(
                    "SELECT started_at, ended_at FROM blackouts WHERE session_id = $1::uuid "
                    "ORDER BY started_at", r["session_id"])]
        d = drones.setdefault(r["drone_id"], {
            "drone_id": r["drone_id"], "name": r["drone_name"],
            "sysid": r["mav_sysid"], "sessions": []})
        d["sessions"].append({
            "session_id": r["session_id"], "plan_id": r["plan_id"],
            "plan_name": r["plan_name"],
            "started_at": _iso(r["started_at"]), "ended_at": _iso(r["ended_at"]),
            "end_reason": r["end_reason"],
            # **基準是計畫航點，不是任一趟的實飛軌跡**：沒有路徑就不給里程，
            # 退回用軌跡會讓那一趟的偏航變成零誤差（§4）
            "reference": "plan" if project else None,
            "route": gj, "gaps": gaps, "samples": samples})
    return {"mission": {"mission_id": m["mission_id"], "name": m["name"],
                        "external": m["external"],
                        "started_at": _iso(min((r["started_at"] for r in rows), default=None)),
                        "ended_at": _iso(m["ended_at"])},
            "method": {"max_offset_m": max_offset_m, "sample_interval_s": 1},
            "drones": list(drones.values())}
