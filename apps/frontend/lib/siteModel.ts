/** 屋頂設備的視覺模型（2026-10-13 使用者指定座標）。
 *
 * **這裡只有幾何，沒有概念。** 使用者裁定「現在先不要有基站或干擾區的概念，
 * 單純嵌入視覺模型就好了」——所以這支不碰 `link_metrics`、不認 PCI、不算
 * 覆蓋範圍，也沒有後端端點。它做的事只有一件：把兩個立體物件放上地圖。
 *
 * **屋頂高度不寫死，向建物資料要。** 第一版把高度寫成 28（52館 的 OSM
 * 樓層數推算值），結果模型飄在半空中——使用者回報「基站怎飄在空中？」。
 * 原因不是高度錯，是**平面位置**：兩個座標都落在 52館 的 OSM 輪廓**之外**
 * （A 差 1.3 m、B 差 8.1 m），所以那個高度上根本沒有屋頂接著它。
 *
 * 使用者說兩台都在屋頂上，那是現場事實；OSM 的輪廓是畫的，水平解析度
 * ±3 m（`libs/buildings.py:OSM_HORIZ_RES_M`）。兩邊不一致時以現場為準，
 * 所以這支會把點**挪進最近那棟樓的輪廓裡**再放模型，並且把挪了多少寫進
 * feature 的 `shift_m`——**位置被動過這件事要留得下來**，不能只是看起來對了。
 *
 * 找不到樓、或那棟樓高度未知時 **不畫**：不知道屋頂在哪個高度，就沒有
 * 「站在屋頂上」這件事可畫（同建物／植被的紀律，猜一個高度出來是撞機）。
 *
 * **只掛在即時頁。** 規劃頁的建物只抓沿線 30 m（那是它的設計），而 52館 離
 * 現行航線 84.7 m——在那一頁拿不到這棟樓，就沒有屋頂可站。
 */
import { M_LAT, mLon } from "@/lib/geo";

/** 使用者給的兩點（2026-10-13）。兩點相距約 20.7 m。 */
export const SITE_POINTS: { id: string; name: string; lat: number; lon: number }[] = [
  { id: "a", name: "A", lat: 24.774047, lon: 121.045195 },
  { id: "b", name: "B", lat: 24.774073, lon: 121.045398 },
];

const MAST_M = 6;        // 桿高（屋頂面往上）
const MAST_W = 0.5;      // 桿寬
const PANEL_H = 1.6;     // 天線面板高
const PANEL_W = 0.45;    // 面板寬
const PANEL_T = 0.22;    // 面板厚
const PANEL_R = 1.0;     // 面板離桿心的距離
const PANEL_TOP_DROP = 0.6;   // 面板頂距桿頂
/** 挪進輪廓後再往內讓多少。
 *
 * 原本 1.5 m，只夠讓它「不在輪廓外」——但 1.5 m 在屋簷線上，傾斜視角下看
 * 起來仍然像掛在邊緣外（使用者 2026-10-13 第二次回報「還是飄在 3D 建築物
 * 外」，並指示「貼齊到 3D 建模的建築物中」）。改成 6 m：目視就在屋頂面上。
 * **這是顯示用的位置，不是量到的位置**——座標與輪廓的落差使用者裁定
 * 「先不管，之後再量」，`shift_m` 仍然記著一共挪了多少。 */
const INSET_M = 6;
/** 超過這個距離就不算「同一棟樓的屋頂」——寧可不畫，也不要硬接。 */
const MAX_SNAP_M = 25;

/** 以 (lat0, lon0) 為原點、公尺為單位的矩形環 → GeoJSON 的經緯度環。 */
function rectRing(lat0: number, lon0: number, dx: number, dy: number,
                  w: number, h: number, rot: number): [number, number][] {
  const ml = mLon(lat0);
  const c = Math.cos(rot), s = Math.sin(rot);
  const corners: [number, number][] = [
    [-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]];
  const ring: [number, number][] = corners.map(([x, y]) => {
    const X = dx + x * c - y * s;
    const Y = dy + x * s + y * c;
    return [lon0 + X / ml, lat0 + Y / M_LAT];
  });
  return [...ring, ring[0]];
}

