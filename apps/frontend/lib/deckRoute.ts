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

/** 泛用路徑層：per-path 色/寬（identity、dim、分級 run 都用它） */
export function pathsLayer(id: string, data: RouteRun[], pickable = false) {
  return new PathLayer<RouteRun>({
    id,
    data,
    getPath: (d) => d.path,
    getColor: (d) => d.color,
    getWidth: (d) => d.width ?? 3,   // 公尺（與原絲帶同寬）
    widthUnits: "meters",
    widthMinPixels: 2,               // 遠 zoom 仍可見
    jointRounded: true,
    capRounded: true,
    // billboard:true（2026-08-11 使用者二輪反饋修正）：false 時寬度只在
    // 水平面展開，爬升/下降段的段方向由趨近零的水平位移決定＝GPS 抖動
    // 雜訊，帶面亂跳＝垂直段顆粒。面向相機後垂直段方向在 3D 中良定義，
    // 俯視/傾斜觀感不變
    billboard: true,
    pickable,
  });
}

/** 全機隊尾跡 → 一個 SINR 分級 PathLayer（呼叫端每次 setProps 換新實例） */
export function routeLayer(id: string, trails: Record<string, TrailPoint[]>) {
  return pathsLayer(id, Object.values(trails).flatMap((tr) => sinrRuns(tr)));
}
