import asyncio
import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

import logging

import mission_time        # libs/ 的共用實作（PYTHONPATH=/srv/libs）
import plan_check

from . import agent_link, db, groups, mavlink_rx
from .config import settings
from .ws import manager

log = logging.getLogger("app.api")
from .jsonsafe import dumps as jdumps
from .link_events import transition as link_transition
from .state import live

router = APIRouter(prefix="/api")


@router.get("/drones")
async def list_drones():
    from .state import fleet
    from .dialect import autopilot_name
    rows = await db.pool.fetch("SELECT * FROM drones ORDER BY created_at")
    out = []
    for r in rows:
        d = dict(r)
        # autopilot：從即時 fleet 帶（runtime，非 DB 欄位）；沒連過 MAVLink＝null
        st = fleet.get(str(d["id"]))
        d["autopilot"] = (autopilot_name(st.autopilot_raw)
                          if st and st.autopilot_raw is not None else None)
        # 038：韌體版本與板子 UID 也是 runtime 值（非 DB 欄位）。
        # 一致性測試靠它把「對哪一版驗過」寫進證據——沒有版本的證據，
        # 等於宣稱「驗過」卻說不出驗的是什麼。
        # runtime 優先、**DB 值墊底**：這兩項是板子的穩定屬性，機此刻沒連線
        # 不代表我們不知道它是誰。原本無條件覆寫，會把已持久化的身分在離線時
        # 抹成 null——前端就會宣告「無板子 UID」，那是假話
        if st and st.flight_sw_version:
            d["flight_sw_version"] = st.flight_sw_version
        if st and st.board_uid:
            d["board_uid"] = st.board_uid
        # 意圖通道現況（協定 §4.2 的鏡像）。**整頁載入時就要有值**——
        # 只靠 WS 推播的話，剛打開頁面到下一拍之間是空白，而空白會被讀成
        # 「沒有代理」，那與「代理在、只是還沒推」是兩件事
        al = agent_link.links.get(d.get("board_uid"))
        d["agent"] = al.as_dict() if al else None
        out.append(d)
    return out


class DroneIn(BaseModel):
    name: str
    connection_url: str | None = None
    note: str | None = None
    # ── 人工維護的身分（038 兩層模型的下半）────────────────────
    # 機器可驗證的那層（sysid／board_uid／韌體版本）由機體自報，**不在此填**：
    # 人填只會製造第二個事實來源。這兩欄是機器問不到的東西。
    airframe_serial: str | None = None    # 機架序號（貼在機身上的那個）
    model: str | None = None              # 型號，如 "X500 v2"


@router.post("/drones")
async def register_drone(d: DroneIn):
    """註冊一台無人機（真機階段用；模擬機由 backend 啟動時自動註冊）。"""
    row = await db.pool.fetchrow(
        """
        INSERT INTO drones (name, serial_no, is_simulated, connection_url, status,
                            airframe_serial, model)
        VALUES ($1, $1, false, $2, 'idle', $3, $4)
        ON CONFLICT (serial_no) DO NOTHING
        RETURNING *
        """,
        d.name, d.connection_url,
        (d.airframe_serial or "").strip() or None,
        (d.model or "").strip() or None,
    )
    if row is None:
        raise HTTPException(409, f"名稱 {d.name} 已存在")
    return dict(row)


class DronePatch(BaseModel):
    name: str | None = None
    video_url: str | None = None      # 空字串＝清除
    airframe_serial: str | None = None   # 空字串＝清除
    model: str | None = None             # 空字串＝清除


class AgentHello(BaseModel):
    """機上代理上線時自報。**註冊是收到它的副作用**（issues/038、協定 §4.1）。

    只收**機器問得到**的東西。機架序號與型號不在這裡——代理知道的是板子，
    不是機架，那兩項只能由人維護。
    """
    board_uid: str                       # 飛控板 UID＝這是哪一架飛機
    agent_uid: str | None = None         # 樹莓派序號＝這是哪一台伴飛電腦
    autopilot: str | None = None
    fw: str | None = None                # 已解碼的韌體版本
    vehicle_type: int | None = None      # MAV_TYPE
    agent_version: str | None = None


@router.post("/agent/hello")
async def agent_hello(h: AgentHello):
    """代理上線：確保這塊飛控板有一筆機體記錄，沒有就自動建。

    **這取代了人工註冊。** 代理知道的（板子 UID、廠牌、韌體、機型）全部是
    機器問得到的，讓人打字只會打錯；而人該維護的（機架序號、型號、名稱）
    代理問不到。前端因此只需要**編輯**，不需要註冊表單。

    冪等：同一塊板子重複上線只會更新，不會長出第二筆。
    """
    uid = (h.board_uid or "").strip()
    if not uid:
        raise HTTPException(422, "board_uid 不可為空——沒有它就沒有穩定的身分")
    drone_id, name, created = await db.ensure_drone_by_board(
        uid, autopilot=h.autopilot, fw=h.fw, agent_uid=h.agent_uid,
        vehicle_type=h.vehicle_type)
    if created:
        # **新機出現不該是靜默的。** 自動化省掉的是打字，不是知情。
        ev = await db.insert_event(
            drone_id, None, "info", "drone_registered",
            {"drone": name, "board_uid": uid, "agent_uid": h.agent_uid,
             "autopilot": h.autopilot, "fw": h.fw,
             "note": "代理自報上線、系統自動建檔——請到無人機頁改名並補機架序號"})
        ev["drone"] = name
        await manager.broadcast({"type": "event", "event": ev})
        log.info("代理自動註冊：%s（board_uid=%s agent_uid=%s）",
                 name, uid, h.agent_uid)
    return {"drone_id": drone_id, "name": name, "created": created}


class GuardIn(BaseModel):
    #: params 裡帶 dry_run:true ＝**只問判決不動飛機**。加這個是因為要驗證
    #: 守門「放行」那幾格，本來一定得真的下指令——2026-08-25 就對一台停在
    #: 地上的真機送出了 RTL／LAND／LOITER，只為了確認守門會放行。
    #: **驗證不該需要動到飛機。**
    """指令服務執行飛行操作前，先問機上守門（協定 §5.2、丙案分工）。"""
    #: 協定訊息型別：intent（問守門／要提案）／decision（人的確認）／
    #: progress（序列逐步回報）。**分開而不是塞進 action**：它們是不同的事，
    #: 混在一個欄位裡，冪等鍵就會把 decision 之後的 progress 當成重送吞掉
    kind: str = "intent"
    action: str
    drone_id: str | None = None
    board_uid: str | None = None
    intent_id: str | None = None
    params: dict | None = None


