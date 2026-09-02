# 任務 API：選任務 → 上傳 → 執行

> 給**外部整合**用的最小介面（2026-09-02 使用者定案：MCP 先不做，
> 提供這三個就好）。機器可讀的規格見 [`openapi.json`](openapi.json)，
> 匯出方式：`python3 scripts/export-openapi.py`。

---

## 0. 三個端點

全部在 **command 服務（`:38001`）**。

| 步 | 端點 | 說明 |
|---|---|---|
| ① 選任務 | `GET /api/missions` | 任務庫總表。**唯讀、不吃 `ENABLE_COMMANDS`**——只是看有哪些航線，不動飛機 |
| ② 上傳 | `POST /api/command/{sysid}/mission/upload` <br>`{"mission_id": "..."}` | 寫進飛控，**並逐項讀回比對** |
| ③ 執行 | `POST /api/command/{sysid}/mission/start` | 讓飛控開始執行**它機上現有**的那份任務 |

**想一次做完**：`POST /api/start`（`{"mission": "<id 或名稱>"}`）——
上傳→解鎖→起飛→切任務，每步讀回確認。
自動化流程用它；互動操作建議走三步，**因為中途出錯時看得出停在哪一步**。

---

## 1. 兩件呼叫前一定要知道的事

### 1.1 ③ 不會讓一台停在地上的機起飛，而 ② 在空中會立即改道

* **`mission/start` 要求機已經解鎖並在空中。** 對停在地面的機切自動任務模式，
  等於叫它自己起飛——那是 [issues/031](../issues/031-arm-guard-auto-mode.md)
  記的那次事故（2026-08-13，SITL 上真的飛起來了）。要從地面一路到飛，
  用 `/api/start` 或 `mission/fly`。
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
GET http://<地面站>:38000/api/admission/<sysid>
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
curl -s $GS/api/missions | jq '.missions[] | {id, name, nav_count}'

# 先確認這台機可以被指揮（省掉一次注定失敗的呼叫）
curl -s $BE/api/admission/1 | jq .state       # 要是 "admitted"

# ② 上傳
curl -s -X POST $GS/api/command/1/mission/upload \
     -H 'Content-Type: application/json' \
     -d '{"mission_id":"6f812621-..."}'

# ③ 執行（機要已解鎖且在空中）
curl -s -X POST $GS/api/command/1/mission/start
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
