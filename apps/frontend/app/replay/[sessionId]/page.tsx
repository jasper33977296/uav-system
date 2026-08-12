"use client";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { colorFor, createDroneLayer } from "@/components/droneLayer";
import EventModal from "@/components/EventModal";
import ReplayVideo, { type SessionVideo } from "@/components/ReplayVideo";
import { SignalBars } from "@/components/SimpleHud";
import { routeLayer } from "@/lib/deckRoute";
import { evText } from "@/lib/evtext";
import { CANVAS, groundGrid, pathArrows, ribbon, trailLineString } from "@/lib/geo";
import { API, classifySinr } from "@/lib/signal";

interface LinkRow {
  time: string; lat: number | null; lon: number | null; alt_rel: number | null;
  sinr: number | null; rtt_ms: number | null;
}
interface Ev {
  id: number; time: string; severity: string; type: string;
  detail: Record<string, unknown>;
  source?: string | null;
}

const fmt = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

/* ── 時序圖：SVG viewBox 1000 寬，preserveAspectRatio none 拉滿容器 ── */
const W = 1000;

function Chart({
  rows, field, height, yLabel, thresholds, events, t0, t1, idx, onSeek, onEvent,
}: {
  rows: LinkRow[]; field: "sinr" | "rtt_ms"; height: number; yLabel: string;
  thresholds?: number[]; events?: Ev[]; t0: number; t1: number; idx: number;
  onSeek?: (idx: number) => void;
  onEvent?: (e: Ev) => void;
}) {
  const vals = rows.map((r) => r[field]).filter((v): v is number => v != null);
  if (!vals.length) return null;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  for (const th of thresholds ?? []) { lo = Math.min(lo, th); hi = Math.max(hi, th); }
  const pad = (hi - lo || 1) * 0.12;
  lo -= pad; hi += pad;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * W;
  const y = (v: number) => height - ((v - lo) / (hi - lo)) * height;
  const pts = rows
    .filter((r) => r[field] != null)
    .map((r) => `${x(new Date(r.time).getTime())},${y(r[field] as number)}`)
    .join(" ");
  const cx = rows[idx] ? x(new Date(rows[idx].time).getTime()) : 0;

  // 點圖表跳時刻（ui-spec §5.3）：點擊位置 → 時間 → 最近樣本；
  // 事件三角同一路徑（三角座標＝事件時刻，點它即跳到事件）
  const seek = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!onSeek || !rows.length) return;
    const r = e.currentTarget.getBoundingClientRect();
    const t = t0 + ((e.clientX - r.left) / r.width) * (t1 - t0);
    let best = 0, bd = Infinity;
    rows.forEach((row, i) => {
      const d = Math.abs(new Date(row.time).getTime() - t);
      if (d < bd) { bd = d; best = i; }
    });
    onSeek(best);
  };

  return (
    <div className="chart">
      <span className="chart-label">{yLabel}</span>
      <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none"
           onPointerDown={seek}
           style={onSeek ? { cursor: "crosshair" } : undefined}>
        {(thresholds ?? []).map((th) => (
          <line key={th} x1={0} x2={W} y1={y(th)} y2={y(th)}
                stroke="var(--muted)" strokeWidth="1" strokeDasharray="4 4"
                vectorEffect="non-scaling-stroke" opacity={0.5} />
        ))}
        <polyline points={pts} fill="none" stroke="var(--series-1)" strokeWidth="1.6"
                  vectorEffect="non-scaling-stroke" />
        {(events ?? []).map((e) => (
          <polygon
            key={e.id}
            points={`${x(new Date(e.time).getTime()) - 5},2 ${x(new Date(e.time).getTime()) + 5},2 ${x(new Date(e.time).getTime())},11`}
            fill={e.severity === "critical" ? "#a01818" : e.severity === "warning" ? "#fab219" : "#8f8b80"}
            style={onEvent ? { cursor: "pointer" } : undefined}
            // 點三角＝跳到事件時刻（pointerdown 冒泡到 svg 的 seek）＋開詳情
            // modal（§2.7：modal 接手細節職責；title 保留 hover 摘要）
            onClick={() => onEvent?.(e)}
          >
            <title>{evText({ type: e.type, detail: e.detail,
              severity: e.severity as "info" | "warning" | "critical" })}</title>
          </polygon>
        ))}
        <line x1={cx} x2={cx} y1={0} y2={height} stroke="var(--ink)" strokeWidth="1"
              vectorEffect="non-scaling-stroke" opacity={0.55} />
      </svg>
    </div>
  );
}

