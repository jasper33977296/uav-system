# Issues

專案的已知問題、待修項目與設計待決事項。一個問題一個檔案，方便在 commit 訊息或
討論中直接引用編號（例：`fix: link_lost 永不觸發 (#001)`）。

## 慣例

- 檔名：`NNN-短標題-用連字號.md`，編號遞增不重用。
- 新問題從 [TEMPLATE.md](TEMPLATE.md) 複製。
- 狀態寫在檔案開頭的欄位，同時更新下方索引表：

| 狀態 | 意義 |
|---|---|
| `open` | 已確認、待處理 |
| `in-progress` | 修改中 |
| `needs-decision` | 卡在設計取捨，需要先決定方向 |
| `closed` | 已修並驗證（在檔案末尾補「解決方式」與 commit）|

- 嚴重度：`high`（擋到主要研究流程／示範）、`medium`（資料正確性或體驗受損）、
  `low`（清理、體感問題）。

## 索引

| # | 標題 | 嚴重度 | 狀態 | 位置 |
|---|---|---|---|---|
| [001](001-link-lost-event-never-fires.md) | `link_lost` 事件永遠不會觸發 ✔實測確認 | high | **closed** | `backend/app/main.py:44-51` |
| [002](002-handover-event-flapping.md) | handover 事件抖動狂噴 → 已移除該事件類型 ✔實測確認 | low | **closed** | `backend/app/link_sim.py:36-43` |
| [003](003-cell-id-not-persisted.md) | `cell_id` 沒寫進 DB → 改判非 bug，是 schema 語意不明 | low | **closed** | `backend/app/db.py:87-100` |
| [004](004-writes-while-disarmed.md) | 未 armed 時仍持續 1Hz 入庫，資料無限成長 ✔實測確認 | medium | **closed** | `backend/app/main.py:31-61` |
| [005](005-sitl-mavlink-target-ip.md) | SITL 在 host network 下把 MAVLink 送到區網閘道 | high | **closed** | `docker-compose.yml` |
| [006](006-battery-pct-x100.md) | `battery_pct` 多乘 100，實際值 10000 ✔實測確認 | medium | **closed** | `backend/app/ingest.py:51` |
| [007](007-heading-never-populated.md) | `heading` 從未訂閱，地圖機頭永遠指北 ✔實測確認 | medium | **closed** | `backend/app/ingest.py` |
| [008](008-readme-test-script-port-conflict.md) | README 測試腳本用 14540，與 backend 搶埠 | low | **closed** | `README.md:65` |
| [009](009-sitl-log-fills-disk.md) | SITL 沒掛 TTY，log 以 4.9GB/hr 寫爆磁碟 ✔實測確認 | critical | **closed** | `docker-compose.yml` |
| [010](010-missions-idle-columns.md) | missions.drone_id / status 欄位閒置（無資料來源）| low | open | `db/init/01_schema.sql` |
| [011](011-register-drone-not-wired.md) | 「註冊無人機」表單未接線（建立資料列但不會連線）| low | open | `apps/frontend/app/drones/page.tsx` |
| [012](012-command-service.md) | command 服務：自製 GCS 指令能力（取代 QGC 作業流程）| medium | open | `doc/gcs-replacement.md` §1 |
| [013](013-group-missions.md) | 群組任務：群飛 group 概念同時配置路徑 | medium | open | `doc/gcs-replacement.md` §3 |
| [014](014-two-tier-collection.md) | 兩層收集：機上傳出資訊全數收集（原始層已實作）| medium | in-progress | `apps/backend/app/capture.py` |

「✔實測確認」= 2026-08-03 首次實飛（SITL 起飛 → 進干擾區 → RTL）取得的實際資料佐證，
不只是讀碼推論。詳見 [progress/log/2026-08-03.md](../progress/log/2026-08-03.md)。