/** 射線法（與 libs/buildings.py:_in_ring 同一個做法）。ring ＝ [x, y] 平面點。 */
function inRing(x: number, y: number, ring: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, n = ring.length; i < n; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % n];
    if ((y1 > y) !== (y2 > y) && x < x1 + ((y - y1) * (x2 - x1)) / (y2 - y1)) {
      inside = !inside;
    }
  }
  return inside;
}

/** 點到線段的最近點與距離。 */
function nearestOnSeg(px: number, py: number, ax: number, ay: number,
                      bx: number, by: number) {
  const dx = bx - ax, dy = by - ay;
  const L2 = dx * dx + dy * dy;
  const t = L2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2));
  const qx = ax + t * dx, qy = ay + t * dy;
  return { x: qx, y: qy, d: Math.hypot(px - qx, py - qy) };
}

interface Placed { lat: number; lon: number; roof: number; shift: number }

/** 把一個點放到腳下那棟樓的屋頂上。放不上去就回 null。 */
function placeOnRoof(lat: number, lon: number,
                     buildings: GeoJSON.FeatureCollection): Placed | null {
  const ml = mLon(lat);
  let best: { d: number; q: { x: number; y: number }; roof: number } | null = null;
  for (const f of buildings.features) {
    const g = f.geometry;
    if (!g || g.type !== "Polygon") continue;
    const h = (f.properties as { height_m?: number | null } | null)?.height_m;
    // **高度未知的樓不接**：不知道屋頂在哪個高度，就沒有屋頂可站
    if (h == null) continue;
    const ring: [number, number][] = (g.coordinates[0] as number[][]).map(
      ([lo, la]) => [(lo - lon) * ml, (la - lat) * M_LAT]);
    if (inRing(0, 0, ring)) return { lat, lon, roof: h, shift: 0 };
    for (let i = 0; i < ring.length - 1; i++) {
      const n = nearestOnSeg(0, 0, ring[i][0], ring[i][1],
                             ring[i + 1][0], ring[i + 1][1]);
      if (!best || n.d < best.d) best = { d: n.d, q: { x: n.x, y: n.y }, roof: h };
    }
  }
  if (!best || best.d > MAX_SNAP_M) return null;
  // 沿「點 → 輪廓最近點」的方向再走 INSET_M，就是往輪廓內
  const len = Math.hypot(best.q.x, best.q.y) || 1;
  const x = best.q.x + (best.q.x / len) * INSET_M;
  const y = best.q.y + (best.q.y / len) * INSET_M;
  return { lat: lat + y / M_LAT, lon: lon + x / ml, roof: best.roof,
           shift: Math.hypot(x, y) };
}

/** CELL 的示意半徑（公尺）。
 *
 * **這是示意，不是量到的覆蓋範圍。** 怎麼來的：實測 4343 筆（2026-09-14 以來
 * 的真實飛行）的中位是 **−89／−90 dBm @ 3D 距離 ~105 m**——這一段是量到的；
 * 往外推到 −100 dBm 要**假設**一個路徑損耗指數，取 n=2.7 得 243–268 m，
 * 這裡取 250。
 *
 * **為什麼不是從量測擬合出來的**：全部樣本只跨 82–141 m（1.7 倍，不到 0.2 個
 * 數量級），那段距離上的路徑損耗只有 4–8 dB，而樣本散佈是 ±6.6–7 dB。實際
 * 擬合出來的衰減指數是**負的**（R²≈0.00／0.11），也就是「越遠越強」——物理
 * 上不可能。訊號埋在雜訊裡，所以斜率只能假設。n 從 2.0 換到 3.5，半徑就在
 * 200–380 m 之間跑，**這個圈的大小主要由那個假設決定，不由量測決定**。
 *
 * 畫成正圓也是示意：兩個 cell 的天線方位我們沒有資料，畫成扇形等於編方向。 */
export const CELL_R_M = 250;

/** CELL 的顏色。兩個圓幾乎同心（天線只差 20.7 m），靠色相分辨。 */
export const CELL_COLORS: Record<string, string> = { a: "#3f7f8c", b: "#8a6b9e" };

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

/** 兩座模型的立體幾何。`base`／`top` 給 fill-extrusion 吃。
 *
 * 形狀是**實際尺寸**（桿 6 m、面板 1.6 m），不放大：放大的模型在縮遠時
 * 看得比較清楚，代價是它與旁邊那棟樓的比例從此是假的。 */
