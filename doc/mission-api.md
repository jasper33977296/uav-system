# 任務 API：選任務 → 上傳 → 執行

> 給**外部整合**用的最小介面（2026-09-02 使用者定案：MCP 先不做，
> 提供這三個就好）。機器可讀的規格見 [`openapi.json`](openapi.json)，
> 匯出方式：`python3 scripts/export-openapi.py`。

---

## 0. 三個端點

全部在 **command 服務（`:38001`）**。

**路徑一律帶版本**，版本號緊接在服務根之後：`/api/v1/…`（即時那邊的 WebSocket 是 `/ws/v1/…`）。
舊的無版本路徑保留為別名，**但對外請寫版本化的那一種**——2026-09-08 把上傳欄位 `mission_id`
改名成 `plan_id` 之所以會**無聲**打斷外部呼叫端，就是因為當時這一組沒有版本可以並存。

| 步 | 端點 | 說明 |
|---|---|---|
| ① 選任務 | `GET /api/v1/missions` | 任務庫總表。**唯讀、不吃 `ENABLE_COMMANDS`**——只是看有哪些航線，不動飛機 |
| ② 上傳 | `POST /api/v1/command/{sysid}/mission/upload` <br>`{"plan_id": "..."}` | 寫進飛控，**並逐項讀回比對**。欄位 09-08 由 `mission_id` 改名為 `plan_id`，服務端**只收新名字**，送舊的回 422 |
| ③ 執行 | `POST /api/v1/command/{sysid}/mission/start` | 讓飛控開始執行**它機上現有**的那份任務。**預設先飛到任務起始點**；要接續中斷處帶 `{"resume": true}` |

**要在自己的地圖上看執行中的飛機**：見 [`external-live-api.md`](external-live-api.md)
（控制端產生一組 UUID 當任務編號，先連上再帶著它起飛；從起飛開始每 0.5 秒送狀態，
最後一台上鎖 3 秒後結束）。**兩種傳法，同一份訊息**：WebSocket 串流
`ws://:38000/ws/v1/missions/{uuid}`，或 HTTP 輪詢
`GET :38000/api/v1/ext/missions/{uuid}/live`。

**要事後比較兩趟或多趟的訊號**：見 [`external-history-api.md`](external-history-api.md)
（一個任務的完整訊號樣本，每一筆帶沿預計航線的里程；已實作 2026-09-16）。

**想一次做完**：`POST /api/v1/start`（`{"plan_id": "<id 或名稱>"}`；`mission` 是它的舊名，暫留為別名、下一版移除）——
上傳→解鎖→起飛→切任務，每步讀回確認。
自動化流程用它；互動操作建議走三步，**因為中途出錯時看得出停在哪一步**。

---

## 1. 兩件呼叫前一定要知道的事

### 1.1 ③ 不會讓一台停在地上的機起飛，而 ② 在空中會立即改道

* **`mission/start` 預設是「重新執行」**（2026-09-21 裁定，issues/060）：先用 GUIDED
  飛到任務起始點、等到位，再送 `MISSION_START`。高度取**航線替起始點寫的高度**，
  不看機當下在多高——同一條航線不論從哪裡重跑都走同一個高度，趟與趟才比得了。
  * 起始點 3 m 內＝不飛，回應的 `steps.transit.skipped` 會說出原因。
  * 離起始點超過門檻回 `409 far_start`，帶 `accept_start_distance_m` 再送一次。
  * **要接續剛才中斷的地方請帶 `{"resume": true}`**：那時不飛回起點，
    也不送 `MISSION_START`（那會把序號歸零），只切回任務模式讓飛控從當下那一項續。
* **`mission/start` 要求機已經解鎖並在空中。** 對停在地面的機切自動任務模式，
  等於叫它自己起飛——那是 [issues/031](../issues/031-arm-guard-auto-mode.md)
  記的那次事故（2026-08-13，SITL 上真的飛起來了）。要從地面一路到飛，
  用 `/api/v1/start` 或 `mission/fly`。
