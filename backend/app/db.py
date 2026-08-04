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


async def find_session_at(drone_id: str, ts) -> str | None:
    """回傳涵蓋 ts 這個時刻的架次 id，沒有則 None。

    真機階段機上是 push 且允許補傳，資料抵達時飛機可能早已上鎖，
    不能用「當前架次」歸屬。用樣本自帶的時間戳反查，語意等同
    issues/004 的 armed gate，但對補傳資料成立。
    走 idx_sessions_drone (drone_id, started_at DESC)。
    """
    row = await pool.fetchrow(
        """
        SELECT id FROM flight_sessions
        WHERE drone_id = $1 AND started_at <= $2
          AND (ended_at IS NULL OR ended_at >= $2)
        ORDER BY started_at DESC LIMIT 1
        """,
        drone_id, ts,
    )
    return str(row["id"]) if row else None


async def insert_link_sample(drone_id: str, session_id: str | None, m: dict) -> bool:
    """寫入一筆機上送來的鏈路量測，使用樣本自帶的時間戳。

    冪等：重試屬 at-least-once 投遞，同一筆可能送達兩次，
    以 (drone_id, time) 為天然鍵 DO NOTHING（唯一索引 idx_link_dedup）。
    回傳是否真的新增（False 表示已存在，機上仍應視為送達成功）。
    """
    row = await pool.fetchrow(
        """
        INSERT INTO link_metrics (time, drone_id, session_id, lat, lon, alt_rel,
          rsrp, rsrq, sinr, cqi, pci, cell_id, band, nr_mode,
          rtt_ms, jitter_ms, packet_loss_pct, throughput_up_kbps, throughput_down_kbps,
          in_interference_zone, source, raw)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18, $19, $20, $21, $22)
        ON CONFLICT (drone_id, time) DO NOTHING
        RETURNING 1
        """,
        m["time"], drone_id, session_id, m.get("lat"), m.get("lon"), m.get("alt_rel"),
        m.get("rsrp"), m.get("rsrq"), m.get("sinr"), m.get("cqi"),
        m.get("pci"), m.get("cell_id"), m.get("band"), m.get("nr_mode"),
        m.get("rtt_ms"), m.get("jitter_ms"), m.get("packet_loss_pct"),
        m.get("throughput_up_kbps"), m.get("throughput_down_kbps"),
        m.get("in_interference_zone"), m.get("source", "modem"),
        json.dumps(m["raw"]) if m.get("raw") is not None else None,
    )
    return row is not None


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
