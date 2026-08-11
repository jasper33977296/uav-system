"use client";
import { MapboxOverlay } from "@deck.gl/mapbox";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { createDroneLayer, DRONE_PALETTE } from "@/components/droneLayer";
import { routeLayer } from "@/lib/deckRoute";
import { CANVAS, groundGrid, ribbon, trailLineString } from "@/lib/geo";
import { API } from "@/lib/signal";

/** 任務視角回放：同一條路徑的一或多條航線（可跨機、跨時間）同步重飛。
 *
 * 時間對齊用**相對時間**（各航線自起飛 t=0 起算）：不同時刻飛的同路徑
 * 才能「一起飛」，同一秒的位置差、訊號差直接可比——重複量測的回放形式。
 *
 * 識別配色以**航線**為單位（同一台機多次飛同路徑時，機別色會全部同色，
 * 分不出是哪一趟）；空中絲帶維持 SINR 分級不變。
 */

interface Sess {
  id: string; drone_id: string; drone_name: string;
  started_at: string; mission_name?: string | null;
}
interface LinkRow {
  time: string; lat: number | null; lon: number | null;
  alt_rel: number | null; sinr: number | null; rtt_ms: number | null;
}

const MAX_OVERLAY = 6;
const W = 1000;
const sessColor = (i: number) => DRONE_PALETTE[i % DRONE_PALETTE.length];

/** 多航線時序圖：x＝相對秒數，每條航線一條線（航線識別色） */
function MultiChart({
  series, field, height, yLabel, thresholds, maxDur, sec,
}: {
  series: { rows: LinkRow[]; t0: number; color: string }[];
  field: "sinr" | "rtt_ms"; height: number; yLabel: string;
  thresholds?: number[]; maxDur: number; sec: number;
}) {
  const vals = series.flatMap((s) => s.rows.map((r) => r[field]))
    .filter((v): v is number => v != null);
  if (!vals.length) return null;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  for (const th of thresholds ?? []) { lo = Math.min(lo, th); hi = Math.max(hi, th); }
  const pad = (hi - lo || 1) * 0.12;
  lo -= pad; hi += pad;
  const x = (t: number) => (t / (maxDur || 1)) * W;
  const y = (v: number) => height - ((v - lo) / (hi - lo)) * height;

  return (
    <div className="chart">
      <span className="chart-label">{yLabel}</span>
      <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none">
        {(thresholds ?? []).map((th) => (
          <line key={th} x1={0} x2={W} y1={y(th)} y2={y(th)}
                stroke="var(--muted)" strokeWidth="1" strokeDasharray="4 4"
                vectorEffect="non-scaling-stroke" opacity={0.5} />
        ))}
        {series.map((s, i) => (
          <polyline key={i} fill="none" stroke={s.color} strokeWidth="1.3"
            vectorEffect="non-scaling-stroke" opacity={0.9}
            points={s.rows
              .filter((r) => r[field] != null)
              .map((r) => `${x((new Date(r.time).getTime() - s.t0) / 1000)},${y(r[field] as number)}`)
              .join(" ")} />
        ))}
        <line x1={x(sec)} x2={x(sec)} y1={0} y2={height} stroke="var(--ink)"
              strokeWidth="1" vectorEffect="non-scaling-stroke" opacity={0.55} />
      </svg>
    </div>
  );
}

