"""一致性測試套的共用骨架（issue 026 B3）。

## 這套測試在回答什麼問題

**「我們的程式會不會講這個廠牌的方言？」**——而不是「這一台機的設定對不對」。
兩者必須分開（見 `doc/autopilot-driver-architecture.md` §3）：

| | 一致性測試（本套） | 執行期前提檢查 |
|---|---|---|
| 問題 | 我們的程式會不會講這個方言 | 這一台機的設定接不接受我們 |
| 何時判定 | CI／SITL，離線 | 連線時，逐台 |
| 例子 | 失聯降級送的模式號對不對 | `SYSID_MYGCS=254`？ |

能力 `ok` ＝ 兩者的交集。混為一談會得到兩種錯誤：用測試結果宣告一台沒設好的機
可用，或用單機設定否定驅動本身的正確性。

## 為什麼測行為而不是測結構

issue 030 是這套測試存在的理由，也是它的試金石：失聯自動懸停寫死了 PX4 的模式
編碼，在 ArduPilot 上送出 GUIDED 而不是 LOITER。**那個 bug 躲過了 B0/B1/B2 三輪
重構**，因為它直接讀模組層的表、沒有經過 `dialect()`——**靜態上它只是取一個
常數**。只有「實際送出去的是什麼、機端最後停在哪個模式」這種行為觀察抓得到它。

所以本套測試一律以**機端的實際狀態**為準（讀回 HEARTBEAT），不看我方送了什麼、
更不看程式碼長什麼樣。

## 誠實邊界

測試跑在 **SITL**，SITL ≠ 真機。所以結果的準確語意是「對 <韌體版本> 的 SITL
驗證通過」，不是「真機保證可用」。結果檔會記下韌體版本與時間，讓能力宣告能帶
上證據強度。
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

BACKEND = os.environ.get("CONF_BACKEND", "http://localhost:38000")
COMMAND = os.environ.get("CONF_COMMAND", "http://localhost:38001")

#: 結果落地位置。能力推導讀這裡，而不是讀寫死的字典。
RESULTS_DIR = pathlib.Path(os.environ.get("CONF_RESULTS", "data/conformance"))


class Skip(Exception):
    """前提不滿足，測不了（不是失敗）。

    **skip 不得被當成 pass**：測不了就是沒有證據，而沒有證據不能宣告能力可用。
    """


def post(path: str, body=None, timeout=90, base=None):
    url = (base or COMMAND) + path
    data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        return True, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:300]
    except Exception as e:
        return False, str(e)[:300]


def delete(path: str, base=None, timeout=30):
    """刪除自己造的測試資料（共用環境紀律：只刪自己造的）。"""
    req = urllib.request.Request((base or COMMAND) + path, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def get(path: str, base=None, timeout=15):
    with urllib.request.urlopen((base or COMMAND) + path, timeout=timeout) as r:
        return json.loads(r.read())


def fleet() -> dict:
    return get("/healthz").get("drones", {})


def local_wps(info: dict, alt: float = 15.0,
              offsets=((40, 0), (40, 40), (0, 40))) -> list[dict]:
    """以該機**當下位置**為原點造航點（offsets 是公尺，(北, 東)）。

    **不要在測項裡寫死座標。** SITL 的出生點由 `sim-fleet/fleet.sh` 的
    `FLEET_HOME` 決定（現為台灣），而映像原廠預設是蘇黎世（PX4）與波士頓
    （ArduPilot）——寫死等於把「測試能不能跑」綁在「模擬器出生在哪」。

    2026-08-24 實測踩到：`mission_fly` 的航點還停在蘇黎世，距實際出生點約
    9600 km，PX4 的任務可行性檢查直接拒絕切 AUTO_MISSION。而 harness 把
    「機端拒絕」歸類成 skip（前提不滿足，非方言問題）——**分類本身是對的，
    但它把一個測試資料的錯誤偽裝成了前提不足**，看起來像環境沒準備好。

    這也是為什麼 `mission_upload` 一直「通過」：上傳不需要可行性檢查，
    它上傳的是一份飛機永遠飛不到的任務。過了，但沒有驗到該驗的東西。
    """
    import math
    lat, lon = info.get("lat"), info.get("lon")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        raise Skip("這台機還沒有位置（GPS 未定位？），無法以當下位置造航點")
    out = []
    for i, (north_m, east_m) in enumerate(offsets):
        out.append({
            "seq": i,
            "lat": round(lat + north_m / 110574.0, 7),
            "lon": round(lon + east_m / (111320.0 * math.cos(math.radians(lat))), 7),
            "alt": alt, "action": "waypoint",
        })
    return out


def pick(autopilot: str) -> tuple[int, dict]:
    """挑一台該廠牌、**未解鎖**的機。找不到就 Skip。

    只挑 disarmed 的：一致性測試會下模式指令，不能打斷別人正在飛的架次。

    可用環境變數 `CONF_SYSID` 指定機號——飛行測項需要一台**預檢過得了**的機，
    而預檢狀態不在 command 的快照裡（那是 backend 的 readiness）。與其讓測試
    去猜，不如讓跑的人指定；猜錯的代價是把「這台機飛不起來」誤記成「驅動壞了」。
    """
    want = os.environ.get("CONF_SYSID")
    for sysid, d in sorted(fleet().items(), key=lambda kv: int(kv[0])):
        if d.get("autopilot") != autopilot or d.get("armed") is not False:
            continue
        if want and str(sysid) != str(want):
            continue
        return int(sysid), d
    raise Skip(f"沒有未解鎖的 {autopilot} 機可測"
               + (f"（指定了 CONF_SYSID={want}）" if want else ""))


#: 機端自己拒絕的訊號。**這些不是方言錯誤**——見 assert_dialect()。
_PRECONDITION_HINTS = ("解鎖被拒", "TEMPORARILY_REJECTED", "DENIED",
                       "Preflight", "預檢", "未連線")

#: **能力 gating 擋下**的訊號。這是一個結構性的雞生蛋問題：
#: 動詞是 unverified → API 拒發 → 測試跑不了 → 永遠拿不到證據 → 永遠 unverified。
#: 記成 fail 是錯的（那宣稱「驗過而且壞了」）；它的真相是「還沒驗、而且**照現行
#: gating 也驗不了**」。這需要一個決定，不是測試能自行繞過的——**繞過 gating
#: 去測，等於測一條產品上不存在的路徑**。
_GATING_HINTS = ("目前不可用（unverified", "目前不可用（unsupported", '"capability"')


def assert_dialect(ok: bool, r, what: str):
    """把「機端因自身狀態拒絕」與「我們的方言錯」分開。

    **這是整套測試的核心區分**（見模組說明的表）：機端電池不健康而拒絕解鎖，
    反映的是那一台機的狀態，不是我們會不會講它的方言。記成 fail 等於**誣賴驅動**
    ——而在能力值改由測試推導之後，那會讓一個好好的動詞被鎖住。

    所以：機端拒絕 → Skip（沒有證據，能力維持 unverified，但不是失敗）。
    """
    if ok:
        return
    text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    if any(h in text for h in _GATING_HINTS):
        raise Skip(
            f"{what}：**被能力 gating 擋下**（該動詞目前是 unverified）。"
            "\n    這是雞生蛋：沒有證據→鎖住→測不了→拿不到證據。"
            "\n    **不繞過 gating**（繞過等於測一條產品上不存在的路徑），"
            "需要決定如何 bootstrap。")
    if any(h in text for h in _PRECONDITION_HINTS):
        raise Skip(f"{what}：機端以自身狀態拒絕（非方言問題）——{text[:160]}")
    raise AssertionError(f"{what}：{text[:220]}")


def mode_of(sysid: int) -> str | None:
    """機端**目前實際**的模式名（讀 HEARTBEAT 的 custom_mode 解碼）。

    刻意不讀 `/api/live`——那個端點只回主機（issue 028 的教訓）。
    """
    st = fleet().get(str(sysid), {})
    cm = st.get("custom_mode")
    if cm is None:
        return None
    return _driver(st.get("autopilot_raw")).decode_mode(cm)


def _driver(autopilot_raw):
    import sys
    libs = str(pathlib.Path(__file__).resolve().parents[2] / "libs")
    if libs not in sys.path:
        sys.path.insert(0, libs)
    from autopilot import get_driver
    return get_driver(autopilot_raw)


def custom_mode_of(sysid: int):
    """機端目前的原始 custom_mode（未解碼）。比對交給驅動的 mode_matches。"""
    return fleet().get(str(sysid), {}).get("custom_mode")


def wait_verb(sysid: int, drv, verb: str, timeout=8.0):
    """等機端**真的**進入某個動詞對應的模式。

    比對用 `drv.mode_matches()` 而不是自己把 encode_mode 的輸出組回 custom_mode
    ——後者要分廠牌（PX4 是 main<<16|sub<<24、ArduPilot 是模式號本身），等於在
    測試裡複製一份方言知識。**測試不該持有方言，它只該問驅動。**
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = custom_mode_of(sysid)
        if last is not None and drv.mode_matches(last, verb):
            return True, last
        time.sleep(0.3)
    return False, last


