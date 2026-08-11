-- 回填 flight_sessions.origin（測試殘留混研究庫的治理；PM 定案：標記不刪除）。
--
-- 冪等：只填 origin IS NULL——保留手動編輯與前向標記，可重跑補新出現的 NULL。
-- 信號優先序（PM 定，皆 → 'test'）：X-Client 測試類 > 假機架次 > 零樣本；
-- 全不中＝信號不明 → 'unknown'（誠實原則：不確定就說不確定，不強標 research）。
--
-- 用法：docker exec -i uav-db psql -U uav -d uav < scripts/backfill-session-origin.sql

UPDATE flight_sessions s SET origin = CASE

  -- 1. X-Client 測試類指令觸發（最強信號）：該機 sysid 在架次時間窗內有
  --    rig/test/acceptance 類 client 的 command_log。'frontend' 等不算測試。
  WHEN EXISTS (
    SELECT 1 FROM command_log c
    WHERE c.sysid = (SELECT mav_sysid FROM drones WHERE id = s.drone_id)
      AND c.client ~* '(rig|test|acceptance)'
      AND c.time BETWEEN s.started_at - interval '60 seconds'
                     AND COALESCE(s.ended_at, s.started_at + interval '30 minutes')
  ) THEN 'test'

  -- 2. 假機架次：drone 是 uav-s%（自動註冊的模擬僚機）且 is_simulated
  WHEN EXISTS (
    SELECT 1 FROM drones d
    WHERE d.id = s.drone_id AND d.name LIKE 'uav-s%' AND d.is_simulated
  ) THEN 'test'

  -- 3. 零鏈路樣本：無研究價值（空架次不是「飛行」）
  WHEN COALESCE((s.summary->>'samples_total')::int, 0) = 0 THEN 'test'

  -- 其餘：信號不明，留 'unknown'（含 sim-uav-1 有資料但無法確認是研究或測試的架次）
  ELSE 'unknown'
END
WHERE s.origin IS NULL;

-- 回填後分布（回報用）
SELECT COALESCE(origin, '(null)') AS origin, count(*) FROM flight_sessions GROUP BY 1 ORDER BY 2 DESC;
