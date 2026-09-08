# 任務與路徑：三個名詞，兩個名字（使用者定案 2026-09-08）

- 狀態：**設計已核准，分兩階段落地**（使用者：「直接 B」）
- 相關：[data-schema.md](data-schema.md)、[ui-spec.md](ui-spec.md) §4／§6b、
  `issues/020-session-mission-association-broken.md`、
  `issues/023-missions-table-role-cleanup.md`

---

## 1. 問題：系統裡有三件事，只有兩個名字

| 概念 | 是什麼 | 今天叫什麼 |
|---|---|---|
| **路徑（plan）** | 一份 QGC `.plan`——航點、高度語意（frame）、圍欄、home、速度。**是一份檔案，不是一次飛行** | `missions` 表（**名字取錯了**；UI 已經叫它「路徑管理」） |
| **架次（session）** | 一次解鎖到上鎖。物理事實，不跨飛機、不跨電池 | `flight_sessions` ✅ |
| **任務（mission）** | 要達成的那件事。**可以跨多份路徑、跨多個架次、跨多台機** | **不存在** |

使用者原話：**「一個 mission 可以接受執行多個 plan 路徑規劃」**。

今天的後果：

* `flight_sessions.mission_id` 指的其實是**路徑**；
* 資訊頁、比較頁、回放頁寫「任務」的地方指的也都是**路徑**；
* 比較頁的「任務」維度整個假設「一趟＝一條路徑」（已用
  `plan_changes` 標 ⚠，但那是補丁不是解法）；
* **真正在扮演「任務」角色的是 `sessions.note` 那個實驗標籤字串**——
  一個沒有結構的欄位。「同一件事的三趟」今天是靠人記住的。

## 2. 為什麼這個改名危險，以及怎麼讓它不危險

**最危險的做法是「一邊把 `missions` 改成 `plans`，一邊建一張新的 `missions`」。**
任何一處漏改的 `mission_id` 不會報錯——它會**安靜地綁到新表上**，
把「路徑」的意思換成「任務」，而兩者都是 UUID，型別上完全合法。
那種錯不會有例外、不會有 500，只會在幾週後變成一批對不上的資料。

**所以分兩階段，而且中間必須有一段「`missions` 這個名字不存在」的時間。**

```
階段 1：missions → plans（全面改名，不改任何行為）
        ↓  此時資料庫沒有 missions 這張表、API 沒有 /api/missions
        ↓  任何漏改都會**炸得很大聲**（relation does not exist / 404）
        ↓  驗收：全 repo grep 不到指涉「路徑」的 mission
階段 2：建立 missions（真任務），flight_sessions.mission_id 指向它
```

血量（2026-09-08 實測）：**26 個資料庫欄位／表名、67 個檔案、802 行**
提到 `mission`。

## 3. 階段 1 的 schema 設計（改名，不改行為）

### 3.1 現況：受影響的欄位與外鍵（2026-09-08 從執行中的資料庫匯出）

| 外鍵 | 刪除行為 | 筆數 |
|---|---|---|
| `waypoints.mission_id` → `missions` | CASCADE | 40 |
| `flight_sessions.mission_id` → `missions` | SET NULL | 35 |
| `drones.current_mission_id` → `missions` | SET NULL | 3 台 |
| `mission_groups.base_mission_id` → `missions` | SET NULL | 0 |
| `group_assignments.mission_id` → `missions` | SET NULL | 0 |

`missions` 本身 3 列。**資料量小，但外鍵有五條**——這正是要用 `RENAME`
而不是「新表＋複製＋刪舊表」的理由：後者要自己重建五條外鍵、兩個
CASCADE，錯一條就是刪除行為靜靜地變了。

### 3.2 改名對照（完整）

| 舊 | 新 | 型別 | 說明不變 |
|---|---|---|---|
| `missions`（表） | `plans` | — | 一份 `.plan` 的快照庫 |
| `waypoints.mission_id` | `plan_id` | uuid → `plans` CASCADE | 航點屬於哪份路徑 |
| `flight_sessions.mission_id` | `plan_id` | uuid → `plans` SET NULL | **解鎖那一刻**那台機要飛的路徑 |
| `flight_sessions.mission_name` | `plan_name` | text | 名稱快照（路徑刪了仍說得出飛的是哪份） |
| `drones.current_mission_id` | `current_plan_id` | uuid → `plans` SET NULL | 這台機上現在裝的是哪份 |
| `mission_groups.base_mission_id` | `base_plan_id` | uuid → `plans` SET NULL | 群飛從哪份路徑分層出來 |
| `group_assignments.mission_id` | `plan_id` | uuid → `plans` SET NULL | 每台機被材料化出來的那份 |

