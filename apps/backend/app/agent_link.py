"""意圖協定的地面站端（doc/agent-intent-protocol.md）。

**本版只做到 `state`**：接受機上代理的 WebSocket 連線、驗信封、收 `hello` 與
`state`，把最新狀態放進登錄表並推給前端。不下任何指令——`intent`／`proposal`
／`decision` 尚未實作，收到就明說「未支援」而不是靜靜丟掉。

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


def on_disconnect(link: AgentLink) -> None:
    link.connected = False
    link.ws = None
    # 等在半路的請求要叫醒，不然它們會一直等到逾時才知道對面走了
    for fut in list(link.waiters.values()):
        if not fut.done():
            fut.set_exception(ConnectionError("意圖通道在等待期間斷線"))
    link.waiters.clear()
    # state 保留：最後已知狀態是有用的資訊，清空等於宣告「不知道」


async def send_intent(link: AgentLink, action: str, params: dict | None,
                      intent_id: str, timeout: float = 4.0,
                      kind: str = "intent") -> dict:
    """送一則 intent 給代理並等它的 event。

    **逾時不等於放行。** 等不到回覆就是不知道守門怎麼說，而「不知道」在飛安
    路徑上要當成「不行」——呼叫端據此擋下操作，並說出是逾時而不是被拒。
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
    finally:
        link.waiters.pop(intent_id, None)
