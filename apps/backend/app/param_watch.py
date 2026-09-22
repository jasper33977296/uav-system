"""飛控參數變了要說出來（issues/058 A）。

2026-09-21：飛控裡存著一個**沒有人記得的六點多邊形圍欄**，機子站在它外面、
解不了鎖，排查花了數小時。畫面上只有飛控的原句 `PreArm: Vehicle breaching
Polygon fence`——因為我們**不記錄參數**，所以說不出「它變了」，更說不出
「什麼時候、是不是我們改的」。

做法：每台機的每個參數存「最後一次確認的值」（`drone_params`）。收到
`PARAM_VALUE` 先緩衝，靜下來 `QUIET_S` 之後一次比對：

* **這台機第一次建檔**：全部寫進去，**只發一則**「記下 N 個參數」——
  代理開機讀一輪就是一千多個，逐則發事件會把事件流淹掉（058 原文的提醒）
* 之後**值變了**才發 `param_changed`，帶舊值、新值、**上次確認舊值的時刻**
  （變更發生在那之後、這次讀到之前——**我們不知道確切時間就不要編一個**）
* **是不是本系統改的**：查 `command_log` 裡這段期間有沒有接受過的
  `param_set` 寫到同一個名字。沒有就說「不是經由地面站的指令服務」——
  **不說「別人改的」**：機上的 `set-fc-params.py` 也是我們的，它直接寫飛控、
  不經過指令服務
* 一次變太多（`MANY`）就併成一則，否則一次參數重設會噴幾百則

基準存在 DB：後端重啟後重讀，**我們不在的時候被改的**也比得出來。
"""
import asyncio
import logging
import math
from datetime import datetime, timezone

from . import db
from .ws import manager

log = logging.getLogger(__name__)

#: 多久沒有新的 PARAM_VALUE 才比對。整輪讀取約 3 秒、一千多筆，逐筆比對
#: 會在讀到一半時就開始發事件
QUIET_S = 2.0
#: 一次變超過這麼多個就併成一則
MANY = 10
#: 彙總事件裡最多列幾個名字（其餘只給數字）
LIST_MAX = 100


def same(a, b) -> bool:
    """同一個值嗎？MAVLink 參數值是 float32，解碼後可能有極小的表示差——
    那不是「變了」。NaN 與 NaN 算相同（NaN 是合法的參數值）。"""
    if a is None or b is None:
        # 架次快照（param_sets）是 JSONB，NaN 在那裡存成 null——拿快照當基準時，
        # null 對 NaN 是同一個值，不然每個 NaN 參數都會被報成「變了」
        other = b if a is None else a
        return other is None or (isinstance(other, float) and math.isnan(other))
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


def diff(known: dict, incoming: dict):
    """`known`：名稱 → (值, 上次確認時刻)；`incoming`：名稱 → 值。

    回傳 `(changed, added)`：
    `changed` ＝ [(名稱, 舊值, 新值, 上次確認時刻)]，依名稱排序；
    `added`   ＝ 以前沒見過的名稱（韌體更新會長出新參數），依名稱排序。
    **只比 incoming 有的**：沒讀到的參數不代表它消失了，可能只是還沒收到。
    """
    changed, added = [], []
    for name in sorted(incoming):
        if name not in known:
            added.append(name)
            continue
        old, when = known[name]
        if not same(old, incoming[name]):
            changed.append((name, old, incoming[name], when))
    return changed, added


def attribute(changes, log_rows):
    """每個變更是不是經由指令服務寫的。

    `log_rows`：`command_log` 裡 accepted 的 `param_set`，`[(id, time, params)]`。
    一筆變更算「本系統改的」要同時成立：那筆寫入**在上次確認舊值之後**、
    寫的是**同一個名字**、而且**寫的值就是現在讀到的值**。只對名字不對值，
    會把「我們寫了 5、之後有人又改成 7」說成是我們。
    回傳 名稱 → command_log.id（沒有就不在裡面）。
    """
    out = {}
    for name, _old, new, since in changes:
        for rid, t, params in log_rows:
            if since is not None and t < since:
                continue
            if name in params and same(params[name], new):
                out[name] = rid        # 取最後一筆（log_rows 依時間排序）
    return out


