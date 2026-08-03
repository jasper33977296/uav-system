from fastapi import APIRouter
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/api")


@router.get("/drones")
async def list_drones():
    return [dict(r) for r in await db.pool.fetch("SELECT * FROM drones ORDER BY created_at")]


@router.get("/cells")
async def list_cells():
    return await db.fetch_cells()


@router.get("/zones")
async def list_zones():
    return await db.fetch_zones()


class ZoneIn(BaseModel):
    name: str
    center_lat: float
    center_lon: float
    radius_m: float
    severity_db: float
    note: str | None = None


@router.post("/zones")
async def create_zone(z: ZoneIn):
    row = await db.pool.fetchrow(
        """
        INSERT INTO interference_zones (name, center_lat, center_lon, radius_m, severity_db, note)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
        """,
        z.name, z.center_lat, z.center_lon, z.radius_m, z.severity_db, z.note,
    )
    return dict(row)


@router.delete("/zones/{zone_id}")
async def delete_zone(zone_id: int):
    await db.pool.execute("DELETE FROM interference_zones WHERE id = $1", zone_id)
    return {"ok": True}


@router.get("/sessions")
async def list_sessions(limit: int = 50):
    rows = await db.pool.fetch(
        """
        SELECT s.*, d.name AS drone_name FROM flight_sessions s
        JOIN drones d ON d.id = s.drone_id
        ORDER BY s.started_at DESC LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


@router.get("/sessions/{session_id}/track")
async def session_track(session_id: str):
    """回放用：一個架次的飛行軌跡 + 鏈路品質時序，前端據此畫上色軌跡與圖表。"""
    telemetry = await db.pool.fetch(
        "SELECT * FROM telemetry WHERE session_id = $1 ORDER BY time", session_id)
    link = await db.pool.fetch(
        "SELECT * FROM link_metrics WHERE session_id = $1 ORDER BY time", session_id)
    return {"telemetry": [dict(r) for r in telemetry],
            "link": [dict(r) for r in link]}


@router.get("/events")
async def list_events(limit: int = 100):
    rows = await db.pool.fetch("SELECT * FROM events ORDER BY time DESC LIMIT $1", limit)
    return [dict(r) for r in rows]
