"""任務幾何預檢：離線檢查路徑 vs 圍欄與方言規則，在上傳前抓出機端會拒絕的任務。

源自現場工具 check_plan.py（2026-08-10 整合進系統）。要點：

  1. **無座標項的 frame 是方言**：由 `libs/autopilot` 的驅動提供可接受值。
     不知道目標機種時只警告不擋——用猜的去否定一份可能合法的航線更糟。
  2. 圍欄**優先用 .plan 自帶的 geoFence**，沒帶才退回系統預設，而且報告
     要說出用的是哪一個（`fence_source`）。
  3. 高度上限永遠是系統設定：QGC 的 geoFence 只畫平面。
  4. 首導航項應為起飛（problem）；末項應為降落（warning——.plan 以 RTL
     結尾時 RTL 不入庫，屬正常）。

兩個消費端、兩種嚴格度：
  - POST /missions（匯入 .plan）：回報告**不擋存檔**——任務庫可放草稿
  - command 服務上傳到機：有 problem 直接 409——那才是安全門

**這裡是唯一一份實作。** 原本 backend 與 command 各有一份「同源副本」，
2026-08-26 發現它們早就漂移了：frame 檢查只存在於 backend 那份，於是
匯入時擋下來的東西，上傳到機時反而不擋。同源副本靠人記得同步是行不通的。
"""
import json
import math

import autopilot as _autopilot


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 111320.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


# 導航類指令（有實際飛行位置）；DO_*（如 178 改速度）是設定類，
# 可出現在任何位置、不計距離——與現場工具 check_plan.py 的語意一致
NAV_CMDS = {16, 17, 18, 19, 20, 21, 22}
_TAKEOFF, _RTL, _LAND = 22, 20, 21


#: 離地高度的警戒線（m）。**低於 0 是「會撞地」，0～這個值之間是「太貼了」**——
#: 分成兩級是因為 SRTM 的相對高程誤差本來就有數公尺，把 1.5 m 的餘裕報成
#: 「安全」跟報成「會撞」一樣不誠實。
MIN_CLEARANCE_M = 2.0

#: 帶相對高度的 frame：高度的意思是「離起飛點」，**不是離地**。
#: 10（`GLOBAL_TERRAIN_ALT`）是飛控自己跟地形，不在這裡檢查。
_REL_FRAMES = {3, 6}
_AMSL_FRAMES = {0, 5}

#: 沿線取樣步長（m）＝ DEM 的解析度。SRTM 1 弧秒約 30 m，
#: 取得比它密只是把同一格內插出來的值再讀一次。
DEM_STEP_M = 30.0

def _cmd(w: dict) -> int | None:
    """航點的 MAV_CMD：新資料帶原始 command；舊資料從 action 推回。"""
    if w.get("command") is not None:
        return int(w["command"])
    return {"takeoff": 22, "land": 21, "rtl": 20,
            "waypoint": 16}.get(w.get("action") or "waypoint")


#: **最低起飛高度**（m，使用者裁定 2026-09-08）。兩個用途共用同一個數字：
#:
#: 1. 航線沒說起飛高度時的保底值（`mission_fly`／`group_exec` 為了讓機離開
#:    地面、滿足切 AUTO 的前提而用）。
#: 2. 規劃時的下限：航線寫得比它低就是一個發現。
#:
#: **一套系統不該有兩個不同的「最低起飛高度」**——分成兩個數字，遲早會出現
#: 「規劃時說 1.5 是下限，實際飛的保底卻是 1.0」這種自己打自己的狀況。
#:
#: 與 `LOW_ALT_M`（3 m）是**不同的規則**，兩者並存是自洽的：起飛到 1.5 m
#: 只是離地懸停，而 `LOW_ALT_M` 管的是「在那個高度**帶著速度移動**」。
#: 所以 1.5 m 起飛合法，但從它出發的第一段必須慢，或者先爬升。
FALLBACK_TAKEOFF_ALT = 1.5
MIN_TAKEOFF_ALT_M = FALLBACK_TAKEOFF_ALT


def takeoff_alt(wps: list[dict]) -> tuple[float | None, str]:
    """這份航線的起飛高度 →（高度, 依據）。沒有可用的起飛項時回 `(None, 原因)`。

    **不在這裡套保底值**：要不要退回 `FALLBACK_TAKEOFF_ALT` 是呼叫端的政策，
    這個函式只回答「這份航線自己說了什麼」。回不出來時說得出為什麼——
    「航線裡沒有起飛項」與「有起飛項但高度是 0」對操作員是兩件事。

    `wps` 是本系統 waypoints 模型（`command` 已從 params 解出來，同
    `check_waypoints`）；舊資料沒有 `command` 時由 `_cmd` 從 action 回推。

    **這裡是唯一一份實作**：切 AUTO 前那一段離地高度原本在單機路徑寫死 10.0、
    在群飛路徑寫死 10.0，兩個常數各自漂——而一份 takeoff 2 m、航點 3 m 的低空
    航線會因此被拉到規劃的五倍高（2026-09-07 使用者回報）。
    """
    for w in wps:
        if _cmd(w) != _TAKEOFF:
            continue
        alt = w.get("alt")
        if alt is not None and alt > 0:
            return float(alt), "航線的 NAV_TAKEOFF"
        return None, f"航線的 NAV_TAKEOFF 高度是 {alt}"
    return None, "航線裡沒有 NAV_TAKEOFF"


def _is_nav(w: dict) -> bool:
    c = _cmd(w)
    return c is None or c in NAV_CMDS