@router.post("/agent/intent")
async def agent_intent(body: GuardIn):
    """把意圖送給機上代理：守門在那邊，執行看動作分工。

    **這個端點由指令服務呼叫，不是由前端。** 守門如果只在前端問，那它就不是
    守門——前端可以不問、也可能有別的呼叫端（驗收 rig、MCP、curl）。
    要擋得住，就得擋在真的會動到飛機的那條路上。

    **沒有代理不等於不能飛**：這台機可能根本沒裝代理（他人的 QGC、SITL）。
    那種情況回 `no_agent`，由呼叫端沿用自己原本的檢查——守門是額外一層，
    不是唯一一層。但**代理在、卻問不到**（逾時）要當成拒絕：那是「不知道
    守門怎麼說」，而不知道在飛安路徑上就是不行。

    **有代理但失聯中回 `queued`**（039 複裁 G）：操作不送出、壓進佇列，
    鏈路恢復時重新問一次判決並攤給人確認。這與 `no_agent` 分開，是因為原本
    兩者同形——而「這台機沒裝代理」與「這台機失聯了」在飛安上完全不同。

    > **已知限制**：`links` 是行程內的記憶體登錄表。地面站重啟後，一台失聯中
    > 的機會退回 `no_agent`（＝放行沿用本地檢查），因為那條 hello 從來沒進來
    > 過。要修就得把代理登錄持久化，屬 issues/033 的範圍。
    """
    import uuid as _uuid
    from . import agent_link
    link = None
    if body.board_uid:
        link = agent_link.links.get(body.board_uid)
    elif body.drone_id:
        link = next((l for l in agent_link.links.values()
                     if l.drone_id == body.drone_id), None)
    if body.kind not in ("intent", "decision", "progress"):
        raise HTTPException(422, f"不認得的協定型別 {body.kind}")
    if link is None:
        return {"verdict": "no_agent",
                "reason": "這台機沒有機上代理，守門這一層不存在"}
    if not link.connected:
        # **有代理但失聯中**——這與「沒有代理」是兩件事，原本兩者都回
        # `no_agent`（＝放行沿用本地檢查）。039 複裁 G：壓下來，恢復後補送。
        iid = body.intent_id or str(_uuid.uuid4())
        if body.kind != "intent":
            # decision／progress 是「已經開始的那件事」的後續，壓下來沒有意義：
            # 那件事在機上早就因為失聯自己收尾了（協定 §6）
            return {"verdict": "unknown", "intent_id": iid,
                    "reason": "意圖通道失聯中，這則後續送不出去"}
        if body.action not in (link.vets or []):
            return {"verdict": "no_agent",
                    "reason": f"機上代理（{link.agent_version}）沒有宣告守 "
                              f"{body.action}，守門這一層不存在"}
        n, dropped = agent_link.queue_intent(link, body.action, body.params, iid)
        return {"verdict": "queued", "intent_id": iid, "pending": n,
                "dropped": dropped,
                "reason": "這台機的意圖通道失聯中，操作**沒有送出去**。"
                          "已經記下來，鏈路恢復後會重新問一次判決並攤給你確認"
                          "——不會自動執行"}
    # **版本協商**：代理在 hello 裡宣告它守哪些意圖（`vets`）。舊版代理沒有
    # 這個欄位，也不會回 event——不先問清楚就送過去，只會等到逾時，然後
    # 「不知道＝不行」把所有飛行操作擋死。**能力宣告要用問的，不要用試的。**
    # decision／progress 是**已經開始的那件事的後續**，守門在 intent 那一關
    # 已經問過了；這裡只檢查 intent
    if body.kind == "intent" and body.action not in (link.vets or []):
        # **分辨「代理不守這個」與「根本沒有這個意圖」**：前者是版本差異、
        # 該放行讓本地檢查接手；後者是呼叫端寫錯，放行等於把一個打錯的字
        # 當成合法操作放過去
        known = set().union(*(l.vets or [] for l in agent_link.links.values())) \
            if agent_link.links else set()
        if known and body.action not in known:
            raise HTTPException(422, f"不認得的意圖 {body.action}")
        return {"verdict": "no_agent",
                "reason": f"機上代理（{link.agent_version}）沒有宣告守 "
                          f"{body.action}，守門這一層不存在"}
    iid = body.intent_id or str(_uuid.uuid4())
    try:
        ev = await agent_link.send_intent(link, body.action, body.params, iid,
                                          kind=body.kind)
    except TimeoutError:
        return {"verdict": "unknown", "intent_id": iid,
                "reason": "代理沒有在時限內回覆守門判決——**不知道不等於可以**"}
    except ConnectionError as e:
        return {"verdict": "unknown", "intent_id": iid, "reason": str(e)}
    kind = ev.get("event")
    verdict = {"guard_refused": "refused", "cleared": "cleared",
               "proposal": "cleared", "sent": "done", "noted": "done",
               "would_execute": "would_execute",
               "cancelled": "cancelled", "failed": "failed"}.get(kind, kind)
    return {"verdict": verdict, "intent_id": iid, "event": ev,
            "reason": ev.get("reason"), "state": ev.get("state")}


class BackfillSample(BaseModel):
    """機上緩衝的一筆取樣。**欄位刻意等同 telemetry 表**——補傳不是另一種
    資料，它就是那段時間我們本來該收到的資料。"""
    t: float                          # 機上的 unix 秒（浮點）
    lat: float | None = None
    lon: float | None = None
    alt_msl: float | None = None
    alt_rel: float | None = None
    heading: float | None = None
    ground_speed: float | None = None
    battery_pct: float | None = None
    battery_voltage: float | None = None
    gps_fix: int | None = None
    satellites: int | None = None
    flight_mode: str | None = None
    armed: bool | None = None


class BackfillIn(BaseModel):
    """C 層：代理把 5G 斷線期間的取樣補傳上來。"""
    board_uid: str
    samples: list[BackfillSample] = Field(min_length=1, max_length=5000)
    #: 這段期間機體有沒有一直保持 armed（代理看得到，地面站看不到）。
    #: D 層用它判斷「回來之後還算不算同一趟」
    stayed_armed: bool | None = None


