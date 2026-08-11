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


async def migrate() -> None:
    """既有資料庫的增量變更（db/init 只在全新 volume 執行）。冪等，啟動時跑。"""
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS video_url TEXT")
    # 2026-08-10：模擬場景改為 link_sim 內建常數，拆除模擬器專用表
    await pool.execute("DROP TABLE IF EXISTS interference_zones")
    await pool.execute("DROP TABLE IF EXISTS cells")
    # mav_sysid 遷移移到 backend（PM 2a：解耦「command 曾啟動過」的部署順序；
    # command 仍保留 IF NOT EXISTS 無妨）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS mav_sysid INT")
    # issue 020：每機「當前飛的任務」——command 上傳任務時設，create_session
    # 據此綁 session.mission_id（任務↔架次因果鏈，非一次性補丁）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS current_mission_id UUID")
    # issue 014 STATUSTEXT Phase A：事件來源分類。'vehicle'＝自駕儀自己吐的 log
    # （STATUSTEXT，QGC vehicle-messages 面板同源）；'system'＝backend 推導的
    # （link_lost/cell_change/session…）。前端據此分「機上訊息」與「系統事件」兩流。
    await pool.execute(
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'system'")
    # issue 013-A：群組任務資料模型（doc/group-missions-design.md）
    await pool.execute("""CREATE TABLE IF NOT EXISTS mission_groups (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        name TEXT NOT NULL,
        base_mission_id UUID REFERENCES missions(id),   -- unified 展開來源
        mode TEXT NOT NULL DEFAULT 'unified',            -- unified / separate
        params JSONB,                                    -- vsep_m/rtl_stagger_m 等
        status TEXT NOT NULL DEFAULT 'draft',            -- 見 §7.1 group.status
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute("""CREATE TABLE IF NOT EXISTS group_assignments (
        group_id UUID REFERENCES mission_groups(id) ON DELETE CASCADE,
        drone_id UUID NOT NULL,
        mission_id UUID REFERENCES missions(id),         -- materialized 具體任務
        layer_index INT NOT NULL DEFAULT 0,
        phase TEXT NOT NULL DEFAULT 'idle',              -- 見 §7.1 assignment.phase
        PRIMARY KEY (group_id, drone_id))""")
    await pool.execute("ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS group_id UUID")
    # issue 013-B：執行期即時態。phase 已在建表；補 error（異常態的
    # {msg,hint,autopilot_notes}，§7.1）與 updated_at（前端 1s 輪詢看新鮮度）。
    await pool.execute("ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS error JSONB")
    await pool.execute(
        "ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
    # 架次自訂備註（使用者要標實驗條件，如「開干擾器那趟」）：短文字，PATCH 可改
    await pool.execute("ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS note TEXT")
    # 架次來源分類（'research'/'test'/'unknown'）：測試殘留混研究庫的治理（PM 定案：
    # 標記不刪除）。預設 NULL＝未定＝API 視為 'unknown'（誠實：不確定就說不確定）。
    # 回填見 scripts/backfill-session-origin.sql；前向由 create_session 依觸發 client 標。
    await pool.execute("ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS origin TEXT")


async def drone_for_sysid(sysid: int) -> tuple[str, str]:
    """sysid → (drone_id, name)。多機自動註冊（issues/011 定案）：

    1. 已有 mav_sysid 對應 → 直接用
    2. 主機還沒認領 sysid → 認領給主機——既有單機部署升級時，
       不會因為多了 sysid 概念而生出一台幽靈機
    3. 都不是 → 自動建檔（uav-s{sysid}，之後無人機頁改名）
    """
    row = await pool.fetchrow(
        "SELECT id::text AS id, name FROM drones WHERE mav_sysid = $1", sysid)
    if row:
        return row["id"], row["name"]
    row = await pool.fetchrow(
        "SELECT id::text AS id, name FROM drones WHERE is_primary AND mav_sysid IS NULL")
    if row:
        await pool.execute("UPDATE drones SET mav_sysid = $2 WHERE id = $1",
                           row["id"], sysid)
        return row["id"], row["name"]
    name = f"uav-s{sysid}"
    # is_simulated 由 config 決定（SITL/dev 全 true、生產 false）——見 config 註解，
    # 避免假機混進真機清單（issue 013-B）
    row = await pool.fetchrow(
        """INSERT INTO drones (name, serial_no, is_simulated, status, mav_sysid)
           VALUES ($1, $1, $3, 'idle', $2)
           ON CONFLICT (serial_no) DO UPDATE SET mav_sysid = EXCLUDED.mav_sysid
           RETURNING id::text AS id, name""",
        name, sysid, settings.autoregister_simulated)
    return row["id"], row["name"]


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


async def recover_orphan_sessions() -> int:
    """補結算孤兒航線：backend 在飛行中重啟時，armed→disarmed 的轉換沒人看見，
    session 會永遠停在開放狀態（實際發生過，一天累積 6 條）。
    啟動時把所有開放 session 用「最後一筆遙測的時間」結算；
    完全沒資料的空殼直接刪除。"""
    r = await pool.execute("""
        UPDATE flight_sessions s SET
          ended_at = (SELECT max(time) FROM telemetry t WHERE t.session_id = s.id),
          summary = (SELECT jsonb_build_object(
            'max_alt_rel', (SELECT max(alt_rel) FROM telemetry WHERE session_id = s.id),
            'avg_sinr',   (SELECT avg(sinr) FROM link_metrics WHERE session_id = s.id),
            'min_sinr',   (SELECT min(sinr) FROM link_metrics WHERE session_id = s.id),
            'avg_rtt_ms', (SELECT avg(rtt_ms) FROM link_metrics WHERE session_id = s.id),
            'samples_in_zone', (SELECT count(*) FILTER (WHERE in_interference_zone)
                                FROM link_metrics WHERE session_id = s.id),
            'samples_total', (SELECT count(*) FROM link_metrics WHERE session_id = s.id)))
        WHERE s.ended_at IS NULL
          AND EXISTS (SELECT 1 FROM telemetry t WHERE t.session_id = s.id)""")
    n = int(r.split()[-1])
    await pool.execute("DELETE FROM flight_sessions WHERE ended_at IS NULL")
    return n


async def get_primary_drone() -> dict | None:
    """主機（MAVLink 資料記在哪台名下）由系統端指定：drones.is_primary。"""
    row = await pool.fetchrow(
        "SELECT id::text AS id, name FROM drones WHERE is_primary LIMIT 1")
    return dict(row) if row else None


async def create_default_primary(is_simulated: bool, connection_url: str) -> dict:
    """全新環境沒有任何主機時自動建一台（名稱可在無人機頁改）——
    資料記錄不等使用者設定，先以預設身分開錄。"""
    row = await pool.fetchrow(
        """INSERT INTO drones (name, serial_no, is_simulated, connection_url, status, is_primary)
           VALUES ('uav-1', 'uav-1', $1, $2, 'idle', true)
           ON CONFLICT (serial_no) DO UPDATE SET is_primary = true
           RETURNING id::text AS id, name""",
        is_simulated, connection_url)
    return dict(row)


async def create_session(drone_id: str, link_mission: bool = True,
                         mission_id: str | None = None) -> str:
    """開一條航線紀錄。mission_id 指定時直接關聯（群飛模擬飛指定任務）；
    否則 link_mission=True 時關聯任務庫當下的啟用路徑（is_active）——
    語意是「操作員宣告要飛的那條」。回放頁據此疊預計路徑。"""
    # 綁定序（issue 020，任務↔架次因果鏈）：明示 mission_id > 該機當前任務
    # （command 上傳時設 drones.current_mission_id，可靠事實源）> is_active 後備
    # 前向 origin 標記：該機 sysid 近 60s 有測試類 client（rig/test/acceptance）的
    # command_log → 'test'；否則 NULL（＝unknown，可由 backfill 再判）。用 command_log
    # 相關性、不做跨服務 drone 欄位 plumbing（會 racy）。與 backfill 同一判準。
    row = await pool.fetchrow(
        """INSERT INTO flight_sessions (drone_id, started_at, mission_id, origin)
           VALUES ($1, now(), COALESCE(
                   $3::uuid,
                   (SELECT current_mission_id FROM drones WHERE id = $1),
                   CASE WHEN $2 THEN (SELECT id FROM missions WHERE is_active LIMIT 1) END),
                   (CASE WHEN EXISTS (
                       SELECT 1 FROM command_log c
                       WHERE c.sysid = (SELECT mav_sysid FROM drones WHERE id = $1)
                         AND c.client ~* '(rig|test|acceptance)'
                         AND c.time > now() - interval '60 seconds'
                   ) THEN 'test' END))
           RETURNING id""",
        drone_id, link_mission, mission_id,
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
                       severity: str, type_: str, detail: dict,
                       source: str = "system") -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO events (drone_id, session_id, severity, type, detail, source)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, time
        """,
        drone_id, session_id, severity, type_, json.dumps(detail), source,
    )
    return {"id": row["id"], "time": row["time"].isoformat(),
            "severity": severity, "type": type_, "detail": detail, "source": source}


async def bump_event(event_id: int, detail: dict) -> dict | None:
    """重複事件折疊（issue 014 Phase A）：把既有事件的 detail（含 count）與時間戳
    就地更新，回更新後的 time。前端據相同 id 原地替換，不新增一列。查無回 None
    （例：那列已被清理輪替掉，呼叫端退回新插一筆）。"""
    row = await pool.fetchrow(
        "UPDATE events SET detail = $2, time = now() WHERE id = $1 RETURNING time",
        event_id, json.dumps(detail))
    if row is None:
        return None
    return {"id": event_id, "time": row["time"].isoformat(), "detail": detail}


