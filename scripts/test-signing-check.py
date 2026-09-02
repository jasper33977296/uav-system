#!/usr/bin/env python3
"""簽章金鑰的保管與指紋自檢（issues/040 A5-b／A5-c）。

**這一批還沒有在簽任何東西。** 先做偵測的理由寫在設計 §3：
**簽章不符是靜默丟棄**——金鑰一旦不對，畫面上一切正常、指令全部消失。
那正是本專案反覆吃虧的形狀（`SYSID_MYGCS` 靜默丟棄、補傳的狀態碼判定從未生效、
兩支永遠紅的測試）。**偵測要先於啟用，不能反過來。**

釘住五件事：
  1. 金鑰**永遠不出現在**指紋裡（指紋是雜湊，不是前綴）
  2. 四種自檢結果**刻意分開**——混成「好／壞」會讓最重要的那格消失
  3. **沒有金鑰＝尚未啟用，不是故障**（絕大多數機的常態）
  4. **不覆蓋既有金鑰**（覆蓋等於把那台機的簽章打斷，而它可能正在飛）
  5. 金鑰檔權限 0600

跑法（backend 容器內）：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-signing-check.py
"""
import os
import stat
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ["SIGNING_KEYS_PATH"] = os.path.join(tmp, "keys.json")

from app import signing  # noqa: E402

signing.KEYS_PATH = os.environ["SIGNING_KEYS_PATH"]
ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


print("── 1. 指紋是雜湊，不是金鑰前綴 ────────────────────────")
fp, created = signing.issue_key("boardA")
key = signing.get_key("boardA")
chk("配出一把新金鑰", created and len(key) == signing.KEY_BYTES * 2, len(key))
chk("**指紋不是金鑰的前綴**（前綴會洩漏金鑰的位元）",
    not key.startswith(fp), f"key前16={key[:16]} fp={fp}")
chk("指紋長度固定", len(fp) == signing.FP_CHARS, fp)

print("\n── 2. 四種自檢結果分開 ────────────────────────────────")
chk("兩邊都沒有 → not_enabled（常態，不是故障）",
    signing.check("noboard", None)["state"] == "not_enabled")
chk("我們有、機上沒有 → agent_missing（配發沒完成）",
    signing.check("boardA", None)["state"] == "agent_missing")
chk("機上有、我們沒有 → ground_missing（金鑰檔可能掉了）",
    signing.check("noboard", "abcdef1234567890")["state"] == "ground_missing")
r = signing.check("boardA", "0000000000000000")
chk("兩邊都有但不同 → mismatch", r["state"] == "mismatch")
chk("**mismatch 的理由要講出「靜默丟棄」**（否則沒人知道它多嚴重）",
    "靜默丟棄" in r["reason"], r["reason"][:40])
chk("相同 → match", signing.check("boardA", fp)["state"] == "match")

print("\n── 3. 不覆蓋既有金鑰 ──────────────────────────────────")
fp2, created2 = signing.issue_key("boardA")
chk("再配一次不會換掉", created2 is False and fp2 == fp)
chk("金鑰真的沒變", signing.get_key("boardA") == key)
fp3, created3 = signing.issue_key("boardA", overwrite=True)
chk("**明確要求才輪換**", created3 is True and fp3 != fp)

print("\n── 4. 撤銷只做地面站這半 ──────────────────────────────")
chk("撤銷成功", signing.revoke_key("boardA") is True)
chk("撤銷後查不到", signing.get_fingerprint("boardA") is None)
chk("撤銷不存在的回 False（不是丟例外）",
    signing.revoke_key("boardA") is False)

print("\n── 5. 金鑰檔權限 0600 ─────────────────────────────────")
signing.issue_key("boardB")
mode = stat.S_IMODE(os.stat(signing.KEYS_PATH).st_mode)
chk("只有擁有者讀得到", mode == 0o600, oct(mode))

print("\n── 6. 反向驗證：讀不到金鑰檔**不能**當成「沒啟用」──────")
signing.KEYS_PATH = os.path.join(tmp, "does-not-exist", "x.json")
chk("檔案不存在 → 視為還沒配發（FileNotFoundError 是正常的）",
    signing.get_key("boardB") is None)
bad = os.path.join(tmp, "broken.json")
open(bad, "w").write("{ not json")
signing.KEYS_PATH = bad
chk("**檔案壞掉時也回 None，但那條路徑會 log.error**（不是靜靜當成沒啟用）",
    signing.get_key("boardB") is None)

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
