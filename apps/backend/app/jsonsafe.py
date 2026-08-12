"""JSON 邊界清洗：**裸 NaN／Infinity 不是合法 JSON**，跨邊界前一律轉 null。

為什麼需要一個共用模組而不是各處自己處理——因為同一個坑我們踩了兩次：

1. **msg_registry**（014 Phase B）：PX4 的 POSITION_TARGET_LOCAL_NED 等訊息帶
   NaN，`json.dumps` 原樣吐出裸 `NaN`，**瀏覽器 `JSON.parse` 整包 throw**，
   前端每則登錄表都解不開。
2. **param_sets**（021 Phase 2）：PX4 有參數的值就是 NaN，寫 JSONB 時
   PostgreSQL 直接拒收（`Token "NaN" is invalid`），**參數快照從頭到尾沒寫進去
   過一次**——而且「程式路徑都在」讓它看起來像完成了。

兩次都是「本地看起來沒事、到邊界才炸」，兩次的修法都只修了當處。第三次的候選
現場還很多（telemetry／link_metrics 的 `raw`、事件 detail、影像 metadata、
未來 MCP 的回傳）。**靠人記得就會有第三次，靠一處程式碼與一個測試才擋得住。**

用法：寫 JSONB 用 `dumps()`；要送出去的 dict 用 `json_safe()`。
測試見 `scripts/test-json-safe.py`。

註：`json.dumps(..., allow_nan=False)` **不是**解法——那只是把「產生非法 JSON」
換成「當場拋例外」，呼叫端一樣壞掉（廣播迴圈會整個停掉）。要的是轉成 null。
"""
import json
import math


def json_safe(v):
    """遞迴把 NaN／±Inf 轉成 None；bytes 轉字串。其餘原樣。

    dict 的 key 不動（JSON key 一定是字串，不會有 NaN 問題）。
    """
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).split(b"\x00", 1)[0].decode("utf-8", "replace")
        except Exception:
            return list(v)
    return v


def dumps(v, **kw) -> str:
    """寫 JSONB／送 HTTP 前用這個，不要直接 json.dumps。"""
    return json.dumps(json_safe(v), **kw)
