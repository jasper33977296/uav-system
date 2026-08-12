"""群組任務地面生成（issue 013-A；doc/group-missions-design.md）。

「統一路徑」＝一條 base → 地面生成 N 條**具體 materialized 任務**，每台加
高度分層（layer_index × vsep）。不做 runtime offset——具體化後可預檢、
可審視、PX4 直接飛。RTL 高度錯開是執行期 param（013-B），不在此。
"""
import json

from . import db, plan_check
from .config import settings
from .jsonsafe import dumps as jdumps


async def _wps(con, mission_id) -> list[dict]:
    rows = await con.fetch(
        "SELECT seq, lat, lon, alt, action, params FROM waypoints "
        "WHERE mission_id = $1 ORDER BY seq", mission_id)
    return [dict(r) for r in rows]


async def _materialize(con, name: str, wps: list[dict], dz: float) -> str:
    """建一條具體任務：base 航點高度 += dz（帶座標的導航項才加）。回 mission_id。"""
    row = await con.fetchrow(
        "INSERT INTO missions (name, created_by, kind) "
        "VALUES ($1, 'group-gen', 'generated') RETURNING id", name)
    mid = row["id"]
    await con.executemany(
        """INSERT INTO waypoints (mission_id, seq, lat, lon, alt, action, params)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        [(mid, w["seq"], w["lat"], w["lon"],
          (float(w["alt"]) + dz) if (w.get("alt") and w.get("lat")) else w["alt"],
          w["action"], w["params"]) for w in wps])
    return str(mid)


class GroupError(Exception):
    """建群組的可預期錯誤（回 422）。"""


async def _lookup_drones(ids: list[str]) -> dict[str, dict]:
    """drone_id → {name, mav_sysid}。非有效 UUID 或查無 → 不在回傳 map（呼叫端
    據此回 422）。用 text 比對避開 UUID 轉型例外，容忍前端傳非 UUID 字串。"""
    rows = await db.pool.fetch(
        "SELECT id::text AS id, name, mav_sysid FROM drones WHERE id::text = ANY($1)",
        ids)
    return {r["id"]: {"name": r["name"], "mav_sysid": r["mav_sysid"]} for r in rows}


async def create_group(name: str, mode: str, base_mission_id, drones: list[dict],
                       params: dict | None) -> dict:
    """drones：[{drone_id, layer_index?, mission_id?}]。unified＝從 base 依 layer
    展開、separate＝用各自 mission_id。**單一交易原子性**（失敗全回滾、不留
    半群組/孤兒任務）。回群組＋跨路徑衝突預檢。"""
    vsep = (params or {}).get("vsep_m", settings.group_vsep_m)
    lsep = (params or {}).get("lsep_m", settings.group_lsep_m)
    if vsep <= 0:
        raise GroupError("vsep_m 需 > 0（分層間隔＝分離要求；0 等於不分離，危險）")
    ids = [d["drone_id"] for d in drones]
    if len(set(ids)) != len(ids):
        raise GroupError("同一台機不可重複指派（每台一個分層）")
    used_params = {"vsep_m": vsep, "lsep_m": lsep,
                   "rtl_stagger_m": (params or {}).get("rtl_stagger_m",
                                                       settings.group_rtl_stagger_m)}
    # 先驗證每台都是真的機（group_assignments.drone_id 無 FK，不擋則會靜默
    # 插入幽靈指派、GET 才發現 drone_name 為空、UI 台數對不上——issue 013-A
    # 前端回饋 #3）。不存在的一律回 422 列名，不做部分成功。
    dmap = await _lookup_drones(ids)
    missing = [i for i in ids if i not in dmap]
    if missing:
        raise GroupError("找不到這些無人機（非有效機號）：" + "、".join(missing))
    assignments, paths = [], []
    async with db.pool.acquire() as con:
        async with con.transaction():
            g = await con.fetchrow(
                """INSERT INTO mission_groups (name, base_mission_id, mode, params)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                name, base_mission_id, mode, jdumps(used_params))
            gid = str(g["id"])
            base_wps = await _wps(con, base_mission_id) if mode == "unified" else None
            for i, d in enumerate(drones):
                layer = i if d.get("layer_index") is None else d["layer_index"]
                if mode == "unified":
                    mid = await _materialize(con, f"{name}·L{layer}", base_wps, layer * vsep)
                    wps = [{**w, "alt": (float(w["alt"]) + layer * vsep)
                            if (w.get("alt") and w.get("lat")) else w["alt"]}
                           for w in base_wps]
                else:
                    mid = d["mission_id"]
                    wps = await _wps(con, mid)
                await con.execute(
                    """INSERT INTO group_assignments (group_id, drone_id, mission_id, layer_index)
                       VALUES ($1, $2, $3, $4)""", gid, d["drone_id"], mid, layer)
                info = dmap[d["drone_id"]]
                assignments.append({"drone_id": d["drone_id"], "mission_id": mid,
                                    "layer_index": layer, "phase": "idle",
                                    "drone_name": info["name"],
                                    "mav_sysid": info["mav_sysid"]})
                paths.append({"label": f"L{layer}", "waypoints": wps})

    conflict = plan_check.check_group(paths, vsep, lsep)
    return {"group_id": gid, "name": name, "mode": mode, "status": "draft",
            "params": used_params, "assignments": assignments, "conflict": conflict}


async def get_group(gid: str) -> dict | None:
    g = await db.pool.fetchrow("SELECT * FROM mission_groups WHERE id = $1", gid)
    if g is None:
        return None
    rows = await db.pool.fetch(
        """SELECT ga.drone_id::text, ga.mission_id::text, ga.layer_index, ga.phase,
                  d.name AS drone_name, d.mav_sysid
           FROM group_assignments ga LEFT JOIN drones d ON d.id = ga.drone_id
           WHERE ga.group_id = $1 ORDER BY ga.layer_index""", gid)
    return {"group_id": str(g["id"]), "name": g["name"], "mode": g["mode"],
            "status": g["status"], "params": g["params"],
            "assignments": [dict(r) for r in rows]}


async def delete_group(gid: str) -> str:
    """刪除群組。只允許 draft（執行中/已飛過的不可刪，保留稽核）。回結果碼：
    'deleted' / 'not_found' / 'locked'。group_assignments 靠 ON DELETE CASCADE
    連帶清；materialized 任務（created_by='group-gen'）另清避免任務庫堆草稿。"""
    g = await db.pool.fetchrow("SELECT status FROM mission_groups WHERE id = $1", gid)
    if g is None:
        return "not_found"
    if g["status"] != "draft":
        return "locked"
    async with db.pool.acquire() as con:
        async with con.transaction():
            mids = [r["mission_id"] for r in await con.fetch(
                "SELECT mission_id FROM group_assignments WHERE group_id = $1", gid)]
            await con.execute("DELETE FROM mission_groups WHERE id = $1", gid)
            # 只清本群組地面生成的具體任務（separate 用的既有任務不動）
            if mids:
                await con.execute(
                    "DELETE FROM missions WHERE id = ANY($1) AND kind = 'generated'",
                    mids)
    return "deleted"
