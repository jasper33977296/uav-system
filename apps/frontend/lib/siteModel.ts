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

/** 兩座設備。座標是使用者 2026-10-13 給的，`place` 是同一天後來給的**擺放
 *  描述**——「一個接近建築物的邊邊，一個接近建築物中心」。
 *
 *  **描述蓋過座標**，因為使用者同時裁定「座標和輪廓的落差先不管，之後再量」：
 *  原始兩點只差 20.7 m 而且都落在輪廓外；照描述擺之後相距 33.1 m。挪了多少
 *  仍然記在 `shift_m` 裡，之後量到真座標可以對回去。 */
export const SITE_POINTS: {
  id: string; name: string; lat: number; lon: number; place: "edge" | "center";
}[] = [
  { id: "a", name: "A", lat: 24.774047, lon: 121.045195, place: "edge" },
  { id: "b", name: "B", lat: 24.774073, lon: 121.045398, place: "center" },
];

const MAST_M = 6;        // 桿高（屋頂面往上）
const MAST_W = 0.5;      // 桿寬
const PANEL_H = 1.6;     // 天線面板高
const PANEL_W = 0.45;    // 面板寬
const PANEL_T = 0.22;    // 面板厚
const PANEL_R = 1.0;     // 面板離桿心的距離
const PANEL_TOP_DROP = 0.6;   // 面板頂距桿頂
/** `place: "edge"` 的那一支往輪廓內讓多少。
 *
 * 改過兩次：1.5 m 只夠「不在輪廓外」，但那是屋簷線上，傾斜視角看起來仍像
 * 掛在邊緣（使用者回報「還是飄在 3D 建築物外」）；6 m 站得穩，但使用者接著
 * 指定這一支要「接近建築物的邊邊」——所以收回 2 m：既在屋頂面上，也確實
 * 靠邊。另一支改用建築物中心（見 `placeOnRoof`）。 */
const INSET_M = 2;
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

/** 多邊形的面積形心（公尺平面）。 */
function polyCentroid(ring: [number, number][]): [number, number] {
  let a2 = 0, cx = 0, cy = 0;
  for (let i = 0; i < ring.length; i++) {
    const [x1, y1] = ring[i], [x2, y2] = ring[(i + 1) % ring.length];
    const cr = x1 * y2 - x2 * y1;
    a2 += cr; cx += (x1 + x2) * cr; cy += (y1 + y2) * cr;
  }
  if (a2 === 0) return [0, 0];
  return [cx / (3 * a2), cy / (3 * a2)];
}

/** 把一個點放到腳下那棟樓的屋頂上。放不上去就回 null。
 *
 * `mode` 是使用者 2026-10-13 給的擺放描述：
 *   `edge`   ＝ 接近建築物的邊邊——挪進輪廓 `INSET_M`（2 m）就停。
 *   `center` ＝ 接近建築物中心——用那棟樓的**面積形心**。形心落在輪廓外時
 *               （L 形、弧形的樓常常如此）退回 `edge` 的做法，不硬放。
 *
 * **這是顯示用的位置。** 使用者裁定座標與輪廓的落差「先不管，之後再量」，
 * 所以這裡照描述擺，`shift` 記下一共離原座標多遠。 */
