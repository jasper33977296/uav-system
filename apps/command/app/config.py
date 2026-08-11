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
    geofence_radius_m: float = 50.0
    geofence_alt_m: float = 15.0
    geofence_margin: float = 0.7
    # 航線檔目錄（repo 的 missions/ 掛進容器；GET /api/plans 與 POST /api/start
    # 只認這一層下的 .plan——外部給的檔名是不受信任輸入，見 plans.resolve）
    missions_dir: str = "/srv/missions"


settings = Settings()
