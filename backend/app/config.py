from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://uav:uav@localhost:35432/uav"  # port 慣例：30000 以上
    mavlink_url: str = "udpin://0.0.0.0:14540"
    drone_name: str = "sim-uav-1"
    link_source: str = "simulated"   # simulated / modem（真機階段實作 modem 讀取）
    broadcast_hz: float = 5.0        # WebSocket 推送頻率
    db_write_hz: float = 1.0         # telemetry / link_metrics 入庫頻率
    # 鏈路狀態門檻（ok / degraded / lost 三態，見 app/main.py:_link_transition）
    sinr_degraded_db: float = 5.0    # 低於此值進入 degraded
    sinr_lost_db: float = -2.0       # 低於此值進入 lost
    sinr_hysteresis_db: float = 3.0  # 回升需超過門檻此值才降級／回復，避免門檻附近抖動

    # 模擬器專用（開發鷹架，真機的 PCI 由 modem 回報，不適用）
    # 候選 cell 要強過現任此值才換手。6dB 是實測出來的：定點 300 秒 0–1 次抖動，
    # 飛越兩個 gNB 之間仍正常換手一次。3dB 不夠（仍每 15 秒抖一次）。
    handover_margin_db: float = 6.0


settings = Settings()
