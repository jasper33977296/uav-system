# 差異分析：規格文件 vs 目前實作（為何只能控制部分機型）

日期：2026-08-11。比對對象：`reference/` 下的官方文件快照 vs `apps/command`、
`apps/backend` 現行程式。

## 結論（TL;DR）

現有 command/backend 堆疊**不是「MAVLink 實作」，是「PX4 方言實作」**。
MAVLink 共通層（心跳、指令 ACK、任務握手、MANUAL_CONTROL 訊息本身）我們做得
符合規格；但**模式設定、模式解讀、起飛序列**三處硬編碼了 PX4 私有慣例，
對 ArduPilot 機不只無效，部分指令會**誤觸完全不同的模式（飛安風險）**。
RB5（Qualcomm Flight RB5）跑 PX4，所以現況只有它能控。

非 MAVLink 機型（DJI 消費機等）不在本協定範圍，需另做 vendor SDK 轉接層——
請先確認昨天失敗的機型清單，區分「MAVLink 方言問題」與「根本不講 MAVLink」。

**2026-08-11 補充：使用者確認受測機型＝高通 AI Model RB5。** RB5 跑 PX4，
所以下面 §1–§4 的自駕儀方言問題不是它的主因——它的問題在**平台連線層**，
見新增的 §0。§1 之後的方言差異仍然成立，適用於未來接入的其他廠牌機。

## 0. RB5 平台連線層（實測機型的直接病因，來源見 rb5/README.md）

RB5 的 MAVLink 對外通道由 ModalAI 平台層管理，與「原生 PX4 自由配 mavlink
實例」的假設有三個落差：

| 落差 | 平台行為 | 我方假設 | 後果 |
|---|---|---|---|
| 連線方向 | 出廠**廣播 :14550** 等 GCS 回應（QGC 自動連線的原理）；或 conf 裡 static IP、**埠 14550 寫死**（voxl-mavlink-server 代） | 機上配好兩條實例主動打我方 14540/14541 | 出廠機永遠打不到我們的埠；沒逐台改機上設定就是「連不上＝不可控」 |
| 5G 網段 | broadcast 不過 cellular；官方建議 VPN＋static GCS IP | 5G 量測是主場景 | WiFi 測通、上 5G 就斷——「有時能控有時不能」的典型病因 |
| sysid | voxl-mavlink-server <1.4.12 bug：**全部機 sysid 被重設為 1** | 單埠多機靠 sysid demux；同 sysid 異位址會告警 | 多機全被當同一台；demux 架構直接失效 |

**落地動作**（上機檢查清單）：
1. `voxl-inspect-services`＋`ls /etc/modalai/` 判定是 m0052 代（full-m0052.config）
   還是 voxl-mavlink-server 代（voxl-mavlink-server.conf）。
2. m0052 代：`param set MAV_BROADCAST 0`＋逐台加兩條實例
   `mavlink start -x -u <udp> -t <我方IP> -o 14540/-o 14541`（正確 flag 以機上
   PX4 版本 `mavlink help` 為準）；server 代：conf 設 static_gcs_ip＋
   我方**加聽 14550**（埠寫死，改埠要改它原始碼）或機上架 mavlink-router 轉埠。
3. 查 voxl-mavlink-server 版本 ≥1.4.12，逐台設不同 MAV_SYS_ID 並實測
   heartbeat 的 sysid 真的不同。
4. 5G 場景：確認機到我方主機的路由（VPN/公網），不要依賴廣播。
5. 追蹤：issues/016。

## 差異總表

