"""command 服務 API：自製 GCS 的指令能力（GCS 取代計畫階段 2）。

與 backend（ingest，唯讀）完全分離的獨立服務：自己的 MAVLink 連線
（單埠多機）、指令佇列＋ACK、任務上傳＋回讀比對、指令留痕入庫。
`ENABLE_COMMANDS` 預設關——關閉時指令端點回 403、不發 GCS 心跳。

端點（sysid 定址；多機模型見 issues/011）：
  GET  /healthz                              服務與各機連線狀態
  POST /api/command/{sysid}/arm | /disarm
  POST /api/command/{sysid}/mode/{mission|hold|rtl|land}
  POST /api/command/{sysid}/mission/start
  POST /api/command/{sysid}/mission/upload   body: {"plan_id": "..."}
"""
import asyncio
import contextvars
import concurrent.futures
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
import uuid

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# 每請求的 client 來源（X-Client header）——留痕歸因用（issue 013-B：驗收 rig 帶
# X-Client: acceptance-rig，指令來源一眼可辨、查案不用反推）。contextvar 讓 _audit
# 不必改每個端點簽名就取得；背景序列（execute 起的 task）沿用觸發請求的 client。
_client_var: contextvars.ContextVar = contextvars.ContextVar("client", default=None)

from . import capabilities as caps
import plan_check          # libs/ 的共用實作（PYTHONPATH=/srv/libs）
import terrain             # 地形圖磚（issues/047 §1-B）

from . import admission, group_exec, guard_client, mav, params as fcparams, plans
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("command")

router: mav.MavRouter | None = None
pool: asyncpg.Pool | None = None
executor: "group_exec.GroupExecutor | None" = None

# waypoints.action → MAV_CMD（舊資料沒存原始 command 時的推回）
ACTION_CMD = {"takeoff": 22, "waypoint": 16, "land": 21, "rtl": 20}


def default_frame(cmd: int) -> int:
    """該指令在沒有明給 frame 時該用哪個 frame。

    **無座標的任務項用 `MAV_FRAME_MISSION`(2)，不是 GLOBAL_RELATIVE_ALT(3)。**
    RTL(20)、CONDITION_*(112–159)、DO_*(176+) 都不帶經緯高，MAVLink 慣例是
    frame 2；QGC 產的 .plan 也是這樣寫。

    **為什麼要有這個函式**（2026-08-12 實測）：原本一律預設 3，於是**任何含
    RTL 的任務都上不去**——機端回 `MAV_MISSION_UNSUPPORTED` 把**整包**拒收。
    這條路徑連我方自己的 `action:"rtl"` 都會踩到（不是使用者給錯值的問題），
    等於 plan_check 那句「最後一個導航項不是返航/降落」的建議**根本做不到**。
    實測 sysid 3（PX4 SITL）：RTL 配 frame 0/3/5/6 全被拒，只有 frame 2 過。
    """
    return 2 if (cmd == 20 or cmd >= 112) else 3


def build_items(wps: list[dict]) -> list[dict]:
    """MAVLink 保真度（對齊實戰工具 upload_mission.py）：新資料帶 .plan 的
    原始 command/frame/p1–p4，原樣送出；舊資料由 action 推回、frame 依指令推
    （見 `default_frame`——**不是一律 3**）、params 補 0。"""
    items = []
    for i, w in enumerate(wps):
        p = w.get("params")
        p = json.loads(p) if isinstance(p, str) else (p or {})
        cmd = int(p.get("command") or ACTION_CMD.get((w.get("action") or "waypoint"), 16))
        # 明給的 frame 一律照送（MAVLink 保真度）；沒給才依指令推。**不覆寫**
        # 使用者/`.plan` 的顯式值——那是保真度的核心，寧可讓機端拒絕得明明白白，
        # 也不要我方偷改成「看起來對」的值（顯式值不合理由 plan_check 事前示警）。
        f = p.get("frame")
        items.append({
            "seq": i,
            "frame": default_frame(cmd) if f is None else int(f),
            "command": int(cmd),
            "p1": float(p.get("p1") or 0.0), "p2": float(p.get("p2") or 0.0),
            "p3": float(p.get("p3") or 0.0), "p4": float(p.get("p4") or 0.0),
            "x": int(round((w["lat"] or 0.0) * 1e7)),
            "y": int(round((w["lon"] or 0.0) * 1e7)),
            "z": float(w["alt"] or 0.0),
        })
    return items


async def _link_of(sysid: int):
    """sysid → (drone_id, 進行中的 session_id)。查不到就 (None, None)。

    **一次查詢解兩件事**：指令是熱路徑，每筆多兩次 round-trip 不划算。

    為什麼要在寫入當下解而不是事後推：只靠 sysid＋時間戳回推「這筆指令
    是哪台機、哪一趟」，得假設 sysid 從那時到現在沒被重新配過號——**而
    sysid 正是會被重新配號的那個東西**（issues/040）。當下解出來的是事實，
    事後推出來的是推論。
    """
    row = await pool.fetchrow(
        """SELECT d.id::text AS drone_id,
                  (SELECT s.id::text FROM flight_sessions s
                    WHERE s.drone_id = d.id AND s.ended_at IS NULL
                    ORDER BY s.started_at DESC LIMIT 1) AS session_id
             FROM drones d WHERE d.mav_sysid = $1""", sysid)
    return (row["drone_id"], row["session_id"]) if row else (None, None)


async def _audit(sysid: int, action: str, params, result: str, detail: str = ""):
    # 2026-09-06：原本只寫 sysid，於是「這趟飛行下了什麼指令」在系統裡
    # 連不起來——307 筆歷史紀錄裡 drone_id 填了 0 筆（欄位 9/2 就加了，
    # 但沒有任何寫入端在填）。解不出來就留 NULL：**空著代表「不知道」**。
    try:
        did, sid = await _link_of(sysid)
    except Exception:
        log.warning("指令留痕解不出機／架次（不影響指令）", exc_info=True)
        did = sid = None
    await pool.execute(
        "INSERT INTO command_log "
        "(sysid, action, params, result, detail, client, drone_id, session_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        sysid, action, json.dumps(params, default=str), result, detail[:500],
        _client_var.get(), did, sid)


async def _refused(sysid: int, action: str, gate: str, reason: str, extra=None):
    """**我們自己擋下的**要留痕，然後才丟出去。

    2026-09-02 使用者問「為什麼系統一直不讓我操作」，而我答不出來——
    因為三道門（入列 403／能力 501／機上守門 409）**在寫紀錄之前就 raise 了**，
    一次都沒有進 `command_log`。

    同一天的八小時裡：**飛控擋了 1395 次，每一次都有紀錄；我們系統擋了 N 次，
    一次都沒有。** 那正好是反的——自己家的門不記帳，別人家的門記得清清楚楚。

    `result='refused'` 與既有三種分得開：
      * `failed`   飛控收到了但拒絕（帶 MAV_RESULT）
      * `error`    例外／逾時（送不到）
      * `rejected` 送到了、做了，但讀回比對不過
      * `refused`  **我們沒有送出去**——被自己的門擋下
    """
    try:
        await _audit(sysid, action, {"gate": gate, **(extra or {})},
                     "refused", f"{gate}：{reason}")
    except Exception:
        log.exception("擋下的留痕寫入失敗（不影響擋下本身）")


def _require_enabled():
    if not settings.enable_commands:
        raise HTTPException(403, "指令能力未啟用（ENABLE_COMMANDS=false，預設關閉）"
                                 "——這是刻意的安全 gate，部署時顯式開啟")


# 能力 gating（issue 015）：capabilities 是伺服器端唯一真相，非 "ok" 的能力
# 一律拒發——UI 與實際放行永不背離。取代舊 _require_px4 硬碼（ardupilot 現在走
# unverified＝全鎖，比舊版嚴、符合四態「僅觀察」）。可攜指令在某機型 SITL 驗過
# 後把該鍵開 "ok"，前後端同時放行。
async def _require_capability(sysid: int, endpoint_key: str):
    if sysid not in router.drones:
        # **第四道門**（2026-09-06）。9/2 補留痕時盤點出三道（入列 403／
        # 能力 501／機上守門 409），漏了這一道——它排在最前面，所以
        # 「機不在線」這個最常見的擋法反而是唯一一個不留痕的。
        # 症狀正是使用者當時抱怨的：擋了，但事後查不到擋過。
        why = f"sysid {sysid} 未連線（心跳未見）"
        await _refused(sysid, endpoint_key, "連線", why)
        raise HTTPException(409, why)
    # **入列檢查排在能力檢查之前**（issues/040 A2）：「這台機是不是我們的」
    # 比「這台機做不做得到」更根本——對一台身分不明的機談能力沒有意義。
    info = await admission.state_of(sysid)
    st_ = info.get("state")
    # **通道斷了但身分還在**：只放行把飛機帶回地面的動作（2026-09-04 裁定）。
    # 那兩個動作在任何飛行狀態下的意思都一樣，所以問不到機上守門也不影響
    # 判斷；其餘的都需要「當下狀態允不允許」，而那正是問不到的東西。
    offline_ok = (st_ in admission.OFFLINE_COMMANDABLE
                  and endpoint_key in admission.OFFLINE_ACTIONS)
    if st_ not in admission.COMMANDABLE and not offline_ok:
        why = admission.why_blocked(info)
        if st_ in admission.OFFLINE_COMMANDABLE:
            why = ("機上代理的意圖通道斷了——問不到機上守門，"
                   f"所以只放行 {sorted(admission.OFFLINE_ACTIONS)}。"
                   "**身分沒有問題**（板號與配號都對得上），"
                   "要恢復完整指揮先讓通道連回來")
        await _refused(sysid, endpoint_key, "入列", why,
                       {"admission": st_, "stale": info.get("stale", False)})
        raise HTTPException(403, {
            "msg": why,
            "code": "not_admitted", "admission": st_,
            "sysid": sysid, "drone": info.get("drone"),
            # 用的是舊答案時要說——「幾秒前的答案」與「現在的答案」
            # 是不同的可信度
            "stale": info.get("stale", False),
            "hint": "本系統只指揮通過入列的機。緊急時實體遙控器不受影響"})
    ap = mav.caps.autopilot_name(router.autopilot_of(sysid))
    cap_key = mav.caps.ENDPOINT_CAP.get(endpoint_key, endpoint_key)
    cap, reasons = mav.caps.capabilities_for(ap, (router.drones.get(sysid) or {}))
    state = cap.get(cap_key, "unsupported")
    if state != "ok":
        await _refused(sysid, endpoint_key, "能力",
                       f"{cap_key} 目前是 {state}：{reasons.get(cap_key, '')}",
                       {"autopilot": ap, "capability": cap_key, "state": state})
        raise HTTPException(501, {
            "msg": f"{cap_key} 目前不可用（{state}）",
            "hint": reasons.get(cap_key, ""),   # 前端 msg＋hint 解析直接顯示
            "autopilot": ap, "capability": cap_key, "state": state,
            "reason": reasons.get(cap_key, "")})


