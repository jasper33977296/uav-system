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

## 3. 階段 1：改名（不改行為）

### 資料庫

```sql
ALTER TABLE missions            RENAME TO plans;
ALTER TABLE waypoints           RENAME COLUMN mission_id       TO plan_id;
ALTER TABLE flight_sessions     RENAME COLUMN mission_id       TO plan_id;
ALTER TABLE flight_sessions     RENAME COLUMN mission_name     TO plan_name;
ALTER TABLE drones              RENAME COLUMN current_mission_id TO current_plan_id;
ALTER TABLE mission_groups      RENAME COLUMN base_mission_id  TO base_plan_id;
ALTER TABLE group_assignments   RENAME COLUMN mission_id       TO plan_id;
```

**`mission_groups` 這張表暫時不改名。** 它是「一次群飛的執行實例」，
在新詞彙裡既不是路徑也不是任務——**它比較接近「一次群飛的架次群」**。
改它會把血量再擴大一輪，而且它的正確名字要等階段 2 之後才看得清楚
（很可能它就是「一個任務的一次群飛執行」）。列為後續。

**遷移用 `RENAME`，不是「新表＋複製＋刪舊表」**：`RENAME` 保住 FK、索引、
hypertable 設定與所有既有資料，而且是原子的。`migrate()` 裡要能重跑，
所以每一句包在「欄位存在才改」的條件裡。

### API

* `/api/plans*` 成為正式路徑。
* **`/api/missions*` 一律 308 轉到 `/api/plans*`**（308 保留 method 與 body，
  POST／PATCH／DELETE 都轉得過去）。
* **階段 2 不得在轉址還在的時候開始**：`/api/missions` 到那時要換意思，
  兩者不能重疊。移除轉址之前先看存取日誌確認沒有人還在打舊路徑。

外部呼叫端（實測 14 處）：`scripts/test-flight.py`、`scripts/fly-mission.py`、
`scripts/conformance/mission_*.py`、`scripts/uitest/empty_state.mjs`、
`apps/command/app/{main,plans,guard_client}.py`、前端 4 個檔。

### 前端

`/missions` 路由改成 `/plans`（舊路由 307 轉址，同 `/captures` 的作法）；
畫面上「任務」一律改成「路徑」——**除了那些真的在講任務的地方**，
而那些地方在階段 2 之前應該一個都沒有。

## 4. 階段 2：建立「任務」

```sql
CREATE TABLE IF NOT EXISTS missions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at    TIMESTAMPTZ            -- NULL＝還在進行
);
ALTER TABLE flight_sessions
  ADD COLUMN IF NOT EXISTS mission_id   UUID REFERENCES missions(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS mission_name TEXT;   -- 名稱快照，同 plan_name 的理由
```

四個決定：

**① 一個任務 N 個架次、N 份路徑、N 台機。** 三個 N 都不加限制——
限制哪一個都會在某次實驗被打破。

**② 指派是人做的，系統不猜。** 架次結束後在資訊頁指定「這趟屬於哪個任務」，
或開新任務。**不做自動歸類**：時間相近、路徑相同都不足以證明是同一件事，
而猜錯的歸類比沒有歸類更難發現。

**③ `mission_name` 存快照**，理由與 `plan_name` 同一條：任務被刪掉之後，
歷史仍要說得出當時屬於哪個任務。

**④ `ended_at` 只是給畫面分「進行中／已結束」用，不影響任何判定。**
不做狀態機——`squads` 那一條教訓：同一個決定只能有一個家，
任務不該長成第二套任務規劃。

### 畫面（階段 2 落地時）

* 資訊頁架次詳情多一列「任務」，可指派／可改。
* 比較頁多一個維度「任務」（真任務），原本的「任務」維度改名「路徑」。
* 任務清單頁——**先不做**。`sessions` 的篩選夠用，等真的需要再說。

## 5. 不做的事

* **`mission_groups` 改名**——見 §3，等階段 2 之後再看它的正確名字。
* **自動歸類架次到任務**——見 §4②。
* **任務層級的參數／預設值**——`squads` 的同一條理由：那會長成第二套規劃。
