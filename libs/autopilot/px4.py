"""PX4 驅動（issue 026 B2）。

內容是從 `apps/command/app/mav.py` 的 `dialect()`、`apps/backend/app/dialect.py`
與 `apps/command/app/capabilities.py` **原樣提取**的 PX4 分支——B2 是搬遷，不是
改寫。任何行為變更都算回歸。
"""
from .driver import Limit

NAME = "px4"
AUTOPILOT_RAW = 12                  # MAV_AUTOPILOT_PX4

# DO_SET_MODE 的 param2/param3（main_mode, sub_mode）
MODES = {
    "position": (3, 0),   # POSCTL（手動位置控制：搖桿→速度，鬆手懸停）
    "mission":  (4, 4),   # AUTO.MISSION
    "hold":     (4, 3),   # AUTO.LOITER
    "rtl":      (4, 5),   # AUTO.RTL
    "land":     (4, 6),   # AUTO.LAND
}

# custom_mode 是 main<<16|sub<<24 的 union
_MAIN = {1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 5: "ACRO",
         6: "OFFBOARD", 7: "STABILIZED", 8: "RATTITUDE"}
_AUTO = {1: "READY", 2: "TAKEOFF", 3: "HOLD", 4: "MISSION",
         5: "RETURN_TO_LAUNCH", 6: "LAND", 8: "FOLLOW_ME", 9: "PRECLAND"}

CAP_KEYS = ["arm", "takeoff", "land", "rtl", "hold",
            "mission_upload", "mission_start", "mission_fly", "manual"]


class Px4Driver:
    name = NAME
    autopilot_raw = AUTOPILOT_RAW
    modes = MODES

    #: PX4 不需要任何訊息層改名——它發的就是我們的標準形。
    MESSAGE_ADJUSTMENTS = ()

    # ── 訊息層 ──────────────────────────────────────────────────────
    def adjust_incoming(self, msg):
        return msg

    def adjust_outgoing(self, msg):
        return msg

    # ── 模式 ────────────────────────────────────────────────────────
    def decode_mode(self, custom_mode: int) -> str:
        if not custom_mode:          # 0＝尚未設定模式（開機瞬間），不顯 MODE_0
            return "—"
        main, sub = (custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF
        if main == 4:
            return _AUTO.get(sub, f"AUTO_{sub}")
        return _MAIN.get(main, f"MODE_{main}")

    def encode_mode(self, mode: str) -> tuple[int, int]:
        return MODES[mode]

    def mode_matches(self, custom_mode: int, mode: str) -> bool:
        return ((custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF) == MODES[mode]

    # ── 動作 ────────────────────────────────────────────────────────
    def takeoff_plan(self, alt: float, ground_amsl: float | None) -> dict:
        """PX4 的 NAV_TAKEOFF param7 是**絕對海拔**，要用地面海拔＋目標高度。

        空白參數（偏航／經緯度）用 NaN＝「用當前值」。
        """
        if ground_amsl is None:
            raise ValueError("PX4 起飛需要地面海拔（GPS/EKF 未就緒）")
        return {"needs_guided": False, "param7": ground_amsl + alt,
                "blank": float("nan"), "alt_semantics": "amsl"}

    def manual_prepare(self) -> str | None:
        return "position"            # POSCTL

    def mission_line(self, items: list[dict]) -> list[dict]:
        return items                 # PX4 不把 home 當 seq 0

    # ── 連線與就緒 ──────────────────────────────────────────────────
    def on_connect(self) -> list:
        return []                    # PX4 預設就串流遙測

    def keepalive(self) -> list:
        return []

    def readiness_signals(self) -> tuple[str, ...]:
        return ("prearm", "ekf")     # PX4 有 PREARM_CHECK 健康位

    # ── 值域與能力 ──────────────────────────────────────────────────
    def limits(self) -> dict[str, Limit]:
        # **不照抄 QGC 的數字**——那等於把別人的驗證當成我們的驗證。
        # B3 一致性測試實測後才會升成 sitl。
        return {"takeoff_alt_m": Limit(confidence="unverified")}

    def capabilities(self, ctx: dict | None = None):
        return {k: "ok" for k in CAP_KEYS}, {}
