/** 3D 地圖的幾何工具：live（MapView）與回放頁共用。 */

export const CANVAS = "#1b1a17";   // ＝--page（design-tokens v1 暖 stone；maplibre 吃不到 CSS 變數，手動同步）
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
    props(a, b) 決定每一節的屬性（顏色分級等）；預設寬 3m、厚度＝寬度的一半
    （窄帶配等厚的板會從側面看成立鰭，厚度得跟著寬度走）。

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
  const halfT = halfW / 2;                            // 垂直半厚（見上）

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
        properties: { base: Math.max(alt - halfT, 0), top: Math.max(alt + halfT, halfT), ...props(a, b) },
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

/** 路徑方向箭頭的**落點與航向**（幾何交給 deck IconLayer，見
    lib/arrowIcon）。每 everyN 個樣本放一枚。

    為什麼不再回傳三角形多邊形：世界座標的三角形大小固定在公尺，
    縮放到近處就變成一片大白板（使用者反饋「箭頭太大」）。改成回傳
    點＋角度後，尺寸由 IconLayer 的 `sizeUnits/size*Pixels` 決定——
    隨縮放連續變化、兩端有界（同 deckRoute 的路徑寬度原則）。

    航向取「往後找到第一個水平位移 ≥1m 的樣本」而非固定下一點：
    1Hz 資料在低速/懸停時相鄰兩點的差幾乎全是 GPS 抖動，直接用會讓
    箭頭亂指。找不到（懸停到底）就不放這一枚——方向不明時不指。 */
export function pathArrows<T extends { lat: number | null; lon: number | null; alt: number | null }>(
  pts: T[], everyN = 12,
): { pos: [number, number, number]; deg: number }[] {
  const out: { pos: [number, number, number]; deg: number }[] = [];
  for (let i = everyN; i < pts.length - 1; i += everyN) {
    const a = pts[i];
    if (a.lat == null || a.lon == null) continue;
    const k = mLon(a.lat);
    let dx = 0, dy = 0;
    for (let j = i + 1; j < Math.min(pts.length, i + everyN); j++) {
      const b = pts[j];
      if (b.lat == null || b.lon == null) continue;
      dx = (b.lon - a.lon) * k; dy = (b.lat - a.lat) * M_LAT;
      if (Math.hypot(dx, dy) >= 1) break;
      dx = 0; dy = 0;
    }
    if (dx === 0 && dy === 0) continue;                 // 懸停段：方向不明
    out.push({
      pos: [a.lon, a.lat, a.alt ?? 0],
      // 羅盤方位（自北順時針）；IconLayer 的 getAngle 是逆時針，呼叫端取負
      deg: (Math.atan2(dx, dy) * 180) / Math.PI,
    });
  }
  return out;
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

/** 任務航點（畫圖只需要這幾欄；`action` 是本系統的語意欄，見 plans.py）。 */
export interface PlanPt {
  lat: number; lon: number; alt: number | null; action?: string | null;
}

/** 任務航點 → 畫得出來的 3D 折線：起飛爬升段 + 航路 + 降落段。
 *
 * **起飛項的高度是「爬到哪」，不是「它所在的高度」。** 直接把 NAV_TAKEOFF
 * 當第一個點畫，折線就從 40 m 的空中開始——任務看起來從半空中出發，而使用者
 * 在 QGC 畫的明明是從地面起飛（2026-09-07 使用者回報）。
 *
 * 起飛項的經緯度可能是 0,0：NAV_TAKEOFF 在 ArduPilot 只需要高度，那組 0,0
 * 的意思是「從 home 起飛」。這種情況位置取 `plannedHomePosition`。
 *
 * ## 收尾的三種寫法**意思完全不同**（2026-09-07 二修）
 *
 * 原本一律當成「回 home 降落」：`all.some(w => w.action === "rtl" || "land")`
 * 就在尾巴接兩個 home 點。於是一份**降落點與起飛點不同**的航線，畫出來會
 * 從降落點再拉一條線回到起飛點——那條線不在任何一份 .plan 裡
 * （使用者回報：「我的起飛點跟降落點不同，但顯示的時候會把兩點連在一起」；
 * 實測那份航線的降落點離 home 5.6 m）。
 *
 * | 項目 | 意思 | 怎麼畫 |
 * |---|---|---|
 * | `RTL`（cmd 20） | 回 home 再降 | 平飛回 home → 垂直下降 |
 * | `LAND` 有座標 | 飛到那個點再降 | 平飛到該點 → 垂直下降 |
 * | `LAND` 沒座標 | **在當下的位置降**（不是回 home）| 在最後一點垂直下降 |
 *
 * 兩段式（先平飛、再垂直下降）而不是一條斜線：斜線會讓人以為航線會穿過
 * 中間的地形。
 *
 * **這裡是唯一一份實作。** 三個地圖頁與路徑管理頁的縮圖全部走這一支——
 * 同一份任務在四個畫面上必須是同一個形狀（縮圖曾經自己寫過一份，
 * 於是它把補進來的 home 點畫在 10 m 的空中，見 MissionThumb3D）。
 */
export function planPath(all: PlanPt[], home?: (number | null)[] | null): PlanPt[] {
  const h = Array.isArray(home) && home.length >= 2 && (home[0] || home[1])
    ? { lat: home[0] as number, lon: home[1] as number } : null;
  // `do` 項沒有位置語意（DO_CHANGE_SPEED 之類），不進折線
  const nav = all.filter((w) => w.action !== "do");
  const out: PlanPt[] = [];
  // 目前高度：航點沒帶高度時沿用上一個——**不要掉回 0**，那會讓折線
  // 無緣無故插一段俯衝到地面再爬回來
  let alt = 0;
  const pos = (w: PlanPt) => (w.lat || w.lon) ? { lat: w.lat, lon: w.lon } : null;
  const lastPos = () => (out.length
    ? { lat: out[out.length - 1].lat, lon: out[out.length - 1].lon } : null);

  for (const w of nav) {
    if (w.action === "takeoff") {
      const at = pos(w) ?? h;
      if (!at) continue;                       // 不知道從哪起飛就不畫這一段
      out.push({ ...at, alt: 0, action: "takeoff-ground" });
      alt = w.alt ?? alt;
      out.push({ ...at, alt, action: "takeoff-leg" });
      continue;
    }
    if (w.action === "rtl") {
      if (!h) continue;                        // 沒有 home 就畫不出返航段
      out.push({ ...h, alt, action: "rtl-leg" });
      out.push({ ...h, alt: 0, action: "rtl-land" });
      alt = 0;
      continue;
    }
    if (w.action === "land") {
      // 沒座標＝在當下的位置降落。**不是回 home**——那是 RTL 的意思
      const at = pos(w) ?? lastPos();
      if (!at) continue;
      if (pos(w)) out.push({ ...at, alt, action: "land-approach" });
      alt = w.alt ?? 0;
      out.push({ ...at, alt, action: "land" });
      continue;
    }
    const at = pos(w);
    if (!at) continue;                         // 沒座標的一般航點畫不出來
    alt = w.alt ?? alt;
    out.push({ ...at, alt, action: w.action });
  }
  return out;
}
