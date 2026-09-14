"""外部起飛時的任務：用呼叫端給的 UUID 建立，或掛進進行中的那一個（doc/external-live-api.md §2.3）。

錯誤一律帶 `code`＋`msg`（＋`how_to`）：這是對外端點，只給代碼的話呼叫端得來問是什麼意思。
"""
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg

TW = timezone(timedelta(hours=8))

_ACTIVE_OF = """
    SELECT m.id::text AS id, m.name FROM missions m
     WHERE m.ended_at IS NULL AND $1::uuid IN (
       SELECT md.drone_id FROM mission_drones md WHERE md.mission_id = m.id
       UNION
       SELECT sm.drone_id FROM squad_members sm WHERE sm.squad_id = m.squad_id)"""


class MissionError(Exception):
    def __init__(self, status: int, code: str, msg: str, how_to: list[str] | None = None):
        super().__init__(msg)
        self.status, self.code, self.msg, self.how_to = status, code, msg, how_to or []

    def detail(self) -> dict:
        return {"code": self.code, "msg": self.msg,
                **({"how_to": self.how_to} if self.how_to else {})}


def parse_id(raw) -> str | None:
    if raw is None or raw == "":
        return None
    try:
        return str(uuid.UUID(str(raw)))
    except ValueError:
        raise MissionError(422, "mission_id_invalid",
                           f"mission_id 要是 UUID，收到的是「{raw}」",
                           ["用 crypto.randomUUID() 或 uuid.uuid4() 產生"])


async def _pick_name(con, name: str | None, plan_name: str) -> str:
    if name and name.strip():
        n = name.strip()
        hit = await con.fetchrow(
            "SELECT id::text AS id, name FROM missions WHERE lower(name) = lower($1)", n)
        if hit:
            raise MissionError(409, "mission_name_taken",
                               f"已經有一個任務叫「{hit['name']}」（{hit['id']}），名稱不分大小寫比對",
                               ["換一個名字，或不給 mission_name 讓地面站自動命名"])
        return n
    now = datetime.now(TW)
    base = f"{plan_name} {now:%m-%d %H:%M}"
    for cand in (base, f"{base}:{now:%S}", *(f"{base}:{now:%S} #{i}" for i in range(2, 20))):
        if not await con.fetchval("SELECT 1 FROM missions WHERE lower(name) = lower($1)", cand):
            return cand
    return f"{base} {uuid.uuid4().hex[:6]}"


async def ensure(pool, mission_id: str | None, name: str | None,
                 drones: list[tuple[str, str]], plan_name: str) -> dict:
    """drones＝[(drone_id, 顯示名)] → {id, name, created}。

    * `mission_id` 對到進行中的任務：這幾台掛進去
    * `mission_id` 對到已結束的任務：409 `mission_ended`
    * 沒給 `mission_id`、而這幾台都已經在同一個進行中的任務：直接用那個任務
    * 其他情況建立新任務，標成外部建立（最後一台上鎖 3 秒後自動結束）
    """
    try:
        async with pool.acquire() as con:
            async with con.transaction():
                row = None
                if mission_id:
                    row = await con.fetchrow(
                        "SELECT id::text AS id, name, ended_at FROM missions WHERE id = $1::uuid",
                        mission_id)
                    if row is not None and row["ended_at"] is not None:
                        raise MissionError(
                            409, "mission_ended",
                            f"任務「{row['name']}」已在 {row['ended_at'].astimezone(TW):%m-%d %H:%M:%S} 結束",
                            ["要再飛就產生新的 UUID 當 mission_id"])
                busy = {}
                for did, dname in drones:
                    b = await con.fetchrow(_ACTIVE_OF, did)
                    if b is not None and b["id"] != mission_id:
                        busy[did] = (dname, b)
                if busy:
                    others = {b["id"] for _, b in busy.values()}
                    if mission_id is None and len(others) == 1 and len(busy) == len(drones):
                        b = next(iter(busy.values()))[1]
                        return {"id": b["id"], "name": b["name"], "created": False}
                    dname, b = next(iter(busy.values()))
                    raise MissionError(
                        409, "mission_busy",
                        f"這台機（{dname}）已經在進行中的任務「{b['name']}」（{b['id']}）裡，"
                        "一台機同一時間只能在一個任務",
                        ["等那個任務結束再起飛",
                         f"或改用 mission_id {b['id']} 起飛，把這一趟掛進那個任務"])
                if row is None:
                    mission_id = mission_id or str(uuid.uuid4())
                    final = await _pick_name(con, name, plan_name)
                    await con.execute(
                        "INSERT INTO missions (id, name, external) VALUES ($1::uuid, $2, true)",
                        mission_id, final)
                else:
                    final = row["name"]
                for did, _ in drones:
                    await con.execute(
                        "INSERT INTO mission_drones (mission_id, drone_id) "
                        "VALUES ($1::uuid, $2::uuid) ON CONFLICT DO NOTHING", mission_id, did)
    except asyncpg.UniqueViolationError as e:
        # 檢查與寫入之間別人搶先了：觸發器或名稱索引丟出來的訊息本身說得出撞到什麼
        code = "mission_busy" if "一台機一次只能執行一個任務" in str(e) else "mission_name_taken"
        raise MissionError(409, code, str(e))
    return {"id": mission_id, "name": final, "created": row is None}
