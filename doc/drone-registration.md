# 無人機註冊流程

> 目的：把一台**實體無人機**變成系統裡一筆**可以被指揮、被記錄的機體**。
> 對象是要接新機或換機的人。相關但不同的文件：
> [`deploy-checklist.md`](deploy-checklist.md)（整套系統部署）、
> [`deployment.md`](deployment.md)（機端連線層設定）。

---

## 0. 先理解一件事：系統靠什麼認出「這是哪一台」

**只靠 sysid。**

    實體飛機 → 飛控參數 SYSID_THISMAV → drones.mav_sysid → 這筆記錄

註冊表單裡的其他欄位**沒有一個能辨識機體**：

| 欄位 | 實際內容 |
|---|---|
| `name` | 人取的標籤，隨時可改 |
| `serial_no` | **name 的複製品**（`INSERT ... VALUES ($1, $1, …)`），不是序號。實際作用只是「名稱不可重複」的唯一鍵 |
| `model` | 目前**沒有任何地方寫入**，永遠 NULL |
| `autopilot` | **不必填，自動偵測**（HEARTBEAT 帶的 `MAV_AUTOPILOT`）。註冊當下本來就不知道，系統不假裝知道 |

所以**「每台機的 `SYSID_THISMAV` 必須唯一」不是建議，是這個設計成立的前提**
（[`deploy-checklist.md`](deploy-checklist.md) §3 已列為勾稽項）。
兩台重號＝資料混料，而且是**靜默的**。

進行中的改善見 [issues/038](../issues/038-board-identity.md)。

---

## 1. 註冊的兩條路徑

### A. 自動註冊（預設，多數情況走這條）

**收到一個沒見過的 sysid 的自駕儀心跳**時，backend 依序嘗試三件事
（`db.drone_for_sysid`）：

1. **已有記錄的 `mav_sysid` 等於它** → 直接用那筆。
2. **主機（`is_primary`）的 `mav_sysid` 是空的** → **把這個 sysid 認領給主機**。
3. **都不是** → 自動建一筆，名稱 `uav-s<sysid>`，之後在無人機頁改名。

> ⚠️ **第 2 條是先到先得，而且它咬過人。** 2026-08-24 實際發生：一筆早已停用的
> 舊機記錄仍是主機且 `mav_sysid` 空著，新接上的四旋翼一開機就被認領進那筆記錄，
> `/api/live` 顯示的是**別台機的名字**。
>
> 判斷方法：無人機頁上該機的名稱與你接的機不符，或 `/api/live` 的 `drone_name`
> 是舊機。解法見 §4。

**只有自駕儀的心跳能建檔**——`MAV_TYPE_GCS` 或 `MAV_AUTOPILOT_INVALID` 的心跳
會被忽略，否則地面站與其他 GCS 會被當成無人機建進來。

### B. 手動註冊（先建檔、之後才接機時）

無人機頁 → 展開註冊 → **只需要名稱**：

```
POST /api/drones   {"name": "pi5-ardu-quad"}
```

建出來的記錄 `mav_sysid` 是空的，等該 sysid 的心跳出現時由上面第 1／2 條綁定。

> 這裡**不問連線位址**，也不問飛控型號：
> * 連線位址在單埠多機設計下是**地面站自己的收聽埠**（`udpin://0.0.0.0:14540`），
>   每台機都一樣、也沒有任何讀取端（[issues/036](../issues/036-live-page-display-honesty.md)）。
> * 飛控型號要等它連上講話才知道，自動偵測。

---

## 2. 接一台新機的完整步驟

### 2.1 機上（每台各做一次）

- [ ] **設定唯一的 `SYSID_THISMAV`**（ArduPilot；PX4 是 `MAV_SYS_ID`）。
      **不要留在預設值 1**，除非它確定是機隊裡唯一的一台。
- [ ] 確認 MAVLink 送得到地面站：機上代理或 mavlink 路由指向
      `<地面站IP>:14540`（資料）與 `:14541`（指令）。細節見
      [`deployment.md`](deployment.md)。
