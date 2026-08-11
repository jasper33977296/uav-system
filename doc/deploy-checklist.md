# 地面站部署前檢查清單（現場照著走）

> 用途：把系統從開發（SITL）搬到地面站接真機（RB5）之前／到現場後，**逐項打勾**。
> 每步附指令與預期結果。卡住看 §6 故障排除。完整背景見 `doc/deployment.md`。
> 埠約定：**14540＝資料唯讀通道（backend）／14541＝指令雙向通道（command）**／
> 14550＝QGC。API：backend 38000、command 38001、前端 33000。

---

## 1. 地面站環境設定（`.env`）

- [ ] `LINK_SOURCE=modem`（真機模式；`simulated` 會拒收機上 POST、機上資料一筆都進不來）
- [ ] `MAVLINK_URL=udpin://0.0.0.0:14540`（backend 收資料通道）
- [ ] `COMMAND_MAVLINK_URL=udpin://0.0.0.0:14541`（command 收指令通道；**不是** SITL 的 14550）
- [ ] `ENABLE_COMMANDS`：**先 `false`**（只觀察、不發指令、不發 GCS 心跳）→ 首飛前單機驗收 OK 後才改 `true`
- [ ] `COMPOSE_PROFILES` 不含 `sim`（生產環境不起 SITL；範本預設已無）
- [ ] `AUTOREGISTER_SIMULATED=false`（自動註冊的是真機，別標成模擬）

## 2. 起服務＋自檢

- [ ] `docker compose up -d`（起 db／backend／command／frontend，**不含** sitl）
- [ ] backend 健康：`curl http://localhost:38000/healthz`
      → 預期 `{"ok":true,"mavlink_connected":false,"link_source":"modem"}`（未接機時 `false` 屬正常）
- [ ] command 健康：`curl http://localhost:38001/healthz` → 200、`drones` 空（未接機）
- [ ] 前端可開：瀏覽器 `http://<地面站IP>:33000`

## 3. 機上設定（每一台 RB5，逐台做）

背景與病因見 `issues/016-rb5-platform-connectivity.md`、範本在 `onboard/rb5-setup/`。

- [ ] **voxl-mavlink-server 版本 ≥ 1.4.12**（<1.4.12 有「全機 sysid 被重設為 1」的 bug，
      會打掉單埠 sysid demux、多機全被當同一台）：在 RB5 上查版本
- [ ] **套用兩通道 MAVLink 設定**（出廠是廣播 :14550，5G 打不到我方埠 → 不改＝連不上）：
      ```bash
      # 在 RB5 上（root）。先 dry-run 看要做什麼：
      sudo ./configure-mavlink.sh --gs-ip <地面站IP>
      # 確認後真的寫入，並指定這台的唯一 sysid：
      sudo ./configure-mavlink.sh --gs-ip <地面站IP> --sysid <N> --apply
      ```
- [ ] **每台 `MAV_SYS_ID` 唯一**（單埠多機靠 sysid demux；重號＝混料，backend 會發
      `sysid_addr_change` 告警）。逐台設不同 N，套用後**實測唯一**（見 §4 第 1 步）
- [ ] 三條 mavlink 實例都起得來（PX4 實例有上限，通常 4；出廠已佔幾個，超額會靜默失敗
      ——`configure-mavlink.sh` 會先數、位子不夠會中止提示）
- [ ] **5G 量測節點 `.env`（每台各設，`onboard/.env`）**：
      | 變數 | 每台要填什麼 |
      |---|---|
      | `GROUND_API` | `http://<地面站IP>:38000`——**每台都填同一個**（＝§4 的地面站 IP）|
      | `MAV_SYSID` | **這台的 sysid，與上一步設的 `MAV_SYS_ID` 相同**（逐台不同：1、2、3…）|
      多機**必設 `MAV_SYSID`**：不設的話每台送回的即時訊號都會被記到「主機」名下
      （靜默混料、僚機的即時卡空白）。節點啟動時用它向地面站解出 drone_id——drone_id 是
      地面站 UUID，要該機首次連上被自動註冊才存在，所以機上填 sysid 不填 UUID。
