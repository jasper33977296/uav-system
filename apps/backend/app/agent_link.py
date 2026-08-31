"""意圖協定的地面站端（doc/agent-intent-protocol.md）。

接受機上代理的 WebSocket 連線、驗信封、收 `hello`／`state`／`event`／`ack`，
把最新狀態放進登錄表並推給前端；反方向送 `intent`／`decision`／`progress`。
不認得的型別明說「未支援」而不是靜靜丟掉。

三條刻意的紀律：

* **權威在代理，這裡只是鏡像。** 收到什麼記什麼，不修正、不補值、不推論。
  地面站看到的位置經 5G 回來已是過期資料，拿它去「修正」代理的判斷，
  等於用比較差的資料覆蓋比較好的（狀態機文件 §0.1）。
* **連線斷掉就是失聯**（協定 §2）。斷線時把狀態標成 stale 並推播，
  **但不清空最後已知狀態**——「最後看到它在 FLYING_MISSION」是有用的資訊，
  清成空白等於宣告「不知道」，那是假話。
* **不認得的協定版本就拒絕**（協定 §3）。半懂的指令比不懂的危險。
"""
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PROTOCOL_V = 1
#: 多久沒收到 state 就視為這條意圖通道不新鮮（代理 1 Hz 保活）
STALE_S = 5.0

#: 斷線期間最多壓幾則 intent（039 複裁 G）。**有上限而且滿了要說**：
#: 操作員在失聯期間連按十次「改航線」時，十則都補送過去毫無意義——
#: 他要的是最後那一個意思，而前面九則只會讓恢復後的畫面塞滿待確認
PENDING_MAX = 8
#: 壓過這麼久的 intent 不再補送。恢復時飛機早就不在當初那個位置，
#: 而補送的目的是「讓人重新看一次」，不是把一個十分鐘前的念頭放出來
PENDING_TTL_S = 600.0


@dataclass
class AgentLink:
    """一台機的意圖通道現況。"""
    board_uid: str
    drone_id: str | None = None
    drone_name: str | None = None
    agent_version: str | None = None
    inputs: list[str] = field(default_factory=list)
    executes: list[str] = field(default_factory=list)   # 代理自己執行的意圖
    vets: list[str] = field(default_factory=list)       # 代理守門的意圖
    connected: bool = False
    connected_at: float | None = None
    last_state_at: float | None = None
    state: str | None = None
    payload: dict | None = None          # 最後一則 state 的完整內容
    ws: object = None                    # 送 intent 用的連線（斷線時清掉）
    waiters: dict = field(default_factory=dict)   # intent_id → asyncio.Future
    #: intent_id → 代理回執的時刻（協定 §2）。**與 waiters 分開**：回執說的是
    #: 「我收到了」，event 說的是「我判決了」。逾時的時候這兩者要分得出來——
    #: 「送不到」與「送到了但代理算不出判決」是完全不同的故障
    receipts: dict = field(default_factory=dict)
    #: 斷線期間壓著的 intent（039 複裁 G），恢復後補送。存的是原話＋壓進來的
    #: 時刻，因為補送時要告訴人「這是你 N 分鐘前按的」
    pending: list = field(default_factory=list)

    def fresh(self) -> bool:
        return (self.connected and self.last_state_at is not None
                and time.monotonic() - self.last_state_at < STALE_S)

    def as_dict(self) -> dict:
        p = self.payload or {}
        return {
            "board_uid": self.board_uid,
            "drone_id": self.drone_id,
            "agent_version": self.agent_version,
            "inputs": self.inputs,
            "connected": self.connected,
            "executes": self.executes, "vets": self.vets,
            "fresh": self.fresh(),
            "state": self.state,
            "since": p.get("ts"),
            "mission_seq": p.get("mission_seq"),
            "mission_total": p.get("mission_total"),
            # 序列進行中的話，鏡像也要看得到——「這台機正在跑一段改航線序列」
            # 是操作員該知道的事，藏在機上等於沒說（協定 §4.2）
            "intent_id": p.get("intent_id"), "seq_step": p.get("seq_step"),
            "derived": p.get("derived"),
            # **RC 遙控器連上了沒有**（039 複裁 A）。這是「機在地上失聯只告警」
            # 那格的前提：沒有 RC 就沒有人能接管。權威守門在代理，這裡只鏡像，
            # 讓畫面說得出「為什麼現在不能起飛」。代理還沒開始送時是 None＝
            # **不知道**，不是「沒有」——畫面要照這個分別顯示
            "rc_link": p.get("rc_link"),
            "pending": len(self.pending),
        }


