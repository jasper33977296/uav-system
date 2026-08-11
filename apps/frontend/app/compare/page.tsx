"use client";
/** 航線比較頁：同一任務（plan）的多次飛行，訊號前後比較。
 *
 * 對齊軸（2026-08-11 討論定案）：**沿計畫路徑的里程（chainage）**——
 * 每筆鏈路樣本投影到 plan 折線上，比的是「同一地點、不同時間的訊號」；
 * 實際航跡的小偏差由投影吸收。輔以 CDF 分布圖與 Δ 摘要表。
 * 第一條勾選的航線＝基準（前後比較的「前」）。
 *
 * 圖表遵循 dataviz 規範：序列色盤經 CVD 驗證（深色底全項通過）、
 * 2px 線、單一 Y 軸、遞弱網格、légende＋線尾直接標籤、crosshair tooltip。
 */
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import MissionThumb3D from "@/components/MissionThumb3D";
import { API, CLIENT_HEADERS, LINK_CLASSES, classifySinr } from "@/lib/signal";
import { aggregateCells, dropoutPct, type SignalCell } from "@/lib/signalMap";

// MapLibre 依賴 window，關閉 SSR（同即時頁 MapView 作法）
const CompareMap3D = dynamic(() => import("@/components/CompareMap3D"), { ssr: false });

// 已驗證的類別色盤（新暖 surface #262624 重驗全過，見 doc/design-tokens.md
// 驗證表；實體是「航線」，固定順序指派、不循環。all-pairs 只有前 3 槽通過
// → 同時高亮上限 3 條，其餘退 muted）
const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"];
const MAX_SEL = 6;
const BIN_M = 5;                      // 里程分箱（公尺）

const METRICS: { key: string; label: string; unit: string }[] = [
  { key: "sinr", label: "SINR", unit: "dB" },
  { key: "rtt_ms", label: "RTT", unit: "ms" },
  { key: "rsrp", label: "RSRP", unit: "dBm" },
  { key: "packet_loss_pct", label: "丟包率", unit: "%" },
];

interface Mission { id: string; name: string; waypoint_count: number }
interface Sess {
  id: string; started_at: string; ended_at: string | null;
  drone_id: string; drone_name: string;
  note?: string | null;
  summary: { samples_total?: number } | null;
}
interface LinkRow { lat: number | null; lon: number | null; [k: string]: unknown }

// ── 幾何：equirect 局部座標 + 投影到折線里程 ─────────────────
function toXY(lat: number, lon: number, lat0: number, lon0: number) {
  return {
    x: (lon - lon0) * 111320 * Math.cos((lat0 * Math.PI) / 180),
    y: (lat - lat0) * 111320,
  };
}