function placeOnRoof(lat: number, lon: number,
                     buildings: GeoJSON.FeatureCollection,
                     mode: "edge" | "center" = "edge"): Placed | null {
  const ml = mLon(lat);
  let best: { d: number; q: { x: number; y: number }; roof: number;
              ring: [number, number][] } | null = null;
  let inside: { roof: number; ring: [number, number][] } | null = null;
  for (const f of buildings.features) {
    const g = f.geometry;
    if (!g || g.type !== "Polygon") continue;
    const h = (f.properties as { height_m?: number | null } | null)?.height_m;
    // **高度未知的樓不接**：不知道屋頂在哪個高度，就沒有屋頂可站
    if (h == null) continue;
    const ring: [number, number][] = (g.coordinates[0] as number[][]).map(
      ([lo, la]) => [(lo - lon) * ml, (la - lat) * M_LAT]);
    if (inRing(0, 0, ring)) { inside = { roof: h, ring }; break; }
    for (let i = 0; i < ring.length - 1; i++) {
      const n = nearestOnSeg(0, 0, ring[i][0], ring[i][1],
                             ring[i + 1][0], ring[i + 1][1]);
      if (!best || n.d < best.d) {
        best = { d: n.d, q: { x: n.x, y: n.y }, roof: h, ring };
      }
    }
  }
  const home = inside ?? (best && best.d <= MAX_SNAP_M
    ? { roof: best.roof, ring: best.ring } : null);
  if (!home) return null;
  if (mode === "center") {
    const [cxm, cym] = polyCentroid(home.ring);
    // 形心在輪廓內才用——弧形／L 形的樓，形心可能根本不在樓上
    if (inRing(cxm, cym, home.ring)) {
      return { lat: lat + cym / M_LAT, lon: lon + cxm / ml, roof: home.roof,
               shift: Math.hypot(cxm, cym) };
    }
  }
  if (inside) return { lat, lon, roof: inside.roof, shift: 0 };
  const len = Math.hypot(best!.q.x, best!.q.y) || 1;
  const x = best!.q.x + (best!.q.x / len) * INSET_M;
  const y = best!.q.y + (best!.q.y / len) * INSET_M;
  return { lat: lat + y / M_LAT, lon: lon + x / ml, roof: best!.roof,
           shift: Math.hypot(x, y) };
}

/** CELL 的示意半徑（公尺）。**這是示意，不是量到的覆蓋範圍。**
 *
 * 2026-10-13 使用者：「覆蓋範圍太大了」「縮小 1/3」→ 250 × 2/3 ≈ **167**。
 *
 * **為什麼不是縮到 1/3（83 m）**：交會點在 117 m（由間距 33.1 m、張角 120°、
 * 岔開 52° 決定），83 m 的扇形根本碰不到彼此——**干擾區會整個消失**，而且
 * 最遠航點 wp1（離兩站 140／150 m）也會掉出覆蓋，與「那個頂點要已經在干擾
 * 區內」直接相反。167 m 兩者都還成立。
 *
 * 這個半徑原本是從量測外推來的：實測 4343 筆的中位 **−89／−90 dBm @ ~105 m**
 * ——這一段是量到的；往外推要**假設**一個路徑損耗指數，n=2.7 時 250 m 相當於
 * −99 dBm 的等值線。縮到 167 m 之後，同一組假設下相當於 **−94 dBm**。
 * 但現在這個數字是**指定的畫面大小**，不是某個門檻算出來的。
 *
 * **為什麼斜率只能假設**：全部樣本只跨 82–141 m（1.7 倍，不到 0.2 個數量級），
 * 那段距離的路徑損耗只有 4–8 dB，而樣本散佈是 ±6.6–7 dB。實際擬合出來的
 * 衰減指數是**負的**（R²≈0.00／0.11），也就是「越遠越強」——物理上不可能。
 * 訊號埋在雜訊裡，所以斜率量不出來。 */
export const CELL_R_M = 167;

/** CELL 的顏色。2026-10-13 使用者：「我不喜歡紫色 換成藍跟綠吧」。
 *
 * 兩座天線只差 33 m、扇形大面積重疊，**重疊處只能靠色相分辨**，所以兩色要
 * 夠遠：藍 221° vs 綠 145°，差 76°。
 *
 * **這張圖上已經有藍也有綠，所以挑的是「同色相但明顯不同調」**：
 *   機體圖示 #3987e5（藍，較淡）  → 這裡用更深更濃的 #1b6ef3
 *   SINR「良好」#0ca30c（偏黃的深綠）
 *   圍欄 #4a9d5f（濁綠）、植被 #5c8a3c（橄欖）→ 這裡用更亮的 #2ee07a
 * 這幾個在畫面上都是細線或小圖示，扇形是大面積的淡色塊，加上圖例有字
 * （§0.1：顏色永不單獨傳達語意），實際上分得出來。鮮紅仍留給干擾區。 */
