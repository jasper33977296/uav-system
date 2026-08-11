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

from . import capabilities as caps
from . import mav

log = logging.getLogger("command.group")

# 執行序列會用到的能力鍵——嚴格 gate 逐台檢查，非 "ok" 一律擋（issue 015 唯一真相）
_REQUIRED_CAPS = ["mission_upload", "arm", "takeoff", "mission_start"]
_FRESH_S = 5.0            # 心跳新鮮度門檻：router.drones 不會自己過期，gate 得自己判
_BASE_TAKEOFF_ALT = 10.0  # 分層起飛基準高度（相對）；逐台再加 layer×vsep


class GroupExecutor:
    def __init__(self, router, pool, build_items, audit, live_fn):
        self.router = router
        self.pool = pool
        self._build_items = build_items     # main.build_items（避免循環 import，注入）
        self._audit = audit                 # main._audit
        self._live = live_fn                # main._live（主機即時態，取地面海拔近似）
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
        cap, reasons = caps.capabilities_for(ap)
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
        ground_amsl = await self._ground_amsl()
        try:
            # Phase 1：UPLOAD（逐台材料化任務上傳＋回讀比對）
            for m in members:
                if abort.is_set():
                    return
                await self._set_phase(gid, m["drone_id"], "uploading")
                try:
                    items = await self._items_for(m["mission_id"])
                    await self._submit(mav.job_upload_mission, m["mav_sysid"], items)
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
                    res = await self._submit(mav.job_command, m["mav_sysid"], 400, [1.0])
                    if not res.get("accepted"):
                        raise mav.CommandError(f"arm 被拒（{res.get('result')}）")
                    await self._set_phase(gid, m["drone_id"], "armed")
                except Exception as e:
                    await self._set_phase(gid, m["drone_id"], "prearm_failed", self._err(e))
                    return await self._fail_group(gid, members, "解鎖失敗")

            # Phase 3：START（逐台 takeoff 到分層高度 → AUTO.MISSION）
            partial = False
            for m in members:
                if abort.is_set():
                    return
                await self._set_phase(gid, m["drone_id"], "starting")
                try:
                    alt_rel = _BASE_TAKEOFF_ALT + m["layer_index"] * vsep
                    amsl = (ground_amsl + alt_rel) if ground_amsl is not None else alt_rel
                    # NAV_TAKEOFF：param7=AMSL、經緯/偏航 NaN=原地。[收尾] 真機逐台
                    # 地面海拔＋等到達高度 gating（單機 mission_fly 的教訓），需 per-sysid alt。
                    await self._submit(mav.job_command, m["mav_sysid"], 22,
                                       [0.0, 0.0, 0.0, float("nan"), float("nan"),
                                        float("nan"), amsl])
                    await self._submit(mav.job_set_mode, m["mav_sysid"], "mission")
                    await self._set_phase(gid, m["drone_id"], "flying")
                except Exception as e:
                    # 空中單機失敗：該台 RTL、其他續飛 → group partial（§3）
                    await self._member_rtl(gid, m, self._err(e))
                    partial = True

            await self._set_status(gid, "partial" if partial else "flying")
        except Exception:
            log.exception("group %s 序列異常", gid)
            await self._set_status(gid, "aborted")
        finally:
            self.runs.pop(gid, None)

    async def _ground_amsl(self):
        """地面海拔近似。[收尾] 骨架用主機 /api/live 當代理，真機需 per-sysid alt。"""
        try:
            d = await self._live()
            if d.get("alt_msl") is not None and d.get("alt_rel") is not None:
                return d["alt_msl"] - d["alt_rel"]
        except Exception:
            pass
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
            await self._submit(mav.job_set_mode, m["mav_sysid"], "rtl")
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
                    await self._submit(mav.job_set_mode, sysid, "rtl")
                    await self._set_phase(gid, m["drone_id"], "rtl")
                    actions.append({"drone_id": m["drone_id"], "action": "rtl"})
                elif ph in ("arming", "armed", "uploaded"):   # 確定在地面 → disarm
                    await self._submit(mav.job_command, sysid, 400, [0.0])
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
                await self._submit(mav.job_set_mode, m["mav_sysid"], "rtl")
                await self._set_phase(gid, m["drone_id"], "rtl")
                actions.append({"drone_id": m["drone_id"], "action": "rtl"})
            except Exception as e:
                actions.append({"drone_id": m["drone_id"], "action": "rtl_failed",
                                "error": str(e)})
        await self._set_status(gid, "aborted")
        await self._audit(0, f"group_rtl:{gid}", {"actions": actions},
                          "accepted", "群組 RTL-all")
        return {"status": "rtl_all", "group_id": gid, "actions": actions}