@router.post("/telemetry/backfill")
async def telemetry_backfill(body: BackfillIn):
    """**這段資料本來就沒有遺失，只是送不出來。**

    5G 斷線時代理照樣看得到飛控的一切；現在我們把它丟掉。這個端點讓它補回來。

    三條紀律：

    * **標記 `backfilled`**。它的時間戳是機上的（可能與地面站有偏差），
      而且它不該觸發任何即時判斷。不標的話，事後分不出哪些是後補的。
    * **不碰即時狀態**。補傳不更新 `live`、不發事件、不改 `connected`——
      那是過去的資料，讓它影響「現在」就是把歷史當成現況。
    * **時間戳去重**。重連後代理可能重送一段，同一秒的資料以先到的為準。
    """
    from . import agent_link
    link = agent_link.links.get(body.board_uid)
    drone_id = link.drone_id if link else None
    if drone_id is None:
        row = await db.pool.fetchrow(
            "SELECT id::text AS id FROM drones WHERE board_uid = $1",
            body.board_uid)
        drone_id = row["id"] if row else None
    if drone_id is None:
        raise HTTPException(404, f"不認得的 board_uid {body.board_uid}")

    lo = min(s.t for s in body.samples)
    hi = max(s.t for s in body.samples)
    # 這段時間屬於哪個架次？**用時間去找**，不是用「現在的架次」——補傳的
    # 是過去的資料，而那時開著的架次可能已經因為失聯被收掉了
    sess = await db.pool.fetchrow(
        "SELECT id::text AS id FROM flight_sessions "
        "WHERE drone_id = $1::uuid AND started_at <= to_timestamp($2) "
        "AND (ended_at IS NULL OR ended_at >= to_timestamp($3)) "
        "ORDER BY started_at DESC LIMIT 1", drone_id, hi, lo)
    session_id = sess["id"] if sess else None

    # **去重靠先查再濾，不靠 ON CONFLICT**：telemetry 是 hypertable，
    # (drone_id, time) 上沒有唯一索引，`ON CONFLICT DO NOTHING` 因此什麼也不做
    # ——重連後代理重送一段，資料就會變成兩份（2026-08-26 測出來的）。
    # 補一個唯一索引要處理既有重複列，而這裡查一次範圍內的時間戳更直接。
    exist = await db.pool.fetch(
        "SELECT extract(epoch FROM time) AS t FROM telemetry "
        "WHERE drone_id = $1::uuid AND time BETWEEN to_timestamp($2) "
        "AND to_timestamp($3)", drone_id, lo - 0.5, hi + 0.5)
    have = {round(float(r["t"]), 2) for r in exist}
    fresh = [s for s in body.samples if round(s.t, 2) not in have]
    rows = [(s.t, drone_id, session_id, s.lat, s.lon, s.alt_msl, s.alt_rel,
             s.heading, s.ground_speed, s.battery_pct, s.battery_voltage,
             s.gps_fix, s.satellites, s.flight_mode) for s in fresh]
    await db.pool.executemany(
        """INSERT INTO telemetry (time, drone_id, session_id, lat, lon,
             alt_msl, alt_rel, heading, ground_speed, battery_pct,
             battery_voltage, gps_fix, satellites, flight_mode, backfilled)
           VALUES (to_timestamp($1), $2::uuid, $3::uuid, $4, $5, $6, $7, $8,
                   $9, $10, $11, $12, $13, $14, true)""", rows)

    # 把這段時間的失明記錄標成「已補回」
    closed = await db.pool.fetch(
        "UPDATE blackouts SET recovered_by = 'backfilled' "
        "WHERE drone_id = $1::uuid AND started_at <= to_timestamp($2) "
        "AND coalesce(ended_at, now()) >= to_timestamp($3) "
        "RETURNING id::text AS id", drone_id, hi, lo)
    # ── D 層：回來之後，那還算不算同一趟 ────────────────────────
    # 代理看得到整段（它就在機上），所以它說得出「這段期間機體有沒有一直
    # armed」。地面站看不到，只能問它。
    #
    # **有代理說話 → 相信它**：沒落地就沿用同一個架次（上面已經用時間找到）。
    # **沒有代理 → 誠實說不知道**：那時 stayed_armed 是 None，我們不去猜，
    # 只把資料補進去、把失明記錄留著。**不要假裝是同一趟**。
    if body.stayed_armed and session_id:
        # 架次被失聯收尾過，但機體其實一直在飛——把結束時間往後推到補傳的
        # 尾端，並改記理由。**不重開架次**：重開會讓一趟飛行在記錄上變成兩趟
        await db.pool.execute(
            "UPDATE flight_sessions SET ended_at = greatest(ended_at, "
            "to_timestamp($2)), end_reason = 'telemetry_lost_backfilled' "
            "WHERE id = $1::uuid AND end_reason = 'telemetry_lost'",
            session_id, hi)
    log.info("補傳 %d 筆、跳過 %d 筆重複（%s，架次 %s，補回 %d 段失明）",
             len(rows), len(body.samples) - len(rows), body.board_uid,
             session_id, len(closed))
    return {"ok": True, "inserted": len(rows),
            "skipped_duplicate": len(body.samples) - len(rows),
            "session_id": session_id,
            "blackouts_recovered": [r["id"] for r in closed],
            "stayed_armed": body.stayed_armed}


@router.patch("/drones/{drone_id}")
async def patch_drone(drone_id: str, body: DronePatch):
    """改名／設定影像串流位址（系統端管理機的身分與屬性，不走環境變數）。
    serial_no 保持原值當穩定鍵。"""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(422, "沒有要更新的欄位")
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise HTTPException(422, "名稱不可為空")
        fields["name"] = name
    for k in ("video_url", "airframe_serial", "model"):
        if k in fields:
            # 空字串＝清除（存 NULL）。**不要存空字串**——那會讓「沒填」與
            # 「填了又刪掉」在資料上長得不一樣，但意思相同。
            fields[k] = (fields[k] or "").strip() or None
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    r = await db.pool.execute(
        f"UPDATE drones SET {sets} WHERE id = $1", drone_id, *fields.values())
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此無人機")
    if live.drone_id == drone_id and "name" in fields:
        live.drone_name = fields["name"]   # 主機改名即時反映（事件標籤、側欄）
    return {"ok": True}


@router.post("/drones/{drone_id}/primary")
async def set_primary(drone_id: str):
    """指定哪台是 MAVLink 主機（14540 收到的遙測記在這台名下）。

    飛行中拒切——切換身分會讓進行中的航線歸屬錯亂。
    切換立即生效（api 與 ingest 同進程，直接改 live state），不需重啟。
    """
    if live.armed:
        raise HTTPException(409, "飛行中無法切換主機")
    row = await db.pool.fetchrow(
        "SELECT id::text AS id, name, is_simulated FROM drones WHERE id = $1", drone_id)
    if row is None:
        raise HTTPException(404, "無此無人機")
    if row["name"].startswith("swarm-"):
        raise HTTPException(409, "群飛模擬僚機不能設為主機")
    async with db.pool.acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE drones SET is_primary = false WHERE is_primary")
            await con.execute("UPDATE drones SET is_primary = true WHERE id = $1", drone_id)
    live.drone_id, live.drone_name = row["id"], row["name"]
    live.session_id = None
    return {"ok": True, "primary": row["name"]}


