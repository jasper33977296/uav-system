/** 3D 地圖的幾何工具：live（MapView）與回放頁共用。 */

export const CANVAS = "#14181c";
export const DRONE_COLOR = "#3987e5";

export const M_LAT = 110574; // 一度緯度的公尺數
export const mLon = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

/** 地面網格：無底圖時的地面基準。每 stepM 一條線，覆蓋 ±halfM。 */
export function groundGrid(
  lat: number, lon: number, halfM = 400, stepM = 50
): GeoJSON.FeatureCollection {
  const feats: GeoJSON.Feature[] = [];
  const line = (a: [number, number], b: [number, number]): GeoJSON.Feature => ({
    type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [a, b] },
  });
  for (let m = -halfM; m <= halfM; m += stepM) {
    feats.push(line([lon + m / mLon(lat), lat - halfM / M_LAT],
                    [lon + m / mLon(lat), lat + halfM / M_LAT]));
    feats.push(line([lon - halfM / mLon(lat), lat + m / M_LAT],
                    [lon + halfM / mLon(lat), lat + m / M_LAT]));
  }
  return { type: "FeatureCollection", features: feats };
}

/** 以點為中心的正多邊形（懸浮機體底面等） */
export function ngonAt(lat: number, lon: number, halfM: number, n = 4): GeoJSON.Polygon {
  const pts: [number, number][] = [];
  for (let i = 0; i <= n; i++) {
    const a = (i / n) * 2 * Math.PI + Math.PI / n;
    pts.push([lon + (halfM * Math.cos(a)) / mLon(lat), lat + (halfM * Math.sin(a)) / M_LAT]);
  }
  return { type: "Polygon", coordinates: [pts] };
}

/** 兩點間的平面段（絲帶的一節）：沿路徑方向、寬 2×halfW 公尺的四邊形。
    搭配 fill-extrusion 的 base/top 讓它**懸浮在飛行高度**——
    路徑本身浮在 3D 空間、下方留空，地面另有投影。
    距離小於 0.3m（懸停）回傳 null，避免退化四邊形。 */
export function segQuad(
  aLat: number, aLon: number, bLat: number, bLon: number, halfW: number
): GeoJSON.Polygon | null {
  const k = mLon((aLat + bLat) / 2);
  const dx = (bLon - aLon) * k, dy = (bLat - aLat) * M_LAT;
  const len = Math.hypot(dx, dy);
  if (len < 0.3) return null;
  const nx = (-dy / len) * halfW, ny = (dx / len) * halfW;
  const c = (lon: number, lat: number, sx: number, sy: number): [number, number] =>
    [lon + sx / k, lat + sy / M_LAT];
  return {
    type: "Polygon",
    coordinates: [[
      c(aLon, aLat, nx, ny), c(bLon, bLat, nx, ny),
      c(bLon, bLat, -nx, -ny), c(aLon, aLat, -nx, -ny), c(aLon, aLat, nx, ny),
    ]],
  };
}

/** 無人機 3D 本體：八角柱近似球體，浮在實際飛行高度。 */
export function droneBall(lat: number, lon: number, alt: number): GeoJSON.Feature {
  const r = 3.2;
  return {
    type: "Feature",
    properties: { base: Math.max(alt - r, 0), top: Math.max(alt + r, r) },
    geometry: ngonAt(lat, lon, r, 8),
  };
}

/** 把一串帶高度的點串成懸浮絲帶（FeatureCollection of 平面段）。
    props(a, b) 決定每一節的屬性（顏色分級等）；厚度 1m、預設寬 3m。 */
export function ribbon<T extends { lat: number | null; lon: number | null; alt: number | null }>(
  pts: T[],
  props: (a: T, b: T) => Record<string, unknown>,
  halfW = 1.5,
): GeoJSON.FeatureCollection {
  const feats: GeoJSON.Feature[] = [];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    if (a.lat == null || a.lon == null || b.lat == null || b.lon == null) continue;
    const quad = segQuad(a.lat, a.lon, b.lat, b.lon, halfW);
    if (!quad) continue;
    const alt = ((a.alt ?? 0) + (b.alt ?? 0)) / 2;
    feats.push({
      type: "Feature",
      properties: { base: Math.max(alt - 0.5, 0), top: Math.max(alt + 0.5, 0.5), ...props(a, b) },
      geometry: quad,
    });
  }
  return { type: "FeatureCollection", features: feats };
}
