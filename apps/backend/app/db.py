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


async def _rename_missions_to_plans() -> None:
    """階段 1：`missions` → `plans`（doc/mission-vs-plan-design.md §3）。

    **必須是 `migrate()` 的第一件事。** 本檔其餘的 SQL 已經全部改用新名字，
    改名放在中段的話，同一支函式前段的 DDL 會先參照 `plans` 而它還不存在
    ——啟動即 `relation "plans" does not exist`，服務起不來（實作時踩過）。

    **七項在同一個 transaction 裡。** 中途失敗留下「三個新名字、四個舊名字」
    是最難救的狀態；要嘛全改、要嘛全不改。

    **用 `RENAME` 不用「新表＋複製＋刪舊表」**：`RENAME` 保住五條外鍵
    （`waypoints` 是 CASCADE、其餘 SET NULL）、索引、既有資料，而且是原子的。
    自己重建外鍵的話，錯一條就是刪除行為靜靜地變了。

    冪等：名字已經是新的就整段跳過。
    """
    async with pool.acquire() as con:
        async with con.transaction():
            if await con.fetchval("SELECT to_regclass('public.missions')") is not None \
                    and await con.fetchval("SELECT to_regclass('public.plans')") is None:
                await con.execute("ALTER TABLE missions RENAME TO plans")
                log.info("migrate: 表 missions → plans")
            for table, old_c, new_c in (
                    ("waypoints", "mission_id", "plan_id"),
                    ("flight_sessions", "mission_id", "plan_id"),
                    ("flight_sessions", "mission_name", "plan_name"),
                    ("drones", "current_mission_id", "current_plan_id"),
                    ("mission_groups", "base_mission_id", "base_plan_id"),
                    ("group_assignments", "mission_id", "plan_id")):
                has = await con.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1 AND column_name = ANY($2::text[])",
                    table, [old_c, new_c])
                names = {r["column_name"] for r in has}
                if old_c in names and new_c not in names:
                    await con.execute(
                        f"ALTER TABLE {table} RENAME COLUMN {old_c} TO {new_c}")
                    log.info("migrate: %s.%s → %s", table, old_c, new_c)
                elif old_c in names and new_c in names:
                    # **兩個名字同時在＝有人（或某次失敗的啟動）把新欄位另外
                    # 建出來了。** 這時不能猜哪一個是真值，只能大聲說出來——
                    # 靜靜跳過的下場是：舊欄位有資料、新欄位是空的，而程式
                    # 從此讀空的那一個（2026-09-08 實際發生過）。
                    log.error("migrate: %s 同時有 %s 與 %s——**沒有改名**。"
                              "請人工確認哪一個是真值再處理", table, old_c, new_c)


