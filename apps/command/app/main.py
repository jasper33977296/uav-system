"""command 服務 API：自製 GCS 的指令能力（GCS 取代計畫階段 2）。

與 backend（ingest，唯讀）完全分離的獨立服務：自己的 MAVLink 連線
（單埠多機）、指令佇列＋ACK、任務上傳＋回讀比對、指令留痕入庫。
`ENABLE_COMMANDS` 預設關——關閉時指令端點回 403、不發 GCS 心跳。

端點（sysid 定址；多機模型見 issues/011）：
  GET  /healthz                              服務與各機連線狀態
  POST /api/command/{sysid}/arm | /disarm
  POST /api/command/{sysid}/mode/{mission|hold|rtl|land}
  POST /api/command/{sysid}/mission/start
  POST /api/command/{sysid}/mission/upload   body: {"mission_id": "..."}
"""
import asyncio
import contextvars
import json
import logging
import math
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

from . import change_route, group_exec, mav, plan_check, plans
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


async def _audit(sysid: int, action: str, params, result: str, detail: str = ""):
    await pool.execute(
        "INSERT INTO command_log (sysid, action, params, result, detail, client) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        sysid, action, json.dumps(params, default=str), result, detail[:500],
        _client_var.get())


def _require_enabled():
    if not settings.enable_commands:
        raise HTTPException(403, "指令能力未啟用（ENABLE_COMMANDS=false，預設關閉）"
                                 "——這是刻意的安全 gate，部署時顯式開啟")


# 能力 gating（issue 015）：capabilities 是伺服器端唯一真相，非 "ok" 的能力
# 一律拒發——UI 與實際放行永不背離。取代舊 _require_px4 硬碼（ardupilot 現在走
# unverified＝全鎖，比舊版嚴、符合四態「僅觀察」）。可攜指令在某機型 SITL 驗過
# 後把該鍵開 "ok"，前後端同時放行。
def _require_capability(sysid: int, endpoint_key: str):
    if sysid not in router.drones:
        raise HTTPException(409, f"sysid {sysid} 未連線（心跳未見）")
    ap = mav.caps.autopilot_name(router.autopilot_of(sysid))
    cap_key = mav.caps.ENDPOINT_CAP.get(endpoint_key, endpoint_key)
    cap, reasons = mav.caps.capabilities_for(ap, (router.drones.get(sysid) or {}))
    state = cap.get(cap_key, "unsupported")
    if state != "ok":
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
    except Exception as e:
        await _audit(sysid, action, params, "error", repr(e))
        raise HTTPException(500, f"內部錯誤：{e}")
    ok = res.get("accepted", True) and res.get("verified", True)
    await _audit(sysid, action, params, "accepted" if ok else "rejected", json.dumps(res))
    if not ok:
        # 結構化拒絕：result code＋操作指引＋PX4 的解釋文字（實戰教訓：
        # 只給 code 操作員無從排查——"Arming denied: ..." 那行才是答案）
        raise HTTPException(409, {
            "msg": f"機端拒絕（{res.get('result')}）",
            "hint": res.get("hint", ""),
            "autopilot_notes": res.get("autopilot_notes", []),
        })
    return res