@router.delete("/drones/{drone_id}")
async def delete_drone(drone_id: str):
    """刪除無人機與其**全部**架次、遙測、鏈路與事件資料。不可復原。

    連線中的無人機拒刪：live 迴圈還在用它的 id 寫入，刪了會整路 FK 錯誤；
    模擬機由系統啟動時自動註冊，刪了重啟也會重新出現。
    """
    if live.drone_id == drone_id:
        raise HTTPException(409, "此無人機目前連線中（模擬機由系統自動註冊），無法刪除")
    async with db.pool.acquire() as con:
        async with con.transaction():
            counts = {}
            for table in ("telemetry", "link_metrics", "events", "flight_sessions"):
                r = await con.execute(f"DELETE FROM {table} WHERE drone_id = $1", drone_id)
                counts[table] = int(r.split()[-1])
            # 路徑不陪葬：missions 是「路徑快照」不綁機（issues/010、023）。
            # 原本這裡要把 missions.drone_id 清 NULL 才不會踩 FK——該欄已於 023
            # 移除（從建表至今無人寫入，唯一用途就是這行），故不再需要。
            r = await con.execute("DELETE FROM drones WHERE id = $1", drone_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此無人機")
    return {"deleted": counts}


@router.get("/live")
async def live_snapshot():
    """主機的即時狀態快照（與 WS 廣播同一份資料）。

    給不方便開 WebSocket 的取用端：驗收腳本（scripts/check-onboard.py）、
    curl 排查。輪詢頻率不宜超過 broadcast_hz，即時顯示請走 WS。
    """
    return live.telemetry_dict()


def _require_uuid(v: str | None) -> str | None:
    """群組任務的 mission/drone id 必須是合法 UUID。擋掉截斷／亂填字串，否則會一路
    帶到建群 DDL 的 ::uuid cast 才炸 asyncpg DataError → 500（該回 422 才對）。
    保留 str 型別（回傳原值），下游程式不受影響。"""
    if v is None:
        return v
    try:
        UUID(v)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"不是合法的 UUID：{v!r}")
    return v


class GroupDroneIn(BaseModel):
    drone_id: str
    layer_index: int | None = None
    mission_id: str | None = None     # separate 模式各自任務

    @field_validator("drone_id", "mission_id")
    @classmethod
    def _v_uuid(cls, v):
        return _require_uuid(v)


class GroupIn(BaseModel):
    name: str
    mode: str = "unified"             # unified / separate
    base_mission_id: str | None = None
    drones: list[GroupDroneIn] = Field(min_length=1, max_length=8)
    params: dict | None = None

    @field_validator("base_mission_id")
    @classmethod
    def _v_uuid(cls, v):
        return _require_uuid(v)


@router.post("/groups")
async def create_group(g: GroupIn):
    """建群組任務（issue 013-A）：unified 從 base 展開 per-drone 具體任務、
    separate 用各自任務。回 assignments＋跨路徑衝突預檢（capability 嚴格
    gate 在 execute／command 服務，此處只做幾何互檢＋materialize）。"""
    if g.mode == "unified" and not g.base_mission_id:
        raise HTTPException(422, "unified 模式需要 base_mission_id")
    if g.mode == "separate" and any(d.mission_id is None for d in g.drones):
        raise HTTPException(422, "separate 模式每台需要 mission_id")
    try:
        return await groups.create_group(
            g.name, g.mode, g.base_mission_id,
            [d.model_dump() for d in g.drones], g.params)
    except groups.GroupError as e:
        raise HTTPException(422, str(e))


@router.get("/groups/{group_id}")
async def get_group(group_id: str):
    grp = await groups.get_group(group_id)
    if grp is None:
        raise HTTPException(404, "無此群組")
    return grp


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str):
    """刪 draft 群組（使用者調整成員／參數會重建、draft 會堆積——前端回饋 #2）。
    只允許 draft：執行中/已飛過的回 409，保留稽核。"""
    r = await groups.delete_group(group_id)
    if r == "not_found":
        raise HTTPException(404, "無此群組")
    if r == "locked":
        raise HTTPException(409, "非 draft 群組不可刪除（保留稽核紀錄）")
    return {"deleted": group_id}


