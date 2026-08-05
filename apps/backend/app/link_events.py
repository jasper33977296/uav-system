"""鏈路狀態機：SINR 位準 → 離散的鏈路事件。

獨立成一個模組，因為模擬與真機兩條路徑都要用它：
模擬走 `main._link_and_db_loop`，真機走 `api` 的即時通道 endpoint。
放在 main.py 會讓 api.py 反向 import 而形成循環。
"""
from . import db
from .config import settings
from .state import LiveState
from .ws import manager


async def transition(s: LiveState, m: dict) -> None:
    """依這筆量測更新 `s.link_state`，跨級時發一次事件。

    以 state 為參數而非全域 live：主機（MAVLink）與群飛模擬的每台僚機
    各自持有 LiveState，共用同一個狀態機。

    SINR 是連續量、事件是離散點，直接比大小會在干擾區內每秒發一筆重複事件。
    這裡用 ok / degraded / lost 三態，只在真的跨級時發事件。

    每一級的回升都要多 `sinr_hysteresis_db` 才算數（lost→degraded 要 -2+3=1dB、
    degraded→ok 要 5+3=8dB），避免 SINR 在門檻附近抖動時來回發事件。

    設計取向是「忠實記錄事實」：每一次跨級都留紀錄（包含從 lost 回到 degraded
    這種中間轉換），detail 帶上 from 欄位，事件序列可完整還原鏈路狀態變化。
    不採用 time-to-trigger——那會讓事件時間戳晚於實際發生時刻，而「位置 ↔ 鏈路
    劣化」的時間對應正是本研究的重點。見 issues/001。

    不發 handover 事件——無人機等價於一台 UE，換手由 modem 與網路側處理，
    應用層不參與也不研究它。服務 cell 仍以 pci 欄位記錄在 link_metrics。
    """
    sinr = m.get("sinr")
    if sinr is None:             # modem 偶爾回不出 SINR，不能讓它變成狀態轉換
        return
    state = s.link_state
    lost_th, deg_th = settings.sinr_lost_db, settings.sinr_degraded_db
    hyst = settings.sinr_hysteresis_db

    if sinr < lost_th and state != "lost":
        new, severity, type_ = "lost", "critical", "link_lost"
    elif lost_th + hyst <= sinr < deg_th and state == "lost":
        new, severity, type_ = "degraded", "warning", "link_degraded"
    elif sinr < deg_th and state == "ok":
        new, severity, type_ = "degraded", "warning", "link_degraded"
    elif sinr >= deg_th + hyst and state != "ok":
        new, severity, type_ = "ok", "info", "link_recovered"
    else:
        return

    s.link_state = new
    if not s.session_id:         # 架次外不發事件（同 issues/004 的 gate 語意）
        return

    ev = await db.insert_event(
        s.drone_id, s.session_id, severity, type_,
        {"sinr": sinr, "in_zone": m.get("in_interference_zone"), "from": state},
    )
    ev["drone"] = s.drone_name       # 多機時事件流要能分辨是哪台
    # 事件要巢狀包住，不能 {"type": "event", **ev} 展開——ev 自帶 type 欄位
    # （link_lost 等），會把外層的 "type": "event" 蓋掉，前端因此永遠比對不到。
    await manager.broadcast({"type": "event", "event": ev})
