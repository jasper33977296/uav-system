/** 飛控說的「為什麼不能解鎖」→ **怎麼解**。
 *
 * ## 為什麼需要這張表
 *
 * `PreArm:` / `Arm:` 那幾句是飛控用英文講的，而且講的是**現象**不是**動作**：
 * 「Throttle (RC3) is not neutral」說得出哪裡不對，說不出要做什麼。操作員
 * 站在場邊，需要的是後者。
 *
 * 2026-09-02 現場：同一趟裡連續遇到三種擋法（油門桿位置、GUIDED 不接受
 * 搖桿解鎖、電池 failsafe），每一種的處置完全不同，而畫面上三者長得一樣。
 *
 * ## 三條紀律
 *
 * 1. **原文不刪。** 處置是**加**在原文後面的一行，不是取代它。翻譯會失真，
 *    而查韌體行為時要的是飛控原本那句話。
 * 2. **認不得就不要編。** 比不到樣式就回 `null`，UI 照原文顯示並說明
 *    「這一項還沒有對應的處置」。**看起來合理但其實錯誤的指示，
 *    比沒有指示危險得多**——這條規矩本專案在 PX4 事件翻譯上已經立過一次
 *    （版本不符時寧可不給 text）。
 * 3. **一句話講完**（2026-09-07 使用者回報）。未就緒時螢幕上同時站著四五條
 *    原因，每條再掛兩行處置＝一整片沒有人會讀的字；而站在場邊的人要的是
 *    「換一顆充飽的電池」那幾個字，不是 `BATT_FS_*_ACT` 的設定學。
 *    所以處置壓成**一行、無標點強調記號**（畫面不解析 Markdown，`**` 會
 *    原樣顯示成星號）；補充說明搬進 `note`，由 UI 掛成 tooltip——
 *    **知識不丟，但不佔版面**。
 */

/** 樣式 → [處置（一行）, 補充（可略）]。順序有意義：先比對具體的，再籠統的。 */
const FIXES: [RegExp, string, string?][] = [
  // ── 遙控器與桿位 ────────────────────────────────────────────
  [/throttle.*(not neutral|too high|below failsafe)/i,
    "把油門桿推到最底再解鎖",
    "解鎖瞬間馬達會照當下的油門位置轉。"],
  [/mode not armable/i,
    "改用地面站的「解鎖」按鈕，或把模式撥到 LOITER／STABILIZE",
    "這個模式不接受遙控器搖桿解鎖。"],
  [/rc not found/i,
    "打開遙控器並確認已對頻",
    "飛控完全收不到遙控器訊號。"],
  [/rc.*(not calibrated|calibrating)/i,
    "做一次 RC 校正",
    "遙控器沒有校正過——在地面站或 QGC 做。"],
  [/(roll|pitch|yaw|throttle).*(radio|rc\d).*(min|max|trim)/i,
    "重做一次 RC 校正",
    "這個通道的校正值不合理。"],

  // ── 電池 ────────────────────────────────────────────────────
  [/battery.*failsafe/i,
    "電池電壓低於門檻，換一顆充飽的",
    "這一項在地面會擋解鎖；在天上要 BATT_FS_*_ACT 不是 0 才會有動作。"],
  [/battery/i,
    "檢查電池電壓與接頭",
    "也看一下 BATT_* 的門檻設定是不是你要的。"],

  // ── 鏈路 ────────────────────────────────────────────────────
  [/gcs failsafe/i,
    "飛控收不到地面站心跳，等鏈路恢復",
    "遙測回得來不代表心跳送得過去，那是兩個方向。"],

  // ── 定位與姿態 ──────────────────────────────────────────────
  [/(need position estimate|need 3d fix|gps.*(fix|hdop)|high gps hdop)/i,
    "等 GPS 定位，到室外空曠處",
    "等衛星數上來，通常要幾十秒到幾分鐘。"],
  [/(ahrs|dcm).*(inconsistent|not healthy|bad)/i,
    "把機體放平靜置別動，等姿態收斂"],
  [/ekf/i,
    "機體靜置別動，等 EKF 收斂",
    "一直不收斂就查震動與羅盤。"],
  [/compass/i,
    "做一次羅盤校正",
    "並確認附近沒有大型金屬或通電的東西。"],
  [/(gyros|accels|baro).*(not healthy|inconsistent|calibrat)/i,
    "把機體放平靜置，重新開機一次",
    "加速度計要在靜止時歸零。"],

  // ── 其他 ────────────────────────────────────────────────────
  [/safety switch/i,
    "按一下飛控上的安全開關",
    "通常是那顆紅色按鈕，按住到燈恆亮。"],
  [/logging/i,
    "SD 卡有問題：確認插好、沒滿、格式正確"],
  [/fence/i,
    "圍欄檢查沒過：確認目前位置在圍欄內"],
];

function match(reason: string): [RegExp, string, string?] | null {
  if (!reason) return null;
  for (const row of FIXES) {
    if (row[0].test(reason)) return row;
  }
  return null;
}

/** 這一條原因該怎麼解，**一行**。認不得回 `null`——不要編一個看起來合理的答案。 */
export function armFix(reason: string): string | null {
  return match(reason)?.[1] ?? null;
}

/** 那一行之外的補充（多半是「但是⋯」）。UI 掛 tooltip，不佔版面；
 * 沒有補充就回 `null`。 */
export function armNote(reason: string): string | null {
  return match(reason)?.[2] ?? null;
}
