#!/usr/bin/env bash
# 前端產物版本指紋（2026-08-13 事故調查用）。**唯讀、離線。**
#
#   ./scripts/frontend-fingerprint.sh <chunks 目錄>
#   ./scripts/frontend-fingerprint.sh --container uav-frontend
#
# ## 為什麼需要它
#
# 部署映像**沒有任何版本標記**（Dockerfile 是 `COPY . . && npm run build`，
# 無 git sha、無 LABEL），所以「事故當下跑的是哪個 commit」無法從產物直接讀。
# 但 UI 字串會隨 commit 出現/消失，**產物裡有哪些字串就把版本夾在一個區間內**。
# 這查的是**實際在跑的產物**，不是 repo 的 HEAD——容器可能跑好幾天前的映像。
#
# ## 三個誠實條款（每一條都對應一種會產生「看似合理但錯」的結論的情況）
#
# 1. **交集為空 ≠ 某個區間**。夾區間演算法**永遠給得出區間**，包括在「產物
#    對應主線某一 commit」這個前提不成立時（dirty tree／分支／手改產物）。
#    字串組合可能在主線上從未同時存在，而演算法照樣吐一個區間且**毫無跡象**。
#    所以本腳本把每列翻成時間約束後**取交集**，交集為空就報
#    「不對應本 repo 任何單一 commit」，**不報區間**。
# 2. **早於最早標記 ≠ 等於最早標記**。全部字串都不在時，只能說「早於 T0」。
# 3. **判準衝突的正確輸出是「我不知道」**，不是多數決、也不是選比較可信的
#    那個。字串組說已修、位移探針說未修 → 那不是要挑一個信，**那是前提破了
#    的直接證據**（與「unknown 不得被當成 match」同一條）。
#
# 交集為空或判準衝突時，別再談版本——**改直接量行為**（後端回 5xx 時事件流
# 顯示什麼、有啟用任務時機體圖示是否被計畫路徑蓋住）。那些不需要先知道版本。
# ## 使用順序（§0.2f 上線後）
#
# **先問端點，指紋法退為交叉驗證**：
#     curl -s http://<host>:33000/api/version    # {"sha":…,"unknown":…,"dirty":…}
# 端點有 sha ⇒ 版本直接確定，本腳本只用來**交叉驗證**（兩者衝突＝有人動過
# 產物或端點，那本身就是發現）。端點回 unknown:true 或根本沒有這個路由
# ⇒ 產物早於 §0.2f，只能用本腳本考古。
set -uo pipefail

# ── 標記表：字串｜首次出現的 commit｜時間（本 repo 提交時間，+0800）──────
# 出現＝版本 ≥ 該時間；不出現＝版本 < 該時間。
#
# **標記一律只用中日韓字元**：minifier 會把非 ASCII 符號轉成跳脫序列——
# 實測 `© 內政部國土測繪中心` 在產物裡是 `"\xa9 內政部國土測繪中心"`，
# 於是原字串**永遠找不到** → 一個假的 [無] → 假的「交集為空」。
# **一個測不到的標記會偽裝成一個真的結論**，而且看起來完全合理。
MARKERS=(
  "尚無足夠遙測|b5e3bf2|2026-08-12 16:24"
  "內政部國土測繪中心|8bc1e53|2026-08-12 18:33"
  "定點停懸|6f40672|2026-08-12 22:45"
  "未能確認字典版本與機上韌體相符|7b1b5f1|2026-08-13 16:11"
  "無法解讀的訊息|cda4a4f|2026-08-13 17:06"
  "這趟沒有訊號量測|7db9e39|2026-08-13 17:19"
  "無法取得事件|9ae9dcc|2026-08-13 17:27"
  "無法連線到|947f1f8|2026-08-13 17:33"
)
# 覆蓋缺陷（計畫路徑蓋住 deck 全層）的修正時間——位移探針的分界
FIX_LAYER_ORDER="2026-08-13 13:08:47"

SRC="${1:-}"
[ -z "$SRC" ] && { echo "用法：$0 <chunks 目錄> | --container <名稱>"; exit 2; }

TMP=""
if [ "$SRC" = "--container" ]; then
  C="${2:-}"; [ -z "$C" ] && { echo "缺容器名"; exit 2; }
  TMP="$(mktemp -d)"
  # 唯讀複製，不進容器做任何修改
  docker cp "$C:/app/.next/static/chunks" "$TMP/" 2>/dev/null \
    || { echo "無法從容器 $C 取出 chunks（可能非部署形映像或路徑不同）"; exit 3; }
  DIR="$TMP/chunks"
else
  DIR="$SRC"
fi
[ -d "$DIR" ] || { echo "目錄不存在：$DIR"; exit 3; }

echo "== 前端產物版本指紋 =="
echo "來源：$DIR"
echo
# 守門：標記若含非 CJK 字元，可能被 minifier 跳脫而永遠找不到
for m in "${MARKERS[@]}"; do
  s="${m%%|*}"
  if printf '%s' "$s" | LC_ALL=C grep -qP '[^\x{4e00}-\x{9fff}]' 2>/dev/null; then
    echo "  ⚠ 標記「$s」含非中文字元，可能被 minifier 跳脫（見檔頭）"
  fi
done

