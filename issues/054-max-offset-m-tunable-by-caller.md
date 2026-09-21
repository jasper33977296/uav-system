# 054 · `max_offset_m` 被外部調得動

- 狀態：closed
- 嚴重度：low
- 位置：`apps/backend/app/ext_history.py:129`
- 建立：2026-09-21

## 現象

`GET /api/v1/ext/missions/{id}/signal?max_offset_m=0.01` 是合法呼叫，
而且會讓那一趟**每一筆** `along_m` 都變成 `null`。

兩份對外文件都沒有寫這個參數：文件把 `max_offset_m` 呈現成回應裡
`method` 區塊的一個說明值，讀起來像固定常數。

## 原因

它不是決定出來的，是寫法的副作用——FastAPI 把非路徑參數自動變成 query：

```python
async def mission_signal(mission_id: str,
                         max_offset_m: float = chainage.DEFAULT_MAX_OFFSET_M):
```

`scripts/test-ext-history.py` 用它把門檻縮到 0.01 驗「偏離超過上限時里程給 null」，
是利用這個副作用。

## 影響

* 同一趟資料在不同呼叫下給出不同的 `along_m`，兩邊各自快取就對不上，
  而回應裡只有 `method.max_offset_m` 一行說得出差別。
* 外部沒有理由比我方更懂這個門檻該設多少——它是「偏離多遠就不該再談里程」的
  判斷，屬於方法，不屬於查詢。

## 修法建議

**拿掉 query 參數，鎖成常數 `chainage.DEFAULT_MAX_OFFSET_M`（60 m）。**
回應照樣附 `method.max_offset_m`，讓控制端知道用的是哪個門檻。

測試改從內部呼叫 `chainage.projector(ref, 0.01)` 驗門檻，
不要為了測試而在對外介面上留一個旋鈕。

**反向的理由**（要留就要寫進文件）：不同場域的航線精度差很多，
空曠地 60 m 可能太鬆。真有這個案例時再開回來，而那時要先量過。

## 解決方式

`mission_signal()` 不再收 `max_offset_m`，一律用 `chainage.DEFAULT_MAX_OFFSET_M`；
用了哪個值照樣寫在 `method.max_offset_m` 裡。測試改成驗「帶了也不生效」，
門檻本身改從內部呼叫 `chainage.projector()` 驗。

2026-09-21，commit `0d404fb`。
