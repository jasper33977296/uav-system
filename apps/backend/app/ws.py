import asyncio
import logging

from fastapi import WebSocket

log = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    #: 一個連線最多等多久。正常的瀏覽器在區網或 5G 上送一則是毫秒級
    SEND_TIMEOUT_S = 2.0

    async def broadcast(self, message: dict) -> None:
        """送給所有畫面。**同時送、每個最多等 SEND_TIMEOUT_S，超過就踢掉。**

        2026-10-20 新地面站兩個問題都出在這裡：
        * 直接迭代 self.clients：每個 send_json 都會 await，那段時間有人連上或離開
          就拋 `Set changed size during iteration`——收遙測的路徑也會呼叫這裡，
          那一則訊息的處理斷在半路（→ 先拍快照）
        * **一個一個送、每個都等**：一條已經死掉但 TCP 還沒發現的連線（關掉的分頁、
          睡著的筆電）會讓 send_json 卡住，後面所有人都在排隊——改名的 API 回不了話
          （名字其實改了，畫面卻像沒反應）、5Hz 的遙測廣播也跟著卡
        """
        clients = list(self.clients)
        if not clients:
            return

        async def one(ws):
            try:
                await asyncio.wait_for(ws.send_json(message), self.SEND_TIMEOUT_S)
                return None
            except Exception:
                return ws

        dead = [ws for ws in await asyncio.gather(*(one(ws) for ws in clients)) if ws]
        for ws in dead:
            self.disconnect(ws)
            try:
                await asyncio.wait_for(ws.close(), 1.0)
            except Exception:
                pass
        if dead:
            log.info("廣播：踢掉 %d 條送不出去的畫面連線（剩 %d）", len(dead), len(self.clients))


manager = ConnectionManager()
