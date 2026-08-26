"""守門客戶端：執行飛行操作前，先問機上代理「現在能不能做這件事」。

**獨立成一個模組，是因為它有兩個呼叫端**：單機路徑（main.py）與群組執行器
（group_exec.py）。原本只寫在 main.py 裡，於是群組那條路直接呼叫底層 MAVLink、
完全繞過守門——**同一個危險操作，用單機介面會被擋、用群組介面直接執行**，
而多機的風險本來就更高（一次動好幾台）。2026-08-26 補上。
"""
import asyncio
import json
import logging
import urllib.request

from fastapi import HTTPException

from .config import settings

log = logging.getLogger("command.guard")

#: 由 main.py 在啟動時注入（router 與 pool 都在那裡建立）
router = None
pool = None


def bind(r, p):
    global router, pool
    router, pool = r, p


#: 指令服務的動作 → 意圖名（丙案：守門在代理，執行在這裡）
_INTENT_OF = {
    "mode:hold": "pause", "mode:mission": "resume",
    "mission_start": "start_mission", "mission_fly": "start_mission",
    "change_route": "change_route",
    # 群組執行器用的動作名（group_exec._submit_audited 的 action 字串）。
    # **名字不同就等於沒守到**：漏一個對映，那個動作會安靜地跳過守門，
    # 而「安靜地跳過」正是這道門最不該有的失效方式
    "upload": "start_mission",      # 群組上傳是起飛序列的第一步
    "takeoff": "start_mission",
    "arm": "start_mission",
    "mode:rtl": "rtl",
    # **空中上鎖＝馬達停轉、飛機直接掉下來。** 守門只在地上放行
    "disarm": "disarm",
}


async def ask_guard(sysid: int, action: str, intent_id: str | None = None,
                     params: dict | None = None, kind: str = "intent"):
    """執行前先問機上守門（協定 §5.2、丙案分工）。

    **為什麼問在這裡而不是前端**：守門只在前端問的話它就不是守門——前端可以
    不問，而且還有別的呼叫端（驗收 rig、MCP、curl）。要擋得住，就得擋在真的
    會動到飛機的那條路上。

    三種結果，**「不知道」歸到「不行」那一側**：
    * `cleared`／`no_agent` → 放行（沒有代理的機沿用原本的檢查）
    * `refused` → 擋下，理由原樣轉給操作員
    * `unknown`（逾時、斷線）→ **擋下**。不知道守門怎麼說，在飛安路徑上就是不行
    """
    intent = _INTENT_OF.get(action)
    if intent is None:
        return None                       # 這個動作不在守門範圍（如 arm/upload）
    uid = (router.drones.get(sysid) or {}).get("board_uid") if router else None
    body = json.dumps({"kind": kind, "action": intent,
                       "intent_id": intent_id,
                       "board_uid": uid, "params": params,
                       "drone_id": await drone_id_of(sysid)}).encode()
    loop = asyncio.get_running_loop()

    def _post():
        req = urllib.request.Request(
            f"{settings.backend_api}/api/agent/intent", data=body,
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode())
    try:
        res = await loop.run_in_executor(None, _post)
    except Exception as e:
        # 問不到守門服務本身（backend 掛了）。**不擋**——守門是額外一層，
        # 讓它的故障變成飛行操作的故障，是把可靠度往下拉而不是往上
        log.warning("問不到守門（%s），沿用本地檢查", e)
        return None
    v = res.get("verdict")
    if v in ("cleared", "no_agent", None):
        return res
    if v == "unknown":
        raise HTTPException(409, {
            "msg": f"問不到機上守門的判決（{res.get('reason')}）——"
                   "不知道不等於可以，這個操作先擋下",
            "code": "guard_unknown"})
    raise HTTPException(409, {
        "msg": f"機上守門擋下：{res.get('reason')}",
        "code": "guard_refused", "state": res.get("state")})


async def show_on_live(sysid: int, mission_id: str, why: str) -> None:
    """把這份航線推到即時畫面上，並在事件流留一筆。

    **為什麼上傳完就要顯示**：上傳的那一刻起，機上的航線就是這一份了——
    而即時頁上畫的如果還是別份（或什麼都沒有），畫面與飛機的事實就對不上。
    改航線確認後同理，而且那一次更重要：使用者剛剛才決定要換成這一條。

    失敗不擋指令：顯示是輔助，上傳已經成功的事不該因為畫面沒更新而回報失敗。
    """
    loop = asyncio.get_running_loop()

    def _post():
        q = urllib.parse.urlencode({"why": why, "sysid": sysid})
        req = urllib.request.Request(
            f"{settings.backend_api}/api/missions/{mission_id}/show?{q}",
            data=b"", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=4).read()
        except Exception as e:
            log.warning("即時畫面更新失敗：%s", e)
    try:
        await loop.run_in_executor(None, _post)
    except Exception:
        log.warning("即時畫面更新失敗（不影響指令）", exc_info=True)


async def drone_id_of(sysid: int) -> str | None:
    row = await pool.fetchrow(
        "SELECT id::text AS id FROM drones WHERE mav_sysid = $1", sysid)
    return row["id"] if row else None


