from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://uav:uav@localhost:35432/uav"
    # 生產：機上 mavlink-router 的 command 端點（14541 雙向）。
    # 開發：SITL 的 QGC 通道空著，COMMAND_MAVLINK_URL=udpin://0.0.0.0:14550。
    command_mavlink_url: str = "udpin://0.0.0.0:14541"
    # 安全 gate：預設關。關閉時所有指令端點回 403，也不發 GCS 心跳
    # （不發＝不進入 PX4 的 datalink-loss 安全鏈，純觀察不擔責）。
    enable_commands: bool = False
    # 任務幾何預檢（與 backend 共用 .env 的 GEOFENCE_*，對齊機上 QGC 設定）：
    # 上傳到機前強制檢查，有 problem 直接 409——這裡是安全門
    geofence_radius_m: float = 50.0
    geofence_alt_m: float = 15.0
    geofence_margin: float = 0.7


settings = Settings()
