#!/usr/bin/env python3
"""013-B 執行器驗證：stub router ＋ 真 DB（issue 013-B）。

不需真機/物理——PM 定：指令流轉、狀態機、全撤、單機失敗處置這些邏輯本來就
不需物理。stub 掉 MAV 送出（router.submit 回罐頭 ACK、可注入失敗），用真 pool
驗真實 phase/status 寫入。跑在 command 容器內：python3 test-group-exec.py <gid>
"""
import asyncio
import sys
import time

sys.path.insert(0, "/srv")
import asyncpg

from app import group_exec
from app.config import settings

GID = sys.argv[1]


class StubRouter:
    def __init__(self, sysids):
        # sysid → 新鮮的 px4 機（autopilot=12 → 能力全 ok）
        # alt_rel 給高值、**故意不給 landed_state** → START 的離地判定走
        # 「機端沒送 → 退回高度判準」那條分支即刻通過（stub 不模擬爬升；
        # 真爬升在假機 live 測）
        self.drones = {s: {"addr": ("127.0.0.1", 1000 + s),
                           "seen_mono": time.monotonic(), "autopilot": 12,
                           "alt_rel": 100.0}
                       for s in sysids}
        self.calls = []
        self.fail = {}          # (sysid, fn_name) → Exception

    def submit(self, fn, sysid, *args, timeout=30):
        self.calls.append((fn.__name__, sysid))
        key = (sysid, fn.__name__)
        if key in self.fail:
            raise self.fail[key]
        n = fn.__name__
        if n == "job_upload_mission":
            return {"uploaded": len(args[0]), "verified": True}
        if n == "job_set_mode":
            return {"accepted": True, "mode_engaged": True}
        if n == "job_arm_prep":
            return {"needed": False}                    # PX4：不需要前置
        # job_command（arm）與 job_takeoff_cmd 都攤平回 accepted
        return {"result": "ACCEPTED", "accepted": True, "attempts": 1}


async def _audit(*a, **k):
    pass


def _build_items(wps):
    return [{"seq": i} for i, _ in enumerate(wps)]


async def reset(pool):
    await pool.execute("UPDATE mission_groups SET status='draft' WHERE id=$1", GID)
    await pool.execute(
        "UPDATE group_assignments SET phase='idle', error=NULL WHERE group_id=$1", GID)


async def phases(pool):
    rows = await pool.fetch(
        "SELECT d.name, ga.phase FROM group_assignments ga JOIN drones d ON d.id=ga.drone_id "
        "WHERE ga.group_id=$1 ORDER BY ga.layer_index", GID)
    return {r["name"]: r["phase"] for r in rows}


async def status(pool):
    return await pool.fetchval("SELECT status FROM mission_groups WHERE id=$1", GID)


async def run_seq(ex, gid):
    r = await ex.execute(gid)
    t = ex.runs.get(gid, {}).get("task")
    if t:
        await t
    return r


ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


async def main():
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)

    # ── A. Happy path：2 台 upload→arm→start→flying ──────────────
    await reset(pool)
    router = StubRouter([2, 3])
    ex = group_exec.GroupExecutor(router, pool, _build_items, _audit)
    r = await run_seq(ex, GID)
    ph, st = await phases(pool), await status(pool)
    check("A execute 回 executing", r.get("status") == "executing")
    check("A 終態 status=flying", st == "flying", f"(got {st})")
    check("A 兩台都 flying", all(v == "flying" for v in ph.values()), str(ph))
    # 每台應有 upload、arm_prep、arm(400)、takeoff、setmode 五步 → 逐台序列
    names = [c[0] for c in router.calls]
    check("A 呼叫序列含 upload/arm_prep/arm/takeoff/setmode×2",
          names.count("job_upload_mission") == 2 and names.count("job_arm_prep") == 2
          and names.count("job_command") == 2 and names.count("job_takeoff_cmd") == 2
          and names.count("job_set_mode") == 2, str(names))
    # **群飛不得自己組起飛指令**：起飛一定走 job_takeoff_cmd（→ driver.takeoff_plan）。
    # 原本這裡是 job_command(22, [...NaN..., g_amsl + alt])——PX4 的語意寫死在
    # 群飛路徑裡，ArduPilot 三條方言全錯，而能力門還會放行（2026-09-07）
    check("A 起飛走驅動（無裸 job_command 22）",
          names.count("job_takeoff_cmd") == 2, str(names))

    # ── B. Gate reject：sysid 2 不在線 → 全不啟動 ─────────────────
    await reset(pool)
    router = StubRouter([3])          # 只有 3 在線，2 缺席
    ex = group_exec.GroupExecutor(router, pool, _build_items, _audit)
    r = await ex.execute(GID)
    check("B gate 擋下（rejected）", r.get("rejected") is True)
    check("B status=gate_rejected", await status(pool) == "gate_rejected")
    check("B 未送任何指令（序列未啟動）", len(router.calls) == 0)
    check("B 逐台原因：uav-s2 標 not-ok", any(
        not m["ok"] and m["mav_sysid"] == 2 for m in r["members"]))

    # ── C. Arm 失敗：sysid 3 arm 被拒 → 該台 prearm_failed、全撤（2 已 arm→disarm）──
    await reset(pool)
    router = StubRouter([2, 3])
    router.fail[(3, "job_command")] = Exception("arm 被拒（DENIED）")  # 只擋 3 的 arm
    ex = group_exec.GroupExecutor(router, pool, _build_items, _audit)
    await run_seq(ex, GID)
    ph, st = await phases(pool), await status(pool)
    check("C status=aborted（自動全撤）", st == "aborted", f"(got {st})")
    check("C uav-s3 prearm_failed", ph.get("uav-s3") == "prearm_failed", str(ph))
    check("C uav-s2 被撤回 idle（已 arm→disarm）", ph.get("uav-s2") == "idle", str(ph))

    # ── D. 操作員 abort：起飛後全撤 → RTL ────────────────────────
    await reset(pool)
    router = StubRouter([2, 3])
    ex = group_exec.GroupExecutor(router, pool, _build_items, _audit)
    await run_seq(ex, GID)                       # 先飛起來
    r = await ex.abort(GID)
    ph, st = await phases(pool), await status(pool)
    check("D abort status=aborted", st == "aborted")
    check("D 兩台 flying→rtl", all(v == "rtl" for v in ph.values()), str(ph))
    check("D abort 回報 rtl actions", all(a["action"] == "rtl" for a in r["actions"]))

    await reset(pool)      # 收尾：留成 draft 給後續清理
    await pool.close()
    print("RESULT:", "ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


asyncio.run(main())
