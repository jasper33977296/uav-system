"""入列檢查：**驗證通過之前，不得指派任務、不得嘗試控制**（issues/040 A2）。

設計見 `doc/drone-admission-protocol.md`。使用者 2026-09-02 的需求兩句：
sysid 必須由系統指派、身分驗證階段完成前不可以指派任務或嘗試控制無人機。

## 為什麼問 backend 而不是自己判斷

入列要看三樣東西——**板號、代理連線、配號登錄**——全都在 backend 那一側。
command 只有 MAVLink router：它看得到號碼，看不到身分。**而號碼正是那個會撞、
會被冒用的東西**，拿它自己判斷等於用問題本身去回答問題。

## 問不到的時候怎麼辦：**用快取，沒有快取就擋**

這一條與 `guard_client` 的紀律**刻意不同**，值得說明白：

* `guard_client` 問不到時**放行**——它是「額外一層」，讓它的故障變成飛行操作的
  故障，是把可靠度往下拉。
* **入列不是額外一層，它是那個門。** 問不到就放行，等於「把 backend 弄掛」變成
  一條繞過入列的路——而那正是本案要關掉的東西。

折衷是快取：**已經答過的答案在 backend 掛掉時仍然可用**（所以 backend 重啟不會
讓飛行中的機失去指揮），**但從來沒答過的機不放行**。

**這樣做之所以可以接受，是因為它從不移除最後的退路**：被擋下的機仍然可以用
實體遙控器飛（issues/033 第 3 層）。沒有那一層的話，這道門就不該這樣設計。
"""
import json
import logging
import time
import urllib.error
import urllib.request

from .config import settings

log = logging.getLogger("command.admission")

#: 快取多久算新鮮。**短**——入列狀態會因為代理斷線而改變，而那正是要擋的情況
FRESH_S = 3.0
#: 過期的快取還能撐多久（backend 掛掉時）。**遠長於 backend 的重啟時間**：
#: 一次重啟不該讓飛行中的機失去指揮
STALE_OK_S = 900.0
#: 可以被指揮的狀態。**只有一個**——這是刻意的，多一個就要說得出為什麼
COMMANDABLE = {"admitted"}

_cache: dict[int, tuple[dict, float]] = {}


def _fetch(sysid: int) -> dict:
    req = urllib.request.Request(
        f"{settings.backend_api}/api/admission/{sysid}", method="GET")
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.loads(r.read().decode())


async def state_of(sysid: int) -> dict:
    """這台機現在的入列狀態。回傳 backend 的原話，外加 `stale` 旗標。"""
    import asyncio
    now = time.monotonic()
    hit = _cache.get(sysid)
    if hit and now - hit[1] < FRESH_S:
        return hit[0]
    loop = asyncio.get_running_loop()
    try:
        got = await loop.run_in_executor(None, _fetch, sysid)
        _cache[sysid] = (got, now)
        return got
    except Exception as e:
        if hit and now - hit[1] < STALE_OK_S:
            # **明說用的是舊答案**：呼叫端據此決定要不要在訊息裡講出來，
            # 而「這是幾秒前的答案」與「這是現在的答案」是不同的可信度
            log.warning("問不到入列狀態（%s），沿用 %.0f 秒前的答案：%s",
                        e, now - hit[1], hit[0].get("state"))
            return {**hit[0], "stale": True, "stale_age_s": now - hit[1]}
        log.error("問不到入列狀態且沒有快取（%s）——**擋下**：不知道這台機是不是"
                  "我們的，就不該指揮它", e)
        return {"sysid": sysid, "state": "unknown",
                "reason": f"問不到地面站的入列登錄（{e}）——"
                          "**不知道這台機是不是我們的，就不該指揮它**。"
                          "查 backend 是否存活；緊急時用實體遙控器"}


def why_blocked(info: dict) -> str:
    """把狀態翻成**說得出下一步**的一句話（ui-spec §0.2c）。

    **不可用的原因必須可行動**：「未驗證」不是原因，「這台機沒有代理」才是。
    """
    sysid, reason = info.get("sysid"), info.get("reason") or ""
    # **不要把 backend 的原話再貼一次**：那些模板已經把同一件事說過了，
    # 重複只會讓訊息變長而不是變清楚。只有 `identifying`／`quarantined`
    # 的原話帶著模板沒有的具體資訊（差在哪個號碼、矛盾在哪），才接上去
    return {
        "unmanaged": f"sysid {sysid} 沒有連線中的機上代理。**本系統只指揮有代理"
                     "的機**——請確認機上代理已啟動並連上地面站",
        "identifying": f"sysid {sysid} 的身分還沒確認完成：{reason}。"
                       "等代理回報板號與配號相符即可指揮",
        "quarantined": f"sysid {sysid} 的身分與記錄矛盾：{reason}。"
                       "**在查清楚之前不指揮它**——指令可能送到錯的飛機",
        "reassigning": f"sysid {sysid} 正在換號：{reason}。**這是我們自己發起的**"
                       "，不是故障——等它用新號碼回來即可",
        "seen": f"sysid {sysid} 上沒有任何遙測，不知道它是誰",
    }.get(info.get("state"), reason or f"sysid {sysid} 未通過入列檢查")
