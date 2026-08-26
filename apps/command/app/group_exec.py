"""群組任務執行器（issue 013-B；doc/group-missions-design.md §3/§7）。

兩階段提交＋伺服器端狀態機。**排程原子性全在伺服器**：execute 通過嚴格 gate
後非同步啟動、立即回 202；序列由本執行器跑到終態或全撤，前端斷線/關頁不影響
（中止只能透過 abort 端點，不是斷 HTTP）。即時態逐步寫入 DB（mission_groups.status
＋group_assignments.phase/error），backend GET /api/groups/{id} 直接反映、前端 1s 輪詢。

狀態機（§7.1）：
  group.status: draft → executing → flying → completed；
                gate_rejected／aborting→aborted／partial
  member.phase: idle→uploading→uploaded→arming→armed→starting→flying→landed；
                upload_failed／prearm_failed／rejected／rtl

骨架階段（本批）：以 1 真＋1 假驗指令流轉與失敗路徑（GROUND_ABORT 全撤、單機失敗
處置）——這些邏輯不需物理。**真實時序驗收**（arm ACK 時序、同時起飛 skew、到達
高度 gating、群組 RTL 高度錯開）需 2 台真 PX4，是本批收尾工作。下面標 [收尾] 的點
即待補。並發：逐台序列（單 socket；011 實測 N=2–3 序列足夠、~25ms/台）。
"""
import asyncio
import json
import logging
import time

from fastapi import HTTPException

from . import capabilities as caps
from . import guard_client, mav

log = logging.getLogger("command.group")

# 執行序列會用到的能力鍵——嚴格 gate 逐台檢查，非 "ok" 一律擋（issue 015 唯一真相）
_REQUIRED_CAPS = ["mission_upload", "arm", "takeoff", "mission_start"]
_FRESH_S = 5.0            # 心跳新鮮度門檻：router.drones 不會自己過期，gate 得自己判
_BASE_TAKEOFF_ALT = 10.0  # 分層起飛基準高度（相對）；逐台再加 layer×vsep


