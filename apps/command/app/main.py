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
import json
import logging
import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import mav, plan_check
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("command")

router: mav.MavRouter | None = None
pool: asyncpg.Pool | None = None

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
        "INSERT INTO command_log (sysid, action, params, result, detail) "
        "VALUES ($1, $2, $3, $4, $5)",
        sysid, action, json.dumps(params, default=str), result, detail[:500])


def _require_enabled():
    if not settings.enable_commands:
        raise HTTPException(403, "指令能力未啟用（ENABLE_COMMANDS=false，預設關閉）"
                                 "——這是刻意的安全 gate，部署時顯式開啟")


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
    return {"ok": True, "enabled": settings.enable_commands,
            "gcs_sysid": mav.GCS_SYSID, "drones": router.snapshot()}


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
                     params={"mission_id": body.mission_id, "items": len(wps)})
    return {**res, "check": report}
