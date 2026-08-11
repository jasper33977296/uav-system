-- issue 020 一次性回填：把孤兒架次（mission_id NULL）綁回它實際飛的任務。
-- 事實源＝command_log 的 mission_upload 留痕（時間/sysid/mission_id，不可變審計）。
-- 對每條孤兒架次，取「該機在架次開始前、最近一筆 accepted 的 mission_upload」。
-- 冪等：只動 mission_id IS NULL 的列；且只綁仍存在的 mission（避免 FK 懸空）。
--
-- 用法（地面站，一次即可；程式碼的前向綁定會自動處理未來所有架次）：
--   docker exec -i uav-db psql -U uav uav < scripts/backfill-session-mission.sql
--
-- 綁不上的（swarm 模擬機無 command_log／手動解鎖無上傳／command 服務之前的
-- 舊架次）維持 NULL＝資料斷代，屬正常，不強綁。

WITH cand AS (
  SELECT s.id AS sid,
    (SELECT (cl.params->>'mission_id')::uuid
     FROM command_log cl
     JOIN drones d ON d.mav_sysid = cl.sysid
     WHERE cl.action = 'mission_upload' AND cl.result = 'accepted'
       AND d.id = s.drone_id
       AND cl.time <= s.started_at
       AND EXISTS (SELECT 1 FROM missions m
                   WHERE m.id = (cl.params->>'mission_id')::uuid)
     ORDER BY cl.time DESC LIMIT 1) AS mid
  FROM flight_sessions s
  WHERE s.mission_id IS NULL)
UPDATE flight_sessions s SET mission_id = cand.mid
FROM cand WHERE s.id = cand.sid AND cand.mid IS NOT NULL;

\echo '回填後孤兒/已綁架次數：'
SELECT count(*) FILTER (WHERE mission_id IS NULL) AS orphan,
       count(*) FILTER (WHERE mission_id IS NOT NULL) AS bound
FROM flight_sessions;
