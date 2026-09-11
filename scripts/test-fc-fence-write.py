#!/usr/bin/env python3
"""飛控圍欄寫入的流程回歸（doc/route-planning-redesign.md §16.3）。

在指令服務的容器裡跑：

    docker cp scripts/test-fc-fence-write.py uav-command:/tmp/
    docker exec uav-command python3 /tmp/test-fc-fence-write.py

**假的是 `_run`，不是飛控**：這裡驗順序與分支（先關、最後開、失敗停在哪、
沒有圍欄就關掉）。協定本身在 ArduCopter SITL 上用實際的 mav.py 驗過（§16.6）。
"""
import asyncio
import sys

sys.path.insert(0, "/srv")
sys.path.insert(0, "/srv/libs")
from fastapi import HTTPException  # noqa: E402

from app import main, mav  # noqa: E402
import plan_check as pc  # noqa: E402

fails = []


def ck(name, cond, got=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  ← {got}" if not cond else ""))
    if not cond:
        fails.append(name)


class FakeFC:
    def __init__(self, vals, fail=None, clamp=None):
        self.vals, self.fail, self.clamp, self.calls = dict(vals), fail, clamp, []

    async def run(self, sysid, action, fn, *args, params=None):
        self.calls.append((fn.__name__, args))
        if self.fail and self.fail(fn, args):
            raise HTTPException(502, "假的失敗")
        if fn is mav.job_get_params:
            return {"values": {k: self.vals[k] for k in args[0] if k in self.vals},
                    "missing": [k for k in args[0] if k not in self.vals]}
        if fn is mav.job_set_params:
            self.vals.update(args[0])
            cl = [f"{k}：夾掉了" for k in args[0] if self.clamp and k in self.clamp]
            return {"written": dict(args[0]), "clamped": cl, "verified": not cl}
        if fn is mav.job_upload_mission:
            return {"uploaded": len(args[0]), "verified": True}
        raise AssertionError(fn.__name__)


async def _no_audit(*a, **k):
    return None

main._audit = _no_audit
AP, PX4 = 3, 12


def go(fc, fence, ap=AP):
    main._run = fc.run
    try:
        return asyncio.run(main._write_fc_fence(1, "plan", fence, ap)), None
    except HTTPException as e:
        return None, e


def sets(fc):
    return [a[0] for n, a in fc.calls if n == "job_set_params"]


HOME = {"lat": 24.7734, "lon": 121.0459}
FENCE = pc.fence_circle(HOME, 120, 30)
OK = {"FENCE_ENABLE": 1, "FENCE_ALT_MAX_TP": 1, "RTL_ALT_M": 2}

print("── 前提 ──")
ck("caps 把 3 認成 ArduPilot", main.caps.autopilot_name(AP) == "ardupilot",
   main.caps.autopilot_name(AP))

print("\n── 沒有圍欄：把飛控的圍欄關掉（U）──")
fc = FakeFC({"FENCE_ENABLE": 1})
r, e = go(fc, None)
ck("開著就寫成 0", sets(fc) == [{"FENCE_ENABLE": 0}], sets(fc))
ck("結果說已關閉", r and "已關閉" in r["summary"], r)
ck("航線傳失敗時要說圍欄已經關了", r and "關閉" in (r.get("fail_note") or ""), r)
fc = FakeFC({"FENCE_ENABLE": 0})
r, e = go(fc, None)
ck("本來就關著就不寫", sets(fc) == [] and "本來就關著" in r["summary"], (sets(fc), r))
fc = FakeFC({})
r, e = go(fc, None)
ck("讀不到 FENCE_ENABLE 就不傳", e is not None and e.status_code == 409, e)
fc = FakeFC({"FENCE_ENABLE": 1}, fail=lambda fn, a: fn is mav.job_set_params)
r, e = go(fc, None)
ck("關不掉就不傳，而且說關不掉", e is not None and "關不掉" in e.detail["msg"], e and e.detail)

print("\n── 有圍欄：先關、寫、傳形狀、最後才開 ──")
fc = FakeFC(OK)
r, e = go(fc, FENCE)
order = [(n, a[0] if n == "job_set_params" else (len(a[0]), a[1]) if n == "job_upload_mission" else None)
         for n, a in fc.calls]
ck("順序：讀 → 關 → 參數 → 形狀 → 開",
   [n for n, _ in order] == ["job_get_params", "job_set_params", "job_set_params",
                             "job_upload_mission", "job_set_params"], order)
ck("第一個寫的是 ENABLE=0", sets(fc)[0] == {"FENCE_ENABLE": 0}, sets(fc))
ck("最後一個寫的是 ENABLE=1", sets(fc)[-1] == {"FENCE_ENABLE": 1}, sets(fc))
ck("形狀是圍欄任務", order[3][1] == (1, mav.M.MAV_MISSION_TYPE_FENCE), order[3])
ck("結果帶摘要", r and r["written"] and "越界返航" in r["summary"], r)
fc = FakeFC({**OK, "FENCE_ENABLE": 0})
go(fc, FENCE)
ck("本來關著就不先寫 0", sets(fc)[0] != {"FENCE_ENABLE": 0}, sets(fc))

fc = FakeFC(OK, fail=lambda fn, a: fn is mav.job_upload_mission)
r, e = go(fc, FENCE)
ck("形狀傳失敗：不開、說停在關著",
   e is not None and "停在關著" in e.detail["msg"]
   and {"FENCE_ENABLE": 1} not in sets(fc), (e and e.detail, sets(fc)))
fc = FakeFC(OK, fail=lambda fn, a: fn is mav.job_set_params and a[0] == {"FENCE_ENABLE": 0})
r, e = go(fc, FENCE)
ck("連關都關不掉：說維持原本那份", e is not None and "維持原本那份" in e.detail["msg"],
   e and e.detail)
fc = FakeFC(OK, clamp={"FENCE_ALT_MAX"})
r, e = go(fc, FENCE)
ck("參數被夾：不開", e is not None and {"FENCE_ENABLE": 1} not in sets(fc),
   (e and e.detail, sets(fc)))
fc = FakeFC({**OK, "RTL_ALT_M": 30})
r, e = go(fc, FENCE)
ck("返航高度撞上限：一個都不寫", e is not None and e.status_code == 409 and sets(fc) == [],
   (e and e.detail, sets(fc)))

print("\n── 不是 ArduPilot ──")
fc = FakeFC(OK)
r, e = go(fc, None, PX4)
ck("沒有圍欄：不讀不寫、不出聲", fc.calls == [] and r["summary"] is None, (fc.calls, r))
fc = FakeFC(OK)
r, e = go(fc, FENCE, PX4)
ck("有圍欄：不寫，但說出來", fc.calls == [] and r["warnings"], (fc.calls, r))

print()
if fails:
    print(f"✗ {len(fails)} 項沒過：" + "、".join(fails))
    sys.exit(1)
print("全部通過")
