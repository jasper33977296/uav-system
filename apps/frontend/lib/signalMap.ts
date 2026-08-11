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
  lastBad?: string | null;         // 最近一次劣化樣本時間（v4 弱區卡用，客端附加欄）
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
  const bins = new Map<string, { v: number[]; s: Set<string>; bad: string | null }>();
  for (const id of ids) {
    for (const r of tracks[id] ?? []) {
      if (r.lat == null || r.lon == null || r.sinr == null) continue;
      const x = (r.lon - origin.lon) * k;
      const y = (r.lat - origin.lat) * M_LAT;
      const key = `${Math.floor(x / grid)}|${Math.floor(y / grid)}`;
      const b = bins.get(key) ?? { v: [], s: new Set<string>(), bad: null };
      b.v.push(r.sinr as number);
      b.s.add(id);
      const t = typeof r.time === "string" ? r.time : null;
      if ((r.sinr as number) < 5 && t && (!b.bad || t > b.bad)) b.bad = t;
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
      lastBad: b.bad,
    });
  }
  return cells;
}

// ── v4 弱區輪廓（§6：持續性弱區＝P10 入劣化以下且涵蓋 ≥2 趟的聚簇）──

export interface WeakZone {
  minVal: number;                    // 圈上標的最差值
  n: number;                         // 樣本總數
  sessionIds: string[];              // 涵蓋趟
  lastBad: string | null;            // 最近一次劣化時間
  labelLat: number; labelLon: number;
  outline: [number, number][][];     // 輪廓環（[lon,lat]，Chaikin 平滑一輪）
}

/** 相鄰弱格聚簇 → 邊界輪廓。只圈飛過的區域（弱格本身即有樣本格）。 */
export function weakZones(
  cells: SignalCell[],
  origin: { lat: number; lon: number },
  grid = 10,
): WeakZone[] {
  const k = mLon(origin.lat);
  const weak = cells.filter((c) => c.p10 < 5 && c.sessionIds.length >= 2);
  if (!weak.length) return [];
  const idx = new Map<string, SignalCell>();
  const keyOf = (c: SignalCell) =>
    `${Math.round(c.x / grid - 0.5)}|${Math.round(c.y / grid - 0.5)}`;
  for (const c of weak) idx.set(keyOf(c), c);

  const seen = new Set<string>();
  const zones: WeakZone[] = [];
  for (const [start, sc] of idx) {
    if (seen.has(start)) continue;
    // flood fill（4 鄰）
    const cluster: string[] = [];
    const q = [start];
    seen.add(start);
    while (q.length) {
      const cur = q.pop()!;
      cluster.push(cur);
      const [ix, iy] = cur.split("|").map(Number);
      for (const nb of [`${ix + 1}|${iy}`, `${ix - 1}|${iy}`,
                        `${ix}|${iy + 1}`, `${ix}|${iy - 1}`]) {
        if (idx.has(nb) && !seen.has(nb)) { seen.add(nb); q.push(nb); }
      }
    }
    const cs = cluster.map((key) => idx.get(key)!);
    // 邊界邊集合（與簇外相鄰的格邊），鏈接成環
    const segs = new Map<string, string>();   // 起點 → 終點（沿格邊逆時針）
    const P = (x: number, y: number) => `${x}|${y}`;
    for (const key of cluster) {
      const [ix, iy] = key.split("|").map(Number);
      const x0 = ix * grid, y0 = iy * grid, x1 = x0 + grid, y1 = y0 + grid;
      if (!idx.has(`${ix}|${iy - 1}`)) segs.set(P(x0, y0), P(x1, y0));   // 下邊 →
      if (!idx.has(`${ix + 1}|${iy}`)) segs.set(P(x1, y0), P(x1, y1));   // 右邊 ↑
      if (!idx.has(`${ix}|${iy + 1}`)) segs.set(P(x1, y1), P(x0, y1));   // 上邊 ←
      if (!idx.has(`${ix - 1}|${iy}`)) segs.set(P(x0, y1), P(x0, y0));   // 左邊 ↓
    }
    const loops: [number, number][][] = [];
    const used = new Set<string>();
    for (const s0 of segs.keys()) {
      if (used.has(s0)) continue;
      const loop: [number, number][] = [];
      let cur = s0;
      while (!used.has(cur) && segs.has(cur)) {
        used.add(cur);
        const [mx, my] = cur.split("|").map(Number);
        loop.push([mx, my]);
        cur = segs.get(cur)!;
      }
      if (loop.length >= 4) loops.push(loop);
    }
    // Chaikin 平滑一輪（閉環）＋公尺 → 經緯度
    const smooth = loops.map((loop) => {
      const out: [number, number][] = [];
      for (let i = 0; i < loop.length; i++) {
        const p = loop[i], nx = loop[(i + 1) % loop.length];
        out.push([p[0] * 0.75 + nx[0] * 0.25, p[1] * 0.75 + nx[1] * 0.25]);
        out.push([p[0] * 0.25 + nx[0] * 0.75, p[1] * 0.25 + nx[1] * 0.75]);
      }
      out.push(out[0]);
      return out.map(([x, y]) =>
        [origin.lon + x / k, origin.lat + y / M_LAT] as [number, number]);
    });
    const worst = cs.reduce((a, c) => (c.min < a.min ? c : a));
    zones.push({
      minVal: Math.min(...cs.map((c) => c.min)),
      n: cs.reduce((t, c) => t + c.n, 0),
      sessionIds: [...new Set(cs.flatMap((c) => c.sessionIds))],
      lastBad: cs.reduce<string | null>(
        (t, c) => (c.lastBad && (!t || c.lastBad > t) ? c.lastBad : t), null),
      labelLat: worst.lat, labelLon: worst.lon,
      outline: smooth,
    });
  }
  return zones;
}

/** 斷訊率：SINR < 劣化門檻（5dB）樣本佔比 %（§6.5：建議後端算，
 * 落地前由已載入的 track 客端算，同義） */
export function dropoutPct(rows: TrackRow[]): number | null {
  const v = rows.filter((r) => r.sinr != null);
  if (!v.length) return null;
  return (v.filter((r) => (r.sinr as number) < 5).length / v.length) * 100;
}
