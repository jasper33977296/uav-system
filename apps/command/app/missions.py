"""外部起飛時的任務（doc/external-live-api.md §2.3）。

**每一次請求都是新的任務**（issues/061，使用者 2026-09-23 裁定）：
「同一條路徑飛三趟」是三件事，不是一件事飛三次；疊在一個任務底下，
`ext/missions/{id}` 的「比較這趟與那趟」就拿不出來。

現階段**一個任務綁定一條路徑**（前端與外部都一樣）——設計上一個任務可以含多條路徑，
但那還沒有被任何流程用到，先把它綁死並且記在 `missions.plan_id` 上，
畫面與 API 才說得出「這個任務飛的是哪一條」。

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
                 drones: list[tuple[str, str]], plan_name: str,
                 plan_id: str | None = None) -> dict:
    """drones＝[(drone_id, 顯示名)] → {id, name, created}。

    **一律建立新任務**（061）：

    * `mission_id` 給了而且**沒被用過**：拿它當新任務的編號（外部要對得上自己的請求）
    * `mission_id` 已經存在（不論進行中或已結束）：409 `mission_id_used`
      ——這一趟是新的一件事，要用新的 UUID
    * 這幾台機還開著的任務**會被結束掉**（資料庫的不變式：一台機一次只能在一個任務），
      並在回傳的 `replaced` 裡列出來——**默默結束別人的任務是不行的**
    """
    try:
        async with pool.acquire() as con:
            async with con.transaction():
                if mission_id:
                    row = await con.fetchrow(
                        "SELECT id::text AS id, name, ended_at FROM missions WHERE id = $1::uuid",
                        mission_id)
                    if row is not None:
                        when = (f"已在 {row['ended_at'].astimezone(TW):%m-%d %H:%M:%S} 結束"
                                if row["ended_at"] else "還在進行中")
                        raise MissionError(
                            409, "mission_id_used",
                            f"mission_id {mission_id} 已經是任務「{row['name']}」（{when}）。"
                            "每一次執行都是新的一個任務——同一條路徑飛多趟，那是多件事",
                            ["用新的 UUID（crypto.randomUUID()／uuid.uuid4()）再送一次",
                             "或不給 mission_id，讓地面站產生"])
                # **先把還開著的任務結束掉**：資料庫的不變式是「一台機一次只能在一個
                # 任務」，而這一趟是新的一件事。結束了誰要說出來（回傳的 replaced）
                replaced = []
                for did, dname in drones:
                    b = await con.fetchrow(_ACTIVE_OF, did)
                    if b is None:
                        continue
                    await con.execute(
                        "UPDATE missions SET ended_at = now() "
                        "WHERE id = $1::uuid AND ended_at IS NULL", b["id"])
                    replaced.append({"id": b["id"], "name": b["name"], "drone": dname})
                mission_id = mission_id or str(uuid.uuid4())
                final = await _pick_name(con, name, plan_name)
                await con.execute(
                    "INSERT INTO missions (id, name, external, plan_id) "
                    "VALUES ($1::uuid, $2, true, $3::uuid)",
                    mission_id, final, plan_id)
                for did, _ in drones:
                    await con.execute(
                        "INSERT INTO mission_drones (mission_id, drone_id) "
                        "VALUES ($1::uuid, $2::uuid) ON CONFLICT DO NOTHING", mission_id, did)
    except asyncpg.UniqueViolationError as e:
        # 檢查與寫入之間別人搶先了：觸發器或名稱索引丟出來的訊息本身說得出撞到什麼
        code = "mission_busy" if "一台機一次只能執行一個任務" in str(e) else "mission_name_taken"
        raise MissionError(409, code, str(e))
    return {"id": mission_id, "name": final, "created": True,
            **({"replaced": replaced} if replaced else {})}
