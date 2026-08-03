from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://uav:uav@localhost:5432/uav"
    mavlink_url: str = "udpin://0.0.0.0:14540"
    drone_name: str = "sim-uav-1"
    link_source: str = "simulated"   # simulated / modem（真機階段實作 modem 讀取）
    broadcast_hz: float = 5.0        # WebSocket 推送頻率
    db_write_hz: float = 1.0         # telemetry / link_metrics 入庫頻率
    sinr_degraded_db: float = 5.0    # 低於此值發 link_degraded 事件
    sinr_lost_db: float = -2.0       # 低於此值發 link_lost 事件


settings = Settings()