| # | 面向 | 目前實作 | 規格/他家慣例 | 對 ArduPilot 機的實際後果 | 嚴重度 |
|---|---|---|---|---|---|
| 1 | 設定模式 | DO_SET_MODE param2/3＝PX4 main/sub（`mav.py:30,253`） | ArduPilot：param2＝模式號、param3 不用 | hold/mission/rtl/land 全變 **GUIDED**；position 變 **AUTO**——誤觸危險模式 | **critical** |
| 2 | 解讀模式 | custom_mode>>16 拆 PX4 main/sub（backend `mavlink_rx.py:52`） | ArduPilot：custom_mode＝整數模式號 | UI 顯示的模式名全錯 | high |
| 3 | 起飛 | arm → NAV_TAKEOFF（param7＝絕對海拔）（`main.py:_do_takeoff`） | Copter：**先進 GUIDED** → arm → takeoff；param7＝**相對高度** | 起飛指令被拒；就算收了，高度語意也錯 | **critical** |
| 4 | 手動控制 | 進 POSCTL＋MANUAL_CONTROL 串流；deadman 失聯切 PX4 Hold | MANUAL_CONTROL 兩家都支援；但前置模式與失聯降級是 PX4 模式表 | 搖桿啟動誤切 AUTO；失聯降級誤切 GUIDED；另 sysid 门檻見 #6 | **critical** |
| 5 | 任務 | MISSION_ITEM_INT 握手＋回讀比對（`mav.py:job_upload_mission`）——協定共通 ✅ | ArduPilot 慣例：seq 0＝home、任務從 seq 1 起（待 SITL 驗證）；啟動任務靠 AUTO＋MISSION_START，非 AUTO.MISSION | 回讀筆數/序號可能位移誤判；「開始任務」走不通 | high |
| 6 | GCS sysid | 254（`mav.py:27`） | ArduPilot 預設只信 `SYSID_MYGCS`（預設 255）的 MANUAL_CONTROL/RC override（待驗證，不在本快照文件內） | 搖桿訊息可能被**靜默忽略** | high |
| 7 | 就緒/預檢 | SYS_STATUS PREARM 位＋STATUSTEXT/PX4 Events | ArduPilot 主要靠 STATUSTEXT（反而更相容）；PREARM 位支援度待驗證 | 顯示可能缺就緒資訊；文字原因反而更完整 | medium |
| 8 | 心跳/failsafe | 1Hz GCS 心跳（共通 ✅） | PX4：COM_DL_LOSS_T；ArduPilot：FS_GCS_ENABLE 參數 | 機制可通，但 failsafe 行為要逐機型確認參數 | medium |

## 逐項細節

### 1. 設定模式（最危險的一項）

`apps/command/app/mav.py:30` 的 `PX4_MODES` 把模式編成 PX4 的
(main_mode, sub_mode)，由 `job_set_mode` 塞進 DO_SET_MODE 的 param2/param3。
這是 PX4 私有 union（見 `px4/px4_custom_mode.h:89`——main_mode<<16、sub_mode<<24
只是 PX4 對 custom_mode 的自家編排）。

ArduPilot 的規則（`ardupilot/mavlink-get-set-flightmode.rst` §"Set the Flightmode"）：
param2＝**該載具型別的模式號**、param3 不用。Copter 模式號權威表在
`ardupilot/copter-mode.h:77`：STABILIZE=0、AUTO=3、GUIDED=4、LOITER=5、
RTL=6、LAND=9、POSHOLD=16。

後果具體算給你看——我們送出的 param2 就是 PX4 main_mode：

| 我方指令 | 送出 param2 | Copter 收到的意思 |
|---|---|---|
| hold (4,3) | 4 | **GUIDED**（不是懸停！） |
| mission (4,4) | 4 | **GUIDED** |
| rtl (4,5) | 4 | **GUIDED**（想返航結果進 GUIDED） |
| land (4,6) | 4 | **GUIDED** |
| position (3,0) | 3 | **AUTO**（搖桿啟動瞬間開始跑機上任務！） |

**跨自駕儀的可攜替代**（`ardupilot/mavlink-get-set-flightmode.rst` §COMMAND_INT 一節，
PX4 同樣支援）：RTL→`MAV_CMD_NAV_RETURN_TO_LAUNCH`(20)、
Land→`MAV_CMD_NAV_LAND`(21)、Loiter→`MAV_CMD_NAV_LOITER_UNLIM`(17)。
長期正解是 MAVLink **Standard Modes 微服務**（`mavlink/standard_modes.md`）：
`MAV_CMD_DO_SET_STANDARD_MODE`＋`AVAILABLE_MODES` 枚舉，設計目的就是
「GCS 不需預知自駕儀」——但各家韌體支援度新（PX4 1.15+，ArduPilot 進行中），
短期仍需按 `HEARTBEAT.autopilot` 分表。

### 2. 解讀模式（UI 顯示）

backend `apps/backend/app/mavlink_rx.py:52` `_mode_name()` 以
`(custom_mode>>16)&0xFF` 拆 main/sub——只對 PX4 成立。ArduPilot 的
custom_mode 是整數模式號（`mavlink-get-set-flightmode.rst` §"Get the Flightmode"：
「flightmode number varies by vehicle type」）。修法：HEARTBEAT 有
`autopilot` 欄位（`mavlink/minimal.xml`：MAV_AUTOPILOT_ARDUPILOTMEGA=3、
MAV_AUTOPILOT_PX4=12）＋`type` 欄位，按 (autopilot, type) 選解碼表。
兩端（backend 顯示、command `snapshot()`）都要改。

### 3. 起飛序列

