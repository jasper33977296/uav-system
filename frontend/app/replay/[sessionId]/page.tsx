"use client";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { CANVAS, groundGrid, ngonAt, ribbon } from "@/lib/geo";
import { API, LINK_CLASSES, classifySinr } from "@/lib/signal";

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
            fill={e.severity === "critical" ? "#a01818" : e.severity === "warning" ? "#fab219" : "#898781"}
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
  const [events, setEvents] = useState<Ev[]>([]);
  const [idx, setIdx] = useState(0);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch(`${API}/api/sessions/${sessionId}/track`)
      .then((r) => r.json())
      .then((d) => {
        const link = (d.link ?? []).filter((r: LinkRow) => r.lat != null && r.lon != null);
        setRows(link);
        setIdx(link.length - 1);   // 預設停在終點：一眼看到整趟全貌
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
        paint: { "line-color": "#232a31", "line-width": 1 } });
      map.addSource("home", { type: "geojson", data: {
        type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [first.lon!, first.lat!] } } });
      map.addLayer({ id: "home-ring", type: "circle", source: "home",
        paint: { "circle-radius": 10, "circle-color": "transparent",
                 "circle-stroke-width": 2, "circle-stroke-color": "#898781" } });

      const cls = (r: LinkRow) => (r.sinr == null ? "unknown" : classifySinr(r.sinr).key);
      map.addSource("track", { type: "geojson", data: {
        type: "FeatureCollection",
        features: rows.map((r) => ({
          type: "Feature", properties: { cls: cls(r) },
          geometry: { type: "Point", coordinates: [r.lon!, r.lat!] },
        })) } });
      map.addLayer({ id: "track", type: "circle", source: "track",
        paint: { "circle-radius": 2.5, "circle-opacity": 0.45,
          "circle-color": ["match", ["get", "cls"],
            ...LINK_CLASSES.flatMap((c) => [c.key, c.color]), "#898781"] as any,
          "circle-stroke-width": 0 } });

      // 懸浮絲帶：路徑本身浮在飛行高度（地面另有投影點）
      map.addSource("track3d", { type: "geojson",
        data: ribbon(
          rows.map((r) => ({ lat: r.lat, lon: r.lon, alt: r.alt_rel, sinr: r.sinr })),
          (_a, b) => ({ cls: b.sinr == null ? "unknown" : classifySinr(b.sinr).key }),
        ) });
      map.addLayer({ id: "track3d", type: "fill-extrusion", source: "track3d",
        paint: { "fill-extrusion-color": ["match", ["get", "cls"],
            ...LINK_CLASSES.flatMap((c) => [c.key, c.color]), "#898781"] as any,
          "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.9 } });

      // 回放游標：懸浮在該時刻高度的機體
      map.addSource("cursor", { type: "geojson",
        data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "cursor", type: "fill-extrusion", source: "cursor",
        paint: { "fill-extrusion-color": "#3987e5",
          "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.95 } });
    });
    return () => { map.remove(); mapRef.current = null; };
  }, [rows]);

  // scrub → 地圖游標
  useEffect(() => {
    const map = mapRef.current, r = rows[idx];
    if (!map || !r || !map.getSource("cursor")) return;
    const alt = r.alt_rel ?? 0, rad = 3.2;
    (map.getSource("cursor") as maplibregl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features: [{ type: "Feature",
        properties: { base: Math.max(alt - rad, 0), top: Math.max(alt + rad, rad) },
        geometry: ngonAt(r.lat!, r.lon!, rad, 8) }],
    });
  }, [idx, rows]);

  const cur = rows[idx];
  const curCls = cur?.sinr != null ? classifySinr(cur.sinr) : null;

  return (
    <div className="replay">
      <div className="replay-head">
        <button className="btn-plain" onClick={() => router.push("/drones")}>← 無人機</button>
        <span className="meta">
          架次回放 · {rows.length} 筆樣本
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
