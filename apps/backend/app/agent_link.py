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
    connected: bool = False
    connected_at: float | None = None
    last_state_at: float | None = None
    state: str | None = None
    payload: dict | None = None          # 最後一則 state 的完整內容

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
            "fresh": self.fresh(),
            "state": self.state,
            "since": p.get("ts"),
            "mission_seq": p.get("mission_seq"),
            "mission_total": p.get("mission_total"),
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


def on_hello(msg: dict, drone_id: str | None, drone_name: str | None) -> AgentLink:
    uid = msg.get("board_uid")
    link = links.get(uid) or AgentLink(board_uid=uid)
    link.drone_id = drone_id
    link.drone_name = drone_name
    link.agent_version = msg.get("agent_version")
    link.inputs = list(msg.get("inputs") or [])
    link.connected = True
    link.connected_at = time.monotonic()
    links[uid] = link
    return link


def on_state(link: AgentLink, msg: dict) -> None:
    link.state = msg.get("state")
    link.payload = msg
    link.last_state_at = time.monotonic()


def on_disconnect(link: AgentLink) -> None:
    link.connected = False
    # state 保留：最後已知狀態是有用的資訊，清空等於宣告「不知道」
