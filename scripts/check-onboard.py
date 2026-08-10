#!/usr/bin/env python3
"""地面站端驗收：確認 onboard node 送來的資訊完整。純標準庫，零依賴。

觀察 GET /api/live 一段時間（預設 20 秒），逐項檢查：

  1. 來源正確   link.source=modem（simulated＝地面站 .env 沒切，會連 409 都收不到）
  2. 節奏       樣本以 ~1Hz 更新（link_age_s 保持小、time 持續前進）
  3. 欄位完整   RF（sinr/rsrp/rsrq/pci/cell_id/band/nr_mode）、端到端
                （rtt_ms/packet_loss_pct）、raw.at_qeng、time——逐欄統計非空率
  4. 位置綁定   樣本內 lat/lon（來自機上 node 聽 PX4）。特別診斷：
                MAVLink 遙測有位置、樣本卻沒有＝機上 router 沒把 MAVLink
                餵給 node 的 PX4_URL
  5. 時鐘       樣本 time 與地面站時鐘偏差（>5s 警告——GPS 時鐘或機上時鐘問題）
  6. 入庫路徑   armed 時抽查 DB 是否真的有新 link_metrics 列
                （未 arm 屬正常不入庫，只提示）

用法：
  python3 scripts/check-onboard.py                     # 本機 backend
  python3 scripts/check-onboard.py http://<GS>:38000 30
  python3 scripts/check-onboard.py --source-any        # 開發：接受 simulated
"""
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

RF_FIELDS = ["sinr", "rsrp", "rsrq", "pci", "cell_id", "band", "nr_mode"]
NET_FIELDS = ["rtt_ms", "packet_loss_pct"]
POS_FIELDS = ["lat", "lon", "alt_rel"]

ok = lambda m: print("  ✅ " + m)
warn = lambda m: print("  ⚠️  " + m)
fail = lambda m: print("  ❌ " + m)


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    source_any = "--source-any" in sys.argv
    base = args[0] if args else "http://localhost:38000"
    seconds = float(args[1]) if len(args) > 1 else 20.0

    print(f"觀察 {base}/api/live，{seconds:.0f} 秒⋯")
    samples, problems = [], 0
    last_key = None
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        try:
            snap = get(base + "/api/live")
        except Exception as e:
            sys.exit(f"❌ 連不到 backend：{e}")
        link = snap.get("link") or {}
        # 去重鍵：modem 樣本帶 time；模擬樣本沒有，退回整筆內容比對
        key = link.get("time") or json.dumps(link, sort_keys=True)
        if link and key != last_key:
            samples.append((link, snap))
            last_key = key
        time.sleep(0.5)

    if not samples:
        print("\n【1. 來源】")
        fail("觀察期間沒有收到任何鏈路樣本")
        print("     排查：機上 node 在跑嗎？journalctl -u uav-link-node -f")
        print("     機上若見「伺服器拒絕 HTTP 409」＝地面站 .env 未設 LINK_SOURCE=modem")
        print("     機上若見「URLError」＝GROUND_API 位址/5G 路由不通")
        sys.exit(1)

    last_link, last_snap = samples[-1]
    n = len(samples)

    print(f"\n【1. 來源】共收到 {n} 筆不重複樣本")
    src = last_link.get("source")
    if src == "modem" or source_any:
        ok(f"link.source = {src}")
    else:
        fail(f"link.source = {src}（預期 modem——這是模擬器資料，不是機上送的）")
        problems += 1

    print("【2. 節奏】")
    rate = n / seconds
    age = last_snap.get("link_age_s")
    if rate >= 0.8:
        ok(f"~{rate:.1f} Hz（預期 ~1Hz），link_age_s={age}")
    elif rate > 0:
        warn(f"只有 ~{rate:.1f} Hz（預期 ~1Hz）——鏈路劣化或機上取樣被拖慢")
        problems += 1
    print("【3. 欄位完整性】（非空率，樣本數 {}）".format(n))
    for group, fields in (("RF", RF_FIELDS), ("端到端", NET_FIELDS)):
        for f in fields:
            filled = sum(1 for s, _ in samples if s.get(f) is not None)
            pct = 100.0 * filled / n
            line = f"{group:4s} {f:16s} {pct:5.1f}%"
            (ok if pct >= 90 else warn if pct > 0 else fail)(line)
            if pct < 90:
                problems += 1
    raw_ok = sum(1 for s, _ in samples if (s.get("raw") or {}).get("at_qeng"))
    (ok if raw_ok == n else fail)(f"raw.at_qeng 保留率 {100.0 * raw_ok / n:.0f}%")

    print("【4. 位置綁定】")
    pos = sum(1 for s, _ in samples if s.get("lat") is not None and s.get("lon") is not None)
    if pos >= n * 0.9:
        ok(f"樣本帶座標 {100.0 * pos / n:.0f}%")
    else:
        fail(f"樣本帶座標僅 {100.0 * pos / n:.0f}%")
        problems += 1
        if last_snap.get("lat") is not None:
            print("     ↳ MAVLink 遙測有位置、樣本卻沒有＝機上 router 沒把")
            print("       MAVLink 分流餵給 node 的 PX4_URL（預設 udpin:0.0.0.0:14540）")
        else:
            print("     ↳ MAVLink 遙測也沒位置：GPS 未定位（室內/EKF 未收斂）屬正常")

    print("【5. 時鐘】")
    try:
        ts = datetime.fromisoformat(last_link["time"])
        off = abs((datetime.now(timezone.utc) - ts).total_seconds())
        (ok if off < 5 else warn)(f"樣本時間與地面站偏差 {off:.1f}s" +
                                  ("" if off < 5 else "——檢查機上 GPS 時鐘/系統時鐘"))
        if off >= 5:
            problems += 1
    except Exception:
        fail("time 欄位缺失或非 ISO 格式")
        problems += 1

    print("【6. 入庫路徑（batch）】")
    if last_snap.get("armed"):
        try:
            cnt = subprocess.run(
                ["docker", "exec", "uav-db", "psql", "-U", "uav", "uav", "-tAc",
                 "SELECT count(*) FROM link_metrics WHERE time > now()-interval '1 minute'"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=10)
            rows = int(cnt.stdout.strip() or 0)
            (ok if rows > 0 else fail)(f"armed 中，近 1 分鐘入庫 {rows} 列")
            if rows == 0:
                problems += 1
        except Exception:
            warn("無法查 DB（非 docker 部署？）——略過")
    else:
        print("  ℹ️  未 arm：batch 樣本被判架次外丟棄屬正常設計（解鎖後才入庫）")

    print("\n" + ("✅ 通過：onboard 資訊完整" if problems == 0
                  else f"❌ 有 {problems} 項需要處理（見上）"))
    sys.exit(0 if problems == 0 else 1)


if __name__ == "__main__":
    main()
