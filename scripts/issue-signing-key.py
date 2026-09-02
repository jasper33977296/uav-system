#!/usr/bin/env python3
"""配發一把簽章金鑰給某塊飛控板（issues/040 A5-b）。

**這是離線配發的工具，不是 API。** 沒有做成端點是刻意的：一個能配發金鑰的
HTTP 端點，就是一個能把金鑰要走的端點——而 backend 目前沒有任何認證。

配發流程（設計 §2.2 的「雞生蛋」只能這樣解）：

  1. 在地面站跑本工具 → 它把金鑰寫進地面站的金鑰檔，並**印出金鑰一次**
  2. 把印出來的金鑰**離線**帶到機上（安裝代理時寫入設定）
  3. 兩邊各自算指紋，代理在 hello 裡回報，地面站比對（A5-c）

**印出來的那一次是唯一一次**：之後只查得到指紋，查不回金鑰。

用法（在 backend 容器內跑，金鑰檔在它的 volume 裡）：
    docker exec -it -w /srv uav-backend python3 -m scripts.issue_signing_key <board_uid>

或用管線（本檔的實際跑法）：
    docker exec -i -w /srv uav-backend python3 - <board_uid> < scripts/issue-signing-key.py
"""
import sys

from app import signing

if len(sys.argv) < 2:
    print(__doc__)
    print("**缺 board_uid**。查法：curl -s localhost:38000/api/drones | grep board_uid")
    raise SystemExit(2)

uid = sys.argv[1].strip()
force = "--rotate" in sys.argv

existing = signing.get_fingerprint(uid)
if existing and not force:
    print(f"這塊板子已經有金鑰了（指紋 {existing}）。")
    print("**不覆蓋**：覆蓋等於把那台機的簽章打斷，而它此刻可能正在飛。")
    print("真的要輪換請加 --rotate，並記得同時更新機上那一把——"
          "**只換一邊的下場是全部封包被靜默丟棄**。")
    raise SystemExit(1)

fp, created = signing.issue_key(uid, overwrite=force)
key = signing.get_key(uid)
print("=" * 68)
print(f"板號     : {uid}")
print(f"指紋     : {fp}")
print(f"金鑰(hex): {key}")
print("=" * 68)
print("**這是唯一一次印出金鑰。** 請立刻離線帶到機上寫進代理設定，然後清掉終端機。")
print("之後只查得到指紋（`signing.get_fingerprint`），查不回金鑰。")
print("")
print("提醒：本批**尚未在線上啟用簽章**——配發與指紋自檢先做，")
print("因為簽章不符是**靜默丟棄**：金鑰不對時畫面一切正常而指令全部消失。")
