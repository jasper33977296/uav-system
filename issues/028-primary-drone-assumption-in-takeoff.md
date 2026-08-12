# 028 起飛序列讀「主機」高度：非主機起飛判斷全錯（同一假設漏在三處）

- 嚴重度：**high**（有飛安方向）
- 狀態：**closed**（2026-08-12 修復並實飛驗證）
- 位置：`apps/command/app/main.py`（`mission_fly`／`_do_takeoff`）、`apps/command/app/group_exec.py`
- 發現：前端 A/A 驗收（2026-08-12），uav-s2 起飛連兩次「失敗」

## 症狀

`POST /api/start`（委派 `mission/fly`）飛 uav-s2 時連兩次回：

    起飛後未達目標高度（目前 -0.04 m / 目標 10.0 m）

但**上傳與 arm 都成功，機也真的飛起來了**；改用手動序列（takeoff → 等高度 →
mission/start）就正常。

## 根因

`mission_fly` 等高度那段讀 backend 的 `/api/live`，而**那個端點只回主機**
（`live.telemetry_dict()`）。飛非主機時，這個判斷跟目標機完全無關。

那個 `-0.04 m` 是**停在地面的主機 sim-uav-1**。手動序列之所以正常，是因為那條
路徑的高度判斷是操作員自己在看，不是程式。

## 同一個假設漏在三處

| 位置 | 錯在哪 | 為什麼沒被發現 |
|---|---|---|
| `mission_fly` 等高度 | 讀主機的 `alt_rel` 判斷目標機到沒到 | 只在飛非主機時才錯；單機測試永遠正確 |
| `_do_takeoff` 的 `ground_amsl` | 拿**主機**地面海拔算 PX4 的絕對起飛高度（param7=AMSL） | **SITL 全機同址、差值為零**。真機分散部署才會差一整個地面高差 |
| `group_exec._ground_amsl` | 全隊共用一個由主機推算的地面海拔 | 原註解已自承是骨架代理（「真機需 per-sysid alt」），但沒人回頭收尾 |

## 反方向才是飛安洞

前端踩到的是**良性方向**：誤判失敗、拒絕啟動任務（機停在懸停，安全）。

**反過來才要命**：主機在空中、目標機沒起來時，這個檢查會**通過**，於是把一台
還在地面的機切進 `AUTO.MISSION`。而這個檢查存在的唯一理由，就是 2026-08-11 那次
「地面直接 MISSION_START 在實機上會失敗」的教訓——**bug 把設立這道檢查的理由
完全架空了**。

> 「用 A 的狀態判斷 B」這類 bug 在多機系統裡通常兩個方向都有，只有一個方向會被
> 踩到，而被踩到的那個往往是安全的那半。

## 教訓：學到一課要問「這課適用的其他地方修了沒」

群組執行器**早就做對了**——它用 `router.drones[sysid]["alt_rel"]`，註解甚至寫著：

> per-sysid 相對高度——群組執行器「等到達高度才切 MISSION」的依據
> （issue 013-B；**單機 mission_fly 教訓的多機版，不靠 backend**）

也就是說，做群組時已經學到「不能靠 backend 的主機視角」，加了 per-sysid 追蹤，
**卻沒有回頭修那個被它取經的單機路徑**。知識只長在新程式碼裡，舊路徑原地不動。

這與 026 B0 的 `EKF_ALIAS_SAFE_BITS`、與「條件變了誰會發現」是同一族問題：
**修正一個認知之後，要主動去找同一個認知的其他寄生處。**

## 可觀測性缺口是共犯

這個 bug 難發現，有一部分原因是**逐機高度哪裡都看不到**：`/healthz` 的逐機快照
沒有 `alt_rel`／`alt_msl`，前端也沒顯示。看不見的數字，沒有人能發現它錯了。

修法一併補上：`/healthz` 逐機快照加 `alt_rel`／`alt_msl`（唯讀診斷欄位）。
（前端同日也補了選中機經緯度＋相對高度顯示在 IMU 卡末，同一個道理。）

## 修法

三處統一改讀 router 的 per-sysid 紀錄（與群組執行器同源）：

- `mav.py`：`GLOBAL_POSITION_INT` 除了 `alt_rel` 再記 `alt_msl`
- `main.py`：`mission_fly` 等高度、`_do_takeoff` 的 `ground_amsl` 都改逐機
- `group_exec.py`：`_ground_amsl()` → `_ground_amsl_for(sysid)`，**逐台**算
- `GroupExecutor` 不再注入 `live_fn`——留著等於留一個「用主機資料」的現成陷阱，
  而它的註解也已經失效

## 驗證（實飛，2026-08-12）

`POST /api/start` sysid=3（非主機）跑完整序列，同時每 2 秒對帳主機與目標機高度：

    18:29:41  主機s1_alt= -0.05 armed=False | 目標s3_alt=  0.09 armed=False
    18:29:47  主機s1_alt= -0.04 armed=False | 目標s3_alt=  0.64 armed=True
    18:29:49  主機s1_alt= -0.01 armed=False | 目標s3_alt=  2.81 armed=True
    18:29:51  主機s1_alt=  0.00 armed=False | 目標s3_alt=  5.53 armed=True
    18:29:53  主機s1_alt= -0.01 armed=False | 目標s3_alt=  8.29 armed=True
    18:29:55  主機s1_alt= -0.03 armed=False | 目標s3_alt=  9.44 armed=True  ← 已切 MISSION

結果 `ok: true`、`alt_reached: {alt_rel: 8.186}`、`mission.mode_engaged: true`。

**主機全程停在地面（-0.05 ~ 0.00、disarmed）**——修復前這個序列會讀到那個
`-0.02` 然後逾時失敗，正是前端的症狀。飛完 RTL 降落上鎖，機隊復原。

附帶確認（非本案）：sysid 2 當時 arm 被拒是**真的**——PX4 回
`Preflight Fail: Battery unhealthy`，`prearm_ok=False`。arm 被拒即中止起飛的
保護有正常作動。

## ⚠️ 這個修正的 commit 標題不是它自己的

修正的程式碼**落在 `8bc1e53`**——一個標題寫「底圖與機體圖示（ui-spec §2.4b）」
的前端 commit 裡。原因：多個 session 共用同一個工作目錄，我把檔案 `git add` 之後
還沒 commit，另一個 session 先 commit 了，**暫存區的內容被一起帶走**。

記在這裡是因為：日後有人用 `git log` 找「起飛高度讀錯機」的修正，**在那個標題底下
永遠找不到**。要看改動請直接 `git show 8bc1e53 -- apps/command/`。

教訓（已回報 PM）：在共用工作目錄裡，**暫存區本身就是共用狀態**。`git add` 與
`git commit` 之間的空窗期，任何 session 的 commit 都會把你的暫存內容一起提交。
處理方式：add 完立刻 commit，不要留空窗。

## 相關

- 013-B 群組任務（做對的那一半）
- 026 驅動層抽象——**若晚一步就會把這個分岔一起抽進驅動層**
