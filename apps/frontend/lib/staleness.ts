/** 資料年齡 → 顯示規則（A 層）。
 *
 * **問題不是「沒有資料」，是有一份看起來很正常的假資料。** 2026-08-26 實測：
 * 畫面顯示 `armed=true / LAND / 高度 1.07m`，那是**兩個半小時前**的殘影，
 * 而畫面上唯一的線索是角落一個 `connected:false` 的小標記。
 *
 * 判準：**看的人不必先注意到某個角落的紅點，才知道自己在看舊資料。**
 * 所以年齡要作用在**數值本身**上，不是只在旁邊加個標籤。
 *
 * 三級：
 *   - `live`（< 2 秒）：正常顯示
 *   - `stale`（2–10 秒）：數值變淡，旁邊標「N 秒前」
 *   - `old`（> 10 秒）：**數值換成「—」**，只留一句明確過去式的
 *     「最後已知：… （N 分鐘前）」
 *
 * 為什麼 old 要把數值拿掉而不是只變灰：一個灰色的「1.07 m」還是一個高度，
 * 人讀到的仍然是「它在 1 公尺」。**要讓人讀不到那個數字**，才會去看時間。
 */
export type StaleLevel = "live" | "stale" | "old" | "never";

export const STALE_S = 2;
export const OLD_S = 10;

export function staleLevel(ageS: number | null | undefined): StaleLevel {
  if (ageS == null) return "never";
  if (ageS > OLD_S) return "old";
  if (ageS > STALE_S) return "stale";
  return "live";
}

/** 人看的年齡字串。**超過一分鐘就用分鐘**——「185 秒前」要心算才知道多久。 */
export function ageText(ageS: number | null | undefined): string {
  if (ageS == null) return "從未收到";
  if (ageS < 60) return `${Math.round(ageS)} 秒前`;
  if (ageS < 3600) return `${Math.round(ageS / 60)} 分鐘前`;
  return `${(ageS / 3600).toFixed(1)} 小時前`;
}

/** 依年齡決定要不要顯示這個數值。`old` 一律回 null（呼叫端顯示「—」）。 */
export function freshOnly<T>(v: T, ageS: number | null | undefined): T | null {
  return staleLevel(ageS) === "old" || staleLevel(ageS) === "never" ? null : v;
}

/** 舊資料的視覺弱化。`old` 不該只靠這個——數值本身要被拿掉。 */
export function staleStyle(ageS: number | null | undefined): React.CSSProperties | undefined {
  const lv = staleLevel(ageS);
  if (lv === "stale") return { opacity: 0.55 };
  if (lv === "old" || lv === "never") return { opacity: 0.35 };
  return undefined;
}