**API JSON 的鍵一起改**（`mission_id`／`mission_name` → `plan_id`／`plan_name`）。
留著舊鍵等於階段 2 之後同一個鍵有兩個意思——那正是這整件事要避免的。

### 3.3 **不動**的東西（清單，逐項有理由）

| 不動 | 為什麼 |
|---|---|
| `MISSION_CURRENT`／`MISSION_ACK`／`MAV_CMD_NAV_*` | **MAVLink 協定詞**。協定裡的 mission 就是機上的航點清單，不歸我們改 |
| `command_log.action` 的值（`mission_upload`／`mission_fly`／`mission_start`…） | **那是歷史資料，不是程式識別字**。改了等於竄改既有紀錄，而且所有依 action 查詢的地方（資訊頁、驗收腳本）會查不到舊列 |
| 事件型別 `mission_state`／`mission_progress`／`mission_shown` | 同上：`events.type` 是存下來的值 |
| `mission_groups`（表名） | 它是「一次群飛的執行實例」，新詞彙裡既不是路徑也不是任務。**它的正確名字要等階段 2 之後才看得清楚**（很可能是「一個任務的一次群飛執行」）。只改它的 `base_mission_id` 欄位 |
| `MISSIONS_DIR`／`missions/` 目錄 | 外部觸發用的 `.plan` 檔放置處，屬於部署設定；改它要動 compose 與現場機器 |

### 3.4 遷移的**執行順序**是硬約束（實作時踩到，記在這裡）

`migrate()` 自己就會跑到參照新名字的 SQL。第一次實作時把改名放在
`migrate()` 中段，結果**同一支函式前段的建表／建索引先參照了 `plans`**，
啟動即 `relation "plans" does not exist`，服務起不來。

所以：

1. **改名必須是 `migrate()` 的第一件事**，在任何其他 DDL／DML 之前；
2. 改名本身要能重跑——先問 `to_regclass('public.plans')` 與
   `information_schema.columns`，名字已經是新的就跳過；
3. **不可以有「一半舊一半新」的中間狀態**：七項改名要在同一個
   transaction 裡（asyncpg 的 `async with con.transaction()`）。
   中途失敗而留下三個新名字四個舊名字，會是最難救的狀態。

```sql
-- 都在同一個 transaction，且是 migrate() 的第一段
ALTER TABLE missions          RENAME TO plans;
ALTER TABLE waypoints         RENAME COLUMN mission_id         TO plan_id;
ALTER TABLE flight_sessions   RENAME COLUMN mission_id         TO plan_id;
ALTER TABLE flight_sessions   RENAME COLUMN mission_name       TO plan_name;
ALTER TABLE drones            RENAME COLUMN current_mission_id TO current_plan_id;
ALTER TABLE mission_groups    RENAME COLUMN base_mission_id    TO base_plan_id;
ALTER TABLE group_assignments RENAME COLUMN mission_id         TO plan_id;
```

`db/init/01_schema.sql` **也要改**（新環境從那裡建庫，不跑 `migrate()` 的改名段）。

### 3.5 API 與前端

* `/api/plans*` 成為正式路徑（11 條路由）。
* **`/api/missions*` 一律 308 轉到 `/api/plans*`**——308 保留 method 與 body，
  POST／PATCH／DELETE 都轉得過去；301／302 會被某些 client 改成 GET。
* **階段 2 不得在轉址還在的時候開始**：`/api/missions` 到那時要換意思。
  移除轉址之前先看存取日誌確認沒有人還在打舊路徑。
* 前端路由 `/missions` → `/plans`（舊路由 307 轉址，同 `/captures` 的作法）。

外部呼叫端（實測 14 處）：`scripts/test-flight.py`、`scripts/fly-mission.py`、
`scripts/conformance/mission_*.py`、`scripts/uitest/empty_state.mjs`、
`apps/command/app/{main,plans,guard_client}.py`、前端 4 個檔。