async def _run(sysid: int, action: str, fn, *args, params=None):
    """執行 MAV 工作＋留痕。失敗一樣留痕——指令史是實驗記錄的一部分。"""
    _require_enabled()
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, router.submit, fn, sysid, *args)
    except mav.CommandError as e:
        await _audit(sysid, action, params, "failed", str(e))
        raise HTTPException(502, str(e))
    except (concurrent.futures.TimeoutError, TimeoutError) as e:
        # **逾時不是「內部錯誤」，它是一個說得出來的故障**：指令排進去了，
        # 但那條工作在 30 秒內沒有做完——在這條鏈路上幾乎一定是
        # 「送到飛機的方向不通」（遙測還是會照樣回來，因為那是反方向）。
        #
        # 而它原本被 `f"內部錯誤：{e}"` 吞掉，**連「逾時」兩個字都沒有**：
        # `str(TimeoutError())` 是**空字串**，所以操作員看到的literally 是
        # 「內部錯誤：」後面什麼都沒有（2026-09-02 現場實測）。
        await _audit(sysid, action, params, "error", "TimeoutError")
        raise HTTPException(504, {
            "code": "link_timeout",
            "msg": f"{action} 送出去了，但 {mav.JOB_TIMEOUT_S:.0f} 秒內沒有回應",
            "hint": "指令到飛機的方向不通。**遙測照樣回得來不代表指令送得過去**"
                    "——那是兩個方向。先看代理的 rx_from_gs 有沒有在增加，"
                    "以及地面站的 5G 介面有沒有重新列舉",
            "how_to": ["確認地面站的 5G 介面沒有換名字（ip -br addr）",
                       "確認 uav-heartbeat 發得到機上（docker compose logs uav-heartbeat）",
                       "機上 journalctl -u uav-agent 看 rx_from_gs 是否停住"]})
    except Exception as e:
        # **`str(e)` 可能是空的**（TimeoutError 就是），所以一律帶上型別名，
        # 不然操作員看到的是一句沒有內容的「內部錯誤：」
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        await _audit(sysid, action, params, "error", repr(e))
        raise HTTPException(500, {"code": "internal", "msg": f"內部錯誤（{detail}）",
                                  "hint": "這一則會進 command_log，"
                                          "細節欄有完整的例外"})
    accepted = res.get("accepted", True)
    ok = accepted and res.get("verified", True)
    await _audit(sysid, action, params, "accepted" if ok else "rejected", json.dumps(res))
    if not ok:
        # **「被拒絕」與「做了但沒能確認」不是同一件事**（2026-09-07）。
        # 兩者原本共用一句 `機端拒絕（{result}）`，於是清除任務讀不回筆數時
        # （mav.job_clear_mission 回 accepted=True／verified=False，並在 `note`
        # 裡寫清楚為什麼）操作員看到的是 **「機端拒絕（None）」**——一句
        # 說錯了事實、又沒有理由的話。那個 None 正是「這裡根本沒有 result」。
        #
        # 兩者都維持 409（未確認一律當成沒成功，安全方向），但話要各自說對。
        if not accepted:
            # 結構化拒絕：result code＋操作指引＋PX4 的解釋文字（實戰教訓：
            # 只給 code 操作員無從排查——"Arming denied: ..." 那行才是答案）
            raise HTTPException(409, {
                "msg": f"機端拒絕（{res.get('result')}）",
                "hint": res.get("hint", ""),
                "autopilot_notes": res.get("autopilot_notes", []),
            })
        raise HTTPException(409, {
            "code": "unverified",
            "msg": f"{action} 送出去了，但沒能讀回確認",
            "hint": res.get("note") or res.get("hint", "")
                    or "機端沒有回應讀回查詢——請自行確認機上的狀態",
            "autopilot_notes": res.get("autopilot_notes", []),
        })
    return res


