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
export interface RouteRun { path: [number, number, number][]; color: Rgba }

const rgba = (hex: string): Rgba => [
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

/** 全機隊尾跡 → 一個 PathLayer（呼叫端每次 setProps 換新實例） */
export function routeLayer(id: string, trails: Record<string, TrailPoint[]>) {
  const data = Object.values(trails).flatMap((tr) => sinrRuns(tr));
  return new PathLayer<RouteRun>({
    id,
    data,
    getPath: (d) => d.path,
    getColor: (d) => d.color,
    getWidth: 3,             // 公尺（與原絲帶同寬）
    widthUnits: "meters",
    widthMinPixels: 2,       // 遠 zoom 仍可見
    jointRounded: true,
    capRounded: true,
    billboard: false,        // 平面帶固定在世界座標，不面向相機
  });
}