class ParamWatch:
    def __init__(self):
        self._known = {}        # drone_id → {name: (value, confirmed_at)}；None＝還沒載入
        self._pending = {}      # drone_id → {name: value}
        self._meta = {}         # drone_id → (drone_name, sysid)
        self._task = {}         # drone_id → 等著比對的 task
        #: 基準是從哪一趟的快照借來的（drone_id → 那一趟的解鎖時刻）
        self._seed_at = {}

    def on_param(self, st, name, value) -> None:
        """rx worker 收到一筆 PARAM_VALUE。**不等 DB**：只放進緩衝、排程比對。"""
        did = st.drone_id
        if not did:
            return              # 還沒認領到機體記錄——不知道是誰的，不能比
        self._pending.setdefault(did, {})[name] = value
        self._meta[did] = (st.drone_name, st.sysid)
        t = self._task.get(did)
        if t is not None and not t.done():
            t.cancel()          # 還在讀：重新計時，讀完一輪才比
        self._task[did] = asyncio.create_task(self._later(did))

    async def _later(self, did):
        try:
            await asyncio.sleep(QUIET_S)
        except asyncio.CancelledError:
            return
        try:
            await self.flush(did)
        except Exception:
            # 背景 task 的例外沒人接就會安靜消失（db.snapshot_params_for_session 的教訓）
            log.exception("參數比對失敗（不影響遙測）")

    async def _load(self, did):
        rows = await db.pool.fetch(
            "SELECT name, value, confirmed_at FROM drone_params WHERE drone_id = $1", did)
        if rows:
            return {r["name"]: (r["value"], r["confirmed_at"]) for r in rows}
        return await self._seed(did)

    async def _seed(self, did):
        """還沒有基準：拿這台機**最近一趟解鎖時的參數快照**（`param_sets`，021）。

        沒有這一步的話，第一次連上只能「建檔」，**上次飛完到現在被改過的看不出來**
        ——2026-09-21 那個圍欄正是這種：飛之間被改的。快照是完整的一份
        （抓不完整就不綁，見 db.snapshot_params_for_session），上次確認時刻就是
        那一趟的解鎖時刻。**事件要說清楚對照的是快照**，不是我們一路看著的值。
        """
        r = await db.pool.fetchrow(
            """SELECT s.started_at, ps.params FROM flight_sessions s
               JOIN param_sets ps ON ps.id = s.param_set_id
               WHERE s.drone_id = $1 ORDER BY s.started_at DESC LIMIT 1""", did)
        if not r:
            return {}
        import json
        params = r["params"]
        if isinstance(params, str):
            params = json.loads(params)
        at = r["started_at"]
        self._seed_at[did] = at
        return {n: (_num(v), at) for n, v in params.items()}

    async def flush(self, did):
        incoming = self._pending.pop(did, {})
        if not incoming:
            return
        if self._known.get(did) is None:
            self._known[did] = await self._load(did)
        known = self._known[did]
        first = not known
        changed, added = diff(known, incoming)
        name, sysid = self._meta.get(did, (None, None))

        # **先發事件，再寫基準**。反過來的話，事件寫入失敗時新值已經成了
        # 「已知」，下一輪就再也比不出來——那個變更永遠不會被說出來。
        # 這個順序的代價是「事件寫了、基準沒寫成」時下一輪會再說一次：
        # **重複一則比漏掉一則好**（與 057 的 notice 同一條）。事件失敗就讓例外
        # 往上走，基準保持舊的
        if first:
            await self._event(did, name, "info", "param_baseline", {
                "count": len(incoming),
                "note": "第一次記下這台機的參數。之後值變了才說得出來——"
                        "這之前被改過的，這裡看不出來"})
        else:
            if added:
                await self._event(did, name, "info", "params_added", {
                    "count": len(added), "names": added[:LIST_MAX],
                    "note": "以前沒見過的參數（多半是韌體更新）——沒有舊值可比"})
            if changed:
                await self._report(did, name, sysid, changed)
        await self._store(did, incoming, {c[0] for c in changed}, known)

    async def _report(self, did, name, sysid, changed):
        via = attribute(changed, await self._command_log(did, sysid, changed))
        if len(changed) <= MANY:
            for n, old, new, since in changed:
                ours = n in via
                await self._event(did, name, "info" if ours else "warning",
                                  "param_changed", {
                    "name": n, "old": _num(old), "new": _num(new),
                    "last_confirmed_at": since.isoformat() if since else None,
                    "via_command": via.get(n),
                    **self._compared_to(did, since),
                    "note": ("經由地面站的指令服務改的" if ours else
                             "不是經由地面站的指令服務改的——可能是 QGC／Mission Planner、"
                             "機上腳本（含 set-fc-params.py）或飛控自己")})
            return
        theirs = [c for c in changed if c[0] not in via]
        await self._event(did, name, "warning" if theirs else "info", "params_changed", {
            "count": len(changed), "not_via_command": len(theirs),
            "changes": [{"name": n, "old": _num(o), "new": _num(v)}
                        for n, o, v, _ in changed[:LIST_MAX]],
            **self._compared_to(did, changed[0][3]),
            "note": "一次變了很多個——參數重設、載入整份設定檔、或換了韌體"})

    def _compared_to(self, did, since):
        """舊值是從架次快照借來的話要說出來：那是「上一趟解鎖時」的值，
        不是我們一路看著的。兩者的「上次確認」意思不同。"""
        at = self._seed_at.get(did)
        if at is None or since != at:
            return {}
        return {"compared_to": "session_snapshot",
                "compared_note": f"對照的是 {at.isoformat()} 那一趟解鎖時的參數快照"}

    async def _store(self, did, incoming, changed_names, known):
        now = datetime.now(timezone.utc)
        await db.pool.executemany(
            """INSERT INTO drone_params (drone_id, name, value, confirmed_at, changed_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (drone_id, name) DO UPDATE
                 SET value = EXCLUDED.value, confirmed_at = EXCLUDED.confirmed_at,
                     changed_at = COALESCE(EXCLUDED.changed_at, drone_params.changed_at)""",
            [(did, n, _num(v), now, now if n in changed_names else None)
             for n, v in incoming.items()])
        # 記憶體裡的基準**在 DB 寫成之後**才換：寫失敗的話兩邊都還是舊的，
        # 下一輪會再比一次
        for n, v in incoming.items():
            known[n] = (_num(v), now)

    async def _command_log(self, did, sysid, changed):
        """這段期間經由指令服務接受過的參數寫入。**查不到就當沒有**——但那時
        每筆都會被說成「不是經由指令服務」，所以查詢失敗要留痕。"""
        since = min((c[3] for c in changed if c[3] is not None), default=None)
        try:
            rows = await db.pool.fetch(
                """SELECT id, time, params, detail FROM command_log
                   WHERE action = 'param_set' AND result = 'accepted'
                     AND (drone_id = $1 OR (drone_id IS NULL AND sysid = $2))
                     AND ($3::timestamptz IS NULL OR time >= $3)
                   ORDER BY time""", did, sysid, since)
        except Exception:
            log.exception("查指令紀錄失敗——這批參數變更的來源無法判斷")
            return []
        return [(r["id"], r["time"], w) for r in rows
                if (w := written_of(r["params"], r["detail"]))]

    async def _event(self, did, drone_name, sev, typ, detail):
        ev = await db.insert_event(did, None, sev, typ, detail, source="system")
        ev["drone"] = drone_name
        await manager.broadcast({"type": "event", "event": ev})


def written_of(params, detail) -> dict:
    """一筆 param_set 紀錄**實際寫進飛控**的值。

    先看 `detail.written`：那是寫入後逐個讀回的結果，**飛控夾過的值也照實在裡面**
    （`params` 是呼叫端要的值，兩者可能不同）。`params` 裡還混著呼叫端自己的
    欄位（實測有 `why: "fc_fence"`），直接拿來比會把非參數名當參數。
    `detail` 太長時會被截斷而解不開，那時退回 `params` 裡**數值的**欄位。
    """
    import json
    try:
        d = json.loads(detail) if isinstance(detail, str) else detail
        w = d.get("written") if isinstance(d, dict) else None
        if isinstance(w, dict) and w:
            return w
    except ValueError:
        pass
    try:
        p = json.loads(params) if isinstance(params, str) else params
    except ValueError:
        return {}
    if not isinstance(p, dict):
        return {}
    return {k: v for k, v in p.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


watch = ParamWatch()