export default function Replay() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const router = useRouter();
  const [rows, setRows] = useState<LinkRow[]>([]);
  const [meta, setMeta] = useState<{ mission_id: string | null;
    mission_name: string | null; drone_name?: string | null;
    drone_id?: string | null } | null>(null);
  const [video, setVideo] = useState<SessionVideo | null>(null);   // §5.4
  const [plan, setPlan] = useState<{ lat: number; lon: number; alt: number | null }[]>([]);
  const [events, setEvents] = useState<Ev[]>([]);
  const [openEv, setOpenEv] = useState<Ev | null>(null);   // 事件詳情 modal（§2.7）
  const [idx, setIdx] = useState(0);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const cursorRef = useRef<LinkRow | null>(null);

  useEffect(() => {
    fetch(`${API}/api/sessions/${sessionId}/track`)
      .then((r) => r.json())
      .then((d) => {
        const link = (d.link ?? []).filter((r: LinkRow) => r.lat != null && r.lon != null);
        setRows(link);
        setIdx(link.length - 1);   // 預設停在終點：一眼看到整趟全貌
        setMeta(d.session ?? null);
        // 航線關聯了任務 → 抓航點疊預計路徑（預計 vs 實際比對）
        if (d.session?.mission_id)
          fetch(`${API}/api/missions/${d.session.mission_id}/waypoints`)
            .then((r) => (r.ok ? r.json() : null))
            .then((m) => m && setPlan(m.waypoints.filter((w: any) => w.lat && w.lon)))
            .catch(() => {});
      })
      .catch(() => {});
    // §5.4 影片中繼資料：video_status 五態全後端算（UI 不做日期運算）
    fetch(`${API}/api/sessions/${sessionId}/video`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setVideo).catch(() => {});
    // 段長未定案（final=false）→ 定期回抓，定案後涵蓋帶依實際長度重畫
    const poll = setInterval(async () => {
      const v: SessionVideo | null = await fetch(`${API}/api/sessions/${sessionId}/video`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      if (!v) return;
      setVideo(v);
      if (v.segments.every((g) => g.final !== false)) clearInterval(poll);
    }, 5000);
    fetch(`${API}/api/events?session_id=${sessionId}`)
      .then((r) => r.json())
      // REST 的 detail 是 JSONB 字串——解析成物件（modal 細節層要用）
      .then((rows) => setEvents(rows.map((e: any) => ({
        ...e,
        detail: typeof e.detail === "string" ? JSON.parse(e.detail) : e.detail ?? {},
      }))))
      .catch(() => {});
    return () => clearInterval(poll);
  }, [sessionId]);

  const [t0, t1] = useMemo(() => {
    if (!rows.length) return [0, 1];
    return [new Date(rows[0].time).getTime(), new Date(rows[rows.length - 1].time).getTime()];
  }, [rows]);

  // 地圖：等資料到才建（要用軌跡範圍 fitBounds、第一點當地面網格中心）
  useEffect(() => {
    if (!rows.length || !containerRef.current || mapRef.current) return;
    const lats = rows.map((r) => r.lat!) , lons = rows.map((r) => r.lon!);
    const first = rows[0];
    const map = new maplibregl.Map({
      container: containerRef.current,
      bounds: [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      fitBoundsOptions: { padding: 90 },
      pitch: 55, maxPitch: 75,
      style: {
        version: 8, sources: {},
        layers: [{ id: "canvas", type: "background", paint: { "background-color": CANVAS } }],
      },
    });
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;

    map.on("load", () => {
      map.addSource("grid", { type: "geojson", data: groundGrid(first.lat!, first.lon!) });
      map.addLayer({ id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#262624", "line-width": 1 } });
      map.addSource("home", { type: "geojson", data: {
        type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [first.lon!, first.lat!] } } });
      map.addLayer({ id: "home-ring", type: "circle", source: "home",
        paint: { "circle-radius": 10, "circle-color": "transparent",
                 "circle-stroke-width": 2, "circle-stroke-color": "#8f8b80" } });

      // 地面投影：中性細線（單機頁identity無歧義，投影只是把 3D 路徑釘回地面）
      map.addSource("track", { type: "geojson", data: {
        type: "FeatureCollection",
        features: [trailLineString(rows.map((r) => ({ lat: r.lat, lon: r.lon })))]
          .filter((f): f is GeoJSON.Feature => f !== null) } });
      map.addLayer({ id: "track", type: "line", source: "track",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#6b7684",
          "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1, 16, 2, 20, 3.5],
          "line-opacity": 0.55 } });

      // 懸浮航跡：deck.gl PathLayer（route-render-tool-eval，取代 fill-extrusion）
      map.addControl(new MapboxOverlay({ interleaved: true, layers: [
        routeLayer("track3d", { track: rows
          .filter((r) => r.lat != null && r.lon != null)
          .map((r) => ({ lat: r.lat!, lon: r.lon!,
                         sinr: r.sinr ?? null, alt: r.alt_rel ?? null })) }),
      ] }) as unknown as maplibregl.IControl);

      // 預計任務路徑（航線開的當下所關聯的任務）：灰絲帶＋地面虛線
      if (plan.length >= 2) {
        map.addSource("plan3d", { type: "geojson",
          data: ribbon(plan.map((w) => ({ lat: w.lat, lon: w.lon, alt: w.alt })), () => ({}), 1.0) });
        map.addLayer({ id: "plan3d", type: "fill-extrusion", source: "plan3d",
          paint: { "fill-extrusion-color": "#8f8b80",
            "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
            "fill-extrusion-opacity": 0.35 } });
        map.addSource("plan-ground", { type: "geojson",
          data: { type: "Feature", properties: {},
            geometry: { type: "LineString", coordinates: plan.map((w) => [w.lon, w.lat]) } } });
        map.addLayer({ id: "plan-ground", type: "line", source: "plan-ground",
          paint: { "line-color": "#8f8b80", "line-width": 1.5,
                   "line-dasharray": [3, 3], "line-opacity": 0.6 } }, "track");
      }

      // 方向箭頭：浮在絲帶上方的小三角形，指出飛行方向
      map.addSource("arrows", { type: "geojson",
        data: pathArrows(rows.map((r) => ({ lat: r.lat, lon: r.lon, alt: r.alt_rel }))) });
      map.addLayer({ id: "arrows", type: "fill-extrusion", source: "arrows",
        paint: { "fill-extrusion-color": "#e8eaed",
          "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.9 } });

      // 回放游標：three.js 球體（與即時頁的機體一致）
      map.addLayer(createDroneLayer("cursor-ball", () => {
        const r = cursorRef.current;
        if (!r || r.lat == null || r.lon == null) return [];
        return [{ id: "cursor", lat: r.lat, lon: r.lon, alt: r.alt_rel ?? 0, color: "#3987e5" }];
      }));
    });
    return () => { map.remove(); mapRef.current = null; };
  }, [rows, plan]);

  // scrub → 球體游標（球體層每幀讀 cursorRef，觸發一次重繪即可）
  useEffect(() => {
    cursorRef.current = rows[idx] ?? null;
    mapRef.current?.triggerRepaint();
  }, [idx, rows]);

  // 播放（ui-spec §5）：1Hz 樣本 → 每 1000/speed ms 前進一格；到底自停
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [speedMenu, setSpeedMenu] = useState(false);
  useEffect(() => {
    if (!playing || rows.length < 2) return;
    const t = setInterval(() => {
      setIdx((i) => {
        if (i >= rows.length - 1) { setPlaying(false); return i; }
        return i + 1;
      });
    }, 1000 / speed);
    return () => clearInterval(t);
  }, [playing, speed, rows.length]);
  // 空白鍵播放/暫停（表單元素聚焦時不攔）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key !== " " || t?.closest("button, input, select, textarea")) return;
      e.preventDefault();
      setPlaying((p) => !p);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // 圖表抽屜（研究工作區 → 展開記憶）
  const [drawerOpen, setDrawerOpen] = useState(false);
  useEffect(() => { setDrawerOpen(localStorage.getItem("replay-drawer-open") === "1"); }, []);

  const cur = rows[idx];

  return (
    <div className="replay">
      {/* 極簡 header（ui-spec §5）：返回＋日期＋任務名，無樣本數 */}
      <div className="replay-head">
        <button className="btn-plain btn-sm" title="返回"
          onClick={() => router.push("/drones")}>←</button>
        <span className="meta">
          {rows.length > 0 && new Date(rows[0].time).toLocaleString("zh-TW",
            { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
              hour12: false })}
          {meta?.mission_name && ` · ${meta.mission_name}`}
          {/* opt-out 留痕（§5.4）：未錄影＝正常態，弱字不宣告 */}
          {video?.video_status === "off" && "　本趟未錄影"}
        </span>
      </div>

      <div className="replay-map" ref={containerRef}>
        {!rows.length && <div className="empty" style={{ padding: 20 }}>載入軌跡中…（若架次無樣本則無可回放）</div>}
      </div>

      {/* §5.4 影片同步窗：時鐘源＝回放 transport，影片跟隨 */}
      {video && rows.length > 1 && cur && (
        <ReplayVideo video={video} rows={rows}
          tCurMs={new Date(cur.time).getTime()}
          playing={playing} speed={speed}
          droneName={meta?.drone_name ?? null}
          droneColor={meta?.drone_id ? colorFor(meta.drone_id) : "#8f8b80"} />
      )}

      {rows.length > 1 && (
        <div className="timeline">
          {/* 播放＋時間軸＋游標處 ▲高度/訊號格；速度收 ⋯（§7 預設） */}
          <div className="scrub-row">
            <button className="btn-plain btn-sm" title="播放/暫停（空白鍵）"
              onClick={() => setPlaying((p) => !p)}>{playing ? "⏸" : "▶"}</button>
            <span className="scrub-wrap">
              <input type="range" min={0} max={rows.length - 1} value={idx}
                     onChange={(e) => { setIdx(Number(e.target.value)); setPlaying(false); }} />
              {/* §5.4 影像涵蓋帶：3px 薄帶、段間空白＝真空白（不拼接）；
                  expired 不顯示（影像已清除，畫涵蓋帶會謊稱資料還在） */}
              {video?.video_status === "available" && video.segments.length > 0 && (
                <span className="vid-cover">
                  {video.segments.map((g) => {
                    const s = new Date(g.started_at).getTime();
                    const e = s + g.duration_s * 1000;
                    const pct = (t: number) => ((t - t0) / (t1 - t0 || 1)) * 100;
                    const L = Math.max(0, pct(s));
                    const R = Math.min(100, pct(e));
                    if (R <= 0 || L >= 100) return null;
                    return (<span key={g.id}>
                      <span style={{ left: `${L}%`, width: `${R - L}%` }} />
                      {/* final=false 尾端：長度未定案——不畫成缺口（那是斷言
                          沒錄到），以處理中樣式延伸到軸末（§5.4） */}
                      {g.final === false && R < 100 && (
                        <span className="cov-proc"
                          style={{ left: `${R}%`, width: `${100 - R}%` }} />
                      )}
                    </span>);
                  })}
                </span>
              )}
            </span>
            <span className="scrub-read">
              {cur && new Date(cur.time).toLocaleTimeString("zh-TW", { hour12: false })}
              　▲{fmt(cur?.alt_rel, 0)}m
              <SignalBars sinr={cur?.sinr} />
            </span>
            <span style={{ position: "relative" }}>
              <button className="btn-plain btn-sm" title="播放速度"
                onClick={() => setSpeedMenu(!speedMenu)}>{speed > 1 ? `${speed}×` : "⋯"}</button>
              {speedMenu && (
                <div className="mcard-menu" style={{ bottom: 34, right: 0 }}>
                  {[1, 4, 8].map((s) => (
                    <button key={s} className="btn-plain btn-sm"
                      onClick={() => { setSpeed(s); setSpeedMenu(false); }}>
                      {s}×{speed === s ? " ✓" : ""}
                    </button>
                  ))}
                </div>
              )}
            </span>
          </div>

          {/* 圖表＝研究工作區 → 上滑抽屜（展開記憶，ui-spec §5） */}
          <details className="replay-drawer" open={drawerOpen}
            onToggle={(e) => {
              const o = e.currentTarget.open;
              setDrawerOpen(o);
              localStorage.setItem("replay-drawer-open", o ? "1" : "0");
            }}>
            <summary>〓 圖表</summary>
            <Chart rows={rows} field="sinr" height={110} yLabel="SINR (dB)"
                   thresholds={[5, -2]} events={events} t0={t0} t1={t1} idx={idx}
                   onSeek={(i) => { setIdx(i); setPlaying(false); }}
                   onEvent={setOpenEv} />
            <Chart rows={rows} field="rtt_ms" height={70} yLabel="RTT (ms)"
                   t0={t0} t1={t1} idx={idx}
                   onSeek={(i) => { setIdx(i); setPlaying(false); }} />
          </details>
        </div>
      )}
      {openEv && (
        <EventModal ev={{ ...openEv, drone: meta?.drone_name ?? null }}
          onClose={() => setOpenEv(null)} />
      )}
    </div>
  );
}
