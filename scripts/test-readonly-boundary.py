#!/usr/bin/env python3
"""backend 的 read-only 邊界測試（issue 021 Phase 2 附帶要求）。

**為什麼需要這個測試**：這條邊界原本有兩道保險——程式的 `SEND_WHITELIST`，
以及模擬環境拓撲上 backend 根本送不出封包（fanout 的 backend 腿是 forward-only）。
2026-08-12 拿掉了拓撲那道，理由是它讓模擬與真機行為分岔、藏住真 bug（實證：
「從機上讀回任務」在機隊環境一直是壞的，沒人發現只因為沒人用過）。

保險從兩道變一道，就必須有東西釘住它——否則日後有人往白名單順手加一行，
不會有任何機制擋。本測試就是那個東西。

跑法（容器內，需要 pymavlink）：
    docker exec -w /srv uav-backend python3 /srv/scripts/test-readonly-boundary.py
或從主機：
    docker exec -i -w /srv uav-backend python3 - < scripts/test-readonly-boundary.py
"""
import sys

sys.path.insert(0, "/srv")

from app.mavlink_rx import SEND_WHITELIST, MavlinkRx  # noqa: E402

# 這份清單就是「本服務被允許送出的全部訊息」。**改動白名單就會讓這個測試失敗**，
# 逼改的人回來改這裡——那正是我們要的：擴充邊界必須是有意識的動作，不能順手。
EXPECTED = {
    "MISSION_REQUEST_LIST",   # 任務讀回握手
    "MISSION_REQUEST_INT",
    "MISSION_ACK",
    "PARAM_REQUEST_LIST",     # 021 Phase 2：參數快照（唯讀查詢）
    "PARAM_REQUEST_READ",
}

# 這些**永遠不該**出現在白名單裡。PARAM_SET 尤其關鍵：參數編輯是 QGC 的職權，
# 本系統只記錄「當時設定是什麼」，不去改它。
FORBIDDEN = ["PARAM_SET", "COMMAND_LONG", "COMMAND_INT", "SET_MODE",
             "MISSION_ITEM_INT", "MISSION_COUNT", "MANUAL_CONTROL"]


class _FakeMsg:
    def __init__(self, name):
        self._name = name

    def get_type(self):
        return self._name


def main():
    fails = []

    # 1. 白名單內容本身（改了就要有人回來看這個測試）
    if SEND_WHITELIST != EXPECTED:
        fails.append(
            "SEND_WHITELIST 與測試預期不符。\n"
            f"    多出：{sorted(SEND_WHITELIST - EXPECTED)}\n"
            f"    缺少：{sorted(EXPECTED - SEND_WHITELIST)}\n"
            "    → 若這是有意的擴充：確認新訊息**不會改變機上狀態**，"
            "再同步更新本測試的 EXPECTED。")

    # 2. 禁止清單一個都不能在白名單裡
    leaked = [m for m in FORBIDDEN if m in SEND_WHITELIST]
    if leaked:
        fails.append(f"**會改變機上狀態的訊息出現在白名單**：{leaked}")

    # 3. _send 對白名單外的訊息必須擋下（且不是靠 assert——assert 在 -O 下會消失）
    rx = object.__new__(MavlinkRx)
    rx.sysids = {}                      # 空的：擋下時不該走到「查 addr」那步
    for name in FORBIDDEN:
        try:
            rx._send(1, _FakeMsg(name))
            fails.append(f"_send 沒有擋下 {name}（邊界破了）")
        except PermissionError:
            pass                        # 正確：明確拒絕
        except Exception as e:
            fails.append(f"_send 擋 {name} 時丟了非預期的例外：{type(e).__name__}: {e}")

    # 4. 白名單內的訊息要能通過檢查（用未連線的 sysid → 應該是 RuntimeError
    #    「未連線」，而不是 PermissionError「不在白名單」）
    for name in sorted(EXPECTED):
        try:
            rx._send(99, _FakeMsg(name))
            fails.append(f"{name}：預期因 sysid 未連線而失敗，卻通過了")
        except PermissionError:
            fails.append(f"{name} 在白名單內卻被當成違規擋下")
        except RuntimeError:
            pass                        # 正確：過了白名單，卡在「未連線」
        except Exception as e:
            fails.append(f"{name}：非預期例外 {type(e).__name__}: {e}")

    if fails:
        print("read-only 邊界測試 **失敗**：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print(f"read-only 邊界測試 OK（白名單 {len(SEND_WHITELIST)} 項，"
          f"擋下 {len(FORBIDDEN)} 種會改變機上狀態的訊息）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