#: board_uid → AgentLink。**鍵與註冊同一個**（見 db.ensure_drone_by_board）
links: dict[str, AgentLink] = {}


def envelope_error(msg: dict) -> str | None:
    """檢查共同信封（協定 §3）。回傳錯誤說明，沒問題回 None。"""
    if not isinstance(msg, dict):
        return "訊息必須是 JSON 物件"
    v = msg.get("v")
    if v != PROTOCOL_V:
        # 不盡力解讀。**這裡是唯一擋得住版本錯配的地方**——放行之後，
        # 欄位語意變了也沒有人會發現，只會看到數字怪怪的
        return f"協定版本 {v!r} 不受支援（本站支援 {PROTOCOL_V}）"
    if not msg.get("type"):
        return "缺 type"
    return None


def on_hello(msg: dict, drone_id: str | None,
             drone_name: str | None) -> tuple[AgentLink, object | None]:
    """一台機一個代理（協定 §7.4 定案 2026-08-25）。

    回傳 (link, 要關掉的舊連線)。**同一塊板子第二條連線進來時，新的贏**：
    最常見的情況是半開的 TCP——舊連線其實已經死了，只是還沒 FIN。讓舊的贏
    會讓真正活著的代理永遠連不上。

    但**舊的如果還新鮮就要大聲說**：那代表真的有兩個代理指向同一台機，
    而那是設定錯誤（例如一台 Pi 被複製過去、board_uid 跟著複製）。兩個代理
    各自守門、各自算提案，飛機會收到互相矛盾的指令。
    """
    uid = msg.get("board_uid")
    old = links.get(uid)
    stale_ws = None
    if old is not None and old.connected and old.ws is not None:
        stale_ws = old.ws
        if old.fresh():
            log.warning("⚠ board_uid %s 出現第二條意圖通道，而舊的還在推狀態"
                        "——**一台機只該有一個代理**。舊的會被關掉；"
                        "如果兩台機真的都在跑，其中一台的 board_uid 是複製來的",
                        uid)
    link = old or AgentLink(board_uid=uid)
    link.drone_id = drone_id
    link.drone_name = drone_name
    link.agent_version = msg.get("agent_version")
    link.inputs = list(msg.get("inputs") or [])
    link.connected = True
    link.connected_at = time.monotonic()
    link.executes = list(msg.get("executes") or [])
    link.vets = list(msg.get("vets") or [])
    links[uid] = link
    return link, stale_ws


def on_state(link: AgentLink, msg: dict) -> None:
    link.state = msg.get("state")
    link.payload = msg
    link.last_state_at = time.monotonic()


def on_event(link: AgentLink, msg: dict) -> None:
    """代理回的 event。有人在等這個 intent_id 就叫醒他。"""
    fut = link.waiters.pop(msg.get("intent_id"), None)
    if fut is not None and not fut.done():
        fut.set_result(msg)


def on_ack(link: AgentLink, msg: dict) -> None:
    """代理對一則 intent 的回執（協定 §2，039 複裁 G）。

    **回執不是判決。** 它只說「這則我收到了」，判決仍然要等 `event`。分開記
    是為了讓逾時說得出是哪一段斷的：送不出去（5G 掉了）與代理收到卻算不出
    判決（守門卡住、飛控沒回應）要走不同的處理，而原本兩者長得一模一樣。
    """
    iid = msg.get("intent_id")
    if not iid:
        return
    link.receipts[iid] = time.monotonic()
    # 只留還在等的那些：waiters 清掉時這裡也該跟著清，否則長跑的地面站會
    # 累積一份永遠不會再被查詢的 intent_id 清單
    for k in [k for k in link.receipts if k not in link.waiters]:
        if k != iid:
            link.receipts.pop(k, None)


