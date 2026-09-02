#!/usr/bin/env python3
"""例外狀況處理有沒有打開——**逐項查，逐項說得出沒過的後果**。

三層防線（issues/039）分別由不同的東西負責，而**沒有一個地方能一次看到
它們現在是開是關**：飛控那層在參數裡、代理那層在 systemd 的 Environment、
地面那層在容器裡。2026-09-02 的實測就是這樣：`BATT_FS_LOW_ACT=0`
（低電量偵測得到但不動作）躺了不知道多久，而畫面上一切正常。

**這支不改任何東西**，只讀、只判、只報。要改用 `set-fc-params.py`。

## 參數從哪裡讀

* `--tlog`：地面站錄的 tlog（**不必停代理**）。**一定要 `--sysid`**——
  那份錄的是那個埠上的所有流量，混著別台機的參數。2026-09-02 實測：
  一份 tlog 裡 sysid 1 有 2031 個參數、sysid 2 有 975 個，**不濾就會拿到
  另一台機的設定當成這台的**（我踩過，把 Plane 的 `RTL_LOITER_RAD` 讀成
  這台 Copter 的）。
  代價是**值可能是舊的**——所以每一列都印出它有多舊。
* `--dev`：直接問飛控（在機上跑，要先停代理，它握著序列埠）。最新，但有停機。

## 電芯數要人給

`--cells` 沒給的話，電壓門檻那幾列一律回「不知道」——**不猜**。
2026-09-02 的教訓：從遙測看到的最高電壓推電芯數，在接電源供應器時完全失效。

用法：
    python3 scripts/check-failsafes.py --tlog /data/mavcap/20260902.tlog --sysid 1 --cells 4
    python3 scripts/check-failsafes.py --dev /dev/ttyAMA0 --cells 4     # 在機上
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

from pymavlink import mavutil

ap = argparse.ArgumentParser()
ap.add_argument("--tlog")
ap.add_argument("--sysid", type=int, help="--tlog 時**必填**：要看哪一台的參數")
ap.add_argument("--dev")
ap.add_argument("--baud", type=int, default=57600)
ap.add_argument("--cells", type=int, help="電池電芯數（4S 就填 4）。不給＝電壓門檻不判定")
ap.add_argument("--gs", default="http://localhost:38000", help="地面站 backend")
ap.add_argument("--command", default="http://localhost:38001")
ap.add_argument("--compose-dir", default="/home/k200/uav-system")
ap.add_argument("--max-age-min", type=float, default=60.0,
                help="--tlog 的值超過這麼舊就不下結論（預設 60 分鐘）。"
                     "**參數很少變，所以一小時前的值通常還是真的**——但剛改過"
                     "參數時它一定是舊的，那正是這條保護要擋的情況")
ap.add_argument("--stop-agent", action="store_true",
                help="--dev 時自動停代理再讀，**讀完一定開回來**（try/finally）")
ap.add_argument("--ssh", default="pi@10.141.2.32")
a = ap.parse_args()

PASS, FAIL, UNKNOWN = "✓", "✗", "?"
rows = []

#: 這支會看的參數。**列在這裡而不是散在檢查裡**——`--dev` 模式要照這張表
#: 逐一請求（不能用 PARAM_REQUEST_LIST：2000 多筆在 57600 上會把鏈路塞滿，
#: 而那條線同時要跑遙測）
WANT = ["BATT_MONITOR", "BATT_FS_LOW_ACT", "BATT_FS_CRT_ACT",
        "BATT_LOW_VOLT", "BATT_CRT_VOLT", "FS_THR_ENABLE", "FS_GCS_ENABLE",
        "FS_GCS_TIMEOUT", "FS_EKF_ACTION", "FS_CRASH_CHECK", "FS_VIBE_ENABLE",
        "ARMING_CHECK", "FENCE_ENABLE", "FENCE_ACTION", "RTL_ALT"]


def row(mark, item, got, verdict, why):
    rows.append((mark, item, got, verdict, why))


# ── 讀參數 ──────────────────────────────────────────────────────
params, ages = {}, {}
if a.tlog:
    if a.sysid is None:
        sys.exit("✗ --tlog 一定要配 --sysid（那份錄的是所有機的流量，"
                 "不濾會拿到別台機的設定）")
    path = a.tlog
    if path.startswith("http"):
        # **從錄製檔的下載端點取**（issues/014）——那份 tlog 住在容器的 volume
        # 裡，host 上看不到。既然今天已經讓它「拿得到」，就用那條路
        import tempfile
        import urllib.request
        fd = tempfile.NamedTemporaryFile(suffix=".tlog", delete=False)
        with urllib.request.urlopen(path, timeout=120) as r:
            while True:
                blk = r.read(1 << 20)
                if not blk:
                    break
                fd.write(blk)
        fd.close()
        path = fd.name
    src = mavutil.mavlink_connection(path)
    newest = 0.0
    while True:
        msg = src.recv_match(type="PARAM_VALUE", blocking=False)
        if msg is None:
            break
        if msg.get_srcSystem() != a.sysid:
            continue
        nm = msg.param_id if isinstance(msg.param_id, str) else msg.param_id.decode()
        t = getattr(msg, "_timestamp", 0.0) or 0.0
        params[nm.rstrip("\x00")] = float(msg.param_value)
        ages[nm.rstrip("\x00")] = t
        newest = max(newest, t)
    print(f"參數來源：{a.tlog}（sysid {a.sysid}，{len(params)} 個，"
          f"最新一筆 {(time.time() - newest) / 60:.0f} 分鐘前）")
    print("⚠ **tlog 裡的參數只在有人問過的時候才會更新。** 2026-09-02 實測："
          "用序列埠改完六個參數之後，只有代理開機會重問的那兩個在 tlog 裡\n"
          "  變新，另外四個還是舊值——**看起來像沒改成功**。"
          "要權威值就用 --dev。\n")
elif a.dev:
    # **停代理這件事要有 finally。** 2026-09-02 實測踩過：用 shell 一行
    # `stop; 讀; start` 跑，讀到一半 SSH 逾時中斷，`start` 那半永遠沒跑到——
    # 代理就一直停著，而地面站畫面上只看得到「代理失聯」，與 Pi 當機、
    # 5G 斷線完全同形。**把它放進工具裡，那個失敗模式就不可能發生。**
    stopped = False
    if a.stop_agent:
        subprocess.run(["systemctl", "stop", "uav-agent"], timeout=30)
        stopped = True
        time.sleep(2)
    try:
        m = mavutil.mavlink_connection(a.dev, baud=a.baud)
        if m.wait_heartbeat(timeout=15) is None:
            raise SystemExit("✗ 沒有心跳——代理還開著嗎？（它握著序列埠）")
        tgt = (m.target_system, m.target_component)
        # **只問要用的那十幾個，不要 PARAM_REQUEST_LIST。** 整份 2000 多筆在
        # 57600 上要跑很久，而且期間把鏈路塞滿——這條線同時要跑遙測
        want = set(WANT)
        for n in WANT:
            m.mav.param_request_read_send(tgt[0], tgt[1], n.encode(), -1)
            time.sleep(0.05)
        end = time.time() + 25
        while want and time.time() < end:
            msg = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=1)
            if msg is None:
                continue
            nm = msg.param_id if isinstance(msg.param_id, str) else msg.param_id.decode()
            nm = nm.rstrip("\x00")
            if nm in want:
                params[nm] = float(msg.param_value)
                ages[nm] = time.time()
                want.discard(nm)
        print(f"參數來源：{a.dev}（現讀 {len(params)}/{len(WANT)} 個"
              + (f"，讀不到 {sorted(want)}" if want else "") + "）\n")
    finally:
        if stopped:
            r = subprocess.run(["systemctl", "start", "uav-agent"], timeout=30)
            print(f"代理已重新啟動（rc={r.returncode}）\n")
else:
    print("（沒給 --tlog 或 --dev：飛控那一層全部跳過）\n")


def p(name):
    return params.get(name)


def chk_param(name, ok_fn, verdict_ok, verdict_bad, why):
    v = p(name)
    if v is None:
        row(UNKNOWN, name, "讀不到", "不知道",
            "沒有這個參數的值就不能說它是開的" if not params else why)
        return
    good = ok_fn(v)
    age = ages.get(name)
    mins = (time.time() - age) / 60 if age else None
    got = f"{v:g}" + (f"（{mins:.0f} 分前）" if mins is not None and a.tlog else "")
    # **舊值不能拿來下結論，不論是哪個方向的結論。** tlog 裡的參數只在有人
    # 問過時才更新，所以一個 54 分鐘前的 `BATT_FS_LOW_ACT=0` 既不證明它現在
    # 是 0，也不證明它不是——2026-09-02 實測就是這樣：改完之後那幾列還顯示
    # 舊值，報表上是四個 ✗，而實際上它們都已經改好了
    if a.tlog and mins is not None and mins > a.max_age_min:
        row(UNKNOWN, name, got, "不知道（值太舊）",
            f"這個值是 {mins:.0f} 分鐘前的。tlog 裡的參數只在有人問過時才更新，"
            f"**舊值不能用來下任何結論**——要判定請用 --dev 現讀")
        return
    row(PASS if good else FAIL, name, got,
        verdict_ok if good else verdict_bad, why)


print("── 第 1 層：飛控自己的 failsafe ────────────────────────────")
chk_param("BATT_MONITOR", lambda v: v != 0, "有在量電池", "**完全沒有在量電池**",
          "0＝沒有電池監測，下面所有電池相關的守門都不存在")
chk_param("BATT_FS_LOW_ACT", lambda v: v != 0, "低電量會動作",
          "**偵測得到但不動作**",
          "0＝None。它仍然會在地面擋你解鎖，但**在天上什麼都不做**")
chk_param("BATT_FS_CRT_ACT", lambda v: v != 0, "危險電量會動作",
          "**偵測得到但不動作**", "同上，這是最後一道電池防線")
if a.cells:
    lo, hi = 3.3 * a.cells, 3.7 * a.cells
    chk_param("BATT_LOW_VOLT", lambda v: lo <= v <= hi,
              f"{a.cells}S 合理（{lo:.1f}–{hi:.1f} V）", "**不像是這顆電池的門檻**",
              f"低電量門檻要落在 3.3–3.7 V/cell。設給另一種電池的話，"
              f"不是永遠觸發就是永遠不觸發")
    clo, chi = 3.0 * a.cells, 3.5 * a.cells
    lowv = p("BATT_LOW_VOLT")
    chk_param("BATT_CRT_VOLT",
              lambda v: clo <= v <= chi and (lowv is None or v < lowv),
              f"{a.cells}S 合理且低於 LOW", "**沒設或不合理**",
              "0＝critical 那層連門檻都沒有；且必須低於 BATT_LOW_VOLT")
else:
    row(UNKNOWN, "BATT_LOW_VOLT / BATT_CRT_VOLT",
        f"{p('BATT_LOW_VOLT')} / {p('BATT_CRT_VOLT')}", "不知道",
        "**沒給 --cells 就不判定**——從遙測推電芯數在接電源供應器時會推錯")

chk_param("FS_THR_ENABLE", lambda v: v != 0, "遙控器失聯會動作", "**不動作**",
          "這是三層防線的第一層：人隨時可以接管的前提")
chk_param("FS_GCS_ENABLE", lambda v: v != 0, "地面失聯會動作", "**不動作**",
          "飛控那層是代理的後備——代理自己掛掉時，沒有它就沒有人接手")
chk_param("FS_EKF_ACTION", lambda v: v != 0, "定位發散會動作", "**不動作**",
          "EKF 發散＝飛機不知道自己在哪，而它還在按那個位置飛")
chk_param("FS_CRASH_CHECK", lambda v: v != 0, "墜地會自動上鎖", "**不會**",
          "撞地後馬達繼續轉會把機體與現場都弄得更糟")
chk_param("FS_VIBE_ENABLE", lambda v: v != 0, "振動失效會處理", "**不處理**",
          "振動讓 EKF 的高度發散，症狀是自己往上衝或往下掉")
chk_param("ARMING_CHECK", lambda v: v == 1, "全部預檢都開", "**預檢被放寬了**",
          "1＝全開。放寬任何一項都要說得出為什麼——這是起飛前唯一的自動守門")
chk_param("FENCE_ENABLE", lambda v: v != 0, "圍欄開著", "**圍欄是關的**",
          "全自動任務沒有地理邊界。**這與「地面站不設預設圍欄」是兩件事**"
          "（那條 2026-08-26 的裁定講的是航線預檢，不是飛控的圍欄）")
chk_param("RTL_ALT", lambda v: v > 0, "有設返航高度", "**沒設**",
          "0＝維持當前高度返航。場地有障礙時那就是直接撞上去——"
          "**這一格的判準要由場地決定，機器只查得出它不是 0**")

print("\n── 第 2 層：機上代理 ──────────────────────────────────────")
env = {}
try:
    out = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", a.ssh,
                          "systemctl show uav-agent -p Environment --value; "
                          "systemctl is-active uav-agent"],
                         capture_output=True, text=True, timeout=45).stdout
    *envline, active = [x for x in out.strip().split("\n") if x]
    for tok in " ".join(envline).split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            env[k] = v
    row(PASS if active == "active" else FAIL, "uav-agent", active,
        "在跑" if active == "active" else "**沒在跑**",
        "它掛掉＝第二層防線不存在，而且飛控與地面站之間沒有橋")
except Exception as e:
    row(UNKNOWN, "uav-agent", f"連不上（{e}）", "不知道", "查不到就不能說它是好的")

if env:
    act = env.get("LINK_ACTION", "rtl")
    row(PASS if act != "none" else FAIL, "LINK_ACTION", act,
        "失聯會處置" if act != "none" else "**只告警不動作**",
        "none＝代理看到地面失聯也不做任何事")
    try:
        solo = float(env.get("LINK_LOSS_MAX_SOLO_S", 30))
        actio = float(env.get("LINK_ACTION_S", 10))
        gcs = p("FS_GCS_TIMEOUT")
        ok = actio < solo and (gcs is None or gcs > solo)
        row(PASS if ok else FAIL, "不變式 FS_GCS_TIMEOUT > 單飛上限 > 處置起算",
            f"{gcs} > {solo} > {actio}",
            "成立" if ok else "**不成立**",
            "處置起算 ≥ 單飛上限時，「任務中續飛」那條分支永遠走不到——"
            "選項 C 安靜退化成「一律 RTL」；FS_GCS_TIMEOUT 太小則飛控會搶先動作")
    except ValueError:
        row(UNKNOWN, "不變式", "環境變數解不出來", "不知道", "")

print("\n── 第 3 層：地面站 ────────────────────────────────────────")
try:
    out = subprocess.run(["docker", "compose", "ps", "--format", "{{.Name}} {{.State}}"],
                         capture_output=True, text=True, cwd=a.compose_dir,
                         timeout=20).stdout
    st = dict(l.split() for l in out.strip().split("\n") if len(l.split()) == 2)
    for name, why in [("uav-heartbeat", "GCS 心跳停了，飛控會在 FS_GCS_TIMEOUT 後"
                                        "判定失聯——而 command 的 /healthz 仍然說一切正常"),
                      ("uav-command", "指令通道，人下不了任何指令"),
                      ("uav-backend", "遙測與錄製；它掛了不影響飛安，但看不到東西")]:
        s_ = st.get(name, "不存在")
        row(PASS if s_ == "running" else FAIL, name, s_,
            "在跑" if s_ == "running" else "**沒在跑**", why)
except Exception as e:
    row(UNKNOWN, "容器狀態", str(e), "不知道", "")

try:
    h = json.loads(urllib.request.urlopen(f"{a.command}/healthz", timeout=5).read())
    ok = h.get("ok") is True or h.get("status") == "ok"
    row(PASS if ok else FAIL, "command /healthz", json.dumps(h, ensure_ascii=False)[:60],
        "正常" if ok else "**不正常**", "殭屍 router 會照回 ok，所以這一格不是充分條件")
except Exception as e:
    row(UNKNOWN, "command /healthz", str(e)[:40], "不知道", "")

# ── 報表 ────────────────────────────────────────────────────────
print("\n" + "═" * 78)
w = max(len(r[1]) for r in rows)
for mark, item, got, verdict, why in rows:
    print(f"{mark} {item:<{w}}  {got:<24} {verdict}")
    if mark != PASS and why:
        print(f"{'':>{w + 3}}  └─ {why}")
n_fail = sum(1 for r in rows if r[0] == FAIL)
n_unk = sum(1 for r in rows if r[0] == UNKNOWN)
print("═" * 78)
print(f"通過 {sum(1 for r in rows if r[0] == PASS)}　"
      f"**沒過 {n_fail}**　不知道 {n_unk}")
if n_unk:
    print("**「不知道」不算通過。** 查不到就是查不到，不要當成沒問題。")
sys.exit(1 if n_fail else 0)