* **`mission/upload` 在地面是存檔，在空中是立即生效的航線變更。**
  飛控收到新任務的那一刻就照它飛——2026-08-24 SITL 實測：上傳完成的瞬間
  飛機就掉頭了，模式全程沒變、沒有任何確認步驟。
  **飛行中要換航線請走 `mission/change-route`**（暫停→上傳→從最近的航點續飛，
  每步讀回確認，並且會先給你一份提案）。

### 1.2 沒有機上代理的機，指不動

2026-09-02 裁定：**要被本系統控制，機上一定要有代理**。沒有代理的機
（第三方的飛機、未掛代理的 SITL）**看得到、指不動**。

查一台機現在能不能被指揮：

```
GET http://<地面站>:38000/api/v1/admission/<sysid>
→ {"state": "admitted", "reason": "板號、配號、代理連線三者相符"}
```

`state` 只有 `admitted` 是可以指揮的。其餘：`seen`（不知道它是誰）／
`identifying`（身分還沒確認完）／`reassigning`（正在換 sysid，稍候）／
`quarantined`（身分與記錄矛盾）／`unmanaged`（沒有代理）。

---

## 2. 被擋下時的三種 4xx，以及它們的差別

**每一步都會先過三道門**，而它們擋的是不同的東西。**回應一定說得出下一步**
——「不可用」不是原因，「這台機沒有代理」才是。

| HTTP | `code` | 哪一道門 | 意思 |
|---|---|---|---|
| **403** | `not_admitted` | **入列** | 這台機不是（或還不確定是）我們的。附 `admission` 欄位說明是哪一態 |
| **501** | — | **能力** | 這個廠牌的這個動作還沒驗過。附 `capability` 與四態的 `state` |
| **409** | `guard_refused` | **機上守門** | 當下這個狀態不允許。**理由一定說得出「那現在能做什麼」** |
| **409** | `guard_unknown` | 機上守門 | 問不到判決。**不知道不等於可以**，所以擋下 |
| **409** | `guard_queued` | 機上守門 | 這台機失聯中，操作**沒有送出去**；已記下，恢復後會重新問一次判決並攤給人確認 |
| **409** | `proposal_drift` | 改航線 | 提案過期（機體在人看提案時移動了）。附**新的**提案，要人重看 |

> **順序有意義**：入列排在能力之前——**對一台身分不明的機談「它做不做得到」
> 沒有意義**。

實際長相：

```json
HTTP 403
{"detail": {
  "msg": "sysid 43 沒有連線中的機上代理。**本系統只指揮有代理的機**——請確認機上代理已啟動並連上地面站",
  "code": "not_admitted", "admission": "unmanaged", "sysid": 43,
  "hint": "本系統只指揮通過入列的機。緊急時實體遙控器不受影響"}}
```

**最後那句 `hint` 是刻意的**：被擋下不等於沒有退路。實體遙控器永遠不受這套
規則影響——如果不是這樣，這道門就不該這樣設計。

---

## 3. 完整流程範例

```bash
GS=http://localhost:38001
BE=http://localhost:38000

# ① 選任務
curl -s $GS/api/v1/missions | jq '.missions[] | {id, name, nav_count}'

# 先確認這台機可以被指揮（省掉一次注定失敗的呼叫）
curl -s $BE/api/v1/admission/1 | jq .state       # 要是 "admitted"

# ② 上傳
curl -s -X POST $GS/api/v1/command/1/mission/upload \
     -H 'Content-Type: application/json' \
     -d '{"plan_id":"6f812621-..."}'

# ③ 執行（機要已解鎖且在空中）
curl -s -X POST $GS/api/v1/command/1/mission/start
```

---

## 4. 明確不提供的東西

* **操作層不對外**（解鎖／切模式／起飛的個別端點雖然存在，但不在「任務」
  這一組）。理由與 [019](../issues/019-agent-mcp-interface.md) 的定位一致：
  外部呼叫端表達的是**意圖**，不是步驟。
* **沒有認證**。這個服務目前**沒有任何身分驗證**——任何連得到 `:38001` 的人
  都可以指揮飛機。現況是私有網段，但這件事要寫在這裡，
  **不能靠「大家都知道」**。
* **MAVLink 簽章不做**（2026-09-02 裁定，見
  [`mavlink-signing-design.md`](mavlink-signing-design.md) 的重啟觸發條件）。