export function siteModelFC(buildings: GeoJSON.FeatureCollection | null)
    : GeoJSON.FeatureCollection {
  if (!buildings) return EMPTY;
  const feats: GeoJSON.Feature[] = [];
  for (const s of SITE_POINTS) {
    const p = placeOnRoof(s.lat, s.lon, buildings);
    if (!p) continue;                 // 腳下沒有（高度已知的）樓：不畫
    const props = { site: s.id, name: s.name, shift_m: Math.round(p.shift * 10) / 10 };
    feats.push({
      type: "Feature",
      properties: { ...props, part: "mast", base: p.roof, top: p.roof + MAST_M },
      geometry: { type: "Polygon", coordinates: [
        rectRing(p.lat, p.lon, 0, 0, MAST_W, MAST_W, 0)] },
    });
    // 三面板 120° 一支，站上去就看得出朝向——這是形狀，不是波束宣告
    for (let k = 0; k < 3; k++) {
      const a = (k * 2 * Math.PI) / 3;
      feats.push({
        type: "Feature",
        properties: { ...props, part: "panel",
                      base: p.roof + MAST_M - PANEL_TOP_DROP - PANEL_H,
                      top: p.roof + MAST_M - PANEL_TOP_DROP },
        geometry: { type: "Polygon", coordinates: [
          rectRing(p.lat, p.lon, PANEL_R * Math.sin(a), PANEL_R * Math.cos(a),
                   PANEL_W, PANEL_T, -a)] },
      });
    }
  }
  return { type: "FeatureCollection", features: feats };
}

/** 以 (lat, lon) 為心、半徑 r 公尺的圓（96 邊形近似——地圖上看不出差別）。 */
function circleRing(lat: number, lon: number, r: number): [number, number][] {
  const ml = mLon(lat);
  const ring: [number, number][] = [];
  for (let i = 0; i <= 96; i++) {
    const a = (i / 96) * 2 * Math.PI;
    ring.push([lon + (r * Math.cos(a)) / ml, lat + (r * Math.sin(a)) / M_LAT]);
  }
  return ring;
}

/** 兩個 CELL 的示意範圍（地面上的半透明圓）。
 *
 * 圓心跟著模型實際站的位置走，**不是另外一組座標**：畫面上只能有一個
 * 「它在哪裡」。半徑見 `CELL_R_M` 的說明——量到的是錨點，外推的斜率是假設。 */
export function siteCellsFC(buildings: GeoJSON.FeatureCollection | null)
    : GeoJSON.FeatureCollection {
  if (!buildings) return EMPTY;
  const feats: GeoJSON.Feature[] = [];
  for (const s of SITE_POINTS) {
    const p = placeOnRoof(s.lat, s.lon, buildings);
    if (!p) continue;
    feats.push({
      type: "Feature",
      properties: { site: s.id, name: s.name, radius_m: CELL_R_M,
                    color: CELL_COLORS[s.id] ?? "#3f7f8c" },
      geometry: { type: "Polygon", coordinates: [circleRing(p.lat, p.lon, CELL_R_M)] },
    });
  }
  return feats.length ? { type: "FeatureCollection", features: feats } : EMPTY;
}

/** 模型腳下的地面標記：6 m 的桿子縮遠只有幾個像素，沒有這個就找不到它在哪。
 *  **畫在模型實際站的位置**（挪過之後），不是原始座標——兩個位置各畫一個
 *  只會看起來像畫錯了。挪了多少記在 `shift_m` 裡。 */
export function sitePointsFC(buildings: GeoJSON.FeatureCollection | null)
    : GeoJSON.FeatureCollection {
  if (!buildings) return EMPTY;
  const feats: GeoJSON.Feature[] = [];
  for (const s of SITE_POINTS) {
    const p = placeOnRoof(s.lat, s.lon, buildings);
    if (!p) continue;
    feats.push({
      type: "Feature",
      properties: { site: s.id, name: s.name,
                    shift_m: Math.round(p.shift * 10) / 10 },
      geometry: { type: "Point", coordinates: [p.lon, p.lat] },
    });
  }
  return feats.length ? { type: "FeatureCollection", features: feats } : EMPTY;
}
