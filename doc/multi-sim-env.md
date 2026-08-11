# 多機模擬環境（N 台真 PX4+Gazebo，單埠 demux）

- 狀態：設計＋盤點完成，待 live bring-up（2026-08-11）
- 需求：使用者要「模擬多台無人機**同時操控與資料蒐集**」；PM 定 Path A（全 Gazebo
  多實例）、3 台起步、架構可擴 N。
- 相關：issue 013（群組任務，收剩餘 skew/RTL 時序）、issue 011（swarm_sim 退役）、
  issue 016（SITL 地面合流＝016 附記預期用法）。

## 定案（PM 2026-08-11）

- **路徑 A：全 Gazebo 多實例**（使用者明選，要完整物理）。3 台起步、可擴。
- **MAVLink 合流：mavlink-router 獨立小容器**（核可）——不動 PX4 image（保原廠、升級不揹債）、
  compose 可重現可版控。與「機上不裝 router」定案不衝突（那條管真機機上；SITL 地面合流是
  016 附記預期）。
- swarm_sim 於新環境跑通後退役（前端側僅一行 guard、無 UI 依賴，退役序無所謂）。

## 盤點結果（可行性）

- 現 image `jonasvautherin/px4-gazebo-headless:1.14.3` **內含 PX4 原生多機工具**
  `/root/Firmware/Tools/simulation/gazebo-classic/sitl_multiple_run.sh`。
- 該腳本：一個 gzserver ＋ N 個 `px4 -i $N` 實例（避免多 gzserver 撞埠）。每實例：
  - `MAV_SYS_ID = instance+1`（rcS `param set MAV_SYS_ID $((px4_instance+1))`）→ **sysid 天然唯一 1/2/3**。
  - gazebo↔PX4 模擬 TCP `4560+N`、`--mavlink_udp_port 14560+N`、gst `5600+N`。
- 單實例 entrypoint 用 `HOST_QGC HOST_API` 兩參數＝PX4 送 MAVLink 到 GS:14550(QGC)＋
  GS:14540(API)。**這是現行 command=14550／backend=14540 的由來**。

## MAVLink 埠實況（讀 px4-rc.mavlink 確認）＋合流設計

每 PX4 SITL 實例 `px4_instance`（0-indexed）的 MAVLink 埠（`init.d-posix/px4-rc.mavlink`）：
- **offboard**：bind `14580+i`、**送到 `14540+i`**（`-o`）。**instance 0 送 14540＝現行
  backend 收得到 sysid 1 的原因**；instance 1/2 送 14541/14542。
- **GCS**：bind `18570+i`（`mavlink start -u 18570+i`，等 GCS 連入）。
- gazebo↔PX4：TCP `4560+i`。

**整合陷阱**：若天真讓 mavlink-router 也送 backend 14540，會與 instance 0 的 offboard(14540)
**撞號**（sysid 1 兩來源）——正是剛修好的洪水成因。

**乾淨合流設計**（避開所有撞埠、image 保原廠、backend/command 程式不改）：
- mavlink-router **以 client 連各實例的 GCS 埠 `18570+i`**（i=0/1/2）——拿逐台全遙測，
  不碰 14540/14550。
- router 合流轉發到 **backend／command 的專用埠**（例 `udpin` 14545／14555）。
- **sim-fleet profile 覆寫** backend `MAVLINK_URL=udpin://0.0.0.0:14545`、command
  `COMMAND_MAVLINK_URL=udpin://0.0.0.0:14555`——只餵 router 合流流，**instance 的
  offboard 14540+i 不進 backend/command，零撞號**。sysid 1/2/3 天然 demux（防線仍在）。
- 單台 uav-sitl profile 維持現狀（14540/14550 直連）；兩 profile 互斥、切換即換埠。

（bring-up 仍需實跑確認 GCS 埠連入行為與 router config 語法，但埠對應已由原始碼定死、非猜測。）

## 建置步驟

1. **compose profile `sim-fleet`**（docker-compose 內 profile 或獨立 compose 檔）：
   - service `sim-fleet`：px4-gazebo-headless image，override command 跑 `sitl_multiple_run.sh -n 3`
     （host network）。
   - service `mavlink-router`：獨立小容器（image 待選：現成 mavlink-router image 或 build），
     config 聽 3 實例埠 → 轉 14540＋14550。
   - 起停：`docker compose --profile sim-fleet up/down`，可重現。**與現單台 uav-sitl 互斥**
     （二選一 profile；bring-up 時先 down uav-sitl）。
2. **逐台 link_sim**（backend `link_sim`/`main` 迴圈）：現只算主機一台。改為
   **每台按自身位置對干擾場景取樣** 生成 5G 鏈路資料（SINR/RTT/PCI…）＋**逐台
   `in_interference_zone` 標注**（各機按自身 lat/lon）。否則僚機無研究資料＝「多機資料蒐集」承諾不成立。
3. **資源實測入文件**：3 台 Gazebo 實際 CPU/RAM（單台基線 **160MiB/13%CPU**，本機 8 核／11GB avail
   → 3 台估 ~0.5GB/~40%CPU，餘裕大；跑通後補精確值＋「本機最多幾台」實測結論）。
   資料量：3×1Hz×兩表（telemetry＋link_metrics）＝現況 3 倍，30 天 retention 無虞（順手確認）。
4. **環境跑通後**：收 013-B 剩兩項時序（起飛 skew 實測、群組 RTL 高度錯開）→ 013 全案收官
   → swarm_sim 退役（011 close）。

## Bring-up 注意

- **disruptive**：會停現行單台 uav-sitl（backend/command 短暫失去 sysid 1）。無人在飛時做。
- **iterative**：mavlink-router 埠對接需第一次實跑驗證、可能要調 config。timebox；比預期重回報。
- rollback：`docker compose up -d uav-sitl` 復原單台。
