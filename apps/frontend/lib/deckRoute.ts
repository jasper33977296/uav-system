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

const VERT_M = 1.0;   // 水平位移小於此值的段＝視為垂直段（爬升/下降）

/** 依幾何把 run 拆成「斜/平段」與「垂直段」兩組（相鄰保留交界點）。
 *
 * 為什麼要拆（§2.4c #2/#4 的耦合）：
 *   - `billboard:false`＝寬度只在水平面展開。**斜/平段**因此有正確的帶面
 *     與明確的分級色；但**垂直段**的水平位移趨近零，展開方向由 GPS 抖動
 *     決定＝帶面亂跳（2026-08-11 使用者反饋的「垂直段顆粒」）。
 *   - `billboard:true`＝帶面永遠正對相機。垂直段因此看得見，但斜向段被
 *     拉平成灰白扁帶、**分級色被亮邊吃掉**（2026-08-12 使用者反饋的「粗糙」）。
 * 兩個模式各自只在一種幾何上正確——所以不是二選一，是**依幾何分流**。
 */
function splitByGeometry(runs: RouteRun[]): { flat: RouteRun[]; vert: RouteRun[] } {
  const flat: RouteRun[] = [], vert: RouteRun[] = [];
  for (const run of runs) {
    let cur: [number, number, number][] = [];
    let curVert: boolean | null = null;
    const flush = () => {
      if (cur.length >= 2) (curVert ? vert : flat).push({ ...run, path: cur });
    };
    for (let i = 1; i < run.path.length; i++) {
      const a = run.path[i - 1], b = run.path[i];
      const k = 111320 * Math.cos((b[1] * Math.PI) / 180);
      const horiz = Math.hypot((b[0] - a[0]) * k, (b[1] - a[1]) * 110574);
      const isVert = horiz < VERT_M;
      if (curVert === null) { cur = [a, b]; curVert = isVert; continue; }
      if (isVert === curVert) { cur.push(b); continue; }
      flush();
      cur = [a, b];          // 交界點兩邊共用，視覺不斷開
      curVert = isVert;
    }
    flush();
  }
  return { flat, vert };
}

const PATH_BASE = {
  getPath: (d: RouteRun) => d.path,
  getColor: (d: RouteRun) => d.color,
  getWidth: (d: RouteRun) => d.width ?? 3,   // 公尺（與原絲帶同寬）
  widthUnits: "meters" as const,
  // 縮放自適應（使用者第四輪）：物理錨定＋螢幕像素夾限——中間隨縮放
  // 連續變化保留距離感，兩端有界（近看不肥帶、遠看不消失）
  widthMinPixels: 4,     // §2.4c：3→4，遠看仍有帶感
  widthMaxPixels: 8,
  jointRounded: true,
  capRounded: true,
};

/** 泛用路徑層：per-path 色/寬（identity、dim、分級 run 都用它）。
 * 回傳**兩層**——斜/平段（billboard:false，保分級色）與垂直段
 * （billboard:true，否則看不見），呼叫端展開進 layers 陣列。 */
export function pathsLayer(id: string, data: RouteRun[], pickable = false) {
  const { flat, vert } = splitByGeometry(data);
  return [
    new PathLayer<RouteRun>({ ...PATH_BASE, id, data: flat,
      billboard: false, pickable }),
    new PathLayer<RouteRun>({ ...PATH_BASE, id: `${id}-vert`, data: vert,
      billboard: true, pickable }),
  ];
}

/** 全機隊尾跡 → 一個 SINR 分級 PathLayer（呼叫端每次 setProps 換新實例） */
export function routeLayer(id: string, trails: Record<string, TrailPoint[]>) {
  return pathsLayer(id, Object.values(trails).flatMap((tr) => sinrRuns(tr)));
}
