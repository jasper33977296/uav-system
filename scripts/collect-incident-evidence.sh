#!/usr/bin/env bash
# 事故證據收集包（2026-08-13 建立）。
#
# 用途：在**部署主機**上跑一次，把調查需要的東西收進單一目錄。
#
#   sudo ./scripts/collect-incident-evidence.sh [輸出目錄]
#
# ## 三個設計前提
#
# 1. **唯讀**：本腳本不修改、不刪除、不重啟任何東西。事故現場只能複製，不能整理。
# 2. **離線**：不連外網、不拉映像。部署現場未必有網路。
# 3. **誠實**：收不到的東西**明列在 MISSING.txt**，不靜默跳過——
#    「沒有這項證據」與「這項證據不存在」是兩回事，調查時差很多。
#
# ## 為什麼要有這個腳本而不是口頭清單
#
# 事故當下人在現場、時間壓力大，而證據**會隨時間消失**（容器日誌滾動、
# tlog 輪替、有人為了「先讓系統恢復」而重啟服務）。一支能照跑的腳本，
# 比一頁要人臨場判斷的清單可靠。
set -uo pipefail

# ── 來源宣告（UI/UX 2026-08-13 要求）─────────────────────────────
# **為什麼這是必要的而不是禮貌**：在開發機上跑這支腳本，位移探針必然輸出
# 「已修」——因為開發機的 HEAD 含修正。那行字**長得跟一份能洗清待查缺陷的
# 結論一模一樣**。一份自測輸出若混進證據目錄，日後讀的人會把「腳本能跑」
# 讀成「事故環境沒這個問題」。
#
# 所以每份輸出第一行都標來源，**且主機用途不得留空**：未宣告就明寫未宣告。
SELFTEST=0
for a in "$@"; do [ "$a" = "--selftest" ] && SELFTEST=1; done
ARGS=(); for a in "$@"; do [ "$a" != "--selftest" ] && ARGS+=("$a"); done
if [ "$SELFTEST" = "1" ]; then
  OUT="${ARGS[0]:-selftest-$(date +%Y%m%d-%H%M%S)}"
else
  OUT="${ARGS[0]:-incident-$(date +%Y%m%d-%H%M%S)}"
fi
mkdir -p "$OUT" || { echo "無法建立輸出目錄 $OUT"; exit 1; }
MISS="$OUT/MISSING.txt"; : > "$MISS"
note_missing() { echo "- $*" >> "$MISS"; echo "  ⚠ 收不到：$*"; }

_FE=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -m1 uav-frontend)
_CMD=$(docker inspect "$_FE" --format '{{json .Config.Cmd}}' 2>/dev/null)
case "$_CMD" in
  *"next dev"*|*"run dev"*) _MODE="next dev（開發模式）";;
  *"next start"*|*"run start"*) _MODE="production build";;
  *) _MODE="不可得";;
esac
_HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo "不可得（非 git 工作目錄）")
if [ "$SELFTEST" = "1" ]; then
  PROV="[來源] 主機=$(hostname)  用途=**自測，非事故環境**  模式=$_MODE  git HEAD=$_HEAD"
  PROV2="⚠ 本目錄是腳本自測輸出，**不得當成事故證據**。其中任何「已修/正常」的結論"
  PROV3="   反映的是本機狀態，與事故環境無關。"
else
  PROV="[來源] 主機=$(hostname)  用途=事故環境（**未宣告為自測**）  模式=$_MODE  git HEAD=$_HEAD"
  PROV2="若這其實是開發機自測，請改用 --selftest 重跑——否則本目錄會被當成事故現場證據。"
  PROV3=""
fi
stamp() { { echo "$PROV"; echo "$PROV2"; [ -n "$PROV3" ] && echo "$PROV3"; echo "----"; cat "$1"; } > "$1.tmp" && mv "$1.tmp" "$1"; }

echo "事故證據收集 → $OUT"
echo "$PROV"
echo "（唯讀：本腳本不會改動任何服務或資料）"

# ── 0. 時間基準（**最容易搞錯的一項**）────────────────────────────
# 調查時 DB 用 UTC、主機可能是本地時區、容器又可能不同——不記下來，
# 後面每個時間戳都要重新猜。（本專案 2026-08-13 就因此差點誤判時間軸。）
{
  echo "收集時間（主機本地）: $(date '+%Y-%m-%d %H:%M:%S %Z %z')"
  echo "收集時間（UTC）     : $(date -u '+%Y-%m-%d %H:%M:%S')"
  echo "主機時區            : $(cat /etc/timezone 2>/dev/null || readlink -f /etc/localtime)"
  echo "uptime              : $(uptime -p 2>/dev/null)"
  echo "主機名              : $(hostname)"
} > "$OUT/00-time-and-host.txt" 2>&1