async def lifespan(app):
    global router, pool, executor
    # **兩處各寫一份 GCS sysid 就是會漂移**，而漂移的症狀是能力判定拿著一個
    # 過期的數字去比對機端參數，然後把一台其實可以指揮的機標成「不行」——
    # 或反過來。開機就比對，不同就不要啟動
    if mav.GCS_SYSID != caps.GCS_SYSID:
        raise RuntimeError(
            f"GCS sysid 兩處不一致：mav.py={mav.GCS_SYSID}、"
            f"capabilities.py={caps.GCS_SYSID}——改一個就要改另一個")
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    await pool.execute("""CREATE TABLE IF NOT EXISTS command_log (
        id BIGSERIAL PRIMARY KEY, time TIMESTAMPTZ NOT NULL DEFAULT now(),
        sysid INT, action TEXT NOT NULL, params JSONB,
        result TEXT NOT NULL, detail TEXT)""")
    # 指令來源歸因（issue 013-B）：X-Client header 落痕，既有表補欄
    await pool.execute("ALTER TABLE command_log ADD COLUMN IF NOT EXISTS client TEXT")
    # 單埠多機的身分對應欄位（issues/011；backend migrate 也建，這裡防序）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS mav_sysid INT")
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS current_plan_id UUID")
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS plan_cleared_at TIMESTAMPTZ")
    # 群組執行期即時態欄位（issue 013-B；backend migrate 也建，這裡防序）
    await pool.execute("ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS error JSONB")
    await pool.execute(
        "ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
    router = mav.MavRouter(settings.command_mavlink_url,
                           heartbeat=settings.enable_commands)
    router.start()
    # 守門客戶端要拿到同一個 router 與 pool（它有兩個呼叫端：本檔與 group_exec）
    guard_client.bind(router, pool)
    executor = group_exec.GroupExecutor(router, pool, build_items, _audit)
    log.info("command 服務啟動：%s（enable_commands=%s，sysid=%d）",
             settings.command_mavlink_url, settings.enable_commands, mav.GCS_SYSID)
    yield
    await pool.close()


#: OpenAPI 的分組。**只有三個標籤**是給外部呼叫端看的（`任務`），其餘是
#: 內部操作面——分開是為了讓「外面該用哪些」一眼看得出來，而不是把 20 個
#: 端點倒給對方自己挑
TAGS = [
    {"name": "任務", "description":
        "**外部呼叫端只需要這三個**：選任務 → 上傳到無人機 → 開始執行。\n\n"
        "每一步都會先過三道門，被擋下時回的是 4xx 與**說得出下一步的理由**：\n"
        "1. **入列**（403 `not_admitted`）——這台機是不是我們的。"
        "沒有機上代理的機**看得到但指不動**。\n"
        "2. **能力**（501）——這個廠牌的這個動作驗過了沒。\n"
        "3. **機上守門**（409 `guard_refused`）——當下這個狀態允不允許。"
        "理由一定說得出「那現在能做什麼」。"},
    {"name": "一鍵", "description":
        "把三步併成一次呼叫（上傳→解鎖→起飛→切任務），每步讀回確認。"
        "**適合自動化流程**；互動操作建議走三步，因為中途出錯時看得出停在哪一步。"},
    {"name": "操作", "description": "解鎖／模式／起飛等操作層動作。"},
    {"name": "群組", "description": "編隊執行。"},
    {"name": "健康", "description": "服務與各機連線狀態。"},
]

app = FastAPI(
    title="UAV Command Service",
    description=(
        "無人機指令服務。**外部整合請看「任務」那一組的三個端點。**\n\n"
        "> 匯出 OpenAPI：`python3 scripts/export-openapi.py`"),
    openapi_tags=TAGS, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _capture_client(request, call_next):
    """把 X-Client header 塞進 contextvar，供 _audit 歸因（背景 task 沿用此 context）。"""
    _client_var.set(request.headers.get("x-client"))
    return await call_next(request)


@app.get("/healthz", tags=["健康"])
async def healthz():
    """服務與各機連線狀態。**`ok` 講的是「還能不能指揮飛機」**，不是
    「HTTP 層還活著」——issue 034：2026-08-11 router 執行緒被網路瞬斷殺死後，
    心跳停發、指令全逾時，而這裡照回 `{"ok": true}`，於是沒有任何人與腳本
    看得出異常，拖了近一小時才發現。失效不得冒充合法狀態（ui-spec §0.2e）。

    **2026-08-31 語意變窄了**：GCS 心跳已搬到獨立行程（issues/033 §4.2.1），
    所以 router 卡住**不再等於心跳停發**。`ok` 仍然是 false（指令送不出去仍然
    是「指不動飛機」），但下面的 detail 不能再說「GCS 心跳已停發」——
    那句話現在是假的，而**一句假的診斷會把人帶去查錯的地方**。
    """
    alive = router is not None and router.alive()
    body = {"ok": alive, "router_alive": alive,
            "enabled": settings.enable_commands, "gcs_sysid": mav.GCS_SYSID,
            "drones": router.snapshot() if router is not None else {}}
    if not alive:
        # 連狀態碼都要說實話：只看狀態碼的檢查（curl -f／docker healthcheck／
        # 外部監看）不會去讀 body，回 200 就是對它們謊報健康。
        body["detail"] = (
            "MAVLink router 迴圈未在運轉（執行緒死亡，或卡住超過 "
            f"{mav.STALL_S:.0f} 秒）：指令不會送達飛機，遙測也不再更新。"
            "**GCS 心跳不受影響**（已獨立為 uav-heartbeat，issues/033 §4.2.1）"
            "——所以飛控不會因為這件事觸發 GCS failsafe，但它也代表"
            "「飛控看起來一切正常」而我們其實指不動它。"
            "查 command 服務日誌後重啟服務。")
        return JSONResponse(body, status_code=503)
    return body


#: 「解鎖後機體會自己動」的模式動詞。在這些模式下裸 arm ＝ 立即自主飛行。
#: 用**動詞**不是模式名——PX4 叫 MISSION、ArduPilot 叫 AUTO，比字串會漏
#: （030 的教訓）。`guided` 不在此列：那是「解鎖後等指令」的正常起飛前置。
_AUTO_EXEC_VERBS = {"mission", "rtl", "land"}


def _guard_bare_arm(sysid: int) -> None:
    """裸 arm 前的模式檢查（issue 031）。

    **為什麼系統要擋而不是靠紀律**：2026-08-13 有人對一台停在 MISSION 模式、
    機上載有任務的機下裸 arm，它立即自主起飛爬到 50m——真機上就是 fly-away。
    那正是 issue 028 危害描述的人工重演（「把一台還在地面的機切進 AUTO.MISSION」）：
    028 修掉了程式會犯的版本，這次證明人也會犯。

    而 MCP 之後 **agent 也會下 arm**——agent 不會累，但也不會「想到先看模式」。
    系統**已經知道**當前模式（HEARTBEAT 一直在報），知情不阻攔說不過去。

    只擋這個 HTTP 端點：內部序列（`_do_takeoff`、群組執行器）直接呼叫
    `job_command`，那些 arm 是**有意圖的**（起飛序列的一步），不該被擋。
    """
    d = (router.drones.get(sysid) or {}) if router else {}
    cm = d.get("custom_mode")
    if cm is None:
        return                      # 還沒收到心跳＝不知道模式，不亂擋（能力 gating 另有把關）
    drv = mav.dialect(router, sysid)["driver"]
    verb = drv.decode_verb(cm)
    if verb not in _AUTO_EXEC_VERBS:
        return
    mode_name = drv.decode_mode(cm)
    raise HTTPException(409, {
        "msg": f"這台機目前在 {mode_name} 模式，解鎖後會**立即開始自主飛行**",
        "hint": "若只是要解鎖（例如測試或地面檢查）：先切 hold 再 arm。"
                "若本來就要讓它飛任務：走 mission/fly 序列"
                "（它會上傳→解鎖→起飛→到高度才切任務，每步都驗過），"
                "不要用裸 arm。",
        "flight_mode": mode_name, "mode_verb": verb, "sysid": sysid,
        "override": "確定要在此模式下解鎖，帶 intent=start_mission 再送一次",
    })


@app.post("/api/command/{sysid}/arm", tags=["操作"])
async def arm(sysid: int, intent: str | None = None):
    _require_enabled(); await _require_capability(sysid, "arm")
    if intent != "start_mission":
        _guard_bare_arm(sysid)
    else:
        # 顯式意圖仍然留痕——**繞過安全檢查這件事本身要看得見**
        await _audit(sysid, "arm", {"intent": intent}, "accepted",
                     "帶 intent=start_mission 繞過自動模式 arm 防護（031）")
    return await _run(sysid, "arm", mav.job_command, 400, [1.0])


@app.post("/api/command/{sysid}/disarm", tags=["操作"])
async def disarm(sysid: int):
    _require_enabled(); await _require_capability(sysid, "disarm")
    # **空中上鎖＝馬達停轉、飛機直接掉下來**——那不是「停止」，是墜毀。
    # 守門只在機體確定在地上時放行；真的要緊急切斷動力用遙控器（那是本系統
    # 設計裡人接管的那一層，而且它不經過我們）
    await guard_client.ask_guard(sysid, "disarm")
    return await _run(sysid, "disarm", mav.job_command, 400, [0.0])


@app.post("/api/command/{sysid}/mode/{mode}", tags=["操作"])
async def set_mode(sysid: int, mode: str, skip_guard: bool = False):
    if mode not in mav.PX4_MODES:
        raise HTTPException(422, f"mode 須為 {sorted(mav.PX4_MODES)}")
    _require_enabled(); await _require_capability(sysid, f"mode:{mode}")
    # skip_guard 只給**改航線序列內部**用：那條序列整體已經過守門，
    # 序列中的每一步再問一次會被守門用「已經在 hold 了」擋下自己
    if not skip_guard:
        await guard_client.ask_guard(sysid, f"mode:{mode}")
    return await _run(sysid, f"mode:{mode}", mav.job_set_mode, mode)


class ParamWrite(BaseModel):
    #: {參數名: 值}。只接受 params.ALLOWED 裡的名字
    params: dict[str, float]


@app.get("/api/command/{sysid}/params", tags=["參數"],
         summary="讀回可修改的飛控參數（現值）")
async def get_params(sysid: int, names: str | None = None):
    """**現值一律直接跟飛控要，不查資料庫。**

    後端確實存了整份參數快照（`param_sets`），但那是「某一趟飛行當時是什麼」。
    有人用 QGC 改過之後那份就是舊的，而這個畫面接下來要拿它當「改之前的值」
    ——**拿一個舊值當現值，比不顯示更糟**。
    """
    _require_enabled()
    await _require_capability(sysid, "param_get")
    # `names`（逗號分隔）＝只讀這幾個。**一次問少一點是有意義的**：
    # ArduPilot 收到 PARAM_REQUEST_READ 是排進一個很小的佇列，滿了就
    # **安靜地丟掉**；而這條 57600 的序列埠上同時跑著約 27 種 4Hz 的遙測，
    # 排到參數回覆的頻寬本來就很窄
    want = [n.strip() for n in names.split(",")] if names else list(fcparams.ALLOWED)
    unknown = [n for n in want if n not in fcparams.ALLOWED]
    if unknown:
        raise HTTPException(400, {"msg": f"不在可讀清單裡：{', '.join(unknown)}"})
    res = await _run(sysid, "param_get", mav.job_get_params, want,
                     params={"names": want})
    return {
        "values": res["values"],
        "missing": res["missing"],
        "elapsed_s": res.get("elapsed_s"),
        # 畫面要有範圍與單位才畫得出可用的輸入格（白名單存在的理由之一）
        "meta": {k: {"label": p.label, "unit": p.unit, "lo": p.lo, "hi": p.hi,
                     "why": p.why, "is_int": p.is_int}
                 for k, p in fcparams.ALLOWED.items()},
    }


@app.post("/api/command/{sysid}/params", tags=["參數"],
          summary="改飛控參數（白名單、只在未解鎖時）")
async def set_params(sysid: int, body: ParamWrite):
    """**白名單 ＋ 只在未解鎖時 ＋ 逐個讀回比對。**（2026-09-07 使用者裁定）

    在這之前本系統一個參數都不寫。改的是「參數編輯是 QGC 的職權」那一層；
    **後端那條 socket 維持唯讀**——它是遙測與錄製的路，永遠不該成為指令的
    來源，所以寫入做在這裡而不是那裡。

    三道自己的門（都在共用的入列／能力門之外另外加的）：

    1. **名字不在白名單就 400**，連送都不送（見 `params.ALLOWED`）。
    2. **超出範圍就 400**，附上那個參數為什麼有範圍。
    3. **解鎖中一律 409**：參數在飛行中生效的時點難以預測，而改錯的後果
       在天上才出現。要在飛行中調的東西不該走這條路。

    寫入後**逐個讀回比對**（同任務上傳的紀律）。飛控會自己夾值而且不告訴你，
    所以夾過的情況照實回報在 `clamped` 裡——那不是失敗（它確實接受了一個值），
    但也不是成功（那不是你要的值）。
    """
    _require_enabled()
    if not body.params:
        raise HTTPException(422, "沒有要改的參數")
    bad = [msg for n, v in body.params.items()
           if (msg := fcparams.validate(n, v)) is not None]
    if bad:
        await _refused(sysid, "param_set", "白名單", "；".join(bad),
                       {"params": body.params})
        raise HTTPException(400, {"msg": "參數不能改", "problems": bad})
    await _require_capability(sysid, "param_set")
    # **解鎖中不寫。** 這一道排在入列／能力之後：先確認是我們的機、
    # 再談它現在的狀態
    if (router.drones.get(sysid) or {}).get("armed"):
        why = "解鎖中不改參數——參數生效的時點難以預測，而改錯的後果在天上才出現"
        await _refused(sysid, "param_set", "解鎖", why, {"params": body.params})
        raise HTTPException(409, {"code": "armed", "msg": why,
                                  "hint": "先上鎖再改；要在飛行中調整的東西不該走這條路"})
    res = await _run(sysid, "param_set", mav.job_set_params, body.params,
                     params=body.params)
    return res


@app.post("/api/command/{sysid}/emergency/land", tags=["操作"],
          summary="緊急原地降落（系統內最高優先）")
async def emergency_land(sysid: int):
    """**把飛機放下來。** 出意外時按這一顆。

    送出去的東西與 `/mode/land` 一模一樣（切 LAND、原地下降），差別有兩個：

    1. **不問機上守門。** 守門回答的是「當下狀態允不允許」，而「把飛機放
       下來」在任何狀態下的答案都一樣——RTL／LAND 在意圖通道斷線時本來
       就已經是這樣處理的（`admission.OFFLINE_ACTIONS`），這裡只是把同一條
       理由推到通道正常的時候也成立。
    2. **在 `command_log` 裡有自己的名字**（`emergency_land`）。事後看得出
       「這一趟有人按過緊急降落」——而那是回放時最想知道的一件事。

    ## 它跳過什麼、不跳過什麼

    **跳過**：機上守門（第三層），以及地面站畫面上的所有節流——等回覆、
    兩段式確認、面板收合。那些是為了防手滑與防指令交錯，而這一顆存在的
    理由正是「其他東西卡住的時候它還要能按」。

    **不跳過這三道，它們不是流程而是「送出去會不會做錯事」**：

    * `ENABLE_COMMANDS=false`——那台部署宣告過自己只觀察不指揮。繞過它
      等於讓一個顯式宣告的安全開關失效，而不是讓飛機更安全。
    * **入列**：身分不明的機不是我們的機。對它下降落指令，可能是在指揮
      別人的飛機。
    * **能力**：機型未驗證時，我方不確定 LAND 在它上面對應到哪個模式；
      送過去可能切到別的東西——**那比不送更危險**。

    三道都會說得出是哪一道擋的，而且擋下的當下操作員手上還有實體遙控器
    ——那才是最後一道，不是這一顆。
    """
    _require_enabled()
    await _require_capability(sysid, "emergency_land")
    return await _run(sysid, "emergency_land", mav.job_set_mode, "land")


@app.post("/api/command/{sysid}/mission/start", tags=["任務"],
          summary="③ 開始執行機上的任務")
async def mission_start(sysid: int):
    """讓飛控開始執行**它機上現有**的那份任務（不帶任務內容——那是上一步的事）。

    **機必須已經解鎖並在空中**：對停在地面的機切自動任務模式，等於叫它自己起飛。
    要從地面一路到飛，用 `/api/command/{sysid}/mission/fly` 或 `/api/start`。
    """
    _require_enabled(); await _require_capability(sysid, "mission_start")
    await guard_client.ask_guard(sysid, "mission_start")
    res = await _run(sysid, "mission_start", mav.job_command, 300, [0.0])
    # 啟動的是**機上現有**的任務，所以要查這台機現在綁的是哪一份
    mid = await pool.fetchval(
        "SELECT current_plan_id::text FROM drones WHERE mav_sysid = $1", sysid)
    if mid:
        await guard_client.show_on_live(sysid, mid, "任務已啟動")
    return res


async def _live() -> dict:
    """backend 的即時快照（高度/armed）。讀不到時丟例外——序列不盲飛。"""
    loop = asyncio.get_running_loop()

    def _get():
        with urllib.request.urlopen(f"{settings.backend_api}/api/live",
                                    timeout=3) as r:
            return json.loads(r.read().decode())
    try:
        return await loop.run_in_executor(None, _get)
    except Exception as e:
        raise HTTPException(502, f"讀不到 backend 即時狀態（{e}）——"
                                 "起飛序列需要高度回饋，中止")


class TakeoffIn(BaseModel):
    alt: float = 10.0                  # 相對起飛點高度（公尺）


async def _do_takeoff(sysid: int, alt: float) -> dict:
    """解鎖（未解鎖時）→ NAV_TAKEOFF 到指定相對高度。

    PX4 的 MAV_CMD_NAV_TAKEOFF param7 是**絕對海拔**——用該機的
    alt_msl - alt_rel 推地面海拔再加目標高度；經緯度/偏航給 NaN＝原地。
    """
    # 地面海拔只有 PX4 需要（它的 param7 是絕對海拔）；ArduPilot 用相對高度，
    # 拿不到也能起飛。方言差異在 mav.job_takeoff 裡，這裡不判斷廠牌。
    #
    # **必須取這一台的高度**：原本讀 backend `/api/live`，而那個端點只回**主機**
    # ——飛非主機時等於拿別台的地面海拔去算絕對起飛高度。SITL 全機同址所以差值
    # 為零、看不出來；真機分散部署就會差一整個地面高差。改讀 router 的 per-sysid
    # 紀錄（與群組執行器同源）。
    dd = (router.drones.get(sysid) if router else None) or {}
    ground_amsl = None
    if dd.get("alt_msl") is not None and dd.get("alt_rel") is not None:
        ground_amsl = dd["alt_msl"] - dd["alt_rel"]
    res = await _run(sysid, f"takeoff:{alt}m", mav.job_takeoff, alt, ground_amsl)
    return res.get("steps", res)


class UploadIn(BaseModel):
    plan_id: str


@app.post("/api/command/{sysid}/takeoff", tags=["操作"])
async def takeoff(sysid: int, body: TakeoffIn):
    """監督式起飛：解鎖＋爬升到指定高度後自動懸停（PX4 自主執行）。
    取代「用 RC 手動飛到高度」的操作——連續操縱仍是 RC 的職權。"""
    _require_enabled(); await _require_capability(sysid, "takeoff")
    return await _do_takeoff(sysid, body.alt)


class FlyIn(BaseModel):
    plan_id: str | None = None      # 給了就先上傳＋回讀比對；不給＝用機上現有任務
    #: 切 AUTO 前那一段 GUIDED 起飛的相對高度。**省略＝跟著任務自己的
    #: NAV_TAKEOFF 走**（見 `_mission_takeoff_alt`），不是一個固定值。
    takeoff_alt: float | None = None
    alt_timeout_s: float = 60.0


def _with_command(row) -> dict:
    """`waypoints` 的一列 → 本系統 waypoints 模型（`command` 從 params 解出來）。

    plan_check 那一組函式吃的是解出來的形狀；DB 存的是 params JSONB。
    """
    w = dict(row)
    p = w.get("params")
    p = json.loads(p) if isinstance(p, str) else (p or {})
    w["command"] = p.get("command")
    # **`frame` 也要解出來。** 原本只解 `command`，於是 plan_check 的
    # frame 方言檢查（無座標項的 frame）在「上傳到機」這條路上**從來沒生效
    # 過**——它讀 `w["frame"]`，而這裡沒放。backend 的 /missions/{id}/check
    # 有解，所以同一份航線在畫面上會被擋、按上傳卻不會，兩邊說法不一致。
    # 地形預檢也要靠它分辨 frame 3（離起飛點）與 frame 10（跟地形）
    w["frame"] = p.get("frame")
    return w


async def _mission_takeoff_alt(plan_id: str | None) -> tuple[float, str]:
    """任務自己的起飛高度 →（高度, 依據）。讀不到就回保底值，**並說出為什麼**。

    **切 AUTO 前那一段不該由一個固定常數決定。** 原本寫死 10 m：對一份
    takeoff 2 m、航點 3 m 的低空航線，序列會先把機拉到 10 m 才切任務——
    實際飛行高度是規劃的三倍以上，而那個 10 不在任何一份 `.plan` 裡
    （2026-09-07 使用者回報）。任務裡的 NAV_TAKEOFF 已經寫了要爬到哪。

    挑選規則在 `plan_check.takeoff_alt`——**群飛路徑用的是同一份**。
    """
    if not plan_id:
        return plan_check.FALLBACK_TAKEOFF_ALT, "沒有任務可讀，用保底值"
    rows = await pool.fetch(
        "SELECT alt, action, params FROM waypoints WHERE plan_id = $1 ORDER BY seq",
        plan_id)
    alt, why = plan_check.takeoff_alt([_with_command(r) for r in rows])
    return (alt, why) if alt is not None else (plan_check.FALLBACK_TAKEOFF_ALT,
                                               f"{why}，用保底值")


def _airborne(sysid: int) -> tuple[bool | None, str, float | None]:
    """這台機離地了沒 →（判定, 依據, alt_rel）。實作在 `mav.airborne_of`，
    **群飛路徑用的是同一份**。"""
    return mav.airborne_of(router, sysid) if router else (None, "指令服務未連線", None)


@app.post("/api/command/{sysid}/mission/fly", tags=["一鍵"])
async def mission_fly(sysid: int, body: FlyIn):
    """起飛→任務自動序列（實戰教訓 2026-08-11：地面直接 MISSION_START
    在實機上會失敗，須先離地）：

      （上傳＋回讀比對）→ 解鎖 → NAV_TAKEOFF → **等機端回報離地**
      → 切 AUTO.MISSION（已在空中，PX4 跳過任務內的 takeoff 項續飛）

    沒判定到離地就不切任務——序列在任何一步失敗都停在安全狀態
    （PX4 起飛後自動懸停），並回報卡在哪一步。

    **這一段只負責「離地」，不負責飛到任務高度**：`takeoff_alt` 省略時取
    任務自己的 NAV_TAKEOFF 高度（見 `_mission_takeoff_alt`），離地判定看
    機端的 `landed_state`（見 `_airborne`）。兩者用的依據都寫進 `steps`。
    """
    _require_enabled(); await _require_capability(sysid, "mission_fly")
    await guard_client.ask_guard(sysid, "mission_fly")
    steps = {}
    if body.plan_id:
        steps["upload"] = await mission_upload(sysid, UploadIn(plan_id=body.plan_id))
    # **這一段只是「離地」，高度跟著任務走。** mid 在這裡就解出來（原本是
    # 序列跑完才解）——不給 plan_id 的呼叫用的是機上現有任務，那份任務的
    # 起飛高度同樣該由它自己決定
    mid = body.plan_id or await pool.fetchval(
        "SELECT current_plan_id::text FROM drones WHERE mav_sysid = $1", sysid)
    if body.takeoff_alt is not None:
        alt_target, alt_src = float(body.takeoff_alt), "呼叫端指定"
    else:
        alt_target, alt_src = await _mission_takeoff_alt(mid)
    steps["takeoff_alt"] = {"alt_m": alt_target, "source": alt_src}
    steps.update(await _do_takeoff(sysid, alt_target))

    # 等機真的離地（退回高度判準時 80% 即視為到位，PX4 收斂段不必等滿）
    # **必須看這一台的高度**：原本讀 backend `/api/live`，而那個端點只回**主機**。
    # 飛非主機時這個判斷完全與目標機無關——2026-08-12 前端驗收實測，uav-s2 起飛
    # 成功卻回報「未達目標高度（-0.04m）」，那個 -0.04 是停在地面的主機。
    # 反方向更危險：**主機在空中而目標機沒起來時，這個檢查會通過**，於是把一台
    # 還在地面的機切進 AUTO.MISSION——正是本序列存在的理由（2026-08-11 教訓）被
    # 架空。群組執行器早就改用 per-sysid（mav.py GLOBAL_POSITION_INT），單機路徑
    # 漏了同一課，現在同源。
    #
    # **判準優先序：機端的 landed_state ＞ 高度**（見 `_airborne`）。原本只看
    # alt_rel ≥ 目標×0.8，而目標值現在跟著任務走、可以低到 1–2 m——那個門檻
    # 會落進 alt_rel 自己的漂移範圍裡（停在地面漂到 4.4 m 是量過的），於是
    # 「等到高度」變成一句不成立的保證。門檻降低不是放寬安全，是讓那道門
    # 不再證明任何事，所以改由機端自己說它在不在空中。
    deadline = asyncio.get_running_loop().time() + body.alt_timeout_s
    alt = None
    basis = None
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        up, why, alt = _airborne(sysid)
        if up:
            basis = why
            break
        if up is None and alt is not None and alt >= alt_target * 0.8:
            # 退回高度判準——**並且說出退回了**。操作員必須分得出「機端說它
            # 在空中」與「機端沒說，我在拿一個會漂的數字猜」
            basis = f"{why}，退回高度判準（alt_rel {alt} m ≥ {alt_target * 0.8:.1f} m）"
            break
    else:
        _, why, alt = _airborne(sysid)
        await _audit(sysid, "mission_fly", body.model_dump(), "failed",
                     f"起飛後 {body.alt_timeout_s:.0f}s 判定不到離地"
                     f"（{why}，alt_rel {alt} m / 目標 {alt_target} m）")
        raise HTTPException(504, {
            "msg": f"起飛後判定不到機已離地（{why}，"
                   f"目前 alt_rel {alt} m / 目標 {alt_target} m）",
            "hint": "機停在懸停狀態，未啟動任務——檢查 RC/遙測後可重試或 RTL",
            "steps": steps})
    steps["airborne"] = {"alt_rel": alt, "basis": basis}

    steps["mission"] = await _run(sysid, "mode:mission", mav.job_set_mode, "mission")
    await _audit(sysid, "mission_fly", body.model_dump(), "accepted", json.dumps(steps))
    if mid:
        await guard_client.show_on_live(sysid, mid, "起飛→任務")
    return {"ok": True, "steps": steps}


#: MAV_AUTOPILOT／MAV_TYPE → 人話。**認不得的值原樣顯示 id**，不寫「未知」——
#: 那會讓「沒宣告」與「宣告了但我們沒收錄」看起來一樣。
_AP_NAMES = {0: "通用", 3: "ArduPilot", 12: "PX4"}
_VT_NAMES = {1: "定翼", 2: "四旋翼", 10: "地面載具", 12: "潛航器",
             13: "六旋翼", 14: "八旋翼"}


def _inflight_upload_block(sysid) -> str | None:
    """空中上傳任務的守門。回傳擋下的理由，可以上傳就回 None。

    **這是狀態機文件 §3-A 列為最高優先的危害，而且不是假想**：2026-08-25 兩家
    SITL 實測——飛行中上傳新任務，**兩家都不會把 `MISSION_CURRENT` 歸零**，
    而是把舊任務的索引原封沿用到新任務上（PX4 seq 2→2、ArduPilot seq 3→4）。
    舊任務的第 N 點與新任務的第 N 點毫無關係，所以飛機會立刻轉向一個純粹由
    「上一份任務碰巧進行到第幾點」決定的位置。那不是次佳解，是未定義行為。

    **上傳在地面是存檔，在空中是立即生效的航線變更。** 同一個動作、兩種語意，
    而畫面上長得一模一樣——這正是它危險的地方。

    擋下之後**不是死路**：空中改航線的合法路徑是三步（§3-A2），每步都要讀回
    機端狀態確認：
      1. `POST /api/command/{sysid}/mode/hold` —— 確認真的進了 hold
      2. 此時上傳（機體在懸停，上傳不會造成移動）
      3. `POST /api/command/{sysid}/mode/mission` ＋ 續飛到指定航點

    **刻意不提供 force 參數**：留一個繞道就等於沒擋——趕時間的人一定會用它，
    而趕時間正是最需要這道門的時候。

    只擋「正在飛任務」這一格：
    * 未 armed → 放行（地面存檔，本來就該允許）
    * armed 但還在地上 → 放行（上傳不會讓它動）
    * armed、在空中、模式是 mission → **擋下**
    * armed、在空中、其他模式（hold／飛手手飛）→ 放行。hold 正是 A2 的第二步；
      飛手手飛時機體不吃任務，上傳不會改變它的航線
    """
    d = (router.drones.get(sysid) or {}) if router else {}
    if not d.get("armed"):
        return None
    alt = d.get("alt_rel")
    if alt is not None and alt < 1.0:
        return None                     # 還在地上
    cm = d.get("custom_mode")
    if cm is None:
        return None                     # 不知道模式就不擋——擋一個不確定的狀態
    drv = mav.dialect(router, sysid)["driver"]
    if drv.decode_verb(cm) != "mission":
        return None
    return ("這台機正在空中執行任務，上傳會**立即改變它現在飛的航線**"
            "（機端不會重設任務索引，會直接轉向新航線的對應點）。"
            "空中改航線請走三步：先切 hold 並確認進入、再上傳、再切回 mission 並"
            "指定續飛航點。")


def _target_mismatch(mission_fw, mission_vt, sysid) -> list[str]:
    """任務自報的機種 vs 這台機實際偵測到的。回傳警告句（可能為空）。

    **為什麼這件事非查不可**：`build_items()` 刻意保留 `.plan` 的顯式
    `frame` 與 `params`（MAVLink 保真度，不覆寫使用者的值）——這是對的，
    但它的後果是**照 A 家語意寫出來的值會原封送給 B 家的機**。
    ArduPilot 與 PX4 在 frame 預設、home 是否佔 seq 0、空白參數慣例
    （NaN vs 0）上都不同（見 issues/026 的差異點表）。

    **示警不擋**（使用者定案 2026-08-24）：多數航點跨家其實飛得動，硬擋會逼
    人去改檔案繞過，反而更糟。但必須**在上傳前就講**，而不是讓他用失敗去發現
    （ui-spec §0.2c 條款 6）。
    """
    out = []
    ap = router.autopilot_of(sysid) if router else None
    vt = (router.drones.get(sysid) or {}).get("type") if router else None
    if mission_fw is not None and ap is not None and int(mission_fw) != int(ap):
        out.append(
            f"這份航線宣告是給 {_AP_NAMES.get(int(mission_fw), f'firmware {mission_fw}')} "
            f"寫的，這台機是 {_AP_NAMES.get(int(ap), f'firmware {ap}')}"
            "——航點的 frame 與參數語意可能不同（見 issues/037）")
    if mission_vt is not None and vt is not None and int(mission_vt) != int(vt):
        out.append(
            f"這份航線宣告的機型是 {_VT_NAMES.get(int(mission_vt), f'type {mission_vt}')}，"
            f"這台機是 {_VT_NAMES.get(int(vt), f'type {vt}')}")
    return out


class GotoIn(BaseModel):
    index: int          # **我方航點索引（0 起）**，不是機端 seq——換算走驅動


@app.post("/api/command/{sysid}/mission/goto")
async def mission_goto(sysid: int, body: GotoIn):
    """從指定的航點續飛（issues/039 的「續飛」）。

    參數是**我方索引**而不是機端 seq：機端 seq 的慣例因廠牌而異
    （ArduPilot 的 home 佔 0），讓呼叫端算＝把方言洩漏到每一個呼叫點。
    """
    _require_enabled(); await _require_capability(sysid, "mission_start")
    if body.index < 0:
        raise HTTPException(422, "index 不得為負")
    return await _run(sysid, "mission_goto", mav.job_mission_goto, body.index,
                      params={"index": body.index})


async def _load_wps(plan_id: str) -> tuple[list[dict], str]:
    rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", plan_id)
    if not rows:
        raise HTTPException(404, "任務不存在或沒有航點")
    name = await pool.fetchval("SELECT name FROM plans WHERE id = $1",
                               plan_id) or plan_id
    wps = []
    for r in rows:
        w = dict(r)
        p = w.get("params")
        p = json.loads(p) if isinstance(p, str) else (p or {})
        w["command"] = p.get("command")
        wps.append(w)
    return wps, name


def _cur_of(sysid: int) -> dict:
    d = (router.drones.get(sysid) or {}) if router else {}
    return {"lat": d.get("lat"), "lon": d.get("lon"),
            "alt_rel": d.get("alt_rel"), "heading": d.get("heading"),
            "armed": d.get("armed")}


class ChangeRouteIn(BaseModel):
    plan_id: str
    hold_alt: float | None = None      # 不給＝暫停後維持當前高度
    # 執行時把**人看到的那份提案**送回來，用來比對這段時間機體有沒有飄掉
    # （協定 §5.1）。省略＝不做漂移檢查，只有非互動的呼叫端該這樣用
    proposal: dict | None = None
    #: 提案的 id。**確認的是那一份**，所以執行時要指名是哪一份
    intent_id: str | None = None


@app.post("/api/command/{sysid}/mission/change-route/proposal")
async def change_route_proposal(sysid: int, body: ChangeRouteIn):
    """**飛行中改航線的第一步：提案。這個端點不動飛機。**

    回傳「會怎麼調整」——狀態機文件 §6.3 要求確認畫面不得只問「確定嗎？」，
    因為那種確認框沒有資訊，人只會照按。所以這裡算得出：續飛到哪一點、
    離現在多遠、往哪個方向、會不會先爬升或下降、以及這是可中止的三步序列。

    **在地上不需要走這條路**：地面上傳是存檔，直接用 mission/upload。
    確認要保持稀有才有意義——每次上傳都跳確認框，會訓練人閉著眼睛按，
    然後空中那次也照按（§6.3 明文禁止把它做成 upload 的預設行為）。
    """
    _require_enabled()
    wps, name = await _load_wps(body.plan_id)
    # **先問機上**：提案的權威在代理（狀態機文件 §0.1）——它讀飛控是微秒級，
    # 而我們手上的位置經 5G 回來已經過期。10 m/s 巡航下一次鏈路抖動就是數十
    # 公尺誤差，而「離當前位置最近的航點」正是對位置最敏感的判斷
    res = await guard_client.ask_guard(sysid, "change_route", params={
        "wps": [{k: w[k] for k in ("seq", "lat", "lon", "alt", "action",
                                   "command") if k in w} for w in wps],
        "hold_alt": body.hold_alt, "plan_name": name,
        "plan_id": body.plan_id})
    p = (res.get("event") or {}).get("proposal") if res else None
    if p is None:
        # **只有機上算，沒有備援**（使用者裁定 2026-08-25）。地面站再寫一份
        # 就是第二個事實來源，而它的症狀特別陰險：同一台機，有代理時飛去
        # A 點、代理掉線時飛去 B 點，沒有錯誤訊息，只有事後看軌跡覺得怪。
        # 沒有代理就老實說做不了——**不能算的時候說不能算，不要拿次一等的
        # 資料算一個看起來很正常的答案**
        raise HTTPException(409, {
            "msg": "飛行中改航線的提案只由機上代理算（它的位置是第一手的）。"
                   f"這台機現在問不到：{(res or {}).get('reason') or '意圖通道未連線'}",
            "code": "no_agent"})
    p["intent_id"] = res.get("intent_id")     # 確認時要用同一個 id
    p["airborne"] = _inflight_upload_block(sysid) is not None
    if not p["airborne"]:
        p["warnings"] = list(p["warnings"]) + [
            "這台機現在不在飛任務——地面上傳直接用 mission/upload 即可，"
            "不需要走三步序列"]
    return p


@app.post("/api/command/{sysid}/mission/change-route")
async def change_route_exec(sysid: int, body: ChangeRouteIn):
    """**第二步：執行三步序列。** 每一步都讀回機端確認，任何一步沒過就停在
    安全狀態（懸停），不會繼續往一個沒人確認過的方向飛。

    **執行當下重算提案**（協定 §5.1）：人確認的是**那一份**提案，而飛機在
    人看提案的那段時間裡一直在動。續飛航點換了、或距離變化超過門檻，就中止
    並回新的提案要人重看——不照著過期的提案做。
    """
    _require_enabled()
    if not body.intent_id:
        raise HTTPException(422, "缺 intent_id——請先取得提案，確認的是**那一份**")
    # **送 decision，不是把提案送回去**（協定 §4.5）：提案留在機上，
    # 所以沒有「送回來的那份跟人看到的不一樣」的空間。代理收到確認後自己
    # 重算並比對過期，守門也在那一刻再過一次（狀態可能在人看提案時變了）
    res = await guard_client.ask_guard(sysid, "change_route", intent_id=body.intent_id,
                           kind="decision", params={"approved": True})
    if res is None or res.get("verdict") == "no_agent":
        raise HTTPException(409, {
            "msg": "飛行中改航線只能由機上代理確認（它的位置是第一手的）。"
                   f"這台機現在問不到：{(res or {}).get('reason') or '意圖通道未連線'}",
            "code": "no_agent"})
    ev = res.get("event") or {}
    if ev.get("event") != "cleared":
        await _audit(sysid, "change_route", body.model_dump(),
                     "rejected_decision", res.get("reason") or "")
        raise HTTPException(409, {
            "msg": res.get("reason") or "機上不同意執行",
            "code": ev.get("event") or "refused",
            "proposal": ev.get("proposal")})
    fresh = ev.get("proposal")
    wps, name = await _load_wps(body.plan_id)
    steps: dict = {}

    async def note(step, ok_, detail=""):
        """把每一步回報給機上（協定 §4.6）。**不是為了記帳**——代理要知道
        序列跑到哪一步，因為 §7 規定序列進行中失聯要立刻 RTL。"""
        try:
            await guard_client.ask_guard(sysid, "change_route", intent_id=body.intent_id,
                             kind="progress",
                             params={"step": step, "ok": ok_,
                                     "detail": str(detail)[:200]})
        except HTTPException:
            raise
        except Exception:
            log.warning("序列回報失敗（不影響序列本身）", exc_info=True)

    async def step(key, coro, label):
        try:
            steps[key] = await coro
        except HTTPException as e:
            steps[key] = {"ok": False, "detail": e.detail}
            await note(key, False, e.detail)
            await _audit(sysid, "change_route", body.model_dump(),
                         "failed", f"{label}：{e.detail}")
            raise HTTPException(409, {
                "msg": f"改航線序列在「{label}」這一步停下，機體停在安全狀態",
                "steps": steps, "proposal": fresh})
        await note(key, True)

    # 1. 暫停 —— job_set_mode 內含讀回確認（mode_engaged），不是只看 ACK
    await step("hold", set_mode(sysid, "hold", skip_guard=True), "切 hold 懸停")
    # 2. 上傳 —— 機體已在 hold，守門會放行（只擋 mission 模式）
    await step("upload", mission_upload(sysid, UploadIn(plan_id=body.plan_id)),
               "上傳新航線")
    # 3. 續飛 —— 先指定航點再切 mission。**順序不能反**：先切 mission 的話
    #    機體會用舊索引開始飛，而那個索引在新航線上毫無意義
    await step("goto", mission_goto(sysid, GotoIn(index=fresh["resume_wp"]["index"])),
               "指定續飛航點")
    await step("resume", set_mode(sysid, "mission", skip_guard=True),
               "切回 mission")

    await _audit(sysid, "change_route", body.model_dump(), "ok",
                 f"續飛第 {fresh['resume_wp']['index']} 點")
    await guard_client.show_on_live(sysid, body.plan_id,
                        f"改航線完成，從第 {fresh['resume_wp']['index']} 點續飛")
    return {"ok": True, "steps": steps, "proposal": fresh}


@app.post("/api/command/{sysid}/mission/clear", tags=["任務"],
          summary="清掉機上那份任務")
async def mission_clear(sysid: int):
    """把機上的任務清空。

    **空中守門與上傳同一條**（`_inflight_upload_block`）：正在飛任務時清掉它，
    後果不會比上傳新的一份輕——飛控手上那份航線消失，而它正在照著飛。
    合法路徑一樣是先 `mode/hold`。

    地面上則放行：那正是這個按鈕存在的理由——**換任務不該被迫用「上傳另一份
    蓋過去」來達成**，那是一個更重、更容易出錯的動作。
    """
    _require_enabled(); await _require_capability(sysid, "mission_upload")
    blocked = _inflight_upload_block(sysid)
    if blocked:
        raise HTTPException(409, {"msg": blocked, "code": "inflight_clear",
                                  "how_to": [
                                      "先 POST /api/command/{sysid}/mode/hold 並確認進了 hold",
                                      "此時再清除（機體在懸停，清除不會造成移動）"]})
    await guard_client.ask_guard(sysid, "mission_clear")
    try:
        res = await _run(sysid, "mission_clear", mav.job_clear_mission)
    except HTTPException as e:
        # 409 讀不回、504 逾時：可能已經清掉了，舊名字不能再掛著——改成未知
        if e.status_code in (409, 504):
            await pool.execute("UPDATE drones SET current_plan_id = NULL, "
                               "plan_cleared_at = NULL WHERE mav_sysid = $1", sysid)
        raise
    # 不清 current_plan_id 的話，畫面還寫著舊路徑，下一趟架次也會掛到它名下
    await pool.execute("UPDATE drones SET current_plan_id = NULL, plan_cleared_at = now() "
                       "WHERE mav_sysid = $1", sysid)
    return res


def _terrain_probe_points(wps: list[dict], home) -> list:
    """要問飛控哪幾個點 →`[(lat, lon, 標籤), ...]`。

    **均勻取樣，而且一定含頭尾。** 問得到的點數有上限（`mav.TERRAIN_PROBE_MAX`
    ——那條 57600 的序列埠一次問不了太多），取前 N 個會讓航線後半段完全沒被
    問到，而地形出問題的地方不會挑前半段。
    """
    pts: list[tuple[float, float, str]] = []
    if home and len(home) >= 2 and (home[0] or home[1]):
        pts.append((float(home[0]), float(home[1]), "起飛點"))
    nav = [w for w in wps if w.get("lat") and w.get("lon") and plan_check._is_nav(w)]
    room = mav.TERRAIN_PROBE_MAX - len(pts)
    if len(nav) > room and room > 1:
        step = (len(nav) - 1) / (room - 1)
        nav = [nav[int(round(i * step))] for i in range(room)]
    return pts + [(w["lat"], w["lon"], f"seq {w['seq']}") for w in nav[:room]]


#: 飛控與地面站兩份地形資料差多少算「對得上」。5 m 不是隨便取的：
#: SRTM 的絕對誤差 LE90 約 16 m，而飛控那份多半也源自 SRTM——兩份同源時
#: 差距應該很小；**差超過 5 m 就代表它們不同源，或有一邊沒有那塊資料**。
TERRAIN_AGREE_M = 5.0


@app.get("/api/command/{sysid}/logs", tags=["任務"],
         summary="飛控 SD 卡上的 dataflash 紀錄清單")
async def flight_logs(sysid: int):
    """**只列清單，不下載。** 下載走 MAVLink 要與遙測搶同一條 57600 的序列埠
    ——先看得到大小，才決定要不要走這條線（幾 MB 就是幾十分鐘起跳），
    還是直接把 SD 卡拔下來讀。
    """
    _require_enabled()
    await _require_capability(sysid, "param_get")
    res = await _run(sysid, "log_list", mav.job_log_list, params={})
    # 傳輸時間的估計要跟著清單走，不然「2.7 MB」對操作員沒有意義。
    # 90 bytes/則 ＋ MAVLink 表頭，57600 8N1 → 每秒約 56 則、5 KB/s，
    # 而那是**遙測完全讓路**時的上限
    for lg in res["logs"]:
        lg["mav_minutes"] = round(lg["size"] / 5000.0 / 60.0, 1)
    return res


LOG_DIR = os.environ.get("FLIGHT_LOG_DIR", "/data/flight-logs")


@app.post("/api/command/{sysid}/logs/{log_id}/fetch", tags=["任務"],
          summary="抓一塊 dataflash 紀錄（分塊，可重複呼叫直到完成）")
async def fetch_log_chunk(sysid: int, log_id: int, ofs: int = 0,
                          nbytes: int = mav.LOG_CHUNK_B):
    """把一塊寫進 `FLIGHT_LOG_DIR/<sysid>-<log_id>.bin`，回報寫到哪裡。

    **分塊是刻意的**：整段抓完要好幾分鐘，而工作跑在與所有指令共用的那條
    執行緒上——那幾分鐘裡解鎖、切模式、緊急降落全部會排在後面。
    呼叫端拿 `next` 再要下一塊（`scripts/fetch-flight-log.py` 就是那個迴圈）。

    **只在 `ofs` 等於目前檔案大小時才接受**：亂序寫入會產生一個看起來
    完整、其實錯位的 `.bin`，而那種檔案解析得出來、內容是錯的。
    """
    _require_enabled()
    await _require_capability(sysid, "param_get")
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{sysid}-{log_id}.bin")
    have = os.path.getsize(path) if os.path.exists(path) else 0
    if ofs != have:
        raise HTTPException(409, {
            "msg": f"位移對不上：檔案目前 {have} bytes，而你要從 {ofs} 開始寫",
            "have": have,
            "how_to": [f"從 ofs={have} 繼續", "或先刪掉那個檔案重抓"]})
    res = await _run(sysid, "log_fetch", mav.job_log_fetch, log_id, ofs,
                     nbytes, path, params={"log_id": log_id, "ofs": ofs})
    return {"path": path, "ofs": ofs, "wrote": res["bytes"],
            "next": res["next"], "holes": res["holes"],
            "total_have": have + res["bytes"]}


@app.get("/api/command/{sysid}/terrain", tags=["任務"],
         summary="跟飛控核對地形資料（issues/047 §2）")
async def terrain_crosscheck(sysid: int, plan_id: str | None = None):
    """問飛控「你認為這幾個點的地面多高」，跟地面站的 SRTM 對照。

    **兩份不一致本身就是要報告的事實**，不是要挑一個當真相——飛機實際跟隨
    的是它自己那份。地面站這份只決定「我們在畫面上警告什麼」。

    `plan_id` 給了就沿那條航線取樣（含起飛點）；不給就只問飛機現在的位置。

    三種要分開讀的結果：

    * **`pending > 0`**：飛控自己缺那塊地形資料。ArduPilot 的圖磚來自 SD 卡
      或會供圖的地面站，而本系統不供圖——**缺的不會自己補上**。
    * **`diff` 大**：兩份資料不同源。照實列出來，不做平均。
    * **沒回應**：`TERRAIN_REPORT` 跟 `PARAM_VALUE` 一樣會被塞滿的序列埠丟掉
      ——**沒回應不等於沒有地形資料**，那是兩件事。
    """
    _require_enabled()
    await _require_capability(sysid, "param_get")
    dem = terrain.shared()
    pts: list[tuple[float, float, str]] = []
    if plan_id:
        rows = await pool.fetch(
            "SELECT seq, lat, lon, alt, action, params FROM waypoints "
            "WHERE plan_id = $1 ORDER BY seq", plan_id)
        if not rows:
            raise HTTPException(404, "任務不存在或沒有航點")
        meta = await pool.fetchrow("SELECT home FROM plans WHERE id = $1",
                                   plan_id)
        home = meta["home"] if meta else None
        if isinstance(home, str):
            home = json.loads(home)
        pts = _terrain_probe_points([_with_command(r) for r in rows], home)
    else:
        d = (router.snapshot() if router else {}).get(str(sysid)) or {}
        if not (d.get("lat") and d.get("lon")):
            raise HTTPException(409, {
                "msg": "沒給 plan_id，而且讀不到飛機現在的位置",
                "how_to": ["帶上 plan_id 沿航線取樣"]})
        pts.append((float(d["lat"]), float(d["lon"]), "現在位置"))

    res = await _run(sysid, "terrain_check", mav.job_terrain_check, pts,
                     params={"plan_id": plan_id, "points": len(pts)})

    max_diff = 0.0
    pending_total = 0
    for rec in res["points"]:
        gz = dem.elevation(rec["lat"], rec["lon"])
        rec["gcs_dem_m"] = None if gz is None else round(gz, 1)
        if "terrain_height_m" in rec:
            pending_total += rec["pending"]
            if gz is not None:
                rec["diff_m"] = round(rec["terrain_height_m"] - gz, 1)
                max_diff = max(max_diff, abs(rec["diff_m"]))
            # 飛控回的座標離我方問的有多遠：差一格就是差一個 spacing
            rec["offset_m"] = round(plan_check._dist_m(
                rec["lat"], rec["lon"], rec["reported_lat"], rec["reported_lon"]), 1)

    notes: list[str] = []
    unanswered = res["asked"] - res["answered"]
    if unanswered:
        notes.append(
            f"{unanswered}/{res['asked']} 個點沒有回應——**這不等於「那裡沒有"
            "地形資料」**。TERRAIN_REPORT 跟 PARAM_VALUE 一樣會被塞滿的序列埠"
            "丟掉，先重試一次再下結論")
    if pending_total:
        notes.append(
            f"飛控還缺 {pending_total} 格地形資料。它的圖磚來自 SD 卡或會供圖的"
            "地面站，**本系統不供圖**——缺的那塊不會自己補上，那一段用地形跟隨"
            "飛就是在等失效返航")
    if max_diff > TERRAIN_AGREE_M:
        notes.append(
            f"兩份地形資料最大差 {max_diff:.1f} m（門檻 {TERRAIN_AGREE_M:g} m）"
            "——代表它們不同源。**飛機跟的是它自己那份**，地面站的預檢只能當參考")
    if res.get("stray"):
        notes.append(
            f"期間收到 {res['stray']} 則配不上任何查詢的地形報告（已丟棄）。"
            "**飛控會自己送不請自來的 TERRAIN_REPORT**——沒有丟掉的話，"
            "整串答案會錯開一格，而且每個數字看起來都很合理")
    return {
        "asked": res["asked"], "answered": res["answered"],
        "stray": res.get("stray", 0),
        "pending_total": pending_total,
        "max_diff_m": round(max_diff, 1) if res["answered"] else None,
        "agree": bool(res["answered"] == res["asked"] and not pending_total
                      and max_diff <= TERRAIN_AGREE_M),
        "points": res["points"], "notes": notes,
    }


@app.post("/api/command/{sysid}/mission/upload", tags=["任務"],
          summary="② 上傳任務到無人機（會逐項讀回比對）")
async def mission_upload(sysid: int, body: UploadIn):
    """把任務庫的一份航線寫進飛控，**並逐項讀回比對**——ACK 是「我收到了」，
    不是「我做到了」。

    ⚠ **上傳在地面是存檔，在空中是立即生效的航線變更**：飛控收到新任務的那一刻
    就會照它飛。飛行中要換航線請走 `change-route`（暫停→上傳→從最近的航點續飛，
    每步讀回確認），不要直接呼叫本端點。
    """
    _require_enabled(); await _require_capability(sysid, "mission_upload")
    rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", body.plan_id)
    if not rows:
        raise HTTPException(404, "任務不存在或沒有航點")
    meta = await pool.fetchrow(
        "SELECT firmware_type, vehicle_type, fence, home FROM plans WHERE id = $1",
        body.plan_id)
    wps = [_with_command(r) for r in rows]   # plan_check 用原始 command 判導航類
    # 幾何預檢：報告一律附在回應與留痕；GEOFENCE_ENFORCE=true 才擋
    # （預設不擋——2026-08-10 使用者決定；空中防線是 PX4 自己的 Geofence）
    # 圍欄用**這份航線自己宣告的**（存在 missions.fence，來自 .plan 的
    # geoFence）；沒宣告才退回系統預設，而報告會說出用的是哪一個
    mf = meta["fence"] if meta else None
    if isinstance(mf, str):
        mf = json.loads(mf)
    # 上傳時**用實際偵測到的機種**，不是航線宣告的：這一刻我們知道真相，
    # 而航線的宣告可能是錯的（QGC 在離線狀態下規劃就會寫成預設的 PX4）
    ap = router.autopilot_of(sysid) if router else None
    # **速度相關的檢查要拿機上的值，不能用猜的**（issues/048 C5）：
    # 航線裡的 `DO_CHANGE_SPEED` 只從被執行到的那一項之後才生效，在那之前
    # 用的是機上的 `WP_SPD`——2026-09-07 使用者以為全程 0.3，起飛到第一個
    # 航點卻是 8。讀不到就是**不判定**（`leg_profile` 會說「速度沒有檢查」），
    # 不是當成安全。
    wp_spd = wp_radius = None
    try:
        got = await _run(sysid, "param_get", mav.job_get_params,
                         ["WP_SPD", "WP_RADIUS_M"],
                         params={"names": ["WP_SPD", "WP_RADIUS_M"],
                                 "why": "leg_profile"})
        wp_spd = got["values"].get("WP_SPD")
        wp_radius = got["values"].get("WP_RADIUS_M")
    except Exception as e:                                    # noqa: BLE001
        log.warning("讀不到 WP_SPD／WP_RADIUS_M（%s）——速度相關的檢查會標成"
                    "「沒有檢查」，不會當成通過", e)
    report = plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, fence=mf, dem=terrain.shared(),
        wp_spd=wp_spd, wp_radius=wp_radius,
        autopilot=ap if ap is not None else (meta["firmware_type"] if meta else None),
        home=json.loads(meta["home"]) if meta and isinstance(meta["home"], str)
        else (meta["home"] if meta else None))
    # 機種不符：併進**既有的 warnings**而不是自成一個欄位——前端已經會顯示
    # 這份報告，多開一個欄位就多一個可能沒人接的顯示點（issues/037）。
    # 併進 warnings 不影響 `ok`，所以不會意外觸發 GEOFENCE_ENFORCE 的擋門。
    mismatch = _target_mismatch(
        meta["firmware_type"] if meta else None,
        meta["vehicle_type"] if meta else None, sysid)
    if mismatch:
        report["warnings"] = list(report.get("warnings") or []) + mismatch
        log.warning("任務機種與機體不符（照常上傳）：%s", "；".join(mismatch))
    # **空中守門排在幾何預檢之前**：幾何預檢問的是「這份航線本身合不合理」，
    # 空中守門問的是「現在做這件事會不會讓飛機立刻轉向」。後者與航線內容無關，
    # 一份完美的航線在空中上傳一樣危險
    blocked = _inflight_upload_block(sysid)
    if blocked:
        await _audit(sysid, "mission_upload", {"plan_id": body.plan_id},
                     "rejected_inflight", blocked)
        log.warning("擋下空中上傳（sysid %d）：%s", sysid, blocked)
        raise HTTPException(409, {"msg": blocked, "code": "inflight_upload",
                                  "how_to": ["切 hold 並確認進入",
                                             "上傳新航線",
                                             "切回 mission 並指定續飛航點"]})
    # **簽核閘門**（redesign §7）：這一份在什麼假設下被誰看過。
    # 放在地形那道門**之前**——地形那道門只會說「這條穿地」，而更根本的
    # 問題是「這一份根本沒有人看過」，那兩句話該分開。
    if settings.sign_enforce:
        h = plan_check.waypoints_hash(wps)
        sg = await pool.fetchrow(
            "SELECT waypoints_hash, fence_hash, ok, problems, acknowledged, "
            "assumed_m, signed_by, checked_at FROM plan_checks WHERE plan_id = $1 "
            "ORDER BY checked_at DESC LIMIT 1", body.plan_id)
        why = None
        if sg is None:
            why = "這一份還沒有人看過檢查結果"
        elif sg["waypoints_hash"] != h:
            why = "航點在簽核之後改過了，那份簽核不算數"
        elif sg["fence_hash"] != plan_check.fence_hash(mf):
            # 圍欄一改，檢查結果就不是審查時看到的那一份。沒有圍欄的航線
            # 兩邊都是 None，不受影響
            why = "圍欄在簽核之後改過了，那份簽核不算數"
        else:
            probs = sg["problems"]
            acks = sg["acknowledged"]
            probs = json.loads(probs) if isinstance(probs, str) else (probs or [])
            acks = json.loads(acks) if isinstance(acks, str) else (acks or [])
            left = [p for p in probs if p not in acks]
            if left:
                why = f"還有 {len(left)} 條沒有人按過「我知道，照飛」"
        if why:
            await _audit(sysid, "mission_upload", {"plan_id": body.plan_id},
                         "rejected_unsigned", why)
            raise HTTPException(409, {
                "msg": f"未上傳：{why}", **report,
                "how_to": [
                    "打開這份航線的規劃頁，看過檢查結果之後按「確認」",
                    "有問題但你決定照飛的，逐條按「我知道，照飛」——"
                    "**那一條會記下是誰、什麼時候、在什麼假設下決定的**",
                    "要整個關掉這道門就把 SIGN_ENFORCE 設成 false"
                    "（那一次會留痕）"]})

    # **地形是自己一道門**（issues/047 §1-B）：不掛在 GEOFENCE_ENFORCE 底下。
    # 圍欄擋下來多半是「系統預設值跟你的場地無關」，地形擋下來是「這條航線
    # 穿過地面」——後者是 2026-09-07 摔機的形狀，預設就該擋。
    terr_bad = [p for p in (report.get("terrain") or {}).get("notes") or []
                if p in report["problems"]]
    if terr_bad and settings.terrain_enforce:
        await _audit(sysid, "mission_upload", {"plan_id": body.plan_id},
                     "rejected_terrain", "；".join(terr_bad))
        raise HTTPException(409, {
            "msg": "航線會穿過地面，未上傳", **report,
            "how_to": [
                f"把相對高度拉高——最低那一點還差 "
                f"{-(report['terrain'].get('min_clearance_m') or 0):.1f} m",
                "或改用地形跟隨（frame 10）讓飛控自己跟地面",
                "地形資料在有樹的地方量到的是樹冠：確定是假警報就把 "
                "TERRAIN_ENFORCE 設成 false（那一次會留痕）"]})
    # **這份航線用地形跟隨（frame 10）→ 先問這台機撐不撐得住**
    # （issues/047 §1-A）。危險的不是「有沒有地形資料」，是**沒有的時候
    # 會怎樣**：ArduCopter 兩秒讀不到地形就轉返航，而那次返航把
    # `RTL_ALT_M` 當「離起飛點」在飛。一台 RTL_ALT_M=2 的機在起伏地形上
    # 做地形跟隨，失效處置本身就是撞地。
    #
    # 讀參數只在**這份航線真的有 frame 10** 時才做：那條 57600 的序列埠上
    # 參數回覆的頻寬本來就窄，每次上傳都多問四個值不划算。
    if settings.terrain_enforce and any(
            int(it.get("frame") or 0) == plan_check.TERRAIN_FRAME
            for it in build_items(wps)):
        want = ["TERRAIN_ENABLE", "TERRAIN_SPACING", "RTL_ALT_M", "RNGFND1_TYPE"]
        try:
            got = await _run(sysid, "param_get", mav.job_get_params, want,
                             params={"names": want, "why": "terrain_ready"})
            vals = got["values"]
        except Exception as e:                                  # noqa: BLE001
            # **問不到就不放行。** 這裡不是「沒查到不判對錯」的場合——
            # 失效處置是低空返航，而我方連它會爬到多高都不知道
            raise HTTPException(409, {
                "msg": f"這份航線是地形跟隨，但問不到飛機的地形設定：{e}",
                "how_to": ["稍後再試一次（參數回覆在這條鏈路上本來就容易被丟）",
                           "或改用原本那份非地形跟隨的航線"]}) from e
        ready = plan_check.check_terrain_ready(
            vals, (report.get("terrain") or {}).get("max_rise_m") or 0.0)
        report["warnings"] = list(report.get("warnings") or []) + ready["warnings"]
        # **順便把飛控自己的地形資料問清楚**（使用者裁定 2026-09-07：
        # 「上傳前要跟無人機飛控要資料做檢查」）。只在地形跟隨的航線上做——
        # frame 3 的航線根本不看飛控的地形庫，為它多花 8 次問答不划算，
        # 而且那條序列埠的頻寬是實測過的稀缺資源。要對其他航線做的話，
        # `GET /api/command/{sysid}/terrain?plan_id=…` 隨時可以單獨叫。
        try:
            tc = await terrain_crosscheck(sysid, body.plan_id)
        except HTTPException:
            raise
        except Exception as e:                                  # noqa: BLE001
            report["warnings"].append(f"問不到飛控的地形資料：{e}")
        else:
            report["terrain_fc"] = tc
            report["warnings"] += tc["notes"]
            if tc["pending_total"]:
                await _audit(sysid, "mission_upload",
                             {"plan_id": body.plan_id},
                             "rejected_terrain_pending",
                             f"pending={tc['pending_total']}")
                raise HTTPException(409, {
                    "msg": "飛控缺這一區的地形資料，未上傳",
                    "problems": [
                        f"飛控還缺 {tc['pending_total']} 格地形資料——地形跟隨"
                        "飛到那裡就是在等失效返航（兩秒讀不到就轉返航）"],
                    "how_to": [
                        "用會供圖的地面站（Mission Planner／MAVProxy）連一次，"
                        "讓飛控把這一區的圖磚補齊",
                        "或把圖磚放進飛控 SD 卡的 Terrain 目錄",
                        "或改用原本那份非地形跟隨的航線"]})
        if ready["problems"]:
            await _audit(sysid, "mission_upload", {"plan_id": body.plan_id},
                         "rejected_terrain_ready", "；".join(ready["problems"]))
            raise HTTPException(409, {
                "msg": "這台機的設定撐不住地形跟隨，未上傳",
                "problems": ready["problems"], "warnings": ready["warnings"],
                "how_to": ["先把返航高度（RTL_ALT_M）改到訊息說的值",
                           "或改用原本那份非地形跟隨的航線"]})

    # **低空帶速擋上傳**（使用者裁定 2026-09-08）。與地形穿地共用
    # `TERRAIN_ENFORCE`：兩者都是「這條航線本身會讓飛機撞到東西」，
    # 而圍欄那個開關管的是完全不同的一件事（系統預設範圍對不對得上場地）。
    low = [p for p in report["problems"] if "地面會擾動這架飛機" in p]
    if low and settings.terrain_enforce:
        await _audit(sysid, "mission_upload", {"plan_id": body.plan_id},
                     "rejected_low_fast", "；".join(low))
        raise HTTPException(409, {
            "msg": "低空又要跑快，未上傳", "problems": low,
            "how_to": ["把那一段抬到門檻以上",
                       "或把速度降下來——**第一段用的是機上的 WP_SPD**，"
                       "航線裡的 DO_CHANGE_SPEED 管不到它",
                       "確定這個組合安全就把 TERRAIN_ENFORCE 設成 false"]})
    if not report["ok"] and settings.geofence_enforce:
        await _audit(sysid, "mission_upload", {"plan_id": body.plan_id},
                     "rejected_precheck", "；".join(report["problems"]))
        raise HTTPException(409, {"msg": "任務未通過幾何預檢，未上傳", **report})
    if not report["ok"]:
        log.warning("預檢有問題但未啟用擋門，照常上傳：%s", "；".join(report["problems"]))
    res = await _run(sysid, "mission_upload", mav.job_upload_mission,
                     build_items(wps),
                     params={"plan_id": body.plan_id, "items": len(wps)})
    # issue 020：記「這台機當前飛的任務」——backend create_session 據此綁架次。
    # sysid→drone 靠 drones.mav_sysid（backend 心跳時寫入）。
    await pool.execute("UPDATE drones SET current_plan_id = $1, plan_cleared_at = NULL "
                       "WHERE mav_sysid = $2", body.plan_id, sysid)
    # **上傳成功 → 即時畫面就該畫這一份**：從這一刻起機上的航線就是它，
    # 畫面上還畫別份（或什麼都不畫）就是與飛機的事實對不上
    await guard_client.show_on_live(sysid, body.plan_id, "已上傳到機上")
    return {**res, "check": report}


# ── 群組任務指令層（issue 013-B；doc/group-missions-design.md §7）────────
# 資料層（建群組/預檢/材料化）在 backend :38000 的 /api/groups；這裡是指令層：
# 兩階段執行＋全撤＋群組 RTL。逐台能力 gate 在 executor 內（嚴格 gate＝唯一真相）。
@app.post("/api/command/group/{group_id}/execute", status_code=202)
async def group_execute(group_id: str):
    """兩階段提交（§3）。**非同步啟動**：嚴格 gate 通過→立即回 202＋群組 handle，
    背景序列跑逐台 upload→arm→start，即時態逐步寫 DB（前端輪詢 backend GET）。
    gate 失敗→同步 409＋逐台原因（未啟動序列）。中止只能透過 abort，不是斷 HTTP。"""
    _require_enabled()
    r = await executor.execute(group_id)
    if r.get("error") == "not_found":
        raise HTTPException(404, "無此群組")
    if r.get("error") == "bad_status":
        raise HTTPException(409, {"msg": f"群組狀態為 {r['status']}，非可執行狀態", **r})
    if r.get("rejected"):
        raise HTTPException(409, {"msg": "嚴格 gate 未通過，未啟動序列", **r})
    return r


@app.post("/api/command/group/{group_id}/abort")
async def group_abort(group_id: str):
    """操作員主動全撤（緊急全撤鈕）。冪等、依當前 phase 自動選動作：
    起飛前→disarm 已解鎖者；已起飛→RTL。與序列偵測失敗自動全撤同終態。"""
    _require_enabled()
    r = await executor.abort(group_id)
    if r.get("error") == "not_found":
        raise HTTPException(404, "無此群組")
    return r


@app.post("/api/command/group/{group_id}/rtl")
async def group_rtl(group_id: str):
    """群組 RTL-all（空中緊急）。冪等。"""
    _require_enabled()
    r = await executor.rtl(group_id)
    if r.get("error") == "not_found":
        raise HTTPException(404, "無此群組")
    return r


# ── 外部觸發 API（feat/command-external-trigger 併入）──────────────────
# 讓外部系統只打 command 服務就能「取航線→預檢→一鍵起飛」，不用自己講 MAVLink。
# 設計原則保留：POST /api/start 的介面不變，但**內部委派現版 mission/fly**（上傳回讀→
# arm→起飛→到高度才切 AUTO.MISSION），取代舊分支「地面直接 MISSION_START」（真機會
# 失敗、後來 main 的教訓）——對外一模一樣、行為升級成真機驗過可飛，且純加法不動現有端點。
FIDELITY_KEYS = ("command", "frame", "p1", "p2", "p3", "p4")
STALE_S = 5.0                # 心跳超過此秒數視為斷線，不對它下指令


def _unpack(rows: list) -> list[dict]:
    """DB waypoint 列 → wps（保真欄位從 params JSONB 攤平到頂層，供預檢/顯示）。"""
    wps = []
    for r in rows:
        w = dict(r)
        p = w.get("params")
        p = json.loads(p) if isinstance(p, str) else (p or {})
        w |= {k: p.get(k) for k in FIDELITY_KEYS}
        wps.append(w)
    return wps


async def _resolve_mission(ref: str) -> dict:
    """任務 id 或名稱 → {id, name, waypoints}。同名多筆取最新建立的那筆。"""
    try:
        uuid.UUID(ref)
        is_id = True
    except ValueError:
        is_id = False
    if is_id:
        row = await pool.fetchrow("SELECT id, name FROM plans WHERE id = $1", ref)
        same = 1
    else:
        rows = await pool.fetch(
            "SELECT id, name FROM plans WHERE name = $1 ORDER BY created_at DESC", ref)
        row, same = (rows[0] if rows else None), len(rows)
    if row is None:
        raise HTTPException(404, f"任務庫找不到「{ref}」")
    wp_rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE plan_id = $1 ORDER BY seq", row["id"])
    if not wp_rows:
        raise HTTPException(404, f"任務「{row['name']}」沒有航點")
    return {"id": str(row["id"]), "name": row["name"], "same_name_count": same,
            "waypoints": _unpack(wp_rows)}


def _check(wps: list[dict]) -> dict:
    return plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, dem=terrain.shared())


