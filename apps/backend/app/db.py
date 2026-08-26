import asyncio
import json
import logging

import asyncpg

from .config import settings
from .jsonsafe import dumps as jdumps
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
    # 037：.plan 自報的目標機種。QGC 的 firmwareType/vehicleType 用的是
    # MAV_AUTOPILOT／MAV_TYPE 這兩個 enum，**與 HEARTBEAT 同源**，所以可以
    # 直接跟機端偵測到的值比對。NULL＝這份任務沒說（手繪、舊資料、從機上讀回）
    await pool.execute(
        "ALTER TABLE missions ADD COLUMN IF NOT EXISTS firmware_type INT")
    await pool.execute(
        "ALTER TABLE missions ADD COLUMN IF NOT EXISTS vehicle_type INT")
    # 038：飛控板的唯一 ID（AUTOPILOT_VERSION.uid2）。**目前唯一機器可驗證的
    # 身分**——sysid 只是機上可改的參數。NULL＝還沒問到（不是「沒有」）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS board_uid TEXT")
    # 航線自帶的圍欄（QGC .plan 的 geoFence）。**圍欄是每份航線自己的事**，
    # 不是系統的全域設定——測繪任務與定點巡檢的合理範圍可以差一個數量級。
    # NULL＝這份 .plan 沒畫圍欄（退回系統預設，而且報告會說出用的是哪一個）
    await pool.execute("ALTER TABLE missions ADD COLUMN IF NOT EXISTS fence JSONB")
    # QGC 的 plannedHomePosition [lat, lon, alt]。**RTL 沒有座標**——它的意思是
    # 「回到 home」，所以少了這個點，返航那一段在畫面上根本畫不出來，
    # 使用者會以為航線在最後一個航點就結束了（2026-08-26 使用者回報）。
    # 它同時也是距離量測該用的原點：起飛項在很多 .plan 裡是 0,0
    await pool.execute("ALTER TABLE missions ADD COLUMN IF NOT EXISTS home JSONB")
    # .plan 宣告的速度，用來估預計時間。**沒宣告就不估**（不給預設值——
    # 猜一個看起來合理的數字，使用者會拿它安排電池）
    await pool.execute(
        "ALTER TABLE missions ADD COLUMN IF NOT EXISTS cruise_speed REAL")
    await pool.execute(
        "ALTER TABLE missions ADD COLUMN IF NOT EXISTS hover_speed REAL")
    # 039/038 兩層身分的**人工維護那層**：機架序號與型號。
    # **不動 serial_no**——它現在扛著自動註冊的冪等性（四處 ON CONFLICT），
    # 改它的語意風險不對稱：那條路徑出錯會讓每次心跳都新增一筆機。
    await pool.execute(
        "ALTER TABLE drones ADD COLUMN IF NOT EXISTS airframe_serial TEXT")
    # 韌體版本也要持久化：它與 board_uid 一樣是**板子的穩定屬性**，
    # 而 LiveState 是記憶體——backend 一重啟就失憶，而 command 服務的
    # 「已問過」旗標還在、不會再問一次，於是畫面上永遠是空的（038 的實作缺口）
    await pool.execute(
        "ALTER TABLE drones ADD COLUMN IF NOT EXISTS flight_sw_version TEXT")
    # 伴飛電腦（樹莓派）的序號。**與 board_uid 回答不同的問題**：
    # board_uid＝這是哪一架飛機，agent_uid＝這是哪一台伴飛電腦。
    # 5G 模組、Wi-Fi 卡、代理版本屬於後者；混成一個欄位，換件時就說不清是哪邊變了。
    await pool.execute(
        "ALTER TABLE drones ADD COLUMN IF NOT EXISTS agent_uid TEXT")
    # current_mission_id → missions 的參照完整性（ON DELETE SET NULL）：少了它，
    # 刪任務會讓 current_mission_id 變懸空指標，之後 create_session 綁 mission_id
    # 就撞 flight_sessions_mission_id_fkey → 解鎖建 session 每次拋錯 → 該機 armed
    # 永遠標不起來、不錄遙測（多機 bring-up 實測炸點：刪光飛行資料後殘留懸空
    # current_mission_id）。先清懸空值再補約束（冪等；約束不存在才加）。
    await pool.execute(
        """DO $$ BEGIN
             UPDATE drones d SET current_mission_id = NULL
               WHERE current_mission_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM missions m WHERE m.id = d.current_mission_id);
             IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname = 'drones_current_mission_id_fkey') THEN
               ALTER TABLE drones ADD CONSTRAINT drones_current_mission_id_fkey
                 FOREIGN KEY (current_mission_id) REFERENCES missions(id) ON DELETE SET NULL;
             END IF;
           END $$;""")
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
    # ── 飛行影像（issue 022；doc/flight-video-design.md）────────────────────
    # video_mode：'on'／'off'（本趟刻意不錄）／'no_source'（該機沒有影像來源）。
    # 為什麼要這欄：零片段有三種完全不同的意思——**沒錄**（實驗設定）與
    # **錄了但鏈路斷光**（實驗結果）對研究的意義相反，不能靠事後推測分辨。
    await pool.execute(
        "ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS video_mode TEXT")
    # 舊架次回填：影像功能上線前的架次本來就沒有錄影，標成 'off'（＝本趟未啟用
    # 錄影，對它們是事實）。不回填的話 NULL 會被判讀成「該錄卻沒錄到」的故障，
    # 整片歷史飛行都亮警報。只動**已結束**的架次——進行中的由 on_session_start
    # 標，不能被這裡蓋掉。冪等（只補 NULL）。
    await pool.execute(
        "UPDATE flight_sessions SET video_mode = 'off' "
        "WHERE video_mode IS NULL AND ended_at IS NOT NULL")
    # 每段影片一列。started_at＝**影片第 0 秒對應的絕對時間**（錨點）：回放
    # seek 用它換算段內 offset。逐段獨立錨點、不假設段段相接——段與段之間的
    # 空白是斷流的證據，照實留白，不靜默拼接假裝連續（使用者硬約束）。
    await pool.execute("""CREATE TABLE IF NOT EXISTS video_segments (
        id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        drone_id   UUID NOT NULL REFERENCES drones(id) ON DELETE CASCADE,
        session_id UUID REFERENCES flight_sessions(id) ON DELETE CASCADE,
        started_at TIMESTAMPTZ NOT NULL,
        duration_s DOUBLE PRECISION,
        path       TEXT NOT NULL,
        codec      TEXT,
        width      INT,
        height     INT,
        fps        DOUBLE PRECISION,
        bytes      BIGINT,
        source     TEXT NOT NULL DEFAULT 'ground',
        UNIQUE (drone_id, started_at))""")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_vseg_session "
                       "ON video_segments (session_id, started_at)")
    # duration_s 會**事後長大**：錄製器的片段長度是逐步結算的，落地後一分鐘查到的
    # 值可能還比最終值短好幾秒。把還沒定案的長度當權威用，尾端那幾秒就會落在涵蓋帶
    # 外、被讀成「此時段無影像（斷流）」——**把正常錄影說成故障**。
    # final=false 表示「這段還可能變長」，UI 據此不對尾端做斷言。
    await pool.execute("ALTER TABLE video_segments "
                       "ADD COLUMN IF NOT EXISTS final BOOLEAN NOT NULL DEFAULT false")
    # ── issue 021 Phase 2：每架次的機上參數快照（唯讀，實驗可重現性）────────
    # **內容定址**：參數在飛行之間通常不變，每架次存一份 851 筆會囤大量重複。
    # 同一組設定只存一列（hash 唯一），架次只記參照。
    await pool.execute("""CREATE TABLE IF NOT EXISTS param_sets (
        id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        hash        TEXT NOT NULL UNIQUE,
        param_count INT  NOT NULL,
        params      JSONB NOT NULL,
        first_seen  TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute(
        "ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS param_set_id UUID "
        "REFERENCES param_sets(id) ON DELETE SET NULL")
    # ── issue 023：missions 正名瘦身（路徑快照庫，不是任務庫）──────────────
    # kind 取代 created_by 兼差當判別欄。**加法不減法**：created_by 保留（歷史
    # 事實，留著零成本），只是不再被程式當分類用。
    await pool.execute("ALTER TABLE missions ADD COLUMN IF NOT EXISTS kind TEXT")
    await pool.execute("""
        UPDATE missions SET kind = CASE created_by
            WHEN 'plan-file'      THEN 'imported'      -- 使用者匯入 .plan
            WHEN 'vehicle'        THEN 'from-vehicle'  -- 從機上讀回
            WHEN 'group-gen'      THEN 'generated'     -- 編隊地面展開
            WHEN 'command-stage2' THEN 'generated'     -- 舊驗收測試的系統產物
            ELSE 'imported' END
        WHERE kind IS NULL""")
    # 架次的路徑名稱快照：使用者定案「飛過的路徑可以刪，但飛行紀錄要永遠存在」。
    # mission_id 是 ON DELETE SET NULL，刪路徑後回放頁只剩空白；留一份名字才能說
    # 「飛的是 X（路徑已刪除）」而不是什麼都說不出來。
    await pool.execute(
        "ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS mission_name TEXT")
    await pool.execute("""
        UPDATE flight_sessions fs SET mission_name = m.name
        FROM missions m WHERE m.id = fs.mission_id AND fs.mission_name IS NULL""")
    # 兩處外鍵原為 NO ACTION：刪「被編隊引用過的路徑」會 FK 違反丟 500（實測復現），
    # 與「飛過的路徑可以刪」的定案直接衝突。改 SET NULL 讓它真的刪得掉。
    for tbl, col in (("group_assignments", "mission_id"),
                     ("mission_groups", "base_mission_id")):
        await pool.execute(f"""
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_constraint
                         WHERE conname = '{tbl}_{col}_fkey' AND confdeltype <> 'n') THEN
                ALTER TABLE {tbl} DROP CONSTRAINT {tbl}_{col}_fkey;
                ALTER TABLE {tbl} ADD CONSTRAINT {tbl}_{col}_fkey
                  FOREIGN KEY ({col}) REFERENCES missions(id) ON DELETE SET NULL;
              END IF;
            END $$;""")
    # 三個死欄位（從建表至今從未被寫入或讀取；遷移前以資料驗證過全為預設/NULL）。
    # 它們是照「任務規劃工具」設計的，但本專案刻意不做規劃（規劃留 QGC）。
    # drone_id 的唯一用途（刪機時清 NULL）已同批從 api.py 移除。
    for col in ("status", "geometry", "drone_id"):
        await pool.execute(f"ALTER TABLE missions DROP COLUMN IF EXISTS {col}")


async def ensure_drone_by_board(board_uid: str, *, autopilot: str | None = None,
                                fw: str | None = None,
                                agent_uid: str | None = None,
                                vehicle_type: int | None = None) -> tuple[str, str, bool]:
    """以**飛控板 UID** 確保有一筆機體記錄。回傳 (drone_id, name, 是否新建)。

    **為什麼鍵是 board_uid 而不是 sysid**：sysid 是機上一個可以隨時改的參數。
    2026-08-24 實際發生過——一筆早已停用的舊記錄佔著 sysid 1，新接上的機一開機
    就被認領進去，`/api/live` 顯示的是別台機的名字。板子 UID 是燒在硬體上的，
    改參數、換機架、重刷韌體都不會變。

    **它認的是飛控板，不是機架**：板子拆到另一台飛機上，記錄跟著板子走，
    而人填的機架序號會變成錯的。這一點自動化解決不了（issues/038）。

    `agent_uid`（樹莓派序號）另外記：機上有些東西屬於伴飛電腦而不屬於飛機
    （5G 模組、Wi-Fi 卡、代理版本），出問題時要分得出是哪一邊。
    """
    row = await pool.fetchrow(
        "SELECT id::text AS id, name FROM drones WHERE board_uid = $1", board_uid)
    created = False
    if row is None:
        name = f"uav-{board_uid[-6:]}"      # 之後由人改名
        row = await pool.fetchrow(
            """INSERT INTO drones (name, serial_no, is_simulated, status, board_uid)
               VALUES ($1, $1, $2, 'idle', $3)
               ON CONFLICT (serial_no) DO UPDATE SET board_uid = EXCLUDED.board_uid
               RETURNING id::text AS id, name""",
            name, settings.link_source == "simulated", board_uid)
        created = True
    await pool.execute(
        """UPDATE drones SET flight_sw_version = COALESCE($2, flight_sw_version),
                             agent_uid         = COALESCE($3, agent_uid)
           WHERE id = $1::uuid""",
        row["id"], fw, agent_uid)
    return row["id"], row["name"], created


async def load_board_identity(drone_id: str | None) -> tuple[str | None, str | None]:
    """從 DB 取回這台機上次記錄的板子身分（uid, 韌體版本）。

    **給 backend 重啟後回填 LiveState 用。** 沒有這一步的話：值在 DB 裡、
    畫面上卻是空的，而且不會自己好——請求 AUTOPILOT_VERSION 的是 command
    服務，它的「已問過」旗標不隨 backend 重啟而清除。
    """
    if not drone_id:
        return None, None
    row = await pool.fetchrow(
        "SELECT board_uid, flight_sw_version FROM drones WHERE id = $1::uuid",
        drone_id)
    return (row["board_uid"], row["flight_sw_version"]) if row else (None, None)


async def set_board_uid(drone_id: str | None, uid: str,
                        fw: str | None = None) -> None:
    """記下這筆記錄目前對應的飛控板。

    **這一步只記錄，不比對、不擋。** 之後要做的「UID 變了就示警」（＝這筆記錄
    現在指的是別塊板子）需要先有一段時間的真實資料，確認 uid2 在同一塊板子上
    跨重開機/韌體升級是穩定的——沒驗證過就先加告警，只會製造假警報。
    """
    if not drone_id:
        return
    await pool.execute(
        "UPDATE drones SET board_uid = $2, "
        "flight_sw_version = COALESCE($3, flight_sw_version) WHERE id = $1::uuid",
        drone_id, uid, fw)


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
    # mission_name 快照（023）：路徑可被刪除（FK 是 SET NULL），但「飛行紀錄要
    # 永遠存在」——留一份當下的名字，刪掉路徑後回放頁仍能說「飛的是 X（路徑已
    # 刪除）」而不是一片空白。用 CTE 解一次 mission_id 再取名，避免把上面那串
    # COALESCE 抄第二遍（抄兩遍遲早會分岔）。
    row = await pool.fetchrow(
        """WITH resolved AS (
             SELECT COALESCE(
               $3::uuid,
               (SELECT current_mission_id FROM drones WHERE id = $1),
               CASE WHEN $2 THEN (SELECT id FROM missions WHERE is_active LIMIT 1) END
             ) AS mid
           )
           INSERT INTO flight_sessions
             (drone_id, started_at, mission_id, mission_name, origin)
           SELECT $1, now(), r.mid,
                  (SELECT name FROM missions WHERE id = r.mid),
                  (CASE WHEN EXISTS (
                       SELECT 1 FROM command_log c
                       WHERE c.sysid = (SELECT mav_sysid FROM drones WHERE id = $1)
                         AND c.client ~* '(rig|test|acceptance)'
                         AND c.time > now() - interval '60 seconds'
                   ) THEN 'test' END)
           FROM resolved r
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
        session_id, jdumps({k: (float(v) if v is not None else None) if k not in ("samples_in_zone", "samples_total") else int(v or 0) for k, v in dict(summary).items()}),
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
        jdumps(m["raw"]) if m.get("raw") is not None else None,
    )
    return row is not None


