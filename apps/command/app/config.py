from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://uav:uav@localhost:35432/uav"
    # 生產：機上 mavlink-router 的 command 端點（14541 雙向）。
    # 開發：SITL 的 QGC 通道空著，COMMAND_MAVLINK_URL=udpin://0.0.0.0:14550。
    command_mavlink_url: str = "udpin://0.0.0.0:14541"
    # 安全 gate：預設關。關閉時所有指令端點回 403，也不發 GCS 心跳
    # （不發＝不進入 PX4 的 datalink-loss 安全鏈，純觀察不擔責）。
    enable_commands: bool = False
    # 任務幾何預檢（與 backend 共用 .env 的 GEOFENCE_*）：
    # 2026-08-10 使用者決定預設**不擋**——報告照附（回應的 check 欄位、
    # command_log 留痕），GEOFENCE_ENFORCE=true 才恢復 409 擋門。
    # 空中的真正防線仍是 PX4 自己的 Geofence（QGC 設定的那個）。
    geofence_enforce: bool = False
    # 起飛→任務序列要讀高度（等實際到達才切 MISSION），來源是 backend 的
    # live 快照（同主機不同容器）
    backend_api: str = "http://localhost:38000"
    # **不再有系統預設圍欄**（2026-08-26 使用者裁定）：圍欄是每份航線自己的
    # 事，一個全域數字只對一個場地成立，而它會產生「seq 6 離起飛點 54 m，
    # 超過圍欄半徑 50 m」這種**看起來很具體的假錯誤**——那個 50 是模擬環境
    # 留下來的值，跟使用者的場地毫無關係。
    # 這兩個值只剩 plan_check 的呼叫簽章還在收，實際上不參與判定；
    # 保留是為了不動簽章，日後確定沒有其他用途再一起拿掉。
    geofence_radius_m: float = 50.0
    geofence_alt_m: float = 15.0
    geofence_margin: float = 0.7
    # 地形預檢（issues/047 §1-B）：**預設會擋**，而且**與 GEOFENCE_ENFORCE
    # 分開**。理由是兩者的性質不同：圍欄的系統預設值是「這套系統只在一個
    # 場地飛」才成立的假設，擋下來多半是假錯誤；而「預期離地是負的」講的是
    # 航線本身穿過地面，那是 2026-09-07 那一趟摔機的形狀。
    #
    # **但留得下退路。** SRTM 在有樹的地方量到的是樹冠不是地面，所以
    # 「地面比航線高」有可能是樹造成的假警報——被擋住的時候有兩條路：
    # 把高度拉高，或把這個開關關掉（關掉的那一次會進 command_log 留痕）。
    terrain_enforce: bool = True
    # 簽核閘門（doc/route-planning-redesign.md §7，使用者裁定 2026-09-09）：
    # **上傳那一刻分不出「沒人看過檢查結果」與「看過、按了照飛」**，所以
    # 在有簽核以前，terrain_enforce 只能全擋或全不擋。簽核補的就是那一半。
    #
    # 擋的規則：沒簽核、或簽核之後航點改過（hash 對不上）、或還有沒被
    # 逐條 ack 的 problem。**退路一樣留著**：關掉的那一次會留痕。
    sign_enforce: bool = True
    # 外部觸發（POST /api/start / GET /api/plans）：repo 的 missions/ 掛進容器唯讀
    missions_dir: str = "/srv/missions"


settings = Settings()