async def lifespan(app):
    global router, pool, executor
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    await pool.execute("""CREATE TABLE IF NOT EXISTS command_log (
        id BIGSERIAL PRIMARY KEY, time TIMESTAMPTZ NOT NULL DEFAULT now(),
        sysid INT, action TEXT NOT NULL, params JSONB,
        result TEXT NOT NULL, detail TEXT)""")
    # 指令來源歸因（issue 013-B）：X-Client header 落痕，既有表補欄
    await pool.execute("ALTER TABLE command_log ADD COLUMN IF NOT EXISTS client TEXT")
    # 單埠多機的身分對應欄位（issues/011；backend migrate 也建，這裡防序）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS mav_sysid INT")
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS current_mission_id UUID")
    # 群組執行期即時態欄位（issue 013-B；backend migrate 也建，這裡防序）
    await pool.execute("ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS error JSONB")
    await pool.execute(
        "ALTER TABLE group_assignments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
    router = mav.MavRouter(settings.command_mavlink_url,
                           heartbeat=settings.enable_commands)
    router.start()
    executor = group_exec.GroupExecutor(router, pool, build_items, _audit)
    log.info("command 服務啟動：%s（enable_commands=%s，sysid=%d）",
             settings.command_mavlink_url, settings.enable_commands, mav.GCS_SYSID)
    yield
    await pool.close()


app = FastAPI(title="UAV Command Service", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _capture_client(request, call_next):
    """把 X-Client header 塞進 contextvar，供 _audit 歸因（背景 task 沿用此 context）。"""
    _client_var.set(request.headers.get("x-client"))
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    """服務與各機連線狀態。**`ok` 講的是「還能不能指揮飛機」**，不是
    「HTTP 層還活著」——issue 034：2026-08-11 router 執行緒被網路瞬斷殺死後，
    心跳停發、指令全逾時，而這裡照回 `{"ok": true}`，於是沒有任何人與腳本
    看得出異常，拖了近一小時才發現。失效不得冒充合法狀態（ui-spec §0.2e）。
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
            f"{mav.STALL_S:.0f} 秒）：GCS 心跳已停發、指令不會送達飛機。"
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


@app.post("/api/command/{sysid}/arm")
async def arm(sysid: int, intent: str | None = None):
    _require_enabled(); _require_capability(sysid, "arm")
    if intent != "start_mission":
        _guard_bare_arm(sysid)
    else:
        # 顯式意圖仍然留痕——**繞過安全檢查這件事本身要看得見**
        await _audit(sysid, "arm", {"intent": intent}, "accepted",
                     "帶 intent=start_mission 繞過自動模式 arm 防護（031）")
    return await _run(sysid, "arm", mav.job_command, 400, [1.0])


@app.post("/api/command/{sysid}/disarm")
async def disarm(sysid: int):
    _require_enabled(); _require_capability(sysid, "disarm")
    return await _run(sysid, "disarm", mav.job_command, 400, [0.0])


@app.post("/api/command/{sysid}/mode/{mode}")
async def set_mode(sysid: int, mode: str, skip_guard: bool = False):
    if mode not in mav.PX4_MODES:
        raise HTTPException(422, f"mode 須為 {sorted(mav.PX4_MODES)}")
    _require_enabled(); _require_capability(sysid, f"mode:{mode}")
    # skip_guard 只給**改航線序列內部**用：那條序列整體已經過守門，
    # 序列中的每一步再問一次會被守門用「已經在 hold 了」擋下自己
    if not skip_guard:
        await _ask_guard(sysid, f"mode:{mode}")
    return await _run(sysid, f"mode:{mode}", mav.job_set_mode, mode)


@app.post("/api/command/{sysid}/mission/start")
async def mission_start(sysid: int):
    _require_enabled(); _require_capability(sysid, "mission_start")
    await _ask_guard(sysid, "mission_start")
    return await _run(sysid, "mission_start", mav.job_command, 300, [0.0])


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
    mission_id: str


@app.post("/api/command/{sysid}/takeoff")
async def takeoff(sysid: int, body: TakeoffIn):
    """監督式起飛：解鎖＋爬升到指定高度後自動懸停（PX4 自主執行）。
    取代「用 RC 手動飛到高度」的操作——連續操縱仍是 RC 的職權。"""
    _require_enabled(); _require_capability(sysid, "takeoff")
    return await _do_takeoff(sysid, body.alt)


class FlyIn(BaseModel):
    mission_id: str | None = None      # 給了就先上傳＋回讀比對；不給＝用機上現有任務
    takeoff_alt: float = 10.0
    alt_timeout_s: float = 60.0


@app.post("/api/command/{sysid}/mission/fly")
async def mission_fly(sysid: int, body: FlyIn):
    """起飛→任務自動序列（實戰教訓 2026-08-11：地面直接 MISSION_START
    在實機上會失敗，須先到高度）：

      （上傳＋回讀比對）→ 解鎖 → NAV_TAKEOFF → **等實際到達目標高度**
      → 切 AUTO.MISSION（已在空中，PX4 跳過任務內的 takeoff 項續飛）

    高度沒到就不切任務——序列在任何一步失敗都停在安全狀態
    （PX4 起飛後自動懸停），並回報卡在哪一步。
    """
    _require_enabled(); _require_capability(sysid, "mission_fly")
    await _ask_guard(sysid, "mission_fly")
    steps = {}
    if body.mission_id:
        steps["upload"] = await mission_upload(sysid, UploadIn(mission_id=body.mission_id))
    steps.update(await _do_takeoff(sysid, body.takeoff_alt))

    # 等高度實際到達（80% 即視為到位，PX4 收斂段不必等滿）
    # **必須看這一台的高度**：原本讀 backend `/api/live`，而那個端點只回**主機**。
    # 飛非主機時這個判斷完全與目標機無關——2026-08-12 前端驗收實測，uav-s2 起飛
    # 成功卻回報「未達目標高度（-0.04m）」，那個 -0.04 是停在地面的主機。
    # 反方向更危險：**主機在空中而目標機沒起來時，這個檢查會通過**，於是把一台
    # 還在地面的機切進 AUTO.MISSION——正是本序列存在的理由（2026-08-11 教訓）被
    # 架空。群組執行器早就改用 per-sysid（mav.py GLOBAL_POSITION_INT），單機路徑
    # 漏了同一課，現在同源。
    deadline = asyncio.get_running_loop().time() + body.alt_timeout_s
    alt = None
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        alt = ((router.drones.get(sysid) if router else None) or {}).get("alt_rel")
        if alt is not None and alt >= body.takeoff_alt * 0.8:
            break
    else:
        await _audit(sysid, "mission_fly", body.model_dump(), "failed",
                     f"起飛後 {body.alt_timeout_s:.0f}s 未達目標高度（目前 {alt} m）")
        raise HTTPException(504, {
            "msg": f"起飛後未達目標高度（目前 {alt} m / 目標 {body.takeoff_alt} m）",
            "hint": "機停在懸停狀態，未啟動任務——檢查 RC/遙測後可重試或 RTL",
            "steps": steps})
    steps["alt_reached"] = {"alt_rel": alt}

    steps["mission"] = await _run(sysid, "mode:mission", mav.job_set_mode, "mission")
    await _audit(sysid, "mission_fly", body.model_dump(), "accepted", json.dumps(steps))
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
    _require_enabled(); _require_capability(sysid, "mission_start")
    if body.index < 0:
        raise HTTPException(422, "index 不得為負")
    return await _run(sysid, "mission_goto", mav.job_mission_goto, body.index,
                      params={"index": body.index})


#: 指令服務的動作 → 意圖名（丙案：守門在代理，執行在這裡）
_INTENT_OF = {
    "mode:hold": "pause", "mode:mission": "resume",
    "mission_start": "start_mission", "mission_fly": "start_mission",
    "change_route": "change_route",
}


async def _ask_guard(sysid: int, action: str, intent_id: str | None = None):
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
    body = json.dumps({"action": intent, "intent_id": intent_id,
                       "board_uid": uid,
                       "drone_id": await _drone_id_of(sysid)}).encode()
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


async def _drone_id_of(sysid: int) -> str | None:
    row = await pool.fetchrow(
        "SELECT id::text AS id FROM drones WHERE mav_sysid = $1", sysid)
    return row["id"] if row else None


async def _load_wps(mission_id: str) -> tuple[list[dict], str]:
    rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = $1 ORDER BY seq", mission_id)
    if not rows:
        raise HTTPException(404, "任務不存在或沒有航點")
    name = await pool.fetchval("SELECT name FROM missions WHERE id = $1",
                               mission_id) or mission_id
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
    mission_id: str
    hold_alt: float | None = None      # 不給＝暫停後維持當前高度
    # 執行時把**人看到的那份提案**送回來，用來比對這段時間機體有沒有飄掉
    # （協定 §5.1）。省略＝不做漂移檢查，只有非互動的呼叫端該這樣用
    proposal: dict | None = None


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
    wps, name = await _load_wps(body.mission_id)
    p = change_route.build_proposal(
        wps=wps, cur=_cur_of(sysid), hold_alt=body.hold_alt,
        mission_name=name, mission_id=body.mission_id,
        cur_seq=(router.drones.get(sysid) or {}).get("mission_seq")
        if router else None)
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
    await _ask_guard(sysid, "change_route")
    wps, name = await _load_wps(body.mission_id)
    fresh = change_route.build_proposal(
        wps=wps, cur=_cur_of(sysid), hold_alt=body.hold_alt,
        mission_name=name, mission_id=body.mission_id)
    if not fresh["ok"]:
        raise HTTPException(409, {"msg": "現在算不出可執行的提案",
                                  "proposal": fresh})
    if body.proposal:
        drift = change_route.drift_reason(body.proposal, fresh)
        if drift:
            # **不照過期的提案做。** 人確認的是那一份提案，不是「授權之後
            # 隨便怎麼飛」。回新的提案要人重看，而不是自作主張用新的執行
            await _audit(sysid, "change_route", body.model_dump(),
                         "rejected_drift", drift)
            raise HTTPException(409, {
                "msg": f"提案已經過期：{drift}。請重看新的提案再決定",
                "code": "proposal_drift", "proposal": fresh})
    steps: dict = {}

    async def step(key, coro, note):
        try:
            steps[key] = await coro
        except HTTPException as e:
            steps[key] = {"ok": False, "detail": e.detail}
            await _audit(sysid, "change_route", body.model_dump(),
                         "failed", f"{note}：{e.detail}")
            raise HTTPException(409, {
                "msg": f"改航線序列在「{note}」這一步停下，機體停在安全狀態",
                "steps": steps, "proposal": fresh})

    # 1. 暫停 —— job_set_mode 內含讀回確認（mode_engaged），不是只看 ACK
    await step("hold", set_mode(sysid, "hold", skip_guard=True), "切 hold 懸停")
    # 2. 上傳 —— 機體已在 hold，守門會放行（只擋 mission 模式）
    await step("upload", mission_upload(sysid, UploadIn(mission_id=body.mission_id)),
               "上傳新航線")
    # 3. 續飛 —— 先指定航點再切 mission。**順序不能反**：先切 mission 的話
    #    機體會用舊索引開始飛，而那個索引在新航線上毫無意義
    await step("goto", mission_goto(sysid, GotoIn(index=fresh["resume_wp"]["index"])),
               "指定續飛航點")
    await step("resume", set_mode(sysid, "mission", skip_guard=True),
               "切回 mission")

    await _audit(sysid, "change_route", body.model_dump(), "ok",
                 f"續飛第 {fresh['resume_wp']['index']} 點")
    return {"ok": True, "steps": steps, "proposal": fresh}


@app.post("/api/command/{sysid}/mission/upload")
async def mission_upload(sysid: int, body: UploadIn):
    _require_enabled(); _require_capability(sysid, "mission_upload")
    rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = $1 ORDER BY seq", body.mission_id)
    if not rows:
        raise HTTPException(404, "任務不存在或沒有航點")
    meta = await pool.fetchrow(
        "SELECT firmware_type, vehicle_type FROM missions WHERE id = $1",
        body.mission_id)
    wps = []
    for r in rows:
        w = dict(r)
        p = w.get("params")
        p = json.loads(p) if isinstance(p, str) else (p or {})
        w["command"] = p.get("command")      # plan_check 用原始 command 判導航類
        wps.append(w)
    # 幾何預檢：報告一律附在回應與留痕；GEOFENCE_ENFORCE=true 才擋
    # （預設不擋——2026-08-10 使用者決定；空中防線是 PX4 自己的 Geofence）
    report = plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin)
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
        await _audit(sysid, "mission_upload", {"mission_id": body.mission_id},
                     "rejected_inflight", blocked)
        log.warning("擋下空中上傳（sysid %d）：%s", sysid, blocked)
        raise HTTPException(409, {"msg": blocked, "code": "inflight_upload",
                                  "how_to": ["切 hold 並確認進入",
                                             "上傳新航線",
                                             "切回 mission 並指定續飛航點"]})
    if not report["ok"] and settings.geofence_enforce:
        await _audit(sysid, "mission_upload", {"mission_id": body.mission_id},
                     "rejected_precheck", "；".join(report["problems"]))
        raise HTTPException(409, {"msg": "任務未通過幾何預檢，未上傳", **report})
    if not report["ok"]:
        log.warning("預檢有問題但未啟用擋門，照常上傳：%s", "；".join(report["problems"]))
    res = await _run(sysid, "mission_upload", mav.job_upload_mission,
                     build_items(wps),
                     params={"mission_id": body.mission_id, "items": len(wps)})
    # issue 020：記「這台機當前飛的任務」——backend create_session 據此綁架次。
    # sysid→drone 靠 drones.mav_sysid（backend 心跳時寫入）。
    await pool.execute("UPDATE drones SET current_mission_id = $1 WHERE mav_sysid = $2",
                       body.mission_id, sysid)
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
        row = await pool.fetchrow("SELECT id, name FROM missions WHERE id = $1", ref)
        same = 1
    else:
        rows = await pool.fetch(
            "SELECT id, name FROM missions WHERE name = $1 ORDER BY created_at DESC", ref)
        row, same = (rows[0] if rows else None), len(rows)
    if row is None:
        raise HTTPException(404, f"任務庫找不到「{ref}」")
    wp_rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = $1 ORDER BY seq", row["id"])
    if not wp_rows:
        raise HTTPException(404, f"任務「{row['name']}」沒有航點")
    return {"id": str(row["id"]), "name": row["name"], "same_name_count": same,
            "waypoints": _unpack(wp_rows)}


def _check(wps: list[dict]) -> dict:
    return plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m, settings.geofence_margin)


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
    """.plan 航線入庫回 mission_id；內容相同就重用（外部反覆觸發不洗版任務庫）。"""
    rows = await pool.fetch(
        "SELECT id FROM missions WHERE name = $1 AND created_by = 'plan-file' "
        "ORDER BY created_at DESC LIMIT 10", name)
    for r in rows:
        old = await pool.fetch(
            "SELECT lat, lon, alt, params FROM waypoints WHERE mission_id = $1 ORDER BY seq",
            r["id"])
        if _same_waypoints(old, wps):
            return str(r["id"])
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "INSERT INTO missions (name, created_by, kind, firmware_type, "
                "vehicle_type) VALUES ($1, 'plan-file', 'imported', $2, $3) "
                "RETURNING id",
                name, firmware_type, vehicle_type)
            await con.executemany(
                "INSERT INTO waypoints (mission_id, seq, lat, lon, alt, action, params) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                [(row["id"], w["seq"], w["lat"], w["lon"], w.get("alt"),
                  w.get("action", "waypoint"),
                  json.dumps({k: w[k] for k in FIDELITY_KEYS if w.get(k) is not None})
                  if w.get("command") is not None else None)
                 for w in wps])
    return str(row["id"])


@app.get("/api/missions")
async def ext_list_missions():
    """外部：任務庫總表（唯讀、不吃 ENABLE_COMMANDS）。name/id 都可餵給 /api/start。"""
    rows = await pool.fetch("""
        SELECT m.id::text AS id, m.name, m.created_by AS source, m.created_at,
               m.is_active, count(w.seq) AS waypoint_count,
               count(*) FILTER (WHERE w.lat <> 0 OR w.lon <> 0) AS nav_count
        FROM missions m LEFT JOIN waypoints w ON w.mission_id = m.id
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
    takeoff_alt: float = 10.0        # 起飛相對高度（現版先起飛才切任務）


@app.post("/api/start")
async def start(body: StartIn):
    """**一鍵起飛**：取航線（任務庫 id/名稱 或 .plan 檔）→ 幾何預檢 → 委派 mission/fly
    （上傳回讀→arm→起飛→到高度→AUTO.MISSION）。航線來源二選一：
      {"mission": "<id 或名稱>"}   任務庫（主要）
      {"plan": "xxx.plan"}         missions/ 目錄（會先入庫再飛）
    失敗帶 mission/fly 的 step 與 PX4 原因。成功回 {source, mission_id, name, sysid, ok, steps}。"""
    _require_enabled()
    if bool(body.mission) == bool(body.plan):
        raise HTTPException(422, "mission 與 plan 二選一（mission＝任務庫，plan＝.plan 檔）")
    if body.mission:
        m = await _resolve_mission(body.mission)
        mission_id, name, src, skipped = m["id"], m["name"], "db", []
        if m["same_name_count"] > 1:
            log.warning("任務名稱「%s」有 %d 筆同名，取最新的 %s",
                        name, m["same_name_count"], mission_id)
    else:
        try:
            path = plans.resolve(settings.missions_dir, body.plan)
            parsed = plans.parse(path)
        except plans.PlanError as e:
            await _audit(None, "start", {"plan": body.plan}, "failed", str(e))
            raise HTTPException(404, {"step": "plan", "msg": str(e)})
        mission_id = await _store_plan(path.stem, parsed["waypoints"],
                                       parsed.get("firmware_type"),
                                       parsed.get("vehicle_type"))
        name, src, skipped = path.name, "file", parsed["skipped"]
    sysid = _resolve_sysid(body.sysid)
    # 委派現版正確流程（capability gate＋到高度 gating＋逐台 audit＋X-Client 都自動繼承）
    result = await mission_fly(sysid, FlyIn(mission_id=mission_id, takeoff_alt=body.takeoff_alt))
    return {"source": src, "mission_id": mission_id, "name": name, "sysid": sysid,
            "skipped": skipped, **result}