def param_hash(params: dict) -> str:
    """對**排序後**的 (名稱, 值) 序列做雜湊。

    排序是必要的：PARAM_VALUE 是非同步到達、每次抓取順序都不同，不正規化的話
    同一組設定會算出不同雜湊，去重直接失效。值統一用 repr 以固定浮點表示法。
    """
    import hashlib
    body = "\n".join(f"{k}={params[k]!r}" for k in sorted(params))
    return hashlib.sha256(body.encode()).hexdigest()


async def store_param_set(params: dict) -> str | None:
    """存一組參數（內容定址去重），回 param_sets.id。空的不存。"""
    if not params:
        return None
    h = param_hash(params)
    row = await pool.fetchrow("SELECT id::text AS id FROM param_sets WHERE hash = $1", h)
    if row:
        return row["id"]                      # 同一組設定已存在，直接參照
    row = await pool.fetchrow(
        """INSERT INTO param_sets (hash, param_count, params) VALUES ($1, $2, $3)
           ON CONFLICT (hash) DO UPDATE SET hash = EXCLUDED.hash
           RETURNING id::text AS id""",
        h, len(params), jdumps(params))
    return row["id"]


async def snapshot_params_for_session(session_id: str, st) -> None:
    """把該機當下的參數表綁到架次上（背景執行，不擋 rx worker）。

    **抓不完整就不綁**（len < 機端宣告的總數）：綁一份殘缺的快照比沒有快照更糟
    ——事後看起來像「當時就是這些設定」，實際上只是還沒收完。

    **會重試**：參數表是連線後才開始收（851 筆約 3 秒），而「剛連上就 arm」是
    真實情境（尤其地面站重啟後飛機還在飛）。一次性快照會在這種時候抓到空的，
    所以隔一段時間再看幾次；期間 st.params 由 PARAM_VALUE 分支持續填。
    """
    import asyncio as _asyncio
    try:
        await _snapshot_params_inner(session_id, st)
    except Exception:
        # 背景 task 的例外沒人接＝asyncio 的「Task exception was never retrieved」，
        # 埋在日誌裡很難發現（本功能第一版就是這樣漏掉 NaN 寫入失敗）。自己接住。
        log.exception("參數快照失敗（不影響架次記錄）")


