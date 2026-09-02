"""簽章金鑰的保管與**指紋自檢**（issues/040 A5-b／A5-c）。

設計見 `doc/mavlink-signing-design.md`。

## 這一批**還沒有在簽任何東西**

本模組只做金鑰的保管與兩邊的指紋比對。**線上的封包目前仍然沒有簽章**，
而這件事必須在畫面與 log 上說清楚——設計 §1 的紀律：
**不要讓它被當成一道其實不存在的防線**。所以每個對外的字眼都是「尚未啟用」，
不是「已保護」。

先做這一批的理由寫在設計 §3：**簽章不符是靜默丟棄**。金鑰一旦不對，
畫面上一切正常、指令全部消失——那正是本專案反覆吃虧的形狀
（`SYSID_MYGCS` 靜默丟棄、補傳的狀態碼判定從未生效、兩支永遠紅的測試）。
**所以偵測要先於啟用，不能反過來。**

## 三條紀律

1. **金鑰本身永遠不出現在 log、API、事件裡。** 只傳指紋（雜湊前綴）。
   這條沒有例外——一個為了除錯而印出金鑰的分支，會在事故當下把金鑰印進
   一份要交出去的 log。
2. **一機一把。** 機體遺失＝那把外洩；共用一把＝掉一台全隊失守。
3. **沒有金鑰＝沒有啟用**，不是「壞掉」。絕大多數機在很長一段時間裡都會是
   這個狀態，把它報成錯誤只會製造一整片假警報。
"""
import hashlib
import json
import logging
import os
import secrets

log = logging.getLogger(__name__)

#: 金鑰檔。**放共用 volume，不放映像**（映像會被推到 registry），
#: 也**不放資料庫**——DB 會被 dump、備份、貼進工單，那是金鑰最容易外流的路徑
KEYS_PATH = os.environ.get("SIGNING_KEYS_PATH", "/state/signing-keys.json")
#: MAVLink 2 的簽章金鑰長度（位元組）
KEY_BYTES = 32
#: 指紋長度（十六進位字元）。**夠長到不會誤撞、夠短到人唸得出來**——
#: 現場對帳是用嘴唸的，不是用複製貼上的
FP_CHARS = 16


def fingerprint(key: bytes | str) -> str:
    """金鑰的指紋。**兩邊交換這個，不交換金鑰。**

    用雜湊而不是金鑰前綴：前綴洩漏金鑰本身的位元，而指紋不會。
    """
    if isinstance(key, str):
        key = bytes.fromhex(key)
    return hashlib.sha256(key).hexdigest()[:FP_CHARS]


def _load() -> dict:
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            return json.load(f).get("keys") or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        # **讀不到不等於沒有金鑰**：檔案壞了而我們當成「沒啟用」，
        # 等於把一道防線靜靜關掉。大聲說，並回空表讓呼叫端走「不知道」那條
        log.error("簽章金鑰檔讀取失敗（%s）——**這不是「沒有金鑰」**，"
                  "是我們讀不到；在修好之前不要當成已停用", e)
        return {}


def _save(keys: dict) -> None:
    tmp = f"{KEYS_PATH}.tmp"
    os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"keys": keys}, f)
    os.chmod(tmp, 0o600)          # **只有擁有者讀得到**
    os.replace(tmp, KEYS_PATH)    # rename 是原子的，讀端不會讀到寫一半的


def get_key(board_uid: str) -> str | None:
    """這塊板子的金鑰（hex）。沒有＝**還沒配發**，不是錯誤。"""
    return _load().get(board_uid)


def get_fingerprint(board_uid: str) -> str | None:
    k = get_key(board_uid)
    return fingerprint(k) if k else None


def issue_key(board_uid: str, *, overwrite: bool = False) -> tuple[str, bool]:
    """配一把新金鑰給這塊板子。回傳 (指紋, 是否新配)。

    **預設不覆蓋既有的**：覆蓋等於把那台機的簽章打斷，而它此刻可能正在飛。
    要換金鑰是一個明確的動作（輪換／撤銷），不該是重新註冊的副作用。
    """
    keys = _load()
    if board_uid in keys and not overwrite:
        return fingerprint(keys[board_uid]), False
    keys[board_uid] = secrets.token_hex(KEY_BYTES)
    _save(keys)
    # **只印指紋。** 這行是本模組唯一會提到金鑰的 log，而它提到的是指紋
    log.warning("已配發簽章金鑰給板號 %s（指紋 %s）——**線上尚未啟用簽章**，"
                "本批只做保管與指紋自檢（issues/040 A5-b）",
                board_uid[-6:], fingerprint(keys[board_uid]))
    return fingerprint(keys[board_uid]), True


def revoke_key(board_uid: str) -> bool:
    """撤銷。**機上那把我們拿不回來**，所以撤銷的實質是「地面站不再用它說話」
    ——真正的撤銷是把那台機從配號登錄裡除役（設計 §2.3）。這裡只做地面站這半。"""
    keys = _load()
    if board_uid not in keys:
        return False
    del keys[board_uid]
    _save(keys)
    log.warning("已撤銷板號 %s 的簽章金鑰。**注意：機上那把仍然存在**——"
                "真正的撤銷要把那台機從配號登錄除役", board_uid[-6:])
    return True


def check(board_uid: str, claimed_fp: str | None) -> dict:
    """比對代理自報的指紋與我們手上的（A5-c）。

    四種結果，**刻意分開**——把它們混成「好／壞」會讓最重要的那格消失：

    | 我們有金鑰 | 代理報指紋 | 結果 | 意思 |
    |---|---|---|---|
    | 否 | 否 | `not_enabled` | 絕大多數機的常態，不是錯誤 |
    | 是 | 否 | `agent_missing` | 我們配了、機上沒有——**配發沒完成** |
    | 否 | 是 | `ground_missing` | 機上有、我們沒有——**金鑰檔可能掉了** |
    | 是 | 是 | `match`／`mismatch` | 不符＝日後開簽章會全部靜默丟棄 |
    """
    ours = get_fingerprint(board_uid)
    if ours is None and not claimed_fp:
        return {"state": "not_enabled",
                "reason": "這塊板子還沒有配發簽章金鑰（尚未啟用，不是故障）"}
    if ours is not None and not claimed_fp:
        return {"state": "agent_missing", "ground_fp": ours,
                "reason": "地面站有這塊板子的金鑰，但代理沒有回報指紋"
                          "——**配發沒有完成**；日後開簽章會全部被丟掉"}
    if ours is None:
        return {"state": "ground_missing", "agent_fp": claimed_fp,
                "reason": "代理有金鑰，地面站沒有——**金鑰檔可能掉了或被還原過**"}
    if ours != claimed_fp:
        return {"state": "mismatch", "ground_fp": ours, "agent_fp": claimed_fp,
                "reason": "兩邊的簽章金鑰不同。**日後開啟簽章時，這台機的封包會"
                          "被靜默丟棄**——畫面一切正常而指令全部消失"}
    return {"state": "match", "ground_fp": ours,
            "reason": "兩邊金鑰指紋相同（線上尚未啟用簽章）"}
