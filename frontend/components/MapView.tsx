"use client";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef } from "react";

import { API, LINK_CLASSES, classifySinr } from "@/lib/signal";
import { useUavStore } from "@/lib/store";

const HOME: [number, number] = [8.5456, 47.3977]; // PX4 SITL 預設起飛點

/** 以中心點/半徑產生圓形 polygon（干擾區顯示用） */
function circlePolygon(lat: number, lon: number, radiusM: number): GeoJSON.Feature {
  const pts: [number, number][] = [];
  for (let i = 0; i <= 48; i++) {
    const a = (i / 48) * 2 * Math.PI;
    pts.push([
      lon + ((radiusM * Math.cos(a)) / 111320) / Math.cos((lat * Math.PI) / 180),
      lat + (radiusM * Math.sin(a)) / 110574,
    ]);
  }
  return { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [pts] } };
}

export default function MapView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const centeredRef = useRef(false);

  useEffect(() => {
    const map = new maplibregl.Map({
      container: containerRef.current!,
      center: HOME,
      zoom: 15,
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors",
          },
        },
        layers: [{ id: "osm", type: "raster", source: "osm" }],
      },
    });
    mapRef.current = map;

    map.on("load", async () => {
      // 干擾區與基地台（靜態設定，由 API 載入）
      const [zones, cells] = await Promise.all([
        fetch(`${API}/api/zones`).then((r) => r.json()).catch(() => []),
        fetch(`${API}/api/cells`).then((r) => r.json()).catch(() => []),
      ]);

      map.addSource("zones", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: zones
            .filter((z: any) => z.enabled)
            .map((z: any) => circlePolygon(z.center_lat, z.center_lon, z.radius_m)),
        },
      });
      map.addLayer({
        id: "zones-fill", type: "fill", source: "zones",
        paint: { "fill-color": "#d03b3b", "fill-opacity": 0.1 },
      });
      map.addLayer({
        id: "zones-line", type: "line", source: "zones",
        paint: { "line-color": "#d03b3b", "line-width": 2, "line-dasharray": [2, 2] },
      });

      map.addSource("cells", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: cells.map((c: any) => ({
            type: "Feature",
            properties: { name: `${c.name} (PCI ${c.pci})` },
            geometry: { type: "Point", coordinates: [c.lon, c.lat] },
          })),
        },
      });
      map.addLayer({
        id: "cells-pt", type: "circle", source: "cells",
        paint: {
          "circle-radius": 6, "circle-color": "#2a78d6",
          "circle-stroke-width": 2, "circle-stroke-color": "#fcfcfb",
        },
      });

      // 飛行軌跡：依鏈路健康分級上色（門檻同 backend 事件門檻）
      map.addSource("trail", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "trail-pt", type: "circle", source: "trail",
        paint: {
          "circle-radius": 4,
          "circle-color": [
            "match", ["get", "cls"],
            ...LINK_CLASSES.flatMap((c) => [c.key, c.color]),
            "#898781",
          ] as any,
          "circle-stroke-width": 1,
          "circle-stroke-color": "rgba(252,252,251,0.6)",
        },
      });
    });

    return () => map.remove();
  }, []);

  // 即時更新：無人機位置 marker + 軌跡
  useEffect(
    () =>
      useUavStore.subscribe((s) => {
        const map = mapRef.current;
        const t = s.live;
        if (!map || !t || t.lat == null || t.lon == null) return;

        if (!markerRef.current) {
          const el = document.createElement("div");
          el.className = "drone-marker";
          markerRef.current = new maplibregl.Marker({ element: el, rotationAlignment: "map" })
            .setLngLat([t.lon, t.lat])
            .addTo(map);
        }
        markerRef.current.setLngLat([t.lon, t.lat]).setRotation(t.heading ?? 0);

        if (!centeredRef.current) {
          centeredRef.current = true;
          map.jumpTo({ center: [t.lon, t.lat], zoom: 16 });
        }

        const src = map.getSource("trail") as maplibregl.GeoJSONSource | undefined;
        src?.setData({
          type: "FeatureCollection",
          features: s.trail.map((p) => ({
            type: "Feature",
            properties: { cls: p.sinr == null ? "unknown" : classifySinr(p.sinr).key },
            geometry: { type: "Point", coordinates: [p.lon, p.lat] },
          })),
        });
      }),
    []
  );

  return (
    <div className="map-wrap">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      <div className="legend">
        <h4>鏈路品質（SINR）</h4>
        {LINK_CLASSES.map((c) => (
          <div className="row" key={c.key}>
            <span className="dot" style={{ background: c.color }} />
            {c.label}
          </div>
        ))}
        <div className="row">
          <span className="dot" style={{ background: "#d03b3b", opacity: 0.35 }} />
          干擾區
          <span className="dot" style={{ background: "#2a78d6" }} />
          gNB 基地台
        </div>
      </div>
    </div>
  );
}