@router.get("/sessions")
async def list_sessions(limit: int = 50, mission_id: str | None = None,
                        since: str | None = None, min_samples: int | None = None,
                        include_test: bool = False):
    """架次清單。可選 mission_id（綁定任務）／since（ISO 時間窗）／min_samples／include_test。

    `min_samples`：只回鏈路樣本數 ≥ 此值的架次（場域訊號頁用，避免空/測試殘留架次
    佔滿載入窗——空架次不是「飛行」，誠實原則）。用架次結束時算好的 summary.samples_total
    篩，不掃 link_metrics；未結束（summary NULL）視為 0、min_samples≥1 時自然排除。

    `include_test`：預設 False＝隱藏 origin='test' 的架次（測試殘留混研究庫的治理；
    見 backfill-session-origin.sql）；True 顯示全部。'unknown'／'research'／NULL 一律顯示。"""
    conds, args = [], []
    if not include_test:
        conds.append("COALESCE(s.origin, 'unknown') <> 'test'")
    if mission_id:
        args.append(mission_id)
        conds.append(f"s.mission_id = ${len(args) + 1}")
    if since:
        args.append(since)
        conds.append(f"s.started_at >= ${len(args) + 1}::text::timestamptz")
    if min_samples is not None:
        args.append(min_samples)
        conds.append(
            f"COALESCE((s.summary->>'samples_total')::int, 0) >= ${len(args) + 1}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    q = f"""
        SELECT s.*, d.name AS drone_name, m.name AS mission_name
        FROM flight_sessions s
        JOIN drones d ON d.id = s.drone_id
        LEFT JOIN missions m ON m.id = s.mission_id
        {where}
        ORDER BY s.started_at DESC LIMIT $1
        """
    rows = await db.pool.fetch(q, limit, *args)
    return [dict(r) for r in rows]


class SessionPatch(BaseModel):
    note: str | None = None            # 自訂備註（實驗條件標註）；空字串/None＝清除


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: str, body: SessionPatch):
    """更新架次自訂備註（使用者標實驗條件，如「開干擾器那趟」）。"""
    note = (body.note or "").strip() or None
    row = await db.pool.fetchrow(
        "UPDATE flight_sessions SET note = $2 WHERE id = $1 RETURNING id::text, note",
        session_id, note)
    if row is None:
        raise HTTPException(404, "無此架次")
    return {"id": row["id"], "note": row["note"]}


@router.get("/sessions/{session_id}/track")
async def session_track(session_id: str):
    """回放用：一條航線的軌跡 + 鏈路時序 + 關聯任務（供疊預計路徑）。"""
    sess = await db.pool.fetchrow(
        """SELECT s.id, s.mission_id, s.started_at, s.ended_at, m.name AS mission_name
           FROM flight_sessions s LEFT JOIN missions m ON m.id = s.mission_id
           WHERE s.id = $1""", session_id)
    telemetry = await db.pool.fetch(
        "SELECT * FROM telemetry WHERE session_id = $1 ORDER BY time", session_id)
    link = await db.pool.fetch(
        "SELECT * FROM link_metrics WHERE session_id = $1 ORDER BY time", session_id)
    return {"session": dict(sess) if sess else None,
            "telemetry": [dict(r) for r in telemetry],
            "link": [dict(r) for r in link]}


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str):
    """整條航線匯出成單一 JSON（lossless，可離線分析或封存）。
    配合 30 天 retention：要長期保留原始資料就先匯出；
    匯出後可呼叫 DELETE 移除 DB 內的資料（UI 的「匯出並移除」流程）。"""
    sess = await db.pool.fetchrow(
        """SELECT s.*, d.name AS drone_name, m.name AS mission_name
           FROM flight_sessions s
           JOIN drones d ON d.id = s.drone_id
           LEFT JOIN missions m ON m.id = s.mission_id WHERE s.id = $1""", session_id)
    if sess is None:
        raise HTTPException(404, "無此航線")
    payload = {
        "format": "uav-system-session-export",
        "version": 1,
        "session": dict(sess),
        "telemetry": [dict(r) for r in await db.pool.fetch(
            "SELECT * FROM telemetry WHERE session_id = $1 ORDER BY time", session_id)],
        "link_metrics": [dict(r) for r in await db.pool.fetch(
            "SELECT * FROM link_metrics WHERE session_id = $1 ORDER BY time", session_id)],
        "events": [dict(r) for r in await db.pool.fetch(
            "SELECT * FROM events WHERE session_id = $1 ORDER BY time", session_id)],
    }
    started = sess["started_at"].strftime("%Y%m%d-%H%M")
    from fastapi.responses import JSONResponse
    return JSONResponse(
        jsonable(payload),
        headers={"Content-Disposition":
                 f'attachment; filename="flight-{started}-{session_id[:8]}.json"'})


def jsonable(o):
    """asyncpg Row 值轉 JSON 可序列化（datetime→ISO、UUID→str、JSONB字串→物件）。"""
    import uuid as _uuid
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [jsonable(v) for v in o]
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, _uuid.UUID):
        return str(o)
    if isinstance(o, str) and o[:1] in "[{":
        try:
            import json as _json
            return _json.loads(o)
        except Exception:
            return o
    return o


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """刪除一條航線與其全部時序資料。搭配匯出使用（匯出後移除）。"""
    if live.session_id == session_id:
        raise HTTPException(409, "此航線正在飛行中")
    async with db.pool.acquire() as con:
        async with con.transaction():
            counts = {}
            for table in ("telemetry", "link_metrics", "events"):
                r = await con.execute(f"DELETE FROM {table} WHERE session_id = $1", session_id)
                counts[table] = int(r.split()[-1])
            r = await con.execute("DELETE FROM flight_sessions WHERE id = $1", session_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此航線")
    return {"deleted": counts}


@router.get("/sessions/{session_id}/params")
async def session_params(session_id: str):
    """該架次的機上參數快照（021 Phase 2，唯讀）。

    實驗可重現性用：這一趟到底是用什麼設定飛的。**沒有快照就誠實回 null**
    ——可能是該機還沒抓完參數、或是影像功能上線前的舊架次，不要拿「現在的」
    參數冒充「當時的」（那會讓事後分析以為設定沒變過）。
    """
    row = await db.pool.fetchrow(
        """SELECT ps.id::text AS id, ps.hash, ps.param_count, ps.params,
                  ps.first_seen
           FROM flight_sessions fs
           LEFT JOIN param_sets ps ON ps.id = fs.param_set_id
           WHERE fs.id = $1""", session_id)
    if row is None:
        raise HTTPException(404, "無此航線")
    if row["id"] is None:
        return {"param_set_id": None, "params": None,
                "note": "本架次沒有參數快照（該機未完成參數讀取，或為功能上線前的舊架次）"}
    return {"param_set_id": row["id"], "hash": row["hash"],
            "param_count": row["param_count"],
            "first_seen": row["first_seen"].isoformat(),
            "params": json.loads(row["params"]) if isinstance(row["params"], str)
                      else row["params"]}


@router.get("/sessions/{session_id}/video")
async def session_video(session_id: str):
    """該架次的影像片段與狀態（契約見 doc/flight-video-design.md §8b）。

    retention_days 動態帶出——事實源是 .env 的 VIDEO_RETENTION_DAYS，同一個
    變數也餵給錄製器清檔，所以 UI 顯示的天數不可能與實際清檔行為不一致。
    """
    from . import video_rec
    r = await video_rec.session_video(session_id)
    if not r:
        raise HTTPException(404, "無此航線")
    return r


@router.get("/video/segments/{segment_id}/file")
async def video_segment_file(segment_id: str):
    """單段影片檔（同源直供，前端免 CORS）。

    影片檔不轉碼——原樣就是機上送來的樣子，畫質劣化是研究證據不是瑕疵。
    """
    from fastapi.responses import FileResponse
    import os
    row = await db.pool.fetchrow(
        "SELECT path, started_at FROM video_segments WHERE id = $1", segment_id)
    if row is None:
        raise HTTPException(404, "無此影片片段")
    # 片段檔名＝錄製器以段起始時間命名（見設計 §4：檔名只是線索，錨點在 DB）
    d = os.path.join(settings.video_rec_dir, row["path"])
    if not os.path.isdir(d):
        raise HTTPException(410, "影像已不存在（可能已過保留期清除）")
    stamp = row["started_at"].strftime("%Y-%m-%d_%H-%M-%S")
    for name in sorted(os.listdir(d)):
        if name.startswith(stamp) and name.endswith(".mp4"):
            return FileResponse(os.path.join(d, name), media_type="video/mp4")
    raise HTTPException(410, "影像已不存在（可能已過保留期清除）")


@router.get("/events")
async def list_events(limit: int = 100, session_id: str | None = None):
    if session_id:
        rows = await db.pool.fetch(
            "SELECT * FROM events WHERE session_id = $1 ORDER BY time LIMIT $2", session_id, limit)
    else:
        rows = await db.pool.fetch("SELECT * FROM events ORDER BY time DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# ── 任務疊圖（roadmap 3）──────────────────────────────────────────────────
# 從飛機讀回 QGC 上傳的任務。MAVLink 任務下載是**唯讀**操作，
# 不違反「backend 對 MAVLink 只讀不寫」——上傳與啟動仍由 QGC 負責。

# MAV_CMD：帶座標的導航類指令（16 WAYPOINT / 21 LAND / 22 TAKEOFF）
_NAV_CMDS = {16, 21, 22}


@router.get("/mission/current")
async def current_mission():
    if mavlink_rx.rx is None or not live.connected:
        raise HTTPException(503, "MAVLink 未連線")
    try:
        items = await asyncio.wait_for(mavlink_rx.rx.download_mission(), timeout=12)
    except Exception as e:
        raise HTTPException(502, f"任務下載失敗：{e}")
    waypoints = [
        {
            "seq": it.seq,
            "command": it.command,
            "lat": it.x / 1e7,       # MISSION_ITEM_INT 的座標是 int32 度 ×1e7
            "lon": it.y / 1e7,
            "alt": it.z,             # frame 3 = 相對起飛點高度（QGC 預設）
            "frame": it.frame,
            "p1": it.param1, "p2": it.param2, "p3": it.param3, "p4": it.param4,
        }
        for it in items
        if it.command in _NAV_CMDS and (it.x or it.y)
    ]
    return {"item_count": len(items), "waypoints": waypoints}


# ── 任務庫（路徑管理頁）───────────────────────────────────────────────────
# 儲存的路徑可標記 is_active（至多一條），即時頁優先顯示它；
# 沒有啟用中的路徑時，退回「從機上讀回目前任務」。

class WaypointIn(BaseModel):
    seq: int
    lat: float                       # DO_* 設定類（無座標）以 0 表示
    lon: float
    alt: float | None = None
    action: str | None = "waypoint"
    # MAVLink 保真度（2026-08-10）：.plan 的原始 command/frame/p1–p4 全保留，
    # 上傳到機時原樣送出——與現場工具 upload_mission.py 的行為一致。
    # 舊資料沒有這些欄位時由 action 推回（見 plan_check._cmd／command 服務）。
    command: int | None = None
    frame: int | None = None
    p1: float | None = None
    p2: float | None = None
    p3: float | None = None
    p4: float | None = None


class MissionIn(BaseModel):
    name: str
    source: str = "plan-file"        # plan-file / vehicle
    waypoints: list[WaypointIn] = Field(min_length=2, max_length=500)
    # 037：`.plan` 自報的目標機種（QGC 的 mission.firmwareType/vehicleType，
    # 值域就是 MAV_AUTOPILOT／MAV_TYPE）。**存下來是為了上傳前能比對**——
    # 航點的 frame 與 params 是照哪一家的語意寫的，只有這兩個欄位說得出來。
    # 不填＝這份任務沒說（手繪、舊資料、從機上讀回）。
    firmware_type: int | None = None
    vehicle_type: int | None = None
    #: 航線自帶的圍欄（前端從 .plan 的 geoFence 解出來）。**圍欄是每份航線
    #: 自己的事**——沒有它就只能拿系統預設值去量，而那個值只對一個場地成立
    fence: dict | None = None
    #: QGC 的 plannedHomePosition [lat, lon, alt]。RTL 沒有座標，少了它
    #: 返航那一段畫不出來；它也是距離量測該用的原點。
    #:
    #: **元素允許 null**：QGC 在沒有地形資料時把高度寫成 null
    #: （`[24.77, 121.04, null]`）。原本宣告成 list[float] 於是整份 .plan 被
    #: 422 擋下——而我們只用得到 lat/lon，高度從來沒讀過。**為了一個用不到的
    #: 欄位拒收一份合法的航線**，是驗證訂得比需求嚴。
    home: list[float | None] | None = None
    #: .plan 宣告的速度（cruiseSpeed／hoverSpeed），用來估預計時間
    cruise_speed: float | None = None
    hover_speed: float | None = None
    #: QGC 的 rallyPoints.points（[[lat, lon, alt], …]）：緊急備降點。
    #: 元素同樣允許 null（理由見 home）
    rally: list[list[float | None]] | None = None


async def _store_mission(name: str, source: str, wps: list[dict],
                         firmware_type: int | None = None,
                         vehicle_type: int | None = None,
                         fence: dict | None = None,
                         home: list[float] | None = None,
                         cruise: float | None = None,
                         hover: float | None = None,
                         rally: list | None = None) -> str:
    async with db.pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "INSERT INTO missions (name, created_by, kind, firmware_type, "
                "vehicle_type, fence, home, cruise_speed, hover_speed, rally) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
                name, source, "from-vehicle" if source == "vehicle" else "imported",
                firmware_type, vehicle_type,
                jdumps(fence) if fence else None,
                jdumps(home) if home else None, cruise, hover,
                jdumps(rally) if rally else None)
            await con.executemany(
                """INSERT INTO waypoints (mission_id, seq, lat, lon, alt, action, params)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                [(row["id"], w["seq"], w["lat"], w["lon"], w.get("alt"),
                  w.get("action", "waypoint"),
                  # MAVLink 保真度塞 params JSONB（閒置欄位正好承接）
                  jdumps({k: w[k] for k in ("command", "frame", "p1", "p2", "p3", "p4")
                              if w.get(k) is not None}) if w.get("command") is not None else None)
                 for w in wps])
    return str(row["id"])


