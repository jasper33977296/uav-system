# 任務與路徑：三個名詞，兩個名字（使用者定案 2026-09-08）

- 狀態：**兩階段皆已落地**（2026-09-08。階段 1：`30ceacf`；階段 2 見本檔 §4）
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

**前提（已滿足）**：階段 1 驗收全過；移除 308 轉址前查過存取日誌——
改名之後 `/api/missions` 只被打過 1 次，而那是我自己的驗證 curl。
前端 `/missions` 的 307 也一併移除，**那個網址從此是 404**：
它屬於「任務」了，而任務清單頁按 §4.2 先不做。

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

### 4.3 驗收（2026-09-08 全過）

1. ✅ 建任務、指派三趟、改名、刪任務 → **三趟都還在**，`mission_id` 變 NULL
   而 `mission_name` 快照留著（畫面上寫「原屬「X」，該任務已刪除」）。
2. ✅ 兩個同名任務 → 409 `已經有一個任務叫「低速測線實驗」`。
3. ✅ 比較頁四個維度：時間／**路徑**（原本叫「任務」，它一直都是路徑）／
   **任務**（新）／機隊。
4. ✅ 衍生統計（架次數／機數／路徑數／最近一趟）都對；統計不存欄位。

**過程中修掉一個自己造的洞**：任務維度的候選原本寫
`r.mission_id === taskId`，而還沒選任務時 `taskId` 是 `null`——
於是**每一趟沒指派的架次都比對成相等**，畫面上變成「全部都在這個任務底下」。
還沒選就是空的，不是「全部未指派」。

## 4.4 詞彙分層：實作照協定，畫面照我們的定義（使用者定案 2026-09-08）

使用者原話：**「對他們來說一個路徑就是一次任務，但我認為它就是一個路徑而已，
我們的定義更廣……把實作跟業務邏輯拆開」**。

QGC／MAVLink／飛控圈子把機上那份航點清單叫 **mission**。我們的「任務」比它大
一級（可跨多份路徑、多趟、多台機）。兩邊都對，但**不能在同一個畫面上同時用**。

所以分層：

| 層 | 用哪個詞 | 例子 |
|---|---|---|
| **協定／實作層** | 照 MAVLink 說 `mission` | `MISSION_CURRENT`、`MAV_CMD_NAV_*`、`command_log.action = 'mission_upload'`、`events.type = 'mission_state'`、API 路徑 `/mission/current`、程式識別字 |
| **業務／畫面層** | 我們的定義：**路徑**與**任務** | 「上傳路徑」「起飛→執行路徑」「繼續路徑」「路徑進度」；「任務」只指跨趟的那件事 |

**中間那一層的翻譯就是 `ACTION_LABELS` 與 `evtext`**：它們吃協定層的值
（`mission_upload`、`mission_state`），吐業務層的話（「上傳路徑」「路徑狀態」）。
兩層在那裡交界，而且只在那裡交界。

### 這一輪改掉的畫面字（2026-09-08）

| 原本 | 現在 |
|---|---|
| 任務控制（面板標題） | **飛行控制** |
| 起飛→任務 | 起飛→執行路徑 |
| 啟動任務／開始任務 | 開始執行路徑 |
| 繼續任務 | 繼續路徑 |
| 中斷任務 | 中斷路徑 |
| 更換任務⋯ | 更換路徑⋯ |
| 清除任務 | 清除機上路徑 |
| 選擇任務⋯ | 選擇路徑⋯ |
| 機上任務： | 機上路徑： |
| 派任務（小隊） | **派飛** |
| 執行任務中 | 執行路徑中 |
| 任務狀態／任務進度（事件） | 路徑狀態／路徑進度 |
| 目前不在任務模式 | 目前不在自動模式 |
| 任務資訊（回放頁） | 這一趟 |
| 無任務 | 無路徑 |

**一個字都沒有動到協定層**：`mission_upload`／`mission_state`／`MISSION_*`／
`/api/mission/current` 原樣不動，它們是資料與協定，不是說法。

> 這一輪是階段 1 的漏網。設計 §3.5 寫了「畫面上『任務』一律改成『路徑』」，
> 但實作時只掃了資料層與幾個頁面，**指令面板那一批字一個都沒動**——
> 於是「任務」在同一套畫面上同時是路徑與任務，是使用者發現的。

## 4.5 任務的生命週期：起飛時命名，落地時問要不要結束（使用者定案 2026-09-08）

使用者要求三件：資訊頁那顆「＋ 新任務」改成改名；**即時頁第一次開始飛行時
讓使用者輸入任務名稱**；**落地時通知使用者任務結束**。

### 這修正了 §4.1② 的一半

