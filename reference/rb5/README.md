# RB5（Qualcomm Flight RB5 5G，ModalAI 平台）連線機制

抓取日期：2026-08-11。`*.html` 為 docs.modalai.com 頁面快照；forum 討論串為
JS 網站無法完整快照，關鍵內容整理如下（附來源連結）。

## 平台事實（比對用）

1. **PX4 跑在機上（voxl-px4）**，不是外接飛控。MAVLink 對外通道由平台層管理，
   有兩代機制，上機用 `voxl-inspect-services`／`ls /etc/modalai/` 確認是哪代：
   - **RB5 原生（m0052）**：PX4 的 mavlink 實例直接寫在
     `/etc/modalai/full-m0052.config`。出廠走 **MAV_BROADCAST 廣播 :14550**，
     等 QGC 回應後鎖定該位址（「自動偵測 QGC」的原理）。
   - **voxl-mavlink-server（新版 voxl-suite）**：GCS 位址寫在
     `/etc/modalai/voxl-mavlink-server.conf`（`primary/secondary_static_gcs_ip`），
     **目的埠 14550 寫死在程式裡**，要改埠得改原始碼重編。
2. **指定自家 GCS 的官方改法**（m0052 代）：停用廣播＋顯式指定目標——
   ```
   param set MAV_BROADCAST 0
   mavlink start -x -u 14557 -r 40000 -t <GCS IP>
   mavlink start -u 14556 -t 127.0.0.1 -n lo -m custom   # 機內通道
   ```
   可加開多條實例（例：`mavlink start -x -o 14540 -u 14558 -n lo` 給 MAVROS）。
3. **廣播不過 5G**：cellular 網段擋 broadcast；官方對 5G/LTE 遠端建議
   Tailscale VPN＋static GCS IP。WiFi 場（同網段）廣播才會通。
4. **sysid 已知 bug**：voxl-mavlink-server 1.4.12（2025-09）之前，
   不論 MAV_SYS_ID 設多少，**所有機 sysid 都被重設為 1**。
5. 多機單埠場景：因 14550 寫死，社群建議在 GCS 側或機上用 mavlink-router 轉接。

## 對本專案的意涵（詳見 ../gap-analysis.md §0）

- 我們的兩通道設計（機→我方 14540 資料／14541 指令）在 RB5 上**不會自動成立**：
  出廠機只會廣播 14550 或打 conf 裡的 static IP:14550。每台 RB5 上機都要改
  full-m0052.config（m0052 代）或 conf＋改埠（server 代）。
- sysid bug 直接打中我們的「單埠 sysid demux」多機架構：全部機 sysid=1
  會被當成同一台＋觸發同 sysid 異位址告警。上機先查 voxl-mavlink-server 版本。

## 來源

- [RB5 Connect to GCS](https://docs.modalai.com/Qualcomm-Flight-RB5-user-guide-connect-gcs/)（快照：`Qualcomm-Flight-RB5-user-guide-connect-gcs.html`）
- [RB5 Functional Description](https://docs.modalai.com/Qualcomm-Flight-RB5-functional-description/)（快照同名）
- [Unable to connect to QGroundControl（forum 616）](https://forum.modalai.com/topic/616/unable-to-connect-to-qgroundcontrol)——MAV_BROADCAST 0＋`-t <IP>` 改法出處
- [MAVLink custom port to GCS（forum 4275）](https://forum.modalai.com/topic/4275/mavlink-custom-port-to-gcs)——14550 寫死、voxl-mavlink-server.conf、sysid bug 與 1.4.12 修復出處
- [MAVROS on VOXL](https://docs.modalai.com/mavros/)——加開 mavlink 實例範例出處
