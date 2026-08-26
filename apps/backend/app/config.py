from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://uav:uav@localhost:35432/uav"  # port 慣例：30000 以上
    mavlink_url: str = "udpin://0.0.0.0:14540"
    # 註：本系統管理哪台無人機（名稱/主機指定）由系統端管理（drones 表 +
    # 無人機頁），不走環境變數——見 issues/011。
    link_source: str = "simulated"   # simulated / modem（真機階段實作 modem 讀取）
    # 自動註冊的機標不標 is_simulated（issue 013-B）：SITL/開發環境所有自動註冊
    # 的都是假機，設 true 讓它們入庫即標 simulated、不混真機清單；生產預設 false
    # （自動註冊的是真機）。dev .env 設 AUTOREGISTER_SIMULATED=true。
    autoregister_simulated: bool = False
    # 原始層錄製（兩層收集，見 doc/gcs-replacement.md）：MAVLink 每框架
    # 無損落盤（tlog）後才轉發給 ingest；~61 MB/hr，依天數滾動清理
    # 任務幾何預檢的圍欄基準（與機上 QGC Geofence 設定保持一致；
    # 匯入 .plan 時回報告，command 服務上傳到機時強制擋）
    # **不再有系統預設圍欄**（2026-08-26 使用者裁定）：圍欄是每份航線自己的
    # 事，一個全域數字只對一個場地成立，而它會產生「seq 6 離起飛點 54 m，
    # 超過圍欄半徑 50 m」這種**看起來很具體的假錯誤**——那個 50 是模擬環境
    # 留下來的值，跟使用者的場地毫無關係。
    # 這兩個值只剩 plan_check 的呼叫簽章還在收，實際上不參與判定；
    # 保留是為了不動簽章，日後確定沒有其他用途再一起拿掉。
    geofence_radius_m: float = 50.0
    geofence_alt_m: float = 15.0
    geofence_margin: float = 0.7     # 超過半徑此比例即警告（Home=擺放位置的餘裕）
    # 群組任務（issue 013；doc/group-missions-design.md）——ops/使用者定終值
    group_vsep_m: float = 5.0        # 相鄰分層垂直間隔
    group_rtl_stagger_m: float = 4.0 # 各台 RTL 返航高度錯開量（013-B 執行期用）
    group_lsep_m: float = 10.0       # 橫向分離門檻（check_group）
    capture_enabled: bool = True
    capture_dir: str = "/data/mavcap"
    capture_keep_days: int = 30
    broadcast_hz: float = 5.0        # WebSocket 推送頻率
    db_write_hz: float = 1.0         # telemetry / link_metrics 入庫頻率
    msg_registry_hz: float = 2.0     # 014-B 泛型訊息登錄表廣播頻率（低於遙測 5Hz）
    # 飛行影像（022）。**保留天數是單一事實源**：同一個 .env 變數同時餵
    # docker-compose 的 MTX_PATHDEFAULTS_RECORDDELETEAFTER（錄製器實際清檔）與
    # 這裡（API 回 retention_days 給前端顯示空態句）。寫死在 UI 或分兩處設定，
    # 就會出現「UI 說 7 天、實際清 14 天」的假話。
    video_retention_days: int = 7
    video_record_enabled: bool = True   # 預設開；特定實驗可關（關閉會在架次留痕）
    video_rec_dir: str = "/rec"         # 與 uav-video 共掛的錄影根目錄
    # 鏈路狀態門檻（ok / degraded / lost 三態，見 app/main.py:_link_transition）
    sinr_degraded_db: float = 5.0    # 低於此值進入 degraded
    sinr_lost_db: float = -2.0       # 低於此值進入 lost
    sinr_hysteresis_db: float = 3.0  # 回升需超過門檻此值才降級／回復，避免門檻附近抖動

    # 模擬器專用（開發鷹架，真機的 PCI 由 modem 回報，不適用）
    # 候選 cell 要強過現任此值才換手。6dB 是實測出來的：定點 300 秒 0–1 次抖動，
    # 飛越兩個 gNB 之間仍正常換手一次。3dB 不夠（仍每 15 秒抖一次）。
    handover_margin_db: float = 6.0


settings = Settings()