def wait_mode(sysid: int, want: str, timeout=8.0) -> str | None:
    """等機端**真的**停在某個模式。回傳最後看到的模式。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = mode_of(sysid)
        if last == want:
            return last
        time.sleep(0.3)
    return last


def firmware_of(autopilot: str, sysid=None) -> str | None:
    """受測機的機上韌體版本（`/api/drones` 的 runtime 欄位，見 issues/038）。

    **測項不會把 sysid 傳進 record()**，所以優先用 `CONF_SYSID`（跑的人指定的
    那台），否則以廠牌比對——同一輪裡每個廠牌通常只有一台 SITL。

    拿不到就回 None——**不要用「未知」之類的字串填充**，那會讓「還沒回報版本」
    與「回報了但我們解不出來」在證據檔裡長得一樣。
    """
    want = sysid if sysid is not None else os.environ.get("CONF_SYSID")
    try:
        rows = get("/api/drones", base=BACKEND) or []
    except Exception:
        return None
    for d in rows:
        if want is not None and str(d.get("mav_sysid")) == str(want):
            return d.get("flight_sw_version")
    if want is not None:
        return None
    hit = [d for d in rows if d.get("autopilot") == autopilot
           and d.get("flight_sw_version")]
    return hit[0]["flight_sw_version"] if len(hit) == 1 else None


def record(verb: str, autopilot: str, status: str, evidence: str,
           sysid: int | None = None) -> dict:
    """把結果落地。**能力值由這些檔案推導，不是人工判斷。**"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{autopilot}.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    prev = data.get(verb) or {}
    if status == "skip" and prev.get("status") == "pass":
        # **skip 不得抹掉既有的 pass**：skip 的語意是「這次沒取得新證據」，
        # 不是「舊證據作廢」。抹掉的話，換一台預檢沒過的機跑一次，就會把
        # 別台實測得到的證據弄丟——然後能力值莫名其妙掉下來。
        prev.setdefault("skips", []).append(
            {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "why": evidence})
        prev["skips"] = prev["skips"][-3:]
        data[verb] = prev
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
        return prev
    entry = {
        "status": status,                       # pass / fail / skip
        "evidence": evidence,                   # 人看得懂的證據，不是 True/False
        "sysid": sysid,
        # **證據要能說出「驗的是哪一版」**：模組說明承諾了這件事，但在
        # 2026-08-24 之前實際上沒有實作（欄位根本不存在），使得
        # 「對 <韌體版本> 的 SITL 驗證通過」這句話說不出後半段。
        # 版本來自 AUTOPILOT_VERSION（issues/038 才開始請求它）。
        # None＝那台機還沒回報版本，不是「沒有版本」。
        "firmware": firmware_of(autopilot, sysid),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "env": "sitl",                          # **不是真機**——見模組說明
    }
    data[verb] = entry
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                    encoding="utf-8")
    return entry


def run(verb: str, autopilot: str, fn):
    """跑一個動詞的一致性測試並落地結果。回傳 shell 結束碼。"""
    try:
        evidence = fn()
        entry = record(verb, autopilot, "pass", evidence)
        print(f"✔ {verb}［{autopilot}］pass — {evidence}")
        return 0
    except Skip as e:
        entry = record(verb, autopilot, "skip", str(e))
        print(f"○ {verb}［{autopilot}］skip — {e}")
        print("  （skip 不等於 pass：測不了就是沒有證據，能力不會因此開啟）")
        return 0
    except AssertionError as e:
        record(verb, autopilot, "fail", str(e))
        print(f"✘ {verb}［{autopilot}］**fail** — {e}")
        return 1
    except Exception as e:
        record(verb, autopilot, "fail", f"{type(e).__name__}: {e}")
        print(f"✘ {verb}［{autopilot}］**fail**（非預期例外）— {type(e).__name__}: {e}")
        return 1
