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


#: **只有這裡列出來的參數寫得進去。**
#:
#: ## 為什麼同一件事會有兩個名字（2026-09-07 踩到）
#:
#: **ArduCopter 4.7 把參數改名成 SI 單位**：`LAND_SPEED`（cm/s）變成
#: `LAND_SPD_MS`（m/s）、`LAND_ALT_LOW`（cm）變成 `LAND_ALT_LOW_M`（m）、
#: `RTL_ALT` 變成 `RTL_ALT_M`。**單位跟著名字一起變**，所以不能只換個名字
#: 就沿用同一組範圍。
#:
#: 這裡兩代都列，是因為**同一個地面站要同時面對兩代**：現場那台是 4.7，
#: 模擬器是舊版。讀取時兩個都問，機上有哪個就回哪個（沒有的那個進 `missing`
#: ——ArduPilot 對不認得的參數名是**安靜不回**，那正是這次查了大半天的原因：
#: 我方一直在問一個這台機上不存在的名字，而「不存在」與「不回應」同形）。
ALLOWED: dict[str, Param] = {
    # ── 降落：4.7 之後（SI 單位）────────────────────────────
    "LAND_SPD_MS": Param(
        "降落速度（最後一段）", "m/s", 0.3, 2.0,
        "觸地前的下降速度。0.3 是 ArduPilot 的下限——再慢下去，落地偵測"
        "分不出「在下降」和「感測雜訊」，機會懸在地效區裡遲遲不上鎖。"),
    "LAND_SPD_HIGH_MS": Param(
        "降落速度（高空段）", "m/s", 0, 5.0,
        "LAND_ALT_LOW_M 以上的下降速度。**0 代表沿用航線的下降速度**"
        "——它顯示 0 不是「不下降」。"),
    "LAND_ALT_LOW_M": Param(
        "降落減速高度", "m", 1, 100,
        "降到這個高度以下就換成 LAND_SPD_MS。**這台機沒有測距儀**"
        "（RNGFND1_TYPE=0），這個高度來自氣壓計、會漂——把兩段速度設成一樣"
        "就不需要這個判斷。"),
    "RTL_ALT_M": Param(
        "返航高度", "m", 2, 80,
        "返航時先爬到這個高度再飛回來。太低會撞到起降點與現在位置之間的東西。"),
    "RTL_ALT_FINAL_M": Param(
        "返航結束高度", "m", 0, 10,
        "回到起降點之後停在這個高度。**0 代表直接降落**（於是降落速度也適用）。"),

    # ── 降落：4.7 之前（cm/s、cm）。模擬器與舊韌體還在用 ──────
    "LAND_SPEED": Param(
        "降落速度（最後一段）", "cm/s", 30, 200,
        "4.7 之前的名字，單位是 cm/s。同 LAND_SPD_MS。", is_int=True),
    "LAND_SPEED_HIGH": Param(
        "降落速度（高空段）", "cm/s", 0, 500,
        "4.7 之前的名字。**0 代表沿用 WPNAV_SPEED_DN**。", is_int=True),
    "LAND_ALT_LOW": Param(
        "降落減速高度", "cm", 100, 10000,
        "4.7 之前的名字，單位是 cm。", is_int=True),
    "RTL_ALT": Param("返航高度", "cm", 200, 8000,
                     "4.7 之前的名字，單位是 cm。", is_int=True),
    "RTL_ALT_FINAL": Param("返航結束高度", "cm", 0, 1000,
                           "4.7 之前的名字，單位是 cm。", is_int=True),
    "WPNAV_SPEED_DN": Param(
        "任務下降速度", "cm/s", 10, 500,
        "4.7 之前的名字。任務航線裡往下飛的速度（不是最後的降落段）。",
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
