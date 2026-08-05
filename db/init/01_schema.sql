-- UAV System schema
-- PostgreSQL + TimescaleDB
-- 研究重點：5G 鏈路品質 × 空間位置（干擾場域）

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- 機隊註冊（靜態）
-- ============================================================
CREATE TABLE drones (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name           TEXT NOT NULL,
  model          TEXT,
  serial_no      TEXT UNIQUE,
  is_simulated   BOOLEAN NOT NULL DEFAULT true,
  connection_url TEXT,                 -- e.g. 'udpin://0.0.0.0:14540'
  status         TEXT NOT NULL DEFAULT 'offline',  -- offline / idle / in_mission / maintenance
  is_primary     BOOLEAN NOT NULL DEFAULT false,   -- MAVLink 主機（至多一台，系統端指定）
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_drones_one_primary ON drones ((true)) WHERE is_primary;

-- ============================================================
-- 5G 基地台（gNB）：模擬鏈路品質的訊號源，之後也可存實網 cell 資訊
-- ============================================================
CREATE TABLE cells (
  id      SERIAL PRIMARY KEY,
  name    TEXT NOT NULL,
  lat     DOUBLE PRECISION NOT NULL,
  lon     DOUBLE PRECISION NOT NULL,
  pci     INT NOT NULL,               -- physical cell id
  band    TEXT NOT NULL DEFAULT 'n78',
  tx_power_dbm REAL NOT NULL DEFAULT 40
);

-- ============================================================
-- 干擾區域：研究場景的核心設定。模擬器據此劣化鏈路品質；
-- 真機階段保留此表作為「已知干擾源」的標注。
-- ============================================================
CREATE TABLE interference_zones (
  id           SERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  center_lat   DOUBLE PRECISION NOT NULL,
  center_lon   DOUBLE PRECISION NOT NULL,
  radius_m     REAL NOT NULL,
  severity_db  REAL NOT NULL,          -- 區內 SINR 額外衰減量 (dB)
  enabled      BOOLEAN NOT NULL DEFAULT true,
  note         TEXT
);

-- ============================================================
-- 飛行架次：armed → disarmed 為一個 session
-- ============================================================
CREATE TABLE flight_sessions (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  drone_id   UUID NOT NULL REFERENCES drones(id),
  mission_id UUID,                   -- 開航線時任務庫的啟用路徑（操作員宣告要飛的那條）
                                       -- 回放頁據此疊出當時的預計路徑；手飛為 NULL
                                       -- （FK 在 missions 建表後以 ALTER 補上，見檔尾）
  started_at TIMESTAMPTZ NOT NULL,
  ended_at   TIMESTAMPTZ,
  summary    JSONB                     -- 落地後計算：航程、最大高度、SINR 統計等
);
CREATE INDEX idx_sessions_drone ON flight_sessions (drone_id, started_at DESC);

-- ============================================================
-- 飛行遙測（hypertable）
-- ============================================================
CREATE TABLE telemetry (
  time           TIMESTAMPTZ NOT NULL,
  drone_id       UUID NOT NULL,
  session_id     UUID,
  lat            DOUBLE PRECISION,
  lon            DOUBLE PRECISION,
  alt_msl        REAL,
  alt_rel        REAL,
  heading        REAL,
  ground_speed   REAL,
  vertical_speed REAL,
  battery_pct    REAL,
  battery_voltage REAL,
  gps_fix        SMALLINT,
  satellites     SMALLINT,
  flight_mode    TEXT,
  armed          BOOLEAN,
  raw            JSONB
);
SELECT create_hypertable('telemetry', 'time');
CREATE INDEX idx_telemetry_session ON telemetry (session_id, time);
CREATE INDEX idx_telemetry_drone ON telemetry (drone_id, time DESC);

-- ============================================================
-- 5G 鏈路品質（hypertable）— 本研究的核心資料表
-- 位置欄位刻意反正規化：分析「訊號 × 空間」時不必和 telemetry 做時間 join
-- ============================================================
CREATE TABLE link_metrics (
  time      TIMESTAMPTZ NOT NULL,
  drone_id  UUID NOT NULL,
  session_id UUID,
  -- 量測當下位置（供空間分析／熱度圖直接使用）
  lat       DOUBLE PRECISION,
  lon       DOUBLE PRECISION,
  alt_rel   REAL,
  -- RF 層指標
  rsrp      REAL,                      -- dBm, 參考訊號接收功率
  rsrq      REAL,                      -- dB
  sinr      REAL,                      -- dB, 訊號干擾雜訊比（干擾研究主指標）
  cqi       SMALLINT,
  pci       INT,                       -- Physical Cell ID（會重複使用，僅鄰區內唯一）
  cell_id   BIGINT,                    -- modem 回報的全域 cell 識別碼 (NCI/CGI)，
                                       -- 用來消除 PCI 重複。模擬資料為 NULL
  band      TEXT,
  nr_mode   TEXT,                      -- SA / NSA / LTE
  -- 端到端鏈路指標
  rtt_ms    REAL,
  jitter_ms REAL,
  packet_loss_pct REAL,
  throughput_up_kbps   REAL,
  throughput_down_kbps REAL,
  in_interference_zone BOOLEAN,        -- 量測當下是否位於已標注干擾區內
  source    TEXT NOT NULL DEFAULT 'simulated',  -- simulated / modem
  raw       JSONB
);
SELECT create_hypertable('link_metrics', 'time');
CREATE INDEX idx_link_session ON link_metrics (session_id, time);
CREATE INDEX idx_link_drone ON link_metrics (drone_id, time DESC);
-- 去重：真機階段機上以批次重送補傳資料，屬 at-least-once 投遞，同一批可能送達兩次。
-- (drone_id, time) 是天然鍵，搭配 INSERT ... ON CONFLICT DO NOTHING 達成冪等。
-- hypertable 的唯一索引必須包含分區欄位 time，此處已滿足。見 doc/onboard-telemetry.md。
CREATE UNIQUE INDEX idx_link_dedup ON link_metrics (drone_id, time);

-- ============================================================
-- 任務與航點
-- ============================================================
CREATE TABLE missions (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name       TEXT NOT NULL,
  drone_id   UUID REFERENCES drones(id),
  status     TEXT NOT NULL DEFAULT 'draft',  -- draft / uploaded / running / completed / aborted
  is_active  BOOLEAN NOT NULL DEFAULT false,  -- 顯示於即時頁的那一條（至多一條）
  geometry   JSONB,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE waypoints (
  mission_id UUID REFERENCES missions(id) ON DELETE CASCADE,
  seq        INT,
  lat DOUBLE PRECISION, lon DOUBLE PRECISION, alt REAL,
  action     TEXT DEFAULT 'waypoint',  -- takeoff / waypoint / hover / photo / land / rtl
  params     JSONB,
  PRIMARY KEY (mission_id, seq)
);

-- ============================================================
-- 事件/告警（含鏈路事件：link_degraded / link_lost / link_recovered）
-- ============================================================
CREATE TABLE events (
  id         BIGSERIAL PRIMARY KEY,
  time       TIMESTAMPTZ NOT NULL DEFAULT now(),
  drone_id   UUID,
  session_id UUID,
  severity   TEXT NOT NULL,            -- info / warning / critical
  type       TEXT NOT NULL,            -- low_battery / gps_lost / link_degraded / link_lost / link_recovered / mode_change
  detail     JSONB,
  acked_at   TIMESTAMPTZ
);
CREATE INDEX idx_events_time ON events (time DESC);

-- ============================================================
-- Seed：PX4 SITL 預設起飛點在蘇黎世 (47.397742, 8.545594)。
-- 放一個 gNB 和一個干擾區，起飛往北飛約 200m 就會進入干擾區，
-- 開箱即可展示「飛入干擾區 → SINR 驟降」。
-- ============================================================
INSERT INTO cells (name, lat, lon, pci, band) VALUES
  ('sim-gnb-1', 47.3970, 8.5450, 101, 'n78'),
  ('sim-gnb-2', 47.4010, 8.5490, 205, 'n78');

INSERT INTO interference_zones (name, center_lat, center_lon, radius_m, severity_db, note) VALUES
  ('sim-jammer-A', 47.3995, 8.5456, 120, 25, '模擬強干擾源：區內 SINR -25dB');

ALTER TABLE flight_sessions
  ADD CONSTRAINT fk_sessions_mission
  FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE SET NULL;

-- ============================================================
-- 資料生命週期（2026-08-04 定案）：
--   原始 1Hz 資料保留 30 天（retention policy 自動清除）；
--   1 分鐘彙總永久保留；要長期保留原始資料先用匯出功能
--   （GET /api/sessions/{id}/export，匯出後可 DELETE 移除）。
-- ============================================================
CREATE MATERIALIZED VIEW link_metrics_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', time) AS bucket, drone_id, session_id,
       avg(sinr) AS avg_sinr, min(sinr) AS min_sinr, max(sinr) AS max_sinr,
       avg(rsrp) AS avg_rsrp, avg(rtt_ms) AS avg_rtt_ms,
       avg(packet_loss_pct) AS avg_loss_pct, count(*) AS samples
FROM link_metrics GROUP BY 1, 2, 3 WITH NO DATA;

CREATE MATERIALIZED VIEW telemetry_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', time) AS bucket, drone_id, session_id,
       avg(alt_rel) AS avg_alt_rel, max(alt_rel) AS max_alt_rel,
       avg(ground_speed) AS avg_speed, min(battery_pct) AS min_battery_pct,
       count(*) AS samples
FROM telemetry GROUP BY 1, 2, 3 WITH NO DATA;

SELECT add_continuous_aggregate_policy('link_metrics_1m',
  start_offset => INTERVAL '2 hours', end_offset => INTERVAL '1 minute',
  schedule_interval => INTERVAL '10 minutes');
SELECT add_continuous_aggregate_policy('telemetry_1m',
  start_offset => INTERVAL '2 hours', end_offset => INTERVAL '1 minute',
  schedule_interval => INTERVAL '10 minutes');

SELECT add_retention_policy('link_metrics', INTERVAL '30 days');
SELECT add_retention_policy('telemetry',    INTERVAL '30 days');
