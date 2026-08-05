"use client";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { colorFor } from "@/components/droneLayer";
import { CANVAS, groundGrid, ribbon } from "@/lib/geo";
import { API, LINK_CLASSES, classifySinr } from "@/lib/signal";

/** 任務比對回放：同一條路徑的所有航線（跨機、跨時間）疊在同一張 3D 圖。
    編碼同即時頁的約定：空中絲帶＝SINR 分級（研究），地面投影＝機別色（誰飛的）。
    這是「可重複量測」的視覺化——同路徑多次飛行的訊號分布比對。 */

interface Sess {
  id: string; drone_id: string; drone_name: string; started_at: string;
}
interface LinkRow {
  time: string; lat: number | null; lon: number | null;
  alt_rel: number | null; sinr: number | null;
}

const MAX_OVERLAY = 6;   // 疊太多不可讀；超過取最新 6 條並明示

export default function MissionReplay() {
  const { missionId } = useParams<{ missionId: string }>();
  const router = useRouter();
  const [name, setName] = useState("");
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [dropped, setDropped] = useState(0);
  const [tracks, setTracks] = useState<Record<string, LinkRow[]>>({});
  const mapRef = useRef<maplibregl.Map | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [plan, setPlan] = useState<{ lat: number; lon: number; alt: number | null }[]>([]);

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

  useEffect(() => {
    const ids = Object.keys(tracks);
    if (!ids.length || !plan.length || !containerRef.current || mapRef.current) return;
    const all = ids.flatMap((id) => tracks[id]);
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
        paint: { "line-color": "#232a31", "line-width": 1 } });

      // 預計路徑
      map.addSource("plan3d", { type: "geojson",
        data: ribbon(plan.map((w) => ({ lat: w.lat, lon: w.lon, alt: w.alt })), () => ({}), 1.0) });
      map.addLayer({ id: "plan3d", type: "fill-extrusion", source: "plan3d",
        paint: { "fill-extrusion-color": "#8a94a3",
          "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.3 } });
      map.addSource("plan-ground", { type: "geojson",
        data: { type: "Feature", properties: {},
          geometry: { type: "LineString", coordinates: plan.map((w) => [w.lon, w.lat]) } } });
      map.addLayer({ id: "plan-ground", type: "line", source: "plan-ground",
        paint: { "line-color": "#8a94a3", "line-width": 1.5,
                 "line-dasharray": [3, 3], "line-opacity": 0.6 } });

      // 每條航線：機別色地面投影 + SINR 絲帶
      const projFeats: GeoJSON.Feature[] = [];
      const ribbonFeats: GeoJSON.Feature[] = [];
      for (const s of sessions) {
        const rows = tracks[s.id] ?? [];
        const dcolor = colorFor(s.drone_id);
        projFeats.push(...rows.map((r) => ({
          type: "Feature" as const,
          properties: { dcolor },
          geometry: { type: "Point" as const, coordinates: [r.lon!, r.lat!] },
        })));
        ribbonFeats.push(...ribbon(
          rows.map((r) => ({ lat: r.lat, lon: r.lon, alt: r.alt_rel, sinr: r.sinr })),
          (_a, b) => ({ cls: b.sinr == null ? "unknown" : classifySinr(b.sinr).key }),
        ).features);
      }
      map.addSource("proj", { type: "geojson",
        data: { type: "FeatureCollection", features: projFeats } });
      map.addLayer({ id: "proj", type: "circle", source: "proj",
        paint: { "circle-radius": 2.5, "circle-color": ["get", "dcolor"],
                 "circle-opacity": 0.8, "circle-stroke-width": 0 } });
      map.addSource("ribbons", { type: "geojson",
        data: { type: "FeatureCollection", features: ribbonFeats } });
      map.addLayer({ id: "ribbons", type: "fill-extrusion", source: "ribbons",
        paint: { "fill-extrusion-color": ["match", ["get", "cls"],
            ...LINK_CLASSES.flatMap((c) => [c.key, c.color]), "#898781"] as any,
          "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"],
          "fill-extrusion-opacity": 0.75 } });
    });
    return () => { map.remove(); mapRef.current = null; };
  }, [tracks, plan, sessions]);

  return (
    <div className="replay">
      <div className="replay-head">
        <button className="btn-plain" onClick={() => router.push("/missions")}>← 路徑管理</button>
        <span className="meta">
          任務比對回放{name && ` · ${name}`} · {sessions.length} 條航線疊圖
          {dropped > 0 && `（另有 ${dropped} 條較舊未顯示）`}
        </span>
        <span className="spacer" />
        {sessions.map((s) => (
          <button key={s.id} className="chip chip-btn" title="單獨回放這條航線"
            onClick={() => router.push(`/replay/${s.id}`)}>
            <span className="dot" style={{ background: colorFor(s.drone_id) }} />
            {s.drone_name} {new Date(s.started_at).toLocaleTimeString("zh-TW",
              { hour12: false, hour: "2-digit", minute: "2-digit" })}
          </button>
        ))}
      </div>
      <div className="replay-map" ref={containerRef}>
        {!sessions.length && <div className="empty" style={{ padding: 20 }}>載入中，或此路徑尚無已完成的航線</div>}
      </div>
    </div>
  );
}