export const CELL_COLORS: Record<string, string> = { a: "#1b6ef3", b: "#2ee07a" };

/** 兩支的**共同軸**（度，正北為 0、順時針）＝ 兩支中點朝最遠航點 wp1。
 *  各自的指向是這個軸往外岔開 `CELL_SPLIT_DEG`，見 `cellAim()`。 */
export const CELL_AIM_DEG = 136.6;

/** 兩支各自往外岔開的角度。**這個數字決定交會點在多遠。**
 *
 * 平行指向時交會點 ≈ 間距/(2·tan(張角/2))，張角一開到 120° 就只剩 9 m——
 * 角度越大兩束越早相交。要把交會點推遠只能靠岔開，讓「A 朝 B 那側的邊」與
 * 「B 朝 A 那側的邊」幾乎平行。
 *
 * 2026-10-13 使用者第二次修正：「那個頂點所在位置要已經在干擾區內了」——
 * 最遠航點 wp1 必須落在干擾區**內**。實測掃描（間距 33.1 m、張角 120°）：
 *
 *     岔開   交會點   wp1 在區內   離邊界   起飛→wp1  wp1→wp2  wp2→起飛
 *     50°    94 m      是          9 m      100%      25%       5%
 *     51°   105 m      是          6 m       97%      19%       0%
 *     52°   117 m      是          4 m       71%      13%       0%
 *     53°   133 m      是          2 m       29%       6%       0%
 *     55°   185 m      **否**      —          0%       0%       0%
 *
 * 取 **52°**：wp1 進區 4 m（53° 只有 2 m，在幾何誤差裡太勉強），而
 * `wp2→起飛` 仍然完全在外。
 *
 * **`wp1→wp2` 的那 13% 躲不掉**：wp1 既然在區內，交於它的兩條邊必然各有
 * 一小段在區內。要兩條邊都 0%，就得把 wp1 推出區外——與這次的要求相反。 */
export const CELL_SPLIT_DEG = 52;

/** 這一支的指向。A（邊緣支）往軸的一側、B（中心支）往另一側。 */
export function cellAim(id: string): number {
  return (CELL_AIM_DEG + (id === "a" ? CELL_SPLIT_DEG : -CELL_SPLIT_DEG) + 360) % 360;
}

/** 干擾區的顏色。**鮮紅，而且與 SINR「瀕斷」的深紅 #a01818 分得開**——
 *  那條是量出來的軌跡分級，這塊是推出來的範圍，不能看起來像同一件事。 */
export const ZONE_COLOR = "#ff3b30";

/** 扇形張角（度）。使用者 2026-10-13 指定 120°（「算到120度」）。
 *
 * 一般三扇區基站是 65° 上下，120° 是雙扇區的常見值——但**這仍然不是量到的
 * 天線規格**，是指定的畫面效果。交會點改由 `CELL_SPLIT_DEG` 控制。 */
export const CELL_BEAM_DEG = 120;

/** 光束的厚度（公尺）。**不是實心楔形，是一片有厚度的斜板。**
 *
 * 2026-10-13 使用者回報「目前的訊號會把建築物建模吃掉，建築物的顯示優先權
 * 大於訊號」。原因是原本的楔形從地面實心填到斜頂——天線那端有 32 m 高，
 * 等於在樓前面豎了一道牆。fill-extrusion 之間會互相做深度測試，牆在相機與
 * 樓之間時，**後畫的樓也贏不了**（深度測試先把它刷掉）。
 *
 * 改成薄板之後，光束在螢幕上只佔一條帶，遮不住樓；而且它本來就比較像一束
 * 投射，不像一塊果凍。 */
const BEAM_SLAB_M = 4;

/** 光束往前推多少才開始畫（公尺）——**只是讓開桅桿本身**。
 *
 * 原本這裡會動態量「跨過腳下那棟樓要多遠」，那是實心楔形時代的補救：楔形
 * 從地面填到斜頂，埋在樓裡的部分會從牆面穿出來。改成薄板之後不需要了，而且
 * 張角開到 120° 之後那個做法會**反過來壞事**——實測掃描顯示 120° 的光束會
 * 掃過 51館／58館，統一切除半徑會變成 120 m，等於砍掉半條光束。
 *
 * 薄板不需要切：它低於屋頂時就在樓**裡面**，被樓擋住——那正是使用者要的
 * 「建築物的顯示優先權大於訊號」。會吃掉樓的是擋在相機與樓之間的那道牆，
 * 而薄板不會形成牆。 */