async def _snapshot_params_inner(session_id: str, st) -> None:
    import asyncio as _asyncio
    for delay in (0.0, 3.0, 10.0, 30.0):
        if delay:
            await _asyncio.sleep(delay)
        params, total = dict(st.params), st.param_total
        if params and not (total and len(params) < total):
            pid = await store_param_set(params)
            if pid:
                await pool.execute(
                    "UPDATE flight_sessions SET param_set_id = $2 WHERE id = $1",
                    session_id, pid)
                log.info("參數快照：架次 %s ← %d 筆參數（param_set %s）",
                         session_id[:8], len(params), pid[:8])
            return
    log.info("參數快照放棄：架次 %s 等不到完整參數表（已收 %d / 宣告 %s）",
             session_id[:8], len(st.params), st.param_total)


async def insert_event(drone_id: str, session_id: str | None,
                       severity: str, type_: str, detail: dict,
                       source: str = "system") -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO events (drone_id, session_id, severity, type, detail, source)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, time
        """,
        drone_id, session_id, severity, type_, jdumps(detail), source,
    )
    return {"id": row["id"], "time": row["time"].isoformat(),
            "severity": severity, "type": type_, "detail": detail, "source": source}


async def bump_event(event_id: int, detail: dict) -> dict | None:
    """重複事件折疊（issue 014 Phase A）：把既有事件的 detail（含 count）與時間戳
    就地更新，回更新後的 time。前端據相同 id 原地替換，不新增一列。查無回 None
    （例：那列已被清理輪替掉，呼叫端退回新插一筆）。"""
    row = await pool.fetchrow(
        "UPDATE events SET detail = $2, time = now() WHERE id = $1 RETURNING time",
        event_id, jdumps(detail))
    if row is None:
        return None
    return {"id": event_id, "time": row["time"].isoformat(), "detail": detail}