@router.get("/missions")
async def list_missions():
    # 排除群組任務地面生成的具體任務（created_by='group-gen'）——那是編隊每台的
    # materialized 任務、不是任務庫草稿，會污染一般任務清單 UI（issue 013-B 前端回報）。
    rows = await db.pool.fetch("""
        SELECT m.id, m.name, m.created_by AS source, m.created_at, m.is_active,
               m.firmware_type, m.vehicle_type, m.home,
               m.cruise_speed, m.hover_speed,
               count(w.seq) AS waypoint_count
        FROM missions m LEFT JOIN waypoints w ON w.mission_id = m.id
        WHERE m.kind IS DISTINCT FROM 'generated'
        GROUP BY m.id ORDER BY m.created_at DESC""")
    out = [dict(r) for r in rows]
    if not out:
        return out
    # 預計時間：**一次撈完所有航點再算**，不要一份一份查（N+1）。
    # 算不出來時 eta_s 是 null 並附上 eta_unknown 說明為什麼——
    # 不給預設速度，因為使用者會拿這個數字去安排電池
    ids = [r["id"] for r in out]
    wrows = await db.pool.fetch(
        "SELECT mission_id, seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = ANY($1::uuid[]) ORDER BY mission_id, seq", ids)
    by_mission: dict = {}
    for r in wrows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w.update({k: pm.get(k) for k in ("command", "frame", "p1", "p2")})
        by_mission.setdefault(w["mission_id"], []).append(w)
    for d in out:
        home = d.get("home")
        if isinstance(home, str):
            home = json.loads(home)
        d["home"] = home
        est = mission_time.estimate(
            by_mission.get(d["id"], []), d.get("cruise_speed"),
            d.get("hover_speed"), home)
        d["eta_s"] = est["seconds"]
        d["eta_unknown"] = est["unknown"]
        d["eta_assumptions"] = est["assumptions"]
    return out


