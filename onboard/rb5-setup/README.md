# RB5 機上 MAVLink 設定（m0052 代）— 機端前提設定

> 這是**主路線的前提設定**（2026-08-11 使用者澄清：機端本來就開兩 port
> 分流資料/控制，三實例設定即現況）。新機/其他 RB5 單元接入時用本目錄的
> 範本＋腳本套用同樣設定，避免每台手改。以 2026-08-10 已實測可控的那台
> RB5 為 known-good。定案見 `issues/016-rb5-platform-connectivity.md`。

把 RB5（ModalAI m0052／Qualcomm Flight RB5，跑 PX4）的 MAVLink 對外通道，
從**出廠的廣播 :14550**改成**主動 unicast 打地面站的 14540／14541**——這是
本系統兩通道架構（14540 資料唯讀／14541 指令雙向）在 RB5 上成立的前置。

背景與病因見 `issues/016-rb5-platform-connectivity.md` 與 `reference/rb5/`。

## known-good 範本（三個 PX4 mavlink 實例）

以 2026-08-10 已實測可控的那台 RB5 為準（`/usr/bin/voxl-px4-start` 加三行）：

```
mavlink start -x -u 14560 -o 14540 -t <地面站IP> -m onboard -r 50000       # 資料 → 地面站（唯讀通道）
mavlink start -x -u 14561 -o 14541 -t <地面站IP> -m minimal -r 20000      # 指令 → 地面站（雙向通道）
mavlink start -x -u 14562 -o 14540 -t 127.0.0.1  -m onboard -n lo -r 100000  # 機內 → onboard node 綁座標
```

要點（踩過的坑，全寫進腳本的檢查）：
- `-u` 每條用不同的**本機空埠**，不可與既有實例撞號。
- **`-m normal` 不是合法模式名**（normal 是預設、不能指定，會靜默啟動失敗）；
  指令通道用 `-m minimal` 最省 5G 頻寬。
- PX4 mavlink **實例有上限（通常 4）**；出廠已有數個實例，超額的行會啟動失敗，
  且可能連累後面的行。腳本會先數現有實例、不夠位子就中止並提示。
- 5G 場景走 **unicast static IP**（`-t <地面站IP>`），不依賴廣播（廣播不過 5G）。
- 多機接入前**逐台設唯一 `MAV_SYS_ID`**（voxl-mavlink-server <1.4.12 有全機
  重設為 1 的 bug，會打掉單埠 sysid demux——見 issue 016）。

## 用法

```bash
# 在 RB5 上（需 root）。預設是「預覽」，不會改任何東西：
sudo ./configure-mavlink.sh --gs-ip 10.141.2.32            # 只印將做什麼（dry-run）
sudo ./configure-mavlink.sh --gs-ip 10.141.2.32 --sysid 1 --apply   # 真的寫入

# 套用後重啟 PX4，並在地面站驗證：
sudo systemctl restart voxl-px4
#   地面站： curl :38000/healthz（mavlink_connected:true）、curl :38001/healthz（drones 有 sysid）
#            python3 scripts/check-onboard.py（第 4 項樣本帶座標＝機內 14562 通）
```

> ⚠️ 本腳本尚未在真機驗證過（開發端無 RB5）。預設 dry-run 就是為此——
> `--apply` 前請先看預覽輸出、確認插入位置與實例數合理。它會先備份
> `voxl-px4-start`，且以標記區塊（marker block）做**冪等**插入，可安全重跑。
> voxl 套件更新可能覆寫 `voxl-px4-start`，更新後重跑本腳本即可回填。

## 不涵蓋

- **server 代（voxl-mavlink-server）**：MAVLink 由 server 層獨佔管理，加不了
  PX4 mavlink 實例——那條路徑（conf static_gcs_ip 或機上 router，可能要對
  「機上不裝 router」定案破例）等使用者上機檢查結果後另做。腳本偵測到 server
  代會中止並說明。
