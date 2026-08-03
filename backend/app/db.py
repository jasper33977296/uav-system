import asyncio
import json
import logging

import asyncpg

from .config import settings
from .state import LiveState

log = logging.getLogger(__name__)
pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """建立連線池；DB 還沒起來時每 3 秒重試。"""
    global pool
    while True:
        try:
            pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
            log.info("database connected")
            return pool
        except OSError as e:
            log.warning("db not ready (%s), retrying in 3s", e)
            await asyncio.sleep(3)


async def ensure_drone(name: str, connection_url: str) -> str:
    row = await pool.fetchrow(
        """
        INSERT INTO drones (name, serial_no, is_simulated, connection_url, status)
        VALUES ($1, $1, true, $2, 'idle')
        ON CONFLICT (serial_no) DO UPDATE SET connection_url = $2
        RETURNING id
        """,
        name, connection_url,
    )
    return str(row["id"])


async def create_session(drone_id: str) -> str:
    row = await pool.fetchrow(
        "INSERT INTO flight_sessions (drone_id, started_at) VALUES ($1, now()) RETURNING id",
        drone_id,
    )
    return str(row["id"])


async def end_session(session_id: str) -> None:
    """關閉架次並統計本次的飛行與鏈路摘要（干擾研究常看的統計先算好）。"""
    summary = await pool.fetchrow(
        """
        SELECT
          (SELECT max(alt_rel) FROM telemetry WHERE session_id = $1)       AS max_alt_rel,
          (SELECT avg(sinr)    FROM link_metrics WHERE session_id = $1)    AS avg_sinr,
          (SELECT min(sinr)    FROM link_metrics WHERE session_id = $1)    AS min_sinr,
          (SELECT avg(rtt_ms)  FROM link_metrics WHERE session_id = $1)    AS avg_rtt_ms,
          (SELECT count(*) FILTER (WHERE in_interference_zone)
             FROM link_metrics WHERE session_id = $1)                      AS samples_in_zone,
          (SELECT count(*) FROM link_metrics WHERE session_id = $1)        AS samples_total
        """,
        session_id,
    )
    await pool.execute(
        "UPDATE flight_sessions SET ended_at = now(), summary = $2 WHERE id = $1",
        session_id, json.dumps({k: (float(v) if v is not None else None) if k not in ("samples_in_zone", "samples_total") else int(v or 0) for k, v in dict(summary).items()}),
    )


async def insert_telemetry(s: LiveState) -> None:
    await pool.execute(
        """
        INSERT INTO telemetry (time, drone_id, session_id, lat, lon, alt_msl, alt_rel,
          heading, ground_speed, vertical_speed, battery_pct, battery_voltage,
          gps_fix, satellites, flight_mode, armed)
        VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        """,
        s.drone_id, s.session_id, s.lat, s.lon, s.alt_msl, s.alt_rel,
        s.heading, s.ground_speed, s.vertical_speed, s.battery_pct, s.battery_voltage,
        s.gps_fix, s.satellites, s.flight_mode, s.armed,
    )


async def insert_link(s: LiveState) -> None:
    m = s.link
    await pool.execute(
        """
        INSERT INTO link_metrics (time, drone_id, session_id, lat, lon, alt_rel,
          rsrp, rsrq, sinr, cqi, pci, band, nr_mode,
          rtt_ms, jitter_ms, packet_loss_pct, throughput_up_kbps, throughput_down_kbps,
          in_interference_zone, source)
        VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19)
        """,
        s.drone_id, s.session_id, s.lat, s.lon, s.alt_rel,
        m.get("rsrp"), m.get("rsrq"), m.get("sinr"), m.get("cqi"),
        m.get("pci"), m.get("band"), m.get("nr_mode"),
        m.get("rtt_ms"), m.get("jitter_ms"), m.get("packet_loss_pct"),
        m.get("throughput_up_kbps"), m.get("throughput_down_kbps"),
        m.get("in_interference_zone"), m.get("source", "simulated"),
    )


async def insert_event(drone_id: str, session_id: str | None,
                       severity: str, type_: str, detail: dict) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO events (drone_id, session_id, severity, type, detail)
        VALUES ($1, $2, $3, $4, $5) RETURNING id, time
        """,
        drone_id, session_id, severity, type_, json.dumps(detail),
    )
    return {"id": row["id"], "time": row["time"].isoformat(),
            "severity": severity, "type": type_, "detail": detail}


async def fetch_cells() -> list[dict]:
    return [dict(r) for r in await pool.fetch("SELECT * FROM cells ORDER BY id")]


async def fetch_zones(enabled_only: bool = False) -> list[dict]:
    q = "SELECT * FROM interference_zones"
    if enabled_only:
        q += " WHERE enabled"
    return [dict(r) for r in await pool.fetch(q + " ORDER BY id")]