@router.get("/missions/active")
async def active_mission():
    row = await db.pool.fetchrow(
        "SELECT id, name, home, fence, rally FROM missions WHERE is_active LIMIT 1")
    if row is None:
        raise HTTPException(404, "沒有啟用中的路徑")
    wps = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action FROM waypoints WHERE mission_id = $1 ORDER BY seq",
        row["id"])
    home = row["home"]
    if isinstance(home, str):
        home = json.loads(home)
    # **返航那一段要畫得出來**：RTL 沒有座標（它的意思是「回到 home」），
    # 少了 home，畫面上航線就停在最後一個航點，看起來像規劃到一半
    def _j(v):
        return json.loads(v) if isinstance(v, str) else v
    # **圍欄與備降點也要畫**：QGC 畫得出來、我們畫不出來，兩邊的圖就不一樣
    # ——而使用者是拿這張圖來確認「機會怎麼飛」的（2026-08-26 回報）
    return {"id": str(row["id"]), "name": row["name"], "home": home,
            "fence": _j(row["fence"]), "rally": _j(row["rally"]),
            "waypoints": [dict(w) for w in wps]}


@router.get("/missions/{mission_id}/waypoints")
async def mission_waypoints(mission_id: str):
    wps = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action FROM waypoints WHERE mission_id = $1 ORDER BY seq",
        mission_id)
    if not wps:
        raise HTTPException(404, "無此路徑或無航點")
    return {"waypoints": [dict(w) for w in wps]}


@router.post("/missions")
async def save_mission(m: MissionIn):
    """存入任務庫並附上幾何預檢報告。**不因預檢失敗而拒存**——任務庫
    可放草稿；真正的擋門在 command 服務上傳到機那一步。"""
    wps = [w.model_dump() for w in m.waypoints]
    mid = await _store_mission(m.name, m.source, wps,
                               m.firmware_type, m.vehicle_type, m.fence, m.home,
                               m.cruise_speed, m.hover_speed, m.rally)
    return {"id": mid, "check": plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, fence=m.fence,
        autopilot=m.firmware_type, home=m.home)}


@router.post("/missions/from-vehicle")
async def import_mission_from_vehicle(name: str | None = None):
    """把機上目前的任務（QGC 上傳的）讀回並存進任務庫。唯讀 + 入庫。"""
    data = await current_mission()
    wps = data["waypoints"]
    if len(wps) < 2:
        raise HTTPException(404, "機上沒有可儲存的任務（航點少於 2）")
    stored = [{"seq": w["seq"], "lat": w["lat"], "lon": w["lon"], "alt": w["alt"],
               "action": {22: "takeoff", 21: "land"}.get(w["command"], "waypoint"),
               "command": w["command"], "frame": w.get("frame"),
               "p1": w.get("p1"), "p2": w.get("p2"), "p3": w.get("p3"), "p4": w.get("p4")}
              for w in wps]
    # 從機上讀回來的任務，機種就是**這台機**——不必猜也不該留空。
    # 這裡填的是實際偵測到的值，跟 .plan 自報的是同一組 enum。
    mid = await _store_mission(
        name or f"機上任務 {datetime.now().strftime('%m/%d %H:%M')}", "vehicle", stored,
        live.autopilot_raw, live.vehicle_type_raw)
    return {"id": mid, "waypoint_count": len(wps),
            "check": plan_check.check_waypoints(
                stored, settings.geofence_radius_m, settings.geofence_alt_m,
                settings.geofence_margin)}