async def migrate() -> None:
    """既有資料庫的增量變更（db/init 只在全新 volume 執行）。冪等，啟動時跑。"""
    # **這一行必須留在最前面**（見 _rename_missions_to_plans 的說明）
    await _rename_missions_to_plans()
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS video_url TEXT")
    # 2026-08-10：模擬場景改為 link_sim 內建常數，拆除模擬器專用表
    await pool.execute("DROP TABLE IF EXISTS interference_zones")
    await pool.execute("DROP TABLE IF EXISTS cells")
    # mav_sysid 遷移移到 backend（PM 2a：解耦「command 曾啟動過」的部署順序；
    # command 仍保留 IF NOT EXISTS 無妨）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS mav_sysid INT")
    # issue 020：每機「當前飛的任務」——command 上傳任務時設，create_session
    # 據此綁 session.plan_id（任務↔架次因果鏈，非一次性補丁）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS current_plan_id UUID")
    # 037：.plan 自報的目標機種。QGC 的 firmwareType/vehicleType 用的是
    # MAV_AUTOPILOT／MAV_TYPE 這兩個 enum，**與 HEARTBEAT 同源**，所以可以
    # 直接跟機端偵測到的值比對。NULL＝這份任務沒說（手繪、舊資料、從機上讀回）
    await pool.execute(
        "ALTER TABLE plans ADD COLUMN IF NOT EXISTS firmware_type INT")
    await pool.execute(
        "ALTER TABLE plans ADD COLUMN IF NOT EXISTS vehicle_type INT")
    # 2026-09-09（doc/route-planning-redesign.md §8）：這份航線是用哪個
    # **高度／速度政策**產生的。逐點的 alt 是它解出來的結果，政策才是意圖
    # ——沒有它，改政策就只能整條重畫
    await pool.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS policy JSONB")
    # 2026-09-09（redesign §7）：**這一份在什麼假設下被誰看過**。
    # 上傳那一刻分不出「沒人看過」與「看過、按了照飛」，所以它只能全擋或
    # 全不擋——簽核就是缺的那一半。`waypoints_hash` 是關鍵：航點一改簽核
    # 就失效，不然它只是「曾經有人在某個版本上按過 OK」。
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS plan_checks (
          id             BIGSERIAL PRIMARY KEY,
          plan_id        UUID REFERENCES plans(id) ON DELETE CASCADE,
          checked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          waypoints_hash TEXT NOT NULL,
          ok             BOOLEAN NOT NULL,
          problems       JSONB,
          acknowledged   JSONB,
          assumed_m      REAL,
          wp_spd         REAL,
          limits         JSONB,
          signed_by      TEXT
        )""")
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS plan_checks_plan_idx "
        "ON plan_checks (plan_id, checked_at DESC)")
    # 2026-09-11：圍欄會寫進飛控之後，審查也綁圍欄。NULL＝那次審查時沒有圍欄
    await pool.execute(
        "ALTER TABLE plan_checks ADD COLUMN IF NOT EXISTS fence_hash TEXT")
    # 2026-09-14：取消軌跡與訊號的 30 天保留（init SQL 原本會加）。已經建好的
    # 資料庫也要拿掉，不然舊部署照樣在 30 天後把回放的原料清掉
    for t in ("telemetry", "link_metrics"):
        await pool.execute(
            f"SELECT remove_retention_policy('{t}', if_exists => true)")
    # 038：飛控板的唯一 ID（AUTOPILOT_VERSION.uid2）。**目前唯一機器可驗證的
    # 身分**——sysid 只是機上可改的參數。NULL＝還沒問到（不是「沒有」）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS board_uid TEXT")
    # ── 階段 2：任務（doc/mission-vs-plan-design.md §4）──────────────
    # **`missions` 現在是「要達成的那件事」**，不是路徑（路徑在 `plans`）。
    # 一個任務可以有 N 個架次、N 份路徑、N 台機——三個 N 都不設限，
    # 限制哪一個都會在某次實驗被打破。所以關聯放在 `flight_sessions` 那一側
    # （多對一），不需要中介表。
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS missions (
          id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          name       TEXT NOT NULL,
          note       TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          ended_at   TIMESTAMPTZ
        )""")
    # 名稱唯一：任務是拿來喊的（「那個低速測線的實驗」）。兩個同名任務畫面上
    # 分得出（有 id），**人喊出來分不出**——與 squads 同一條理由
    await pool.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_missions_name "
                       "ON missions (lower(name))")
    # §4.5 曾經限制「同時只能有一個進行中的任務」，**§4.6 拿掉了**——使用者的
    # 目標是多組同時跑多個任務。改用「一台機同時只能執行一個任務」，那條窄得多
    await pool.execute("DROP INDEX IF EXISTS idx_missions_one_active")
    # 綁定：小隊（活的連結）與單台，兩種都可以（§4.6）
    await pool.execute("ALTER TABLE missions ADD COLUMN IF NOT EXISTS squad_id UUID")
    await pool.execute("""
        DO $$ BEGIN
          ALTER TABLE missions ADD CONSTRAINT missions_squad_id_fkey
            FOREIGN KEY (squad_id) REFERENCES squads(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$""")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS mission_drones (
          mission_id UUID NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
          drone_id   UUID NOT NULL REFERENCES drones(id)   ON DELETE CASCADE,
          PRIMARY KEY (mission_id, drone_id)
        )""")
    # ── 不變式：一台機不得同時在兩個「進行中」任務的有效名單裡（§4.6）──
    #
    # **partial unique index 做不到**：它的 WHERE 要問 missions.ended_at，而
    # partial index 的條件必須不可變、只吃本表欄位——PostgreSQL 直接拒絕子查詢。
    # 所以用觸發器，而且要掛**三個**寫入點。第三個最容易漏：把一台已經在任務 B
    # 的機加進小隊 S，而 S 綁在任務 A——沒有人碰 missions 或 mission_drones，
    # 不變式卻被打破了。
    #
    # **不靠應用層自己記得檢查**：那條規則會在某次改動被繞過，而它是起飛時
    # 自動歸類唯一的前提。約束要住在資料庫裡。
    # 檢查方式刻意寫成「**看整體**」而不是「看這一列」：任何一次寫入之後，
    # 只要有任何一台機落在兩個進行中任務的有效名單裡就擋。三個觸發點共用同一
    # 段邏輯，不必各自推導「這次寫入可能造成什麼」——那種推導漏一種情況就破功。
    # 進行中的任務只有個位數，全掃的成本可以忽略。
    await pool.execute("""
        CREATE OR REPLACE FUNCTION mission_drone_guard() RETURNS trigger AS $fn$
        DECLARE c RECORD;
        BEGIN
          WITH eff AS (
            SELECT m.id AS mission_id, m.name, x.drone_id
              FROM missions m
              JOIN LATERAL (
                SELECT md.drone_id FROM mission_drones md WHERE md.mission_id = m.id
                UNION
                SELECT sm.drone_id FROM squad_members sm WHERE sm.squad_id = m.squad_id
              ) x ON true
             WHERE m.ended_at IS NULL)
          SELECT d.name AS drone_name,
                 string_agg(eff.name, '」與「' ORDER BY eff.name) AS names
            INTO c
            FROM eff JOIN drones d ON d.id = eff.drone_id
           GROUP BY eff.drone_id, d.name
          HAVING count(*) > 1
           LIMIT 1;
          IF FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'unique_violation',
              MESSAGE = format('「%s」同時被排進「%s」——一台機一次只能執行一個任務',
                               c.drone_name, c.names);
          END IF;
          RETURN NULL;
        END $fn$ LANGUAGE plpgsql""")
    for tbl in ("mission_drones", "missions", "squad_members"):
        await pool.execute(
            f"DROP TRIGGER IF EXISTS trg_mission_drone_guard ON {tbl}")
        await pool.execute(
            f"CREATE CONSTRAINT TRIGGER trg_mission_drone_guard "
            f"AFTER INSERT OR UPDATE ON {tbl} "
            "DEFERRABLE INITIALLY IMMEDIATE "
            "FOR EACH ROW EXECUTE FUNCTION mission_drone_guard()")
    await pool.execute(
        "ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS mission_id UUID")
    # 名稱快照，與 plan_name 同一條理由：任務被刪掉之後，歷史仍要說得出
    # 當時屬於哪個任務
    await pool.execute(
        "ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS mission_name TEXT")
    # 刪任務不刪歷史（SET NULL）。ADD CONSTRAINT 沒有 IF NOT EXISTS，
    # 而 migrate() 每次啟動都跑——照既有慣例用 duplicate_object 包起來
    await pool.execute("""
        DO $$ BEGIN
          ALTER TABLE flight_sessions ADD CONSTRAINT flight_sessions_mission_id_fkey
            FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$""")

    # 錄製起訖條件（flight-video-design §8c，使用者定案 2026-09-08）。
    # `landed_state` 是飛控自己算的「我在地上還是空中」——**它原本只活在
    # 記憶體裡**，於是事後查不出「這一趟到底離地了沒」。
    await pool.execute(
        "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS landed_state TEXT")
    # 真正離地的區間。NULL 有兩種意思，靠 landed_state_seen 分辨：
    # 「確定沒離地」與「不知道有沒有離地」——後者不得觸發影像刪除
    for col, typ in (("airborne_from", "TIMESTAMPTZ"),
                     ("airborne_to", "TIMESTAMPTZ"),
                     ("landed_state_seen", "BOOLEAN NOT NULL DEFAULT false")):
        await pool.execute(
            f"ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS {col} {typ}")

    # **這筆記錄該是哪一家的自駕儀**（issues/038 的比對半邊，2026-09-02）。
    # 沒有它就沒有「期望值」可比：sysid 撞號時新來的機會直接繼承舊記錄，
    # 而廠牌從 ArduPilot 變成 PX4 這種明顯矛盾也沒有任何人看得出來。
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS autopilot INT")
    # ── 040 A1：sysid 由系統指派（2026-09-02 使用者裁定）────────────────
    # **`mav_sysid` 與 `assigned_sysid` 是兩件事，刻意分開兩欄**：
    # 前者是「這台機現在自報的號碼」（觀察到的事實），後者是「我們配給這塊板子
    # 的號碼」（我們的決定）。合成一欄就再也分不出「它跑錯號碼了」這件事。
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS assigned_sysid INT")
    # 一個號碼只能配給一塊板子。**用 partial unique index 而不是 UNIQUE 欄位**：
    # 還沒配號的機是 NULL，而 NULL 在 UNIQUE 下雖然可以重複，寫成部分索引更
    # 明確——它宣告的是「有配號的那些之間唯一」，正是我們要的規則
    await pool.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS drones_assigned_sysid_uniq "
        "ON drones (assigned_sysid) WHERE assigned_sysid IS NOT NULL")
    # 一次性回填：既有記錄若同時有板號與觀察到的號碼，那組配對是既成事實，
    # 直接登錄。**只回填不衝突的**——衝突的留給人處理，不要在遷移裡自動改號
    await pool.execute("""
        UPDATE drones d SET assigned_sysid = d.mav_sysid
        WHERE d.assigned_sysid IS NULL AND d.board_uid IS NOT NULL
          AND d.mav_sysid IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM drones o
                          WHERE o.assigned_sysid = d.mav_sysid)""")
    # 航線自帶的圍欄（QGC .plan 的 geoFence）。**圍欄是每份航線自己的事**，
    # 不是系統的全域設定——測繪任務與定點巡檢的合理範圍可以差一個數量級。
    # NULL＝這份 .plan 沒畫圍欄（退回系統預設，而且報告會說出用的是哪一個）
    await pool.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS fence JSONB")
    # QGC 的 plannedHomePosition [lat, lon, alt]。**RTL 沒有座標**——它的意思是
    # 「回到 home」，所以少了這個點，返航那一段在畫面上根本畫不出來，
    # 使用者會以為航線在最後一個航點就結束了（2026-08-26 使用者回報）。
    # 它同時也是距離量測該用的原點：起飛項在很多 .plan 裡是 0,0
    await pool.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS home JSONB")
    # .plan 宣告的速度，用來估預計時間。**沒宣告就不估**（不給預設值——
    # 猜一個看起來合理的數字，使用者會拿它安排電池）
    await pool.execute(
        "ALTER TABLE plans ADD COLUMN IF NOT EXISTS cruise_speed REAL")
    await pool.execute(
        "ALTER TABLE plans ADD COLUMN IF NOT EXISTS hover_speed REAL")
    # QGC 的 rallyPoints（緊急備降點）。**QGC 畫得出來、我們畫不出來，
    # 兩邊的圖就不一樣**——而使用者是拿這張圖來確認「機會怎麼飛」的
    await pool.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS rally JSONB")
    # 039/038 兩層身分的**人工維護那層**：機架序號與型號。
    # **不動 serial_no**——它現在扛著自動註冊的冪等性（四處 ON CONFLICT），
    # 改它的語意風險不對稱：那條路徑出錯會讓每次心跳都新增一筆機。
    await pool.execute(
        "ALTER TABLE drones ADD COLUMN IF NOT EXISTS airframe_serial TEXT")
    # 2026-09-09（issues/048 第 4 項）：槳徑。**它不是拿來算門檻的**——
    # 教科書的地效區是 1–2 倍槳徑，這台算出來 0.3–0.6 m，而 09-07 是在
    # 1.5 m 出事的，兩者對不上。填它的用途是讓「門檻 3 m 是怎麼來的」
    # 這件事在畫面上說得清楚：那是往外留的保守值，不是算出來的。
    await pool.execute(
        "ALTER TABLE drones ADD COLUMN IF NOT EXISTS prop_diameter_mm INT")
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
    # current_plan_id → missions 的參照完整性（ON DELETE SET NULL）：少了它，
    # 刪任務會讓 current_plan_id 變懸空指標，之後 create_session 綁 plan_id
    # 就撞 flight_sessions_mission_id_fkey → 解鎖建 session 每次拋錯 → 該機 armed
    # 永遠標不起來、不錄遙測（多機 bring-up 實測炸點：刪光飛行資料後殘留懸空
    # current_plan_id）。先清懸空值再補約束（冪等；約束不存在才加）。
    await pool.execute(
        """DO $$ BEGIN
             UPDATE drones d SET current_plan_id = NULL
               WHERE current_plan_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM plans m WHERE m.id = d.current_plan_id);
             IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname = 'drones_current_mission_id_fkey') THEN
               ALTER TABLE drones ADD CONSTRAINT drones_current_mission_id_fkey
                 FOREIGN KEY (current_plan_id) REFERENCES plans(id) ON DELETE SET NULL;
             END IF;
           END $$;""")
    # issue 014 STATUSTEXT Phase A：事件來源分類。'vehicle'＝自駕儀自己吐的 log
    # （STATUSTEXT，QGC vehicle-messages 面板同源）；'system'＝backend 推導的
    # （link_lost/cell_change/session…）。前端據此分「機上訊息」與「系統事件」兩流。
    await pool.execute(
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'system'")
    # 資訊頁（2026-09-07）：事件要能**按架次讀完**、也要能跨架次往回翻。
    # 原本只有 `idx_events_time`——問「這一趟發生了什麼」得掃全表，而事件表
    # 是全系統寫得最兇的一張。逐架次數事件（/sessions?with_events）走同一支索引
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_events_session "
                       "ON events (session_id, time)")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_events_drone_time "
                       "ON events (drone_id, time DESC)")
    # issue 013-A：群組任務資料模型（doc/group-missions-design.md）
    await pool.execute("""CREATE TABLE IF NOT EXISTS mission_groups (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        name TEXT NOT NULL,
        base_plan_id UUID REFERENCES plans(id),   -- unified 展開來源
        mode TEXT NOT NULL DEFAULT 'unified',            -- unified / separate
        params JSONB,                                    -- vsep_m/rtl_stagger_m 等
        status TEXT NOT NULL DEFAULT 'draft',            -- 見 §7.1 group.status
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute("""CREATE TABLE IF NOT EXISTS group_assignments (
        group_id UUID REFERENCES mission_groups(id) ON DELETE CASCADE,
        drone_id UUID NOT NULL,
        plan_id UUID REFERENCES plans(id),         -- materialized 具體任務
        layer_index INT NOT NULL DEFAULT 0,
        phase TEXT NOT NULL DEFAULT 'idle',              -- 見 §7.1 assignment.phase
        PRIMARY KEY (group_id, drone_id))""")
    await pool.execute("ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS group_id UUID")
    # ── 小隊＝常設編組（doc/squads-design.md，2026-09-08 使用者核准）────────
    #
    # **與 mission_groups 分開**：後者是一次群飛的執行實例（status／phase／
    # materialized 任務），一次飛行一筆、飛完就是歷史；小隊是跨飛行存在的名單。
    # 塞進同一張表會長出「status 永遠是 draft 的群組」，而且刪一次飛行紀錄
    # ＝刪掉編組——兩者的生命週期不同。
    await pool.execute("""CREATE TABLE IF NOT EXISTS squads (
        id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        name       TEXT NOT NULL,
        note       TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    # **名稱唯一（不分大小寫）**：小隊是拿來喊的（「等一下派 A 隊出去」），
    # 兩隊同名時畫面分得出（有 id）、人喊出來分不出
    await pool.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_squads_name "
                       "ON squads (lower(name))")
    # **多對多，不是 drones.squad_id**：同一台機在不同實驗扮不同角色是常事，
    # 一欄外鍵會逼人二選一。`position` 是顯示順序與 layer_index 的**預設種子**，
    # 不是 layer_index 本身——分層要看 vsep／航線高度／地形，那是派任務當下的決定
    await pool.execute("""CREATE TABLE IF NOT EXISTS squad_members (
        squad_id UUID NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
        drone_id UUID NOT NULL REFERENCES drones(id) ON DELETE CASCADE,
        position INT NOT NULL DEFAULT 0,
        PRIMARY KEY (squad_id, drone_id))""")
    # 這一次群飛是哪一隊派出去的。**ON DELETE SET NULL**：刪小隊不刪歷史；
    # 而 mission_groups.name 在派任務時就寫入當時的隊名快照，所以即使 FK 斷了，
    # 歷史仍說得出當時是哪一隊飛的（不另外加 squad_name 欄位）
    await pool.execute(
        "ALTER TABLE mission_groups ADD COLUMN IF NOT EXISTS squad_id UUID")
    await pool.execute("""DO $$ BEGIN
        ALTER TABLE mission_groups ADD CONSTRAINT mission_groups_squad_fk
          FOREIGN KEY (squad_id) REFERENCES squads(id) ON DELETE SET NULL;
    EXCEPTION WHEN duplicate_object THEN NULL; END $$;""")
    # issue 013-B：執行期即時態。phase 已在建表；補 error（異常態的
    # {msg,hint,autopilot_notes}，§7.1）與 updated_at（前端 1s 輪詢看新鮮度）。
    await pool.execute("ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS error JSONB")
    await pool.execute(
        "ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
    # 架次自訂備註（使用者要標實驗條件，如「開干擾器那趟」）：短文字，PATCH 可改
    await pool.execute("ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS note TEXT")
    # 架次為什麼結束。**「上鎖」與「我們看不到它了」是兩件事**：後者代表
    # 這筆記錄在那一刻之後就沒有資料，飛機可能還飛了很久。不分開的話，
    # 事後看架次會以為那趟飛行就是那麼長（2026-08-26：一筆架次開著 2.5 小時，
    # 而遙測在開始後 10 秒就斷了）
    await pool.execute(
        "ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS end_reason TEXT")
    # ── B 層：失明區間本身是一筆記錄 ───────────────────────────
    # 「我們沒看到的那段」原本是一個空洞，事後完全看不出來——回放會把缺口
    # 兩端的軌跡直接連起來，**那條直線是畫出來的謊**。把它變成一等公民：
    # 回放畫成斷點、架次摘要說得出「本趟有 N 秒沒有資料」
    await pool.execute("""CREATE TABLE IF NOT EXISTS blackouts (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        drone_id UUID NOT NULL REFERENCES drones(id) ON DELETE CASCADE,
        session_id UUID REFERENCES flight_sessions(id) ON DELETE SET NULL,
        started_at TIMESTAMPTZ NOT NULL,
        ended_at TIMESTAMPTZ,
        reason TEXT NOT NULL,
        armed_at_start BOOLEAN,
        recovered_by TEXT)""")
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_blackouts_drone_time "
        "ON blackouts (drone_id, started_at DESC)")
    # 補傳進來的遙測要標記出來（C 層）：它的時間戳是機上的，可能與地面站有
    # 偏差，而且它不該觸發任何即時判斷。**不標的話，事後分不出哪些是後補的**
    await pool.execute(
        "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS backfilled BOOLEAN "
        "NOT NULL DEFAULT false")
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
    await pool.execute("ALTER TABLE plans ADD COLUMN IF NOT EXISTS kind TEXT")
    await pool.execute("""
        UPDATE plans SET kind = CASE created_by
            WHEN 'plan-file'      THEN 'imported'      -- 使用者匯入 .plan
            WHEN 'vehicle'        THEN 'from-vehicle'  -- 從機上讀回
            WHEN 'group-gen'      THEN 'generated'     -- 編隊地面展開
            WHEN 'command-stage2' THEN 'generated'     -- 舊驗收測試的系統產物
            ELSE 'imported' END
        WHERE kind IS NULL""")
    # 架次的路徑名稱快照：使用者定案「飛過的路徑可以刪，但飛行紀錄要永遠存在」。
    # plan_id 是 ON DELETE SET NULL，刪路徑後回放頁只剩空白；留一份名字才能說
    # 「飛的是 X（路徑已刪除）」而不是什麼都說不出來。
    await pool.execute(
        "ALTER TABLE flight_sessions ADD COLUMN IF NOT EXISTS plan_name TEXT")
    await pool.execute("""
        UPDATE flight_sessions fs SET plan_name = m.name
        FROM plans m WHERE m.id = fs.plan_id AND fs.plan_name IS NULL""")
    # 兩處外鍵原為 NO ACTION：刪「被編隊引用過的路徑」會 FK 違反丟 500（實測復現），
    # 與「飛過的路徑可以刪」的定案直接衝突。改 SET NULL 讓它真的刪得掉。
    for tbl, col in (("group_assignments", "plan_id"),
                     ("mission_groups", "base_plan_id")):
        await pool.execute(f"""
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_constraint
                         WHERE conname = '{tbl}_{col}_fkey' AND confdeltype <> 'n') THEN
                ALTER TABLE {tbl} DROP CONSTRAINT {tbl}_{col}_fkey;
                ALTER TABLE {tbl} ADD CONSTRAINT {tbl}_{col}_fkey
                  FOREIGN KEY ({col}) REFERENCES plans(id) ON DELETE SET NULL;
              END IF;
            END $$;""")
    # 三個死欄位（從建表至今從未被寫入或讀取；遷移前以資料驗證過全為預設/NULL）。
    # 它們是照「任務規劃工具」設計的，但本專案刻意不做規劃（規劃留 QGC）。
    # drone_id 的唯一用途（刪機時清 NULL）已同批從 api.py 移除。
    for col in ("status", "geometry", "drone_id"):
        await pool.execute(f"ALTER TABLE plans DROP COLUMN IF EXISTS {col}")

    # ══ 事實來源：drones 是那張 metadata 表，其他人用 FK 指回來 ══════════
    # （2026-09-02 使用者裁定）**所有資料都要靠 DB 存；事實來源由一張 metadata
    # 表記得，其他人透過 UID 外鍵指回去查。**
    #
    # 在這之前，一半的關聯是**沒有外鍵的裸 `drone_id` 欄**——資料庫不保證，
    # 只靠 `delete_drone` 記得一張張刪。實測後果：**284 筆事件指向 22 台已經
    # 不存在的機**（2026-09-02 清掉）。漏一張表就長孤兒，而孤兒不會叫。

    # ① 板號是唯一鍵——**現在由資料庫保證，不再只是一句宣稱**。
    # issues/040 從一開始就寫「唯一鍵值是板號」，但 schema 上從來沒有這個約束：
    # 2026-09-02 實測 `ON CONFLICT (board_uid)` 直接報錯，因為根本沒有索引。
    # 部分索引（board_uid IS NOT NULL）：**還沒問到板號的機不該被這條擋住**，
    # 而它們可以有很多台。
    await pool.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS drones_board_uid_uniq "
        "ON drones (board_uid) WHERE board_uid IS NOT NULL")

    # ② 每一張帶 drone_id 的表都掛上外鍵，刪機由資料庫連帶清乾淨。
    # telemetry／link_metrics 是 TimescaleDB hypertable——**hypertable 指出去的
    # 外鍵是支援的**（2.29.1 實測），不支援的是反過來指進 hypertable。
    #
    # **掛不上就大聲說，不要自己刪資料。** 別人的資料庫可能有我們不知道的
    # 孤兒列，而「啟動時安靜地刪掉一批列」是這個專案不能接受的行為。
    for tbl in ("telemetry", "link_metrics", "events", "flight_sessions"):
        try:
            await pool.execute(f"""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                                 WHERE conname = '{tbl}_drone_id_fkey'
                                   AND confdeltype = 'c') THEN
                    IF EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname = '{tbl}_drone_id_fkey') THEN
                      ALTER TABLE {tbl} DROP CONSTRAINT {tbl}_drone_id_fkey;
                    END IF;
                    ALTER TABLE {tbl} ADD CONSTRAINT {tbl}_drone_id_fkey
                      FOREIGN KEY (drone_id) REFERENCES drones(id) ON DELETE CASCADE;
                  END IF;
                END $$;""")
        except Exception as e:
            # 幾乎一定是孤兒列。**把查法一起印出來**——「掛不上」這句話本身
            # 沒有用，要說得出「哪幾列擋著、怎麼看」
            log.error(
                "⚠ %s.drone_id 的外鍵掛不上（%s）。多半是孤兒列——用這句查：\n"
                "  SELECT count(*) FROM %s x WHERE x.drone_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM drones d WHERE d.id = x.drone_id);\n"
                "**在它掛上之前，刪機不會連帶清掉這張表**", tbl, e, tbl)

    # ③ 指令紀錄的鍵原本只有 sysid——**而 sysid 是會被重新配號的**（issues/040）。
    # 一旦某台機換過號碼，歷史指令就會指向**現在持有那個號碼的另一台機**。
    # 補一個 drone_id 外鍵，往後由寫入端解析。
    # **舊資料不回填**：拿今天的 sysid 去反推當時是誰，正是這個欄位要防的錯誤——
    # 空著代表「不知道」，那是實話。
    await pool.execute("ALTER TABLE command_log ADD COLUMN IF NOT EXISTS drone_id UUID")
    await pool.execute("""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint
                         WHERE conname = 'command_log_drone_id_fkey') THEN
            ALTER TABLE command_log ADD CONSTRAINT command_log_drone_id_fkey
              FOREIGN KEY (drone_id) REFERENCES drones(id) ON DELETE SET NULL;
          END IF;
        END $$;""")

    # ④ 指令與「哪一趟飛行」之間原本沒有任何欄位相連（2026-09-06）。
    # 只剩 sysid＋時間戳，要對起來得假設「時間落在 started_at/ended_at 之間，
    # 且 sysid 對得上那台機」——**而 sysid 正是會被重新配號的那個東西**。
    # 於是「這趟飛行我下了什麼、系統擋了我幾次」在匯出檔裡等於不存在。
    #
    # **舊資料一樣不回填**（同 ③ 的理由）：session 是從 drone_id＋時間推出來的，
    # 而歷史列的 drone_id 就是空的。拿今天的 sysid 去反推當時是誰，正是這兩個
    # 欄位要防的錯誤。空著代表「不知道」——那是實話，猜出來的不是。
    await pool.execute(
        "ALTER TABLE command_log ADD COLUMN IF NOT EXISTS session_id UUID")
    await pool.execute("""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint
                         WHERE conname = 'command_log_session_id_fkey') THEN
            ALTER TABLE command_log ADD CONSTRAINT command_log_session_id_fkey
              FOREIGN KEY (session_id) REFERENCES flight_sessions(id)
              ON DELETE SET NULL;
          END IF;
        END $$;""")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_command_log_session "
                       "ON command_log (session_id, time)")

    # ⑤ 電流與累積消耗（2026-09-07）。**原本只存在於 014 的原始層**——
    # `telemetry` 只有電壓與百分比，於是「待機能撐多久」「電流刻度準不準」
    # 只能去翻幾十 MB 的 tlog。而那個百分比正是飛控拿電流積分算出來的，
    # 對一個以電力與鏈路為研究核心的系統，缺這兩欄等於把推導過程丟掉、
    # 只留結論。
    await pool.execute(
        "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS battery_current REAL")
    await pool.execute(
        "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS battery_consumed_mah REAL")

    # ══ 大檔案：DB 記路徑，內容留在磁碟 ═══════════════════════════════
    # （2026-09-02 使用者裁定）**資料本身很大的時候，SQL 欄位記路徑，
    # 要內容再到那個路徑下去看。**
    #
    # 這張表管兩層錄製（issues/014）：`ground`＝地面站錄的「送到地面站的東西」、
    # `onboard`＝機上錄的「飛控送出的東西」。**兩者相差的就是 5G 斷線那一段**，
    # 所以 tier 是欄位不是兩張表——要能用一句 SQL 把兩層對起來。
    #
    # 原本機上那一層的 metadata 是**寫在磁碟上的 `.meta` JSON**，清單靠 glob。
    # 那等於把事實來源放在檔案系統裡：查不了、關聯不了、刪機時也不會連帶清。
    # 現在檔案還在原地（大），metadata 進 DB（小），彼此用 path 相連。
    #
    # `covers_from`／`covers_to`＝這份錄製涵蓋的時間，收尾驗 sha256 那一遍
    # 順手掃出來的。有了它，「地面站瞎掉的那一段機上補到了沒有」才是一句
    # 可以用 SQL 核對的話，而不是一句宣稱。
    await pool.execute("""CREATE TABLE IF NOT EXISTS captures (
        id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        drone_id    UUID REFERENCES drones(id) ON DELETE CASCADE,
        tier        TEXT NOT NULL,                    -- ground / onboard
        name        TEXT NOT NULL,                    -- 存起來的檔名
        onboard_name TEXT,                            -- 機上原本的檔名（撞名時不同）
        path        TEXT NOT NULL,                    -- **內容在這裡，不在 DB 裡**
        bytes       BIGINT NOT NULL DEFAULT 0,
        expected_bytes BIGINT,                        -- 機端宣告的大小（續傳用）
        sha256      TEXT,
        status      TEXT NOT NULL DEFAULT 'partial',  -- partial / complete / lost
        covers_from TIMESTAMPTZ,
        covers_to   TIMESTAMPTZ,
        frames      BIGINT,
        received_at TIMESTAMPTZ,
        lost_at     TIMESTAMPTZ,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now())""")
    # **唯一鍵要寫 `NULLS NOT DISTINCT`。** 地面站那一層的列沒有 drone_id
    # （那是整台地面站錄的，不屬於任何一台機），而在一般的唯一約束裡
    # **NULL 不等於 NULL**——於是 `ON CONFLICT` 永遠不成立，每對帳一次就
    # 多一份重複的列。實測：開機一次、按一次 /api/captures，11 個檔案變成
    # 22 列（PostgreSQL 15 起支援這個寫法，本機 16.14）。
    # 舊版建出來的普通唯一約束先拆掉（它就是上面那個 NULL 陷阱的來源）
    await pool.execute("ALTER TABLE captures DROP CONSTRAINT IF EXISTS "
                       "captures_tier_drone_id_name_key")
    # 先清掉舊約束造成的重複列（同 tier/name 只留最舊那一列）
    await pool.execute("""
        DELETE FROM captures a USING captures b
         WHERE a.tier = b.tier AND a.name = b.name
           AND a.drone_id IS NOT DISTINCT FROM b.drone_id
           AND a.created_at > b.created_at""")
    await pool.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS captures_key "
        "ON captures (tier, drone_id, name) NULLS NOT DISTINCT")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_captures_drone_time "
                       "ON captures (drone_id, covers_from DESC)")
    # 續傳認的是 sha256 不是檔名（機上的 RTC 沒有電池，冷開機檔名會重複）
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_captures_sha "
                       "ON captures (tier, drone_id, sha256)")


async def ensure_drone_by_board(board_uid: str, *, autopilot: str | None = None,
                                fw: str | None = None,
                                agent_uid: str | None = None,
                                vehicle_type: int | None = None,
                                claimed_sysid: int | None = None) -> tuple[str, str, bool]:
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
    if row is None and claimed_sysid is not None:
        # **收養佔位記錄，不要長出第二筆**（issues/040 A4）。一台機通常先用
        # MAVLink 出現（`drone_for_sysid` 建 `uav-s{sysid}` 佔位），代理稍後才
        # 帶著板號來註冊。兩條路各建一筆的話，**同一台飛機會有兩筆記錄，
        # 而遙測在其中一筆、身分在另一筆**——那比沒有身分更難查。
        #
        # **只收養沒有板號的**：有板號的那筆已經有身分了，蓋掉它等於把兩台機
        # 併成一台（09-01 的 SITL 事件就是這種合併的鏡像）。
        row = await pool.fetchrow(
            "UPDATE drones SET board_uid = $2 WHERE mav_sysid = $1 "
            "AND board_uid IS NULL RETURNING id::text AS id, name",
            claimed_sysid, board_uid)
        if row:
            log.info("板號 %s 收養既有的佔位記錄 %s（sysid %d）",
                     board_uid[-6:], row["name"], claimed_sysid)
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


async def load_board_identity(
        drone_id: str | None) -> tuple[str | None, str | None, int | None]:
    """從 DB 取回這台機上次記錄的身分（uid, 韌體版本, 自駕儀廠牌）。

    **給 backend 重啟後回填 LiveState 用。** 沒有這一步的話：值在 DB 裡、
    畫面上卻是空的，而且不會自己好——請求 AUTOPILOT_VERSION 的是 command
    服務，它的「已問過」旗標不隨 backend 重啟而清除。

    **回填同時也是「期望值」**（issues/038，2026-09-02）：新接上的機若與這裡
    記的對不上，那就不是同一台。2026-09-01 的實例：PX4 SITL 用 sysid 1 連上，
    直接繼承了一台 ArduPilot 真機的記錄，而**回填這一步把真機的 board_uid
    填到了模擬器的狀態上**——身分鏈被我們自己接反了。
    """
    if not drone_id:
        return None, None, None
    row = await pool.fetchrow(
        "SELECT board_uid, flight_sw_version, autopilot FROM drones WHERE id = $1::uuid",
        drone_id)
    return ((row["board_uid"], row["flight_sw_version"], row["autopilot"])
            if row else (None, None, None))


async def set_autopilot(drone_id: str | None, raw: int) -> None:
    """第一次認得這台機是哪一家時記下來。**只在原本是 NULL 時寫**——
    有值之後它就是期望值，覆蓋它等於把守門自己關掉。"""
    if not drone_id or raw is None:
        return
    await pool.execute(
        "UPDATE drones SET autopilot = $2 WHERE id = $1::uuid AND autopilot IS NULL",
        drone_id, raw)


#: 號碼池：MAVLink 的 sysid 值域是 1–255，而 **255 是我方地面站**
#: （`command/app/mav.py` 的 `GCS_SYSID`）。0 是廣播位址，不是誰的號碼。
SYSID_MIN, SYSID_MAX, SYSID_GCS = 1, 254, 255


async def allocate_sysid(board_uid: str,
                         claimed: int | None) -> dict:
    """配號（issues/040 A1，2026-09-02 使用者裁定）。

    **識別的唯一鍵值是板號**；`sysid` 只是地址。所以撞號不是「要隔離誰」的
    兩難——**地址撞了就換一個**。四種情況：

    | 板號 | 它現在用的號碼 | 回傳的 action |
    |---|---|---|
    | 已登錄 | 就是配給它的 | `keep` |
    | 已登錄 | 不是配給它的 | `change`（改回登錄上那個）|
    | 沒見過 | 沒人用 | `keep`，並把這個號碼登錄給它 |
    | 沒見過 | **已被別的板號佔用** | `change`（配一個新的）|

    **本函式只決定號碼，不下發、不寫飛控**——下發是 A3、由代理執行。
    A1 階段呼叫端拿到 `change` 只需要顯示與留痕。

    號碼不自動回收：釋放（把 `assigned_sysid` 設回 NULL）必須是明確的動作。
    **一個剛釋放又立刻被配給別台機的號碼，會讓歷史資料的歸屬無法回溯**——
    同一個號碼在時間軸上指過兩台不同的飛機，而遙測只記得號碼。
    """
    row = await pool.fetchrow(
        "SELECT id::text AS id, name, assigned_sysid FROM drones "
        "WHERE board_uid = $1", board_uid)
    if row and row["assigned_sysid"] is not None:
        want = row["assigned_sysid"]
        return {"sysid": want, "action": "keep" if claimed == want else "change",
                "drone_id": row["id"], "drone": row["name"],
                "reason": ("號碼與登錄相符" if claimed == want else
                           f"這塊板子登錄的號碼是 {want}，但它現在用 {claimed}")}

    # 板號沒登錄過（或登錄了但還沒配號）
    if claimed is not None and SYSID_MIN <= claimed <= SYSID_MAX:
        taken_by = await pool.fetchrow(
            "SELECT board_uid FROM drones WHERE assigned_sysid = $1", claimed)
        if taken_by is None:
            # 它現在用的號碼沒人要 → 就配這個，機端完全不用動
            return {"sysid": claimed, "action": "keep",
                    "drone_id": row["id"] if row else None,
                    "drone": row["name"] if row else None,
                    "reason": "這個號碼沒有配給任何板子，直接登錄給它"}
        conflict = taken_by["board_uid"]
    else:
        conflict = None

    free = await _next_free_sysid()
    if free is None:
        # **號碼用完要明說，不要靜默給一個重複的**。254 台機是很大的機隊，
        # 走到這裡幾乎一定是號碼沒回收，而不是真的養了那麼多台
        raise RuntimeError(
            f"sysid 號碼池已用盡（{SYSID_MIN}–{SYSID_MAX} 全數配出）。"
            "退役的機請明確釋放號碼（assigned_sysid 設回 NULL）")
    why = (f"它現在用的 {claimed} 已經配給板號 {conflict}" if conflict
           else f"它沒有自報有效號碼（claimed={claimed}）")
    return {"sysid": free, "action": "change",
            "drone_id": row["id"] if row else None,
            "drone": row["name"] if row else None,
            "reason": f"{why}，改配 {free}"}


async def _next_free_sysid() -> int | None:
    """取一個沒被登錄的號碼。**由小往大取**：可預測比隨機好——
    現場報號碼、翻查核表都靠人眼，而人眼對連號比對亂數在行。"""
    rows = await pool.fetch(
        "SELECT assigned_sysid FROM drones WHERE assigned_sysid IS NOT NULL")
    taken = {r["assigned_sysid"] for r in rows} | {SYSID_GCS}
    for n in range(SYSID_MIN, SYSID_MAX + 1):
        if n not in taken:
            return n
    return None


async def record_assignment(board_uid: str, sysid: int,
                            drone_id: str | None = None) -> None:
    """把配號寫進登錄。**同一塊板子改號時要先把舊的空出來**，否則部分唯一索引
    會擋住——而那個擋是對的：兩塊板子不能共用一個號碼。"""
    if drone_id:
        await pool.execute(
            "UPDATE drones SET assigned_sysid = $2 WHERE id = $1::uuid",
            drone_id, sysid)
    else:
        await pool.execute(
            "UPDATE drones SET assigned_sysid = $2 WHERE board_uid = $1",
            board_uid, sysid)


async def set_board_uid(drone_id: str | None, uid: str,
                        fw: str | None = None) -> None:
    """記下這筆記錄目前對應的飛控板。

    **原本這裡只記錄、不比對、不擋**，理由是 uid2 在同一塊板子上跨重開機／
    韌體升級穩不穩定還沒有真實資料，沒驗證過就加告警只會製造假警報。
    那個顧慮**現在仍然成立**，所以 uid 不合只示警、不擋（見 mavlink_rx 的
    `_identity_guard`）——但**不再覆蓋**：覆蓋等於把唯一的期望值抹掉，
    之後永遠比不出來。廠牌不合才是硬擋的那一條（那個訊號沒有穩定性疑慮：
    一台機不會重開機之後從 ArduPilot 變成 PX4）。
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
    2. **配號登錄說這個號碼是配給某塊板子的 → 用那筆**（issues/040 A4）
    3. 都不是 → 自動建檔（uav-s{sysid}，之後無人機頁改名）

    ## 第 2 步 2026-09-02 換掉了，因為原本那條認錯過機

    原本寫的是「**主機**還沒認領 sysid → 認領給主機」。2026-08-24 實際出事：
    一筆早已停用的舊記錄仍是主機、`mav_sysid` 空著，新接上的機一開機就被認領
    進那筆記錄，`/api/live` 顯示的是**別台機的名字**——而全程只有一行 log。

    **問題不在那條規則寫錯，在它用錯了鍵**：`is_primary` 是「哪一台是主要顯示
    對象」，它從來就不是身分。用它來回答「這個號碼是誰的」，等於拿一個排版設定
    去做身分判斷。

    現在改用**配號登錄**（`assigned_sysid`）：那張表的鍵是板號，而板號是唯一
    穩定的身分（2026-09-02 使用者裁定）。**認領因此以板號為鍵，只是繞了一層
    號碼**——而那一層正是我們自己配的，不是機端自報的。
    """
    row = await pool.fetchrow(
        "SELECT id::text AS id, name FROM drones WHERE mav_sysid = $1", sysid)
    if row:
        return row["id"], row["name"]
    row = await pool.fetchrow(
        "SELECT id::text AS id, name FROM drones "
        "WHERE assigned_sysid = $1 AND board_uid IS NOT NULL", sysid)
    if row:
        # **配號登錄說這個號碼是它的**，所以這是它回來了，不是一台新機
        await pool.execute("UPDATE drones SET mav_sysid = $2 WHERE id = $1::uuid",
                           row["id"], sysid)
        log.info("sysid %d 依配號登錄歸給 %s（板號為鍵）", sysid, row["name"])
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
                         plan_id: str | None = None) -> str:
    """開一條航線紀錄。plan_id 指定時直接關聯（群飛模擬飛指定任務）；
    否則 link_mission=True 時關聯任務庫當下的啟用路徑（is_active）——
    語意是「操作員宣告要飛的那條」。回放頁據此疊預計路徑。"""
    # 綁定序（issue 020，任務↔架次因果鏈）：明示 plan_id > 該機當前任務
    # （command 上傳時設 drones.current_plan_id，可靠事實源）> is_active 後備
    # 前向 origin 標記：該機 sysid 近 60s 有測試類 client（rig/test/acceptance）的
    # command_log → 'test'；否則 NULL（＝unknown，可由 backfill 再判）。用 command_log
    # 相關性、不做跨服務 drone 欄位 plumbing（會 racy）。與 backfill 同一判準。
    # plan_name 快照（023）：路徑可被刪除（FK 是 SET NULL），但「飛行紀錄要
    # 永遠存在」——留一份當下的名字，刪掉路徑後回放頁仍能說「飛的是 X（路徑已
    # 刪除）」而不是一片空白。用 CTE 解一次 plan_id 再取名，避免把上面那串
    # COALESCE 抄第二遍（抄兩遍遲早會分岔）。
    row = await pool.fetchrow(
        """WITH resolved AS (
             SELECT COALESCE(
               $3::uuid,
               (SELECT current_plan_id FROM drones WHERE id = $1),
               CASE WHEN $2 THEN (SELECT id FROM plans WHERE is_active LIMIT 1) END
             ) AS mid
           )
           INSERT INTO flight_sessions
             (drone_id, started_at, plan_id, plan_name, origin,
              mission_id, mission_name)
           SELECT $1, now(), r.mid,
                  (SELECT name FROM plans WHERE id = r.mid),
                  (CASE WHEN EXISTS (
                       SELECT 1 FROM command_log c
                       WHERE c.sysid = (SELECT mav_sysid FROM drones WHERE id = $1)
                         AND c.client ~* '(rig|test|acceptance)'
                         AND c.time > now() - interval '60 seconds'
                   ) THEN 'test' END),
                  -- **這台機參與中的那個任務自動接手這一趟**（§4.6）：
                  -- 人在起飛時決定過一次「誰在跑哪個任務」，之後每一趟不必
                  -- 再問。有效名單＝直接綁的機 ∪ 綁的小隊的成員；
                  -- **恰好一個**是資料庫的不變式保證的（mission_drone_guard），
                  -- 所以這裡不必處理「兩個」。名字存快照，理由同 plan_name
                  (SELECT m.id FROM missions m WHERE m.ended_at IS NULL
                     AND $1::uuid IN (
                       SELECT md.drone_id FROM mission_drones md
                        WHERE md.mission_id = m.id
                       UNION
                       SELECT sm.drone_id FROM squad_members sm
                        WHERE sm.squad_id = m.squad_id) LIMIT 1),
                  (SELECT m.name FROM missions m WHERE m.ended_at IS NULL
                     AND $1::uuid IN (
                       SELECT md.drone_id FROM mission_drones md
                        WHERE md.mission_id = m.id
                       UNION
                       SELECT sm.drone_id FROM squad_members sm
                        WHERE sm.squad_id = m.squad_id) LIMIT 1)
           FROM resolved r
           RETURNING id""",
        drone_id, link_mission, plan_id,
    )
    return str(row["id"])


async def end_session(session_id: str, reason: str = "disarmed") -> None:
    """關閉架次並統計本次的飛行與鏈路摘要（干擾研究常看的統計先算好）。

    `reason`：`disarmed`＝看到機體上鎖（正常結束）；`telemetry_lost`＝**我們
    看不到它了**。後者不代表飛行結束——飛機可能還飛了很久，只是我們沒有資料。
    """
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
    # **失去遙測時，結束時間用最後一筆資料的時間**，不是「現在」。
    # 用 now() 會讓架次長度包含我們什麼都沒看到的那一大段——那不是飛行時間，
    # 是我們的失明時間。兩者混在一起，事後的架次統計全部失真
    ended = "now()" if reason == "disarmed" else (
        "coalesce((SELECT max(time) FROM telemetry WHERE session_id = $1), now())")
    await pool.execute(
        f"UPDATE flight_sessions SET ended_at = {ended}, summary = $2, "
        "end_reason = $3 WHERE id = $1",
        session_id,
        jdumps({k: (float(v) if v is not None else None) if k not in ("samples_in_zone", "samples_total") else int(v or 0) for k, v in dict(summary).items()}),
        reason,
    )


async def insert_telemetry(s: LiveState) -> None:
    await pool.execute(
        """
        INSERT INTO telemetry (time, drone_id, session_id, lat, lon, alt_msl, alt_rel,
          heading, ground_speed, vertical_speed, battery_pct, battery_voltage,
          gps_fix, satellites, flight_mode, armed,
          battery_current, battery_consumed_mah, landed_state)
        VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                $16, $17, $18)
        """,
        s.drone_id, s.session_id, s.lat, s.lon, s.alt_msl, s.alt_rel,
        s.heading, s.ground_speed, s.vertical_speed, s.battery_pct, s.battery_voltage,
        s.gps_fix, s.satellites, s.flight_mode, s.armed,
        s.battery_current, s.battery_consumed_mah,
        # 飛控自己算的「在地上還是空中」。**NULL＝那一秒沒收到**，
        # 不是「在地上」（flight-video-design §8c）
        s.landed_state,
    )


async def mark_airborne(session_id: str, first: bool) -> None:
    """記下這一趟真正離地的區間。`first` ＝這是起點（否則是終點）。

    **起點只寫一次**（`COALESCE` 不覆蓋），終點每次覆蓋——一趟可能起降好幾次，
    我們要的是「第一次離地」到「最後一次落地」。
    只在**狀態轉換的那一刻**呼叫，不是每一則 `EXTENDED_SYS_STATE` 都呼叫
    （它 4 Hz，逐則寫等於每秒四次無謂的 UPDATE）。
    """
    sql = ("UPDATE flight_sessions SET airborne_from = COALESCE(airborne_from, now()), "
           "landed_state_seen = true WHERE id = $1") if first else (
          "UPDATE flight_sessions SET airborne_to = now(), "
          "landed_state_seen = true WHERE id = $1")
    await pool.execute(sql, session_id)


async def mark_landed_seen(session_id: str) -> None:
    """這一趟收到過 `landed_state`。**與「有沒有離地」是兩件事**：
    收到過而且一直是 on_ground＝確定沒飛；從沒收到過＝不知道。 """
    await pool.execute(
        "UPDATE flight_sessions SET landed_state_seen = true WHERE id = $1", session_id)


async def airborne_of_session(session_id: str) -> dict | None:
    """這一趟飛過沒有。回傳 None＝查不到那一趟。"""
    row = await pool.fetchrow(
        "SELECT airborne_from, airborne_to, landed_state_seen, video_mode, end_reason "
        "FROM flight_sessions WHERE id = $1", session_id)
    return dict(row) if row else None


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


#: 嚴重度的合法值。**`warn` 與 `warning` 曾經兩種都寫進去過**（2026-09-07
#: 實測：292 列是 `warn`），而前端的對照表只認 `warning`——查不到的鍵退回
#: 灰色的「資訊」，於是那 292 則警告在畫面上長得跟正常事件一模一樣
#: （ui-spec §0.2b：非正常不得冒充正常）。**正規化放在唯一的寫入點**，
#: 呼叫端寫哪一種都不會再分岔。歷史那 292 列不改——改寫既有事件等於改寫
#: 紀錄；讀的那一端自己認得舊值（前端 lib/severity.ts）。
SEVERITY_ALIASES = {"warn": "warning"}


async def insert_event(drone_id: str, session_id: str | None,
                       severity: str, type_: str, detail: dict,
                       source: str = "system") -> dict:
    severity = SEVERITY_ALIASES.get(severity, severity)
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




# ── B 層：失明區間 ──────────────────────────────────────────────────────

async def blackout_open(drone_id: str, session_id: str | None, reason: str,
                        armed: bool | None, started_at=None) -> str | None:
    """開一筆失明記錄。回傳 id（開不了回 None——記錄失敗不該拖垮資料路徑）。

    `started_at` 給**最後一次收到資料的時間**，不是「發現失聯的時間」——
    兩者差一個逾時門檻，而那段時間我們其實也沒有資料。
    """
    try:
        row = await pool.fetchrow(
            "INSERT INTO blackouts (drone_id, session_id, started_at, reason, "
            "armed_at_start) VALUES ($1::uuid, $2::uuid, "
            "coalesce($3, now()), $4, $5) RETURNING id::text AS id",
            drone_id, session_id, started_at, reason, armed)
        return row["id"]
    except Exception:
        log.exception("失明記錄開啟失敗（不影響資料路徑）")
        return None


async def blackout_close(blackout_id: str, recovered_by: str) -> None:
    """收一筆失明記錄。`recovered_by`：telemetry_resumed／backfilled／giving_up。"""
    try:
        await pool.execute(
            "UPDATE blackouts SET ended_at = now(), recovered_by = $2 "
            "WHERE id = $1::uuid AND ended_at IS NULL",
            blackout_id, recovered_by)
    except Exception:
        log.exception("失明記錄收尾失敗（不影響資料路徑）")


async def blackouts_for_session(session_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id::text AS id, started_at, ended_at, reason, armed_at_start, "
        "recovered_by, extract(epoch FROM coalesce(ended_at, now()) - started_at) "
        "AS seconds FROM blackouts WHERE session_id = $1::uuid ORDER BY started_at",
        session_id)
    return [dict(r) for r in rows]