# ── 1. 部署版本（(a) 項）──────────────────────────────────────────
{
  echo "== git =="
  git rev-parse HEAD 2>/dev/null || echo "(不是 git 工作目錄)"
  git log -1 --format='%H%n%ad%n%s' --date=iso 2>/dev/null
  echo; echo "== 工作區是否乾淨（有無未提交改動＝跑的碼可能不等於該 commit）=="
  git status --porcelain 2>/dev/null | head -50
  echo; echo "== 映像 =="
  docker images --format '{{.Repository}}:{{.Tag}}  {{.ID}}  built={{.CreatedAt}}' 2>/dev/null | grep -i uav
} > "$OUT/01-version.txt" 2>&1
[ -s "$OUT/01-version.txt" ] || note_missing "部署版本資訊（git／docker images 都取不到）"

# ── 2. 容器狀態與啟動時間（(d) 項）────────────────────────────────
# **重點：command 服務有沒有在飛行時段重啟。** 我方 1Hz GCS 心跳是機上
# datalink-loss failsafe 的觸發源——服務一斷，任務就會暫停（切 Hold）。
{
  echo "== docker ps -a =="
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null
  echo; echo "== 各容器 StartedAt（UTC）／FinishedAt／重啟次數 =="
  for c in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    docker inspect "$c" --format '{{.Name}}  started={{.State.StartedAt}}  finished={{.State.FinishedAt}}  restarts={{.RestartCount}}  running={{.State.Running}}' 2>/dev/null
  done
} > "$OUT/02-containers.txt" 2>&1

# ── 3. 容器日誌（(d) 項）──────────────────────────────────────────
mkdir -p "$OUT/03-logs"
for c in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
  docker logs --timestamps "$c" > "$OUT/03-logs/$c.log" 2>&1 || note_missing "容器日誌 $c"
done
# 系統層：服務重啟／OOM／網路事件也可能是「任務停下來」的成因
journalctl --since "24 hours ago" --no-pager > "$OUT/03-logs/_journal-24h.txt" 2>/dev/null \
  || note_missing "journalctl（可能需要 root，或系統不用 systemd）"

# ── 4. 資料庫（(b) 項）────────────────────────────────────────────
DB_C=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -m1 -E 'uav-db|postgres')
if [ -n "$DB_C" ]; then
  docker exec "$DB_C" pg_dump -U uav -d uav > "$OUT/04-db-full.sql" 2>/dev/null \
    && echo "  DB dump: $(du -h "$OUT/04-db-full.sql" | cut -f1)" \
    || note_missing "DB dump（pg_dump 失敗——確認使用者/資料庫名）"
  # 摘要：不必等分析就能看的關鍵表
  docker exec "$DB_C" psql -U uav -d uav -c "\
    SELECT id, drone_id, started_at, ended_at, mission_name, origin \
    FROM flight_sessions ORDER BY started_at DESC LIMIT 30;" \
    > "$OUT/04-recent-sessions.txt" 2>&1
  docker exec "$DB_C" psql -U uav -d uav -c "\
    SELECT time, sysid, action, result, client, left(detail,120) \
    FROM command_log ORDER BY time DESC LIMIT 200;" \
    > "$OUT/04-recent-commands.txt" 2>&1
  docker exec "$DB_C" psql -U uav -d uav -c "\
    SELECT time, severity, type, left(detail::text,200) \
    FROM events ORDER BY time DESC LIMIT 300;" \
    > "$OUT/04-recent-events.txt" 2>&1
else
  note_missing "資料庫容器（找不到 uav-db／postgres）"
fi

# ── 5. tlog 原始層（(c) 項）───────────────────────────────────────
# **不複製整包**（可能數 GB）：先列清單，讓調查者挑事故時段那幾支。
BE_C=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -m1 uav-backend)
if [ -n "$BE_C" ]; then
  docker exec "$BE_C" sh -c 'ls -la /data/mavcap/ 2>/dev/null' > "$OUT/05-tlog-list.txt" 2>&1 \
    || note_missing "tlog 清單"
  echo "取檔方式（挑事故當天那支）：" >> "$OUT/05-tlog-list.txt"
  echo "  docker cp $BE_C:/data/mavcap/<YYYYMMDD>.tlog ." >> "$OUT/05-tlog-list.txt"
  echo "  ⚠ tlog 可能數 GB；它是**唯一的原始層無損紀錄**，事故調查務必取回。" >> "$OUT/05-tlog-list.txt"
