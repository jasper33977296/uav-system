#!/usr/bin/env python3
"""跟飛控核對地形資料（issues/047 §2）：離線驗證問答與判讀。

**重點是三種「不一樣」不得互相冒充**：

  1. **沒回應** ≠ 沒有地形資料。`TERRAIN_REPORT` 跟 `PARAM_VALUE` 一樣會被
     塞滿的序列埠丟掉（2026-09-07 查了大半天的那件事），把它讀成「那裡沒有
     地形」會得到一個完全錯的結論。
  2. **`pending > 0`** ＝飛控自己缺那塊圖磚。本系統不供圖，所以它不會自己補上。
  3. **兩份高程差很多** ＝兩份資料不同源。飛機跟的是它自己那份。

還有一件容易錯的：**一次只能有一個未回覆的請求**。ArduPilot 回的座標可能已經
吸附到格點（實測格距 100 m），同時問多點再靠座標配對，會在航點相距小於格距時
配錯——而配錯的後果是拿 A 點的地面高度去判斷 B 點安不安全。

跑法（需要 pymavlink；沒有的話在容器裡跑）：
    python3 scripts/test-terrain-crosscheck.py
    docker exec uav-command python3 /srv/scripts/test-terrain-crosscheck.py
"""
import sys
import time

sys.path.insert(0, "/home/k200/uav-system/apps/command")
sys.path.insert(0, "/home/k200/uav-system/libs")

from pymavlink.dialects.v20 import ardupilotmega as M  # noqa: E402

from app import mav  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


class FakeMsg:
    """夠用的 TERRAIN_REPORT 替身（`_recv` 只用到這幾個介面）。"""

    def __init__(self, lat, lon, height, pending=0, loaded=336, spacing=100,
                 sysid=1, typ="TERRAIN_REPORT"):
        self.lat, self.lon = int(lat * 1e7), int(lon * 1e7)
        self.terrain_height, self.current_height = height, 0.0
        self.pending, self.loaded, self.spacing = pending, loaded, spacing
        self._t, self._s = typ, sysid

    def get_type(self):
        return self._t

    def get_srcSystem(self):
        return self._s