function buildPath(wps: { lat: number; lon: number }[]) {
  const [o] = wps;
  const pts = wps.map((w) => toXY(w.lat, w.lon, o.lat, o.lon));
  const chain = [0];
  for (let i = 1; i < pts.length; i++) {
    chain.push(chain[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
  }
  return { origin: o, pts, chain, total: chain[chain.length - 1] };
}

function projectChainage(path: ReturnType<typeof buildPath>, lat: number, lon: number) {
  const p = toXY(lat, lon, path.origin.lat, path.origin.lon);
  let best = { d2: Infinity, s: 0 };
  for (let i = 1; i < path.pts.length; i++) {
    const a = path.pts[i - 1], b = path.pts[i];
    const dx = b.x - a.x, dy = b.y - a.y;
    const len2 = dx * dx + dy * dy || 1e-9;
    const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
    const qx = a.x + t * dx, qy = a.y + t * dy;
    const d2 = (p.x - qx) ** 2 + (p.y - qy) ** 2;
    if (d2 < best.d2) best = { d2, s: path.chain[i - 1] + t * Math.sqrt(len2) };
  }
  return best.s;
}

// ── 折線圖（SVG 手刻，遵循 mark 規範）────────────────────────
// dim（design-tokens v1）：同時高亮上限 3 條——本色盤 all-pairs 只驗過前
// 3 槽，多線交錯超過 3 條就難分。dim 線退 muted 細線墊底、靠線尾標名識別
interface Series {
  id: string;                 // React key 用（label 可能撞名，不可當 key）
  label: string; color: string; points: { x: number; y: number }[];
  raw?: { x: number; y: number }[];   // v3：原始淡線與平滑並存（誠實原則）
  dim?: boolean;
}

function LineChart({ series, xMax, unit, xUnit, xMin = 0, highlight }: {
  series: Series[]; xMax: number; unit: string; xUnit: string; xMin?: number;
  highlight?: [number, number] | null;   // 點熱區格 → 對應里程段高亮帶
}) {
  // T=18：Y 軸單位獨立一行（原 T=12 時單位與最上排刻度數字疊字）
  // R=96：右邊留白放標籤欄——時間標籤固定排在繪圖區外，不再壓到線
  const W = 640, H = 240, L = 44, R = 96, T = 18, B = 26;
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const ys = series.flatMap((s) => s.points.map((p) => p.y));
  if (!ys.length) return <div className="empty">選取的航線沒有可比較的樣本</div>;
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (yMax - yMin < 1e-6) { yMax += 1; yMin -= 1; }
  const pad = (yMax - yMin) * 0.08;
  yMin -= pad; yMax += pad;
  // x 域帶 xMin：CDF 的 x 是量測值本身，SINR/RSRP 常為負——寫死 0 起點
  // 會把負值畫到 Y 軸左邊、壓在刻度文字上
  const xSpan = xMax - xMin || 1;
  const sx = (x: number) => L + ((x - xMin) / xSpan) * (W - L - R);
  const sy = (y: number) => T + (1 - (y - yMin) / (yMax - yMin)) * (H - T - B);

  const ticksY = [0, 1, 2, 3].map((i) => yMin + ((yMax - yMin) * i) / 3);
  const d = (s: Series) => {
    let out = "", pen = false;
    for (const p of s.points) {
      out += `${pen ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`;
      pen = true;
    }
    return out;
  };

  function onMove(e: React.PointerEvent) {
    const r = ref.current!.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * W;
    if (x < L || x > W - R) { setHover(null); return; }
    setHover(xMin + ((x - L) / (W - L - R)) * xSpan);
  }
  const near = (s: Series, x: number) =>
    s.points.reduce<{ dx: number; p: Series["points"][0] } | null>(
      (b, p) => (b === null || Math.abs(p.x - x) < b.dx ? { dx: Math.abs(p.x - x), p } : b),
      null);

  // 線尾標籤（2026-08-11 改版）：文字**固定排在繪圖區右側留白**、不進
  // 繪圖區（原本跟著線尾座標放，會壓到其他線）；線尾畫色點、標籤前也帶
  // 同色點，靠色點對應「哪條線是哪個時間」。高度相近時照舊以 12px 最小
  // 間距往下推開、超出下緣整串上移
  const LB = 12;
  const endLabels = series.flatMap((s) => {
      const last = s.points[s.points.length - 1];
      return last
        ? [{ id: s.id, label: s.label, color: s.dim ? "var(--muted)" : s.color,
             ex: sx(last.x), ey: sy(last.y), ly: sy(last.y) + 3 }]
        : [];
    }).sort((a, b) => a.ly - b.ly);
  for (let i = 1; i < endLabels.length; i++) {
    if (endLabels[i].ly - endLabels[i - 1].ly < LB) {
      endLabels[i].ly = endLabels[i - 1].ly + LB;
    }
  }
  const overflow = endLabels.length
    ? endLabels[endLabels.length - 1].ly - (H - B) : 0;
  if (overflow > 0) for (const e of endLabels) e.ly -= overflow;

  return (
    <div className="cmp-chartwrap">
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} className="cmp-chart"
        onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {ticksY.map((t, i) => (
          <g key={i}>
            <line x1={L} x2={W - R} y1={sy(t)} y2={sy(t)} className="grid" />
            <text x={L - 6} y={sy(t) + 3} className="tick" textAnchor="end">{t.toFixed(0)}</text>
          </g>
        ))}
        <text x={L - 6} y={8} className="tick" textAnchor="end">{unit}</text>
        {[0, 0.5, 1].map((f, i) => (
          <text key={i} x={sx(xMin + xSpan * f)} y={H - 8} className="tick" textAnchor="middle">
            {(xMin + xSpan * f).toFixed(0)}{i === 2 ? ` ${xUnit}` : ""}
          </text>
        ))}
        {highlight && (
          <rect x={sx(highlight[0])} y={T}
            width={Math.max(2, sx(highlight[1]) - sx(highlight[0]))}
            height={H - T - B} fill="var(--ink)" opacity={0.1} />
        )}
        {[...series].sort((a, b) => Number(!!b.dim) - Number(!!a.dim)).map((s) => (
          <g key={s.id}>
            {/* 原始淡線（1px 40%）＋平滑線（2px）並存——禁只畫平滑 */}
            {s.raw && !s.dim && (
              <path d={d({ ...s, points: s.raw })} fill="none"
                stroke={s.color} strokeWidth={1} strokeOpacity={0.35}
                strokeLinejoin="round" />
            )}
            <path d={d(s)} fill="none"
              stroke={s.dim ? "var(--muted)" : s.color}
              strokeWidth={s.dim ? 1 : 2} strokeOpacity={s.dim ? 0.6 : 1}
              strokeLinejoin="round" />
          </g>
        ))}
        {endLabels.map((e) => (
          <g key={e.id}>
            <circle cx={e.ex} cy={e.ey} r={3} fill={e.color} />
            <circle cx={W - R + 8} cy={e.ly - 3.5} r={2.5} fill={e.color} />
            <text x={W - R + 14} y={e.ly}
              className="dlabel" fill={e.color}>{e.label}</text>
          </g>
        ))}
        {hover !== null && (
          <line x1={sx(hover)} x2={sx(hover)} y1={T} y2={H - B} className="xhair" />
        )}
      </svg>
      {hover !== null && (
        <div className="cmp-tip">
          <div className="meta">{hover.toFixed(0)} {xUnit}</div>
          {series.map((s) => {
            const n = near(s, hover);
            return n && n.dx < xSpan * 0.05 && (
              <div key={s.id}>
                <span className="dot" style={{ background: s.color }} />
                {s.label}：{n.p.y.toFixed(1)} {unit}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── 熱區圖（v3 主視覺）：俯視 2D、~10m 格網、P10 分級色 ────────
const SPARSE_N = 5;   // 樣本 < 門檻＝稀疏格（斜線紋理＋降透明）

function HeatMap({ cells, plan, grid, sel, onSel }: {
  cells: SignalCell[];
  plan: { x: number; y: number }[];
  grid: number;
  sel: SignalCell | null;
  onSel: (c: SignalCell | null) => void;
}) {
  if (!cells.length) {
    return <div className="empty">選中架次沒有訊號樣本</div>;
  }
  const xs = [...cells.map((c) => c.x), ...plan.map((p) => p.x)];
  const ys = [...cells.map((c) => c.y), ...plan.map((p) => p.y)];
  const x0 = Math.min(...xs) - grid, x1 = Math.max(...xs) + grid;
  const y0 = Math.min(...ys) - grid, y1 = Math.max(...ys) + grid;
  // 縮放取雙軸 min（驗收 blocker 修正：只算 x 軸時，南北向航線的東西跨
  // 極小 → scale 爆大 → 10m 格畫成巨格）；高度上限 460、內容水平置中
  const W = 640, HMAX = 460, PAD = 12;
  const spanX = x1 - x0 || 1, spanY = y1 - y0 || 1;
  const scale = Math.min((W - 2 * PAD) / spanX, (HMAX - 2 * PAD) / spanY);
  const H = Math.max(160, spanY * scale + 2 * PAD);
  const cx0 = ((W - 2 * PAD) - spanX * scale) / 2;   // 水平置中偏移
  const X = (x: number) => PAD + cx0 + (x - x0) * scale;
  const Y = (y: number) => H - PAD - (y - y0) * scale;
  const s = grid * scale;
  return (
    <svg viewBox={`0 0 ${W} ${H.toFixed(0)}`} className="heatmap"
      onClick={() => onSel(null)}>
      <defs>
        <pattern id="hm-sparse" patternUnits="userSpaceOnUse" width="6" height="6">
          <path d="M0 6 L6 0" stroke="var(--ink-2)" strokeWidth="0.8" opacity="0.5" />
        </pattern>
      </defs>
      {/* 計畫路徑虛線＝空間定位參考 */}
      {plan.length >= 2 && (
        <polyline
          points={plan.map((p) => `${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(" ")}
          fill="none" stroke="#8f8b80" strokeWidth="1.2"
          strokeDasharray="4 4" opacity="0.6" />
      )}
      {plan.length > 0 && (
        <circle cx={X(plan[0].x)} cy={Y(plan[0].y)} r="4" fill="none"
          stroke="#8f8b80" strokeWidth="1.5" />
      )}
      {cells.map((c) => {
        const cls = classifySinr(c.p10);
        const sparse = c.n < SPARSE_N;
        const isSel = sel && sel.x === c.x && sel.y === c.y;
        return (
          <g key={`${c.x}|${c.y}`} className="hm-cell"
            onClick={(e) => { e.stopPropagation(); onSel(c); }}>
            <rect x={X(c.x) - s / 2} y={Y(c.y) - s / 2} width={s} height={s}
              fill={cls.color} opacity={sparse ? 0.3 : 0.55}
              stroke={isSel ? "var(--ink)" : "none"} strokeWidth="1.5">
              <title>{`樣本 ${c.n}｜P10 ${c.p10.toFixed(1)} dB｜最差 ${c.min.toFixed(1)} dB｜涵蓋 ${c.sessionIds.length} 趟`}</title>
            </rect>
            {sparse && (
              <rect x={X(c.x) - s / 2} y={Y(c.y) - s / 2} width={s} height={s}
                fill="url(#hm-sparse)" pointerEvents="none" />
            )}
          </g>
        );
      })}
    </svg>
  );
}

// ── 主頁 ─────────────────────────────────────────────────────
export default function Compare() {
  const [missions, setMissions] = useState<Mission[]>([]);
  const [missionId, setMissionId] = useState("");
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [selected, setSelected] = useState<string[]>([]);   // 順序即基準順序
  const [tracks, setTracks] = useState<Record<string, LinkRow[]>>({});
  const [wps, setWps] = useState<{ lat: number; lon: number; alt?: number }[]>([]);
  const [metric, setMetric] = useState("sinr");
  // 同時高亮上限 3 條（design-tokens v1）：超過的退 muted，點圖例切換
  const [focus, setFocus] = useState<string[]>([]);
  // ── v3「場域弱區分析」（§6 使用者核准）───────────────────────
  // 檢視切換：熱區（主視覺）↔ 軌跡 3D；記憶（工作區判準）
  const [view3, setView3] = useState<"heat" | "3d">("heat");
  useEffect(() => {
    const saved = localStorage.getItem("cmp-view");
    if (saved === "3d" || saved === "heat") setView3(saved);
  }, []);
  const switchView = (v: "heat" | "3d") => {
    setView3(v);
    localStorage.setItem("cmp-view", v);
  };
  // 斷訊率排序（優化前後對比用）；備註 inline 編輯；點格下鑽
  const [sortDrop, setSortDrop] = useState<"none" | "desc" | "asc">("none");
  const [noteEdit, setNoteEdit] = useState<{ id: string; text: string } | null>(null);
  const [selCell, setSelCell] = useState<SignalCell | null>(null);
  const chartCardRef = useRef<HTMLDivElement>(null);

  async function saveNote(id: string, note: string) {
    setNoteEdit(null);
    try {
      const res = await fetch(`${API}/api/sessions/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
        body: JSON.stringify({ note }),
      });
      if (res.ok) {
        setSessions((cur) => cur.map((s) => (s.id === id ? { ...s, note } : s)));
      }
    } catch { /* 存失敗維持原值（欄位顯示未變＝如實） */ }
  }

  // v2：任務選擇＝3D 縮圖橫捲列（共用 §4 元件）——逐任務抓 waypoints、
  // 選中卡自動捲入
  const [thumbs, setThumbs] = useState<Record<string, { lat: number; lon: number; alt?: number }[]>>({});
  const selCardRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    for (const m of missions) {
      if (thumbs[m.id]) continue;
      fetch(`${API}/api/missions/${m.id}/waypoints`)
        .then((r) => r.json())
        .then((d) => setThumbs((t) => ({ ...t, [m.id]: d.waypoints ?? [] })))
        .catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missions]);
  useEffect(() => {
    selCardRef.current?.scrollIntoView(
      { behavior: "smooth", inline: "nearest", block: "nearest" });
  }, [missionId]);

  // 每卡架次數按 mission_id 計（誠實原則：標示必須與點開行為一致——
  // 曾用 mission_name 聚合，重複同名任務全掛同一數字、多數點開是空的）
  const [missionCounts, setMissionCounts] = useState<Record<string, number>>({});
  useEffect(() => {
    fetch(`${API}/api/missions`).then((r) => r.json())
      .then((ms: Mission[]) => setMissions(ms)).catch(() => {});
    fetch(`${API}/api/sessions?limit=500`).then((r) => r.json())
      .then((rows: { mission_id: string | null }[]) => {
        const c: Record<string, number> = {};
        for (const r of rows) if (r.mission_id) c[r.mission_id] = (c[r.mission_id] ?? 0) + 1;
        setMissionCounts(c);
      }).catch(() => {});
  }, []);
  // 有架次的排前（找得到可比任務）、0 架次淡化殿後
  const orderedMissions = useMemo(() =>
    [...missions].sort((a, b) =>
      (missionCounts[b.id] ?? 0) - (missionCounts[a.id] ?? 0)),
    [missions, missionCounts]);

  useEffect(() => {
    if (!missionId) { setSessions([]); setSelected([]); return; }
    fetch(`${API}/api/sessions?mission_id=${missionId}&limit=100`)
      .then((r) => r.json()).then(setSessions).catch(() => {});
    fetch(`${API}/api/missions/${missionId}/waypoints`)
      .then((r) => r.json())
      .then((d) => setWps(d.waypoints.filter((w: LinkRow) => w.lat && w.lon)))
      .catch(() => {});
    setSelected([]);
  }, [missionId]);

  const toggle = useCallback((sid: string) => {
    setSelected((cur) => {
      if (cur.includes(sid)) return cur.filter((x) => x !== sid);
      if (cur.length >= MAX_SEL) return cur;
      if (!tracks[sid]) {
        fetch(`${API}/api/sessions/${sid}/track`).then((r) => r.json())
          .then((d) => setTracks((t) => ({ ...t, [sid]: d.link })))
          .catch(() => {});
      }
      return [...cur, sid];
    });
  }, [tracks]);

  const path = useMemo(() => (wps.length >= 2 ? buildPath(wps) : null), [wps]);
  const mUnit = METRICS.find((m) => m.key === metric)!;

  const loaded = selected.filter((sid) => tracks[sid]);
  const loadedKey = loaded.join(",");
  // loaded 變動時修剪/補位 focus：預設前 3 條，使用者的選擇盡量保留
  useEffect(() => {
    setFocus((cur) => {
      const kept = cur.filter((id) => loaded.includes(id));
      const fill = loaded.filter((id) => !kept.includes(id))
        .slice(0, Math.max(0, 3 - kept.length));
      return [...kept, ...fill];
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedKey]);
  const isDim = (sid: string) => loaded.length > 3 && !focus.includes(sid);
  const toggleFocus = (sid: string) =>
    setFocus((cur) => cur.includes(sid)
      ? cur.filter((x) => x !== sid)
      : [...cur, sid].slice(-3));           // 滿 3 條丟最舊的
  // 標籤＝起飛時間（到分鐘）。腳本連飛的 session 常落在同一分鐘——撞名的
  // 才補秒數（全部帶秒是為極少數情況付出常態寬度）。標籤僅供顯示，
  // React key 一律用 session id：label 撞名曾讓 tooltip 在重複 key 下
  // 錯誤 reconcile、重複列無限堆疊（2026-08-11 bug）
  const labelOf = useMemo(() => {
    const fmt = (iso: string, sec: boolean) => new Date(iso).toLocaleString("zh-TW", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
      ...(sec ? { second: "2-digit" as const } : {}), hour12: false,
    });
    const counts = new Map<string, number>();
    for (const s of sessions) {
      const k = fmt(s.started_at, false);
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    return new Map(sessions.map((s) => [
      s.id, fmt(s.started_at, (counts.get(fmt(s.started_at, false)) ?? 0) > 1),
    ]));
  }, [sessions]);
  const label = (sid: string) => labelOf.get(sid) ?? sid.slice(0, 6);
  const color = (sid: string) => SERIES[selected.indexOf(sid) % SERIES.length];

  // 沿線里程序列（分箱）＋平滑（v3：原始淡線＋平滑線並存，禁只畫平滑）
  const chainSeries: Series[] = useMemo(() => {
    if (!path) return [];
    return loaded.map((sid) => {
      const bins = new Map<number, { sum: number; n: number }>();
      for (const r of tracks[sid]) {
        const v = r[metric];
        if (r.lat == null || r.lon == null || v == null) continue;
        const b = Math.round(projectChainage(path, r.lat, r.lon) / BIN_M) * BIN_M;
        const e = bins.get(b) ?? { sum: 0, n: 0 };
        e.sum += Number(v); e.n += 1;
        bins.set(b, e);
      }
      const raw = [...bins.entries()].sort((a, b) => a[0] - b[0])
        .map(([x, e]) => ({ x, y: e.sum / e.n }));
      // 平滑＝±2 箱滑動平均（25m 窗）；raw 保留並存
      const points = raw.map((p, i) => {
        const w = raw.slice(Math.max(0, i - 2), i + 3);
        return { x: p.x, y: w.reduce((t, q) => t + q.y, 0) / w.length };
      });
      return { id: sid, label: label(sid), color: color(sid), points, raw,
               dim: isDim(sid) };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded.length, tracks, metric, path, selected, focus]);

  // 熱區格網（§6.5 契約形狀；後端 query_signal_map 就緒前客端聚合）
  const cells = useMemo(() => {
    if (!path || !loaded.length) return [];
    return aggregateCells(tracks, loaded, path.origin, 10);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded.length, tracks, path]);
  // 斷訊率（已載入架次客端算；未載入顯示 —，全欄位等後端摘要）
  const dropouts = useMemo(() => {
    const out: Record<string, number | null> = {};
    for (const sid of loaded) out[sid] = dropoutPct(tracks[sid] ?? []);
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded.length, tracks]);
  // 點格 → 對應里程段（沿線圖高亮帶）
  const cellHl = useMemo<[number, number] | null>(() => {
    if (!selCell || !path) return null;
    const s = projectChainage(path, selCell.lat, selCell.lon);
    return [Math.max(0, s - 10), s + 10];
  }, [selCell, path]);

  // v3：CDF 曲線與 Δ 摘要（基準概念）移除——CDF 的 KPI 由架次表
  // 「斷訊率」承接，多趟平等無基準（§6.1 使用者裁決）
  // 沿線圖圖例：>3 條時是高亮切換器（顏色跟航線不跟排名）
  const legend = (
    <div className="cmp-legend">
      {loaded.map((sid) => (
        <button key={sid} className={`chip chip-btn ${isDim(sid) ? "chip-off" : ""}`}
          onClick={() => toggleFocus(sid)}
          title={loaded.length > 3 ? "點擊切換高亮（同時最多 3 條）" : undefined}>
          <span className="dot" style={{ background: color(sid) }} />
          {label(sid)}
        </button>
      ))}
      {loaded.length > 3 && (
        <span className="hint-line">高亮 {focus.length}/3——點圖例切換</span>
      )}
    </div>
  );

  // 疊最近 5 趟快捷：一鍵勾選（含 track 補抓）
  function selectRecent5() {
    const ids = sessions.slice(0, 5).map((s) => s.id);
    for (const sid of ids) {
      if (!tracks[sid]) {
        fetch(`${API}/api/sessions/${sid}/track`).then((r) => r.json())
          .then((d) => setTracks((t) => ({ ...t, [sid]: d.link })))
          .catch(() => {});
      }
    }
    setSelected(ids);
  }

  // 架次表排序：預設時間新→舊；斷訊率欄可切（未載入者無值殿後）
  const orderedSessions = useMemo(() => {
    if (sortDrop === "none") return sessions;
    const val = (s: Sess) => dropouts[s.id] ?? (sortDrop === "desc" ? -1 : 1e9);
    return [...sessions].sort((a, b) =>
      sortDrop === "desc" ? val(b) - val(a) : val(a) - val(b));
  }, [sessions, sortDrop, dropouts]);

  const durTxt = (s: Sess) => {
    if (!s.ended_at) return "進行中";
    const sec = Math.round(
      (new Date(s.ended_at).getTime() - new Date(s.started_at).getTime()) / 1000);
    return `${Math.floor(sec / 60)}:${(sec % 60).toString().padStart(2, "0")}`;
  };

  return (
    <div className="page-pad">
      {/* v2：任務選擇＝3D 縮圖橫捲列（拖縮圖旋轉、點卡選任務）＋列尾下拉輔助。
          ⓘ 整移除維持（§6 定案：比較頁不放解釋入口） */}
      <div className="mstrip">
        {orderedMissions.map((m) => {
          const n = missionCounts[m.id] ?? 0;
          return (
            <button key={m.id} ref={m.id === missionId ? selCardRef : undefined}
              className={`mstrip-card ${m.id === missionId ? "on" : ""}`
                + ` ${n === 0 ? "dim" : ""}`}
              onClick={() => setMissionId(m.id)}>
              <MissionThumb3D wps={thumbs[m.id]}
                onTap={() => setMissionId(m.id)} />
              <span className="mstrip-name">{m.name}</span>
              <span className="meta">{n} 架次</span>
            </button>
          );
        })}
        <select className="mstrip-tail" value={missionId}
          onChange={(e) => setMissionId(e.target.value)}>
          <option value="">選擇任務⋯</option>
          {orderedMissions.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}（{missionCounts[m.id] ?? 0} 架次）
            </option>
          ))}
        </select>
      </div>

      {missionId && (
        <>
          {/* v3 架次表：勾選＋備註 inline＋斷訊率排序；多趟平等無基準 */}
          <div className="card">
            <div className="drone-head" style={{ marginBottom: 6 }}>
              <span className="meta">架次（{sessions.length}）</span>
              <span className="spacer" />
              <button className="btn-plain btn-sm" disabled={!sessions.length}
                onClick={selectRecent5}>疊最近 5 趟</button>
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th></th><th>時間</th><th>機</th><th>備註</th><th>時長</th>
                  <th className="num sortable"
                    title="劣化門檻以下航段佔比；點擊排序（優化前後對比）"
                    onClick={() => setSortDrop(
                      sortDrop === "desc" ? "asc" : "desc")}>
                    斷訊率{sortDrop === "desc" ? " ↓" : sortDrop === "asc" ? " ↑" : ""}
                  </th>
                </tr>
              </thead>
              <tbody>
                {sessions.length === 0 && (
                  <tr><td colSpan={6} className="empty">此任務尚無航線</td></tr>
                )}
                {orderedSessions.map((s) => {
                  const on = selected.includes(s.id);
                  const drop = dropouts[s.id];
                  return (
                    <tr key={s.id} className="row-link"
                      onClick={() => toggle(s.id)}>
                      <td onClick={(e) => e.stopPropagation()}>
                        <input type="checkbox" checked={on}
                          disabled={!on && selected.length >= MAX_SEL}
                          onChange={() => toggle(s.id)} />
                      </td>
                      <td>
                        <span className="dot" style={{
                          display: "inline-block", marginRight: 6,
                          background: on ? color(s.id) : "var(--hairline)" }} />
                        {label(s.id)}
                      </td>
                      <td>{s.drone_name}</td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {noteEdit?.id === s.id ? (
                          <input className="note-input" autoFocus
                            value={noteEdit.text}
                            onChange={(e) =>
                              setNoteEdit({ id: s.id, text: e.target.value })}
                            onBlur={() => saveNote(s.id, noteEdit.text)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") saveNote(s.id, noteEdit.text);
                              if (e.key === "Escape") setNoteEdit(null);
                            }} />
                        ) : (
                          <button className="note-btn" title="編輯備註"
                            onClick={() =>
                              setNoteEdit({ id: s.id, text: s.note ?? "" })}>
                            {s.note || "—"} ✎
                          </button>
                        )}
                      </td>
                      <td>{durTxt(s)}</td>
                      <td className="num">
                        {drop != null ? `${drop.toFixed(0)}%` : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {loaded.length < sessions.length && sortDrop !== "none" && (
              <div className="hint-line">斷訊率僅已勾選（已載入）架次可算；其餘待後端摘要欄</div>
            )}
          </div>

          {/* 檢視切換：熱區（主視覺）｜軌跡 3D（看高度差異用）——記憶 */}
          {loaded.length > 0 && (
            <div className="seg" style={{ alignSelf: "flex-start" }}>
              <button className={view3 === "heat" ? "on" : ""}
                onClick={() => switchView("heat")}>熱區</button>
              <button className={view3 === "3d" ? "on" : ""}
                onClick={() => switchView("3d")}>軌跡（3D）</button>
            </div>
          )}

          <div className="cmp-main">
            {loaded.length === 0 && (
              <div className="card"><div className="empty">勾選 2 條以上航線開始比較</div></div>
            )}

            {/* 熱區圖（v3 主視覺）：找弱區 → 點格下鑽 */}
            {loaded.length > 0 && path && view3 === "heat" && (
              <div className="card">
                <h4>訊號熱區
                  <span className="h3-note">格色＝該格最差 10%（P10）· 點格下鑽</span>
                </h4>
                <HeatMap cells={cells} plan={path.pts} grid={10}
                  sel={selCell}
                  onSel={(c) => {
                    setSelCell(c);
                    if (c) chartCardRef.current?.scrollIntoView(
                      { behavior: "smooth", block: "nearest" });
                  }} />
                <div className="cmp-legend">
                  {LINK_CLASSES.map((c) => (
                    <span key={c.key}>
                      <span className="dot" style={{ background: c.color }} />
                      {c.label}
                    </span>
                  ))}
                  <span><span className="dot hm-sparse-key" />樣本不足（&lt;{SPARSE_N}）</span>
                </div>
                {selCell && (
                  <div className="hint-line">
                    選中格：樣本 {selCell.n}．P10 {selCell.p10.toFixed(1)} dB．
                    最差 {selCell.min.toFixed(1)} dB．涵蓋 {selCell.sessionIds.length} 趟
                    ——沿線圖已高亮對應里程段（線疊著掉＝環境性；線分岔＝時變性）
                    <button className="btn-plain btn-sm" style={{ marginLeft: 8 }}
                      onClick={() => setSelCell(null)}>清除</button>
                  </div>
                )}
              </div>
            )}

            {/* 軌跡 3D（v2 疊圖沿用）：看高度差異時切換 */}
            {loaded.length > 0 && path && view3 === "3d" && (
              <div className="card">
                <h4>軌跡疊圖（3D）
                  <span className="h3-note">拖曳旋轉 · 點絲帶看訊號</span>
                </h4>
                <CompareMap3D wps={wps} loaded={loaded} tracks={tracks}
                  colorOf={color} labelOf={label}
                  dimIds={loaded.filter(isDim)} />
              </div>
            )}

            {/* 沿線圖（下鑽視角）：回答「這格為什麼弱」 */}
            {loaded.length > 0 && path && (
              <div className="card" ref={chartCardRef}>
                <h4>{mUnit.key === "sinr" ? "訊號" : mUnit.label} vs 飛行距離
                  <span className="h3-note">
                    原始＋平滑（25m 窗）·
                    <select className="metric-mini" value={metric}
                      onChange={(e) => setMetric(e.target.value)}>
                      {METRICS.map((m) => (
                        <option key={m.key} value={m.key}>{m.label}</option>
                      ))}
                    </select>
                  </span>
                </h4>
                <LineChart series={chainSeries} xMax={path.total}
                  unit={mUnit.unit} xUnit="m" highlight={cellHl} />
                {legend}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