`apps/command/app/main.py:_do_takeoff`：直接 arm → NAV_TAKEOFF，param7 用
「絕對海拔」（live 高度＋目標）。Copter 的規矩
（`ardupilot/copter-commands-in-guided-mode.rst` 範例序列）：
**GUIDED → arm throttle → takeoff 10**——NAV_TAKEOFF 只在 GUIDED 接受，
param7 是**相對高度**。即：對 Copter 要先 DO_SET_MODE(param2=4)，
且高度語意完全不同（差一個地形海拔，錯了就是撞地或飛太高）。

### 4. 手動控制（虛擬搖桿）

MANUAL_CONTROL 訊息本身兩家都實作（`mavlink/manual_control.md` §Implementations，
含 PX4 與 ArduPilot Copter/Plane/Rover/Sub）。軸向慣例我們符合規格
（-1000..1000、推力型 z 用 0..1000 中位 500；注意 ArduRover 的 y/r 對調）。
破口在模式面：

- 進手動前切 POSCTL（PX4 專用）；Copter 等效是 **LOITER(5)** 或 POSHOLD(16)。
- deadman >2s 失聯降級送 PX4 hold(4,3) → Copter 收到變 GUIDED，
  「安全降級」變成危險動作。
- 失聯降級的可攜寫法：`MAV_CMD_NAV_LOITER_UNLIM`（兩家通）。

### 5. 任務協定

好消息：任務微服務（`mavlink/mission.md`）是共通層，我們的完整握手＋
MISSION_REQUEST_INT/**MISSION_REQUEST 雙處理**（`mav.py:275`）＋回讀比對
都是照規格做的，可保留。差異在慣例與啟動：

- ArduPilot 把 **home 當 seq 0**，任務項從 seq 1 起（QGC 上傳也帶 home 項）。
  我們的 count 與回讀逐項比對會因位移誤判。※此點不在本快照文件內，
  屬社群共識，列 SITL 驗證清單。
- 啟動任務：我們切 AUTO.MISSION（PX4 式）。Copter 走 **mode AUTO(3)＋
  `MAV_CMD_MISSION_START`(300)**；PX4 也支援 MISSION_START，可作共通路徑。
- frame 建議統一 `MAV_FRAME_GLOBAL_RELATIVE_ALT_INT`，兩家語意一致。

### 6. GCS sysid 與訊息接受門檻

我們用 sysid 254（QGC 慣用 255 的並存設計）。ArduPilot 預設只接受
`SYSID_MYGCS`（預設 255；新版改名 MAV_GCS_SYSID）來源的
MANUAL_CONTROL/RC_CHANNELS_OVERRIDE——sysid 254 的搖桿訊息會被**靜默丟棄**
（無 ACK 可觀察，最難查的一種故障）。※不在本快照文件內，待 SITL 驗證。
解法擇一：接 ArduPilot 機時設其 `SYSID_MYGCS=254`，或 sysid 做成每機設定。

### 7. 就緒顯示與拒絕原因

`SYS_STATUS` PREARM 健康位是 backend「可飛」顯示的來源（PX4 SITL 實測）；
ArduPilot 的支援度要實測。拒絕原因文字方面 ArduPilot 反而**更相容**：
它全走 STATUSTEXT（我們已收），沒有 PX4 1.14 Events 協定「STATUSTEXT 常為空」
的問題（issue 014 的 Events 解碼是 PX4 限定議題）。`px4_notes` 這個欄位名
建議改中性（`autopilot_notes`）。

### 8. 心跳與 failsafe

1Hz GCS 心跳是 MAVLink 共通慣例（`mavlink/heartbeat.md`），兩家都以它做
GCS 失聯 failsafe（PX4：COM_DL_LOSS_T；ArduPilot：FS_GCS_ENABLE 參數組）。
機制可通；接新機型時要逐台確認 failsafe 參數（觸發時間與動作）。

## 建議的落地順序

1. **機型盤點**（等使用者提供昨天失敗清單）：分成 PX4／ArduPilot／非 MAVLink 三類。
2. **自駕儀偵測**：HEARTBEAT 的 autopilot+type 存進 drones 表，全鏈路帶著走
   （command snapshot、backend 顯示、前端 UI）。
3. **指令層抽象**：`PX4_MODES` 改為 per-autopilot 策略表；優先改用可攜指令
   （NAV_RETURN_TO_LAUNCH／NAV_LAND／NAV_LOITER_UNLIM／MISSION_START），
   剩下的才分家（起飛序列、手動前置模式、deadman 降級）。
4. **SITL 驗證**：ArduPilot SITL（sim_vehicle.py）跑同一套驗收：模式切換、
   起飛、任務上傳＋回讀、搖桿＋deadman、拔心跳 failsafe。驗證清單含上述
   「待驗證」項（home seq 0、SYSID_MYGCS、PREARM 位）。
5. 非 MAVLink 機型另立 adapter 設計（範圍另議）。
