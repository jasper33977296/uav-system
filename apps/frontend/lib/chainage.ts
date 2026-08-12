/** 弧長投影與前後對照（ui-spec §6b.5 契約形狀）。
 *
 * 後端 query_signal_map 同層的對照查詢就緒前先在客端算——回傳形狀與
 * 契約一致：{chainage[], cells[], summary{}}，之後換 fetch 只動一處。
 *
 * 為什麼是弧長不是時間：兩趟速度不同，時間對齊會把「同一地點」錯位成
 * 不同 X（§6b.2）。投影＝把樣本打到共同參考路徑上，兩趟才有共同 X 軸。
 * 誠實原則：投影距離過遠的樣本（偏離參考路徑）不硬塞進某個里程，捨棄
 * 並計數；每個聚合點必附樣本數。
 */

export interface Pt { lat: number; lon: number }
export interface Sample extends Pt {
  sinr?: number | null;
  rsrp?: number | null;
}

const M_LAT = 110574;
const mLon = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

/** 參考路徑的累積里程（公尺）；同時回傳投影用的公尺座標 */
function toXY(pts: Pt[], origin: Pt) {
  const k = mLon(origin.lat);
  return pts.map((p) => ({
    x: (p.lon - origin.lon) * k,
    y: (p.lat - origin.lat) * M_LAT,
  }));
}

/** 樣本 → 參考路徑里程。回傳 null＝離路徑太遠（不硬塞，§6b.4 誠實原則） */
function projectOne(
  px: number, py: number,
  ref: { x: number; y: number }[], cum: number[], maxOff: number,
): number | null {
  let best = null as number | null;
  let bestD = Infinity;
  for (let i = 0; i < ref.length - 1; i++) {
    const ax = ref[i].x, ay = ref[i].y;
    const bx = ref[i + 1].x, by = ref[i + 1].y;
    const dx = bx - ax, dy = by - ay;
    const len2 = dx * dx + dy * dy;
    if (len2 === 0) continue;
    // 樣本點到線段的投影參數（夾在 [0,1]＝落在段內）
    const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
    const cx = ax + t * dx, cy = ay + t * dy;
    const d = Math.hypot(px - cx, py - cy);
    if (d < bestD) { bestD = d; best = cum[i] + t * Math.sqrt(len2); }
  }
  return bestD <= maxOff ? best : null;
}

export interface ChainPoint {
  m: number;                                  // 路徑里程（公尺）
  a_sinr: number | null; b_sinr: number | null;
  a_rsrp: number | null; b_rsrp: number | null;
  a_n: number; b_n: number;                   // 樣本數（誠實原則必附）
}

export interface AbSummary {
  mean: number | null; p50: number | null; p5: number | null; n: number;
}

const pct = (sorted: number[], q: number): number | null =>
  sorted.length ? sorted[Math.min(sorted.length - 1,
    Math.max(0, Math.floor(q * (sorted.length - 1))))] : null;

export function summarize(vals: number[]): AbSummary {
  const v = vals.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  return {
    mean: v.length ? v.reduce((a, b) => a + b, 0) / v.length : null,
    p50: pct(v, 0.5),
    p5: pct(v, 0.05),      // 尾部才是關鍵：平均掉 2dB 無感、最差掉 10dB 就斷鏈
    n: v.length,
  };
}

export interface AbResult {
  chainage: ChainPoint[];
  totalM: number;
  binM: number;              // 實際分箱寬度（畫面須如實標示，不寫死 10m）
  summary: { a: AbSummary; b: AbSummary; aRsrp: AbSummary; bRsrp: AbSummary };
  dropped: { a: number; b: number };   // 離參考路徑太遠而未納入的樣本數
}

/** 前後兩趟 → 沿路徑對照（契約 §6b.5 的 chainage＋summary 部分）。
 * 參考路徑：優先用計畫航點（兩趟共同基準），否則用「前」那趟的軌跡。 */
/** 分箱寬度：以較稀那趟的平均樣本間距為準（×2），下限 10 m。
 * 為什麼不固定 10 m：稀疏架次（實測有每趟僅 10 筆者）在 10 m 格下兩趟
 * 幾乎不落同格，對照欄永遠是空的——那不是「沒有差異」而是「格子太細」。
 * 加大格寬是聚合選擇（樣本仍是真的），與插值（無中生有）不同。 */
export function autoBin(totalM: number, nA: number, nB: number): number {
  const n = Math.max(1, Math.min(nA, nB));
  return Math.max(10, Math.round((totalM / n) * 2 / 5) * 5);
}