class FakeRouter:
    """回覆由 `answers` 決定：每次 `_sendto` 取一個，None ＝這一點不回應。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent = []
        self._queue = []

    def _sendto(self, sysid, encode_fn):
        # 真的編一次，確保 terrain_check_encode 的欄位與型別對得上
        msg = encode_fn(M.MAVLink(None, srcSystem=255))
        self.sent.append((msg.lat / 1e7, msg.lon / 1e7))
        a = self.answers.pop(0) if self.answers else None
        if a is not None:
            self._queue.append(a)

    def _recv(self, timeout):
        if self._queue:
            return self._queue.pop(0)
        time.sleep(min(timeout, 0.01))
        return None


PTS = [(24.7734, 121.0459, "起飛點"), (24.7740, 121.0461, "seq 1"),
       (24.7746, 121.0463, "seq 2")]

print("── 1. 三點都答得出來 ─────────────────────────────────")
r = FakeRouter([FakeMsg(la, lo, 123.0 + i) for i, (la, lo, _) in enumerate(PTS)])
res = mav.job_terrain_check(r, 1, PTS)
chk("問了三個點", res["asked"] == 3 and len(r.sent) == 3, r.sent)
chk("三個都有答", res["answered"] == 3)
chk("座標編碼沒有跑掉（degE7 往返）",
    abs(r.sent[0][0] - 24.7734) < 1e-6, r.sent[0])
chk("高度與 loaded 帶回來", res["points"][0]["terrain_height_m"] == 123.0
    and res["points"][0]["loaded"] == 336, res["points"][0])

print("\n── 2. 一次只有一個未回覆的請求（不靠座標配對）──────────")
# 飛控把第 2 點的答案吸附到 100 m 外——**照樣算第 2 點的答案**，
# 因為問的時候只有它一個在等
snapped = FakeMsg(24.7749, 121.0463, 130.0)
r = FakeRouter([FakeMsg(*PTS[0][:2], 123.0), snapped, FakeMsg(*PTS[2][:2], 125.0)])
res = mav.job_terrain_check(r, 1, PTS)
chk("吸附到格點的回覆仍歸給問的那一點",
    res["points"][1]["terrain_height_m"] == 130.0, res["points"][1])
chk("**而且照實記下飛控回的座標**（差多少看得見）",
    res["points"][1]["reported_lat"] == 24.7749, res["points"][1])

print("\n── 3. 沒回應 ≠ 沒有地形資料 ──────────────────────────")
r = FakeRouter([FakeMsg(*PTS[0][:2], 123.0), None, FakeMsg(*PTS[2][:2], 125.0)])
res = mav.job_terrain_check(r, 1, PTS)
chk("沒回應的那點不帶高度欄位（不填 0）",
    "terrain_height_m" not in res["points"][1], res["points"][1])
chk("answered 少一個，asked 不變", res["answered"] == 2 and res["asked"] == 3)

print("\n── 4. 全部沒回應，但鏈路在動 → 說得出兩種可能 ────────────")
noise = FakeMsg(0, 0, 0, typ="ATTITUDE")
r = FakeRouter([None, None, None])
r._queue = [noise] * 20      # 清場也會讀走一些——鏈路活著的證據不能因此消失
try:
    mav.job_terrain_check(r, 1, PTS)
    chk("應該要丟 CommandError", False)
except mav.CommandError as e:
    chk("丟 CommandError 並指出 TERRAIN_ENABLE 與頻寬兩種可能",
        "TERRAIN_ENABLE" in str(e) and "序列埠" in str(e), str(e)[:60])

print("\n── 4b. 不請自來的報告不得讓答案錯開一格（2026-09-08 實機抓到）──")
# 實機現象：室內沒有 GPS，飛控自己送了一則 lat/lon = 0,0 的 TERRAIN_REPORT。
# 它卡在緩衝區被當成第一個查詢的答案，於是**每個航點拿到的是前一個航點的
# 地面高度**——五個點全部「有答案」、數字也都合理，看不出哪裡不對。
ghost = FakeMsg(0.0, 0.0, 0.0)
r = FakeRouter([FakeMsg(la, lo, 123.0 + i) for i, (la, lo, _) in enumerate(PTS)])
r._queue = [ghost]                      # 送第一個查詢之前就躺在緩衝區裡
res = mav.job_terrain_check(r, 1, PTS)
chk("**每一點拿到的是自己的答案，不是前一點的**",
    [p["terrain_height_m"] for p in res["points"]] == [123.0, 124.0, 125.0],
    [p.get("terrain_height_m") for p in res["points"]])
chk("配不上的那則算進 stray（唯一的外顯訊號）", res["stray"] >= 1, res["stray"])
# 就算清場沒清到（時序不同），距離門檻也要擋下來：0,0 離現場 12541 km。
# **只有 0,0 可收時，寧可回報「沒有答案」，也不拿它頂替**
r = FakeRouter([ghost])
try:
    mav.job_terrain_check(r, 1, PTS[:1])
    chk("距離門檻擋得掉 0,0（不拿它頂替）", False, "竟然收下了")
except mav.CommandError:
    chk("距離門檻擋得掉 0,0——寧可說沒有答案，也不拿它頂替", True)

print("\n── 5. pending 與 loaded 分得開 ──────────────────────")
r = FakeRouter([FakeMsg(*PTS[0][:2], 123.0, pending=0, loaded=336),
                FakeMsg(*PTS[1][:2], 124.0, pending=4, loaded=336),
                FakeMsg(*PTS[2][:2], 125.0, pending=0, loaded=336)])
res = mav.job_terrain_check(r, 1, PTS)
tot = sum(p.get("pending", 0) for p in res["points"])
chk("缺格數加得起來（4）", tot == 4, tot)
chk("loaded 有值不代表 pending 是 0",
    res["points"][1]["loaded"] == 336 and res["points"][1]["pending"] == 4)

print("\n── 6. 問的點數有上限，多的不送 ─────────────────────────")
many = [(24.77 + i * 1e-4, 121.04, f"seq {i}") for i in range(20)]
r = FakeRouter([FakeMsg(24.77, 121.04, 123.0)] * 20)
res = mav.job_terrain_check(r, 1, many)
chk(f"最多問 {mav.TERRAIN_PROBE_MAX} 個",
    len(r.sent) == mav.TERRAIN_PROBE_MAX, len(r.sent))

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
