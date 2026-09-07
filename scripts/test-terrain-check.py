#!/usr/bin/env python3
"""地形預檢（issues/047 §1-B）：離線自我驗證。

**重點不是「算得對不對」，是「不知道的時候會不會假裝知道」。**
三種會產生假結論的失效，各有一組案例：

  1. **只檢查航點**：兩個航點各有 5 m 餘裕，中間隔著一個 8 m 的土坡——
     只看航點的檢查會回報「通過」。這是本檢查存在的理由（§ 3）。
  2. **沒有圖磚時回報通過**：查不到高程與「離地很夠」在資料上長得一樣
     （都沒有 problem），必須有一句話說出「沒有檢查」（§ 4）。
  3. **把空洞內插成數字**：`-32768` 是雷達沒測到，拿它去做雙線性內插
     會得到一個看起來很正常的負幾千（§ 2）。

跑法（不需要服務、不需要資料庫、不需要真圖磚）：
    python3 scripts/test-terrain-check.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, "/home/k200/uav-system/libs")

import plan_check as P  # noqa: E402
import terrain as T  # noqa: E402

ok = True


def chk(label, cond, note=""):
    global ok
    ok &= bool(cond)
    print(f"{'✓' if cond else '✗'} {label}{('｜' + str(note)) if note else ''}")


N = 3601                      # 1 弧秒版：格距約 30 m，跟真圖磚一致
TMP = tempfile.mkdtemp(prefix="dem-")


def write_tile(name, fn):
    """fn(row, col) → 高程（int）。row 0 是**北**邊。"""
    buf = bytearray(N * N * 2)
    for r in range(N):
        for c in range(N):
            struct.pack_into(">h", buf, (r * N + c) * 2, fn(r, c))
    with open(os.path.join(TMP, name), "wb") as f:
        f.write(buf)


# 底 100 m，第 1795～1797 列（緯度 0.5008～0.5014，約在起飛點北方 90～155 m）
# 有一條 +8 m 的土堤
def ground(r, c):
    return 108 if 1795 <= r <= 1797 else 100


write_tile("N00E000.hgt", ground)
dem = T.Dem(TMP)

print("── 1. 圖磚定址：第 0 列是北邊，命名要含南/西半球 ──────────")
# 土堤寫在第 1795～1797 列。第 0 列若不是北邊，它會出現在起飛點**南方**
chk("土堤在起飛點北邊（證明第 0 列是北）",
    dem.elevation(0.50111, 0.5) == 108 and dem.elevation(0.49889, 0.5) == 100,
    (dem.elevation(0.50111, 0.5), dem.elevation(0.49889, 0.5)))
chk("起飛點緯度是平地 100 m", dem.elevation(0.5, 0.5) == 100)
chk("南半球/西半球的檔名用 floor 不是 int",
    T.tile_name(-0.5, -0.5) == "S01W001.hgt", T.tile_name(-0.5, -0.5))
chk("N24E121 的西南角是 (24,121)", T.tile_name(24.77, 121.04) == "N24E121.hgt")

print("\n── 2. 空洞不內插成數字 ──────────────────────────────")
write_tile("N01E000.hgt", lambda r, c: T.VOID if (r, c) == (1800, 1800) else 100)
d2 = T.Dem(TMP)
chk("四角有空洞就回 None（不是 −32768、也不是內插出來的負幾千）",
    d2.elevation(1.5, 0.5) is None, d2.elevation(1.5, 0.5))
chk("旁邊沒碰到空洞的格子照常有值", d2.elevation(1.4, 0.4) == 100)

print("\n── 3. 只檢查航點會漏掉的：兩點之間的土堤 ────────────────")
home = {"lat": 0.5, "lon": 0.5}
# 起飛點與兩個航點都在平地，相對高度 5 m ⇒ 航點各有 5 m 餘裕
wps = [
    {"seq": 0, "lat": 0.5, "lon": 0.5, "alt": 5, "command": 22, "frame": 3},
    {"seq": 1, "lat": 0.503, "lon": 0.5, "alt": 5, "command": 16, "frame": 3},
]
r = P.check_terrain(wps, home, dem=dem)
chk("航點本身餘裕 5 m（單看航點會說通過）",
    dem.elevation(0.503, 0.5) == 100)
chk("**沿線取樣抓到土堤，判為 problem**", len(r["problems"]) == 1,
    r["problems"])
chk("問題指得出位置與差多少",
    "之間" in r["problems"][0] and "-3.0 m" in r["problems"][0],
    r["problems"][0])
chk("結構化結果的最小離地是 −3.0", r["terrain"]["min_clearance_m"] == -3.0,
    r["terrain"])
chk("附上解析度的誠實話", any("SRTM" in w for w in r["warnings"]))

print("\n── 4. 沒有資料時說「沒有檢查」，不是「通過」 ──────────────")
r = P.check_terrain(wps, home, dem=None)
chk("沒給 DEM：沒有 problem，但有一句沒檢查",
    not r["problems"] and any("沒有檢查" in w for w in r["warnings"]),
    r["warnings"])
chk("source 說得出是 none", r["terrain"]["source"] == "none")
empty = T.Dem(tempfile.mkdtemp())
chk("目錄空的也一樣（available=False）", not empty.available)
far = P.check_terrain(
    [{"seq": 1, "lat": 40.5, "lon": 40.5, "alt": 5, "command": 16, "frame": 3}],
    {"lat": 40.5, "lon": 40.5}, dem=T.Dem(TMP))
chk("圖磚缺就說缺哪一塊", any("N40E040" in w for w in far["warnings"]),
    far["warnings"])

print("\n── 5. 絕對誤差會相減消掉（所以起飛點也用 DEM 是對的）────────")
write_tile("N02E000.hgt", lambda r, c: ground(r, c) + 50)   # 整塊抬 50 m
d3 = T.Dem(TMP)
shifted = [{**w, "lat": w["lat"] + 2} for w in wps]
r3 = P.check_terrain(shifted, {"lat": 2.5, "lon": 0.5}, dem=d3)
chk("整塊圖磚抬 50 m，最小離地不變", r3["terrain"]["min_clearance_m"] == -3.0,
    r3["terrain"])
chk("起飛點 AMSL 跟著變（150 而不是 100）",
    r3["terrain"]["home_amsl_m"] == 150.0, r3["terrain"]["home_amsl_m"])

print("\n── 6. 降落點不該被報成「撞地」 ────────────────────────")
land = [
    {"seq": 0, "lat": 0.5, "lon": 0.5, "alt": 5, "command": 22, "frame": 3},
    {"seq": 1, "lat": 0.5004, "lon": 0.5, "alt": 0, "command": 21, "frame": 3},
]
r = P.check_terrain(land, home, dem=dem)
chk("降落項高度 0 不算撞地", not r["problems"], r["problems"])
land2 = [land[0], {**land[1], "lat": 0.50111}]     # 降落點落在土堤上
r = P.check_terrain(land2, home, dem=dem)
chk("**但平飛到土堤上的降落點還是要擋**", len(r["problems"]) == 1,
    r["problems"])

print("\n── 7. frame 的語意：10 不歸這裡管、0 是 AMSL ───────────")
t10 = [{"seq": 1, "lat": 0.50111, "lon": 0.5, "alt": 5, "command": 16,
        "frame": 10}]
r = P.check_terrain(t10, home, dem=dem)
chk("frame 10（跟地形）跳過、記在 skipped",
    not r["problems"] and r["terrain"]["skipped"] == 1, r["terrain"])
amsl = [{"seq": 1, "lat": 0.50111, "lon": 0.5, "alt": 105, "command": 16,
         "frame": 0}]
r = P.check_terrain(amsl, home, dem=dem)
chk("frame 0 的 105 是 AMSL，在 108 m 的土堤上就是 −3",
    r["terrain"]["min_clearance_m"] == -3.0, r["terrain"])

print("\n── 8. 沒被地形吃掉的低餘裕要說清楚不是地形造成的 ──────────")
low = [{"seq": 1, "lat": 0.5, "lon": 0.5, "alt": 1, "command": 16, "frame": 3}]
r = P.check_terrain(low, home, dem=dem)
chk("1 m 航線在平地：warning 而非 problem", not r["problems"])
chk("而且說明「不是地形造成的」",
    any("不是地形造成的" in w for w in r["warnings"]), r["warnings"])

print("\n" + ("全部通過" if ok else "**有未通過項目**"))
sys.exit(0 if ok else 1)
