"use client";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { createDroneLayer } from "@/components/droneLayer";
import { routeLayer } from "@/lib/deckRoute";
import { CANVAS, groundGrid, pathArrows, ribbon, trailLineString } from "@/lib/geo";
import { API, classifySinr } from "@/lib/signal";

interface LinkRow {
  time: string; lat: number | null; lon: number | null; alt_rel: number | null;
  sinr: number | null; rtt_ms: number | null;
}
interface Ev { id: number; time: string; severity: string; type: string }

const fmt = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

/* ── 時序圖：SVG viewBox 1000 寬，preserveAspectRatio none 拉滿容器 ── */
const W = 1000;

function Chart({
  rows, field, height, yLabel, thresholds, events, t0, t1, idx,
}: {
  rows: LinkRow[]; field: "sinr" | "rtt_ms"; height: number; yLabel: string;
  thresholds?: number[]; events?: Ev[]; t0: number; t1: number; idx: number;
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

  return (
    <div className="chart">
      <span className="chart-label">{yLabel}</span>
      <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none">
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
          >
            <title>{e.type}</title>
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
  const [meta, setMeta] = useState<{ mission_id: string | null; mission_name: string | null } | null>(null);
  const [plan, setPlan] = useState<{ lat: number; lon: number; alt: number | null }[]>([]);
  const [events, setEvents] = useState<Ev[]>([]);
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
    fetch(`${API}/api/events?session_id=${sessionId}`)
      .then((r) => r.json()).then(setEvents).catch(() => {});
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
        paint: { "line-color": "#6b7684", "line-width": 2, "line-opacity": 0.55 } });

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

  const cur = rows[idx];
  const curCls = cur?.sinr != null ? classifySinr(cur.sinr) : null;

  return (
    <div className="replay">
      <div className="replay-head">
        <button className="btn-plain" onClick={() => router.push("/drones")}>← 無人機</button>
        <span className="meta">
          航線回放 · {rows.length} 筆樣本
          {meta?.mission_name && ` · 任務：${meta.mission_name}`}
          {rows.length > 0 &&
            ` · ${new Date(rows[0].time).toLocaleString("zh-TW", { hour12: false })}`}
        </span>
      </div>

      <div className="replay-map" ref={containerRef}>
        {!rows.length && <div className="empty" style={{ padding: 20 }}>載入軌跡中…（若架次無樣本則無可回放）</div>}
      </div>

      {rows.length > 1 && (
        <div className="timeline">
          <Chart rows={rows} field="sinr" height={110} yLabel="SINR (dB)"
                 thresholds={[5, -2]} events={events} t0={t0} t1={t1} idx={idx} />
          <Chart rows={rows} field="rtt_ms" height={70} yLabel="RTT (ms)"
                 t0={t0} t1={t1} idx={idx} />
          <div className="scrub-row">
            <input type="range" min={0} max={rows.length - 1} value={idx}
                   onChange={(e) => setIdx(Number(e.target.value))} />
            <span className="scrub-read">
              {cur && new Date(cur.time).toLocaleTimeString("zh-TW", { hour12: false })}
              　SINR {fmt(cur?.sinr)} dB
              {curCls && <span className="dot" style={{ background: curCls.color, marginLeft: 4 }} />}
              　RTT {fmt(cur?.rtt_ms, 0)} ms　高度 {fmt(cur?.alt_rel, 0)} m
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