else
  note_missing "backend 容器（拿不到 tlog 清單）"
fi

# ── 6. 版本窗判定：兩個已知缺陷在不在這個部署版本裡 ──────────────
# 這兩個是 2026-08-13 已查明並修復的前端缺陷。**若部署版本早於修復 commit，
# 它們就確定存在於事故環境**（是否構成因果另判——存在不等於致因）。
FIX_LAYER=b8e5933      # 08-13 13:08 計畫路徑蓋住實測軌跡與機體圖示
FIX_EMPTY=9ae9dcc      # 08-13 17:27 HTTP 錯誤被說成「沒有資料」
{
  echo "== 已知缺陷的版本窗判定 =="
  for pair in "$FIX_LAYER|任務執行中，灰色計畫路徑蓋住實測軌跡與機體圖示（操作者看不到機在哪，且畫面不顯得壞）" \
              "$FIX_EMPTY|事件流／清單把載入失敗顯示成「尚無資料」（看起來沒事，其實是沒讀到）"; do
    fix="${pair%%|*}"; desc="${pair#*|}"
    if git merge-base --is-ancestor "$fix" HEAD 2>/dev/null; then
      echo "  ✔ 已含修復 $fix —— 此缺陷**不存在**於本部署：$desc"
    else
      echo "  ✘ **未含修復 $fix —— 此缺陷存在於本部署**：$desc"
    fi
  done
  echo
  echo "注意：以上判定看的是 git HEAD。**若容器跑的是舊映像或有未提交改動，"
  echo "實際行為未必等於 HEAD**——請一併看 01-version.txt 的工作區狀態與映像建立時間。"
} > "$OUT/06-known-defect-windows.txt" 2>&1
cat "$OUT/06-known-defect-windows.txt"

# ── 7. 機上 ulog 取法（(e) 項）——本腳本收不到，只能給指引 ───────
cat > "$OUT/07-onboard-ulog-HOWTO.txt" <<'TXT'
機上 ulog（PX4 SD 卡黑盒子）——**本腳本收不到，必須人工取**

為什麼非取不可：ulog 是「任務為什麼停」與「掉落瞬間」的**權威紀錄**。
地面站只看得到「透過鏈路傳回來的東西」；鏈路一斷，地面就什麼都不知道，
而機上仍在寫 ulog。failsafe 觸發原因、模式切換、姿態與馬達輸出、
掉落前最後幾秒——只有 ulog 有。