def _resolve_sysid(sysid: int | None) -> int:
    """指定就用指定的；沒指定且只有一台在線就用那台。多台不猜（猜錯＝錯的機起飛）。"""
    seen = router.snapshot()
    fresh = sorted(int(k) for k, v in seen.items() if v["age_s"] <= STALE_S)
    if sysid is not None:
        d = seen.get(str(sysid))
        if d is None or d["age_s"] > STALE_S:
            raise HTTPException(409, f"sysid {sysid} 未連線／心跳逾時"
                                     f"｜目前在線：{fresh or '無'}")
        return sysid
    if not fresh:
        raise HTTPException(503, "沒有任何機在線（查 :38001/healthz）")
    if len(fresh) > 1:
        raise HTTPException(409, f"連線中有多台 {fresh}，請在 payload 指定 sysid")
    return fresh[0]


def _same_waypoints(old: list, wps: list[dict]) -> bool:
    if len(old) != len(wps):
        return False
    for o, w in zip(old, wps):
        p = o["params"]
        p = json.loads(p) if isinstance(p, str) else (p or {})
        if (p.get("command") != w.get("command")
                or abs((o["lat"] or 0.0) - (w["lat"] or 0.0)) > 1e-7
                or abs((o["lon"] or 0.0) - (w["lon"] or 0.0)) > 1e-7
                or abs((o["alt"] or 0.0) - (w["alt"] or 0.0)) > 0.1):
            return False
    return True