const BEAM_START_M = 3;

/** 立體扇形切成幾段。fill-extrusion 的每一塊只有一個高度，所以「從天線往外
 *  下斜」是用多段階梯逼近的——段數只影響平不平滑，不影響它是推估這件事。
 *
 *  14 → 40（2026-10-13 使用者：「現在的訊號格子很粗糙」）。250 m 切 40 段，
 *  一段 6 m、落差 0.8 m，階梯在一般縮放下看不出來。 */
const CELL_BANDS = 40;

/** 訊號的衰減漸層：離天線越遠越淡。
 *
 * **為什麼把透明度寫進顏色**：MapLibre 的 `fill-extrusion-opacity` 不吃
 * data-driven 運算式（整個圖層只能一個值），但 `fill-extrusion-color` 吃，
 * 而且顏色可以帶 alpha。所以漸層做在顏色的 alpha 上，圖層 opacity 設 1。
 *
 * 曲線是**視覺上的衰減示意**，不是任何傳播模型的輸出——真正的衰減指數量不
 * 出來（見 `CELL_R_M`），這裡只是讓它看起來像訊號，而不是一塊色板。 */
function fade(hex: string, r: number, a0: number): string {
  const t = Math.max(0, 1 - r / CELL_R_M);
  const a = Math.max(0.03, a0 * Math.pow(t, 0.9));
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a.toFixed(3)})`;
}

/** 最靠近天線處的不透明度。2026-10-13 使用者指定 50% → 再改 **35%**。
 *  立體與地面都取 0.35，往外照 `fade()` 的曲線遞減。 */
const FADE_VOL = 0.35;
const FADE_GROUND = 0.35;

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
    const p = placeOnRoof(s.lat, s.lon, buildings, s.place);
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

/** 以 (lat, lon) 為心的環狀扇形（r0→r1、方位 a0→a1 度）的外環。
 *  r0 = 0 時退化成一般扇形（圓心是一個點）。 */
function sectorRing(lat: number, lon: number, r0: number, r1: number,
                    a0: number, a1: number): [number, number][] {
  const ml = mLon(lat);
  const N = 36;
  // 方位角是「正北 0、順時針」，而三角函數是「正東 0、逆時針」
  const pt = (r: number, degN: number): [number, number] => {
    const t = ((90 - degN) * Math.PI) / 180;
    return [lon + (r * Math.cos(t)) / ml, lat + (r * Math.sin(t)) / M_LAT];
  };
  const ring: [number, number][] = [];
  for (let i = 0; i <= N; i++) ring.push(pt(r1, a0 + ((a1 - a0) * i) / N));
  if (r0 > 0) {
    for (let i = N; i >= 0; i--) ring.push(pt(r0, a0 + ((a1 - a0) * i) / N));
  } else {
    ring.push(pt(0, a0));
  }
  ring.push(ring[0]);
  return ring;
}

/** 兩個 CELL 落在地面上的扇形：**切成同心環段、往外漸淡**，不是一塊色板
 *  （2026-10-13 使用者：「現在的訊號格子很粗糙 換成比較像訊號的視覺呈現
 *  方式」）。最後再多一個整塊的 `edge` feature，給虛線邊界用。
 *
 * 扇心跟著模型實際站的位置走，**不是另外一組座標**：畫面上只能有一個
 * 「它在哪裡」。半徑見 `CELL_R_M`、方位見 `cellAim()`——量到的只有錨點，
 * 往外的斜率、方位、張角全是假設。 */
export function siteCellsFC(buildings: GeoJSON.FeatureCollection | null)
    : GeoJSON.FeatureCollection {
  if (!buildings) return EMPTY;
  const feats: GeoJSON.Feature[] = [];
  for (const s of SITE_POINTS) {
    const p = placeOnRoof(s.lat, s.lon, buildings, s.place);
    if (!p) continue;
    const hex = CELL_COLORS[s.id] ?? "#3f7f8c";
    const a0 = cellAim(s.id) - CELL_BEAM_DEG / 2;
    const a1 = cellAim(s.id) + CELL_BEAM_DEG / 2;
    for (let k = 0; k < CELL_BANDS; k++) {
      const r0 = BEAM_START_M + ((CELL_R_M - BEAM_START_M) * k) / CELL_BANDS;
      const r1 = BEAM_START_M + ((CELL_R_M - BEAM_START_M) * (k + 1)) / CELL_BANDS;
      feats.push({
        type: "Feature",
        properties: { site: s.id, name: s.name, band: k,
                      rgba: fade(hex, (r0 + r1) / 2, FADE_GROUND) },
        geometry: { type: "Polygon",
                    coordinates: [sectorRing(p.lat, p.lon, r0, r1, a0, a1)] },
      });
    }
    feats.push({
      type: "Feature",
      properties: { site: s.id, name: s.name, edge: 1, color: hex },
      geometry: { type: "Polygon", coordinates: [sectorRing(
        p.lat, p.lon, BEAM_START_M, CELL_R_M, a0, a1)] },
    });
  }
  return feats.length ? { type: "FeatureCollection", features: feats } : EMPTY;
}

// ── 干擾區＝兩個扇形的重疊（2026-10-13 使用者裁定）──────────────────────
// 系統裡沒有場地干擾區的定義（真表 interference_zones 2026-08-10 砍掉，只剩
// link_sim 在台北的模擬常數；4343 筆實測的 in_interference_zone 全是 NULL）。
// 使用者裁定「兩個 cell 重疊範圍就是干擾區」——所以它是**算出來的幾何**，
// 不是量出來的區域，而且**它繼承兩個扇形的每一個假設**：半徑來自假設的
// 衰減指數、方位是指定的、張角 65° 是常見值。圖例的 ⓘ 要把這件事講出來，
// 因為紅色讀起來就是「這裡有事」。

type XY = [number, number];

/** 以 (lat0, lon0) 為原點的公尺平面。扇形只有幾百公尺，平面近似綽綽有餘。 */
function toXY(lat: number, lon: number, lat0: number, lon0: number): XY {
  return [(lon - lon0) * mLon(lat0), (lat - lat0) * M_LAT];
}
function toLL(p: XY, lat0: number, lon0: number): [number, number] {
  return [lon0 + p[0] / mLon(lat0), lat0 + p[1] / M_LAT];
}

/** 公尺平面上的扇形（方位角：正北 0、順時針）。r0 = 0 時是一般扇形。 */
function sectorXY(cx: number, cy: number, r0: number, r1: number,
                  a0: number, a1: number): XY[] {
  const N = 36;
  const pt = (r: number, degN: number): XY => {
    const t = ((90 - degN) * Math.PI) / 180;
    return [cx + r * Math.cos(t), cy + r * Math.sin(t)];
  };
  const ring: XY[] = [];
  for (let i = 0; i <= N; i++) ring.push(pt(r1, a0 + ((a1 - a0) * i) / N));
  if (r0 > 0) for (let i = N; i >= 0; i--) ring.push(pt(r0, a0 + ((a1 - a0) * i) / N));
  else ring.push(pt(0, a0));
  return ring;
}

/** Sutherland–Hodgman：用**凸**多邊形 clip 去裁 subject（subject 可以凹）。
 *  扇形張角 65° < 180°，所以兩個扇形都是凸的——交集算得出精確解，
 *  不需要拉一個多邊形布林運算的函式庫進來。 */
function clipConvex(subject: XY[], clip: XY[]): XY[] {
  // clip 的繞向決定「內側」是哪一邊，先量一次有號面積
  let a2 = 0;
  for (let i = 0; i < clip.length; i++) {
    const [x1, y1] = clip[i], [x2, y2] = clip[(i + 1) % clip.length];
    a2 += x1 * y2 - x2 * y1;
  }
  const ccw = a2 > 0;
  let out = subject;
  for (let i = 0; i < clip.length && out.length; i++) {
    const A = clip[i], B = clip[(i + 1) % clip.length];
    const side = (p: XY) => {
      const c = (B[0] - A[0]) * (p[1] - A[1]) - (B[1] - A[1]) * (p[0] - A[0]);
      return ccw ? c : -c;
    };
    const inp = out;
    out = [];
    for (let j = 0; j < inp.length; j++) {
      const P = inp[j], Q = inp[(j + 1) % inp.length];
      const sp = side(P), sq = side(Q);
      if (sp >= 0) out.push(P);
      if ((sp >= 0) !== (sq >= 0)) {
        const t = sp / (sp - sq);
        out.push([P[0] + t * (Q[0] - P[0]), P[1] + t * (Q[1] - P[1])]);
      }
    }
  }
  return out;
}

function centroid(ring: XY[]): XY {
  let x = 0, y = 0;
  for (const p of ring) { x += p[0]; y += p[1]; }
  return [x / ring.length, y / ring.length];
}

interface Site { lat: number; lon: number; roof: number; id: string }

function placedSites(buildings: GeoJSON.FeatureCollection): Site[] {
  const out: Site[] = [];
  for (const s of SITE_POINTS) {
    const p = placeOnRoof(s.lat, s.lon, buildings, s.place);
    if (p) out.push({ lat: p.lat, lon: p.lon, roof: p.roof, id: s.id });
  }
  return out;
}

/** 干擾區：兩個立體扇形的重疊，切成同心環段好做出和扇形一致的下斜。
 *
 * 每一段的高度取**兩個扇形在那一段形心處的較低者**——重疊只到比較矮的那個
 * 為止。兩座天線只差 20.7 m，所以兩者其實差不到 10%，但照算不花什麼力氣。 */
export function interferenceZoneFC(buildings: GeoJSON.FeatureCollection | null)
    : GeoJSON.FeatureCollection {
  if (!buildings) return EMPTY;
  const sites = placedSites(buildings);
  if (sites.length < 2) return EMPTY;      // 要兩個才有重疊
  const [A, B] = sites;
  const lat0 = A.lat, lon0 = A.lon;
  const bxy = toXY(B.lat, B.lon, lat0, lon0);
  const hAnt = (s: Site) => s.roof + MAST_M - PANEL_TOP_DROP - PANEL_H / 2;
  const top = (c: XY, cx: number, cy: number, s: Site) => {
    const d = Math.hypot(c[0] - cx, c[1] - cy);
    return Math.max(0, hAnt(s) * (1 - d / CELL_R_M));
  };
  const sectorB = sectorXY(bxy[0], bxy[1], 0, CELL_R_M,
                           cellAim(B.id) - CELL_BEAM_DEG / 2,
                           cellAim(B.id) + CELL_BEAM_DEG / 2);
  const feats: GeoJSON.Feature[] = [];
  for (let k = 0; k < CELL_BANDS; k++) {
    const r0 = (CELL_R_M * k) / CELL_BANDS;
    const r1 = (CELL_R_M * (k + 1)) / CELL_BANDS;
    const bandA = sectorXY(0, 0, r0, r1, cellAim(A.id) - CELL_BEAM_DEG / 2,
                           cellAim(A.id) + CELL_BEAM_DEG / 2);
    const hit = clipConvex(bandA, sectorB);
    if (hit.length < 3) continue;
    const c = centroid(hit);
    const h = Math.min(top(c, 0, 0, A), top(c, bxy[0], bxy[1], B));
    if (h <= 0.05) continue;
    const ring = hit.map((p) => toLL(p, lat0, lon0));
    feats.push({
      type: "Feature",
      // **比 cell 的薄板上下各多 0.4 m**：兩者在重疊處的面本來完全重合，
      // 深度測試分不出先後會閃爍（z-fighting），看起來就是「分不出哪邊是
      // 重疊、哪邊是單一 cell」。讓紅色那片整個包住它，先後就定了。
      properties: { band: k, top: h + 0.4,
                    base: Math.max(0, h - BEAM_SLAB_M - 0.4) },
      geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] },
    });
  }
  return feats.length ? { type: "FeatureCollection", features: feats } : EMPTY;
}

/** 干擾區落在地面上的那一塊（整塊重疊，給填色與邊界用）。 */
export function interferenceFootprintFC(buildings: GeoJSON.FeatureCollection | null)
    : GeoJSON.FeatureCollection {
  if (!buildings) return EMPTY;
  const sites = placedSites(buildings);
  if (sites.length < 2) return EMPTY;
  const [A, B] = sites;
  const lat0 = A.lat, lon0 = A.lon;
  const bxy = toXY(B.lat, B.lon, lat0, lon0);
  const hit = clipConvex(
    sectorXY(0, 0, 0, CELL_R_M, cellAim(A.id) - CELL_BEAM_DEG / 2,
             cellAim(A.id) + CELL_BEAM_DEG / 2),
    sectorXY(bxy[0], bxy[1], 0, CELL_R_M, cellAim(B.id) - CELL_BEAM_DEG / 2,
             cellAim(B.id) + CELL_BEAM_DEG / 2));
  if (hit.length < 3) return EMPTY;
  const ring = hit.map((p) => toLL(p, lat0, lon0));
  return { type: "FeatureCollection", features: [{
    type: "Feature", properties: {},
    geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] } }] };
}

/** 兩個 CELL 的**立體**扇形：從天線往外、往下斜到地面的那一片斜板。
 *
 * 使用者 2026-10-13：「把 CELL 畫成立體的，不要只有平面圓，然後直接以扇形
 * 投射到草皮區域」。做法是把扇形切成 `CELL_BANDS` 段同心環，每段的頂高從
 * 天線高度線性遞減到 0——fill-extrusion 一塊只有一個高度，斜面只能這樣逼近。
 *
 * **這個楔形不是波束形狀，是一個投射示意。** 真的波束有垂直張角、下傾角、
 * 旁瓣，還會被樓擋住；這裡一個都沒有算。它回答的是「這個方向、這個距離」，
 * 不是「這裡收得到多少 dB」。 */
export function siteCellVolFC(buildings: GeoJSON.FeatureCollection | null)
    : GeoJSON.FeatureCollection {
  if (!buildings) return EMPTY;
  const feats: GeoJSON.Feature[] = [];
  for (const s of SITE_POINTS) {
    const p = placeOnRoof(s.lat, s.lon, buildings, s.place);
    if (!p) continue;
    // 天線面板的中心高度：屋頂 ＋ 桿高 − 面板頂距桿頂 − 半個面板
    const hAnt = p.roof + MAST_M - PANEL_TOP_DROP - PANEL_H / 2;
    const a0 = cellAim(s.id) - CELL_BEAM_DEG / 2;
    const a1 = cellAim(s.id) + CELL_BEAM_DEG / 2;
    const rc = BEAM_START_M;
    for (let k = 0; k < CELL_BANDS; k++) {
      const r0 = rc + ((CELL_R_M - rc) * k) / CELL_BANDS;
      const r1 = rc + ((CELL_R_M - rc) * (k + 1)) / CELL_BANDS;
      const top = hAnt * (1 - (r0 + r1) / 2 / CELL_R_M);
      feats.push({
        type: "Feature",
        properties: { site: s.id, name: s.name, band: k, top,
                      // 薄板：底 ＝ 頂減厚度，不是從地面實心填上來
                      base: Math.max(0, top - BEAM_SLAB_M),
                      // 漸層寫在 alpha 裡（圖層 opacity 不吃 data-driven）
                      rgba: fade(CELL_COLORS[s.id] ?? "#3f7f8c",
                                 (r0 + r1) / 2, FADE_VOL) },
        geometry: { type: "Polygon",
                    coordinates: [sectorRing(p.lat, p.lon, r0, r1, a0, a1)] },
      });
    }
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
    const p = placeOnRoof(s.lat, s.lon, buildings, s.place);
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
