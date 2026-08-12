#!/usr/bin/env python3
"""JSON 邊界清洗測試（PM 要求的系統性防線）。

**為什麼要這個測試**：裸 NaN 在 JSON 邊界會炸，而且是「本地看起來沒事、到邊界
才炸」的那種——我們踩過兩次（msg_registry 讓瀏覽器 JSON.parse 整包 throw；
param_sets 讓 PostgreSQL 拒收、快照從頭到尾沒寫進去過）。兩次都只修了當處。
第三次的候選現場還很多，所以收斂成一個 `jsonsafe` 模組＋這個測試釘住它。

**測到真實寫入路徑而不只是純函式**：純函式測試只證明函式自己對，證明不了
「呼叫端真的有用它」——第二次出事正是因為那時 msg_registry 已經修好、但 db.py
沒有用同一套。

跑法：docker exec -i -w /srv uav-backend python3 - < scripts/test-json-safe.py
"""
import asyncio
import json
import math
import sys

sys.path.insert(0, "/srv")

from app.jsonsafe import json_safe, dumps  # noqa: E402

NAN, INF, NINF = float("nan"), float("inf"), float("-inf")


def check_pure():
    fails = []

    # 1. 三種非有限浮點都要變 None
    for name, v in (("NaN", NAN), ("+Inf", INF), ("-Inf", NINF)):
        if json_safe(v) is not None:
            fails.append(f"{name} 沒被轉成 None（得到 {json_safe(v)!r}）")

    # 2. 正常值不可被動到（清洗不能順手改資料）
    for v in (0.0, -1.5, 42, "text", True, None, [1, 2], {"a": 1}):
        if json_safe(v) != v:
            fails.append(f"正常值被改動了：{v!r} → {json_safe(v)!r}")

    # 3. 巢狀結構要遞迴（真實 payload 都是巢狀的）
    nested = {"a": {"b": [1, NAN, {"c": INF}]}, "d": (NINF, 2)}
    out = json_safe(nested)
    if out != {"a": {"b": [1, None, {"c": None}]}, "d": [None, 2]}:
        fails.append(f"巢狀清洗不正確：{out!r}")

    # 4. dumps() 產出的必須是**合法 JSON**（用嚴格模式解回來驗證）
    try:
        json.loads(dumps({"x": NAN, "y": [INF]}), parse_constant=_reject)
    except Exception as e:
        fails.append(f"dumps() 產出的不是合法 JSON：{e}")

    # 5. 裸 dumps 的反面對照——證明這個坑真的存在（不是我們想像出來的）
    raw = json.dumps({"x": NAN})
    if "NaN" not in raw:
        fails.append("預期 json.dumps 會吐裸 NaN（對照組失效，測試前提要重新檢視）")

    return fails


def _reject(tok):
    raise ValueError(f"JSON 裡出現非法常數 {tok}")


async def check_db_path():
    """真實寫入路徑：含 NaN 的參數集要能寫進 JSONB 並讀回成 null。"""
    from app import db
    fails = []
    await db.init_pool()
    # 用可辨識的測試前綴，收尾時精準刪掉。
    # **不能用交易 ROLLBACK 包**：store_param_set 走的是連線池自己的連線，
    # 不會參與呼叫端的交易——第一版這樣寫，結果測試留下了殘留列（自己踩到）。
    params = {"__TEST_OK": 1.5, "__TEST_NAN": NAN, "__TEST_INF": INF}
    pid = None
    try:
        pid = await db.store_param_set(params)
        if not pid:
            fails.append("store_param_set 沒回 id")
        else:
            row = await db.pool.fetchrow(
                "SELECT params FROM param_sets WHERE id = $1::uuid", pid)
            got = row["params"]
            if isinstance(got, str):
                got = json.loads(got)
            if got.get("__TEST_NAN") is not None or got.get("__TEST_INF") is not None:
                fails.append(f"NaN/Inf 沒有存成 null：{got}")
            if got.get("__TEST_OK") != 1.5:
                fails.append(f"正常值走樣：{got.get('__TEST_OK')}")
    except Exception as e:
        fails.append(f"含 NaN 的參數集寫入失敗（這正是曾經的 bug）："
                     f"{type(e).__name__}: {e}")
    finally:
        if pid:                          # 精準清掉，不留測試殘留
            await db.pool.execute("DELETE FROM param_sets WHERE id = $1::uuid", pid)
    return fails


def main():
    fails = check_pure()
    try:
        fails += asyncio.run(check_db_path())
    except Exception as e:
        fails.append(f"DB 路徑測試無法執行：{type(e).__name__}: {e}")
    if fails:
        print("JSON 邊界測試 **失敗**：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("JSON 邊界測試 OK（純函式＋真實 JSONB 寫入路徑，含巢狀與合法性驗證）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
