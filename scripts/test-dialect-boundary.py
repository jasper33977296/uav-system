#!/usr/bin/env python3
"""backend 方言層的邊界測試（issue 026 B0）。

**為什麼需要這個測試**：`dialect.py` §1 宣稱「EKF_STATUS_REPORT 可以當
ESTIMATOR_STATUS 讀」。那個宣稱**只在 bit 1..512 成立**——bit 1024 在兩邊
分別是 ESTIMATOR_GPS_GLITCH 與 EKF_UNINITIALIZED，同位元不同意義。

這種「在某個範圍內成立」的等價最危險的地方在於：**寫的時候是對的，用的時候
沒人記得範圍**。有人日後要讀第 1024 位，程式不會報錯，只會靜默給出錯的答案。
本測試把範圍釘住：pymavlink 方言若變動、或有人把安全區改大，這裡就會失敗。

跑法：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-dialect-boundary.py
"""
import sys

sys.path.insert(0, "/srv")

from pymavlink.dialects.v20 import ardupilotmega as M  # noqa: E402

from app import dialect  # noqa: E402


def main():
    fails = []

    # ── 1. 宣告的安全區內，兩個 enum 必須逐位同名同義 ──────────────
    est = {v: k[len("ESTIMATOR_"):] for k, v in vars(M).items()
           if k.startswith("ESTIMATOR_") and isinstance(v, int)}
    ekf = {v: k[len("EKF_"):] for k, v in vars(M).items()
           if k.startswith("EKF_") and isinstance(v, int)}

    bit = 1
    while bit <= dialect.EKF_ALIAS_SAFE_BITS:
        if bit & dialect.EKF_ALIAS_SAFE_BITS:
            a, b = est.get(bit), ekf.get(bit)
            if a is None or b is None or a != b:
                fails.append(
                    f"安全區內 bit {bit} 兩邊不同義："
                    f"ESTIMATOR_{a} vs EKF_{b}\n"
                    "    → 這個位元不能再當成可互換。縮小 EKF_ALIAS_SAFE_BITS，"
                    "並確認沒有程式在讀它。")
        bit <<= 1

    # ── 2. 安全區**外**必須真的有分歧（否則這個邊界是白畫的）────────
    #    這條反向檢查的用意：如果哪天兩邊變成完全一致，安全區就該放寬；
    #    留著一個過度保守又沒人知道為什麼的常數，比沒有還糟。
    outside_diverges = False
    for v in set(est) | set(ekf):
        if v & dialect.EKF_ALIAS_SAFE_BITS or v.bit_count() != 1:
            continue                      # 只看單一位元、且在安全區外的
        if est.get(v) != ekf.get(v):
            outside_diverges = True
    if not outside_diverges:
        fails.append(
            "安全區外找不到任何分歧位元——EKF_ALIAS_SAFE_BITS 可能過度保守。\n"
            "    → 確認 pymavlink 方言是否已對齊；若是，放寬常數並更新 §1 說明。")

    # ── 3. 就緒位元必須落在安全區內（我們實際用到的那三個）──────────
    if dialect.EKF_READY_BITS & ~dialect.EKF_ALIAS_SAFE_BITS:
        fails.append(
            f"EKF_READY_BITS({dialect.EKF_READY_BITS:#x}) 用到了安全區外的位元——"
            "**這代表我們正在跨廠牌讀一個不同義的位元**。")

    # ── 4. ekf_ready 的實際行為（兩家常數算出來必須一致）───────────
    px4_need = (M.ESTIMATOR_ATTITUDE | M.ESTIMATOR_VELOCITY_HORIZ
                | M.ESTIMATOR_POS_HORIZ_ABS)
    apm_need = (M.EKF_ATTITUDE | M.EKF_VELOCITY_HORIZ | M.EKF_POS_HORIZ_ABS)
    if px4_need != apm_need:
        fails.append(f"兩家所需位元不再相等：{px4_need:#x} vs {apm_need:#x}")
    if dialect.EKF_READY_BITS != px4_need:
        fails.append(
            f"EKF_READY_BITS({dialect.EKF_READY_BITS:#x}) 與 pymavlink 常數"
            f"({px4_need:#x}) 不符——B0 搬運時算錯了。")
    for flags, want in [(px4_need, True), (px4_need | 0x20, True),
                        (px4_need & ~1, False), (0, False)]:
        if dialect.ekf_ready(flags) is not want:
            fails.append(f"ekf_ready({flags:#x}) 應為 {want}")

    # ── 5. 模式解讀（B0 是純搬運，值必須與搬運前相同）──────────────
    cases = [
        # (custom_mode, autopilot_raw, 期望)
        (0, 12, "—"),                       # 未設定模式
        (0, 3, "—"),
        (4 << 16 | 4 << 24, 12, "MISSION"),  # PX4 AUTO.MISSION
        (3 << 16, 12, "POSCTL"),             # PX4 主模式
        (4, 3, "GUIDED"),                    # ArduPilot Copter GUIDED
        (6, 3, "RTL"),
        (99, 3, "MODE_99"),                  # 未知 ArduPilot 模式號
        (3 << 16, None, "POSCTL"),           # unknown 廠牌 → 按 PX4 解（既有行為）
    ]
    for cm, ap, want in cases:
        got = dialect.mode_name(cm, ap)
        if got != want:
            fails.append(f"mode_name({cm:#x}, {ap}) = {got!r}，應為 {want!r}")

    # ── 6. 串流請求策略：只有 ArduPilot 要 ──────────────────────────
    for raw, want in [(3, True), (12, False), (None, False), (99, False)]:
        if dialect.needs_stream_request(raw) is not want:
            fails.append(f"needs_stream_request({raw}) 應為 {want}")

    # ── 7. 廠牌名不得寫死在就緒原因裡 ──────────────────────────────
    if dialect.prearm_label(3) == dialect.prearm_label(12):
        fails.append("prearm_label 對兩家回同一個字串——廠牌名又寫死了")
    if "PX4" not in dialect.prearm_label(12):
        fails.append("prearm_label(px4) 應含 PX4")

    # ── 8. mavlink_rx 不得再持有廠牌表（B0 的收斂本身要被釘住）──────
    import app.mavlink_rx as rx
    leaked = [n for n in ("_PX4_MAIN", "_PX4_AUTO", "_ARDU_COPTER",
                          "_AUTOPILOT_NAMES", "_mode_name", "autopilot_name")
              if hasattr(rx, n)]
    if leaked:
        fails.append(
            f"mavlink_rx 又出現方言名字：{leaked}\n"
            "    → 方言只能住在 dialect.py。留轉出等於留下第二個看似權威的位置。")

    if fails:
        print("方言邊界測試 **失敗**：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print(f"方言邊界測試 OK（等價安全區 {dialect.EKF_ALIAS_SAFE_BITS:#x}，"
          f"就緒位元 {dialect.EKF_READY_BITS:#x}，模式案例 {len(cases)} 項）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
