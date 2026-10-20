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

    async def broadcast(self, message: dict) -> None:
        dead = []
        # **先拍一份快照再迭代**（2026-10-20）。每個 send_json 都會 await、讓出執行權，
        # 那段時間裡有瀏覽器連上或離開，集合就被改了——直接迭代 self.clients 會拋
        # `Set changed size during iteration`。新地面站實測：有人開關頁面時一直爆，
        # 而 mavlink_rx 收遙測的路徑也會呼叫這裡推事件，例外一路往上把收遙測弄停，
        # 畫面上兩台機都顯示斷線（mavlink_connected=false）
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