取法（擇一）：
  1. QGroundControl → Analyze → Log Download → 選事故時段的 log 下載
  2. 直接拔飛控 SD 卡，複製 /log/<日期>/*.ulg
  3. voxl 平台：ulog 可能在 /data/px4/log/ 或 /var/log/px4/（依版本）

⚠ **飛控重新燒錄可能清掉 SD 卡內容或覆寫既有 log——取回 ulog 之前不要再燒。**

取回後與本目錄一起打包。分析工具：PlotJuggler、Flight Review
（https://logs.px4.io 需上傳，涉外部揭露，內部案件請用離線工具）。
TXT

# ── 8. 給操作者的問題（**證據裡沒有、只有人知道**）─────────────────
cat > "$OUT/08-QUESTIONS-for-operator.txt" <<'TXT'
請操作者回答——這幾題的答案**日誌裡找不到**，而且它們決定整個調查方向。

■ 1. 任務執行中，你在地圖上看到什麼？（**這題能直接判斷部署版本**）
   (A) 沒有任務航線，圖例也沒有「預計任務路徑」那一列
   (B) 看得到灰色航線，但**看不到無人機圖示與實測軌跡**
   (C) 航線、無人機、實測軌跡都看得到
   → (A)/(B) 各對應一個已查明的前端缺陷版本區間；(C) 代表版本較新。
     **(B) 的性質最要命：畫面不顯得壞，但你在任務中看不到機在哪。**

■ 2. 事故大概時間？（到分鐘最好，到小時也可用）
   飛行開始 ______  任務停下來 ______  掉落 ______

■ 3. 「任務停下來」時，畫面與機體各是什麼狀態？
   - 無人機是懸停不動、還是繼續飛但不照航線、還是完全失聯？
   - 地面站當時有沒有顯示失聯／異常？（**若畫面看起來正常，也請照實說**）

■ 4. 那段時間，地面站這邊有人在更新或重啟服務嗎？
   （docker compose up／git pull／重開機／改設定都算。**這題很關鍵**：
     我方 1Hz 心跳是機上 datalink-loss failsafe 的觸發源——心跳一斷，
     PX4 會自己暫停任務切 Hold。時間若重疊，這條鏈就成立。）

■ 5. 「重新燒入」具體是什麼動作？當下機的狀態？
   - 燒韌體（flash）／重啟 voxl-px4 服務／其他：______
   - 執行那一刻，機是**已落地並上鎖**，還是仍 armed／在空中？
     （若仍在空中，掉落是物理必然——飛控停轉、地面站救不了。
       這題不是要歸咎，是要確認因果，才不會去修錯的東西。）

■ 6. 那台機當時是連著地面站，還是只用 QGC 飛？
   （若沒連地面站，我方系統不在迴路上，這批地面站證據就與事故無關。）
TXT

# ── 9. 前端產物指紋（呼叫前端交付的專用腳本）──────────────────
# **不自己編判定邏輯**：判定表與判讀規則是前端／設計師的東西，我這裡再寫一份
# 就會變成第二個權威來源，然後兩份漂移（本專案今天已經因為「兩份表」吃過虧）。
# 本節只負責讓它在部署主機上跑得起來、把輸出收進證據目錄。
#
# 為什麼要有這一節：第 6 節的版本窗判定看 git HEAD，而**跑的東西未必等於
# HEAD**——映像可能是舊的、node_modules volume 可能遮住新裝的套件。
# 2026-08-13 本專案正好踩過：映像重建「成功」，容器實際用的卻是 volume 裡的
# 舊 node_modules。所以要**直接驗執行中的產物**，並與第 6 節互相對質；
# **兩者不一致本身就是重要發現**（部署失配）。
FP_SH="$(dirname "$0")/frontend-fingerprint.sh"
if [ -x "$FP_SH" ]; then
  "$FP_SH" --container uav-frontend > "$OUT/09-frontend-fingerprint.txt" 2>&1 \
    || note_missing "前端產物指紋（容器不存在或非部署形映像）"
  {
    echo
    echo "== 判讀注意（前端交付時特別註明）=="
    echo "1. **位移探針只判即時頁**——刻意縮小到「操作者任務中看不看得到機」，"
    echo "   不是遺漏。含 plan3d 但非即時頁的 chunk 會被略過並標明。"
    echo "0. **⚠ 資料缺口不得冒充機體狀態**：我方 DB／tlog 若在事故時段有整段"
    echo "   缺口，那只證明「我們看不到」，**不證明「機停了」**。缺口期間機體"
    echo "   實際在做什麼，只有機上 ulog 答得出來。判讀時不要把「我們沒有資料」"
    echo "   寫成「機沒有動作」——那是兩件事。"
    echo "2. **「無效樣本」不等於「未修」**——只含單一字串的 chunk 一律報無效，"
    echo "   因為少一個標記可能只是該 chunk 沒用到，不構成任何結論。"
    echo "3. **不要跨機比對樣本數**：dev 與 production build 的 chunk 切分不同，"
    echo "   命中的檔案不只數量不同、集合也不同。一致性只能在同一份產物內要求。"
    echo "4. 三條守門由該腳本自己實作（交集為空不報區間／早於 T0 不報成 T0／"
    echo "   判準衝突報「不確定」不用多數決），並做過鑑別力測試"
    echo "   （人造兩版混合 chunk → 正確拒絕給版本）。"
  } >> "$OUT/09-frontend-fingerprint.txt" 2>/dev/null
  echo "  前端產物指紋 → 09-frontend-fingerprint.txt"
else
  note_missing "frontend-fingerprint.sh（不在 scripts/ 或無執行權限）"
fi

# ── 10. 來源標頭：**每一份輸出的第一行都要說明它從哪裡收的** ──────
# 「收不到要明列」的反面同樣成立：**收到的東西要標明從哪收的，否則它會被
# 當成從『該查的地方』收的**（UI/UX 2026-08-13）。
for f in "$OUT"/*.txt; do
  [ -f "$f" ] && [ "$(basename "$f")" != "MISSING.txt" ] && stamp "$f"
done

# ── 完成 ──────────────────────────────────────────────────────────
if [ -s "$MISS" ]; then
  echo; echo "以下項目未能收集（已記在 MISSING.txt）："; cat "$MISS"
else
  echo "（無遺漏項目）" > "$MISS"
fi
stamp "$MISS"      # 最後才蓋，免得標頭混進上面那份「未能收集」清單
echo
echo "完成。打包：  tar czf ${OUT}.tar.gz $OUT"
echo "⚠ 內含完整 DB dump 與日誌，可能含座標等敏感資訊——傳遞前確認接收方。"
