#!/usr/bin/env python3
"""把飛控 SD 卡上的一份 dataflash 紀錄撈下來（issues/046 的「仍缺」）。

**為什麼是分塊迴圈而不是一次拉完**：下載走的是 FC↔Pi 那條 57600 的序列埠，
而遙測正在上面跑——實測約 **2.3 KB/s**，一份 1.8 MB 要十幾分鐘。指令服務的
工作跑在單一執行緒上，整段抓完等於那十幾分鐘裡解鎖、切模式、緊急降落全部
排在後面。分塊之後其他指令插得進來，而且中斷了可以從 `next` 續傳。

    python3 scripts/fetch-flight-log.py --list
    python3 scripts/fetch-flight-log.py 36            # 抓完整份
    python3 scripts/fetch-flight-log.py 36 --until 262144   # 只抓前 256 KB

檔案落在 `data/flight-logs/<sysid>-<id>.bin`（容器內 `/data/flight-logs`）。
**不進 git**。續傳靠檔案目前的大小，所以中途 Ctrl-C 再跑一次就接得回去。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:38001/api/command"


def call(path, method="GET"):
    req = urllib.request.Request(f"{API}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()[:400].decode("utf-8", "replace")
        # **位移對不上是可以自己接回去的**，不是失敗：端點拒收亂序寫入
        # （那會產生一個看起來完整、其實錯位的 .bin），並在回話裡說出
        # 檔案目前多大。續傳就是從那裡繼續
        try:
            d = json.loads(raw).get("detail") or {}
        except ValueError:
            d = {}
        if e.code == 409 and isinstance(d, dict) and "have" in d:
            return {"resume_at": int(d["have"])}
        raise SystemExit(f"HTTP {e.code}：{raw}") from e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_id", type=int, nargs="?")
    ap.add_argument("--sysid", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--until", type=int, default=0, help="只抓到這個位元組數")
    ap.add_argument("--chunk", type=int, default=65536)
    a = ap.parse_args()

    if a.list or a.log_id is None:
        d = call(f"/{a.sysid}/logs")
        print(f"共 {d['num_logs']} 份")
        print("%-4s %10s %8s  %s" % ("id", "bytes", "MB", "飛控說的時間(UTC)"))
        import datetime
        for lg in d["logs"]:
            t = lg["time_utc"]
            ts = ("（飛控當時不知道時間）" if not t or t < 1_000_000_000 else
                  datetime.datetime.fromtimestamp(
                      t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            print("%-4d %10d %8.1f  %s" % (lg["id"], lg["size"],
                                           lg["size"] / 1e6, ts))
        return 0

    size = next((x["size"] for x in call(f"/{a.sysid}/logs")["logs"]
                 if x["id"] == a.log_id), None)
    if size is None:
        raise SystemExit(f"飛控上沒有第 {a.log_id} 份紀錄")
    target = min(a.until, size) if a.until else size
    ofs, start_ofs, t0, stalls = 0, 0, time.time(), 0
    while ofs < target:
        r = call(f"/{a.sysid}/logs/{a.log_id}/fetch"
                 f"?ofs={ofs}&nbytes={min(a.chunk, target - ofs)}", "POST")
        if "resume_at" in r:              # 已經抓過一部分，接上去
            ofs = r["resume_at"]
            print(f"續傳：從 {ofs:,} 開始")
            start_ofs, t0 = ofs, time.time()
            continue
        if r["wrote"] == 0:
            # **停下來說話，不要無限重試**：飛控不再回應這份紀錄時，
            # 一直要下去只是佔著那條線，而遙測正在上面跑
            stalls += 1
            if stalls >= 3:
                raise SystemExit(f"連續三次拿不到資料（停在 {ofs}/{target}）"
                                 "——飛控可能不再回應這份紀錄了")
        else:
            stalls = 0
        ofs = r["next"]
        el = time.time() - t0
        rate = (ofs - start_ofs) / el if el else 0
        left = (target - ofs) / rate if rate else 0
        print(f"\r{ofs:>9,}/{target:,} ({ofs*100//max(target,1)}%) "
              f"{rate/1024:.1f} KB/s 剩約 {left/60:.0f} 分"
              f"{'  洞 %d' % r['holes'] if r['holes'] else ''}", end="", flush=True)
    print(f"\n完成：{r['path']}（{ofs:,} bytes）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
