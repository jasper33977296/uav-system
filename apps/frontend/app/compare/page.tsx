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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { API } from "@/lib/signal";

// 已驗證的類別色盤（dark surface #1a1a19 全項 PASS；與地圖機隊色盤不同——
// 這裡的實體是「航線」，固定順序指派、不循環）
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
  id: string; started_at: string; ended_at: string | null; drone_name: string;
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
interface Series { label: string; color: string; points: { x: number; y: number }[] }

function LineChart({ series, xMax, unit, xUnit }: {
  series: Series[]; xMax: number; unit: string; xUnit: string;
}) {
  const W = 640, H = 240, L = 44, R = 86, T = 12, B = 26;
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const ys = series.flatMap((s) => s.points.map((p) => p.y));
  if (!ys.length) return <div className="empty">選取的航線沒有可比較的樣本</div>;
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (yMax - yMin < 1e-6) { yMax += 1; yMin -= 1; }
  const pad = (yMax - yMin) * 0.08;
  yMin -= pad; yMax += pad;
  const sx = (x: number) => L + (x / (xMax || 1)) * (W - L - R);
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
    setHover(((x - L) / (W - L - R)) * xMax);
  }
  const near = (s: Series, x: number) =>
    s.points.reduce<{ dx: number; p: Series["points"][0] } | null>(
      (b, p) => (b === null || Math.abs(p.x - x) < b.dx ? { dx: Math.abs(p.x - x), p } : b),
      null);

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
        <text x={L - 6} y={T - 2} className="tick" textAnchor="end">{unit}</text>
        {[0, 0.5, 1].map((f, i) => (
          <text key={i} x={sx(xMax * f)} y={H - 8} className="tick" textAnchor="middle">
            {(xMax * f).toFixed(0)}{i === 2 ? ` ${xUnit}` : ""}
          </text>
        ))}
        {series.map((s) => (
          <path key={s.label} d={d(s)} fill="none" stroke={s.color}
            strokeWidth={2} strokeLinejoin="round" />
        ))}
        {series.length <= 4 && series.map((s) => {
          const last = s.points[s.points.length - 1];
          return last && (
            <text key={s.label} x={sx(last.x) + 5} y={sy(last.y) + 3}
              className="dlabel" fill={s.color}>{s.label}</text>
          );
        })}
        {hover !== null && (
          <line x1={sx(hover)} x2={sx(hover)} y1={T} y2={H - B} className="xhair" />
        )}
      </svg>
      {hover !== null && (
        <div className="cmp-tip">
          <div className="meta">{hover.toFixed(0)} {xUnit}</div>
          {series.map((s) => {
            const n = near(s, hover);
            return n && n.dx < xMax * 0.05 && (
              <div key={s.label}>
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

// ── 主頁 ─────────────────────────────────────────────────────
export default function Compare() {
  const [missions, setMissions] = useState<Mission[]>([]);
  const [missionId, setMissionId] = useState("");
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [selected, setSelected] = useState<string[]>([]);   // 順序即基準順序
  const [tracks, setTracks] = useState<Record<string, LinkRow[]>>({});
  const [wps, setWps] = useState<{ lat: number; lon: number }[]>([]);
  const [metric, setMetric] = useState("sinr");

  useEffect(() => {
    fetch(`${API}/api/missions`).then((r) => r.json())
      .then((ms: Mission[]) => setMissions(ms)).catch(() => {});
  }, []);

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
  const label = (sid: string) => {
    const s = sessions.find((x) => x.id === sid);
    return s ? new Date(s.started_at).toLocaleString("zh-TW",
      { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : sid.slice(0, 6);
  };
  const color = (sid: string) => SERIES[selected.indexOf(sid) % SERIES.length];

  // 沿線里程序列（分箱平均）
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
      const points = [...bins.entries()].sort((a, b) => a[0] - b[0])
        .map(([x, e]) => ({ x, y: e.sum / e.n }));
      return { label: label(sid), color: color(sid), points };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded.length, tracks, metric, path, selected]);

  // CDF：值→累積比例
  const cdfSeries: Series[] = useMemo(() => loaded.map((sid) => {
    const vals = tracks[sid].map((r) => r[metric]).filter((v) => v != null)
      .map(Number).sort((a, b) => a - b);
    const points = vals.map((v, i) => ({ x: v, y: ((i + 1) / vals.length) * 100 }));
    return { label: label(sid), color: color(sid), points };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [loaded.length, tracks, metric, selected]);
  const cdfXMax = Math.max(...cdfSeries.flatMap((s) => s.points.map((p) => p.x)), 1);

  // Δ 摘要（vs 基準＝第一條）
  const stats = loaded.map((sid) => {
    const rows = tracks[sid];
    const nums = (k: string) => rows.map((r) => r[k]).filter((v) => v != null).map(Number);
    const avg = (a: number[]) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : NaN);
    const sinr = nums("sinr");
    return {
      sid,
      avg_sinr: avg(sinr), min_sinr: sinr.length ? Math.min(...sinr) : NaN,
      avg_rtt: avg(nums("rtt_ms")), samples: rows.length,
      degraded_pct: sinr.length
        ? (sinr.filter((v) => v < 5).length / sinr.length) * 100 : NaN,
    };
  });
  const base = stats[0];
  const dfmt = (v: number, b: number, unit: string, betterHigh: boolean) => {
    if (!isFinite(v) || !isFinite(b)) return "—";
    const d = v - b;
    if (Math.abs(d) < 1e-9) return "基準";
    const better = betterHigh ? d > 0 : d < 0;
    return `${d > 0 ? "+" : ""}${d.toFixed(1)} ${unit} ${better ? "▲" : "▼"}`;
  };

  return (
    <div className="page-pad">
      <div className="card">
        <h3>航線比較——同一任務的訊號前後比較</h3>
        <p className="hint-line">
          對齊軸＝沿計畫路徑的里程（樣本投影到 plan 折線，比「同一地點不同時間」的訊號）。
          第一條勾選＝基準。最多 {MAX_SEL} 條。
        </p>
        <div className="cmd-row" style={{ marginTop: 8 }}>
          <select value={missionId} onChange={(e) => setMissionId(e.target.value)}>
            <option value="">選擇任務⋯</option>
            {missions.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            {METRICS.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
          </select>
        </div>
      </div>

      {missionId && (
        <div className="cmp-layout">
          <div className="card cmp-sessions">
            <h4>航線（{sessions.length}）</h4>
            {sessions.map((s) => (
              <label className="cmp-sess" key={s.id}>
                <input type="checkbox" checked={selected.includes(s.id)}
                  disabled={!selected.includes(s.id) && selected.length >= MAX_SEL}
                  onChange={() => toggle(s.id)} />
                <span className="dot" style={{
                  background: selected.includes(s.id) ? color(s.id) : "transparent",
                  border: selected.includes(s.id) ? "none" : "1px solid var(--hairline)",
                }} />
                <span>{label(s.id)}</span>
                <span className="meta">{s.summary?.samples_total ?? "—"} 筆</span>
                {selected[0] === s.id && <span className="chip">基準</span>}
              </label>
            ))}
            {sessions.length === 0 && <div className="empty">此任務尚無航線</div>}
          </div>

          <div className="cmp-main">
            {loaded.length === 0 && (
              <div className="card"><div className="empty">勾選 2 條以上航線開始比較</div></div>
            )}
            {loaded.length > 0 && path && (
              <div className="card">
                <h4>{mUnit.label} vs 沿線里程</h4>
                <LineChart series={chainSeries} xMax={path.total}
                  unit={mUnit.unit} xUnit="m" />
                <div className="cmp-legend">
                  {loaded.map((sid) => (
                    <span key={sid}>
                      <span className="dot" style={{ background: color(sid) }} />
                      {label(sid)}{selected[0] === sid ? "（基準）" : ""}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {loaded.length > 0 && (
              <div className="card">
                <h4>Δ 摘要（vs 基準）</h4>
                <table className="table">
                  <thead><tr>
                    <th>航線</th><th className="num">平均 SINR</th>
                    <th className="num">最低 SINR</th><th className="num">平均 RTT</th>
                    <th className="num">劣化樣本比</th><th className="num">Δ 平均 SINR</th>
                    <th className="num">Δ 平均 RTT</th>
                  </tr></thead>
                  <tbody>
                    {stats.map((st) => (
                      <tr key={st.sid}>
                        <td><span className="dot" style={{ background: color(st.sid) }} /> {label(st.sid)}</td>
                        <td className="num">{st.avg_sinr.toFixed(1)} dB</td>
                        <td className="num">{st.min_sinr.toFixed(1)} dB</td>
                        <td className="num">{st.avg_rtt.toFixed(0)} ms</td>
                        <td className="num">{st.degraded_pct.toFixed(0)}%</td>
                        <td className="num">{dfmt(st.avg_sinr, base.avg_sinr, "dB", true)}</td>
                        <td className="num">{dfmt(st.avg_rtt, base.avg_rtt, "ms", false)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {loaded.length > 0 && (
              <div className="card">
                <h4>{mUnit.label} 分布（CDF）</h4>
                <LineChart series={cdfSeries} xMax={cdfXMax} unit="%"
                  xUnit={mUnit.unit} />
              </div>
            )}

            {loaded.length > 0 && path && (
              <details className="card">
                <summary>軌跡疊圖（確認航線位置用）</summary>
                <MiniMap path={path} loaded={loaded} tracks={tracks} color={color} />
              </details>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function MiniMap({ path, loaded, tracks, color }: {
  path: ReturnType<typeof buildPath>; loaded: string[];
  tracks: Record<string, LinkRow[]>; color: (sid: string) => string;
}) {
  const all = path.pts;
  const xs = all.map((p) => p.x), ys = all.map((p) => p.y);
  const x0 = Math.min(...xs) - 20, x1 = Math.max(...xs) + 20;
  const y0 = Math.min(...ys) - 20, y1 = Math.max(...ys) + 20;
  const W = 640, H = Math.max(200, (W * (y1 - y0)) / (x1 - x0 || 1));
  const sx = (x: number) => ((x - x0) / (x1 - x0)) * W;
  const sy = (y: number) => H - ((y - y0) / (y1 - y0)) * H;
  const line = (pts: { x: number; y: number }[]) =>
    pts.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="cmp-map">
      <path d={line(path.pts)} fill="none" stroke="#8a94a3" strokeWidth={1.5}
        strokeDasharray="4 4" opacity={0.6} />
      {loaded.map((sid) => {
        const pts = tracks[sid]
          .filter((r) => r.lat != null && r.lon != null)
          .filter((_, i) => i % 3 === 0)
          .map((r) => toXY(r.lat!, r.lon!, path.origin.lat, path.origin.lon));
        return <path key={sid} d={line(pts)} fill="none"
          stroke={color(sid)} strokeWidth={2} opacity={0.85} />;
      })}
    </svg>
  );
}
