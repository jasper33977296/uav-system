/** 訊號熱區聚合（ui-spec §6.5 query_signal_map 契約形狀）。
 *
 * 後端共用查詢層就緒前先在客端聚合（選中架次的 track 本來就已載入）——
 * 回傳形狀與契約一致：cells[{中心座標, p10, min, n, session_ids}]，
 * 之後換 fetch 後端只動 loadCells 一處。
 * 誠實原則：聚合值必附樣本數；無樣本格不出現（不插值不腦補）。
 */

export interface TrackRow {
  lat: number | null; lon: number | null;
  sinr?: number | null;
  [k: string]: unknown;
}

export interface SignalCell {
  lat: number; lon: number;        // 格中心
  x: number; y: number;            // 格中心（公尺、相對 origin）
  p10: number;                     // 最差 10% 分位（保守統計，無平均選項）
  min: number;
  n: number;                       // 樣本數（誠實原則必附）
  sessionIds: string[];            // 涵蓋哪幾趟
}

const M_LAT = 110574;
const mLon = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

/** 客端聚合：選中架次的 link 樣本 → ~grid 公尺格網的保守統計 */
export function aggregateCells(
  tracks: Record<string, TrackRow[]>,
  ids: string[],
  origin: { lat: number; lon: number },
  grid = 10,
): SignalCell[] {
  const k = mLon(origin.lat);
  const bins = new Map<string, { v: number[]; s: Set<string> }>();
  for (const id of ids) {
    for (const r of tracks[id] ?? []) {
      if (r.lat == null || r.lon == null || r.sinr == null) continue;
      const x = (r.lon - origin.lon) * k;
      const y = (r.lat - origin.lat) * M_LAT;
      const key = `${Math.floor(x / grid)}|${Math.floor(y / grid)}`;
      const b = bins.get(key) ?? { v: [], s: new Set<string>() };
      b.v.push(r.sinr as number);
      b.s.add(id);
      bins.set(key, b);
    }
  }
  const cells: SignalCell[] = [];
  for (const [key, b] of bins) {
    const [ix, iy] = key.split("|").map(Number);
    const x = (ix + 0.5) * grid, y = (iy + 0.5) * grid;
    const v = [...b.v].sort((a, c) => a - c);
    cells.push({
      x, y,
      lat: origin.lat + y / M_LAT,
      lon: origin.lon + x / k,
      p10: v[Math.floor(0.1 * (v.length - 1))],
      min: v[0],
      n: v.length,
      sessionIds: [...b.s],
    });
  }
  return cells;
}

/** 斷訊率：SINR < 劣化門檻（5dB）樣本佔比 %（§6.5：建議後端算，
 * 落地前由已載入的 track 客端算，同義） */
export function dropoutPct(rows: TrackRow[]): number | null {
  const v = rows.filter((r) => r.sinr != null);
  if (!v.length) return null;
  return (v.filter((r) => (r.sinr as number) < 5).length / v.length) * 100;
}
