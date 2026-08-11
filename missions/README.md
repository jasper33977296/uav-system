# missions/

QGroundControl 的 `.plan` 航線檔。研究用的固定航線放這裡，讓實驗可重現
（同一條航線飛多次、比較不同干擾設定下的鏈路品質）。

| 檔案 | 用途 |
|---|---|
| `interference-survey.plan` | 起飛 50m → 沿經線往北穿越 `sim-jammer-A` 干擾區 → RTL。SITL 場景（蘇黎世），驗證「飛入干擾區 → SINR 驟降」|
| `complex-survey.plan` | 較長的多航點測繪航線 |

## 用法

**QGC**：Plan 頁面 → Open → 選 `.plan` → Upload。

**外部觸發 API**（command 服務 `:38001`；此目錄唯讀掛進容器 `/srv/missions`）：
- `GET /api/plans` — 列出這裡的 `.plan`（含解析失敗的，帶 error）
- `GET /api/plans/{name}` — 單一 `.plan` 解析＋幾何預檢（`?raw=true` 回 QGC 原始 JSON）
- `POST /api/start {"plan": "interference-survey"}` — 一鍵：匯入任務庫 → 上傳 → arm →
  起飛 → 到高度切 AUTO.MISSION（見 [doc/api.md](../doc/api.md) §2）

檔名只認這一層、拒路徑穿越（`resolve()`）。

## 格式

`.plan` 是 QGC 的 JSON 格式（`version: 1`、`mission.version: 2`）。
`items` 的 `command` 是 MAV_CMD enum：22 = NAV_TAKEOFF、16 = NAV_WAYPOINT、
20 = NAV_RETURN_TO_LAUNCH；`frame` 3 = GLOBAL_RELATIVE_ALT、2 = MISSION。
上傳時 `command`/`frame`/`p1`–`p4` 原樣保真送出。