@router.get("/missions/{mission_id}/check")
async def check_mission(mission_id: str):
    """任務庫裡某一份的幾何預檢。**檢查不該只在匯入的那一刻做一次。**

    匯入時看到的報告會隨畫面關掉就消失，而使用者是在**要飛之前**才需要它；
    再者圍欄的系統預設值可能在匯入之後被改過，那時候舊報告就是過期的。
    每次點開一份航線就重算一次，成本是一次查表。
    """
    row = await db.pool.fetchrow(
        "SELECT name, fence, home, firmware_type, vehicle_type FROM missions "
        "WHERE id = $1", mission_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    rows = await db.pool.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = $1 ORDER BY seq", mission_id)
    wps = []
    for r in rows:
        w = dict(r)
        pm = w.get("params")
        pm = json.loads(pm) if isinstance(pm, str) else (pm or {})
        w["command"] = pm.get("command")
        w["frame"] = pm.get("frame")
        wps.append(w)
    fence = row["fence"]
    if isinstance(fence, str):
        fence = json.loads(fence)
    return plan_check.check_waypoints(
        wps, settings.geofence_radius_m, settings.geofence_alt_m,
        settings.geofence_margin, fence=fence,
        # frame 規則是方言，要知道是給哪一家寫的才判得了（沒宣告時只警告）
        autopilot=row["firmware_type"],
        home=json.loads(row["home"]) if isinstance(row["home"], str) else row["home"])


@router.post("/missions/{mission_id}/activate")
async def activate_mission(mission_id: str, active: bool = True):
    async with db.pool.acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE missions SET is_active = false WHERE is_active")
            if active:
                r = await con.execute(
                    "UPDATE missions SET is_active = true WHERE id = $1", mission_id)
                if r.split()[-1] == "0":
                    raise HTTPException(404, "無此路徑")
    return {"ok": True}


@router.post("/missions/{mission_id}/show")
async def show_mission(mission_id: str, why: str = "", sysid: int | None = None):
    """**這份航線就是機上現在的那份 → 畫到即時頁上，並留一筆事件。**

    與 `/activate` 的差別是**語意**，不是行為：`activate` 是人手動切換要看哪
    一份（低頻、不必留痕）；這個是系統在說「機上的航線剛剛變成這一份」。
    後者一定要留痕——上傳與改航線都是會改變飛機行為的操作，事後查案時
    「畫面上那時候畫的是哪一條」是關鍵事實。

    由指令服務在上傳成功／改航線完成後呼叫。
    """
    row = await db.pool.fetchrow("SELECT name FROM missions WHERE id = $1",
                                 mission_id)
    if row is None:
        raise HTTPException(404, "無此路徑")
    async with db.pool.acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE missions SET is_active = false WHERE is_active")
            await con.execute("UPDATE missions SET is_active = true WHERE id = $1",
                              mission_id)
    drone_id = None
    if sysid is not None:
        r = await db.pool.fetchrow(
            "SELECT id::text AS id FROM drones WHERE mav_sysid = $1", sysid)
        drone_id = r["id"] if r else None
    try:
        ev = await db.insert_event(
            drone_id, None, "info", "mission_shown",
            {"mission_id": mission_id, "mission": row["name"], "why": why})
        ev["drone"] = None
        await manager.broadcast({"type": "event", "event": ev})
    except Exception:
        log.exception("航線顯示事件寫入失敗（不影響顯示本身）")
    return {"ok": True, "mission": row["name"]}


@router.delete("/missions/{mission_id}")
async def delete_mission(mission_id: str):
    r = await db.pool.execute("DELETE FROM missions WHERE id = $1", mission_id)
    if r.split()[-1] == "0":
        raise HTTPException(404, "無此路徑")
    return {"ok": True}  # waypoints 由 FK CASCADE 一併刪除


# ── 機上 5G 量測回傳（真機階段）────────────────────────────────────────────
# 設計見 doc/onboard-telemetry.md。兩條通道分工：
#   live  即時通道：只送最新一筆、失敗不重試、只更新 live state 不入庫
#   batch 記錄通道：送未確認的樣本、重試到成功、唯一的入庫路徑
# 分開的理由是鏈路差時小封包還擠得過去、大批次則否；共用一條通道會讓
# 「為完整性而重試」卡住即時性，操作員看到的是舊資料卻以為是現況。

class LinkSample(BaseModel):
    """機上一次採樣的完整結果。欄位對應 RM500Q-GL 的 AT+QENG 回應。"""
    drone_id: str | None = None     # 多機：這筆樣本屬於哪台（不填＝主機）
    seq: int | None = None          # 機上單調遞增序號，只用於批次確認，不入庫
    time: datetime                  # 機上採樣時刻，須含時區
    lat: float | None = None        # 採樣當下位置，機上從 PX4 取得後綁進同一筆
    lon: float | None = None
    alt_rel: float | None = None
    rsrp: float | None = None
    rsrq: float | None = None
    sinr: float | None = None       # 干擾研究主指標，取自 AT+QENG 而非 QMI 的 SNR
    cqi: int | None = None
    pci: int | None = None
    cell_id: int | None = None      # 全域識別碼 NCI/CGI，AT+QENG 的 <cellID>
    band: str | None = None
    nr_mode: str | None = None
    rtt_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_pct: float | None = None
    throughput_up_kbps: float | None = None
    throughput_down_kbps: float | None = None
    in_interference_zone: bool | None = None
    raw: dict | None = None         # modem 原始回應，便於事後追查


class LinkBatch(BaseModel):
    drone_id: str | None = None     # 省略則用目前註冊的無人機（單機情境）
    samples: list[LinkSample] = Field(min_length=1, max_length=1000)


def _resolve_drone(drone_id: str | None) -> str:
    resolved = drone_id or live.drone_id
    if not resolved:
        raise HTTPException(503, "系統尚未完成初始化，無人機未註冊")
    return resolved


def _require_modem_mode() -> None:
    """simulated 模式下拒收 push，避免兩個寫入者打架。

    模擬迴圈每秒更新 live.link 並寫 link_metrics；此時若還接受外部 POST，
    live state 會被兩邊輪流覆蓋、link_metrics 出現兩套來源混雜的資料。
    實際發生過：殘留的 fake-onboard-node 讓 simulated 模式廣播出 source=modem，
    診斷了一輪才發現。拒收並講明原因，好過靜默接受後產生混料。
    """
    if settings.link_source != "modem":
        raise HTTPException(
            409, f"link_source={settings.link_source}，push 端點僅在 modem 模式開放"
            "（模擬迴圈是這個模式下唯一的鏈路資料寫入者）")


def _require_aware(ts: datetime) -> datetime:
    """拒收沒有時區的時間戳。

    機上送來的時間是資料唯一的時間依據——沒有時區就無法確定它代表哪個瞬間，
    寫進 TIMESTAMPTZ 會被當成伺服器時區而靜默偏移。寧可拒收也不要污染資料。
    """
    if ts.tzinfo is None:
        raise HTTPException(422, f"時間戳必須含時區（收到 {ts.isoformat()}）")
    return ts


@router.post("/link-metrics/live", status_code=204)
async def link_metrics_live(s: LinkSample):
    """即時通道：更新 live state 並跑鏈路狀態機。**不寫資料庫。**

    不入庫是為了避免與記錄通道重複寫入——live 只負責顯示，記錄通道負責留存，
    職責不重疊就不需要去重邏輯。

    機上送失敗時不該重試：下一秒的新樣本本來就會取代這一筆，重試只會佔用
    本來就不夠的頻寬。
    """
    _require_modem_mode()
    _require_aware(s.time)
    # 多機：樣本自帶 drone_id 決定更新哪台的 live state（不填＝主機）
    from .state import fleet
    target = fleet.get(s.drone_id) if s.drone_id else live
    if target is None:
        raise HTTPException(404, f"未知的 drone_id：{s.drone_id}（該機尚未註冊）")
    # mode="json" 讓 datetime 變成 ISO 字串。live.link 會被 WebSocket 廣播出去，
    # 放進 datetime 物件會讓 json.dumps 拋錯而整個廣播迴圈死掉。
    m = s.model_dump(mode="json", exclude_none=False)
    m.pop("drone_id", None)
    m["source"] = "modem"
    target.link = m
    target.mark_link_seen()
    await link_transition(target, m)
    return Response(status_code=204)


@router.post("/link-metrics/batch")
async def link_metrics_batch(batch: LinkBatch):
    """記錄通道：唯一的入庫路徑。冪等，可安全重送。

    `session_id` 用樣本自帶的時間戳反查涵蓋它的架次，而非「當前架次」——
    補傳資料抵達時飛機可能早已上鎖。見 doc/onboard-telemetry.md。

    回應的 `accepted_seq` **包含落在架次外而被丟棄的樣本**，機上據此標記可刪除。
    若不回報這些，機上會永遠重送那些本來就不該保留的資料。
    """
    _require_modem_mode()
    drone_id = _resolve_drone(batch.drone_id)
    accepted, stored, duplicate, outside = [], 0, 0, 0

    for s in batch.samples:
        _require_aware(s.time)
        # 這裡不能用 mode="json"：time 要保持 datetime 才能寫進 TIMESTAMPTZ
        m = s.model_dump(exclude_none=False)
        m["source"] = "modem"
        session_id = await db.find_session_at(drone_id, s.time)
        if session_id is None:
            outside += 1                      # 架次外：等同 issues/004 的 gate，丟棄
        elif await db.insert_link_sample(drone_id, session_id, m):
            stored += 1
        else:
            duplicate += 1                    # 已存在，重送造成，視為成功
        if s.seq is not None:
            accepted.append(s.seq)

    return {"accepted_seq": accepted, "stored": stored,
            "duplicate": duplicate, "outside_session": outside}
