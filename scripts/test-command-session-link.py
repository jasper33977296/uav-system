#!/usr/bin/env python3
"""指令留痕要接得回「哪一台機、哪一趟飛行」（2026-09-06）。

## 起因

`command_log` 307 筆歷史紀錄裡 `drone_id` 填了 **0 筆**——欄位 9/2 就加了、
外鍵也建了，但沒有任何寫入端在填。於是 9/2 補上的「被系統擋下的也要留痕」
那些痕，掛不到任何一趟飛行上；匯出檔裡連 commands 這一段都沒有。

## 這支測什麼

真的打 command 服務的 HTTP 端點（不是呼叫函式），因為要驗的正是**寫入端**
有沒有在填。用一台臨時機，跑完刪掉並確認沒有殘留——2026-09-02 就是測試機
沒清乾淨，害即時頁面多出一台不存在的 uav-s42。

用法：`python3 scripts/test-command-session-link.py`
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

SYSID = 199                       # 不與真機重疊
NAME = f"zz-test-cmdlink-{uuid.uuid4().hex[:6]}"
CMD = "http://localhost:38001"
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def sql(q):
    r = subprocess.run(
        ["docker", "exec", "uav-db", "psql", "-U", "uav", "-d", "uav", "-tAc", q],
        capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"SQL 失敗：{r.stderr}")
    # INSERT/UPDATE 會多吐一行命令標籤（"INSERT 0 1"）——只要第一行，
    # 不然回傳值會變成 "<uuid>\nINSERT 0 1" 然後在下一句 SQL 裡炸掉
    return r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""


def post(path, body):
    req = urllib.request.Request(
        CMD + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


drone_id = session_id = None
try:
    drone_id = sql(f"INSERT INTO drones (name, mav_sysid) "
                   f"VALUES ('{NAME}', {SYSID}) RETURNING id")
    session_id = sql(f"INSERT INTO flight_sessions (drone_id, started_at) "
                     f"VALUES ('{drone_id}', now()) RETURNING id")
    print(f"臨時機 {NAME}  drone={drone_id[:8]}  session={session_id[:8]}\n")

    before = int(sql("SELECT count(*) FROM command_log"))
    # **故意會被擋**：機不在線。被擋下的那條路徑正是 9/2 才補上留痕的，
    # 也是最需要接得回架次的——「系統擋了我幾次」要看得出是哪一趟
    status, body = post(f"/api/command/{SYSID}/mode/rtl", {})
    print(f"POST /api/command/{SYSID}/mode/rtl → HTTP {status} {body[:150]}\n")

    after = int(sql("SELECT count(*) FROM command_log"))
    chk("指令有留痕（不論成敗）", after > before, f"{before} → {after}")

    row = sql(f"SELECT result || '|' || coalesce(drone_id::text,'-') || '|' "
              f"|| coalesce(session_id::text,'-') FROM command_log "
              f"WHERE sysid = {SYSID} ORDER BY time DESC LIMIT 1")
    result, did, sid = row.split("|") if row else ("", "-", "-")
    print(f"最新那筆：result={result}  drone_id={did[:8]}  session_id={sid[:8]}\n")

    chk("**drone_id 有填**（這正是 307 筆全空的那一欄）", did == drone_id, did)
    chk("**session_id 有填**——指令接得回哪一趟飛行", sid == session_id, sid)
    chk("被系統擋下的也留痕，不是只有成功的才記",
        result in ("refused", "failed", "error"), result)

    n = int(sql(f"SELECT count(*) FROM command_log WHERE session_id = '{session_id}'"))
    chk("照 session_id 查得到這趟的指令（匯出檔那一段靠它）", n >= 1, n)

    # **反向驗證**：架次結束之後下的指令不該再掛到它身上
    sql(f"UPDATE flight_sessions SET ended_at = now() WHERE id = '{session_id}'")
    post(f"/api/command/{SYSID}/mode/rtl", {})
    sid2 = sql(f"SELECT coalesce(session_id::text,'-') FROM command_log "
               f"WHERE sysid = {SYSID} ORDER BY time DESC LIMIT 1")
    chk("**反向驗證**：架次結束後的指令不掛到已結束的架次",
        sid2 == "-", sid2)
finally:
    if drone_id:
        sql(f"DELETE FROM drones WHERE id = '{drone_id}'")
        # 2026-09-02 的教訓：刪了機但 session 沒跟著走，機隊裡就留下一台
        # 不存在的飛機。這裡**驗到零**，不是刪完就算
        left = int(sql(f"SELECT count(*) FROM flight_sessions "
                       f"WHERE drone_id = '{drone_id}'"))
        left += int(sql(f"SELECT count(*) FROM drones WHERE id = '{drone_id}'"))
        chk("臨時機清乾淨（含 session 連帶刪除）", left == 0, left)
        stray = int(sql("SELECT count(*) FROM drones WHERE name LIKE 'zz-test-%'"))
        chk("沒有其他測試機殘留", stray == 0, stray)
        # 指令列的外鍵是 ON DELETE SET NULL，刪機不會帶走它們——**這幾筆是
        # 測試造的假指令，留著會混進真的指令史**（那是實驗記錄的一部分）
        sql(f"DELETE FROM command_log WHERE sysid = {SYSID}")
        chk("測試造的指令列也清掉（別混進真的指令史）",
            int(sql(f"SELECT count(*) FROM command_log WHERE sysid = {SYSID}")) == 0)

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
