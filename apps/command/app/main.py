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

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 每請求的 client 來源（X-Client header）——留痕歸因用（issue 013-B：驗收 rig 帶
# X-Client: acceptance-rig，指令來源一眼可辨、查案不用反推）。contextvar 讓 _audit
# 不必改每個端點簽名就取得；背景序列（execute 起的 task）沿用觸發請求的 client。
_client_var: contextvars.ContextVar = contextvars.ContextVar("client", default=None)

from . import group_exec, mav, plan_check
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("command")

router: mav.MavRouter | None = None
pool: asyncpg.Pool | None = None
executor: "group_exec.GroupExecutor | None" = None

# waypoints.action → MAV_CMD（舊資料沒存原始 command 時的推回）
ACTION_CMD = {"takeoff": 22, "waypoint": 16, "land": 21, "rtl": 20}


def build_items(wps: list[dict]) -> list[dict]:
    """MAVLink 保真度（對齊實戰工具 upload_mission.py）：新資料帶 .plan 的
    原始 command/frame/p1–p4，原樣送出；舊資料由 action 推回、frame 3
    （GLOBAL_RELATIVE_ALT，QGC 預設）、params 補 0。"""
    items = []
    for i, w in enumerate(wps):
        p = w.get("params")
        p = json.loads(p) if isinstance(p, str) else (p or {})
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
    cap, reasons = mav.caps.capabilities_for(ap)
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
    executor = group_exec.GroupExecutor(router, pool, build_items, _audit, _live)
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
    return {"ok": True, "enabled": settings.enable_commands,
            "gcs_sysid": mav.GCS_SYSID, "drones": router.snapshot()}


@app.post("/api/command/{sysid}/arm")
async def arm(sysid: int):
    _require_enabled(); _require_capability(sysid, "arm")
    return await _run(sysid, "arm", mav.job_command, 400, [1.0])


@app.post("/api/command/{sysid}/disarm")
async def disarm(sysid: int):
    _require_enabled(); _require_capability(sysid, "disarm")
    return await _run(sysid, "disarm", mav.job_command, 400, [0.0])


@app.post("/api/command/{sysid}/mode/{mode}")
async def set_mode(sysid: int, mode: str):
    if mode not in mav.PX4_MODES:
        raise HTTPException(422, f"mode 須為 {sorted(mav.PX4_MODES)}")
    _require_enabled(); _require_capability(sysid, f"mode:{mode}")
    return await _run(sysid, f"mode:{mode}", mav.job_set_mode, mode)


@app.post("/api/command/{sysid}/mission/start")
async def mission_start(sysid: int):
    _require_enabled(); _require_capability(sysid, "mission_start")
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

    PX4 的 MAV_CMD_NAV_TAKEOFF param7 是**絕對海拔**——用 live 的
    alt_msl - alt_rel 推地面海拔再加目標高度；經緯度/偏航給 NaN＝原地。
    """
    d = await _live()
    if d.get("alt_msl") is None or d.get("alt_rel") is None:
        raise HTTPException(409, "沒有高度資料（GPS/EKF 未就緒），無法起飛")
    target_amsl = (d["alt_msl"] - d["alt_rel"]) + alt
    steps = {}
    if not d.get("armed"):
        steps["arm"] = await _run(sysid, "arm", mav.job_command, 400, [1.0])
    steps["takeoff"] = await _run(
        sysid, f"takeoff:{alt}m", mav.job_command, 22,
        [0.0, 0.0, 0.0, math.nan, math.nan, math.nan, target_amsl])
    return steps


class ManualIn(BaseModel):
    x: float = 0.0    # pitch：前+ 後−（-1..1）
    y: float = 0.0    # roll：右+ 左−
    z: float = 0.0    # throttle：上+ 下−（0＝定高懸停）
    r: float = 0.0    # yaw rate：右+ 左−


@app.post("/api/command/{sysid}/manual/start")
async def manual_start(sysid: int):
    """啟用虛擬搖桿：切 POSCTL（位置模式）——鬆手即懸停不墜落。
    連續操縱的安全鏈在 mav._tick_manual：deadman＋失聯自動 Hold。
    RC 實體遙控在 PX4 端永遠優先（COM_RC_OVERRIDE），可隨時接管。

    順序關鍵（PX4 雞生蛋）：POSCTL 需要「已存在的手動控制串流」才會
    engage——先送中位 MANUAL_CONTROL 建立串流，再切模式。"""
    _require_capability(sysid, "manual")   # POSCTL＋deadman 降級是 PX4 方言
    router.set_manual(sysid, 0.0, 0.0, 0.0, 0.0)   # 起手中位＝懸停；先開串流
    await asyncio.sleep(0.5)                        # 讓幾筆 MANUAL_CONTROL 先出去
    return await _run(sysid, "manual_start", mav.job_set_mode, "position")


@app.post("/api/command/{sysid}/manual", status_code=204)
async def manual_setpoint(sysid: int, body: ManualIn):
    """搖桿設定點（前端 ~10Hz 串流；deadman 靠持續更新維持）。
    高頻端點：直接寫入、不 ACK、不留痕（留痕會灌爆 command_log）。"""
    if not settings.enable_commands:
        raise HTTPException(403, "指令能力未啟用")
    for v in (body.x, body.y, body.z, body.r):
        if not -1.0 <= v <= 1.0:
            raise HTTPException(422, "搖桿值須在 -1..1")
    router.set_manual(sysid, body.x, body.y, body.z, body.r)


@app.post("/api/command/{sysid}/manual/stop")
async def manual_stop(sysid: int):
    """收桿：結束手動並切 Hold（自主懸停）。"""
    _require_enabled()
    router.stop_manual(sysid)
    res = await _run(sysid, "manual_stop", mav.job_set_mode, "hold")
    return res


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
    steps = {}
    if body.mission_id:
        steps["upload"] = await mission_upload(sysid, UploadIn(mission_id=body.mission_id))
    steps.update(await _do_takeoff(sysid, body.takeoff_alt))

    # 等高度實際到達（80% 即視為到位，PX4 收斂段不必等滿）
    deadline = asyncio.get_running_loop().time() + body.alt_timeout_s
    alt = None
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        alt = (await _live()).get("alt_rel")
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


@app.post("/api/command/{sysid}/mission/upload")
async def mission_upload(sysid: int, body: UploadIn):
    _require_enabled(); _require_capability(sysid, "mission_upload")
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