- [ ] **ArduPilot 專屬**：它預設幾乎不送遙測，要有人送
      `REQUEST_DATA_STREAM`／`SET_MESSAGE_INTERVAL`。backend 與機上代理都會送，
      但**不送就等於連得上卻是瞎的**——只測 PX4 時永遠不會發現這件事。

### 2.2 地面站

- [ ] `.env` 的 `LINK_SOURCE=modem`（真機模式）
- [ ] 服務都起著：`curl :38000/healthz`、`curl :38001/healthz`

### 2.3 上電並確認被認出來

```bash
# command 服務看到這台機了嗎（sysid 為鍵）
curl -s :38001/healthz | python3 -m json.tool

# 主機的即時狀態：名字對不對、自駕儀認出來了沒
curl -s :38000/api/live | python3 -m json.tool
```

**要確認的四件事**：

| 檢查 | 期望 |
|---|---|
| `drones` 裡出現這台的 sysid | `age_s` 是小數（持續更新） |
| `drone_name` | **是你接的那台**，不是別台的名字 |
| `autopilot` | `ardupilot` / `px4`，不是 `unknown` |
| `capabilities` | 該有的動詞是 `ok`；非 `ok` 的看 `capability_reasons` |

### 2.4 改名與設主機

- 自動建出來的叫 `uav-s<sysid>`，在無人機頁改成有意義的名字。
- 即時頁顯示的是**主機**：`POST /api/drones/{id}/primary`（或無人機頁的按鈕）。

---

## 3. 換機／退役一台機

**不要直接刪記錄**——架次、事件、任務都掛在它的 `drone_id` 上，刪掉等於刪歷史。

正確做法是**解除 sysid 綁定**，讓那筆記錄保留資料但不再對應任何實體機：

```sql
UPDATE drones SET mav_sysid = NULL WHERE id = '<舊機的 id>';
```

然後把新機註冊起來、設為主機。舊記錄的名稱、架次、影像設定全部原封保留。

> 2026-08-24 就是這樣處理 `ai-model-rb5 [掛掉了]` 的：它的 `mav_sysid=1` 是個
> **過期的宣告**（那台機已經掛了，而實際在用 sysid 1 的是另一台新機）。

---

## 4. 排查：名字對不上／兩台混在一起

| 症狀 | 可能原因 | 處理 |
|---|---|---|
| 新機顯示成舊機的名字 | §1-A 第 2 條的先到先得認領到舊記錄 | 解除舊記錄的 `mav_sysid`（§3），重啟 backend 讓記憶體中的 sysid 對應重建 |
| 兩台機的資料互相蓋掉 | **兩台 sysid 重號** | 改其中一台的 `SYSID_THISMAV`，重開機端 |
| 日誌出現 `sysid N 來源位址改變` | 同一 sysid 從不同位址送來 | 可能是換網路，**也可能是兩台重號**——後者是靜默混料，要查清楚 |
| 機出現在清單但沒有資料 | 記錄存在、實體機沒連上 | 正常。從未連上的機不會出現在即時頁（[036](../issues/036-live-page-display-honesty.md)） |

**重啟 backend 為什麼有必要**：sysid → 記錄的對應在記憶體裡，改資料庫不會自動
重新對應。

---

## 5. 目前做不到的事（誠實清單）

| 缺口 | 後果 |
|---|---|
| 註冊時**不能指定 sysid** | 只能靠自動認領，撞到舊記錄就要事後修 |
| **沒有機架序號欄位可填** | `serial_no` 被名稱佔用；實體機與記錄的對應只存在人的腦裡 |
| **兩筆記錄綁到同一個 sysid 不會示警** | schema 沒有唯一約束，靜默發生 |
| `model` 是死欄位 | 機型只有自動偵測的 `MAV_TYPE`（幾旋翼），沒有「這台叫什麼型號」 |

進行中：[issues/038](../issues/038-board-identity.md) 已加入**飛控板 UID**
（`AUTOPILOT_VERSION.uid2`）的請求與記錄——那是目前唯一**機器可驗證**的身分。

> ⚠️ 但要清楚它的界線：**`uid2` 認的是飛控板，不是機架。** 板子拆到另一台
> 飛機上，UID 跟著板子走。**機架身分只能由人維護**——所以上表第二列
> （沒有機架序號欄位）不會因為有了 UID 就消失。