def queue_intent(link: AgentLink, action: str, params: dict | None,
                 intent_id: str) -> tuple[int, int]:
    """意圖通道斷線時把 intent 壓下來（039 複裁 G：補送，由代理守門擋）。

    回傳 (佇列長度, 丟掉幾則)。**滿了丟最舊的**：操作員在失聯期間按的最後
    一次才是他現在的意思，而最舊的那則距離現況最遠。丟掉會留 log——
    安靜地丟掉一個飛行操作，等於系統替人做了決定卻沒說。
    """
    now = time.monotonic()
    before = len(link.pending)
    link.pending = [q for q in link.pending if now - q["at"] < PENDING_TTL_S]
    expired = before - len(link.pending)
    link.pending.append({"action": action, "params": params or {},
                         "intent_id": intent_id, "at": now})
    over = max(0, len(link.pending) - PENDING_MAX)
    if over:
        link.pending = link.pending[over:]
    dropped = expired + over
    if dropped:
        log.warning("意圖佇列丟掉 %d 則（%d 則過期、%d 則超過上限 %d）：%s",
                    dropped, expired, over, PENDING_MAX, link.board_uid)
    return len(link.pending), dropped


def take_pending(link: AgentLink) -> list[dict]:
    """取出並清空待補送的 intent，順便濾掉過期的。

    **取出就清空**：補送只做一次。失敗了也不要留著自動再試——重試一個
    人在十分鐘前按下的飛行操作，是這條規則最該避免的事。
    """
    now = time.monotonic()
    out = [q for q in link.pending if now - q["at"] < PENDING_TTL_S]
    if len(out) < len(link.pending):
        log.info("補送前濾掉 %d 則過期 intent（%s）",
                 len(link.pending) - len(out), link.board_uid)
    link.pending = []
    for q in out:
        q["age_s"] = now - q["at"]
    return out


def on_disconnect(link: AgentLink) -> None:
    link.connected = False
    link.ws = None
    # 等在半路的請求要叫醒，不然它們會一直等到逾時才知道對面走了
    for fut in list(link.waiters.values()):
        if not fut.done():
            fut.set_exception(ConnectionError("意圖通道在等待期間斷線"))
    link.waiters.clear()
    link.receipts.clear()
    # state 保留：最後已知狀態是有用的資訊，清空等於宣告「不知道」
    # pending 也保留：那正是「斷線期間壓下來、恢復後補送」要用的東西


async def send_intent(link: AgentLink, action: str, params: dict | None,
                      intent_id: str, timeout: float = 4.0,
                      kind: str = "intent") -> dict:
    """送一則 intent 給代理並等它的 event。

    **逾時不等於放行。** 等不到回覆就是不知道守門怎麼說，而「不知道」在飛安
    路徑上要當成「不行」——呼叫端據此擋下操作，並說出是逾時而不是被拒。

    逾時的說法分兩種（039 複裁 G 的逐則回執）：收過回執＝送到了、是代理那端
    算不出判決；沒收過＝這則根本沒送達。兩種都是「不行」，但**要查的地方
    完全不同**，而原本它們回同一句話。
    """
    import asyncio
    if not link.connected or link.ws is None:
        raise ConnectionError("意圖通道未連線")
    fut = asyncio.get_running_loop().create_future()
    link.waiters[intent_id] = fut
    try:
        msg = {"v": PROTOCOL_V, "type": kind, "intent_id": intent_id,
               "action": action}
        # decision／progress 的欄位在協定裡是平的（approved／step／ok），
        # 不包在 params 底下——照協定的形狀送，不要自己多包一層
        msg.update(params or {}) if kind != "intent" else msg.update(
            {"params": params or {}})
        await link.ws.send_json(msg)
        return await asyncio.wait_for(fut, timeout)
    except (TimeoutError, asyncio.TimeoutError):
        raise TimeoutError(
            "代理已回執（收到了）但沒有在時限內給出判決"
            if intent_id in link.receipts else
            "代理連回執都沒有回——這則很可能沒有送達") from None
    finally:
        link.waiters.pop(intent_id, None)
        link.receipts.pop(intent_id, None)
