/** deck.gl 航跡路徑層（doc/route-render-tool-eval.md 定案，取代 fill-extrusion 絲帶）。
 *
 * 解掉使用者的兩個抱怨：
 *   - 顆粒感：PathLayer 3D 座標＋公尺寬＋jointRounded——斜向段是連續斜帶，
 *     不再是水平樓板量化出的階梯
 *   - 閃爍：deck.gl attribute 更新是同幀 GPU buffer 寫入，
 *     無 maplibre setData 整源替換的幀溝
 *
 * 顏色：PathLayer 是 per-path 上色（無 per-vertex）→「同 SINR 分級的連續段
 * ＝一條 path」run 分割，相鄰 run 共用交界點、rounded joints 讓交界無縫。
 * 與誠實原則「分級不插值」同構。即時／回放／比較三頁共用本模組。
 */
import { PathLayer } from "@deck.gl/layers";

import { classifySinr } from "@/lib/signal";
import type { TrailPoint } from "@/lib/store";

const UNKNOWN = "#8f8b80";   // 無 SINR 樣本的段落（＝muted，不造假）

type Rgba = [number, number, number, number];
export interface RouteRun {
  path: [number, number, number][];
  color: Rgba;
  width?: number;        // 公尺；預設 3
  sid?: string;          // 拾取用（比較頁點絲帶選架次）
}

export const rgba = (hex: string): Rgba => [
  parseInt(hex.slice(1, 3), 16),
  parseInt(hex.slice(3, 5), 16),
  parseInt(hex.slice(5, 7), 16),
  255,
];

/** 一條尾跡 → 同分級 runs（相鄰 run 共用交界點以保視覺連續） */
export function sinrRuns(pts: TrailPoint[]): RouteRun[] {
  const runs: RouteRun[] = [];
  let curKey: string | null = null;
  let curPath: [number, number, number][] = [];
  for (const p of pts) {
    const cls = p.sinr == null
      ? { key: "unknown", color: UNKNOWN }
      : classifySinr(p.sinr);
    const pos: [number, number, number] = [p.lon, p.lat, p.alt ?? 0];
    if (curKey === cls.key) {
      // run 內抽稀 xy 近重合點（<0.5m 水平位移只留高度變化端點）：
      // 垂直爬升段的 GPS 抖動點串會讓段方向變成雜訊（顆粒感來源之一）
      const prev = curPath[curPath.length - 1];
      if (prev) {
        const k = 111320 * Math.cos((p.lat * Math.PI) / 180);
        const horiz = Math.hypot((pos[0] - prev[0]) * k, (pos[1] - prev[1]) * 110574);
        if (horiz < 0.5 && curPath.length >= 2) {
          const prev2 = curPath[curPath.length - 2];
          const horiz2 = Math.hypot((prev[0] - prev2[0]) * k, (prev[1] - prev2[1]) * 110574);
          if (horiz2 < 0.5) { curPath[curPath.length - 1] = pos; continue; }
        }
      }
      curPath.push(pos);
      continue;
    }
    const boundary = curPath[curPath.length - 1];
    curPath = boundary ? [boundary, pos] : [pos];
    curKey = cls.key;
    runs.push({ path: curPath, color: rgba(cls.color) });
  }
  return runs.filter((r) => r.path.length >= 2);
}

const PATH_BASE = {
  getPath: (d: RouteRun) => d.path,
  getColor: (d: RouteRun) => d.color,
  getWidth: (d: RouteRun) => d.width ?? 3,   // 公尺（與原絲帶同寬）
  widthUnits: "meters" as const,
  // 縮放自適應（使用者第四輪）：物理錨定＋螢幕像素夾限——中間隨縮放
  // 連續變化保留距離感，兩端有界（近看不肥帶、遠看不消失）。
  // 上限 8→5（使用者第五輪「粗細不一」）：8px 的餘裕在常用縮放下只有
  // 近端段吃得到，遠端段被壓到 4px——同一條線因此忽粗忽細。上下界收到
  // 4–5px 後，整條線在任何縮放下的螢幕寬度幾乎一致
  widthMinPixels: 4,     // §2.4c：3→4，遠看仍有帶感
  widthMaxPixels: 5,
  jointRounded: true,
  capRounded: true,
};

/** 泛用路徑層：per-path 色/寬（identity、dim、分級 run 都用它）。
 *
 * **全段 `billboard: true`（帶面永遠正對相機）**——使用者第五輪反饋
 * 「路徑線有些地方粗、有些地方細」的解。先前依幾何分流（斜/平段
 * billboard:false、垂直段 billboard:true）的兩層寫法，粗細不一有三個來源，
 * 而三個都出在 `billboard: false`：
 *   1. 寬度只在水平面展開，投影到螢幕後被俯角壓縮——**壓縮量取決於航向**：
 *      朝向相機的段是滿寬，橫過畫面的段被壓成細線。同一條航跡因此忽粗忽細。
 *   2. 圓形 joint/cap 躺在水平面上，橫向段的線被壓細、接點卻仍是滿徑的
 *      橢圓——就是截圖上那一顆顆比線還粗的瘤。
 *   3. 兩層各自的規則不同，交界處必然對不齊。
 * 代價：帶面不再表達「這段在空中怎麼躺」。但那個資訊本來就被 §1 的
 * 俯角壓縮扭曲得讀不出來，換到的是一條寬度處處相同、看得出分級色的線。
 * 垂直段可見（原本分流的目的）由 billboard 一併保住。 */
export function pathsLayer(id: string, data: RouteRun[], pickable = false) {
  return [
    new PathLayer<RouteRun>({ ...PATH_BASE, id, data, billboard: true, pickable }),
  ];
}

/** 全機隊尾跡 → 一個 SINR 分級 PathLayer（呼叫端每次 setProps 換新實例） */
export function routeLayer(id: string, trails: Record<string, TrailPoint[]>) {
  return pathsLayer(id, Object.values(trails).flatMap((tr) => sinrRuns(tr)));
}
