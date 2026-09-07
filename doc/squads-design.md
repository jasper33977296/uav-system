# 小隊（常設編組）設計

- 狀態：**已核准並實作完成**（核准：使用者 2026-09-08；實作：後端六條驗收全過、前端落地同日）。使用者需求原話：
  「我想要多一個群組的功能，先在這把無人機組成小隊，要分派群飛任務時比較方便」
- 原型：[drones-redesign-proto.html](drones-redesign-proto.html)（機隊管理頁上半）
- 相關：[group-missions-design.md](group-missions-design.md)（一次群飛的執行模型）、
  [data-schema.md](data-schema.md)、`issues/013-group-missions.md`

---

## 1. 小隊是什麼、不是什麼

**小隊＝一份常設名單。** 先把「哪幾台一起飛」定下來，派群飛任務時直接選一隊，
不必每次重新勾機。

**小隊不是任務設定。** 隊形、高度分層（`vsep_m`）、航線、返航錯開——這些全部
在派任務的時候決定，不存進小隊。

> **為什麼要把這條寫死**：小隊很容易長成「第二套任務規劃」——今天存個預設
> vsep，明天存個預設航線，後天就有兩個地方可以決定同一件事，而它們會不一致。
> 那時候操作員得先知道「哪一個贏」才敢按。**同一個決定只能有一個家。**

**小隊不影響任何飛安判定。** 入列（admission）、預檢、守門、能力 gate 全部照舊
**逐機**判定。小隊只是選機的捷徑，不是繞過檢查的捷徑。

## 2. 為什麼不重用 `mission_groups`

`mission_groups` 是**一次群飛的執行實例**：它有 `status`（draft/executing/flying/
aborted…）、`base_mission_id`、每台一條 materialized 任務、`phase`、`error`。
一次飛行一筆，飛完就是歷史。

小隊是**跨飛行存在的名單**。硬塞進同一張表會長出兩種矛盾：

* 「status 永遠是 draft 的群組」——那個欄位對常設編組沒有意義，但它會出現在
  每一個讀 status 的地方。
* **刪掉一次飛行紀錄＝刪掉編組**。歷史與名單的生命週期不同，不該共用一列。

所以是兩張新表 ＋ 一個指向：**小隊派出去的那一次，`mission_groups` 記得自己
來自哪一隊**。

## 3. 資料模型（新增）

```sql
-- 常設編組
squads
  id          UUID PK
  name        TEXT NOT NULL          -- 使用者取的、**可改**（§5）
  note        TEXT                   -- 選填，一句用途（「低速測線用」）
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()

-- 成員（多對多：一台機可以同時屬於多個小隊）
squad_members
  squad_id  UUID NOT NULL REFERENCES squads(id) ON DELETE CASCADE
  drone_id  UUID NOT NULL REFERENCES drones(id) ON DELETE CASCADE
  position  INT  NOT NULL DEFAULT 0  -- 顯示順序；派任務時 layer_index 的**預設種子**
  PRIMARY KEY (squad_id, drone_id)

-- 既有表加一欄：這次群飛是哪一隊派出去的
mission_groups
  + squad_id UUID NULL REFERENCES squads(id) ON DELETE SET NULL
```

四個決定與理由：

**① 多對多，不是 `drones.squad_id`。** 同一台機在不同實驗扮不同角色是常事
（今天在「低速測線隊」、明天在「高空對照隊」）。一欄外鍵會逼人二選一。

**② `position` 是顯示順序與預設種子，不是 `layer_index`。** 分層要看 vsep、
航線高度、地形，那是派任務當下的決定；小隊只提供一個合理的起始順序，
派任務頁仍可調整。**兩個欄位分開，才分得出「我們排的順序」與「這次實際飛的
分層」。**

**③ 名稱唯一（`UNIQUE (lower(name))`）。** 小隊是拿來喊的——「等一下派 A 隊
出去」。兩隊同名時，畫面上分得出（有 id），人喊出來分不出。代價是改名可能撞名，
此時如實回「已經有一隊叫這個名字」。

**④ 刪小隊不刪歷史。** `ON DELETE SET NULL` 讓過去的群飛紀錄留著；而
`mission_groups.name` 在派任務時就寫入**當時的隊名快照**（如
`測試小隊 · 09/08 14:03`），所以即使小隊被刪、FK 斷了，歷史仍說得出當時是哪一隊
飛的。**不另外加 `squad_name` 欄位**——既有的 `name` 本來就是快照語意。

### 遷移

照專案既有做法：**全部寫在 `db.migrate()`**（啟動時跑的冪等
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`）。
`db/init/01_schema.sql` 只有最早那批表，`mission_groups`、`blackouts`、
`video_segments` 都在 migrate 裡——小隊照同一條路，不動 init 檔。
**沒有資料要回填**：這是純新增，舊資料不受影響。

外鍵 `mission_groups.squad_id` 用 `DO $$ … EXCEPTION WHEN duplicate_object`
包起來——`ADD CONSTRAINT` 沒有 `IF NOT EXISTS`，而 migrate 每次啟動都會跑。

## 4. 「上次群飛 · 共 N 趟」怎麼算

```sql
SELECT g.squad_id,
       max(g.created_at)            AS last_flight,
       count(*)                     AS flights          -- 群飛次數，不是架次數
  FROM mission_groups g
 WHERE g.squad_id IS NOT NULL AND g.status <> 'draft'
 GROUP BY g.squad_id
