/** 013-A 編隊預覽試算（doc/group-missions-design.md §2）。
 *
 * unified 展開與後端演算法一致：每台航點高度 += layer_index × 層距
 * （layer_index＝targetIds 內順序）。這裡是**前端試算**——013-B 串上
 * /api/groups 後，預覽改讀後端 materialized assignments（單一真相），
 * 本模組只服務規劃階段的即時回饋。
 * 誠實預覽原則（ui-spec §2.5）：畫「實際會飛的 N 條」，原始路徑退灰虛線。
 */
import { rgba, type RouteRun } from "@/lib/deckRoute";

export interface Wp { lat: number; lon: number; alt?: number | null }

const withAlpha = (c: [number, number, number, number], a: number):
  [number, number, number, number] => [c[0], c[1], c[2], a];

/** unified：一條 base 路徑 → N 條高度分層預覽（識別色半透明細帶） */
export function unifiedPreview(
  wps: Wp[],
  targets: { id: string; color: string }[],
  spacing: number,
): RouteRun[] {
  const pts = wps.filter((w) => w.lat && w.lon);
  if (pts.length < 2) return [];
  return targets.map((t, layer) => ({
    sid: t.id,
    path: pts.map((w) =>
      [w.lon, w.lat, (w.alt ?? 10) + layer * spacing] as [number, number, number]),
    color: withAlpha(rgba(t.color), 190),
    width: 1.2,
  }));
}

/** separate：逐台指派任務 → 各自識別色預覽 */
export function separatePreview(
  perDrone: { id: string; color: string; wps: Wp[] }[],
): RouteRun[] {
  return perDrone.flatMap((d) => {
    const pts = d.wps.filter((w) => w.lat && w.lon);
    if (pts.length < 2) return [];
    return [{
      sid: d.id,
      path: pts.map((w) => [w.lon, w.lat, w.alt ?? 10] as [number, number, number]),
      color: withAlpha(rgba(d.color), 190),
      width: 1.2,
    }];
  });
}

/** 原始（未展開）路徑：灰色細帶參考——絕不畫一條線假裝大家都飛它 */
export function basePreview(wps: Wp[]): RouteRun[] {
  const pts = wps.filter((w) => w.lat && w.lon);
  if (pts.length < 2) return [];
  return [{
    path: pts.map((w) => [w.lon, w.lat, w.alt ?? 10] as [number, number, number]),
    color: [143, 139, 128, 110],   // muted 半透明
    width: 0.8,
  }];
}
