"""可以從本系統改的飛控參數——**白名單，不是參數編輯器**。

## 為什麼是白名單（2026-09-07 使用者裁定「這個系統可以改 param_set」）

在這之前本系統一個參數都不寫：`mavlink_rx.py` 的送出白名單裡明文寫著
「PARAM_SET 永遠不得加入」。那條規則有兩層意思，這次只改第二層：

1. **後端那條 socket 維持唯讀。** 它是遙測與錄製的路——它永遠不該成為
   指令的來源。寫入做在指令服務（本檔所在的地方），那裡本來就會解鎖、
   切模式、上傳任務。
2. 「參數編輯是 QGC 的職權」——**這一層改掉**。

改成白名單而不是全開，理由是**這個畫面上沒有 QGC 有的東西**：參數說明、
允許範圍、單位、重開機才生效的標記。1176 筆全開，等於給一個沒有標籤的
儀表板，而寫錯一個值的後果是飛控飛不起來或開不了機。

## 加一個參數要附什麼

`label`（人話）、`unit`、`lo`/`hi`（**我方的範圍，不是飛控的**——飛控也會
自己夾，但夾完不會告訴你）、`why`（為什麼有人會想改它）。
沒有把握就不要加：一個沒有範圍的欄位比沒有這個欄位危險。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    label: str
    unit: str
    lo: float
    hi: float
    why: str
    #: 整數參數。PARAM_SET 的值一律是 float，但型別要對——
    #: 用浮點型別去寫一個整數參數，ArduPilot 存進去的會是別的數字
    is_int: bool = False


#: **只有這裡列出來的參數寫得進去。** 其餘一律 400，連送都不送。
ALLOWED: dict[str, Param] = {
    # ── 降落 ────────────────────────────────────────────────
    "LAND_SPEED": Param(
        "降落速度（最後一段）", "cm/s", 30, 200,
        "觸地前的下降速度。ArduPilot 的下限就是 30——再低下去，落地偵測"
        "分不出「在下降」和「感測雜訊」，機會懸在地效區裡遲遲不上鎖。",
        is_int=True),
    "LAND_SPEED_HIGH": Param(
        "降落速度（高空段）", "cm/s", 0, 500,
        "LAND_ALT_LOW 以上的下降速度。**0 代表沿用 WPNAV_SPEED_DN**——"
        "所以它顯示 0 不是「不下降」，是「跟著另一個參數走」。",
        is_int=True),
    "LAND_ALT_LOW": Param(
        "降落減速高度", "cm", 100, 10000,
        "降到這個高度以下就換成 LAND_SPEED。**這台機沒有測距儀**，這個高度"
        "來自氣壓計，會漂——把兩段速度設成一樣就不需要這個判斷。",
        is_int=True),
    # ── 航線速度 ────────────────────────────────────────────
    "WPNAV_SPEED_DN": Param(
        "任務下降速度", "cm/s", 10, 500,
        "任務航線裡往下飛的速度（不是最後的降落段）。LAND_SPEED_HIGH 為 0 時"
        "降落的高空段也用它。", is_int=True),
    "WPNAV_SPEED_UP": Param(
        "任務爬升速度", "cm/s", 10, 1000, "任務航線裡往上飛的速度。", is_int=True),
    "WPNAV_SPEED": Param(
        "任務水平速度", "cm/s", 20, 2000, "任務航線的水平巡航速度。", is_int=True),
    # ── 返航 ────────────────────────────────────────────────
    "RTL_ALT": Param(
        "返航高度", "cm", 200, 8000,
        "返航時先爬到這個高度再飛回來。太低會撞到起降點與現在位置之間的東西。",
        is_int=True),
    "RTL_ALT_FINAL": Param(
        "返航結束高度", "cm", 0, 1000,
        "回到起降點之後停在這個高度。**0 代表直接降落**（於是 LAND_SPEED 也適用）。",
        is_int=True),
}


def validate(name: str, value: float) -> str | None:
    """認不得或超範圍就回一句話（可直接呈現給操作員）；沒問題回 None。"""
    p = ALLOWED.get(name)
    if p is None:
        return (f"{name} 不在可修改清單裡。本系統只開放飛行安全相關的少數參數，"
                f"其餘請用 QGC——那裡才有參數說明與範圍提示")
    if not (p.lo <= value <= p.hi):
        return (f"{name} = {value} 超出允許範圍 {p.lo:g}～{p.hi:g} {p.unit}。"
                f"{p.why}")
    if p.is_int and float(value) != int(value):
        return f"{name} 是整數參數（{p.unit}），不接受 {value}"
    return None
