"""影像串流的**對外事實**：path 名稱怎麼取、外部控制看得到什麼。

兩個服務都要用到（backend 的錄影與對外資料面、command 的 `/api/ext/drones`），
所以放在 `libs/`。**path 名稱的規則只能有一份**——抄第二份的下場是兩邊算出
不同的名字，而錄影是綁在名字上的（issues/040 的坑，見 `video_rec.path_for`）。

## 外部控制拿得到什麼（2026-09-23 使用者裁定）

裁定三件事：**RTSP**、**不限制也不記錄外部拉流**、以及
「只要系統在、代理有連線，外部要求或前端打開時就持續送，直到兩邊都斷」。

最後那句在現在的架構下是自然成立的，不必另外實作：前端的 WHEP 讀者與外部的
RTSP 讀者**讀同一條 path**，所以機上只被拉一次、兩邊共用同一條上行；
最後一個讀者離開之後才收（`sourceOnDemand`）。**外部接進來不會讓上行變兩倍。**

## 為什麼不是直接給一條網址就好

給網址很容易，但「給了網址」與「拉得到畫面」是兩件事。三種情況對外部的意義
完全不同，混成一條網址等於把判斷丟給對方去猜：

| state | 意思 | 給網址嗎 |
|---|---|---|
| `ready` | 來源已設定，而且這台現在有連線 | ✅ |
| `no_camera` | 這台沒有設定相機來源 | ❌ |
| `offline` | 有相機，但這台現在沒連線 | ❌ |

**`ready` 不保證此刻拉得到。** 我們知道的是「來源設好了」與「MAVLink 還在通」；
機上的相機服務有沒有在跑、鏡頭有沒有被拔掉，地面站在真的去拉之前無從得知
（拉流是 on-demand，平時根本沒有連線可以觀察）。所以 `ready` 的話術是
「可以去拉」，不是「一定有畫面」——拉不到請當成正常的可能結果處理。
"""

RTSP_PORT = 8554


def path_for(drone_id: str) -> str:
    """MediaMTX path 名稱 ↔ **機體身分**（`drones.id`），不是 sysid。

    sysid 會被重新指派（issues/040），而錄影與串流都綁在 path 名稱上。
    2026-09-08 已經看過一次後果：`uav-1` 的來源指向另一台機的相機。
    """
    return f"uav-{drone_id}"


def rtsp_url(drone_id: str, host: str, port: int = RTSP_PORT) -> str:
    return f"rtsp://{host}:{port}/{path_for(drone_id)}"


def ext_video(drone_id: str | None, camera_url: str | None, connected: bool,
              host: str, port: int = RTSP_PORT) -> dict:
    """外部控制的 `video` 欄位。**三態分明，不給可能是死的網址。**

    `connected` 傳進來而不是在這裡算——「有沒有連線」各個呼叫端的判準不同
    （command 看心跳 `age_s <= STALE_S`、資料面看 `LiveState.connected`），
    在這裡再定義一次只會多出第三種說法。
    """
    if drone_id is None:
        return {"state": "no_camera", "rtsp": None,
                "reason": "這台還沒有機體記錄，沒有可以綁定的影像路徑"}
    if not (camera_url or "").strip():
        return {"state": "no_camera", "rtsp": None,
                "reason": "這台沒有設定相機來源（在無人機管理頁的「相機來源」填）"}
    if not connected:
        return {"state": "offline", "rtsp": None,
                "reason": "這台現在沒有連線，拉了也不會有畫面"}
    return {"state": "ready", "rtsp": rtsp_url(drone_id, host, port),
            # **這句是契約的一部分**，不是客套話：我們沒有辦法在不去拉的情況下
            # 知道機上相機此刻好不好，所以不能讓對方把 ready 讀成保證
            "note": "來源已設定且這台有連線；機上相機是否正在運作要拉了才知道"}