def check_waypoints(wps: list[dict], fence_r: float, fence_alt: float,
                    margin: float = 0.7, fence: dict | None = None,
                    autopilot: int | None = None,
                    home: list[float] | None = None,
                    dem=None, min_clear: float = MIN_CLEARANCE_M,
                    wp_spd: float | None = None,
                    wp_radius: float | None = None) -> dict:
    """wps：本系統 waypoints 模型 [{seq, lat, lon, alt, action, command?}]。
    DO_* 設定類不計距離；回傳 {ok, problems, warnings, max_dist_m, ...}。

    `dem` 給了才做地形預檢（`libs.terrain.Dem`）——**不給不等於通過**，
    報告裡會有一句「離地高度沒有檢查」。
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not wps:
        return {"ok": False, "problems": ["沒有航點"], "warnings": [],
                "max_dist_m": 0.0, "max_alt_m": 0.0, "fence_source": "none"}

    # 無座標項（RTL、CONDITION_*、DO_*）的 frame **是方言，不是通則**。
    #
    # 2026-08-12 在 PX4 SITL 實測「RTL 配 frame 0/3/5/6 全拒、只有 2 過」，
    # 那條結果被當成通則寫死在這裡。2026-08-26 它擋下了一份**從 ArduPilot
    # 自己下載回來的**任務——ArduPilot 存 RTL 用 frame 0，下載時原樣回報，
    # 於是我們拿 PX4 的規則去否定 ArduPilot 自己的表示法。
    # **一家的實測結果被當成兩家的事實**，正是 issues/026 要收掉的洩漏。
    #
    # 現在規則由驅動提供；**不知道目標機種時只警告不擋**——兩家不同的事，
    # 在不知道是哪一家的情況下擋下來，等於用猜的去否定一份可能完全合法的航線。
    drv = _autopilot.get_driver(autopilot) if autopilot is not None else None
    allowed = drv.no_coord_frames if drv else None
    for w in wps:
        c, fr = _cmd(w), w.get("frame")
        if c is None or fr is None:
            continue
        if not (c == _RTL or c >= 112):
            continue
        if allowed is None:
            if int(fr) != 2:
                warnings.append(
                    f"seq {w.get('seq')}：command={c} 是無座標項、frame={fr}。"
                    "PX4 只吃 frame 2（其餘整包拒收），ArduPilot 兩者都吃——"
                    "**這份航線沒宣告目標機種，所以無法判定**")
        elif int(fr) not in allowed:
            problems.append(
                f"seq {w.get('seq')}：command={c} 是無座標項，frame 目前是 {fr}，"
                f"但 {_autopilot.autopilot_name(autopilot)} 只接受 "
                f"{sorted(allowed)}——機端會拒收整包任務")

    nav = [w for w in wps if _is_nav(w)]
    if not nav:
        problems.append("沒有任何導航項目")
    else:
        if _cmd(nav[0]) != _TAKEOFF:
            problems.append(f"第一個導航項不是起飛（command={_cmd(nav[0])}）")
        if _cmd(nav[-1]) not in (_RTL, _LAND):
            warnings.append(f"最後一個導航項不是返航/降落（command={_cmd(nav[-1])}）")

    # 距離的原點優先用 **.plan 的 plannedHomePosition**：起飛項在很多 .plan
    # 裡是 0,0（起飛只需要高度），拿「第一個有座標的航點」當原點會把整條
    # 航線的距離量錯一整段
    origin = ({"lat": home[0], "lon": home[1]} if home and len(home) >= 2
              and (home[0] or home[1]) else None)
    home = origin or next((w for w in nav if w.get("lat") and w.get("lon")), None)
    if home is None:
        return {"ok": False, "problems": problems + ["找不到帶座標的導航項"],
                "warnings": warnings, "max_dist_m": 0.0, "max_alt_m": 0.0,
                # 這裡原本回 `fence_src`，而它要到下面才指派——這條路徑一走就
                # NameError，等於「沒有帶座標的導航項」這個錯誤永遠報不出來
                "fence_source": "none"}

    # **圍欄優先用航線自己宣告的**（QGC .plan 的 geoFence）。系統預設值是
    # 「這套系統只在一個場地飛」才成立的假設，而測繪任務與定點巡檢的合理範圍
    # 可以差一個數量級。沒宣告才退回預設，而且訊息要說出用的是哪一個——
    # 使用者必須分得出「這份航線宣告了 N m 而你超出」與「這份沒宣告，
    # 我拿系統預設在量」，後者多半代表**預設值該改，不是航線該改**
    fence_src = "plan" if fence else "none"   # none＝這份沒宣告，系統也不替它設
    if fence:
        fp, fw = check_fence(nav, fence)
        problems += fp
        warnings += fw

    # **沒有系統預設圍欄**（使用者裁定 2026-08-26）。圍欄是每份航線自己的事，
    # 一個全域數字只對一個場地成立——而它會產生**看起來很具體的假錯誤**：
    # 「seq 6 離起飛點 54 m，超過圍欄半徑 50 m」讀起來像航線有問題，
    # 實際上那 50 是模擬環境留下來的值，跟使用者的場地毫無關係。
    #
    # 沒宣告圍欄時只報事實（最遠多少、最高多少），不判對錯——**我們不知道
    # 這個場地允許飛多遠，就不要假裝知道**。
    max_d, max_alt = 0.0, 0.0
    for w in nav:
        if w.get("lat") and w.get("lon"):
            max_d = max(max_d, _dist_m(home["lat"], home["lon"],
                                       w["lat"], w["lon"]))
        alt = w.get("alt")
        if alt is not None:
            max_alt = max(max_alt, float(alt))

    if not fence:
        warnings.append(
            f"這份航線沒有宣告圍欄（.plan 的 geoFence 是空的）——"
            f"最遠航點離起飛點 {max_d:.0f} m、最高 {max_alt:.0f} m，"
            "**系統不替你設一個範圍**，這條航線適不適合這個場地要你自己判斷。"
            "要讓系統幫你擋，在 QGC 的 Plan 頁畫一個 GeoFence 再存檔")
    # 地形預檢（issues/047 §1-B）。擺在最後：它需要上面解出來的起飛點，
    # 而且它的發現要跟圍欄/機種的發現混在同一組 problems/warnings 裡
    # ——前端已經會顯示那兩組，多開一個顯示點就多一個沒人接的欄位（issues/037）
    terr = check_terrain(nav, home, dem=dem, min_clear=min_clear)
    problems += terr["problems"]
    warnings += terr["warnings"]

    # 逐段剖面（issues/048）：離地與速度的**組合**。`check_terrain` 問的是
    # 「會不會低於地面」，而 2026-09-07 那一趟離地 1.5 m 是正的、會被放行
    # ——「在地面上方」不等於「飛得安全」。
    prof = leg_profile(wps, home, dem=dem, home_amsl=None,
                       wp_spd=wp_spd, wp_radius=wp_radius)
    problems += prof["problems"]
    warnings += prof["warnings"]

    return {"ok": not problems, "problems": problems, "warnings": warnings,
            "max_dist_m": round(max_d, 1), "max_alt_m": round(max_alt, 1),
            "terrain": terr["terrain"],
            # 逐段的事實：剖面圖、逐段表、自動修正共用的輸入
            "legs": prof["legs"],
            # **門檻要跟著出來**：畫面要在剖面圖上畫那條線，而它不該自己
            # 寫死一個數字（見 `leg_profile` 裡 `low_fast` 的說明）
            "limits": {"low_alt_m": LOW_ALT_M, "low_speed_ms": LOW_SPEED_MS,
                       "min_takeoff_alt_m": MIN_TAKEOFF_ALT_M},
            # **量測用的是哪一份圍欄，要跟著報告走**：同一句「超出圍欄」在
            # 兩種來源下的處置完全不同
            "fence_source": fence_src}



def check_terrain(nav: list[dict], home: dict, dem=None,
                  min_clear: float = MIN_CLEARANCE_M,
                  home_amsl: float | None = None) -> dict:
    """地形預檢（issues/047 §1-B）：沿著整條航線算 `預期離地`，不足的指名報出來。

        預期離地 = (起飛點 AMSL + 相對高度) − DEM 高程(該點)

    **不是只檢查航點，是檢查整條線**：兩個航點之間隔著一個土坡，
    兩端各有 5 m 餘裕、中間是 −2 m ——只看航點的檢查會說「通過」。
    取樣步長跟著 DEM 的解析度走（30 m），取更密只是把同一格內插出來的
    數字再讀一次，不會多知道任何事。

    `home_amsl` 給定時用它（上傳前跟飛控核對的那條路），否則用 DEM 查起飛點
    ——後者會讓 DEM 的**絕對**誤差在相減時消掉，見 `libs/terrain` 的說明。

    回 `{problems, warnings, terrain}`；`terrain` 是給畫面用的結構化結果，
    `source` 說得出這次到底是**查了**還是**沒得查**。
    """
    out = {"problems": [], "warnings": [],
           "terrain": {"source": "none", "checked": 0, "skipped": 0}}
    if dem is None or not dem.available:
        out["warnings"].append(
            "**離地高度沒有檢查**：地面站沒有地形資料。航點的相對高度是"
            "「離起飛點」，地面沿路往上抬多少就吃掉多少離地空間")
        return out

    ha = home_amsl if home_amsl is not None else dem.elevation(
        home["lat"], home["lon"])
    if ha is None:
        out["warnings"].append(
            f"**離地高度沒有檢查**：起飛點（{home['lat']:.5f}, "
            f"{home['lon']:.5f}）沒有地形資料，缺圖磚 "
            f"{'、'.join(sorted(dem.missing)) or '未知'}")
        return out

    # ── 把航線攤成「一條有高度的折線」，全部換算成 AMSL ────────────
    #
    # * `frame 10`（跟地形）不參加：那是飛控自己在跟地面，這裡的算法會
    #   把它的高度誤讀成「離起飛點」。
    # * **降落點用的是它前一點的高度**，不是 0。飛機是先平飛到降落點上方
    #   再往下——真正要檢查的是「平飛過去的那一段會不會撞到」，而降落點
    #   本身離地 0 是它的目的，不是錯誤。
    # `amsl` 是 None ＝**這一點的地面要量，但離地高度不歸這裡算**
    # （`frame 10` 交給飛控自己跟）。地面起伏是地面的性質，跟高度用哪個
    # 基準無關——而 `max_rise_m` 正是 `check_terrain_ready` 用來判斷
    # 「地形資料失效時那次返航爬得夠不夠高」的依據，**不能因為航線改成
    # 地形跟隨就變成 0**
    pts: list[tuple[float, float, float | None, int]] = []
    skipped = 0
    for w in nav:
        lat, lon, alt = w.get("lat"), w.get("lon"), w.get("alt")
        fr = w.get("frame")
        fr = 3 if fr is None else int(fr)
        if not lat or not lon:
            skipped += 1
            continue
        if alt is None or fr not in _REL_FRAMES | _AMSL_FRAMES:
            skipped += 1
            pts.append((lat, lon, None, w.get("seq")))
            continue
        a = float(alt) if fr in _AMSL_FRAMES else ha + float(alt)
        if _cmd(w) in (_LAND, _RTL):
            a = pts[-1][2] if pts else None
        pts.append((lat, lon, a, w.get("seq")))

    worst = None      # (離地, 說法, 地面高程)
    below = []
    rise = 0.0
    checked = ground_n = 0

    def look(lat, lon, amsl, where):
        nonlocal worst, rise, checked, ground_n
        gz = dem.elevation(lat, lon)
        if gz is None:
            return
        ground_n += 1
        rise = max(rise, gz - ha)
        if amsl is None:          # 只量地面，不判離地
            return
        checked += 1
        rec = (amsl - gz, where, gz)
        if worst is None or rec[0] < worst[0]:
            worst = rec
        if rec[0] < min_clear:
            below.append(rec)

    for i, (lat, lon, a, seq) in enumerate(pts):
        look(lat, lon, a, f"seq {seq}")
        if i + 1 >= len(pts):
            break
        lat2, lon2, a2, seq2 = pts[i + 1]
        leg = _dist_m(lat, lon, lat2, lon2)
        if leg <= 0:
            continue
        # 每段最多 200 個取樣點：一條 20 km 的航線不該讓預檢跑上千次查表
        step = max(DEM_STEP_M, leg / 200.0)
        n = int(leg // step)
        for k in range(1, n + 1):
            f = k * step / leg
            # 兩端有任一端不判離地（frame 10）時，中間也只量地面
            mid = None if (a is None or a2 is None) else a + (a2 - a) * f
            look(lat + (lat2 - lat) * f, lon + (lon2 - lon) * f, mid,
                 f"seq {seq}→{seq2} 之間（離 seq {seq} 約 {k * step:.0f} m）")

    out["terrain"] = {
        "source": "srtm" if home_amsl is None else "srtm+fc",
        "home_amsl_m": round(ha, 1), "checked": checked, "skipped": skipped,
        "step_m": DEM_STEP_M, "max_rise_m": round(rise, 1),
        "min_clearance_m": round(worst[0], 1) if worst else None,
        "min_clearance_at": worst[1] if worst else None,
        "below_count": len(below),
        # **要顯示的句子跟著結構走。** 上傳成功那條路上，前端只拿得到
        # 一個 `check`，而 `warnings` 裡混著圍欄、機種、frame 方言的話——
        # 全部顯示會變成沒人讀的一大段（使用者：字太多）。把地形這幾句
        # 單獨列出來，畫面才挑得出「這次真正該看的是哪幾行」
        "notes": [],
    }
    if not checked:
        # **「都是地形跟隨」與「查不到地形資料」是兩件事**，說錯了會讓人
        # 以為自己剛做的改寫沒生效。分辨的依據是地面到底量到了沒
        out["warnings"].append(
            "這份航線的高度是**離地面**（frame 10），跟著地面走的是飛控自己的"
            "地形圖庫——地面站不重複判斷離地"
            if ground_n else
            f"**離地高度沒有檢查**：這份航線沿線都查不到地形資料"
            f"（缺圖磚 {'、'.join(sorted(dem.missing)) or '未知'}）")
        return out
    if below:
        notes = out["terrain"]["notes"]
        c, where, gz = min(below)
        d = gz - ha
        more = f"，另有 {len(below) - 1} 處同樣不足" if len(below) > 1 else ""
        rel = (f"地面比起飛點高 {d:.1f} m" if d >= 0.05 else
               f"地面比起飛點低 {-d:.1f} m" if d <= -0.05 else "地面與起飛點齊平")
        if c < 0:
            out["problems"].append(
                f"{where}：{rel}，**預期離地 {c:.1f} m——這一段會撞地**{more}")
            notes.append(out["problems"][-1])
        elif d < min_clear / 2:
            # **餘裕不足，但不是地形造成的**：地形在這裡是平的，是航線
            # 自己就規劃在這個高度。這兩件事的處置完全不同（一個是改航線
            # 繞開地形，一個是「你要用比資料誤差還小的餘裕飛」），
            # 混成同一句話會讓真正的地形警告被當成雜訊忽略
            out["warnings"].append(
                f"{where}：**預期離地只有 {c:.1f} m**{more}。{rel}——"
                f"**這不是地形造成的**，是航線本身就規劃在這個高度")
            notes.append(out["warnings"][-1])
        else:
            out["warnings"].append(
                f"{where}：{rel}，**預期離地只有 {c:.1f} m**{more}")
            notes.append(out["warnings"][-1])
        # 誠實話**跟著發現走**，不是每次都念一遍：有東西可報的時候，
        # 操作員才需要知道這份資料的解析度撐不撐得住他要做的決定
        out["warnings"].append(
            "地形資料是 SRTM（水平約 30 m、只有地形不含樹木電線）——"
            "它能防「整片地高了幾公尺」，防不了「前面有個 1 m 土堆」。"
            "真的要貼地飛需要測距儀")
        notes.append(out["warnings"][-1])
    return out


#: 地形跟隨（`frame 10`）要飛控自己有地形資料才成立。這幾個參數決定
#: 「它有沒有」與「沒有的時候會怎樣」——後者才是危險的地方。
TERRAIN_FRAME = 10


def to_terrain_frame(nav: list[dict], home: dict, dem=None,
                     home_amsl: float | None = None) -> dict:
    """把 `frame 3`（相對起飛點）的航點改寫成 `frame 10`（相對地形）。

    新高度就是地形預檢算的那個 `預期離地`：

        地形高度 = (起飛點 AMSL + 相對高度) − DEM 高程(該點)

    **不轉的項目，以及為什麼**

    | 項目 | 保持原樣的理由 |
    |---|---|
    | 起飛（22）| ArduPilot 的起飛高度是相對 home，地形框在這裡沒有意義 |
    | 降落（21）| LAND 本來就是降到地面 |
    | RTL、DO_*、CONDITION_* | 不帶座標，frame 2 |

    **查不到高程就整份不轉。** 一份一半 frame 3、一半 frame 10 的航線，
    高度的意思在中途換了定義——那比不轉更危險。

    回 `{ok, waypoints, problems, warnings, converted, kept}`。
    `waypoints` 只在 `ok` 為真時有內容。
    """
    out = {"ok": False, "waypoints": [], "problems": [], "warnings": [],
           "converted": 0, "kept": 0}
    if dem is None or not dem.available:
        out["problems"].append(
            "地面站沒有地形資料，算不出每個航點該離地多少——無法改寫成地形跟隨")
        return out
    ha = home_amsl if home_amsl is not None else dem.elevation(
        home["lat"], home["lon"])
    if ha is None:
        out["problems"].append("起飛點沒有地形資料，算不出基準高度")
        return out

    new: list[dict] = []
    for w in nav:
        c = _cmd(w)
        lat, lon, alt = w.get("lat"), w.get("lon"), w.get("alt")
        fr = w.get("frame")
        fr = 3 if fr is None else int(fr)
        if (c in (_TAKEOFF, _LAND, _RTL) or not lat or not lon or alt is None
                or fr not in _REL_FRAMES):
            new.append(dict(w))
            out["kept"] += 1
            continue
        gz = dem.elevation(lat, lon)
        if gz is None:
            out["problems"].append(
                f"seq {w.get('seq')}（{lat:.5f}, {lon:.5f}）查不到地形高程"
                f"，缺圖磚 {'、'.join(sorted(dem.missing)) or '未知'}")
            continue
        agl = round(ha + float(alt) - gz, 1)
        if agl <= 0:
            out["problems"].append(
                f"seq {w.get('seq')}：改寫後的地形高度是 {agl:.1f} m"
                "——這個航點本來就在地面下，改 frame 不會讓它變得可飛")
            continue
        nw = dict(w)
        nw["frame"] = TERRAIN_FRAME
        nw["alt"] = agl
        params = dict(nw.get("params") or {})
        if params:
            params["frame"] = TERRAIN_FRAME
            nw["params"] = params
        new.append(nw)
        out["converted"] += 1

    if out["problems"]:
        return out
    if not out["converted"]:
        out["problems"].append("這份航線沒有可以改寫的航點（起飛/降落/RTL 不轉）")
        return out
    out["ok"] = True
    out["waypoints"] = new
    out["warnings"].append(
        "改寫之後高度的意思變成**離地面**，而跟著地面走的是飛控自己的地形圖庫"
        "——上傳前會檢查那台機的地形設定")
    return out


def check_terrain_ready(params: dict, max_rise_m: float = 0.0,
                        min_clear: float = MIN_CLEARANCE_M) -> dict:
    """這台機的設定撐不撐得住 `frame 10`。`params`＝飛控回報的參數值。

    **最危險的一條不是「有沒有地形資料」，是「沒有的時候會怎樣」。**
    ArduCopter 的地形資料失效處置是：兩秒讀不到就轉 RTL，而那次 RTL
    **把 `RTL_ALT_M` 當成「離 home」在飛**（不是離地形，與 `RTL_ALT_TYPE`
    無關）。所以一台 `RTL_ALT_M = 2` 的機在起伏地形上做地形跟隨，
    失效處置本身就是撞地——這正是 2026-09-07 那趟的形狀。

    回 `{problems, warnings}`；`problems` 非空就不該上傳 frame 10 的航線。
    """
    pr: list[str] = []
    wn: list[str] = []

    def g(name):
        v = params.get(name)
        return None if v is None else float(v)

    en = g("TERRAIN_ENABLE")
    if en is None:
        wn.append("讀不到 TERRAIN_ENABLE——這台機是不是支援地形資料，無法確認")
    elif en != 1:
        pr.append(f"TERRAIN_ENABLE = {en:g}：飛控沒有開地形資料，"
                  "frame 10 的航點它跟不了地面")

    # 需要的返航高度＝沿線地面比起飛點高多少 ＋ 一份離地餘裕
    need = max_rise_m + min_clear
    rtl = g("RTL_ALT_M")
    if rtl is None:
        rtl = g("RTL_ALT")
        rtl = None if rtl is None else rtl / 100.0     # 4.7 之前是 cm
    if rtl is None:
        wn.append("讀不到返航高度——地形資料失效時飛機會爬到多高，無法確認")
    elif rtl < need:
        pr.append(
            f"**返航高度 {rtl:g} m 不夠**：地形資料一斷（兩秒讀不到）飛控就轉返航，"
            f"而那次返航是照「離起飛點 {rtl:g} m」在飛。這條航線沿線的地面"
            f"比起飛點高到 {max_rise_m:.1f} m，回程會撞上去——"
            f"要用地形跟隨，返航高度至少 {need:.1f} m")

    if g("RNGFND1_TYPE") in (0.0, None):
        wn.append("這台機沒有測距儀，跟地面靠的是飛控裡的地形圖庫，不是實測")
    sp = g("TERRAIN_SPACING")
    if sp:
        wn.append(f"飛控的地形格距是 {sp:g} m——**比地面站檢查用的 30 m 還粗**，"
                  "它跟的是那個解析度下的地面")
    return {"problems": pr, "warnings": wn}


# ── 逐段剖面（issues/048、doc/route-planning-first-principles.md）────────
#
# **每一個常數都要說得出出處，而且樣本數 1 的要標樣本數 1。**

#: 低空的界線（m）。**不是算出來的**：2026-09-07 出事在離地 1.5 m，往外留。
#: 教科書的地效區是 1–2 倍槳徑（這台約 0.5–0.7 m），**與實際出事的高度對不上**
#: ——所以不拿槳徑推算，用實際事故的高度往安全側取，並且每台機可以改。
#: 想量出這台機真正的界線：在不同高度各懸停 20 秒，看氣壓高度（`CTUN.BAlt`）
#: 的抖動從哪裡開始收斂——09-07 在 1.6 m 懸停時它在 1.45～2.15 之間跳，
#: 而 EKF 融合後的高度是平的，**那個差就是「地面還在干擾這架飛機」**。
LOW_ALT_M = 3.0
#: 低空時容許的速度上限（m/s，使用者裁定 2026-09-08）。同一個場地
#: 0.3 m/s 貼地飛過好幾趟沒事，出事那趟是加速通過 5 m/s。
LOW_SPEED_MS = 1.0
#: 轉彎的提前量（秒）。**樣本數 1**：2026-09-07 實測 8 m/s 配 2 m 到達半徑
#: 過衝約 9 m。報告裡一律寫「估計」並附這句出處——用單一樣本產生一個看起來
#: 精確的數字，比不給還糟。
TURN_LEAD_S = 1.1
#: 轉角超過這個度數就算「急轉」。90° 是直角，超過就是要往回折。
SHARP_TURN_DEG = 90.0

_DO_CHANGE_SPEED = 178


def _bearing(lat1, lon1, lat2, lon2) -> float:
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(math.radians(lon2 - lon1)))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def leg_profile(wps: list[dict], home: dict | None = None, dem=None,
                home_amsl: float | None = None, wp_spd: float | None = None,
                wp_radius: float | None = None,
                low_alt: float = LOW_ALT_M,
                low_speed: float = LOW_SPEED_MS) -> dict:
    """把一份航線攤成**逐段的事實**：離地、有效速度與它的來源、長度、轉角。

    這是剖面圖、逐段表、自動修正、擋門**四個功能共同的輸入**，所以它只算
    事實與發現，不決定要不要擋——那是呼叫端的政策。

    ## 有效速度為什麼要自己算

    **`DO_CHANGE_SPEED` 只從被執行到的那一項之後才生效。** 2026-09-07：
    使用者以為全程 0.3 m/s，而起飛到第一個航點那一段用的是機上的 `WP_SPD`
    （實測 8 m/s）——那正是「速度感覺不是我設的」的答案。

    所以每一段的速度是「上一次**在它之前**出現的 `DO_CHANGE_SPEED`」，
    沒有的話就是 `wp_spd`。而 `wp_spd` 只有飛機說得準：**沒給就是不知道**，
    那一段的速度相關判定一律不做（`speed_src` 為 `"unknown"`），
    **不是當成安全**。

    回 `{legs: [...], problems: [...], warnings: [...]}`。
    """
    out: dict = {"legs": [], "problems": [], "warnings": []}
    ha = home_amsl
    if ha is None and dem is not None and getattr(dem, "available", False) and home:
        ha = dem.elevation(home["lat"], home["lon"])

    # ── 走一遍，同時解出「每個導航項生效時的速度」────────────────
    speed, src = wp_spd, ("機上 WP_SPD" if wp_spd is not None else "unknown")
    pts: list[dict] = []
    for w in wps:
        c = _cmd(w)
        if c == _DO_CHANGE_SPEED:
            # `params` 在不同呼叫端可能是 dict 或還沒解開的 JSON 字串
            # （`_with_command` 解出 command／frame，但沒把解好的寫回去）
            p = w.get("params") or {}
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except ValueError:
                    p = {}
            v = w.get("p2")
            if v is None:
                v = p.get("p2")
            # p2 <= 0 ＝「不改速度」，不是「速度 0」
            if v is not None and float(v) > 0:
                speed, src = float(v), f"航線 seq {w.get('seq')}"
            continue
        if not _is_nav(w):
            continue
        fr = w.get("frame")
        fr = 3 if fr is None else int(fr)
        lat, lon = w.get("lat"), w.get("lon")
        if not lat or not lon:
            # **起飛項通常是 0,0**（它只說「爬到多高」，位置就是起飛點）。
            # 把它丟掉的話，**第一段就整段消失**——而 2026-09-07 出事的正是
            # 那一段（起飛點→第一個航點，用機上的 WP_SPD 8 m/s 飛）。
            # 有起飛點座標就用它補上；沒有才真的跳過。
            if c == _TAKEOFF and home and home.get("lat") and home.get("lon"):
                lat, lon = home["lat"], home["lon"]
            else:
                continue
        alt = w.get("alt")
        # **降落／返航項的高度用前一點的**：飛機是平飛過去再下降，
        # 照 0 算會讓最後一段的離地變成負的。`check_terrain` 與
        # `route_profile` 都是這樣做的——**三個視圖對同一條航線不能給出
        # 不同的離地**，那種不一致比單一個算錯更難查。
        if c in (_LAND, _RTL) and pts:
            alt, fr = pts[-1]["alt"], pts[-1]["frame"]
        pts.append({"seq": w.get("seq"), "lat": lat, "lon": lon,
                    "alt": alt, "frame": fr, "cmd": c,
                    "speed": speed, "speed_src": src})

    # ── 逐段 ──────────────────────────────────────────────────
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        d = _dist_m(a["lat"], a["lon"], b["lat"], b["lon"])
        leg: dict = {
            "from": a["seq"], "to": b["seq"], "length_m": round(d, 1),
            "speed_ms": b["speed"], "speed_src": b["speed_src"],
            # **判定跟著資料走，呼叫端不要自己再判一次。** 門檻在這裡
            # （`low_alt`／`low_speed`），前端若照著數字重寫一次條件，
            # 改了這裡它不會跟著變——那就是 2026-08-26 抓到的「同源副本
            # 早就漂移了」的同一種錯，而且更難發現（畫面看起來很正常）。
            "low_fast": False,
        }
        # 離地：段內取最低的一點（含兩端）。frame 10 交給飛控，不判
        agl = None
        if dem is not None and getattr(dem, "available", False) and ha is not None:
            samples = []
            n = max(1, int(d // DEM_STEP_M))
            for k in range(n + 1):
                f = k / n
                la = a["lat"] + (b["lat"] - a["lat"]) * f
                lo = a["lon"] + (b["lon"] - a["lon"]) * f
                gz = dem.elevation(la, lo)
                if gz is None or a["alt"] is None or b["alt"] is None:
                    continue
                if a["frame"] in _REL_FRAMES and b["frame"] in _REL_FRAMES:
                    amsl = ha + float(a["alt"]) + (float(b["alt"]) - float(a["alt"])) * f
                elif a["frame"] in _AMSL_FRAMES and b["frame"] in _AMSL_FRAMES:
                    amsl = float(a["alt"]) + (float(b["alt"]) - float(a["alt"])) * f
                else:
                    continue          # frame 10 或混用：不在這裡判
                samples.append(amsl - gz)
            if samples:
                agl = round(min(samples), 1)
        leg["agl_m"] = agl

        # 轉角：**這一段起點那個航點上的轉折**（進來的方向 vs 出去的方向）。
        # 第一段沒有「進來的方向」，所以是 None——不是 0
        if i > 0:
            prev = pts[i - 1]
            b1 = _bearing(prev["lat"], prev["lon"], a["lat"], a["lon"])
            b2 = _bearing(a["lat"], a["lon"], b["lat"], b["lon"])
            leg["turn_deg"] = round(abs((b2 - b1 + 180) % 360 - 180), 0)
        out["legs"].append(leg)

    # ── 發現 ──────────────────────────────────────────────────
    tk = next((p for p in pts if p["cmd"] == _TAKEOFF), None)
    if tk is not None and tk["alt"] is not None \
            and float(tk["alt"]) < MIN_TAKEOFF_ALT_M:
        out["warnings"].append(
            f"起飛高度 {float(tk['alt']):g} m 低於下限 {MIN_TAKEOFF_ALT_M:g} m"
            "——**切 AUTO 之前要先確實離地**，太低的話落地偵測與地效都還在作用")

    v_unknown = any(l["speed_src"] == "unknown" for l in out["legs"])
    if v_unknown:
        out["warnings"].append(
            "**速度沒有檢查**：這一段的速度取決於機上的 `WP_SPD`，而還沒讀過"
            "那台機。連上線之後重看一次——**讀不到不等於沒問題**")

    for l in out["legs"]:
        l["low_fast"] = bool(
            l["agl_m"] is not None and l["agl_m"] < low_alt
            and l["speed_ms"] is not None and l["speed_ms"] > low_speed)
    low = [l for l in out["legs"] if l["low_fast"]]
    if low:
        w = min(low, key=lambda l: (l["agl_m"], -l["speed_ms"]))
        more = f"，另有 {len(low) - 1} 段相同" if len(low) > 1 else ""
        out["problems"].append(
            f"seq {w['from']}→{w['to']}：離地 {w['agl_m']:.1f} m 卻要飛 "
            f"{w['speed_ms']:g} m/s（速度來自{w['speed_src']}）{more}。"
            f"**低於 {low_alt:g} m 時地面會擾動這架飛機**——2026-09-07 就是在 "
            f"1.5 m 加速通過 5 m/s 時失控的。要嘛把這一段抬到 {low_alt:g} m 以上，"
            f"要嘛把速度降到 {low_speed:g} m/s 以下")

    if wp_radius:
        short = [l for l in out["legs"] if l["length_m"] < wp_radius]
        if short:
            l = short[0]
            out["problems"].append(
                f"seq {l['from']}→{l['to']} 只有 {l['length_m']:.1f} m，"
                f"比到達半徑 {wp_radius:g} m 還短——**飛機不會真的飛到那個點**，"
                "它一開始就算「已到達」了")
        for l in out["legs"]:
            v = l["speed_ms"]
            if v is None or l.get("turn_deg") is None:
                continue
            lead = v * TURN_LEAD_S
            # **觸發條件要說得出口**：轉彎的提前量比你給它的到達半徑還大，
            # 就代表飛機**做不到你指定的精度**——它會在半徑之外才轉完。
            # （先前寫的是 `lead > 長度/2`，那個比較沒有可解釋的意義。）
            # 過衝小於 1 m 不值得說——「估計會衝過頭約 0 m」是一句廢話，
            # 而廢話會讓真正的警告被一起忽略
            if l["turn_deg"] >= SHARP_TURN_DEG and lead - wp_radius >= 1.0:
                out["warnings"].append(
                    f"seq {l['from']}→{l['to']}：轉角 {l['turn_deg']:.0f}° 配 "
                    f"{v:g} m/s，轉彎提前量估計 {lead:.1f} m > 到達半徑 "
                    f"{wp_radius:g} m ——**估計會衝過頭約 {lead - wp_radius:.1f} m**"
                    f"（這一段長 {l['length_m']:.0f} m）。這個估計來自"
                    "**單一次實測**（8 m/s 配 2 m 半徑過衝約 9 m，樣本數 1），"
                    "當成量級看，不要當成精確值")
                break
    return out


def route_profile(wps: list[dict], home: dict | None = None, dem=None,
                  home_amsl: float | None = None, step: float = DEM_STEP_M) -> dict:
    """剖面圖的資料：沿航線每 `step` 公尺，地面高程與規劃高度各一條。

    **這是那條綠線該有的樣子**（doc/route-planning-first-principles.md F1）。
    使用者的原始問題不是沒有警告，是「QGC 有一條綠線而我不知道那是什麼」
    ——門檻要求人先知道數字的意思，**圖不用**：線穿到地下、或兩條線貼在
    一起，看一眼就知道。

    回 `{points, home_amsl_m, frame, unit}`；`points` 的每一筆是
    `{d, ground, plan, agl, seq}`（`d` 是沿線里程，公尺）。
    `plan` 為 None ＝那一段的高度基準這裡判不了（`frame 10` 交給飛控）。
    """
    out = {"points": [], "home_amsl_m": None, "frames": []}
    if dem is None or not getattr(dem, "available", False):
        return out
    ha = home_amsl
    if ha is None and home:
        ha = dem.elevation(home["lat"], home["lon"])
    if ha is None:
        return out
    out["home_amsl_m"] = round(ha, 1)

    pts = []
    for w in wps:
        c = _cmd(w)
        if not _is_nav(w):
            continue
        lat, lon = w.get("lat"), w.get("lon")
        if (not lat or not lon) and c == _TAKEOFF and home:
            lat, lon = home.get("lat"), home.get("lon")
        if not lat or not lon or w.get("alt") is None:
            continue
        fr = w.get("frame")
        fr = 3 if fr is None else int(fr)
        pts.append((lat, lon, float(w["alt"]), fr, w.get("seq"), c))
    out["frames"] = sorted({p[3] for p in pts})

    d0 = 0.0
    for i, (lat, lon, alt, fr, seq, c) in enumerate(pts):
        # 降落項的高度是 0，但飛機是**平飛過去再下降**——照 0 畫會讓剖面圖
        # 在最後憑空多一條斜線下去，那不是它會飛的路徑（同 check_terrain）
        if c in (_LAND, _RTL) and i:
            alt, fr = pts[i - 1][2], pts[i - 1][3]
        if i:
            pa = pts[i - 1]
            palt, pfr = (pa[2], pa[3])
            leg = _dist_m(pa[0], pa[1], lat, lon)
            n = max(1, int(leg // step))
            for k in range(1, n + 1):
                f = k / n
                la, lo = pa[0] + (lat - pa[0]) * f, pa[1] + (lon - pa[1]) * f
                gz = dem.elevation(la, lo)
                a = palt + (alt - palt) * f
                fr_k = fr if f > 0.5 else pfr
                plan = (ha + a if fr_k in _REL_FRAMES else
                        a if fr_k in _AMSL_FRAMES else None)
                out["points"].append({
                    "d": round(d0 + leg * f, 1),
                    # **座標要跟著出來**：3D 那一層要把這些點放回地圖上，
                    # 而讓它自己再去查一次航點，兩邊就會有兩份可能不同步的資料
                    "lat": round(la, 7), "lon": round(lo, 7),
                    "ground": None if gz is None else round(gz, 1),
                    "plan": None if plan is None else round(plan, 1),
                    "agl": (None if gz is None or plan is None
                            else round(plan - gz, 1)),
                    "seq": seq if k == n else None,
                })
            d0 += leg
        else:
            gz = dem.elevation(lat, lon)
            plan = (ha + alt if fr in _REL_FRAMES else
                    alt if fr in _AMSL_FRAMES else None)
            out["points"].append({
                "d": 0.0, "lat": round(lat, 7), "lon": round(lon, 7),
                "ground": None if gz is None else round(gz, 1),
                "plan": None if plan is None else round(plan, 1),
                "agl": (None if gz is None or plan is None
                        else round(plan - gz, 1)),
                "seq": seq})
    return out


def build_plan(points: list[dict], takeoff_alt: float, speed: float,
               home: dict | None = None) -> list[dict]:
    """把「一串點 ＋ 一個高度 ＋ 一個速度」組成一份飛得起來的航線。

    **這個函式存在的理由是「不需要先備知識就能規劃出可飛的路線」**
    （使用者 2026-09-08，見 doc/route-planning-first-principles.md）。
    操作員給的是意圖；起飛項、`frame`、改速度項、降落項這些**飛控要求的
    結構**由系統補——那些正是 QGC／Mission Planner 要求使用者自己知道的東西。

    組出來的形狀，每一項都說得出為什麼：

    * **起飛項的座標是 0,0**：`NAV_TAKEOFF` 只說「爬到多高」，位置就是
      解鎖的地方。這是 ArduPilot 的慣例，也是既有航線的樣子。
    * **`DO_CHANGE_SPEED` 擺在第一個航點之前**：它只從被執行到的那一項
      之後才生效，擺在後面的話**起飛到第一個航點那一段會用機上的
      `WP_SPD`** ——2026-09-07 摔機那一趟就是這樣（使用者以為 0.3，實際 8）。
    * **降落在起飛點**，不是最後一個航點：那是操作員站的地方。
      要落在別處是另一個決定，讓他自己改。
    * 全部 `frame 3`（離起飛點）。要跟地形就用「改成地形跟隨」轉一次
      ——那條路已經有了，而且會另存一份。
    """
    out: list[dict] = []

    def add(**kw):
        kw["seq"] = len(out)
        out.append(kw)

    add(lat=0.0, lon=0.0, alt=float(takeoff_alt), action="takeoff",
        command=_TAKEOFF, frame=3)
    # **速度先設好再飛第一段**（見 docstring）
    add(lat=0.0, lon=0.0, alt=0.0, action="do", command=_DO_CHANGE_SPEED,
        frame=2, p1=1.0, p2=float(speed), p3=-1.0, p4=0.0)
    for p in points:
        add(lat=float(p["lat"]), lon=float(p["lon"]),
            alt=float(p.get("alt", takeoff_alt)), action="waypoint",
            command=16, frame=3)
    if home and home.get("lat") and home.get("lon"):
        add(lat=float(home["lat"]), lon=float(home["lon"]), alt=0.0,
            action="land", command=_LAND, frame=3)
    return out


def check_group(paths: list[dict], vsep_m: float, lsep_m: float) -> dict:
    """群組跨路徑互檢（issue 013-A）：N 條同時飛的路徑要分離足夠。
    paths：[{label, waypoints:[{lat,lon,alt}]}]。兩條路徑若在某處**橫向 < lsep
    且垂直 < vsep**＝衝突（都靠太近才危險，分層或分離任一夠即安全）。
    unified 高度分層下垂直本就 ≥ vsep，天然通過；separate 才真的互檢。"""
    conflicts = []
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            a, b = paths[i], paths[j]
            wa = [w for w in a.get("waypoints", []) if w.get("lat") and w.get("lon")]
            wb = [w for w in b.get("waypoints", []) if w.get("lat") and w.get("lon")]
            hit = None
            for pa in wa:
                for pb in wb:
                    dh = _dist_m(pa["lat"], pa["lon"], pb["lat"], pb["lon"])
                    dv = abs((pa.get("alt") or 0) - (pb.get("alt") or 0))
                    if dh < lsep_m and dv < vsep_m:
                        hit = (round(dh), round(dv))
                        break
                if hit:
                    break
            if hit:
                conflicts.append({
                    "a": a.get("label"), "b": b.get("label"),
                    "why": f"最近處 橫向 {hit[0]}m／垂直 {hit[1]}m"
                           f"（門檻 橫向 {lsep_m:.0f}m 或 垂直 {vsep_m:.0f}m）"})
    return {"ok": not conflicts, "conflicts": conflicts}


# ── .plan 自帶的圍欄（QGC geoFence）────────────────────────────────────
#
# **圍欄是每份航線自己的事，不是系統的全域設定。** 原本只有 .env 的
# GEOFENCE_RADIUS_M 一個值：那是「這套系統只在一個場地飛」才成立的假設，
# 而測繪任務與定點巡檢的合理範圍可以差一個數量級。
# QGC 的 .plan 本來就帶 geoFence（圓形／多邊形、含納／排除），讀它就好。
#
# **沒帶就退回系統預設，而且要說出來**：使用者看到「超過圍欄半徑 50 m」時，
# 必須分得出「這份航線宣告了 50 m 而你超出」與「這份航線沒宣告，我拿系統
# 預設值在量」——後者多半代表預設值該改，不是航線該改。

def fence_from_plan(plan: dict) -> dict | None:
    """QGC `.plan` 的 geoFence → 本系統的形狀。沒有可用的圍欄回 None。

    只取**含納**（inclusion）的圓與多邊形——那是「只准在裡面飛」的邊界。
    排除區（exclusion）是另一回事（不准進去的區域），另外查。
    """
    gf = (plan or {}).get("geoFence") or {}
    inc_c, exc_c, inc_p, exc_p = [], [], [], []
    for c in gf.get("circles") or []:
        cir = c.get("circle") or {}
        ctr = cir.get("center") or []
        if len(ctr) < 2 or not cir.get("radius"):
            continue
        item = {"lat": ctr[0], "lon": ctr[1], "radius": float(cir["radius"])}
        (inc_c if c.get("inclusion", True) else exc_c).append(item)
    for p in gf.get("polygons") or []:
        pts = [(v[0], v[1]) for v in (p.get("polygon") or []) if len(v) >= 2]
        if len(pts) < 3:
            continue
        (inc_p if p.get("inclusion", True) else exc_p).append(pts)
    if not (inc_c or exc_c or inc_p or exc_p):
        return None
    return {"inclusion_circles": inc_c, "exclusion_circles": exc_c,
            "inclusion_polygons": inc_p, "exclusion_polygons": exc_p}


def _in_polygon(lat: float, lon: float, poly) -> bool:
    """射線法。邊界上的點視為在內（圍欄邊上不該因為浮點誤差被判出界）。"""
    inside = False
    n = len(poly)
    for i in range(n):
        y1, x1 = poly[i]
        y2, x2 = poly[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xc = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < xc:
                inside = not inside
    return inside


def check_fence(wps: list[dict], fence: dict) -> tuple[list[str], list[str]]:
    """航點對 .plan 自帶圍欄的檢查。回傳 (problems, warnings)。"""
    problems, warnings = [], []
    inc_c = fence.get("inclusion_circles") or []
    inc_p = fence.get("inclusion_polygons") or []
    exc_c = fence.get("exclusion_circles") or []
    exc_p = fence.get("exclusion_polygons") or []
    for w in wps:
        if not (w.get("lat") and w.get("lon")):
            continue
        seq, la, lo = w.get("seq"), w["lat"], w["lon"]
        # 含納圍欄：**任一個含納區包住就算在內**（QGC 允許多個含納區）
        if inc_c or inc_p:
            ok = any(_dist_m(c["lat"], c["lon"], la, lo) <= c["radius"]
                     for c in inc_c) or any(_in_polygon(la, lo, p) for p in inc_p)
            if not ok:
                near = min((_dist_m(c["lat"], c["lon"], la, lo) - c["radius"]
                            for c in inc_c), default=None)
                extra = f"（最近的含納圓還差 {near:.0f} m）" if near is not None else ""
                problems.append(
                    f"seq {seq} 在航線自帶的含納圍欄之外{extra}")
        for c in exc_c:
            if _dist_m(c["lat"], c["lon"], la, lo) <= c["radius"]:
                problems.append(f"seq {seq} 落在航線自帶的排除圓內")
        for p in exc_p:
            if _in_polygon(la, lo, p):
                problems.append(f"seq {seq} 落在航線自帶的排除多邊形內")
    return problems, warnings
