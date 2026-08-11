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
    props(a, b) 決定每一節的屬性（顏色分級等）；厚度 1m、預設寬 3m。

    相鄰段**共用 miter join 頂點**（issue 017 P1）：每個樣本點的左右
    offset 沿角平分線計算，整條水平鏈是連續三角帶——轉角不再有逐段獨立
    四邊形的楔形縫隙/重疊。轉角過銳時 miter 長度上限 2×halfW（bevel 退化）
    避免尖刺。每一節仍是獨立 feature：顏色分級逐段取實際樣本、不插值
    （誠實原則——平滑的是幾何接縫，不是資料）。 */
export function ribbon<T extends { lat: number | null; lon: number | null; alt: number | null }>(
  pts: T[],
  props: (a: T, b: T) => Record<string, unknown>,
  halfW = 1.5,
): GeoJSON.FeatureCollection {
  const feats: GeoJSON.Feature[] = [];

  // 一條「水平鏈」＝連續且水平位移 ≥0.3m 的樣本序列，整鏈做 miter join
  const emitChain = (chain: T[]) => {
    if (chain.length < 2) return;
    const k = mLon(chain[Math.floor(chain.length / 2)].lat!);
    const xy = chain.map((p) => ({ x: p.lon! * k, y: p.lat! * M_LAT }));
    const norms: { x: number; y: number }[] = [];       // 每段單位法線
    for (let i = 1; i < xy.length; i++) {
      const dx = xy[i].x - xy[i - 1].x, dy = xy[i].y - xy[i - 1].y;
      const len = Math.hypot(dx, dy);
      norms.push({ x: -dy / len, y: dx / len });
    }
    // 每點的左右 offset：內點取相鄰兩段法線的角平分線，端點取鄰段法線
    const offs = xy.map((_, j) => {
      const n1 = norms[Math.max(j - 1, 0)], n2 = norms[Math.min(j, norms.length - 1)];
      let mx = n1.x + n2.x, my = n1.y + n2.y;
      const ml = Math.hypot(mx, my);
      if (ml < 1e-9) { mx = n2.x; my = n2.y; }          // 180° 折返：退回段法線
      else { mx /= ml; my /= ml; }
      const dot = mx * n2.x + my * n2.y;                // = cos(半轉角)
      const scale = Math.min(1 / Math.max(dot, 1e-6), 2);
      return { x: mx * halfW * scale, y: my * halfW * scale };
    });
    const pt = (j: number, sign: 1 | -1): [number, number] =>
      [(xy[j].x + sign * offs[j].x) / k, (xy[j].y + sign * offs[j].y) / M_LAT];
    for (let i = 1; i < chain.length; i++) {
      const a = chain[i - 1], b = chain[i];
      const alt = ((a.alt ?? 0) + (b.alt ?? 0)) / 2;
      feats.push({
        type: "Feature",
        properties: { base: Math.max(alt - 0.5, 0), top: Math.max(alt + 0.5, 0.5), ...props(a, b) },
        geometry: { type: "Polygon", coordinates: [[
          pt(i - 1, 1), pt(i, 1), pt(i, -1), pt(i - 1, -1), pt(i - 1, 1),
        ]] },
      });
    }
  };

  let chain: T[] = [];
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i];
    if (p.lat == null || p.lon == null) {               // GPS 缺值：中斷、不跨缺口連線
      emitChain(chain); chain = [];
      continue;
    }
    const prev = chain[chain.length - 1];
    if (!prev) { chain = [p]; continue; }
    const k = mLon((prev.lat! + p.lat) / 2);
    const horiz = Math.hypot((p.lon - prev.lon!) * k, (p.lat - prev.lat!) * M_LAT);
    if (horiz >= 0.3) { chain.push(p); continue; }
    // 水平位移過小（起降／懸停中的爬升）：中斷水平鏈，畫成該點的垂直段，
    // 讓上升下降的路徑同樣被顏色標示，不再隱形
    emitChain(chain); chain = [p];
    const lo = Math.min(prev.alt ?? 0, p.alt ?? 0), hi = Math.max(prev.alt ?? 0, p.alt ?? 0);
    if (hi - lo < 0.6) continue;   // 純懸停不畫
    feats.push({
      type: "Feature",
      properties: { base: Math.max(lo, 0), top: hi, ...props(prev, p) },
      geometry: ngonAt(p.lat, p.lon, halfW * 0.8, 8),
    });
  }
  emitChain(chain);
  return { type: "FeatureCollection", features: feats };
}

/** 路徑方向箭頭：沿飛行方向的小三角形，浮在絲帶上方一點。
    每 everyN 個樣本放一枚；水平位移太小的段落跳過（垂直段方向無意義）。 */
export function pathArrows<T extends { lat: number | null; lon: number | null; alt: number | null }>(
  pts: T[], everyN = 12, sizeM = 6,
): GeoJSON.FeatureCollection {
  const feats: GeoJSON.Feature[] = [];
  for (let i = everyN; i < pts.length - 1; i += everyN) {
    const a = pts[i], b = pts[i + 1];
    if (a.lat == null || a.lon == null || b.lat == null || b.lon == null) continue;
    const k = mLon(a.lat);
    const dx = (b.lon - a.lon) * k, dy = (b.lat - a.lat) * M_LAT;
    const len = Math.hypot(dx, dy);
    if (len < 0.5) continue;
    const ux = dx / len, uy = dy / len;              // 前進方向單位向量
    const px = -uy, py = ux;                          // 垂直向
    const c = (sx: number, sy: number): [number, number] =>
      [a.lon! + sx / k, a.lat! + sy / M_LAT];
    const tip = c(ux * sizeM * 0.6, uy * sizeM * 0.6);
    const l = c(-ux * sizeM * 0.4 + px * sizeM * 0.35, -uy * sizeM * 0.4 + py * sizeM * 0.35);
    const r = c(-ux * sizeM * 0.4 - px * sizeM * 0.35, -uy * sizeM * 0.4 - py * sizeM * 0.35);
    const alt = a.alt ?? 0;
    feats.push({
      type: "Feature",
      properties: { base: alt + 0.7, top: alt + 1.2 },
      geometry: { type: "Polygon", coordinates: [[tip, l, r, tip]] },
    });
  }
  return { type: "FeatureCollection", features: feats };
}

/** 地面投影線：一串點 → LineString。逐點圓點在 1Hz 資料下是一串顆粒，
    連續線（搭配 round join/cap）才是平滑的路徑投影。 */
export function trailLineString<T extends { lat: number | null; lon: number | null }>(
  pts: T[], props: Record<string, unknown> = {},
): GeoJSON.Feature | null {
  const coords = pts
    .filter((p) => p.lat != null && p.lon != null)
    .map((p) => [p.lon!, p.lat!]);
  if (coords.length < 2) return null;
  return { type: "Feature", properties: props,
           geometry: { type: "LineString", coordinates: coords } };
}
