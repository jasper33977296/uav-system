# missions/

QGroundControl 的 `.plan` 航線檔。研究用的固定航線放這裡，讓實驗可重現
（同一條航線飛多次、比較不同干擾設定下的鏈路品質）。

| 檔案 | 用途 |
|---|---|
| `interference-survey.plan` | 起飛 50m → 沿經線往北穿越 `sim-jammer-A` 干擾區 → RTL。SITL 場景（蘇黎世），用於驗證「飛入干擾區 → SINR 驟降」|

## 用法

**QGC**：Plan 頁面 → Open → 選 `.plan` → Upload。

**程式**（MAVSDK 3.17.2）：

```python
data = await drone.mission_raw.import_qgroundcontrol_mission("missions/interference-survey.plan")
await drone.mission_raw.upload_mission(data.mission_items)
await drone.mission_raw.start_mission()
```

實測：`interference-survey.plan` 5 個 item 上傳後，backend 從另一條 link
下載耗時 13 ms，執行中可追蹤 `mission_progress` 0/5 → 4/5。
詳見 [doc/qgc-integration.md](../doc/qgc-integration.md)。

## 格式

`.plan` 是 QGC 的 JSON 格式（`version: 1`、`mission.version: 2`）。
`items` 的 `command` 是 MAV_CMD enum：22 = NAV_TAKEOFF、16 = NAV_WAYPOINT、
20 = NAV_RETURN_TO_LAUNCH；`frame` 3 = GLOBAL_RELATIVE_ALT、2 = MISSION。