```

**「共 2 趟」＝這一隊一起飛過 2 次**（2 個 group），不是 2 個 `flight_sessions`
——三台一起飛一次會產生三個架次，把它顯示成「6 趟」會讓人以為飛了六次。
`status = 'draft'` 的排除掉：草稿是「想過但沒飛」。

## 5. API

| 方法 | 路徑 | 說明 |
|---|---|---|
| `GET` | `/api/squads` | 全部小隊：`{id, name, note, members:[{drone_id, position}], last_flight, flights}`。**成員只回 id**——狀態（在線／訊號／電量）前端已經有，join 在畫面做，不重複一份會過期的快照 |
| `POST` | `/api/squads` | `{name, note?, members:[drone_id]}`；`members` 至少 1 台 |
| `PATCH` | `/api/squads/{id}` | `{name?, note?, members?}`。**改名就是改這一欄**（使用者要求 2026-09-08）；`members` 給了就是整份取代（差異比對在前端做，避免「加一台」與「換一批」兩種語意混在同一支） |
| `DELETE` | `/api/squads/{id}` | 只刪編組，不動任何一台機與其紀錄 |

**派任務不新增第二條路徑**：`POST /api/groups` 加一個選填的 `squad_id`。
後端據此把成員展開成 `drones[]`（沿用既有 `GroupIn`），寫回 `mission_groups.squad_id`，
`name` 預設帶隊名快照。**執行、預檢、衝突檢查、gate 全部走既有那條路**——
小隊只是省掉勾機那一步。

錯誤語意（照專案慣例，說得出是哪一件）：

* 名稱撞號 → 409 `已經有一隊叫「A 隊」`
* 成員裡有已被刪除的機 → 422 `名單裡有 1 台機的記錄已經不存在了`
* 空名單 → 422 `一隊至少要有一台機`
* 派任務時同一台機被兩隊同時選中 → **沿用既有的跨路徑衝突預檢**（§4 of
  group-missions-design），不在小隊這一層擋

## 6. 邊界情況（畫面上要分得開）

| 情況 | 呈現 | 理由 |
|---|---|---|
| 成員機被刪除 | CASCADE 掉出名單，小隊人數自動變少 | 名單裡不留鬼影；那一趟的歷史仍在 `mission_groups` |
| 小隊剩 0 台 | **保留空小隊**，畫面說「這隊已經沒有成員」 | 自動刪掉會讓人以為自己按錯了。要刪由人決定 |
| 成員此刻離線 | 成員膠囊照畫，狀態字寫「未連線」 | 派任務前最想知道的就是「這隊現在幾台在線」 |
| 同一台機在多隊 | 兩隊都顯示它，機列上掛多個小隊 chip | 這是刻意允許的（決定①） |
| 改名 | 立即生效；**歷史不受影響** | `mission_groups.name` 是當時的快照（決定④） |

## 7. UI（原型已畫，見 drones-redesign-proto.html）

機隊管理頁最上方一段「小隊」：

```
小隊（1）  [＋ 新增小隊]                                          ⓘ
┌──────────────────────────────────────────────────────────────┐
│ 測試小隊  2 台  低速測線用            [編輯] [派任務] [刪除]  │
│ ⬤ pi5-sdmodelh7v2-ardu 飛行中   ◯ uav-s2 未連線               │
│ 上次群飛 09/02 15:47  共 2 趟                                 │
└──────────────────────────────────────────────────────────────┘
```

* 「編輯」＝**改名與改成員同一個 modal**（勾選清單＋名稱欄）。
* 「派任務」跳到即時頁的任務控制，帶著這一隊——隊形與分層在那裡決定。
* 機列上掛小隊 chip（中性色：**accent 只准互動 chrome**，這是標籤不是按鈕）。
* 解釋（小隊只是名單、一台可屬多隊）住頁首 ⓘ，不佔版面。

## 8. 不做的事（現在）

* **小隊層級的預設參數**（vsep、返航高度）——見 §1 的理由。真的需要時再談，
  而且要先回答「與派任務頁的設定誰贏」。
* **小隊層級的權限／鎖定**——目前系統沒有使用者概念。
* **自動編隊建議**（依機型/電量自動分隊）——那是另一個題目，且需要先有實測依據。

## 9. 驗收

1. 建一隊、改名、加減成員、刪隊；重整後狀態一致。
2. 刪掉一台在隊裡的機 → 它從名單消失，小隊還在，歷史群飛紀錄仍查得到。
3. 刪掉一整隊 → `mission_groups` 那幾筆還在，且畫面上仍說得出當時是哪一隊。
4. 用小隊派一次群飛 → `mission_groups.squad_id` 有值、`name` 帶隊名快照、
   `flight_sessions.group_id` 照舊綁得上（既有因果鏈不變）。
5. 兩隊同名 → 409，訊息說得出撞到哪一個名字。
6. 一台機同時在兩隊、且兩隊同時派任務 → 既有衝突預檢擋下，訊息指得出是哪一台。
