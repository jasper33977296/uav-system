"use client";
/** 等距投影 3D 路線縮圖（ui-spec §4 B 案定案）：零 WebGL——以固定 pitch 55°
 * 等距投影把 waypoints 畫進 SVG，近似即時頁觀感（暖畫布＋網格＋灰絲帶＋
 * 起點綠圓/終點方塊）。全卡可同時互動（無 context 上限）。
 *
 * 互動規格：拖曳＝繞路徑中心旋轉（bearing 唯一變數）；無平移/縮放；
 * 不掛 wheel（滾輪自然穿透捲頁）。鏡頭恆 fit：縮放以旋轉不變的
 * 最大半徑計算，轉動時比例不跳。拖曳後抑制 click（父卡的點擊行為不誤觸）。 */
import { useId, useRef, useState } from "react";

export interface ThumbWp { lat: number; lon: number; alt?: number | null }

const PITCH = (55 * Math.PI) / 180;
const COS_P = Math.cos(PITCH), SIN_P = Math.sin(PITCH);
const GRID_M = 50;

export default function MissionThumb3D({ wps, className = "" }: {
  wps?: ThumbWp[]; className?: string;
}) {
  const clipId = useId();
  const [bearing, setBearing] = useState(-30);
  const dragRef = useRef<{ x: number; b: number } | null>(null);
  const movedRef = useRef(false);

  const pts = (wps ?? []).filter((w) => w.lat && w.lon);
  if (pts.length < 2) return <div className={`mthumb ${className}`} />;

  const lat0 = pts.reduce((t, w) => t + w.lat, 0) / pts.length;
  const lon0 = pts.reduce((t, w) => t + w.lon, 0) / pts.length;
  const k = 111320 * Math.cos((lat0 * Math.PI) / 180);
  const xyz = pts.map((w) => ({
    x: (w.lon - lon0) * k,
    y: (w.lat - lat0) * 110574,
    z: w.alt ?? 10,
  }));
  const maxR = Math.max(...xyz.map((p) => Math.hypot(p.x, p.y)), 1);
  const zMax = Math.max(...xyz.map((p) => p.z), 0);
  // 恆 fit＝旋轉不變：以水平最大半徑（旋轉掃出的圓）＋高度餘裕算比例
  const scale = Math.min(
    (50 - 10) / maxR,
    (50 - 10) / (maxR * COS_P + zMax * SIN_P),
  );

  const B = (bearing * Math.PI) / 180;
  const cB = Math.cos(B), sB = Math.sin(B);
  const proj = (x: number, y: number, z: number): [number, number] => {
    const xr = x * cB - y * sB;
    const yr = x * sB + y * cB;
    return [50 + xr * scale, 56 - (yr * COS_P + z * SIN_P) * scale];
  };
  const d = (z: (p: { z: number }) => number) => xyz
    .map((p, i) => {
      const [sx, sy] = proj(p.x, p.y, z(p));
      return `${i ? "L" : "M"}${sx.toFixed(1)},${sy.toFixed(1)}`;
    }).join("");

  // 地面網格（z=0 平面，50m 步）
  const ext = Math.max(GRID_M, Math.ceil(maxR / GRID_M) * GRID_M);
  const grid: string[] = [];
  for (let g = -ext; g <= ext; g += GRID_M) {
    const [ax, ay] = proj(g, -ext, 0), [bx, by] = proj(g, ext, 0);
    const [cx, cy] = proj(-ext, g, 0), [dx, dy] = proj(ext, g, 0);
    grid.push(`M${ax.toFixed(1)},${ay.toFixed(1)}L${bx.toFixed(1)},${by.toFixed(1)}`);
    grid.push(`M${cx.toFixed(1)},${cy.toFixed(1)}L${dx.toFixed(1)},${dy.toFixed(1)}`);
  }
  const [fx, fy] = proj(xyz[0].x, xyz[0].y, xyz[0].z);
  const last = xyz[xyz.length - 1];
  const [lx, ly] = proj(last.x, last.y, last.z);

  return (
    <svg viewBox="0 0 100 100" className={`mthumb mthumb3d ${className}`}
      style={{ touchAction: "pan-y" }}
      onPointerDown={(e) => {
        movedRef.current = false;
        dragRef.current = { x: e.clientX, b: bearing };
        (e.currentTarget as Element).setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const dr = dragRef.current;
        if (!dr) return;
        const dx = e.clientX - dr.x;
        if (Math.abs(dx) > 3) movedRef.current = true;
        setBearing(dr.b + dx * 0.8);
      }}
      onPointerUp={() => { dragRef.current = null; }}
      onPointerCancel={() => { dragRef.current = null; }}
      onClickCapture={(e) => {
        // 拖曳結束的 click 不往上冒（父卡的選取/啟用行為不誤觸）
        if (movedRef.current) { e.preventDefault(); e.stopPropagation(); }
      }}>
      <defs>
        <clipPath id={clipId}><rect width="100" height="100" rx="6" /></clipPath>
      </defs>
      <g clipPath={`url(#${clipId})`}>
        <rect width="100" height="100" fill="#1b1a17" />
        {grid.map((g, i) => (
          <path key={i} d={g} stroke="#262624" strokeWidth="0.6" fill="none" />
        ))}
        {/* 地面投影虛線 ＋ 空中灰絲帶（近似即時頁的主從關係） */}
        <path d={d(() => 0)} stroke="#3a3833" strokeWidth="1"
          strokeDasharray="2 2" fill="none" />
        <path d={d((p) => p.z)} stroke="#c2bfb3" strokeWidth="2" fill="none"
          strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={fx} cy={fy} r="2.8" fill="#0ca30c" />
        <rect x={lx - 2.2} y={ly - 2.2} width="4.4" height="4.4" fill="#c2bfb3" />
      </g>
    </svg>
  );
}
