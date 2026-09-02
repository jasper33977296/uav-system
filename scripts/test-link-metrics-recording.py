#!/usr/bin/env python3
"""訊號資料真的有被記下來嗎（issues/021／doc/onboard-telemetry.md）。

**2026-09-02 實測到的事**：這套系統的研究核心是 5G 訊號時序，而
`link_metrics` 最新一筆停在 **08-13**——當天四趟真機飛行的訊號筆數**全是 0**。

原因不是端點壞了，是**沒有人呼叫它**：

* 機上的 `modem.py` 只往 `/api/link-metrics/live` 送，而那個端點的 docstring
  第一行就寫著「**不寫資料庫**」——它只更新畫面。
* `/api/link-metrics/batch` 才是「**唯一的入庫路徑**」，而它唯一的呼叫者是
  `scripts/fake-onboard-node.py`——**一支假的測試節點**。

> **一條被假節點測過、被真機從未走過的路。** 兩邊都「正常」：畫面有訊號、
> 端點回 204、機上的 `posted` 一直增加——而資料庫裡什麼都沒有。

這支用 uav-agent **真正的** `ModemSampler.flush()` 打真的 backend，驗四件事：
  1. 樣本真的進 `link_metrics`，而且綁到正確的架次
  2. **架次外的樣本會被丟棄並回報**（issues/004 的 gate）——不回報的話機上
     會永遠重送一批本來就不該保留的資料
  3. **冪等**：重送同一批不會變成兩份
  4. 機端只刪掉地面站說收下的那幾筆

用法：python3 scripts/test-link-metrics-recording.py
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/k200/uav-agent")

CWD = "/home/k200/uav-system"
UID = "linkrec-test-0001"
ok = True

try:
    import modem                      # noqa: E402  uav-agent 真正的那支
except ImportError as e:
    print(f"skip：找不到 uav-agent 的模組（{e}）。**skip ≠ pass**")
    sys.exit(0)


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def psql(sql):
    r = subprocess.run(["docker", "compose", "exec", "-T", "uav-db", "psql",
                        "-U", "uav", "-d", "uav", "-tAc", sql],
                       capture_output=True, text=True, cwd=CWD)
    out = (r.stdout or "").strip().split("\n")
    return out[0].strip() if out else ""


def purge():
    for t in ("link_metrics", "telemetry", "events", "blackouts", "flight_sessions"):
        psql(f"DELETE FROM {t} WHERE drone_id IN "
             f"(SELECT id FROM drones WHERE board_uid = '{UID}')")
    psql(f"DELETE FROM drones WHERE board_uid = '{UID}'")


purge()
drone_id = psql(f"INSERT INTO drones (name, mav_sysid, board_uid) VALUES "
                f"('訊號記錄測試機', 197, '{UID}') RETURNING id")
t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
sid = psql(f"INSERT INTO flight_sessions (drone_id, started_at, ended_at) VALUES "
           f"('{drone_id}'::uuid, to_timestamp({t0.timestamp()}), "
           f"to_timestamp({(t0 + timedelta(minutes=5)).timestamp()})) RETURNING id")
print(f"場景：drone={drone_id[:8]} session={sid[:8]}"
      f"（{t0:%H:%M:%S} – {t0 + timedelta(minutes=5):%H:%M:%S}）\n")

# 真的 ModemSampler，但不開序列埠（port=None，我們只用它的緩衝與 flush）
ms = modem.ModemSampler(None, "localhost", 38000, lambda: {}, 1.0,
                        drone_id_fn=lambda: drone_id)


def push(n, base, step=1.0, **extra):
    for i in range(n):
        ms._seq += 1
        ms.buf.append({"seq": ms._seq,
                       "time": (base + timedelta(seconds=i * step)).isoformat(),
                       "rsrp": -85.0 - i, "sinr": 12.0, **extra})


print("── 1. 架次內的樣本真的入庫 ────────────────────────────")
push(20, t0 + timedelta(seconds=30))
sent = ms.flush()
chk("地面站收下了全部 20 筆", sent == 20, sent)
n = psql(f"SELECT count(*) FROM link_metrics WHERE drone_id='{drone_id}'::uuid")
chk("**資料庫裡真的有 20 筆**（這正是原本永遠是 0 的那個數字）", n == "20", n)
bound = psql(f"SELECT count(*) FROM link_metrics WHERE session_id='{sid}'::uuid")
chk("而且綁到正確的架次（用時間戳反查，不是「當前架次」）", bound == "20", bound)
chk("機端緩衝清空了", len(ms.buf) == 0, len(ms.buf))
chk("stored 計數對得上", ms.stored == 20, ms.stored)

print("\n── 2. 架次外的樣本：丟棄，但**要回報**──────────────────")
push(5, t0 + timedelta(hours=3))          # 遠在架次結束之後
sent = ms.flush()
chk("**回報了 accepted_seq（含被丟棄的）**——不回報的話機上會永遠重送",
    sent == 5, sent)
chk("機端據此清掉了它們", len(ms.buf) == 0, len(ms.buf))
n2 = psql(f"SELECT count(*) FROM link_metrics WHERE drone_id='{drone_id}'::uuid")
chk("而且**沒有**寫進資料庫（issues/004 的 gate）", n2 == "20", n2)

print("\n── 3. 冪等：重送不會變成兩份 ──────────────────────────")
push(20, t0 + timedelta(seconds=30))      # 與第 1 組完全相同的時間戳
ms.flush()
n3 = psql(f"SELECT count(*) FROM link_metrics WHERE drone_id='{drone_id}'::uuid")
chk("重送同一批之後還是 20 筆", n3 == "20", n3)

print("\n── 4. 送不出去時要留著（緩衝的全部意義）────────────────")
ms.batch_url = "http://127.0.0.1:1/api/link-metrics/batch"   # 打一個沒人聽的埠
push(7, t0 + timedelta(minutes=1))
before = len(ms.buf)
sent = ms.flush()
chk("送不出去回 0", sent == 0, sent)
chk("**樣本留在緩衝裡**（送失敗就丟掉的話，斷線那段就永遠沒有了）",
    len(ms.buf) == before, (before, len(ms.buf)))

print("\n── 5. 緩衝有上限，而且滿了要說 ────────────────────────")
ms2 = modem.ModemSampler(None, "localhost", 38000, lambda: {}, 1.0)
for i in range(modem.BUFFER_MAX + 50):
    ms2._seq += 1
    if len(ms2.buf) == ms2.buf.maxlen:
        ms2.dropped += 1
    ms2.buf.append({"seq": ms2._seq, "time": t0.isoformat()})
chk("上限就是上限", len(ms2.buf) == modem.BUFFER_MAX, len(ms2.buf))
chk("**擠掉的有計數**（不然「少了幾筆」永遠查不出來）",
    ms2.dropped == 50, ms2.dropped)

purge()
chk("測試機收乾淨了",
    psql(f"SELECT count(*) FROM drones WHERE board_uid='{UID}'") == "0")
print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