async def _store_plan(name: str, wps: list[dict],
                      firmware_type=None, vehicle_type=None) -> str:
    """.plan 航線入庫回 plan_id；內容相同就重用（外部反覆觸發不洗版任務庫）。"""
    rows = await pool.fetch(
        "SELECT id FROM plans WHERE name = $1 AND created_by = 'plan-file' "
        "ORDER BY created_at DESC LIMIT 10", name)
    for r in rows:
        old = await pool.fetch(
            "SELECT lat, lon, alt, params FROM waypoints WHERE plan_id = $1 ORDER BY seq",
            r["id"])
        if _same_waypoints(old, wps):
            return str(r["id"])
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "INSERT INTO plans (name, created_by, kind, firmware_type, "
                "vehicle_type) VALUES ($1, 'plan-file', 'imported', $2, $3) "
                "RETURNING id",
                name, firmware_type, vehicle_type)
            await con.executemany(
                "INSERT INTO waypoints (plan_id, seq, lat, lon, alt, action, params) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                [(row["id"], w["seq"], w["lat"], w["lon"], w.get("alt"),
                  w.get("action", "waypoint"),
                  json.dumps({k: w[k] for k in FIDELITY_KEYS if w.get(k) is not None})
                  if w.get("command") is not None else None)
                 for w in wps])
    return str(row["id"])


