"use client";
/** 2D 俯視路線縮圖（issues/064）：**北方朝上、固定朝向、同場地同比例**。
 *
 * 為什麼不是把 3D 那張壓平就好：3D 縮圖（`MissionThumb3D`）的目的是「近似即時頁
 * 觀感」，而**認出這是哪一條路徑**是另一回事——同一塊場地的幾條路徑在斜角投影下
 * 形狀彼此很像，每張卡的 bearing 又各自被轉過，兩張卡之間不一定同一個朝向。
 *
 * 所以這張刻意沒有互動：不能轉、不能縮。要看高低差請看 3D 那張或進編輯頁。
 *
 * **比例尺由呼叫端給**（`radiusM`）：同一場地的幾條路徑餵同一個值，形狀才比得了。
 * 各自 fit 的話，一條 40 m 的與一條 400 m 的在卡片上一樣大。
 */
import { useId } from "react";

export interface Thumb2DPt { lat: number; lon: number; hold_s?: number | null }

const PAD = 12;          // viewBox 100×100 的邊距
const HALF = 50 - PAD;   // 中心到可畫範圍的邊

export default function MissionThumb2D({ wps, radiusM, className = "", onTap }: {
  wps?: Thumb2DPt[];
  /** 這張圖要涵蓋的半徑（公尺）。**同場地的每張卡給同一個值** */
  radiusM?: number | null;
  className?: string;
  onTap?: () => void;
}) {
  const clipId = useId();
  const pts = (wps ?? []).filter((w) => w.lat && w.lon);
  if (pts.length < 2) return <div className={`mthumb ${className}`} />;

  const lat0 = pts.reduce((t, w) => t + w.lat, 0) / pts.length;
  const lon0 = pts.reduce((t, w) => t + w.lon, 0) / pts.length;
  const k = 111320 * Math.cos((lat0 * Math.PI) / 180);
  const xy = pts.map((w) => ({
    x: (w.lon - lon0) * k, y: (w.lat - lat0) * 110574, hold: w.hold_s ?? 0,
  }));
  const own = Math.max(...xy.map((p) => Math.hypot(p.x, p.y)), 1);
  // 呼叫端沒給就用自己的（單獨顯示時合理）；給了就用給的，**即使這條比較小**
  const R = radiusM && radiusM > 0 ? radiusM : own;
  const s = HALF / R;
  // 北方朝上：螢幕 y 與北向相反
  const proj = (p: { x: number; y: number }): [number, number] =>
    [50 + p.x * s, 50 - p.y * s];
  const d = xy.map((p, i) => {
    const [x, y] = proj(p);
    return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join("");

  // 方向：每一段中點一個小箭頭（**看得出往哪飛**，不只看得出形狀）
  const arrows: { x: number; y: number; a: number }[] = [];
  for (let i = 1; i < xy.length; i++) {
    const [ax, ay] = proj(xy[i - 1]), [bx, by] = proj(xy[i]);
    const len = Math.hypot(bx - ax, by - ay);
    if (len < 8) continue;                       // 太短的段不放，免得糊成一團
    arrows.push({ x: (ax + bx) / 2, y: (ay + by) / 2,
                  a: (Math.atan2(by - ay, bx - ax) * 180) / Math.PI });
  }
  const holds = xy.filter((p) => p.hold > 0);
  const [fx, fy] = proj(xy[0]);
  const last = proj(xy[xy.length - 1]);
  // 比例尺：取一個「好讀」的長度（1/2/5 × 10^n），畫成一條線
  const nice = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]
    .filter((v) => v * s <= HALF).pop() ?? 1;

  return (
    <svg viewBox="0 0 100 100" className={`mthumb mthumb2d ${className}`}
      onClick={() => onTap?.()}>
      <defs><clipPath id={clipId}><rect width="100" height="100" rx="6" /></clipPath></defs>
      <g clipPath={`url(#${clipId})`}>
        <rect width="100" height="100" fill="#1b1a17" />
        <path d={d} stroke="#c2bfb3" strokeWidth="2" fill="none"
          strokeLinejoin="round" strokeLinecap="round" />
        {arrows.map((a, i) => (
          <path key={i} d="M-2.2,-1.8 L1.8,0 L-2.2,1.8 Z" fill="#8f8b80"
            transform={`translate(${a.x.toFixed(1)},${a.y.toFixed(1)}) rotate(${a.a.toFixed(0)})`} />
        ))}
        {/* 停留點（issues/062）：**規劃完要看得出來**，與飛過去的點不同 */}
        {holds.map((p, i) => {
          const [x, y] = proj(p);
          return <circle key={i} cx={x} cy={y} r="3.4" fill="none"
            stroke="#c2bfb3" strokeWidth="1" opacity="0.9" />;
        })}
        <circle cx={fx} cy={fy} r="2.8" fill="#0ca30c" />
        <rect x={last[0] - 2.2} y={last[1] - 2.2} width="4.4" height="4.4" fill="#c2bfb3" />
        {/* 北方朝上：一個固定的 N，提醒這張圖不會被轉 */}
        <text x="93" y="11" fill="#6b6862" fontSize="7" textAnchor="middle">N</text>
        <path d="M93,13 L93,19" stroke="#6b6862" strokeWidth="1" />
        <g opacity="0.85">
          <path d={`M6,94 h${(nice * s).toFixed(1)}`} stroke="#8f8b80" strokeWidth="1" />
          <text x="6" y="91" fill="#8f8b80" fontSize="6">{nice} m</text>
        </g>
      </g>
    </svg>
  );
}