### 3.6 驗收（階段 1 做完才算完）

1. `select to_regclass('public.missions')` 回 **NULL**；`plans` 回非 NULL。
2. 五條外鍵的刪除行為與 §3.1 完全相同（`pg_constraint` 逐條比對）。
3. `waypoints` 40、`flight_sessions` 35、`missions/plans` 3 列——**筆數不變**。
4. 全 repo `grep -n "mission"` 的每一個命中，都落在 §3.3 的白名單裡。
5. 三支既有腳本跑得過：`scripts/test-flight.py`、`conformance/mission_upload.py`、
   `scripts/uitest/empty_state.mjs`。
6. 五個頁面開得起來且資料正確：即時、機隊、路徑、比較、資訊。

## 4. 階段 2 的 schema 設計：建立「任務」

**前提：階段 1 的驗收全過，而且 `/api/missions` 的 308 轉址已經移除。**

```sql
CREATE TABLE IF NOT EXISTS missions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at    TIMESTAMPTZ                     -- NULL＝還在進行
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_missions_name ON missions (lower(name));

ALTER TABLE flight_sessions
  ADD COLUMN IF NOT EXISTS mission_id   UUID REFERENCES missions(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS mission_name TEXT;
```

| 欄位 | 型別 | 說明 |
|---|---|---|
| `missions.name` | text NOT NULL，`UNIQUE(lower(name))` | 任務是拿來喊的（「那個低速測線的實驗」）。**兩個同名任務畫面上分得出（有 id），人喊出來分不出**——與 `squads` 同一條理由。撞名回 409，訊息說得出撞到哪一個 |
| `missions.ended_at` | timestamptz | **只給畫面分「進行中／已結束」，不影響任何判定**。不做狀態機——`squads` 的教訓：任務不該長成第二套任務規劃 |
| `flight_sessions.mission_id` | uuid → `missions` **SET NULL** | 刪任務不刪歷史 |
| `flight_sessions.mission_name` | text | 名稱快照，與 `plan_name` 同一條理由：任務刪掉之後，歷史仍要說得出當時屬於哪個任務 |

### 4.1 四個決定

**① 一個任務 N 個架次、N 份路徑、N 台機。** 三個 N 都不設限——
限制哪一個都會在某次實驗被打破。這也是為什麼關聯放在 `flight_sessions`
那一側（多對一），不需要中介表。

**② 指派是人做的，系統不猜。** 架次結束後在資訊頁指定「這趟屬於哪個任務」，
或當場開一個新任務。**不做自動歸類**：時間相近、路徑相同都不足以證明是
同一件事，而**猜錯的歸類比沒有歸類更難發現**。

**③ 不動 `sessions.note`。** 它今天扮演的正是任務的角色（實驗標籤），
但那是使用者自己寫的字，**不能自動搬進 `missions.name`**——搬錯就是
替他決定了兩趟屬於同一件事。畫面上可以在指派時把 `note` 當預設建議值，
按不按是他的事。

**④ `ended_at` 不自動關。** 沒有任何規則說得出「這個任務結束了」——
最後一趟飛完之後三天，人可能還要補一趟。

### 4.2 畫面（階段 2 落地時）

* 資訊頁架次詳情多一列「任務」，可指派／可改／可開新的。
* 比較頁：原本的「任務」維度改名「**路徑**」（它一直都是路徑），
  另外新增一個真正的「任務」維度。
* **任務清單頁先不做。** `sessions` 的篩選夠用，等真的需要再說。

### 4.3 驗收

1. 建任務、改名、把三趟指到同一個任務、刪任務 → 架次還在、`mission_name`
   仍說得出當時的任務名。
2. 兩個同名任務 → 409，訊息說得出撞到哪一個。
3. 比較頁的兩個維度分得開：「路徑」按 `plan_id`、「任務」按 `mission_id`。
4. 一個任務底下混著不同路徑、不同機的架次，畫面不當掉也不亂講。

## 5. 不做的事

* **`mission_groups` 改名**——見 §3，等階段 2 之後再看它的正確名字。
* **自動歸類架次到任務**——見 §4②。
* **任務層級的參數／預設值**——`squads` 的同一條理由：那會長成第二套規劃。
