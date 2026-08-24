# Reference：協定與自駕儀規格文件

目的：專案目標是控制**各種不同規格型號**的無人機，但 2026-08-10 實測發現能控制的
機型非常有限。根因是現有實作綁定 PX4 的私有慣例（見 [gap-analysis.md](gap-analysis.md)）。
此資料夾收錄權威規格文件的本地快照，供比對與後續實作依據。

抓取日期：2026-08-11。來源皆為官方 repo 的 raw 檔案。

## mavlink/ — MAVLink 官方協定（跨自駕儀的共通層）

| 檔案 | 內容 | 來源 |
|---|---|---|
| `heartbeat.md` | 心跳/連線協定（存在偵測、sysid 規則） | mavlink/mavlink-devguide `en/services/heartbeat.md` |
| `command.md` | 指令協定（COMMAND_LONG/INT、ACK、重送） | 同上 `en/services/command.md` |
| `mission.md` | 任務上傳/下載完整握手（微服務定義） | 同上 `en/services/mission.md` |
| `manual_control.md` | 搖桿協定（MANUAL_CONTROL 軸向/範圍/各家實作差異） | 同上 `en/services/manual_control.md` |
| `standard_modes.md` | **標準模式微服務**（跨自駕儀設模式的正解，取代各家 custom_mode） | 同上 `en/services/standard_modes.md` |
| `mavlink_2.md` | MAVLink 2 框架 | 同上 `en/guide/mavlink_2.md` |
| `routing.md` | 訊息路由規則（sysid/compid 定址） | 同上 `en/guide/routing.md` |
| `common.xml` | 訊息/指令/enum 權威定義（MAV_CMD、MAV_AUTOPILOT…） | mavlink/mavlink `message_definitions/v1.0/common.xml` |
| `minimal.xml` | HEARTBEAT 與基礎 enum（MAV_AUTOPILOT/MAV_TYPE 值表） | 同上 `minimal.xml` |

## ardupilot/ — ArduPilot 的 MAVLink 方言（與 PX4 差異最大處）

| 檔案 | 內容 | 來源 |
|---|---|---|
| `mavlink-get-set-flightmode.rst` | **模式設定**：DO_SET_MODE param2＝模式號（param3 不用）——與 PX4 main/sub 完全不同 | ArduPilot/ardupilot_wiki `dev/source/docs/` |
| `mavlink-arming-and-disarming.rst` | 解鎖/上鎖（含 force code 21196） | 同上 |
| `copter-commands-in-guided-mode.rst` | **GUIDED 模式指令**：takeoff 前置序列、SET_POSITION_TARGET 速度控制 | 同上 |
| `mavlink-mission-upload-download.rst` | ArduPilot 端任務上傳/下載說明 | 同上 |
| `mavlink-basics.rst` | ArduPilot MAVLink 基礎 | 同上 |
| `mavlink-requesting-data.rst` | 資料流請求（REQUEST_DATA_STREAM vs SET_MESSAGE_INTERVAL） | 同上 |
| `copter-mode.h` | **Copter 模式號權威表**（STABILIZE=0…AUTO=3、GUIDED=4、LOITER=5、RTL=6、LAND=9、POSHOLD=16…） | ArduPilot/ardupilot `ArduCopter/mode.h` |

## fibocom-fm160/ — 機上 5G 模組的 AT 指令（訊號採樣的依據）

抓取日期：2026-08-24。機上模組是 **Fibocom FM160-JK**，
**不是** `LinkSample` schema 註解假設的 Quectel RM500Q-GL——
`AT+QENG="servingcell"` 在它上面直接回 `ERROR`。

| 檔案 | 內容 | 來源 |
|---|---|---|
| `README.md` | **`AT+GTCCINFO?` NR 欄位對照表**（含進位判定的推導與交叉驗證）、CESQ 尾三碼順序、3GPP 換算公式 | 本專案整理 |
| `Fibocom_FM350_AT_Commands_User_Manual_V2.10.pdf` | 原始手冊（§11.1.15 定義 GTCCINFO）。FM160 手冊未公開流通，用同家族 FM350 並以實測交叉驗證 | minipc.de 鏡像 |
| `GTCCINFO-excerpt.txt` | §11.1.15 節錄（UMTS/LTE/NR/EN-DC 四種格式） | 上述 PDF |
| `CESQ-excerpt.txt` | CESQ 相關段落節錄 | 上述 PDF |

⚠️ **對照表錯了會靜默污染研究資料**：`pci` 與 `ss_sinr` 的數值域重疊，
填錯不會報錯，只會讓「干擾發生時哪個細胞在服務」的分析得到看似合理的錯誤答案。
所以未確認的欄位一律留 `None`，原始回應整包存進 `LinkSample.raw` 供日後重算。
同 `px4-events/` 的「版本對不上就 fallback 到 raw id」原則。

## px4/ — PX4 私有慣例（現有實作的依據，留作對照）

| 檔案 | 內容 | 來源 |
|---|---|---|
| `px4_custom_mode.h` | PX4 custom_mode union（main_mode<<16、sub_mode<<24）與 main/sub 值表 | PX4/PX4-Autopilot `src/modules/commander/px4_custom_mode.h` |