@app.get("/api/ext/drones", tags=["任務"], summary="外部：現在有哪些無人機、可不可以指揮")
async def ext_drones():
    """**對外**的機隊清單：一次回答「有哪些機、哪些現在指得動」。

    外部系統原本要打兩支（本服務的 `/healthz` 拿在線清單、backend 的
    `/api/admission/{sysid}` 逐台問可不可以指揮），而那兩支都是**內部端點**
    ——它們回的模式編號、板號、能力四態是給我方 UI 與排查用的，形狀也會
    隨內部演進而變。這一支只回外部真正要用的四件事，並且把兩個問題合成一次
    往返（見 `doc/external-api-v2.html#ext-drones`）。

    `controllable` 為 true 才可以下指令；為 false 時 `reason` 一定說得出
    是什麼擋住了（沿用 `admission.why_blocked`，與指令被擋時的說法同一份）。
    """
    snap = router.snapshot() if router is not None else {}
    out = []
    for key in sorted(snap, key=int):
        sysid, d = int(key), snap[key]
        online = d["age_s"] <= STALE_S
        info = await admission.state_of(sysid)
        ok = online and info.get("state") in admission.COMMANDABLE
        row = {"sysid": sysid, "name": info.get("drone"),
               "online": online, "age_s": d["age_s"], "armed": d.get("armed"),
               "controllable": bool(ok and settings.enable_commands)}
        if not row["controllable"]:
            # **擋下的理由要說得出下一步**：三種擋法各自的話不一樣，
            # 混成一句「不可用」等於什麼都沒說
            row["reason"] = ("這台地面站目前只觀察不指揮（ENABLE_COMMANDS=false）"
                             if not settings.enable_commands else
                             f"sysid {sysid} 心跳已 {d['age_s']:.0f} 秒沒更新——視為斷線"
                             if not online else admission.why_blocked(info))
        out.append(row)
    return {"drones": out}