§4.1② 說「指派是人做的，系統不猜」。**那條沒有變**——變的是**問的時機**：
與其飛完之後回頭一趟一趟指，不如**在起飛那一刻問一次**，之後同一個任務底下
的每一趟自動歸進去。人還是那個做決定的人，只是決定提前到它最便宜的時刻。

系統仍然**不猜**任何一件事：不猜名字、不猜哪幾趟算同一件事、不猜任務何時結束。

### 規則

```
起飛（建立架次）
  ├─ 有「進行中」的任務 → 自動歸到它（後端 create_session 做）
  └─ 沒有                → 即時頁跳出來問名字 → 建立 + 歸入
落地（架次結束）
  └─ 問：「任務『X』要結束嗎？」 → 結束／還要再飛
```

**「進行中」＝`ended_at IS NULL`。**
> ⚠ **「同時只能有一個」這條已於同日被 §4.6 取代**——使用者的目標是
> 多組同時跑多個任務。下面這段留著是為了說明它當初回答的是哪個問題。
> 原文：*而且同時只能有一個*。
用 partial unique index 擋住：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_missions_one_active
  ON missions ((ended_at IS NULL)) WHERE ended_at IS NULL;
```

> **為什麼要限制成一個。** 「自動歸到進行中的那個」只有在「那個」唯一時才是
> 一句確定的話。兩個同時進行的話，自動歸類就得猜——而猜錯的歸類比沒有歸類
> 更難發現（§4.1②）。代價是不能同時跑兩個實驗；**這個場地一次只有一組人在飛**，
> 而真的需要時，結束一個再開一個是一次點擊。
>
> 這條限制是**這一版的取捨，不是真理**。要放寬的話，自動歸類就得換成
> 「起飛時選一個」，那是另一個設計。

### 落地時不自動結束

落地只是**這一趟**結束，不是任務結束——一個任務本來就可以有多趟。所以落地時
**問**，不自動關（§4.1④ 的同一條：沒有任何規則說得出「這個任務結束了」）。

問的時機：架次結束（`ended_at` 有值）。**不用 `landed_state`**——那是錄影的
判準；架次結束才是「這一趟結束了」這件事本身。

### 資訊頁那顆按鈕

| 這一趟的狀態 | 按鈕 | 為什麼 |
|---|---|---|
| 已歸到某個任務 | **改名** | 起飛時打的名字事後才發現不對，是最常見的需求 |
| 還沒歸 | **＋ 新任務** | 35 趟歷史架次是在這個流程之前飛的，它們得有辦法補歸 |

一顆按鈕，標籤說得出它按下去會做什麼。

**補歸歷史架次要用「已結束」的任務。** 同時只能有一個進行中，而回頭替以前
飛過的那幾趟開一個任務，不該把現在正在進行的那個擠掉——所以資訊頁那個入口
建立時一律 `ended: true`（`POST /api/missions` 多一個 `ended` 旗標）。
**進行中的任務只從即時頁的起飛流程開。**

**改名不影響歷史**：架次上的 `mission_name` 是指派當下的快照。
所以「改名」要**同時更新那個任務底下所有架次的快照**——不然畫面上會出現
「任務叫 A，但這一趟寫著原屬 B」。這是與 §4 原本設計不同的一點，理由：
起飛時打錯字是常態，而那個快照的用途是「任務被刪之後還說得出當時叫什麼」，
不是「記住每一次改名前的舊名字」。

## 4.6 多組同時跑多個任務（使用者目標 2026-09-08）——**§4.5 的限制要拿掉**

使用者原話：**「我的目標是可多個無人機群組同時執行多個任務」**。

§4.5 加的那條「同時只能有一個進行中的任務」**直接擋住這件事**，必須拿掉。
但它當初是為了回答一個真問題：**起飛的那一刻，這一趟屬於哪個任務？**
唯一時那句話才確定。所以不能只是把限制刪掉，要換一個同樣確定的答案。

### 換的答案：任務有參與的機，起飛時按機查

```
起飛（建立架次）
  └─ 找「這台機參與中、而且進行中」的任務
       恰好一個 → 歸給它
       零個     → 問（即時頁跳出來，同 §4.5）
       兩個以上 → **不可能**（見下面的限制）
```

新的限制比舊的窄得多，而且是使用者定的那一條：
**一台機同時只能執行一個任務**（使用者原話 2026-09-08）。

### 綁定：小隊或機，兩種都要（使用者定案）

> 使用者原話：**「任務可以綁小隊也可以綁機，但機一定一次只能執行一個任務」**。

```sql
ALTER TABLE missions ADD COLUMN IF NOT EXISTS squad_id UUID
  REFERENCES squads(id) ON DELETE SET NULL;         -- 綁一整隊（活的連結）