echo "-- 字串標記 --"
LOWER=""; LOWER_TAG=""; UPPER=""; UPPER_TAG=""; ANY_PRESENT=0
for m in "${MARKERS[@]}"; do
  s="${m%%|*}"; rest="${m#*|}"; sha="${rest%%|*}"; t="${rest#*|}"
  if grep -rqF -- "$s" "$DIR" 2>/dev/null; then
    printf '  [有] %-30s → 版本 ≥ %s (%s)\n' "$s" "$t" "$sha"
    ANY_PRESENT=1
    if [ -z "$LOWER" ] || [[ "$t" > "$LOWER" ]]; then LOWER="$t"; LOWER_TAG="$sha"; fi
  else
    printf '  [無] %-30s → 版本 < %s (%s)\n' "$s" "$t" "$sha"
    if [ -z "$UPPER" ] || [[ "$t" < "$UPPER" ]]; then UPPER="$t"; UPPER_TAG="$sha"; fi
  fi
done

echo
echo "-- 位移探針（覆蓋缺陷：計畫路徑蓋住軌跡與機體圖示）--"
# 修正把 plan 圖層移到 overlay 建立之前；minify 保留語句順序。
# 兩個字串必須在**同一個 chunk**才算有效樣本（否則無從比較先後）。
#
# **只驗即時頁**（含 `missions/active` 的 chunk＝MapView）。理由：
#   - 比對回放頁（/replay-mission）**也**有自己的地圖與 plan 圖層，但它
#     不在 b8e5933 的修正範圍內，順序本來就不同——**把它算進來會得到
#     「同時有已修與未修」的假衝突**（實測發生過，差點報成前提破裂）。
#   - 即時頁正是事故關心的畫面：操作者在任務執行中看不看得到機。
FIXED=0; UNFIXED=0; INVALID=0; SKIPPED=0
while IFS= read -r f; do
  if ! grep -qF "missions/active" "$f" 2>/dev/null; then
    SKIPPED=$((SKIPPED+1)); continue      # 不是即時頁的 chunk，不適用
  fi
  a=$(grep -abo "plan3d" "$f" 2>/dev/null | head -1 | cut -d: -f1)
  b=$(grep -abo "interleaved" "$f" 2>/dev/null | head -1 | cut -d: -f1)
  if [ -z "$a" ] || [ -z "$b" ]; then
    [ -n "$a$b" ] && { INVALID=$((INVALID+1)); printf '  [無效樣本] %s（只含其一，不可比較）\n' "$(basename "$f")"; }
    continue
  fi
  if [ "$a" -lt "$b" ]; then
    FIXED=$((FIXED+1)); printf '  [已修] %-46s plan3d %s < interleaved %s\n' "$(basename "$f")" "$a" "$b"
  else
    UNFIXED=$((UNFIXED+1)); printf '  [未修] %-46s plan3d %s > interleaved %s\n' "$(basename "$f")" "$a" "$b"
  fi
done < <(grep -rl "plan3d" "$DIR" 2>/dev/null)
[ $((FIXED+UNFIXED)) -eq 0 ] && echo "  （無有效樣本——找不到即時頁 chunk）"
[ "$SKIPPED" -gt 0 ] && echo "  （略過 $SKIPPED 個含 plan3d 但非即時頁的 chunk：不在本修正範圍）"

echo
echo "-- 判定 --"
VERDICT_VER=""
if [ -n "$LOWER" ] && [ -n "$UPPER" ] && [[ "$LOWER" > "$UPPER" ]]; then
  echo "  ⚠ **交集為空**：字串組合在本 repo 主線上從未同時存在"
  echo "     （下界 $LOWER/$LOWER_TAG 晚於上界 $UPPER/$UPPER_TAG）"
  echo "     → 結論是「**不對應本 repo 任何單一 commit**」，不是某個區間。"
  echo "     可能是 dirty tree／分支／手改產物。**別再談版本，改直接量行為。**"
  VERDICT_VER="none"
elif [ "$ANY_PRESENT" -eq 0 ]; then
  echo "  版本**早於 ${MARKERS[0]##*|}**（最早標記亦不存在）——不可再細分，"
  echo "  且**不得**報成該時間點本身。"
  VERDICT_VER="older"
else
  echo "  版本區間：≥ $LOWER${UPPER:+，< $UPPER}"
  VERDICT_VER="$LOWER"
fi

if [ "$FIXED" -gt 0 ] && [ "$UNFIXED" -gt 0 ]; then
  echo "  ⚠ 位移探針**內部不一致**（同時有已修與未修的 chunk）→ 前提破了，不採信。"
elif [ "$VERDICT_VER" != "none" ] && [ "$VERDICT_VER" != "older" ] && [ $((FIXED+UNFIXED)) -gt 0 ]; then
  # 交叉檢查：字串下界與位移探針必須指向同一側，否則是前提破了的直接證據
  if [ "$FIXED" -gt 0 ] && [[ "$LOWER" < "$FIX_LAYER_ORDER" ]] && [ -n "$UPPER" ] \
     && [[ "$UPPER" < "$FIX_LAYER_ORDER" ]]; then
    echo "  ⚠ **判準衝突**：位移說已修（≥$FIX_LAYER_ORDER），字串說 < $UPPER。"
    echo "     正確輸出是「**我不知道**」，不是選一個信。改直接量行為。"
  elif [ "$UNFIXED" -gt 0 ] && [[ "$LOWER" > "$FIX_LAYER_ORDER" ]]; then
    echo "  ⚠ **判準衝突**：位移說未修，字串說版本 ≥ $LOWER（晚於修正）。"
    echo "     正確輸出是「**我不知道**」。改直接量行為。"
  else
    [ "$FIXED" -gt 0 ] && echo "  覆蓋缺陷：**已修**（與字串區間一致）"
    [ "$UNFIXED" -gt 0 ] && echo "  覆蓋缺陷：**存在於此產物**——有啟用任務時，計畫路徑會蓋住實測軌跡、機體圖示與標籤（與字串區間一致）"
  fi
fi

[ -n "$TMP" ] && rm -rf "$TMP"
exit 0