@app.get("/api/missions", tags=["任務"], summary="① 選任務：列出任務庫")
async def ext_list_missions():
    """任務庫總表。**唯讀、不吃 `ENABLE_COMMANDS`**——只是看有哪些航線，
    不動飛機，所以指令能力關著也查得到。

    回傳的 `id` 與 `name` 都可以餵給下一步（上傳）與 `/api/start`。
    `waypoint_count` 含無座標項（RTL 之類），`nav_count` 只算導航航點。
    """
    rows = await pool.fetch("""
        SELECT m.id::text AS id, m.name, m.created_by AS source, m.created_at,
               m.is_active, count(w.seq) AS waypoint_count,
               count(*) FILTER (WHERE w.lat <> 0 OR w.lon <> 0) AS nav_count
        FROM plans m LEFT JOIN waypoints w ON w.plan_id = m.id
        GROUP BY m.id ORDER BY m.created_at DESC""")
    return {"source": "db", "missions": [dict(r) for r in rows]}


@app.get("/api/missions/{ref}")
async def ext_get_mission(ref: str):
    """外部：單一航線內容（保真航點）＋幾何預檢。ref＝id 或名稱。"""
    m = await _resolve_mission(ref)
    return {**m, "waypoint_count": len(m["waypoints"]), "check": _check(m["waypoints"])}


