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


def pick(autopilot: str) -> tuple[int, dict]:
    """挑一台該廠牌、**未解鎖**的機。找不到就 Skip。

    只挑 disarmed 的：一致性測試會下模式指令，不能打斷別人正在飛的架次。
    """
    for sysid, d in sorted(fleet().items(), key=lambda kv: int(kv[0])):
        if d.get("autopilot") == autopilot and d.get("armed") is False:
            return int(sysid), d
    raise Skip(f"沒有未解鎖的 {autopilot} 機可測")


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
    entry = {
        "status": status,                       # pass / fail / skip
        "evidence": evidence,                   # 人看得懂的證據，不是 True/False
        "sysid": sysid,
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