export default function MissionReplay() {
  const { missionId } = useParams<{ missionId: string }>();
  const router = useRouter();
  const [name, setName] = useState("");
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [dropped, setDropped] = useState(0);
  const [tracks, setTracks] = useState<Record<string, LinkRow[]>>({});
  const [plan, setPlan] = useState<{ lat: number; lon: number; alt: number | null }[]>([]);
  const [sec, setSec] = useState(0);
  const secRef = useRef(0);
  // 顯隱切換：六條全開會互相蓋，讓使用者自己決定比對哪幾條
  const [hidden, setHidden] = useState<string[]>([]);
  const hiddenRef = useRef<string[]>([]);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch(`${API}/api/missions/${missionId}/waypoints`)
      .then((r) => (r.ok ? r.json() : null))
      .then((m) => m && setPlan(m.waypoints.filter((w: any) => w.lat && w.lon)))
      .catch(() => {});
    fetch(`${API}/api/sessions?mission_id=${missionId}&limit=100`)
      .then((r) => r.json())
      .then(async (rows: any[]) => {
        const done = rows.filter((r) => r.ended_at);
        setDropped(Math.max(0, done.length - MAX_OVERLAY));
        const take = done.slice(0, MAX_OVERLAY);
        setSessions(take);
        if (take[0]?.mission_name) setName(take[0].mission_name);
        const result: Record<string, LinkRow[]> = {};
        for (const s of take) {
          const d = await fetch(`${API}/api/sessions/${s.id}/track`).then((r) => r.json());
          result[s.id] = (d.link ?? []).filter((r: LinkRow) => r.lat != null && r.lon != null);
        }
        setTracks(result);
      })
      .catch(() => {});
  }, [missionId]);

  // 每條航線的相對時間基準與總長
  const series = useMemo(() =>
    sessions
      .map((s, i) => {
        const rows = tracks[s.id] ?? [];
        return { sess: s, rows, color: sessColor(i),
                 t0: rows.length ? new Date(rows[0].time).getTime() : 0 };
      })
      .filter((s) => s.rows.length > 1),
    [sessions, tracks]);
  // 軸長按**可見**航線算：隱藏一條長趟後，剩下的應填滿時間軸
  const maxDur = useMemo(() =>
    Math.max(1, ...series
      .filter((s) => !hidden.includes(s.sess.id))
      .map((s) => (new Date(s.rows[s.rows.length - 1].time).getTime() - s.t0) / 1000)),
    [series, hidden]);

  function projData(ser: typeof series, hid: string[]): GeoJSON.FeatureCollection {
    return { type: "FeatureCollection",
      features: ser
        .filter((s) => !hid.includes(s.sess.id))
        .map((s) => trailLineString(
          s.rows.map((r) => ({ lat: r.lat, lon: r.lon })), { dcolor: s.color }))
        .filter((f): f is GeoJSON.Feature => f !== null) };
  }
  // 空中航跡改 deck.gl PathLayer（route-render-tool-eval）：每航線一組尾跡
  function ribbonTrails(ser: typeof series, hid: string[]) {
    return Object.fromEntries(ser
      .filter((s) => !hid.includes(s.sess.id))
      .map((s) => [s.sess.id, s.rows
        .filter((r) => r.lat != null && r.lon != null)
        .map((r) => ({ lat: r.lat!, lon: r.lon!,
                       sinr: r.sinr ?? null, alt: r.alt_rel ?? null }))]));
  }

  /** 相對秒數 → 該航線當下樣本（1Hz 資料，時間比對取下界） */
  function rowAt(s: (typeof series)[number], t: number): LinkRow {
    const idx = Math.min(s.rows.length - 1, Math.max(0, Math.round(t)));
    return s.rows[idx];
  }

  useEffect(() => {
    if (!series.length || !plan.length || !containerRef.current || mapRef.current) return;
    const all = series.flatMap((s) => s.rows);
    const lats = all.map((r) => r.lat!), lons = all.map((r) => r.lon!);
    const map = new maplibregl.Map({
      container: containerRef.current,
      bounds: [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      fitBoundsOptions: { padding: 90 },
      pitch: 55, maxPitch: 75,
      style: { version: 8, sources: {},
        layers: [{ id: "canvas", type: "background", paint: { "background-color": CANVAS } }] },
    });
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
    mapRef.current = map;

    map.on("load", () => {
      map.addSource("grid", { type: "geojson", data: groundGrid(plan[0].lat, plan[0].lon) });
      map.addLayer({ id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#262624", "line-width": 1 } });

      map.addSource("plan3d", { type: "geojson",
        data: ribbon(plan.map((w) => ({ lat: w.lat, lon: w.lon, alt: w.alt })), () => ({}), 1.0) });
      map.addLayer({ id: "plan3d", type: "fill-extrusion", source: "plan3d",
        paint: { "fill-extrusion-color": "#8f8b80",
          "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.3 } });
      map.addSource("plan-ground", { type: "geojson",
        data: { type: "Feature", properties: {},
          geometry: { type: "LineString", coordinates: plan.map((w) => [w.lon, w.lat]) } } });
      map.addLayer({ id: "plan-ground", type: "line", source: "plan-ground",
        paint: { "line-color": "#8f8b80", "line-width": 1.5,
                 "line-dasharray": [3, 3], "line-opacity": 0.6 } });

      // 各航線：航線色地面投影線（去顆粒）+ SINR 絲帶
      map.addSource("proj", { type: "geojson", data: projData(series, hiddenRef.current) });
      map.addLayer({ id: "proj", type: "line", source: "proj",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-width": 2, "line-color": ["get", "dcolor"], "line-opacity": 0.7 } });
      const overlay = new MapboxOverlay({ interleaved: true, layers: [
        routeLayer("ribbons", ribbonTrails(series, hiddenRef.current)),
      ] });
      map.addControl(overlay as unknown as maplibregl.IControl);
      overlayRef.current = overlay;

      // 回放游標：每條可見航線一顆球，沿相對時間同步前進
      map.addLayer(createDroneLayer("cursors", () =>
        series
          .filter((s) => !hiddenRef.current.includes(s.sess.id))
          .map((s) => {
            const r = rowAt(s, secRef.current);
            return { id: s.sess.id, lat: r.lat!, lon: r.lon!,
                     alt: r.alt_rel ?? 0, color: s.color };
          })));
    });
    return () => { map.remove(); mapRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series, plan]);

  useEffect(() => {
    secRef.current = sec;
    mapRef.current?.triggerRepaint();
  }, [sec]);

  useEffect(() => {
    hiddenRef.current = hidden;
    const map = mapRef.current;
    if (!map || !map.getSource("proj")) return;
    (map.getSource("proj") as maplibregl.GeoJSONSource).setData(projData(series, hidden));
    overlayRef.current?.setProps({
      layers: [routeLayer("ribbons", ribbonTrails(series, hidden))] });
    map.triggerRepaint();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hidden, series]);

  const mmss = (t: number) => `${Math.floor(t / 60)}:${Math.floor(t % 60).toString().padStart(2, "0")}`;

  return (
    <div className="replay">
      <div className="replay-head">
        <button className="btn-plain" onClick={() => router.push("/missions")}>← 路徑管理</button>
        <span className="meta">
          任務回放{name && ` · ${name}`} · {series.length} 條航線同步重飛（相對時間）
          {dropped > 0 && `（另有 ${dropped} 條較舊未顯示）`}
        </span>
        <span className="spacer" />
        {series.map((s) => {
          const off = hidden.includes(s.sess.id);
          return (
            <button key={s.sess.id}
              className={`chip chip-btn ${off ? "chip-off" : ""}`}
              title={off ? "顯示這條航線" : "隱藏這條航線"}
              onClick={() => setHidden(off
                ? hidden.filter((x) => x !== s.sess.id)
                : [...hidden, s.sess.id])}>
              <span className="dot" style={{ background: s.color }} />
              {s.sess.drone_name} {new Date(s.sess.started_at).toLocaleTimeString("zh-TW",
                { hour12: false, hour: "2-digit", minute: "2-digit" })}
              <span className="chip-link" title="單獨回放"
                onClick={(e) => { e.stopPropagation(); router.push(`/replay/${s.sess.id}`); }}>
                ↗
              </span>
            </button>
          );
        })}
      </div>

      <div className="replay-map" ref={containerRef}>
        {!series.length && <div className="empty" style={{ padding: 20 }}>載入中，或此路徑尚無已完成的航線</div>}
      </div>

      {series.length > 0 && (
        <div className="timeline">
          <MultiChart series={series.filter((s) => !hidden.includes(s.sess.id))}
                      field="sinr" height={110} yLabel="SINR (dB)"
                      thresholds={[5, -2]} maxDur={maxDur} sec={sec} />
          <MultiChart series={series.filter((s) => !hidden.includes(s.sess.id))}
                      field="rtt_ms" height={70} yLabel="RTT (ms)"
                      maxDur={maxDur} sec={sec} />
          <div className="scrub-row">
            <input type="range" min={0} max={Math.ceil(maxDur)} value={Math.min(sec, Math.ceil(maxDur))}
                   onChange={(e) => setSec(Number(e.target.value))} />
            <span className="scrub-read">
              T+{mmss(sec)} / {mmss(maxDur)}
              {series.filter((s) => !hidden.includes(s.sess.id)).map((s) => {
                const r = rowAt(s, sec);
                return (
                  <span key={s.sess.id} style={{ marginLeft: 10 }}>
                    <span className="dot" style={{ background: s.color, marginRight: 3 }} />
                    {r.sinr?.toFixed(1) ?? "—"}dB
                  </span>
                );
              })}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
