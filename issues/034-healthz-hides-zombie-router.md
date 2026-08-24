# 034 · `/healthz` 不反映 router 死活：殭屍服務照回 ok

- 狀態：in-progress
- 嚴重度：high
- 位置：`apps/command/app/main.py:184`（healthz）、`apps/command/app/mav.py`（MavRouter）
- 建立：2026-08-24

## 現象

2026-08-11 現場：command 服務的 mav-router 執行緒被 5G 瞬斷殺死後，服務變成殭屍——
socket 沒人讀（14541 的 Recv-Q 塞到 213 KB 後靜靜丟包）、GCS 心跳停發、所有指令等到
逾時（UI 只顯示轉圈）。**而 `GET :38001/healthz` 全程回 200 `{"ok": true}`。**
沒有任何人或腳本看得出異常，拖了近一小時才發現。

## 原因

`healthz()` 回的是**寫死的字面值**：

```python
return {"ok": True, "enabled": settings.enable_commands,
        "gcs_sysid": mav.GCS_SYSID, "drones": router.snapshot()}
```

`ok: True` 只證明「FastAPI 還在服務 HTTP 請求」。但這個服務的職責是「指揮飛機」，
執行它的是另一條執行緒（`MavRouter`），HTTP 層對那條執行緒的死活一無所知。
`drones` 也不補救：路由表是**上次**收到心跳時留下的殘影，router 死後它靜止不動，
在 3 秒輪詢的畫面上與「一切正常」無從分辨。

**執行緒殺不死 ≠ 迴圈還在轉。** 2026-08-11 事故的直接修法（run() 主迴圈 catch-all、
`_sendto` 的 OSError 轉 CommandError）已經在 main 上，純例外確實不再殺得死它；
但這只堵住了其中一種死法，剩下的兩種仍然無聲：

1. `BaseException`（MemoryError／SystemExit）不被 catch-all 接住 → 執行緒真的死掉。
2. 執行緒活著但**卡住**：socket 進入不明狀態、job 內無限等待。對飛機的效果與死掉
   完全相同（心跳停發、指令不動），而 `is_alive()` 仍回 True。

## 影響

高。心跳一開始發，本服務就進入 PX4 的 datalink-loss 安全鏈（`COM_DL_LOSS_T` →
`NAV_DCL_ACT`）——router 無聲停擺等於**飛行中 GCS 心跳突然停止**，機體會依設定
自行 RTL／降落，而地面站畫面與健康檢查都顯示正常。033（意外狀況可用性分層防線）
的前提是「失效要能被看見」；這一條不修，那層設計等於建在假訊號上。

同一條原則的前端版本已寫進 `doc/ui-spec.md` §0.2e（失效不得冒充合法狀態）；
本案是它在服務端的對應面。

## 修法建議

`ok` 必須反映「還能不能指揮飛機」，而不是「HTTP 層還活著」：

- `MavRouter.alive()` 同時問兩件事——`is_alive()`（執行緒在不在）與 `_alive_t`
  時戳（迴圈最近轉過一圈沒有，門檻 `STALL_S = 5s`）。前者抓死亡、後者抓卡住。
- 時戳蓋在 `_tick()`：指令對話期間 `run()` 會停在 `_wait()` 裡數十秒，但 `_wait()`
  每圈都呼叫 `_tick()`（心跳不能斷），所以 `_tick` 才是「迴圈真的有在轉」的唯一
  共同點。蓋在 `run()` 迴圈頂端會讓每次任務上傳都誤判成卡住。
- router 不健康時 `/healthz` 回 **503**＋`detail` 說明。只看狀態碼的檢查
  （`curl -f`／docker healthcheck／外部監看）不會去讀 body，回 200 就是對它們謊報。

### 尚未做（本案不含，另行處理）

- **前端**：`CommandPanel` 目前只分「fetch 得到／fetch 不到」，不讀 `ok`。router
  殭屍時面板照常顯示可按的指令鈕——按下去必然逾時。依 §0.2e 應改為顯示明確的
  失效告示。這是 033 分層防線的一部分。
- **自動重啟**：偵測到之後要不要讓 router 自我重建（或讓 docker healthcheck 重啟
  容器）尚未決定。飛行中重啟會有一段心跳空窗，取捨需要先決定，不宜順手加。

## 解決方式

（closed 時補：實際改法 + commit hash）
