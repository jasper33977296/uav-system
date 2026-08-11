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

外部觸發（單一呼叫跑完整個起飛流程，不必自己串上面那串）：
  GET  /api/missions                         總表：任務庫所有航線
  GET  /api/missions/{id|name}               內容：單一航線的航點＋幾何預檢
  POST /api/start                            body: {"mission": "<id|名稱>", "sysid": 1?}
  GET  /api/plans | /api/plans/{name}        次要來源：missions/ 目錄的 .plan 檔
"""
import asyncio
import functools
import json
import logging
import time
import uuid
import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import mav, plan_check, plans
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("command")

router: mav.MavRouter | None = None
pool: asyncpg.Pool | None = None

# waypoints.action → MAV_CMD（舊資料沒存原始 command 時的推回）
ACTION_CMD = {"takeoff": 22, "waypoint": 16, "land": 21, "rtl": 20}


FIDELITY_KEYS = ("command", "frame", "p1", "p2", "p3", "p4")


def build_items(wps: list[dict]) -> list[dict]:
    """MAVLink 保真度（對齊實戰工具 upload_mission.py）：新資料帶 .plan 的
    原始 command/frame/p1–p4，原樣送出；舊資料由 action 推回、frame 3
    （GLOBAL_RELATIVE_ALT，QGC 預設）、params 補 0。

    保真欄位有兩種形狀都要吃：DB 來的包在 `params` JSONB 裡，.plan 解析出來的
    是平鋪的。只認 params 會讓 .plan 的 RTL（frame 2）被當成 frame 3 送出，
    PX4 直接回 MAV_MISSION_UNSUPPORTED。
    """
    items = []
    for i, w in enumerate(wps):
        p = w.get("params")
        p = json.loads(p) if isinstance(p, str) else (p or {})
        p = {**p, **{k: w[k] for k in FIDELITY_KEYS if w.get(k) is not None}}
        cmd = p.get("command") or ACTION_CMD.get((w.get("action") or "waypoint"), 16)
        items.append({
            "seq": i,
            "frame": p.get("frame", 3),
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
        "INSERT INTO command_log (sysid, action, params, result, detail) "
        "VALUES ($1, $2, $3, $4, $5)",
        sysid, action, json.dumps(params, default=str), result, detail[:500])


def _require_enabled():
    if not settings.enable_commands:
        raise HTTPException(403, "指令能力未啟用（ENABLE_COMMANDS=false，預設關閉）"
                                 "——這是刻意的安全 gate，部署時顯式開啟")


async def _run(sysid: int, action: str, fn, *args, params=None, timeout: float = 30.0):
    """執行 MAV 工作＋留痕。失敗一樣留痕——指令史是實驗記錄的一部分。

    `timeout` 是等 router 執行緒回覆的上限。指令類 30 秒綽綽有餘；任務上傳
    要留大一點——光是握手期限就 30 秒，後面還有逐項回讀（每項最多重試 2 次
    × 3 秒），長航線很容易超過預設值而在協定還在跑的時候就被判逾時。
    """
    _require_enabled()
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(
            None, functools.partial(router.submit, fn, sysid, *args, timeout=timeout))
    except mav.CommandError as e:
        await _audit(sysid, action, params, "failed", str(e))
        raise HTTPException(502, str(e))
    except TimeoutError:                      # router 執行緒沒在期限內回覆
        await _audit(sysid, action, params, "timeout", f"等 router 回覆逾時 {timeout}s")
        raise HTTPException(504, f"指令逾時（{timeout} 秒內未完成）——機端或鏈路無回應")
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
            "px4_notes": res.get("px4_notes", []),
        })
    return res


async def lifespan(app):
    global router, pool
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    await pool.execute("""CREATE TABLE IF NOT EXISTS command_log (
        id BIGSERIAL PRIMARY KEY, time TIMESTAMPTZ NOT NULL DEFAULT now(),
        sysid INT, action TEXT NOT NULL, params JSONB,
        result TEXT NOT NULL, detail TEXT)""")
    # 單埠多機的身分對應欄位（issues/011；先建欄位，UI 對應後續接）
    await pool.execute("ALTER TABLE drones ADD COLUMN IF NOT EXISTS mav_sysid INT")
    router = mav.MavRouter(settings.command_mavlink_url,
                           heartbeat=settings.enable_commands)
    router.start()
    log.info("command 服務啟動：%s（enable_commands=%s，sysid=%d）",
             settings.command_mavlink_url, settings.enable_commands, mav.GCS_SYSID)
    yield
    await pool.close()


app = FastAPI(title="UAV Command Service", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/healthz")
async def healthz():
    """`ok` 反映 **router 執行緒是否活著**，不只是 HTTP 有沒有回應。

    這條迴圈是與飛機唯一的收發者，它死了服務就是殭屍：socket 沒人讀、心跳
    停發（PX4 會依 `COM_DL_LOSS_T` 觸發 failsafe）、指令全部等到逾時，但
    HTTP 層一切正常。2026-08-11 實際發生過，只回 ok:True 讓它整整一小時
    沒被發現——健康檢查必須看得到這件事。
    """
    alive = router is not None and router.alive()
    return {"ok": alive, "router_alive": alive, "enabled": settings.enable_commands,
            "gcs_sysid": mav.GCS_SYSID, "drones": router.snapshot() if router else {}}


@app.post("/api/command/{sysid}/arm")
async def arm(sysid: int):
    return await _run(sysid, "arm", mav.job_command, 400, [1.0])


@app.post("/api/command/{sysid}/disarm")
async def disarm(sysid: int):
    return await _run(sysid, "disarm", mav.job_command, 400, [0.0])


@app.post("/api/command/{sysid}/mode/{mode}")
async def set_mode(sysid: int, mode: str):
    if mode not in mav.PX4_MODES:
        raise HTTPException(422, f"mode 須為 {sorted(mav.PX4_MODES)}")
    return await _run(sysid, f"mode:{mode}", mav.job_set_mode, mode)


@app.post("/api/command/{sysid}/mission/start")
async def mission_start(sysid: int):
    return await _run(sysid, "mission_start", mav.job_command, 300, [0.0])


class UploadIn(BaseModel):
    mission_id: str


@app.post("/api/command/{sysid}/mission/upload")
async def mission_upload(sysid: int, body: UploadIn):
    _require_enabled()
    rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = $1 ORDER BY seq", body.mission_id)
    if not rows:
        raise HTTPException(404, "任務不存在或沒有航點")
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
    if not report["ok"] and settings.geofence_enforce:
        await _audit(sysid, "mission_upload", {"mission_id": body.mission_id},
                     "rejected_precheck", "；".join(report["problems"]))
        raise HTTPException(409, {"msg": "任務未通過幾何預檢，未上傳", **report})
    if not report["ok"]:
        log.warning("預檢有問題但未啟用擋門，照常上傳：%s", "；".join(report["problems"]))
    res = await _run(sysid, "mission_upload", mav.job_upload_mission,
                     build_items(wps),
                     params={"mission_id": body.mission_id, "items": len(wps)},
                     timeout=UPLOAD_TIMEOUT_S)
    return {**res, "check": report}


# ── 外部觸發：航線總表／航線內容／一鍵起飛 ────────────────────────────────
# 上面那串端點是「一個動作一支 API」，給 UI 與人工排查用。外部系統要的是
# 一次呼叫跑完整個流程，所以另外包一層。
#
# 航線來源以**任務庫（DB）為主**（2026-08-11 使用者決定）：總表與內容都讀
# missions/waypoints 表，與前端路徑管理頁看到的是同一份。`missions/` 目錄的
# .plan 檔是次要來源——/api/start 仍可直接吃檔名（會先匯入任務庫再飛），
# 檔案清單見 /api/plans。
#
# 安全性未加碼（2026-08-11 使用者決定不驗證身分）：擋門仍只有
# `ENABLE_COMMANDS` 與網路隔離。**任何連得到本埠的人都能讓飛機起飛**，
# 部署時務必確認 :38001 不對外曝露。

UPLOAD_TIMEOUT_S = 180.0     # 握手 30s + 逐項回讀重試，長航線要留餘裕
STALE_S = 5.0                # 心跳超過這個秒數視為斷線，不對它下指令


def _unpack(rows: list) -> list[dict]:
    """waypoints 資料列 → 帶平鋪保真欄位的航點（plan_check 與 build_items 都吃這個）。"""
    wps = []
    for r in rows:
        w = dict(r)
        p = w.get("params")
        p = json.loads(p) if isinstance(p, str) else (p or {})
        w |= {k: p.get(k) for k in FIDELITY_KEYS}
        wps.append(w)
    return wps


async def _resolve_mission(ref: str) -> dict:
    """任務 id 或名稱 → {id, name, waypoints}。

    同名多筆時取**最新建立**的那筆（任務庫允許同名，測試腳本重跑就會產生
    一堆同名任務）；回應一律帶解析出來的 id，外部看得到自己拿到的是哪筆。
    """
    try:
        uuid.UUID(ref)
        is_id = True
    except ValueError:
        is_id = False
    if is_id:
        row = await pool.fetchrow("SELECT id, name, created_at FROM missions WHERE id = $1", ref)
        same_name = 1
    else:
        rows = await pool.fetch(
            "SELECT id, name, created_at FROM missions WHERE name = $1 "
            "ORDER BY created_at DESC", ref)
        row, same_name = (rows[0] if rows else None), len(rows)
    if row is None:
        raise HTTPException(404, f"任務庫找不到「{ref}」")
    wp_rows = await pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = $1 ORDER BY seq", row["id"])
    if not wp_rows:
        raise HTTPException(404, f"任務「{row['name']}」沒有航點")
    return {"id": str(row["id"]), "name": row["name"],
            "created_at": row["created_at"], "same_name_count": same_name,
            "waypoints": _unpack(wp_rows)}


def _check(wps: list[dict]) -> dict:
    return plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m, settings.geofence_margin)


@app.get("/api/missions")
async def list_missions():
    """**總表**：任務庫裡所有航線（與前端路徑管理頁同一份資料）。

    唯讀，不吃 `ENABLE_COMMANDS` gate——看有哪些航線不會動到飛機。
    `name` 與 `id` 都可以直接餵給 `/api/start` 的 `mission`。
    """
    rows = await pool.fetch("""
        SELECT m.id::text AS id, m.name, m.created_by AS source, m.created_at,
               m.is_active, count(w.seq) AS waypoint_count,
               count(*) FILTER (WHERE w.lat <> 0 OR w.lon <> 0) AS nav_count
        FROM missions m LEFT JOIN waypoints w ON w.mission_id = m.id
        GROUP BY m.id ORDER BY m.created_at DESC""")
    return {"source": "db", "missions": [dict(r) for r in rows]}


@app.get("/api/missions/{ref}")
async def get_mission(ref: str):
    """**內容**：單一航線的完整航點（含 command/frame/p1–p4 保真欄位）＋幾何預檢。

    `ref` 可以是 mission id 或名稱。這裡的 `check` 與 `/api/start` 上傳前跑的是
    同一份檢查——外部可以先看過再決定要不要觸發。
    """
    m = await _resolve_mission(ref)
    return {**m, "waypoint_count": len(m["waypoints"]), "check": _check(m["waypoints"])}


@app.get("/api/plans")
async def list_plans():
    """次要來源：`missions/` 目錄下的 `.plan` 檔一覽（含解析失敗的，帶 error）。

    這些檔案不在任務庫裡；`/api/start` 帶 `plan` 觸發時會先匯入任務庫再飛。
    """
    return {"source": "file", "dir": settings.missions_dir,
            "plans": plans.scan(settings.missions_dir)}


@app.get("/api/plans/{name}")
async def get_plan(name: str, raw: bool = False):
    """單一 `.plan` 檔：預設回解析後的航點＋幾何預檢；`raw=true` 回 QGC 原始 JSON。"""
    try:
        path = plans.resolve(settings.missions_dir, name)
        if raw:
            return plans.raw(path)
        d = plans.detail(path)
    except plans.PlanError as e:
        raise HTTPException(404, str(e))
    return {**d, "check": _check(d["waypoints"])}


def _resolve_sysid(sysid: int | None) -> int:
    """指定就用指定的；沒指定且只有一台連線就用那台。

    多台時**不猜**——猜錯是讓錯的飛機起飛。
    """
    seen = router.snapshot()
    fresh = sorted(int(k) for k, v in seen.items() if v["age_s"] <= STALE_S)
    if sysid is not None:
        d = seen.get(str(sysid))
        if d is None:
            raise HTTPException(404, f"sysid {sysid} 未連線（心跳未見）"
                                     f"｜目前看得到：{fresh or '無'}")
        if d["age_s"] > STALE_S:
            raise HTTPException(409, f"sysid {sysid} 心跳已停 {d['age_s']} 秒，視為斷線")
        return sysid
    if not fresh:
        raise HTTPException(503, "沒有任何機在線——檢查 COMMAND_MAVLINK_URL "
                                 "與機端 MAVLink 實例（curl :38001/healthz）")
    if len(fresh) > 1:
        raise HTTPException(409, f"連線中有多台 {fresh}，請在 payload 指定 sysid")
    return fresh[0]


def _same_waypoints(old: list, wps: list[dict]) -> bool:
    """既有任務的航點是否與這份 .plan 相同（alt 是 REAL，比對留 0.1 m 容差）。"""
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


async def _store_plan(name: str, wps: list[dict]) -> str:
    """航線入庫並回 mission_id；**內容相同就重用既有那筆**。

    入庫是為了讓飛行架次關聯得到任務（回放疊預計路徑、`/api/sessions` 的
    mission_name 都靠它）。但這支端點會被外部反覆觸發，每次新增一筆會把
    任務庫洗版，所以同名且航點一致時直接重用。
    """
    rows = await pool.fetch(
        "SELECT id FROM missions WHERE name = $1 AND created_by = 'plan-file' "
        "ORDER BY created_at DESC LIMIT 10", name)
    for r in rows:
        old = await pool.fetch(
            "SELECT lat, lon, alt, params FROM waypoints WHERE mission_id = $1 "
            "ORDER BY seq", r["id"])
        if _same_waypoints(old, wps):
            return str(r["id"])
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "INSERT INTO missions (name, created_by) VALUES ($1, 'plan-file') "
                "RETURNING id", name)
            await con.executemany(
                "INSERT INTO waypoints (mission_id, seq, lat, lon, alt, action, params) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                [(row["id"], w["seq"], w["lat"], w["lon"], w.get("alt"),
                  w.get("action", "waypoint"),
                  # MAVLink 保真度欄位塞 params JSONB（與 backend 的入庫格式一致）
                  json.dumps({k: w[k] for k in ("command", "frame", "p1", "p2", "p3", "p4")
                              if w.get(k) is not None}) if w.get("command") is not None
                  else None)
                 for w in wps])
    return str(row["id"])


async def _step(step: str, coro):
    """把失敗標上是哪一步——外部拿到的錯誤要能直接指出斷在哪。"""
    try:
        return await coro
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"msg": e.detail}
        raise HTTPException(e.status_code, {"step": step, **detail})


class StartIn(BaseModel):
    mission: str | None = None       # 任務庫的 id 或名稱（主要來源）
    plan: str | None = None          # 次要：missions/ 下的 .plan 檔名（副檔名可省略）
    sysid: int | None = None         # 省略＝唯一在線的那台；多台時必填
    store: bool = True               # 僅 plan 來源有意義：匯入的航線是否入庫


@app.post("/api/start")
async def start(body: StartIn):
    """**一鍵起飛**：取航線 → 幾何預檢 → 上傳（回讀比對）→ 任務啟動。

    等同 `scripts/fly-mission.py` 的全流程，但外部只要打這一支。航線來源二選一：

      {"mission": "<id 或名稱>"}   任務庫（主要）——清單見 GET /api/missions
      {"plan": "xxx.plan"}         missions/ 目錄的檔案，會先匯入任務庫再飛

    **同步回應**：上傳含逐項回讀比對，實測 10–40 秒，呼叫端 timeout 請設
    60 秒以上。失敗時 `detail.step` 指出斷在哪一步（mission/plan/precheck/
    upload/start），上傳與啟動的失敗還會帶 PX4 的 `px4_notes` 原文。

    成功的定義沿用單步端點：`verified=true`（機上任務與送出內容一致）且
    `started=true`（機端 ACK 為 ACCEPTED）。兩者任一不成立都會是非 2xx。
    """
    _require_enabled()
    t0 = time.monotonic()
    if bool(body.mission) == bool(body.plan):
        raise HTTPException(422, "mission 與 plan 二選一（mission＝任務庫，plan＝.plan 檔）")

    src = {"source": "db" if body.mission else "file", "skipped": []}
    if body.mission:
        m = await _step("mission", _resolve_mission(body.mission))
        src |= {"mission_id": m["id"], "name": m["name"], "waypoints": m["waypoints"]}
        if m["same_name_count"] > 1:
            log.warning("任務名稱「%s」有 %d 筆同名，取最新的 %s",
                        m["name"], m["same_name_count"], m["id"])
    else:
        try:
            path = plans.resolve(settings.missions_dir, body.plan)
            parsed = plans.parse(path)
        except plans.PlanError as e:
            await _audit(None, "start", {"plan": body.plan}, "failed", str(e))
            raise HTTPException(404, {"step": "plan", "msg": str(e)})
        src |= {"mission_id": None, "name": path.name,
                "waypoints": parsed["waypoints"], "skipped": parsed["skipped"]}

    sysid = _resolve_sysid(body.sysid)
    wps = src["waypoints"]
    audit_params = {"source": src["source"], "name": src["name"],
                    "items": len(wps), "sysid": sysid}

    report = _check(wps)
    if not report["ok"] and settings.geofence_enforce:
        await _audit(sysid, "start", audit_params, "rejected_precheck",
                     "；".join(report["problems"]))
        raise HTTPException(409, {"step": "precheck",
                                  "msg": "航線未通過幾何預檢，未上傳", **report})
    if not report["ok"]:
        log.warning("預檢有問題但未啟用擋門，照常起飛：%s", "；".join(report["problems"]))

    # .plan 來源才需要入庫（任務庫來源本來就在庫裡）——架次要關聯到任務就得有 id
    mission_id = src["mission_id"]
    if mission_id is None and body.store:
        mission_id = await _store_plan(path.stem, wps)

    up = await _step("upload", _run(
        sysid, "start:upload", mav.job_upload_mission, build_items(wps),
        params={**audit_params, "mission_id": mission_id}, timeout=UPLOAD_TIMEOUT_S))
    st = await _step("start", _run(
        sysid, "start:mission_start", mav.job_command, 300, [0.0], params=audit_params))

    await _audit(sysid, "start", audit_params, "accepted",
                 json.dumps({"mission_id": mission_id, "uploaded": up.get("uploaded")}))
    return {
        "source": src["source"],
        "name": src["name"],
        "mission_id": mission_id,
        "sysid": sysid,
        "waypoints": len(wps),
        "skipped": src["skipped"],
        "uploaded": up.get("uploaded"),
        "verified": up.get("verified"),
        "started": st.get("accepted"),
        "result": st.get("result"),
        "check": report,
        "px4_notes": (up.get("px4_notes") or []) + (st.get("px4_notes") or []),
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