class GroupExecutor:
    def __init__(self, router, pool, build_items, audit):
        self.router = router
        self.pool = pool
        self._build_items = build_items     # main.build_items（避免循環 import，注入）
        self._audit = audit                 # main._audit
        # **刻意不注入 backend /api/live**：那個端點只回主機，拿它當全隊的地面
        # 海拔來源是錯的（見 _ground_amsl_for）。高度一律走 router 的 per-sysid。
        self.runs: dict[str, dict] = {}     # gid → {task, abort: asyncio.Event}

    # ── 載入群組（command 與 backend 共用同一 DB）──────────────────
    async def _load(self, gid: str):
        g = await self.pool.fetchrow(
            "SELECT id::text, name, mode, status, params FROM mission_groups WHERE id=$1",
            gid)
        if g is None:
            return None, None
        rows = await self.pool.fetch(
            """SELECT ga.drone_id::text, ga.mission_id::text, ga.layer_index,
                      d.mav_sysid, d.name AS drone_name
               FROM group_assignments ga LEFT JOIN drones d ON d.id = ga.drone_id
               WHERE ga.group_id = $1 ORDER BY ga.layer_index""", gid)
        return dict(g), [dict(r) for r in rows]

    # ── 嚴格 gate（同步、序列啟動前）───────────────────────────────
    def _gate_member(self, m: dict):
        """回 None＝通過；否則回擋下原因 dict（逐台結構化，前端原文顯示）。"""
        sysid = m.get("mav_sysid")
        if sysid is None:
            return {"reason": "未綁定 mav_sysid（該機未曾連線／未認機）"}
        d = self.router.drones.get(sysid)
        if not d or "addr" not in d:
            return {"reason": f"sysid {sysid} 未連線（command 未見心跳）"}
        if time.monotonic() - d.get("seen_mono", 0) > _FRESH_S:
            return {"reason": f"sysid {sysid} 心跳逾時 >{_FRESH_S:.0f}s（可能失聯）"}
        ap = caps.autopilot_name(d.get("autopilot"))
        cap, reasons = caps.capabilities_for(ap, d)
        for key in _REQUIRED_CAPS:
            if cap.get(key) != "ok":
                return {"reason": f"{key} 能力={cap.get(key)}", "autopilot": ap,
                        "capability": key, "hint": reasons.get(key, "")}
        return None

    async def _set_status(self, gid: str, status: str):
        await self.pool.execute(
            "UPDATE mission_groups SET status=$2 WHERE id=$1", gid, status)

    async def _set_phase(self, gid: str, drone_id: str, phase: str, error=None):
        await self.pool.execute(
            "UPDATE group_assignments SET phase=$3, error=$4, updated_at=now() "
            "WHERE group_id=$1 AND drone_id=$2",
            gid, drone_id, phase, json.dumps(error) if error else None)

    # ── 阻塞式 MAV 工作丟進 router 執行緒（不擋事件迴圈）────────────
    async def _submit(self, fn, sysid: int, *args, timeout: float = 35.0):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.router.submit(fn, sysid, *args, timeout=timeout))

    async def _submit_audited(self, sysid: int, action: str, fn, *args,
                              timeout: float = 35.0):
        """逐台指令＋留痕（指令史是實驗記錄的一部分）。群組執行器的每台
        upload/arm/takeoff/mode 都寫 command_log，失敗一樣留痕、可事後追軌跡。

        **守門也走這裡**（2026-08-26 補）：原本這條路直接呼叫底層 MAVLink，
        完全繞過機上守門——同一個危險操作，用單機介面會被擋、用群組介面
        直接執行，而多機的風險本來就更高（一次動好幾台）。

        守門是**逐台問**的：編隊裡每台機的狀態可能不同（一台還在地上、
        一台已經在飛），拿其中一台的判決套用到全部，就等於沒問。
        """
        try:
            await guard_client.ask_guard(sysid, action)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else \
                (e.detail or {}).get("msg", str(e.detail))
            await self._audit(sysid, f"group:{action}", {},
                              "rejected_guard", str(detail)[:400])
            raise
        try:
            res = await self._submit(fn, sysid, *args, timeout=timeout)
        except Exception as e:
            await self._audit(sysid, f"group:{action}", {}, "failed", str(e)[:400])
            raise
        ok = res.get("accepted", True) and res.get("verified", True)
        await self._audit(sysid, f"group:{action}", {},
                          "accepted" if ok else "rejected",
                          json.dumps(res, default=str)[:400])
        return res

    async def _items_for(self, mission_id: str):
        rows = await self.pool.fetch(
            "SELECT seq, lat, lon, alt, action, params FROM waypoints "
            "WHERE mission_id=$1 ORDER BY seq", mission_id)
        if not rows:
            raise mav.CommandError("材料化任務沒有航點")
        return self._build_items([dict(r) for r in rows])

    @staticmethod
    def _err(e) -> dict:
        """例外 → §7.1 error 形狀（沿用單機拒絕的 {msg,hint,autopilot_notes}）。"""
        return {"msg": str(e), "hint": getattr(e, "hint", ""),
                "autopilot_notes": getattr(e, "autopilot_notes", [])}

    # ── execute：嚴格 gate → 202 非同步啟動 ────────────────────────
    async def execute(self, gid: str) -> dict:
        g, members = await self._load(gid)
        if g is None:
            return {"error": "not_found"}
        if gid in self.runs:                       # 冪等：已在跑回現況
            return {"status": g["status"], "group_id": gid, "already": True}
        if g["status"] not in ("draft", "gate_rejected"):
            return {"error": "bad_status", "status": g["status"]}
        gated = []
        for m in members:
            r = self._gate_member(m)
            gated.append({"drone_id": m["drone_id"], "drone_name": m["drone_name"],
                          "mav_sysid": m["mav_sysid"], "ok": r is None,
                          **({} if r is None else r)})
        if not all(x["ok"] for x in gated):        # 任一擋下＝全不啟動（同步回 409）
            await self._set_status(gid, "gate_rejected")
            return {"rejected": True, "group_id": gid, "members": gated}

        await self._set_status(gid, "executing")
        for m in members:
            await self._set_phase(gid, m["drone_id"], "idle")
        abort = asyncio.Event()
        task = asyncio.create_task(self._sequence(gid, g, members, abort))
        self.runs[gid] = {"task": task, "abort": abort}
        await self._audit(0, f"group_execute:{gid}", {"members": len(members)},
                          "accepted", "序列啟動")
        return {"status": "executing", "group_id": gid,
                "members": [{"drone_id": m["drone_id"], "drone_name": m["drone_name"],
                             "mav_sysid": m["mav_sysid"],
                             "layer_index": m["layer_index"]} for m in members]}

    # ── 背景序列（兩階段提交）─────────────────────────────────────
    async def _sequence(self, gid, g, members, abort):
        params = g.get("params")
        if isinstance(params, str):
            params = json.loads(params)
        vsep = float((params or {}).get("vsep_m", 5.0))
        try:
            # Phase 1：UPLOAD（逐台材料化任務上傳＋回讀比對）
            for m in members:
                if abort.is_set():
                    return
                await self._set_phase(gid, m["drone_id"], "uploading")
                try:
                    items = await self._items_for(m["mission_id"])
                    await self._submit_audited(m["mav_sysid"], "upload",
                                               mav.job_upload_mission, items)
                    await self.pool.execute(
                        "UPDATE drones SET current_mission_id=$1 WHERE mav_sysid=$2",
                        m["mission_id"], m["mav_sysid"])
                    await self._set_phase(gid, m["drone_id"], "uploaded")
                except Exception as e:
                    await self._set_phase(gid, m["drone_id"], "upload_failed", self._err(e))
                    return await self._fail_group(gid, members, "上傳失敗")

            # Phase 2：ARM（全機）——任一失敗＝全撤（尚無機起飛，disarm 已解鎖者）
            for m in members:
                if abort.is_set():
                    return
                await self._set_phase(gid, m["drone_id"], "arming")
                try:
                    res = await self._submit_audited(m["mav_sysid"], "arm",
                                                     mav.job_command, 400, [1.0])
                    if not res.get("accepted"):
                        raise mav.CommandError(f"arm 被拒（{res.get('result')}）")
                    await self._set_phase(gid, m["drone_id"], "armed")
                except Exception as e:
                    await self._set_phase(gid, m["drone_id"], "prearm_failed", self._err(e))
                    return await self._fail_group(gid, members, "解鎖失敗")

            # Phase 3：START —— **同時起飛**（全體 takeoff → 等全體到高度 → 全體 MISSION）。
            # 逐台序列會讓後面的等前面爬完、起飛 skew 大；拆三步讓 N 台幾乎同時離地。
            # 「等到達目標高度才切 MISSION」是單機 mission_fly 的教訓（地面直接切 MISSION 會
            # 失敗）；per-sysid 高度由 command router 從 GLOBAL_POSITION_INT 存（不靠 backend）。
            partial = False
            airborne = []
            for m in members:               # 3a. 全體 takeoff（連發、不等爬升）
                if abort.is_set():
                    return
                await self._set_phase(gid, m["drone_id"], "starting")
                m["_alt_target"] = _BASE_TAKEOFF_ALT + m["layer_index"] * vsep
                g_amsl = self._ground_amsl_for(m["mav_sysid"])   # **逐台**，不是全隊一個
                amsl = ((g_amsl + m["_alt_target"]) if g_amsl is not None
                        else m["_alt_target"])   # NAV_TAKEOFF param7=AMSL、經緯/偏航 NaN=原地
                try:
                    await self._submit_audited(
                        m["mav_sysid"], f"takeoff:{m['_alt_target']:.0f}m",
                        mav.job_command, 22, [0.0, 0.0, 0.0, float("nan"),
                                              float("nan"), float("nan"), amsl])
                    airborne.append(m)
                except Exception as e:
                    await self._member_rtl(gid, m, self._err(e))
                    partial = True
            for m in airborne:              # 3b. 等各自到目標高度 80%（per-sysid alt gating）
                if abort.is_set():
                    return
                if await self._wait_alt(m["mav_sysid"], m["_alt_target"] * 0.8):
                    m["_alt_ok"] = True
                else:
                    alt = (self.router.drones.get(m["mav_sysid"]) or {}).get("alt_rel")
                    await self._member_rtl(gid, m, {
                        "msg": f"起飛後未達目標高度（目前 {alt} m / 目標 {m['_alt_target']:.0f} m）",
                        "hint": "機停在懸停、未進任務——檢查後可重試或 RTL"})
                    partial = True
            for m in airborne:              # 3c. 到高度者切 MISSION（已在空中，PX4 跳過任務內 takeoff）
                if abort.is_set() or not m.get("_alt_ok"):
                    continue
                try:
                    await self._submit_audited(m["mav_sysid"], "mode:mission",
                                               mav.job_set_mode, "mission")
                    await self._set_phase(gid, m["drone_id"], "flying")
                except Exception as e:
                    await self._member_rtl(gid, m, self._err(e))   # 空中單機失敗→RTL、其他續（§3）
                    partial = True

            await self._set_status(gid, "partial" if partial else "flying")
        except Exception:
            log.exception("group %s 序列異常", gid)
            await self._set_status(gid, "aborted")
        finally:
            self.runs.pop(gid, None)

    async def _wait_alt(self, sysid: int, target: float, timeout: float = 60.0) -> bool:
        """等該 sysid 相對高度達 target（m）才回 True；逾時 False。per-sysid alt 由
        command router 從 GLOBAL_POSITION_INT 存（mav._recv）。[收尾] 真機 timeout
        與 80% 門檻依實測調。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(0.5)
            alt = (self.router.drones.get(sysid) or {}).get("alt_rel")
            if alt is not None and alt >= target:
                return True
        return False

    def _ground_amsl_for(self, sysid):
        """該機的地面海拔（NAV_TAKEOFF param7=AMSL 用）。

        取代原本的 `_ground_amsl()`——那個讀 backend `/api/live`，而該端點只回
        **主機**，等於拿主機的地面海拔去算全隊每一台的絕對起飛高度。原註解已
        自承是骨架代理（「真機需 per-sysid alt」）；SITL 全機同址所以差值為零、
        看不出來，**真機分散部署就會差一整個地面高差**。

        改讀 router 的 per-sysid 紀錄（mav.py GLOBAL_POSITION_INT），與「等到達
        高度才切 MISSION」同源。拿不到就回 None＝退回相對高度語意。
        """
        d = (self.router.drones.get(sysid) or {}) if self.router else {}
        if d.get("alt_msl") is not None and d.get("alt_rel") is not None:
            return d["alt_msl"] - d["alt_rel"]
        return None

    async def _fail_group(self, gid, members, reason):
        """序列中（起飛前）失敗：全撤——已解鎖者 disarm，未動者留 idle。status=aborted。"""
        await self._set_status(gid, "aborting")
        await self._retract(gid, members)
        await self._set_status(gid, "aborted")
        await self._audit(0, f"group_auto_abort:{gid}", {"reason": reason},
                          "rejected", reason)
        self.runs.pop(gid, None)

    async def _member_rtl(self, gid, m, error=None):
        try:
            await self._submit_audited(m["mav_sysid"], "mode:rtl",
                                       mav.job_set_mode, "rtl")
        except Exception:
            log.exception("member %s RTL 失敗", m["drone_id"])
        await self._set_phase(gid, m["drone_id"], "rtl", error)

    # ── 撤銷（依當前 phase 選動作，冪等）──────────────────────────
    async def _retract(self, gid, members) -> list:
        rows = await self.pool.fetch(
            "SELECT drone_id::text, phase FROM group_assignments WHERE group_id=$1", gid)
        phases = {r["drone_id"]: r["phase"] for r in rows}
        actions = []
        for m in members:
            ph, sysid = phases.get(m["drone_id"]), m.get("mav_sysid")
            if sysid is None:
                continue
            try:
                if ph in ("starting", "flying"):        # 可能在空中 → RTL（不可 disarm）
                    await self._submit_audited(sysid, "mode:rtl", mav.job_set_mode, "rtl")
                    await self._set_phase(gid, m["drone_id"], "rtl")
                    actions.append({"drone_id": m["drone_id"], "action": "rtl"})
                elif ph in ("arming", "armed", "uploaded"):   # 確定在地面 → disarm
                    await self._submit_audited(sysid, "disarm", mav.job_command, 400, [0.0])
                    await self._set_phase(gid, m["drone_id"], "idle")
                    actions.append({"drone_id": m["drone_id"], "action": "disarm"})
                # uploading／idle：無需動作
            except Exception as e:
                actions.append({"drone_id": m["drone_id"], "action": "retract_failed",
                                "error": str(e)})
        return actions

    async def abort(self, gid: str) -> dict:
        """操作員主動全撤（緊急鈕）。冪等、依當前 phase 自動選動作。"""
        g, members = await self._load(gid)
        if g is None:
            return {"error": "not_found"}
        run = self.runs.get(gid)
        if run:
            run["abort"].set()
        await self._set_status(gid, "aborting")
        actions = await self._retract(gid, members)
        await self._set_status(gid, "aborted")
        await self._audit(0, f"group_abort:{gid}", {"actions": actions},
                          "accepted", "操作員全撤")
        return {"status": "aborted", "group_id": gid, "actions": actions}

    async def rtl(self, gid: str) -> dict:
        """群組 RTL-all（空中緊急）。冪等。[收尾] 返航高度逐台錯開 rtl_stagger_m
        （避免同時返航撞機）需 2 台真機驗；骨架先做基本 RTL-all。"""
        g, members = await self._load(gid)
        if g is None:
            return {"error": "not_found"}
        if gid in self.runs:
            self.runs[gid]["abort"].set()
        await self._set_status(gid, "aborting")
        actions = []
        for m in members:
            if m["mav_sysid"] is None:
                continue
            try:
                await self._submit_audited(m["mav_sysid"], "mode:rtl",
                                           mav.job_set_mode, "rtl")
                await self._set_phase(gid, m["drone_id"], "rtl")
                actions.append({"drone_id": m["drone_id"], "action": "rtl"})
            except Exception as e:
                actions.append({"drone_id": m["drone_id"], "action": "rtl_failed",
                                "error": str(e)})
        await self._set_status(gid, "aborted")
        await self._audit(0, f"group_rtl:{gid}", {"actions": actions},
                          "accepted", "群組 RTL-all")
        return {"status": "rtl_all", "group_id": gid, "actions": actions}
