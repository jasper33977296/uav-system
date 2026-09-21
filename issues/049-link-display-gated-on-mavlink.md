# 049 · 訊號面板被 MAVLink 綁架：飛控不在，5G 訊號就整塊消失

- 狀態：open
- 嚴重度：medium（顯示正確性；但發作時機正好是「最需要看訊號」的當下）
- 位置：`apps/backend/app/main.py:207`（廣播閘）、`mavlink_rx.py:406`（唯一寫入點）、
  `apps/frontend/lib/useTelemetry.ts:40`、`apps/frontend/components/SidePanel.tsx:381`
- 建立：2026-09-16

## 現象

2026-09-16 上午，飛控串列斷掉之後，即時頁**連 5G 訊號也一起空白**——
SINR／RSRP／PCI／RTT 全變「—」，不是變舊，是整塊不見。

而同一時間後端其實握著新鮮的訊號：直接打 HTTP

```
$ curl -s http://localhost:38000/api/live | jq .link
{ "time": "2026-09-16T03:41:42Z", "pci": 133, "sinr": 30.0, "rsrp": -60.0, "rtt_ms": 49.5, ... }
```

機上也回報送得好好的：`modem: {samples: 7357, posted: 7355, errors: 2, hz: 1.0}`。
**資料一路都在，只是沒被廣播出去。**

### 重現

1. 讓機上 agent 正常跑（訊號採樣 1Hz 在送 `/api/link-metrics/live`）。
2. 讓飛控不要講話（拔線、關機，或本次的成因：termios 被改掉）。
3. **在飛控已經啞掉的狀態下重啟 backend 容器。**
4. 即時頁：訊號面板全空。HTTP `/api/live` 卻有完整且持續更新的 `link`。

第 3 步是必要條件——backend 若沒重啟，`ever_connected` 還是 True，訊號會照顯示。
本次是湊巧：飛控 11:19:30 啞掉，backend 11:24:13 重啟（agent 日誌有對應的
`link-metrics 送出失敗（Connection refused）`），中間 21 分鐘畫面全空，
直到 11:45:44 重啟 agent 把 baud 復原、MAVLink 回來才恢復。

## 原因

訊號與遙測是**兩條完全獨立的路**，卻共用同一道閘。

訊號走 agent 直接 HTTP POST `/api/link-metrics/live`（`api.py:3274`），與 MAVLink 無關，
更新 `live.link` 並呼叫 `mark_link_seen()`。

但前端拿不到 `live.link` 的獨立管道——它是**搭遙測的便車**出去的：

```
useTelemetry.ts:40   if (msg.type === "telemetry") setLive(msg);
SidePanel.tsx:381    const link = live?.link;
```

`telemetry_dict()` 裡包著 `link`，兩者同一顆封包。而廣播迴圈上有一道閘：

```python
# main.py:207
if not st.ever_connected:
    continue
```

`ever_connected` 全庫只有一個地方會設 True：

```python
# mavlink_rx.py:406
st.ever_connected = True     # 收到 MAVLink 才算
```

訊號樣本只呼叫 `mark_link_seen()`（`state.py:195`），那個函式只動 `link_seen_mono`，
**碰不到 `ever_connected`**。於是：backend 重啟 → `ever_connected` 歸零 →
飛控已啞、沒有 MAVLink 把它翻回 True → 整台機被 `continue` 跳過 →
前端一筆 `telemetry` 都收不到 → `live` 是 null → `live?.link` 是 undefined → 面板空白。

### 這道閘是 036 的修法留下的

閘旁邊的註解說得很清楚，而且擋 `ever_connected` 而非 `connected` 是刻意的：

> **從未產生過遙測的機不廣播**——沒有遙測可以報。（…）
> 注意**不是擋 `connected`**：斷線但曾連上的機要繼續送最後已知位置（使用者定案），
> 前端以紅框閃爍標示斷線（issues/036）

設計是對的，但它假設「曾連上」這件事記得住。`ever_connected` 是 process 內的記憶體狀態，
容器一重啟就忘了——而這時候唯一還在講話的那條路（訊號），恰好沒有資格開這道閘。

**這是 036 的鏡像**：036 是把「沒有資料」畫成「有資料」，這一條是把「有資料」畫成「沒有資料」。

## 影響

- 5G 訊號是本專案的**主要研究對象**，而它消失的時機正好是鏈路／飛控出狀況的當下——
  最需要看訊號品質來判斷「是不是 5G 的問題」的那一刻，畫面什麼都不給。
- 誤導性排查：畫面空白會讓人以為機上 agent 死了或訊號採樣停了，
  實際上兩者都好好的（本次就先往這個方向查過）。
- **資料沒有遺失**：入庫走的是記錄通道 `/link-metrics/batch`，與這道閘無關；
  live 通道本來就不寫 DB（`api.py:3274` docstring）。純粹是呈現層問題。

## 修法建議

> **機上側是 [050](050-agent-fc-link-watchdog.md)**：這一條解「後端手上有訊號卻不廣播」，
> 050 解「飛控斷了代理不出聲」。兩條在同一個畫面上會合，修法要對接——
> 050 打算讓代理走 `/ws/agent` 回報飛控失聯與最後已知快照，
> 本條的選項 1／2 要能讓那份回報把機留在即時頁上。

### 選項 1（建議）：閘改成「有遙測**或**有鏈路樣本」

```python
# main.py:207
if not st.ever_connected and st.link_age_s is None:
    continue
```

訊號自己就能讓這台機上線，不必等 MAVLink。**不會退回 036 的原始問題**
（沒連上的機從後端啟動那一刻就佔著即時頁），因為仍要求至少收過一筆鏈路樣本，
而鏈路樣本代表機上 agent 真的在講話。

要一起看的地方——`SimpleHud.tsx:113` 的假設會被打破：

> `// fleet 裡的機都是**收過遙測**才進來的（見 store.setLive），所以不必再濾`

改完之後 fleet 裡可能出現「只有訊號、沒有位置沒有姿態」的機。要確認 HUD、MapView
在這種機上不會炸，而且**要照 036 的標準顯示成「沒有遙測」而不是畫成 0**。

### 選項 2：訊號另開一條廣播（`type: "link"`）

把訊號與遙測徹底解耦，前端各自訂閱。架構上比較乾淨，也順手解掉
「兩條獨立的路共用一道閘」這個根本毛病，但前後端都要動，範圍大一些。

### 選項 3：不修

接受「飛控斷 + backend 在斷線期間重啟」時訊號會空白。
**重啟觸發條件**：若之後要做長時間的純訊號量測（無人機不飛、只跑 agent 採樣），
這個組合會變成常態而非巧合，屆時必須修。

## 解決方式

（closed 時補）