CREATE TABLE IF NOT EXISTS mission_drones (          -- 綁單台
  mission_id UUID NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  drone_id   UUID NOT NULL REFERENCES drones(id)   ON DELETE CASCADE,
  PRIMARY KEY (mission_id, drone_id)
);
```

**有效參與名單 ＝ 直接綁的機 ∪ 綁的小隊的成員**，**在查詢時展開，不存快照**。

> 為什麼是活的連結而不是快照：「綁小隊」的意思就是**小隊改成員，任務跟著變**。
> 存快照的話那句話會變成假的——而使用者要的是綁，不是「用小隊挑一次機」。
> 代價寫在下一段。

### 不變式與它的三個檢查點

**一台機不得同時出現在兩個「進行中」任務的有效名單裡。**

partial unique index 做不到：它的 `WHERE` 需要問 `missions.ended_at`，
而 partial index 的條件必須不可變、只吃本表欄位（**這是實際試出來的**，
PostgreSQL 直接拒絕子查詢）。所以用**觸發器**，一個共用的檢查函式掛三個地方：

| 寫入 | 為什麼要檢查 |
|---|---|
| `mission_drones` INSERT／UPDATE | 直接把一台機加進第二個任務 |
| `missions` UPDATE（`squad_id`、`ended_at`） | 換綁小隊、或把已結束的任務重新開起來 |
| **`squad_members` INSERT／UPDATE** | **最容易漏的那一個**：把一台已經在任務 B 的機加進小隊 S，而 S 綁在任務 A ——沒有人碰 `missions` 或 `mission_drones`，不變式卻被打破了 |

不用「應用層自己記得檢查」：那條規則會在某次改動被繞過，而它是自動歸類唯一
的前提。**約束要住在資料庫裡。**

### 參與名單怎麼填

起飛時問名字的那個 modal 順便問「這次誰要飛」：可以勾小隊（綁一整隊）、
也可以勾單台。預設帶**當下連線中的機**。

### 這與 `mission_groups`、`squads` 的關係（三者不重疊）

| | 是什麼 | 生命週期 |
|---|---|---|
| `squads` | **常設編組**：哪幾台常常一起飛 | 跨任務、跨飛行 |
| `missions` | **要達成的那件事** | 一段實驗；可以有多趟、多份路徑、多台機 |
| `mission_groups` | **一次群飛的執行實例**（同時起飛、分層、材料化的那一批） | 一次飛行 |

一個任務底下可以有好幾次群飛（`mission_groups`），也可以有單機的架次。
**`mission_groups` 應該多一個 `mission_id`** 指向任務——那才是它現在缺的
上層歸屬（它今天只有 `squad_id` 與 `base_plan_id`）。

### 要改的東西

| 現在 | 改成 |
|---|---|
| `idx_missions_one_active`（全域只能一個） | **刪掉** |
| `create_session` 取 `missions WHERE ended_at IS NULL` | 取「這台機參與中且進行中」的那一個 |
| `GET /api/missions/active` 回單一 | 回**清單**；另加 `?drone_id=` 回那台機的那一個 |
| 即時頁起飛 modal | 名字 ＋ **參與的機**（可勾小隊） |
| 落地 modal | 問的是**這台機所屬的那個任務**要不要結束，不是「唯一那個」 |
| 資訊頁建立時一律 `ended: true` | 不再需要——多個進行中本來就合法 |

### 驗收（2026-09-08 全過）

1. ✅ 兩個任務同時進行：甲綁小隊「低速測線隊 A」（2 台）、乙直接綁隊外那一台。
   `GET /api/missions/active?drone_id=` 各自回自己的那一個。
2. ✅ 把甲的機加進乙 → **409**，訊息是
   `「pi5-sdmodelh7v2-ardu」同時被排進「任務乙」與「第一次穩定完成任務」——一台機一次只能執行一個任務`。
3. ✅ **第三個檢查點也擋得住**：把乙的機加進甲綁的那個小隊（沒有碰
   `missions` 也沒有碰 `mission_drones`）→ 一樣 409。
4. ✅ 沒有參與任何進行中任務的機起飛 → 即時頁跳出來問（截圖確認：名稱欄、
   小隊下拉、可勾的機、以及「一台機一次只能執行一個任務」那句提醒）。

**修過一個自己造的洞**：`PATCH /api/missions` 換名單時撞到不變式會回 500——
觸發器的例外在 `mission_drones` 那一段才被踩到，而 `try` 只包了
`UPDATE missions`。兩段都要各自把它翻成 409 的人話。

## 5. 不做的事

* **`mission_groups` 改名**——見 §3，等階段 2 之後再看它的正確名字。
* **自動歸類架次到任務**——見 §4②。
* **任務層級的參數／預設值**——`squads` 的同一條理由：那會長成第二套規劃。
