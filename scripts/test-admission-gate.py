#!/usr/bin/env python3
"""入列守門：驗證通過之前不得指揮（issues/040 A2）。

使用者 2026-09-02 的需求原話：**多個無人機身份驗證階段，在驗證完成前不可以
指派任務或嘗試控制無人機**；以及**要被本系統控制，機上一定要有代理**。

本測試對**跑著的真實堆疊**驗四件事：
  1. 有連線但**沒有代理**的機 → `unmanaged`，所有指令端點 403
  2. 訊息**說得出下一步**（不是「未驗證」這種不可行動的字眼）
  3. **入列檢查排在能力檢查之前**——身分不明時不該先談能力
  4. 已入列的機不受影響（反向驗證：守門不是把所有東西都擋掉）

用法（需要 backend 與 command 都在跑）：
    python3 scripts/test-admission-gate.py [--fake-sysid 42]
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--fake-sysid", type=int, default=42)
a = ap.parse_args()

BACKEND, COMMAND = "http://localhost:38000", "http://localhost:38001"
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


def get(url):
    with urllib.request.urlopen(url, timeout=6) as r:
        return json.loads(r.read().decode())


def post(url, payload=None):
    """**要帶 body 的端點就要帶**：少了它 FastAPI 會先回 422（請求畸形），
    端點函式根本不會執行——那樣測到的是「畸形請求進不去」，不是「入列擋下」。
    兩者都安全，但只有後者是本測試要驗的東西。"""
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"} if payload else {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


fake = subprocess.Popen(
    [sys.executable, "scripts/fake-drone.py", "--sysid", str(a.fake_sysid)],
    cwd="/home/k200/uav-system",
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    time.sleep(12)

    print("── 1. 沒有代理的機：unmanaged ─────────────────────────")
    st = get(f"{BACKEND}/api/admission/{a.fake_sysid}")
    chk("backend 判 unmanaged", st.get("state") == "unmanaged", st.get("reason"))

    print("\n── 2. 所有指令端點都擋（不是只擋危險的那幾個）──────────")
    for path, label, payload in [
            ("/arm", "解鎖", None), ("/disarm", "上鎖", None),
            ("/mode/hold", "切模式", None),
            ("/takeoff", "起飛", {"alt": 10}),
            ("/mission/start", "開始任務", None),
            ("/mission/upload", "上傳任務", {"mission_id": "00000000-0000-0000-0000-000000000000"})]:
        code, body = post(f"{COMMAND}/api/command/{a.fake_sysid}{path}", payload)
        d = body.get("detail") or {}
        good = code == 403 and d.get("code") == "not_admitted"
        chk(f"{label} → 403 not_admitted", good, f"HTTP {code}")

    print("\n── 3. 訊息要說得出下一步（不可行動的原因等於沒有原因）──")
    code, body = post(f"{COMMAND}/api/command/{a.fake_sysid}/arm")
    msg = (body.get("detail") or {}).get("msg", "")
    chk("說得出「沒有代理」而不是只說「未驗證」", "代理" in msg, msg[:60])
    chk("說得出該做什麼", "請確認" in msg)
    chk("**說出實體遙控器不受影響**（擋下不等於沒有退路）",
        "遙控器" in json.dumps(body, ensure_ascii=False))
    chk("同一句話不重複貼兩次", msg.count("沒有連線中的機上代理") == 1, msg[:80])

    print("\n── 4. 入列檢查排在能力檢查之前 ────────────────────────")
    # 假機是 PX4，能力表對 PX4 幾乎全開；若能力檢查先跑，錯誤會是 501 而不是 403
    chk("身分不明時回的是 403（身分）不是 501（能力）", code == 403, f"HTTP {code}")

    print("\n── 5. 反向驗證：已入列的機沒有被一起擋掉 ───────────────")
    real = None
    for d in get(f"{BACKEND}/api/drones"):
        if (d.get("agent") or {}).get("connected") and d.get("mav_sysid"):
            real = d
            break
    if real is None:
        print("  （跳過：現在沒有連著代理的機——**skip ≠ pass**）")
    else:
        sid = real["mav_sysid"]
        st2 = get(f"{BACKEND}/api/admission/{sid}")
        chk(f"有代理的機（sysid {sid}）判 admitted",
            st2.get("state") == "admitted", st2.get("reason"))
        code2, body2 = post(f"{COMMAND}/api/command/{sid}/mode/hold")
        d2 = (body2.get("detail") or {})
        chk("**它的指令不會被入列擋下**（擋它的是別的關卡或成功）",
            d2.get("code") != "not_admitted",
            f"HTTP {code2} code={d2.get('code')}")
finally:
    fake.terminate()
    fake.wait(timeout=5)

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