export function compareAlongPath(
  a: Sample[], b: Sample[],
  refPath: Pt[] | null,
  binM?: number,
  maxOffM = 60,
): AbResult {
  const ref = (refPath && refPath.length >= 2 ? refPath : a).filter(
    (p) => p.lat != null && p.lon != null);
  if (ref.length < 2) {
    return { chainage: [], totalM: 0, binM: binM ?? 10, dropped: { a: 0, b: 0 },
      summary: { a: summarize([]), b: summarize([]),
        aRsrp: summarize([]), bRsrp: summarize([]) } };
  }
  const origin = ref[0];
  const rxy = toXY(ref, origin);
  const cum = [0];
  for (let i = 1; i < rxy.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(rxy[i].x - rxy[i - 1].x, rxy[i].y - rxy[i - 1].y));
  }
  const totalM = cum[cum.length - 1];
  const k = mLon(origin.lat);
  const bw = binM ?? autoBin(totalM, a.length, b.length);

  const bins = new Map<number, {
    a: number[]; b: number[]; ar: number[]; br: number[] }>();
  const dropped = { a: 0, b: 0 };
  const put = (rows: Sample[], side: "a" | "b") => {
    for (const r of rows) {
      if (r.lat == null || r.lon == null) continue;
      const m = projectOne((r.lon - origin.lon) * k, (r.lat - origin.lat) * M_LAT,
        rxy, cum, maxOffM);
      if (m == null) { dropped[side]++; continue; }
      const key = Math.floor(m / bw);
      const slot = bins.get(key)
        ?? { a: [], b: [], ar: [], br: [] };
      if (r.sinr != null) slot[side].push(r.sinr);
      if (r.rsrp != null) slot[side === "a" ? "ar" : "br"].push(r.rsrp);
      bins.set(key, slot);
    }
  };
  put(a, "a");
  put(b, "b");

  const avg = (v: number[]) => (v.length ? v.reduce((x, y) => x + y, 0) / v.length : null);
  const chainage: ChainPoint[] = [...bins.entries()]
    .sort(([x], [y]) => x - y)
    .map(([key, s]) => ({
      m: (key + 0.5) * bw,
      a_sinr: avg(s.a), b_sinr: avg(s.b),
      a_rsrp: avg(s.ar), b_rsrp: avg(s.br),
      a_n: s.a.length, b_n: s.b.length,
    }));

  return {
    chainage, totalM, binM: bw, dropped,
    summary: {
      a: summarize(a.map((r) => r.sinr).filter((v): v is number => v != null)),
      b: summarize(b.map((r) => r.sinr).filter((v): v is number => v != null)),
      aRsrp: summarize(a.map((r) => r.rsrp).filter((v): v is number => v != null)),
      bRsrp: summarize(b.map((r) => r.rsrp).filter((v): v is number => v != null)),
    },
  };
}

/** 差值熱區的格（§6b.2 ③）：兩趟都有樣本才有 delta；單趟＝無對照 */
export interface DeltaCell {
  lat: number; lon: number;
  delta: number | null;      // null＝無對照（只有一趟有樣本）
  a_sinr: number | null; b_sinr: number | null;
  a_n: number; b_n: number;
}

/** 中位數（熱區用；比平均耐離群） */
const med = (v: number[]): number | null => {
  if (!v.length) return null;
  const s = [...v].sort((x, y) => x - y);
  return s[Math.floor(s.length / 2)];
};

export function deltaCells(
  a: Sample[], b: Sample[], origin: Pt, grid = 10,
): DeltaCell[] {
  const k = mLon(origin.lat);
  const bins = new Map<string, { a: number[]; b: number[] }>();
  const put = (rows: Sample[], side: "a" | "b") => {
    for (const r of rows) {
      if (r.lat == null || r.lon == null || r.sinr == null) continue;
      const x = (r.lon - origin.lon) * k, y = (r.lat - origin.lat) * M_LAT;
      const key = `${Math.floor(x / grid)}|${Math.floor(y / grid)}`;
      const slot = bins.get(key) ?? { a: [], b: [] };
      slot[side].push(r.sinr);
      bins.set(key, slot);
    }
  };
  put(a, "a");
  put(b, "b");
  const out: DeltaCell[] = [];
  for (const [key, s] of bins) {
    const [ix, iy] = key.split("|").map(Number);
    const x = (ix + 0.5) * grid, y = (iy + 0.5) * grid;
    const av = med(s.a), bv = med(s.b);
    out.push({
      lat: origin.lat + y / M_LAT, lon: origin.lon + x / k,
      // 兩趟都有樣本才給 delta；否則 null＝無對照（不用 0 冒充「沒變化」）
      delta: av != null && bv != null ? bv - av : null,
      a_sinr: av, b_sinr: bv, a_n: s.a.length, b_n: s.b.length,
    });
  }
  return out;
}
