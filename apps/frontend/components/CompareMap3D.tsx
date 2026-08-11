"use client";
/** 比較頁 3D 軌跡疊圖（doc/compare-drones-restyle.md §1）：與即時/回放頁
 * 同一套 3D 語言（傾斜視角＋暖畫布＋網格＋起飛點，geo.ts 重用）。
 *
 * 色彩語意（兩色盤不混用原則）：
 *   - 多線比較＝identity——絲帶用航線類別色，與下方圖表同色同序；
 *     圖表「高亮 ≤3」的 dim 集合同步（dim＝muted 細帶）
 *   - 選中單一架次（點絲帶或圖例）＝該絲帶切 SINR 分級色、其餘退 muted——
 *     單線時狀態色才有意義，這是狀態色在本頁唯一的入口
 */
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";

import { CANVAS, groundGrid, ribbon } from "@/lib/geo";
import { LINK_CLASSES, classifySinr } from "@/lib/signal";

const MUTED = "#8f8b80";   // ＝--muted（maplibre 吃不到 CSS 變數）

interface Row { lat: number | null; lon: number | null; [k: string]: unknown }

interface Props {
  wps: { lat: number; lon: number; alt?: number }[];
  loaded: string[];
  tracks: Record<string, Row[]>;
  colorOf: (sid: string) => string;
  labelOf: (sid: string) => string;
  dimIds: string[];
}

export default function CompareMap3D({
  wps, loaded, tracks, colorOf, labelOf, dimIds,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const fittedRef = useRef(false);
  const [sel, setSel] = useState<string | null>(null);
  const selRef = useRef(sel);
  useEffect(() => { selRef.current = sel; }, [sel]);

  useEffect(() => {
    const origin: [number, number] = wps.length
      ? [wps[0].lon, wps[0].lat] : [8.5456, 47.3977];
    const map = new maplibregl.Map({
      container: containerRef.current!,
      center: origin,
      zoom: 15,
      pitch: 55,
      maxPitch: 75,
      style: {
        version: 8,
        sources: {},
        layers: [{ id: "canvas", type: "background",
                   paint: { "background-color": CANVAS } }],
      },
    });
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }),
      "bottom-left");
    mapRef.current = map;

    map.on("load", () => {
      map.resize();   // 保險：容器尺寸若在載入後才定，canvas 跟上
      map.addSource("grid", { type: "geojson", data: groundGrid(origin[1], origin[0]) });
      map.addLayer({ id: "grid", type: "line", source: "grid",
        paint: { "line-color": "#262624", "line-width": 1 } });
      map.addSource("home", { type: "geojson",
        data: { type: "Feature", properties: {},
                geometry: { type: "Point", coordinates: origin } } });
      map.addLayer({ id: "home-ring", type: "circle", source: "home",
        paint: { "circle-radius": 10, "circle-color": "transparent",
                 "circle-stroke-width": 2, "circle-stroke-color": MUTED } });
      map.addLayer({ id: "home-dot", type: "circle", source: "home",
        paint: { "circle-radius": 3, "circle-color": MUTED } });

      // 計畫路徑：灰絲帶＋地面虛線＋航點圈（同即時頁主從關係）
      if (wps.length >= 2) {
        map.addSource("plan3d", { type: "geojson",
          data: ribbon(wps.map((w) => ({ lat: w.lat, lon: w.lon, alt: w.alt ?? 0 })),
                       () => ({}), 1.0) });
        map.addLayer({ id: "plan3d", type: "fill-extrusion", source: "plan3d",
          paint: { "fill-extrusion-color": MUTED,
                   "fill-extrusion-height": ["get", "top"],
                   "fill-extrusion-base": ["get", "base"],
                   "fill-extrusion-opacity": 0.35 } });
        map.addSource("plan-ground", { type: "geojson",
          data: { type: "Feature", properties: {}, geometry: {
            type: "LineString",
            coordinates: wps.map((w) => [w.lon, w.lat]) } } });
        map.addLayer({ id: "plan-ground", type: "line", source: "plan-ground",
          paint: { "line-color": MUTED, "line-width": 1.5,
                   "line-dasharray": [3, 3], "line-opacity": 0.6 } });
      }

      map.addSource("runs", { type: "geojson",
        data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "runs", type: "fill-extrusion", source: "runs",
        paint: { "fill-extrusion-color": ["get", "c"],
                 "fill-extrusion-height": ["get", "top"],
                 "fill-extrusion-base": ["get", "base"],
                 "fill-extrusion-opacity": 0.88 } });

      // 點絲帶＝選中該架次（再點一次或點空白取消）
      map.on("click", (e) => {
        const hit = map.queryRenderedFeatures(e.point, { layers: ["runs"] })[0];
        const sid = hit?.properties?.sid as string | undefined;
        setSel((cur) => (sid ? (cur === sid ? null : sid) : null));
      });
      map.on("mousemove", (e) => {
        const hit = map.queryRenderedFeatures(e.point, { layers: ["runs"] })[0];
        map.getCanvas().style.cursor = hit ? "pointer" : "";
      });
      setReady(true);
    });

    return () => map.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 絲帶資料：loaded/tracks/dim/選中變動時重建（identity ↔ SINR 上色）
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const feats: GeoJSON.Feature[] = [];
    const bounds = new maplibregl.LngLatBounds();
    for (const w of wps) bounds.extend([w.lon, w.lat]);
    for (const sid of loaded) {
      const rows = (tracks[sid] ?? [])
        .filter((r) => r.lat != null && r.lon != null)
        .filter((_, i) => i % 3 === 0)
        .map((r) => ({ lat: r.lat as number, lon: r.lon as number,
                       alt: (r.alt_rel as number | null) ?? 0,
                       sinr: r.sinr as number | null }));
      const active = sel === sid;
      const dim = sel ? !active : dimIds.includes(sid);
      feats.push(...ribbon(rows, (_a, b) => ({
        sid,
        c: active
          ? (b.sinr == null ? MUTED : classifySinr(b.sinr).color)
          : dim ? MUTED : colorOf(sid),
      }), dim ? 0.8 : 1.5).features);
      for (const r of rows) bounds.extend([r.lon, r.lat]);
    }
    (map.getSource("runs") as maplibregl.GeoJSONSource | undefined)
      ?.setData({ type: "FeatureCollection", features: feats });
    if (!fittedRef.current && !bounds.isEmpty()) {
      fittedRef.current = true;
      map.fitBounds(bounds, { padding: 48, pitch: 55, duration: 0 });
    }
  }, [ready, loaded, tracks, dimIds, sel, wps, colorOf]);

  return (
    <div className="cmp3d">
      <div ref={containerRef} className="cmp3d-map" />
      <div className="legend legend-right">
        {sel ? (
          <>
            <h4>SINR 分級 · {labelOf(sel)}</h4>
            {LINK_CLASSES.map((c) => (
              <div className="row" key={c.key}>
                <span className="dot" style={{ background: c.color }} />
                {c.label}
              </div>
            ))}
            <button className="btn-plain btn-sm" onClick={() => setSel(null)}>
              返回航線色
            </button>
          </>
        ) : (
          <>
            <h4>航線</h4>
            {loaded.map((sid) => (
              <button className="legend-row" key={sid} onClick={() => setSel(sid)}>
                <span className="dot" style={{
                  background: dimIds.includes(sid) ? MUTED : colorOf(sid) }} />
                {labelOf(sid)}
              </button>
            ))}
            <div className="hint-line">點航線看 SINR 分級</div>
          </>
        )}
      </div>
    </div>
  );
}