- [ ] 裝成服務並看到解析成功：`sudo ./install.sh`（會先 preflight 檢查地面站可達性與
      AT 埠）→ `journalctl -u uav-link-node -f` 應出現
      `[identity] sysid N → drone_id <uuid>（樣本開始送出）`
      - 若持續印「地面站還沒有 mav_sysid=N 的機」：該機還沒被地面站自動註冊
        （先確認 §3 的 MAVLink 設定生效、前端無人機頁看得到這台）。此時節點**刻意不送**
        樣本、留在緩衝，解出後自動補傳，不會掉資料。

## 4. 5G 連線可達性（機↔地面站）

- [ ] RB5 能路由到地面站 IP：在 RB5 上 `ping <地面站IP>` 通
- [ ] 機上是 **unicast static IP**（`-t <地面站IP>`），不靠廣播（廣播不過 5G）
- [ ] 地面站防火牆放行 UDP 14540／14541（機主動打進來）
- [ ] （QGC 若要用）我方需**加聽 14550**（voxl 埠寫死），或機上架 mavlink-router 轉埠

## 5. 首飛驗收順序（**單機先 → 編隊後**）

### 5a. 單機（每台各驗一次）
- [ ] 機上線後 backend 自動註冊該機，前端「無人機」頁看得到、`mav_sysid` 正確且**唯一**
- [ ] **機上資料完整性**（真機資料回傳的唯一自檢工具）：
      ```bash
      python3 scripts/check-onboard.py http://<地面站IP>:38000 30
      ```
      → 逐項綠：source=modem、~1Hz、RF 欄位（sinr/rsrp/pci/band…）非空、位置綁定、時鐘偏差 <5s
- [ ] 設 `ENABLE_COMMANDS=true` 並重起 command（`docker compose up -d uav-command`）後，
      單機 arm→takeoff→RTL 走一遍（前端或 `POST :38001/api/command/<sysid>/takeoff`），落地正常
- [ ] armed 時 DB 真的有新 `telemetry`／`link_metrics` 列（check-onboard 第 6 項會抽查）

### 5b. 編隊（≥2 台，單機都過之後）
- [ ] 前端建群組任務（unified 或 separate）→ 執行 → 各機 phase 逐台推進到 flying
- [ ] 緊急鈕：群組 RTL-all／abort 能全撤、各機安全返航落地

## 6. 安全注意事項

> ⚠️ **separate 模式編隊：各機任務請規劃「不同飛行高度」。**
> 顯式 RTL 返航高度錯開（`GROUP_RTL_STAGGER_M`）**尚未實作**（設計已知限制，
> 見 `doc/group-missions-design.md` §10.2）。unified 模式各台自動高度分層（vsep）、
> 返航會自然錯開；但 **separate 模式若各機任務高度相同，緊急 RTL-all 的返航高度分離
> 無保證**——各機可能以同高度往同一 home 上空匯合。**操作規避＝separate 編隊任務
> 規劃不同高度**，直到未來版本補上顯式錯開。

- [ ] 首飛在開闊、可視、低風場地，Home 點淨空
- [ ] 每台在前端確認 `ready=True`（PX4 預檢過）才 arm
- [ ] 已知 sysid 唯一（§3、§4）——重號會讓指令送錯機

## 7. 故障排除速查

| 症狀 | 最可能原因 | 查 |
|---|---|---|
| 前端看不到機 | 機上沒改設定（還在廣播 :14550）／5G 打不到 14540 | §3、§4；`ping`、`ss -ulnp` 看 14540 有無封包 |
| 多機互相干擾、`sysid_addr_change` 告警洪水 | sysid 重號（voxl <1.4.12 bug 或沒逐台設） | §3 版本＋逐台唯一 sysid |
| 有遙測、`check-onboard` 說位置空 | 機上 router 沒把 MAVLink 餵給 onboard node 的 PX4_URL | §3 第三條 lo 實例 |
| 指令送出無反應 | `ENABLE_COMMANDS=false`／command 收在 14541 但機打 14540 | §1、§5a |
| `check-onboard` 收 409 / source 非 modem | 地面站 `.env` 還是 `simulated` | §1 `LINK_SOURCE=modem` |