@app.get("/api/plans")
async def ext_list_plans():
    """外部：missions/ 目錄的 .plan 檔一覽（含解析失敗的，帶 error）。"""
    return {"source": "file", "dir": settings.missions_dir,
            "plans": plans.scan(settings.missions_dir)}


@app.get("/api/plans/{name}")
async def ext_get_plan(name: str, raw: bool = False):
    """外部：單一 .plan——預設回解析航點＋預檢；raw=true 回 QGC 原始 JSON。"""
    try:
        path = plans.resolve(settings.missions_dir, name)
        if raw:
            return plans.raw(path)
        d = plans.detail(path)
    except plans.PlanError as e:
        raise HTTPException(404, str(e))
    return {**d, "check": _check(d["waypoints"])}


class StartIn(BaseModel):
    mission: str | None = None       # 任務庫 id 或名稱（主要來源）
    plan: str | None = None          # 次要：missions/ 下的 .plan 檔名
    sysid: int | None = None         # 省略＝唯一在線的那台；多台必填
    store: bool = True               # 保留相容；plan 路徑一律入庫（現版經 mission_fly 需 DB mission，去重不洗版）
    takeoff_alt: float | None = None  # 省略＝跟著任務的 NAV_TAKEOFF（見 FlyIn.takeoff_alt）


@app.post("/api/start", tags=["一鍵"], summary="一鍵：上傳→解鎖→起飛→切任務")
async def start(body: StartIn):
    """**一鍵起飛**：取航線（任務庫 id/名稱 或 .plan 檔）→ 幾何預檢 → 委派 mission/fly
    （上傳回讀→arm→起飛→到高度→AUTO.MISSION）。航線來源二選一：
      {"mission": "<id 或名稱>"}   任務庫（主要）
      {"plan": "xxx.plan"}         missions/ 目錄（會先入庫再飛）
    失敗帶 mission/fly 的 step 與 PX4 原因。成功回 {source, plan_id, name, sysid, ok, steps}。"""
    _require_enabled()
    if bool(body.mission) == bool(body.plan):
        raise HTTPException(422, "mission 與 plan 二選一（mission＝任務庫，plan＝.plan 檔）")
    if body.mission:
        m = await _resolve_mission(body.mission)
        plan_id, name, src, skipped = m["id"], m["name"], "db", []
        if m["same_name_count"] > 1:
            log.warning("任務名稱「%s」有 %d 筆同名，取最新的 %s",
                        name, m["same_name_count"], plan_id)
    else:
        try:
            path = plans.resolve(settings.missions_dir, body.plan)
            parsed = plans.parse(path)
        except plans.PlanError as e:
            await _audit(None, "start", {"plan": body.plan}, "failed", str(e))
            raise HTTPException(404, {"step": "plan", "msg": str(e)})
        plan_id = await _store_plan(path.stem, parsed["waypoints"],
                                       parsed.get("firmware_type"),
                                       parsed.get("vehicle_type"))
        name, src, skipped = path.name, "file", parsed["skipped"]
    sysid = _resolve_sysid(body.sysid)
    # 委派現版正確流程（capability gate＋到高度 gating＋逐台 audit＋X-Client 都自動繼承）
    result = await mission_fly(sysid, FlyIn(plan_id=plan_id, takeoff_alt=body.takeoff_alt))
    return {"source": src, "plan_id": plan_id, "name": name, "sysid": sysid,
            "skipped": skipped, **result}
